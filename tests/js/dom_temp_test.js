/* DOM-level test of the temperature UI using jsdom.
 * Proves the [data-r="panel"] selector fix drives the right POSTs. */
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
    text: () => Promise.resolve('1.7.1')
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

const panel = window.document.createElement('div');
panel.setAttribute('data-r', 'panel');
panel.innerHTML = window.temperatureHtml(printer);
window.document.body.appendChild(panel);

panel.querySelector('[data-role="input-nozzle"]').value = '215';
panel.querySelector('[data-role="input-bed"]').value = '65';

const confirm = panel.querySelector('[data-role="confirm-thermal"]');
check('confirm checkbox present', Boolean(confirm));
const applyBtn = panel.querySelector('[data-action="apply-temps"]');
check('Apply button present', Boolean(applyBtn));

const click = (btn) => window.handleAction('node_x', btn, { jogStep: 1 });

// 1) without the tick nothing is sent
click(applyBtn);
setTimeout(() => {
  check('Apply blocked while the tick is off', !calls.some((c) => c.url.includes('/temperature')));

  // 2) with the tick, the setpoints are posted
  calls.length = 0;
  confirm.checked = true;
  click(applyBtn);
  setTimeout(() => {
    const temp = calls.find((c) => c.url.includes('/temperature'));
    check('Apply posts to /temperature', Boolean(temp), temp ? temp.url : 'no call');
    if (temp) {
      const body = JSON.parse(temp.body);
      check('  nozzle 215', body.nozzle === 215, body.nozzle);
      check('  bed 65', body.bed === 65, body.bed);
      check('  confirm_thermal true', body.confirm_thermal === true);
      check('  chamber omitted (tile removed)', body.chamber === undefined, body.chamber);
    }

    // 3) presets fill the fields
    const preset = panel.querySelector('[data-action="preset"][data-nozzle="220"]'); // PLA
    click(preset);
    check('preset fills nozzle', panel.querySelector('[data-role="input-nozzle"]').value === '220',
      panel.querySelector('[data-role="input-nozzle"]').value);
    check('preset fills bed', panel.querySelector('[data-role="input-bed"]').value === '55',
      panel.querySelector('[data-role="input-bed"]').value);

    // 4) cooldown zeroes everything
    click(panel.querySelector('[data-action="cooldown"]'));
    check('cooldown zeroes nozzle', panel.querySelector('[data-role="input-nozzle"]').value === '0');

    console.log('\n' + pass + ' passed, ' + fail + ' failed');
    process.exit(fail ? 1 : 0);
  }, 20);
}, 20);
