/* DOM-level test of the print dialog's options block (jsdom).
 *
 * The options must be truthful: homing always happens at print start (the
 * printer and the file's own start G-code command it), so the dialog says so
 * and only offers the calibration routines. layer_inspect must ride along in
 * the print request like every other flag. */
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
  if (u.indexOf('/files/plan') >= 0) payload = PLAN;
  else if (u.endsWith('/api/printers') && method === 'GET') payload = FLEET;
  else if (u.indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  else if (u.indexOf('/api/uploads') >= 0) payload = [];
  else if (u.indexOf('/print-remote') >= 0) payload = { status: 'started', printer: 'X1C', job: FILE, skipped: [] };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.13.0') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const printPosts = () => calls.filter((c) => c.url.indexOf('/print-remote') >= 0 && c.method === 'POST');
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await sleep(40);
  window.initWebSocket();
  window.__ws.onmessage({
    data: JSON.stringify({ event: 'telemetry', printer_id: 'n1', data: { gcode_state: 'IDLE', subtask_name: FILE } })
  });
  await sleep(20);

  await window.openPrintDialog('n1', '/' + FILE);
  await sleep(30);

  const options = doc.getElementById('print-options').textContent || '';
  check('bed levelling label says auto bed leveling', options.indexOf('Bed levelling (auto bed leveling)') >= 0, options.slice(0, 200));
  check('flow calibration is labelled', options.indexOf('Flow calibration') >= 0);
  check('vibration compensation is labelled', options.indexOf('Vibration compensation') >= 0);
  check('first-layer inspection is offered', options.indexOf('First-layer inspection') >= 0);
  check('the homing note is shown',
    options.indexOf('always homes at print start') >= 0 && options.indexOf('start G-code') >= 0,
    options.slice(-220));

  const inspect = doc.querySelector('#print-options [data-opt="layer_inspect"]');
  check('layer_inspect is a checkbox, checked by default', Boolean(inspect) && inspect.checked === true);

  calls.length = 0;
  click(doc.getElementById('print-start'));
  await sleep(60);
  let sent = null;
  try { sent = JSON.parse(printPosts()[0].body); } catch (_) { sent = null; }
  check('the print request carries layer_inspect true by default',
    sent && sent.layer_inspect === true, JSON.stringify(sent));

  // uncheck -> the flag follows into the payload
  window.openPrintDialog('n1', '/' + FILE);
  await sleep(30);
  const inspect2 = doc.querySelector('#print-options [data-opt="layer_inspect"]');
  inspect2.checked = false;
  calls.length = 0;
  click(doc.getElementById('print-start'));
  await sleep(60);
  let sent2 = null;
  try { sent2 = JSON.parse(printPosts()[0].body); } catch (_) { sent2 = null; }
  check('unchecking layer_inspect sends false',
    sent2 && sent2.layer_inspect === false, JSON.stringify(sent2));

  window.closePrintDialog();
  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });
