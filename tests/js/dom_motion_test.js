/* DOM-level test of the motion-unlock + confirmation-tick flow (jsdom). */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const utilJs = fs.readFileSync('static/js/lib/util.js', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.WebSocket = class { close() {} send() {} };

let settings = { allow_motion: false };
const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url, method: (opts.method || 'GET').toUpperCase(), body: opts.body });
  const payload = url.includes('/api/settings') ? { ...settings, current_port: 8000 } : {};
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.7.1') });
};

window.eval(utilJs);
window.eval(appJs);

const printer = {
  id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'S', access_code: 'c', status: 'idle',
  nozzleTemp: 29, nozzleTarget: 0, bedTemp: 26, bedTarget: 0, chamberTemp: 30,
  homed: true, lights: {}, seenAt: Date.now()
};

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };

const panel = window.document.createElement('div');
panel.setAttribute('data-r', 'panel');
panel.innerHTML = window.jogHtml(printer, { jogStep: 1 });
window.document.body.appendChild(panel);

const jogBtn = panel.querySelector('[data-action="jog"]');
const homeBtn = panel.querySelector('[data-action="home"]');
const tick = panel.querySelector('[data-role="confirm-motion"]');
check('jog panel has a Jog button', Boolean(jogBtn));
check('jog panel has a confirm tick', Boolean(tick));

const click = (b) => window.handleAction('n1', b, { jogStep: 1 });
const posted = (frag) => calls.some((c) => c.url.includes(frag));

// 1) locked, no tick -> nothing sent
click(jogBtn);
setTimeout(() => {
  check('locked + no tick: jog blocked', !posted('/jog'));

  // 2) locked, ticked -> sent
  calls.length = 0;
  tick.checked = true;
  click(jogBtn);
  setTimeout(() => {
    check('locked + ticked: jog posted', posted('/jog'));
    if (posted('/jog')) {
      const body = JSON.parse(calls.find((c) => c.url.includes('/jog')).body);
      check('  confirm_motion true', body.confirm_motion === true);
      check('  axis X, distance -1 (first button)', body.axis === 'X' && body.distance === -1, JSON.stringify(body));
    }

    // 3) unlocked globally -> no tick needed
    settings = { allow_motion: true };
    window.loadSettings().then(() => {
      calls.length = 0;
      tick.checked = false;
      click(jogBtn);
      setTimeout(() => {
        check('unlocked: jog posted without the tick', posted('/jog'));

        calls.length = 0;
        click(homeBtn);
        setTimeout(() => {
          check('unlocked: Home posted without the tick', posted('/home'));
          console.log('\n' + pass + ' passed, ' + fail + ' failed');
          process.exit(fail ? 1 : 0);
        }, 20);
      }, 20);
    });
  }, 20);
}, 20);
