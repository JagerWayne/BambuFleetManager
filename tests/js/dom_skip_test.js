/* DOM-level test of the skip-object list and its live refresh (jsdom). */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.WebSocket = class { close() {} send() {} };
window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('1.1.0') });
window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };

const base = {
  id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'S', status: 'running',
  progress: 40, objects: [], objectIndex: null, skippedIds: []
};

/* --- the printer reports job.stage[] + job.cur_stage.idx ------------------ */

const p = Object.assign({}, base);
window.applyTelemetry(p, {
  gcode_state: 'RUNNING',
  subtask_name: 'plate.gcode.3mf',
  mc_print_sub_stage: 2,
  job: {
    cur_stage: { idx: 2, state: 0 },
    stage: [
      { idx: 0, name: '', tool: ['HX00'], est_time: 12 },
      { idx: 1, name: 'Phone stand', tool: ['HX00'], est_time: 20 },
      { idx: 2, name: 'Cable clip', tool: ['HX00'], est_time: 8 },
      { idx: 3, name: 'Name tag', tool: ['HX00'], est_time: 5 }
    ]
  }
});

check('object count from job.stage', p.objects.length === 4, p.objects.length);
check('object names kept', p.objects[1].name === 'Phone stand', p.objects[1].name);
check('unnamed objects fall back to a number', p.objects[0].name === '', JSON.stringify(p.objects[0]));
check('current object index', p.objectIndex === 2, p.objectIndex);

/* --- the rendered panel names the object that will be skipped ------------- */

const markup = window.controlHtml(p);
check('shows the current object position', markup.includes('Object 3 of 4'), '');
check('lists every object row', (markup.match(/data-action="skip"/g) || []).length >= 6,
  (markup.match(/data-action="skip"/g) || []).length);
check('marks the printing object', markup.includes('is-current'));
check('marks the object that is next up', markup.includes('is-next'));
check('marks already-printed objects', markup.includes('is-done'));
check('shows the slice object name', markup.includes('Phone stand'));
check('"Skip next" targets object id 3', markup.includes('data-value="3"'));
check('still describes the protocol', markup.includes('object id'));

/* --- no object list: say so instead of inventing a grid ------------------- */

const empty = window.controlHtml(Object.assign({}, base, { status: 'idle' }));
check('no list -> explains itself', empty.includes('no object list'));
check('no list -> no skip rows', !empty.includes('data-action="skip"'));

/* --- not printing: rows are disabled, buttons inert ---------------------- */

const idle = window.controlHtml(Object.assign({}, p, { status: 'idle', objects: p.objects, objectIndex: 2 }));
check('idle job disables the rows', idle.includes('data-action="skip" data-value="2" disabled'));
check('idle job disables Skip current', idle.includes('data-action="skip" data-value="2" disabled'));

/* --- queued skips show up, and drop off once the printer passes them ------ */

const queued = Object.assign({}, p, { skippedIds: [3] });
const queuedMarkup = window.controlHtml(queued);
check('queued skip is marked', queuedMarkup.includes('skip queued'));

const body = window.document.createElement('div');
body.innerHTML = window.controlHtml(p);
const before = body.querySelector('[data-live="skip-objects"]').dataset.sig;

// telemetry advances by one object -> the list must follow
const p2 = Object.assign({}, p);
window.applyTelemetry(p2, {
  gcode_state: 'RUNNING',
  job: { cur_stage: { idx: 3 }, stage: p.objects.map((o) => ({ idx: o.id, name: o.name })) }
});
window.refreshLive(body, p2);
const after = body.querySelector('[data-live="skip-objects"]').dataset.sig;
check('signature changes as the printer advances', before !== after, `${before} -> ${after}`);
check('panel now shows object 4 of 4', body.innerHTML.includes('Object 4 of 4'), '');
check('queued skip is pruned once passed', (p2.skippedIds || []).length === 0, JSON.stringify(p2.skippedIds));

/* --- s_obj-only firmware still produces a usable list -------------------- */

const sObjOnly = Object.assign({}, base);
window.applyTelemetry(sObjOnly, { gcode_state: 'RUNNING', s_obj: [0, 1, 2], mc_print_sub_stage: 1 });
check('s_obj fallback builds a list', sObjOnly.objects.length === 3, sObjOnly.objects.length);
check('s_obj fallback keeps ids', sObjOnly.objects[2].id === 2, sObjOnly.objects[2].id);

/* --- the card puts the name and status above the camera ------------------ */

const root = window.document.createElement('div');
window.document.body.appendChild(root);
const card = window.createCard({ id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'SN' });
root.appendChild(card.root);

const order = [...card.root.querySelectorAll('[data-r]')].map((n) => n.dataset.r);
const nameNode = card.root.querySelector('[data-r="name"]');
const camNode = card.root.querySelector('[data-role="cam-box"]');
check('card still exposes the name ref', order.indexOf('name') >= 0, order.join(','));
check('name/status render before the camera in document order',
  nameNode.compareDocumentPosition(camNode) === window.Node.DOCUMENT_POSITION_FOLLOWING,
  `${nameNode.dataset.r} -> cam-box`);
check('camera still inside the card', Boolean(camNode));
check('status chip still present', Boolean(card.root.querySelector('[data-r="chip"]')));
check('one card header only', card.root.querySelectorAll('[data-r="name"]').length === 1);

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);