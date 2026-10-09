/* DOM-level test of the temperature UI using jsdom.
 * Sliders and presets send immediately; there is no confirm checkbox or
 * "Apply targets" button any more. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
class FakeWS { close() {} send() {} }
window.WebSocket = FakeWS;

const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url, method: (opts.method || 'GET').toUpperCase(), body: opts.body });
  return Promise.resolve({
    ok: true, status: 200,
    json: () => Promise.resolve({ entries: [], commands: [], risk: {} }),
    text: () => Promise.resolve('1.10.0')
  });
};

window.eval(appJs);

const printer = {
  id: 'node_x', name: 'X1C', ip: '1.2.3.4', sn: 'SN', access_code: 'c',
  status: 'idle', nozzleTemp: 29, nozzleTarget: 0, bedTemp: 26, bedTarget: 0,
  chamberTemp: 31, speedLevel: 2, homed: true, lights: {}, seenAt: Date.now()
};

let pass = 0, fail = 0;
const check = (label, ok, extra = '') => {
  console.log((ok ? 'PASS  ' : 'FAIL  ') + label + (extra ? ' -> ' + extra : ''));
  ok ? pass++ : fail++;
};

// the panel must sit inside a [data-card-id] card, or the delegated change
// listener cannot resolve which printer to command
const card = window.document.createElement('div');
card.setAttribute('data-card-id', 'node_x');
const panel = window.document.createElement('div');
panel.setAttribute('data-r', 'panel');
panel.innerHTML = window.temperatureHtml(printer);
card.appendChild(panel);
window.document.body.appendChild(card);
window.wireStatic();

const tempCalls = () => calls.filter((c) => c.url.includes('/temperature'));
const lastBody = () => JSON.parse(tempCalls()[tempCalls().length - 1].body);
const tick = () => new Promise((r) => setTimeout(r, 20));

(async () => {
  check('no confirm checkbox', !panel.querySelector('[data-role="confirm-thermal"]'));
  check('no apply-temps button', !panel.querySelector('[data-action="apply-temps"]'));

  // --- a slider release sends exactly that one value ---------------------
  calls.length = 0;
  const nozzle = panel.querySelector('[data-role="slider-nozzle"]');
  nozzle.value = '215';
  nozzle.dispatchEvent(new window.Event('change', { bubbles: true }));
  await tick();
  check('nozzle slider release posts to /temperature', tempCalls().length === 1,
    String(tempCalls().length));
  if (tempCalls().length) {
    const body = lastBody();
    check('  nozzle 215', body.nozzle === 215, body.nozzle);
    check('  confirm_thermal true', body.confirm_thermal === true);
    check('  bed omitted for a nozzle-only drag', body.bed === undefined, body.bed);
    check('  allow_while_printing false by default', body.allow_while_printing === false,
      body.allow_while_printing);
  }

  // --- the allow-while-printing tick rides along on every send -----------
  calls.length = 0;
  panel.querySelector('[data-role="allow-while-printing"]').checked = true;
  const bed = panel.querySelector('[data-role="slider-bed"]');
  bed.value = '65';
  bed.dispatchEvent(new window.Event('change', { bubbles: true }));
  await tick();
  if (tempCalls().length) {
    const body = lastBody();
    check('bed slider sends bed only', body.bed === 65 && body.nozzle === undefined, JSON.stringify(body));
    check('allow_while_printing true when ticked', body.allow_while_printing === true);
  }
  panel.querySelector('[data-role="allow-while-printing"]').checked = false;

  // --- a preset fills the fields AND sends immediately -------------------
  calls.length = 0;
  const preset = panel.querySelector('[data-action="preset"][data-nozzle="220"]'); // PLA
  await window.handleAction('node_x', preset, { jogStep: 1 });
  await tick();
  check('preset fills nozzle', panel.querySelector('[data-role="input-nozzle"]').value === '220',
    panel.querySelector('[data-role="input-nozzle"]').value);
  check('preset fills bed', panel.querySelector('[data-role="input-bed"]').value === '55',
    panel.querySelector('[data-role="input-bed"]').value);
  check('preset posts immediately', tempCalls().length === 1, String(tempCalls().length));
  if (tempCalls().length) {
    const body = lastBody();
    check('  preset nozzle 220', body.nozzle === 220, body.nozzle);
    check('  preset bed 55', body.bed === 55, body.bed);
    check('  chamber omitted (tile absent)', body.chamber === undefined, body.chamber);
    check('  confirm_thermal true', body.confirm_thermal === true);
  }

  // --- cooldown zeroes and sends -----------------------------------------
  calls.length = 0;
  await window.handleAction('node_x', panel.querySelector('[data-action="cooldown"]'), { jogStep: 1 });
  await tick();
  check('cooldown zeroes nozzle', panel.querySelector('[data-role="input-nozzle"]').value === '0');
  if (tempCalls().length) {
    const body = lastBody();
    check('cooldown sends zeros', body.nozzle === 0 && body.bed === 0, JSON.stringify(body));
  }

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });
