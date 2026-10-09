/* DOM-level test of the skip modal's top-down bed, the print-preview entry
 * point and the fullscreen camera HUD (jsdom).
 *
 * The bed fetches plate geometry once per (path, plate) and draws one
 * tappable toggle per object; the print modal's "Skip objects…" button always
 * opens the modal, which itself enforces the running-job requirement. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const utilJs = fs.readFileSync('static/js/lib/util.js', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
const doc = window.document;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
window.WebSocket = class { constructor() { window.__ws = this; } close() {} send() {} };

const GEOM = {
  path: '/Cube_plate_1.gcode.3mf', plate: 1, bed: [256, 256], bbox_all: [45, 45, 200, 200],
  // id is the slice_info identify_id the skip command needs; plate_id is the
  // plate_N.json bbox id (the two lists are in the same slicer order).
  objects: [60, 112, 134, 156, 178].map((id, i) => ({
    id, plate_id: [92, 191, 192, 193, 196][i], name: ['A', 'B', 'C', 'D', 'E'][i],
    bbox: [10 * i, 10 * i, 10 * i + 8, 10 * i + 8]
  }))
};
const PLAN = {
  name: 'Cube', path: '/Cube_plate_1.gcode.3mf',
  plates: [{ index: 1, filaments: [], time_sec: 0, weight_g: 0 }], trays: [], external: null
};
const FLEET = [{ id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'SN', bed_type: 'cool_plate', ams_count: 1 }];

const calls = [];
window.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const u = String(url);
  calls.push({ url: u, method, body: opts.body });
  let payload = {};
  if (u.indexOf('/files/objects') >= 0) payload = GEOM;
  else if (u.indexOf('/files/plan') >= 0) payload = PLAN;
  else if (u.endsWith('/api/printers') && method === 'GET') payload = FLEET;
  else if (u.indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  else if (u.indexOf('/api/uploads') >= 0) payload = [];
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.11.0') });
};

window.eval(utilJs);
window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const objectCalls = () => calls.filter((c) => c.url.indexOf('/files/objects') >= 0).length;
const skipPosts = () => calls.filter((c) => c.url.indexOf('/skip-objects') >= 0 && c.method === 'POST');
const panel = () => {
  const c = doc.querySelector('[data-card-id]');
  return c && c.querySelector('[data-r="panel"]');
};

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await sleep(40);
  window.initWebSocket();
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: {
        gcode_state: 'RUNNING', subtask_name: 'Cube_plate_1.gcode.3mf', mc_percent: 40,
        job: {
          cur_stage: { idx: 2 },
          stage: [
            { idx: 92, name: 'A' }, { idx: 191, name: 'B' }, { idx: 192, name: 'C' },
            { idx: 193, name: 'D' }, { idx: 196, name: 'E' }
          ]
        }
      }
    })
  });
  await sleep(30);

  // The Control tab now pre-fetches the plate geometry for the job; opening
  // the modal must reuse that cache, not fetch again.
  const geomCalls = objectCalls();
  window.openSkipModal('n1', { path: '/Cube_plate_1.gcode.3mf', plate: 1 });
  await sleep(30);

  const bed = doc.getElementById('skip-bed');
  check('bed svg rendered', Boolean(bed.querySelector('svg.bed-svg')));
  const rects = [...bed.querySelectorAll('rect.bed-obj')];
  check('one rect per object', rects.length === 5, String(rects.length));
  check('rects expose their skip id', Boolean(bed.querySelector('[data-skip-id="60"]')));
  check('the last object uses its geometry skip id', Boolean(bed.querySelector('[data-skip-id="178"]')));
  check('normal objects are green/keep',
    rects.every((r) => r.getAttribute('class').indexOf('is-todo') >= 0),
    rects.map((r) => r.getAttribute('class')).join(','));

  check('geometry was fetched for the job', geomCalls >= 1, String(geomCalls));
  check('opening the modal reuses the cached geometry',
    objectCalls() === geomCalls, String(objectCalls()));

  // reopening reuses the cached geometry
  window.closeSkipModal();
  window.openSkipModal('n1', { path: '/Cube_plate_1.gcode.3mf', plate: 1 });
  await sleep(20);
  check('geometry is cached (no refetch)', objectCalls() === geomCalls, String(objectCalls()));
  window.closeSkipModal();

  // print preview entry point
  window.openPrintDialog('n1', '/Cube_plate_1.gcode.3mf');
  await sleep(40);
  check('print modal has a Skip objects button', Boolean(doc.getElementById('print-skip')));
  check('Skip objects is always clickable (the modal enforces job state)',
    doc.getElementById('print-skip').disabled === false, String(doc.getElementById('print-skip').disabled));

  // idle printer: the button stays live and opens the modal, which refuses to skip
  window.__ws.onmessage({
    data: JSON.stringify({ event: 'telemetry', printer_id: 'n1', data: { gcode_state: 'IDLE' } })
  });
  await sleep(10);
  check('Skip objects stays enabled while the printer is idle',
    doc.getElementById('print-skip').disabled === false, String(doc.getElementById('print-skip').disabled));
  doc.getElementById('print-skip').click();
  await sleep(30);
  check('idle printer still opens the skip modal',
    doc.getElementById('modal-skip').classList.contains('flex'));
  check('idle modal says it is not printing',
    (doc.getElementById('skip-sub').textContent || '').indexOf('not printing') >= 0,
    doc.getElementById('skip-sub').textContent);
  check('idle modal cannot confirm a skip with nothing selected',
    doc.getElementById('skip-confirm').disabled === true, String(doc.getElementById('skip-confirm').disabled));

  // idle: objects are selectable so the skip can be pre-armed
  calls.length = 0;
  const idleRects = [...bed.querySelectorAll('rect.bed-obj')];
  check('idle objects are not disabled',
    idleRects.every((r) => r.getAttribute('data-disabled') !== '1'),
    idleRects.map((r) => r.getAttribute('data-disabled')).join(','));
  doc.querySelector('#skip-bed [data-skip-id="60"]').dispatchEvent(
    new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await sleep(10);
  check('idle selection turns pending/red',
    doc.querySelector('#skip-bed [data-skip-id="60"]').getAttribute('class').indexOf('is-pending') >= 0,
    doc.querySelector('#skip-bed [data-skip-id="60"]').getAttribute('class'));
  check('idle selection enables confirm',
    doc.getElementById('skip-confirm').disabled === false, String(doc.getElementById('skip-confirm').disabled));

  // confirming while idle arms the selection instead of posting
  doc.getElementById('skip-confirm').dispatchEvent(
    new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await sleep(30);
  check('arming while idle sends no skip request', skipPosts().length === 0, String(skipPosts().length));
  check('arming closes the modal',
    doc.getElementById('modal-skip').classList.contains('hidden'));
  check('armed object is not marked skipped yet',
    (window.printerById('n1').skippedIds || []).indexOf(60) < 0);
  await sleep(30);
  check('Control tab surfaces the armed count',
    panel().innerHTML.indexOf('1 armed') >= 0, '');

  // the matching RUNNING telemetry applies the arm exactly once
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: {
        gcode_state: 'RUNNING', subtask_name: 'Cube_plate_1.gcode.3mf',
        job: {
          cur_stage: { idx: 0 },
          stage: [
            { idx: 92, name: 'A' }, { idx: 191, name: 'B' }, { idx: 192, name: 'C' },
            { idx: 193, name: 'D' }, { idx: 196, name: 'E' }
          ]
        }
      }
    })
  });
  await sleep(40);
  const armed = skipPosts();
  let armedIds = null;
  try { armedIds = armed.length ? JSON.parse(armed[0].body).object_ids : null; } catch (_) { armedIds = null; }
  check('the arm fires exactly once on RUNNING',
    armed.length === 1, String(armed.length));
  check('the arm carries the pre-selected ids',
    Boolean(armedIds) && armedIds.length === 1 && armedIds[0] === 60, JSON.stringify(armedIds));

  window.openSkipModal('n1', { path: '/Cube_plate_1.gcode.3mf', plate: 1 });
  await sleep(20);
  const armedRect = doc.querySelector('#skip-bed [data-skip-id="60"]');
  check('an applied arm renders as is-skipped',
    armedRect.getAttribute('class').indexOf('is-skipped') >= 0, armedRect.getAttribute('class'));
  check('an applied arm is locked',
    armedRect.getAttribute('data-disabled') === '1', armedRect.getAttribute('data-disabled'));
  window.closeSkipModal();

  // a second RUNNING tick does not re-send
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: {
        gcode_state: 'RUNNING', subtask_name: 'Cube_plate_1.gcode.3mf',
        job: {
          cur_stage: { idx: 0 },
          stage: [
            { idx: 92, name: 'A' }, { idx: 191, name: 'B' }, { idx: 192, name: 'C' },
            { idx: 193, name: 'D' }, { idx: 196, name: 'E' }
          ]
        }
      }
    })
  });
  await sleep(40);
  check('a second RUNNING tick does not re-send', skipPosts().length === 1, String(skipPosts().length));

  // already-skipped objects stay non-togglable
  window.openSkipModal('n1', { path: '/Cube_plate_1.gcode.3mf', plate: 1 });
  await sleep(20);
  calls.length = 0;
  doc.querySelector('#skip-bed [data-skip-id="60"]').dispatchEvent(
    new window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await sleep(10);
  check('a skipped object stays locked and unselected',
    doc.querySelector('#skip-bed [data-skip-id="60"]').getAttribute('class').indexOf('is-skipped') >= 0 &&
    doc.getElementById('skip-confirm').disabled === true);
  check('clicking a skipped object sends nothing', skipPosts().length === 0);
  window.closeSkipModal();
  window.closePrintDialog();

  // fullscreen HUD lives inside the camera box
  const root = doc.createElement('div');
  doc.body.appendChild(root);
  const card = window.createCard({ id: 'n2', name: 'X1C', ip: '1.2.3.4', sn: 'SN' });
  root.appendChild(card.root);
  const hud = card.root.querySelector('[data-role="cam-hud"]');
  check('card has a fullscreen HUD', Boolean(hud));
  check('HUD has an exit control', Boolean(hud && hud.querySelector('[data-action="cam-normal"]')));
  check('HUD has temperature readouts', Boolean(
    card.root.querySelector('[data-r="hudNozzle"]') && card.root.querySelector('[data-r="hudBed"]')));

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });
