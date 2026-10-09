/* DOM-level test of the redesigned skip-object UX (jsdom).
 *
 * The Control tab only summarises and opens the skip modal; the modal renders
 * one toggle per object (green keep / red pending / grey locked), and "Skip
 * selected" posts the whole pending set in one request. A skip can never be
 * undone. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
const doc = window.document;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
window.WebSocket = class { constructor() { window.__ws = this; } close() {} send() {} };

const FLEET = [
  { id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'SN', bed_type: 'cool_plate', ams_count: 1 }
];

const calls = [];
window.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  calls.push({ url: String(url), method, body: opts.body });
  let payload = {};
  if (String(url).endsWith('/api/printers') && method === 'GET') payload = FLEET;
  else if (String(url).indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  else if (String(url).indexOf('/api/uploads') >= 0) payload = [];
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.11.0') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const card = () => doc.querySelector('[data-card-id]');
const panel = () => card() && card().querySelector('[data-r="panel"]');
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
const skipPost = () => calls.find((c) => c.url.indexOf('/skip-objects') >= 0 && c.method === 'POST');

/* --- telemetry still builds the live object list ------------------------- */

const base = {
  id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'S', status: 'running',
  progress: 40, objects: [], objectIndex: null, skippedIds: []
};

const p = Object.assign({}, base);
window.applyTelemetry(p, {
  gcode_state: 'RUNNING',
  subtask_name: 'Cube_plate_1.gcode.3mf',
  mc_print_sub_stage: 2,
  job: {
    cur_stage: { idx: 2, state: 0 },
    stage: [
      { idx: 92, name: 'Phone stand', tool: ['HX00'], est_time: 20 },
      { idx: 191, name: 'Cable clip', tool: ['HX00'], est_time: 8 },
      { idx: 192, name: 'Name tag', tool: ['HX00'], est_time: 5 },
      { idx: 193, name: 'Bracket', tool: ['HX00'], est_time: 12 }
    ]
  }
});

check('object count from job.stage', p.objects.length === 4, p.objects.length);
check('object names kept', p.objects[1].name === 'Cable clip', p.objects[1].name);
check('current object index', p.objectIndex === 2, p.objectIndex);

/* --- Control tab: summary + modal entry, no direct skip ------------------- */

const markup = window.controlHtml(p);
check('shows the current object position', markup.includes('Object 3 of 4'), '');
check('offers the skip modal', markup.includes('Skip objects'), '');
check('the entry point is a button', markup.includes('data-action="open-skip"'), '');
check('no direct skip targets remain', !markup.includes('data-action="skip"'), '');
check('Skip current / Skip next are gone',
  !markup.includes('Skip current') && !markup.includes('Skip next'), '');
check('still describes the flow', markup.includes('keeps the rest'), '');

const empty = window.controlHtml(Object.assign({}, base, { status: 'idle' }));
check('no list -> explains itself', empty.includes('no object list'));
check('no list -> no skip targets', !empty.includes('data-action="skip"'));

/* --- modal flow driven through the DOM ------------------------------------ */

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await sleep(40);
  check('a printer is mounted', Boolean(card()));
  check('control tab has no direct skip targets', !/data-action="skip"/.test(panel().innerHTML));

  window.initWebSocket();
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: {
        gcode_state: 'RUNNING', subtask_name: 'Cube_plate_1.gcode.3mf', mc_percent: 40,
        job: {
          cur_stage: { idx: 2 },
          stage: [
            { idx: 92, name: 'Phone stand' }, { idx: 191, name: 'Cable clip' },
            { idx: 192, name: 'Name tag' }, { idx: 193, name: 'Bracket' }
          ]
        }
      }
    })
  });
  await sleep(30);
  check('control tab has the skip button', panel().innerHTML.includes('data-action="open-skip"'),
    panel().innerHTML.slice(0, 120));

  // opening from the Control tab button
  click(panel().querySelector('[data-action="open-skip"]'));
  await sleep(30);
  check('skip modal opens from the Control tab',
    !doc.getElementById('modal-skip').classList.contains('hidden'));

  const items = () => [...doc.querySelectorAll('#skip-bed [data-skip-id]')];
  check('one toggle per object', items().length === 4, String(items().length));
  check('normal objects are green/keep',
    items().every((el) => el.getAttribute('class').indexOf('is-todo') >= 0),
    items().map((el) => el.getAttribute('class')).join(','));
  check('confirm is inert with nothing selected', doc.getElementById('skip-confirm').disabled);

  // toggle one on -> pending / red
  click(doc.querySelector('#skip-bed [data-skip-id="191"]'));
  await sleep(10);
  check('selected object turns pending/red',
    doc.querySelector('#skip-bed [data-skip-id="191"]').getAttribute('class').indexOf('is-pending') >= 0);
  check('confirm reflects the pending count',
    doc.getElementById('skip-confirm').textContent.includes('(1)'), doc.getElementById('skip-confirm').textContent);
  check('confirm arms with a selection', !doc.getElementById('skip-confirm').disabled);

  // toggle it off again
  click(doc.querySelector('#skip-bed [data-skip-id="191"]'));
  await sleep(10);
  check('clicking a pending object unmarks it',
    doc.querySelector('#skip-bed [data-skip-id="191"]').getAttribute('class').indexOf('is-todo') >= 0);
  check('confirm disarms when nothing is pending', doc.getElementById('skip-confirm').disabled);

  // select two and confirm
  click(doc.querySelector('#skip-bed [data-skip-id="191"]'));
  click(doc.querySelector('#skip-bed [data-skip-id="192"]'));
  await sleep(10);
  calls.length = 0;
  click(doc.getElementById('skip-confirm'));
  await sleep(40);

  const sent = skipPost();
  let ids = null;
  try { ids = sent ? JSON.parse(sent.body).object_ids : null; } catch (_) { ids = null; }
  check('one request carries exactly the selected ids',
    Boolean(ids) && ids.length === 2 && ids.indexOf(191) >= 0 && ids.indexOf(192) >= 0 && ids.indexOf(92) < 0,
    JSON.stringify(ids));
  check('exactly one skip request is sent',
    calls.filter((c) => c.url.indexOf('/skip-objects') >= 0).length === 1,
    String(calls.filter((c) => c.url.indexOf('/skip-objects') >= 0).length));
  check('modal closes after a successful skip',
    doc.getElementById('modal-skip').classList.contains('hidden'));

  // reopening shows the skipped objects locked out
  window.openSkipModal('n1', { path: '/Cube_plate_1.gcode.3mf', plate: 1 });
  await sleep(20);
  const skippedRect = doc.querySelector('#skip-bed [data-skip-id="191"]');
  check('a skipped object is greyed', skippedRect.getAttribute('class').indexOf('is-skipped') >= 0,
    skippedRect.getAttribute('class'));
  check('a skipped object is disabled',
    skippedRect.disabled === true || skippedRect.getAttribute('data-disabled') === '1',
    skippedRect.getAttribute('data-disabled'));

  // clicking a locked object can never unskip it
  calls.length = 0;
  click(skippedRect);
  await sleep(10);
  check('a skipped object cannot be reselected',
    doc.querySelector('#skip-bed [data-skip-id="191"]').getAttribute('class').indexOf('is-skipped') >= 0);
  check('a skipped object does not arm confirm', doc.getElementById('skip-confirm').disabled);
  check('clicking a skipped object sends nothing', !skipPost());

  // cancel never posts
  click(doc.querySelector('#skip-bed [data-skip-id="193"]'));
  await sleep(10);
  calls.length = 0;
  click(doc.getElementById('skip-cancel'));
  await sleep(10);
  check('cancel sends nothing', !skipPost());
  check('cancel closes the modal', doc.getElementById('modal-skip').classList.contains('hidden'));

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });
