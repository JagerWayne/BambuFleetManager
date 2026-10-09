/* DOM-level test of the top-down skip bed and the fullscreen camera HUD.
 * The bed fetches plate geometry once and draws one tappable rect per object. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });

const GEOM = {
  path: '/Cube_plate_1.gcode.3mf', plate: 1, bed: [256, 256], bbox_all: [45, 45, 200, 200],
  objects: [92, 191, 192, 193, 196].map((id, i) => ({
    id, name: ['A', 'B', 'C', 'D', 'E'][i], bbox: [10 * i, 10 * i, 10 * i + 8, 10 * i + 8]
  }))
};
window.fetch = (url) => {
  if (String(url).indexOf('/files/objects') >= 0) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(GEOM) });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('1.10.0') });
};
window.WebSocket = class { close() {} send() {} };

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };

const printer = {
  id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'SN', status: 'running', progress: 40,
  job: 'Cube_plate_1.gcode.3mf', objectIndex: 2, skippedIds: [],
  nozzleTemp: 220, bedTemp: 55, remainingSec: 300,
  objects: [
    { id: 92, name: 'A' }, { id: 191, name: 'B' }, { id: 192, name: 'C' },
    { id: 193, name: 'D' }, { id: 196, name: 'E' }
  ]
};

(async () => {
  // first render only starts the fetch; once it resolves the bed is drawn
  window.controlHtml(printer);
  await new Promise((r) => setTimeout(r, 30));
  const markup = window.controlHtml(printer);

  check('bed svg rendered', markup.includes('bed-svg'));
  const rects = (markup.match(/class="bed-obj/g) || []).length;
  check('one rect per object', rects === 5, String(rects));
  check('rects are tappable to skip', markup.includes('data-action="skip" data-value="92"'));
  check('the last object keeps its telemetry id', markup.includes('data-value="196"'));
  check('printing object is highlighted', markup.includes('bed-obj is-current'));
  check('printed objects are dimmed', markup.includes('bed-obj is-done'));
  check('the next object is flagged', markup.includes('bed-obj is-next'));
  check('Skip current / Skip next are kept',
    markup.includes('Skip current') && markup.includes('Skip next'));

  // geometry is cached: a second render must not fetch again
  window.__calls = 0;
  window.fetch = ((orig) => (url, opts) => {
    if (String(url).indexOf('/files/objects') >= 0) window.__calls++;
    return orig(url, opts);
  })(window.fetch);
  window.controlHtml(printer);
  check('geometry is cached (no refetch)', window.__calls === 0, String(window.__calls));

  // fullscreen HUD lives inside the camera box
  const root = window.document.createElement('div');
  window.document.body.appendChild(root);
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
