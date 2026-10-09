/* DOM-level test of the camera progress ring and the info strip (jsdom).
 *
 * The horizontal job/progress tile in the aside was replaced by a conic
 * progress ring drawn as the camera border (clockwise from the bottom-left,
 * driven by the --progress custom property) plus a compact mono info strip
 * directly under the camera. Both are patched via cached refs from
 * updateCard() - never re-serialised - and the ring is hidden when the
 * printer is not actively printing. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });
window.__ws = null;
window.WebSocket = class { constructor() { window.__ws = this; } close() {} send() {} addEventListener() {} };
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };

const printer = { id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: '00M09A', bed_type: 'cool_plate', ams_count: 1 };
window.fetch = (url) => {
  if (String(url).endsWith('/api/printers')) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([printer]), text: () => Promise.resolve('1.15.0') });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('1.15.0') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (ok ? '' : ' -> ' + x)); ok ? pass++ : fail++; };
const doc = window.document;
const $q = (s) => doc.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const telemetry = (data) => window.__ws.onmessage({ data: JSON.stringify({ event: 'telemetry', printer_id: 'n1', data }) });
const ring = () => $q('[data-card-id="n1"] .cam-ring');
const info = () => $q('[data-card-id="n1"] [data-r="camInfo"]');

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await sleep(50);
  check('websocket wired', Boolean(window.__ws));
  check('card mounted', Boolean($q('[data-card-id="n1"]')));
  check('ring element exists in the cam box', Boolean($q('[data-card-id="n1"] [data-role="cam-box"] .cam-ring')));
  check('info strip exists under the cam box', Boolean(info()));

  // idle: ring hidden, info strip hidden
  telemetry({ gcode_state: 'IDLE', subtask_name: 'None' });
  await sleep(30);
  check('ring hidden while idle', ring() && !ring().classList.contains('is-active'));
  check('info strip hidden while idle', info() && info().classList.contains('hidden'));

  // running: ring active, --progress set from mc_percent, strip filled
  telemetry({ gcode_state: 'RUNNING', subtask_name: 'Cube_plate_1.gcode.3mf', mc_percent: 42,
    layer_num: 7, total_layer_num: 100, mc_remaining_time: 15 });
  await sleep(30);
  check('ring active while running', ring() && ring().classList.contains('is-active'));
  check('--progress is 0.42 for 42%',
    ring() && ring().style.getPropertyValue('--progress') === '0.42',
    ring() && ring().style.getPropertyValue('--progress'));
  check('strip is visible while running', info() && !info().classList.contains('hidden'));
  check('strip names the job', info() && info().querySelector('[data-r="camJob"]').textContent.includes('Cube_plate_1'));
  check('strip shows the layer', info() && /L 7\/100/.test(info().querySelector('[data-r="camLayer"]').textContent),
    info() && info().querySelector('[data-r="camLayer"]').textContent);
  check('strip shows the percentage', info() && info().querySelector('[data-r="camPct"]').textContent === '42%');
  check('strip shows the remaining time',
    /15m/.test(info().querySelector('[data-r="camRemaining"]').textContent),
    info() && info().querySelector('[data-r="camRemaining"]').textContent);

  // paused / prepare keep it visible
  telemetry({ gcode_state: 'PAUSED', mc_percent: 50 });
  await sleep(30);
  check('ring stays active while paused', ring() && ring().classList.contains('is-active'));
  telemetry({ gcode_state: 'PREPARE', mc_percent: 1 });
  await sleep(30);
  check('ring active during prepare', ring() && ring().classList.contains('is-active'));

  // finish: ring hides again, progress value is harmless
  telemetry({ gcode_state: 'FINISH', subtask_name: 'Cube_plate_1.gcode.3mf', mc_percent: 100 });
  await sleep(30);
  check('ring hidden after finish', ring() && !ring().classList.contains('is-active'));
  check('info strip hidden after finish', info() && info().classList.contains('hidden'));

  // the ring must not swallow clicks: camera controls sit above it
  const startBtn = $q('[data-card-id="n1"] [data-action="cam-start"]');
  check('camera controls still present', Boolean(startBtn));

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });
