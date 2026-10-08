/* DOM-level test of the light/dark theme toggle (jsdom). */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.WebSocket = class { close() {} send() {} };
window.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('1.7.3') });

// fake OS preference: dark
const listeners = [];
window.matchMedia = (q) => ({
  matches: false,
  media: q,
  addEventListener: (_e, fn) => listeners.push(fn),
  removeEventListener: () => {}
});

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const root = window.document.documentElement;
const icon = () => window.document.getElementById('theme-icon').textContent;

window.initTheme();
check('defaults to dark when the OS prefers dark', root.getAttribute('data-theme') === 'dark', root.getAttribute('data-theme'));
check('icon is the moon', icon() === '🌙', icon());

window.toggleTheme();
check('toggle -> light', root.getAttribute('data-theme') === 'light', root.getAttribute('data-theme'));
check('icon is the sun', icon() === '☀️', icon());
check('choice persisted', window.localStorage.getItem('bfm-theme') === 'light', window.localStorage.getItem('bfm-theme'));

window.toggleTheme();
check('toggle back -> dark', root.getAttribute('data-theme') === 'dark');
check('persisted dark', window.localStorage.getItem('bfm-theme') === 'dark');

// a remembered choice wins over the OS preference
window.localStorage.setItem('bfm-theme', 'light');
window.initTheme();
check('saved choice overrides the OS', root.getAttribute('data-theme') === 'light');

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
