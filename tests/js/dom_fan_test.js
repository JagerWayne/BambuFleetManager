/* Fan control in the Control tab: the three sliders, their reported values,
   the preset buttons and the slider -> command path. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const utilJs = fs.readFileSync('static/js/lib/util.js', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
window.__ws = null;
window.WebSocket = class { constructor() { window.__ws = this; } close() {} send() {} addEventListener() {} };

const PRINTER = { id: 'n1', name: 'X1C', ip: '10.0.0.1', sn: 'SN', bed_type: 'cool_plate', ams_count: 1 };
const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url: String(url), method: (opts.method || 'GET').toUpperCase(), body: opts.body });
  let payload = {};
  if (String(url).endsWith('/api/printers')) payload = [PRINTER];
  else if (String(url).indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  else if (String(url).indexOf('/api/uploads') >= 0) payload = [];
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.7.0') });
};

window.eval(utilJs);
window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const doc = window.document;
const fanCall = () => calls.filter((c) => c.url.includes('/fan')).pop();

function telemetry(data) {
  window.__ws.onmessage({ data: JSON.stringify({ event: 'telemetry', printer_id: 'n1', data }) });
}

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await new Promise((r) => setTimeout(r, 40));

  const card = doc.querySelector('[data-card-id="n1"]');
  check('card rendered', Boolean(card));
  const panel = card.querySelector('[data-r="panel"]');

  /* --- the section and its three sliders -------------------------------- */
  check('a Fans section is shown in Control', panel.textContent.includes('Fans'));
  const sliders = [...panel.querySelectorAll('[data-role^="fan-slider-"]')];
  check('three fan sliders', sliders.length === 3, sliders.map((s) => s.dataset.role).join(','));
  check('part / aux / chamber are all present',
    ['fan-slider-part', 'fan-slider-aux', 'fan-slider-chamber']
      .every((r) => Boolean(panel.querySelector(`[data-role="${r}"]`))));
  check('sliders are 0-100', sliders.every((s) => s.min === '0' && s.max === '100'));

  /* --- the printer's reported speed is shown next to each fan ----------- */
  telemetry({ gcode_state: 'IDLE', cooling_fan_speed: 42, big_fan1_speed: 17, big_fan2_speed: 88 });
  await new Promise((r) => setTimeout(r, 40));
  check('reports the part fan speed', panel.querySelector('[data-live="fan-part"]').textContent === '42%',
    panel.querySelector('[data-live="fan-part"]').textContent);
  check('reports the aux fan speed', panel.querySelector('[data-live="fan-aux"]').textContent === '17%',
    panel.querySelector('[data-live="fan-aux"]').textContent);
  check('reports the chamber fan speed', panel.querySelector('[data-live="fan-chamber"]').textContent === '88%',
    panel.querySelector('[data-live="fan-chamber"]').textContent);

  /* --- a preset button sends the command -------------------------------- */
  calls.length = 0;
  const auxFull = panel.querySelector('[data-action="fan"][data-fan="aux"][data-speed="100"]');
  check('aux has a 100% preset', Boolean(auxFull));
  auxFull.click();
  await new Promise((r) => setTimeout(r, 40));
  check('preset POSTs to the fan endpoint', Boolean(fanCall()), calls.map((c) => c.url).join(' | '));
  check('preset sends the fan name and speed',
    fanCall().body === JSON.stringify({ fan: 'aux', speed: 100 }), fanCall().body);

  /* --- dragging the slider sends one command ---------------------------- */
  calls.length = 0;
  const chamber = panel.querySelector('[data-role="fan-slider-chamber"]');
  chamber.value = '65';
  chamber.dispatchEvent(new window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 40));
  check('slider POSTs on change', Boolean(fanCall()), calls.map((c) => c.url).join(' | '));
  check('slider sends its fan and value',
    fanCall().body === JSON.stringify({ fan: 'chamber', speed: 65 }), fanCall().body);

  /* --- the % label follows the slider as you drag ----------------------- */
  const part = panel.querySelector('[data-role="fan-slider-part"]');
  const label = panel.querySelector('[data-role="fan-value-part"]');
  part.value = '35';
  part.dispatchEvent(new window.Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 20));
  check('the value label tracks the slider', label.textContent === '35%', label.textContent);

  /* --- controlling a fan needs no confirmation -------------------------- */
  check('no fan action sent a confirmation flag',
    !calls.some((c) => c.url.includes('/fan') && c.body && c.body.includes('confirm')),
    calls.filter((c) => c.url.includes('/fan')).map((c) => c.body).join(' | '));

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });