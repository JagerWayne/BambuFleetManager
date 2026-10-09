/* DOM-level test of the pre-armed object skip lifecycle (jsdom).
 *
 * Two things are covered:
 *  1. the primary path - a selection armed while the printer is idle is sent
 *     WITH the print request (skip_object_ids) and the arm is then cleared;
 *  2. the fallback path - the arm survives the idle -> prepare transition
 *     (where subtask_name is transiently empty, the regression that used to
 *     delete it) and still fires for a print started outside the app. */
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

const FILE = 'Cube_plate_1.gcode.3mf';
const GEOM = {
  path: '/' + FILE, plate: 1, bed: [256, 256], bbox_all: [45, 45, 200, 200],
  objects: [60, 112, 134].map((id, i) => ({
    id, plate_id: [92, 191, 192][i], name: ['A', 'B', 'C'][i],
    bbox: [10 * i, 10 * i, 10 * i + 8, 10 * i + 8]
  }))
};
const PLAN = {
  name: 'Cube', path: '/' + FILE,
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
  else if (u.indexOf('/print-remote') >= 0) {
    // the backend echoes back what it actually published as skip_objects
    let skipped = [];
    try { skipped = JSON.parse(opts.body || '{}').skip_object_ids || []; } catch (_) { skipped = []; }
    payload = { status: 'started', printer: 'X1C', job: FILE, skipped };
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.12.2') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const skipPosts = () => calls.filter((c) => c.url.indexOf('/skip-objects') >= 0 && c.method === 'POST');
const printPosts = () => calls.filter((c) => c.url.indexOf('/print-remote') >= 0 && c.method === 'POST');
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));

const telemetry = (data) => window.__ws.onmessage({
  data: JSON.stringify({ event: 'telemetry', printer_id: 'n1', data })
});

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await sleep(40);
  window.initWebSocket();
  await sleep(20);

  // ---- idle: arm a selection -------------------------------------------
  telemetry({ gcode_state: 'IDLE', subtask_name: FILE });
  await sleep(20);
  window.openSkipModal('n1', { path: '/' + FILE, plate: 1 });
  await sleep(30);
  click(doc.querySelector('#skip-bed [data-skip-id="60"]'));
  click(doc.querySelector('#skip-bed [data-skip-id="134"]'));
  await sleep(10);
  click(doc.getElementById('skip-confirm'));
  await sleep(20);
  check('arming while idle sends no skip request', skipPosts().length === 0, String(skipPosts().length));

  // ---- the print dialog says what will be skipped ----------------------
  window.openPrintDialog('n1', '/' + FILE);
  await sleep(40);
  const info = doc.getElementById('print-info').textContent || '';
  check('the print dialog reports the armed count',
    info.indexOf('will be skipped when this print starts') >= 0 && info.indexOf('2 object') >= 0, info.trim());

  // ---- primary path: the ids ride along with the print request ---------
  calls.length = 0;
  click(doc.getElementById('print-start'));
  await sleep(60);
  const printed = printPosts();
  let sentIds = null;
  try { sentIds = printed.length ? JSON.parse(printed[0].body).skip_object_ids : null; } catch (_) { sentIds = null; }
  check('the print request carries skip_object_ids', printed.length === 1, String(printed.length));
  check('the armed ids are sent with the print request',
    Array.isArray(sentIds) && sentIds.length === 2 && sentIds.indexOf(60) >= 0 && sentIds.indexOf(134) >= 0,
    JSON.stringify(sentIds));
  check('the browser does not also POST a skip itself', skipPosts().length === 0, String(skipPosts().length));

  // the arm is consumed, so the Control tab no longer shows it
  await sleep(40);
  const card = doc.querySelector('[data-card-id]');
  check('the arm is cleared once the backend applied it',
    (card && card.textContent.indexOf('2 armed') < 0), card ? card.textContent.slice(0, 80) : '');

  // ---- the applied ids come back as already-skipped --------------------
  check('the applied ids show up as skipped',
    (window.printerById('n1').skippedIds || []).indexOf(60) >= 0,
    JSON.stringify(window.printerById('n1').skippedIds));

  // ---- regression: idle -> prepare with an empty subtask_name ---------
  // Re-arm (a fresh selection for the same file), then walk the printer
  // through the start transition. During PREPARE the printer reports an empty
  // subtask_name; that used to count as "the job changed" and deleted the arm
  // exactly when it should have fired.
  window.printerById('n1').skippedIds = [];
  telemetry({ gcode_state: 'IDLE', subtask_name: FILE });
  await sleep(20);
  window.openSkipModal('n1', { path: '/' + FILE, plate: 1 });
  await sleep(30);
  click(doc.querySelector('#skip-bed [data-skip-id="112"]'));
  click(doc.getElementById('skip-confirm'));
  await sleep(20);
  calls.length = 0;

  telemetry({ gcode_state: 'PREPARE', subtask_name: '' });
  await sleep(20);
  check('the arm survives the idle -> prepare transition (empty subtask_name)',
    (doc.querySelector('[data-card-id]') || {}).textContent.indexOf('1 armed') >= 0,
    doc.querySelector('[data-card-id]') ? doc.querySelector('[data-card-id]').textContent.slice(0, 80) : '');
  check('nothing is sent while the job is only preparing', skipPosts().length === 0,
    String(skipPosts().length));

  telemetry({ gcode_state: 'RUNNING', subtask_name: FILE });
  await sleep(50);
  const fired = skipPosts();
  let firedIds = null;
  try { firedIds = fired.length ? JSON.parse(fired[0].body).object_ids : null; } catch (_) { firedIds = null; }
  check('the fallback fires once the job is running', fired.length === 1, String(fired.length));
  check('the fallback carries the armed ids',
    Array.isArray(firedIds) && firedIds.length === 1 && firedIds[0] === 112, JSON.stringify(firedIds));

  // a duplicate RUNNING tick must not re-send
  telemetry({ gcode_state: 'RUNNING', subtask_name: FILE });
  await sleep(40);
  check('a duplicate RUNNING tick does not re-send', skipPosts().length === 1, String(skipPosts().length));

  // an unrelated job drops a stale arm instead of firing it later
  telemetry({ gcode_state: 'RUNNING', subtask_name: 'Other_plate_1.gcode.3mf' });
  await sleep(40);
  check('a different running job does not fire the arm', skipPosts().length === 1, String(skipPosts().length));

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });