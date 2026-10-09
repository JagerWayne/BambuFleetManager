/* The node-removal guard in the System tab: a number challenge that must be
   answered correctly before the node is deleted. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });
window.WebSocket = class { constructor() {} close() {} send() {} addEventListener() {} };
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };

const FLEET = [
  { id: 'alpha', name: 'X1C - Alpha', ip: '10.0.0.1', sn: 'AAA', bed_type: 'cool_plate', ams_count: 1 },
  { id: 'bravo', name: 'X1C - Bravo', ip: '10.0.0.2', sn: 'BBB', bed_type: 'cool_plate', ams_count: 1 }
];
const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url: String(url), method: (opts.method || 'GET').toUpperCase() });
  let payload = {};
  if (String(url).indexOf('/api/printers') >= 0 && (opts.method || 'GET').toUpperCase() === 'DELETE') payload = { status: 'ok' };
  else if (String(url).endsWith('/api/printers')) payload = FLEET;
  else if (String(url).indexOf('/api/uploads') >= 0) payload = [];
  else if (String(url).indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.4.0') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const doc = window.document;
const card = () => doc.querySelector('[data-card-id]');
const panel = () => card().querySelector('[data-r="panel"]');

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await new Promise((r) => setTimeout(r, 40));

  check('a printer is mounted', Boolean(card()));

  // open the System tab
  card().querySelector('[data-tab="system"]').click();
  await new Promise((r) => setTimeout(r, 40));

  const gate = panel().querySelector('[data-role="remove-gate"]');
  check('danger zone with remove button is in System', Boolean(gate));
  check('remove is not armed until asked', !gate.querySelector('[data-role="remove-challenge"]'));

  gate.querySelector('[data-action="remove-node-start"]').click();
  await new Promise((r) => setTimeout(r, 30));

  const challenge = panel().querySelector('[data-role="remove-challenge"]');
  check('starting removal shows the number challenge', Boolean(challenge));
  const target = Number(challenge.dataset.target);
  check('target is between 1 and 99', target >= 1 && target <= 99, String(target));

  const picks = [...challenge.querySelectorAll('[data-action="remove-node-pick"]')];
  check('exactly three numbers are offered', picks.length === 3, String(picks.length));
  check('the offered numbers are unique',
    new Set(picks.map((b) => b.dataset.value)).size === 3, picks.map((b) => b.dataset.value).join(','));
  check('the target is among them', picks.some((b) => Number(b.dataset.value) === target));

  // a wrong tap must not remove anything, and must re-roll the numbers
  calls.length = 0;
  const wrong = picks.find((b) => Number(b.dataset.value) !== target);
  wrong.click();
  await new Promise((r) => setTimeout(r, 30));
  check('a wrong tap does not delete', !calls.some((c) => c.method === 'DELETE'));
  check('a wrong tap keeps the challenge open', Boolean(panel().querySelector('[data-role="remove-challenge"]')));

  // cancel closes the challenge without deleting
  calls.length = 0;
  panel().querySelector('[data-action="remove-node-cancel"]').click();
  await new Promise((r) => setTimeout(r, 30));
  check('cancel does not delete', !calls.some((c) => c.method === 'DELETE'));
  check('cancel restores the plain button', Boolean(panel().querySelector('[data-action="remove-node-start"]')));

  // arm again and answer correctly
  panel().querySelector('[data-action="remove-node-start"]').click();
  await new Promise((r) => setTimeout(r, 30));
  const challenge2 = panel().querySelector('[data-role="remove-challenge"]');
  const target2 = Number(challenge2.dataset.target);
  calls.length = 0;
  [...challenge2.querySelectorAll('[data-action="remove-node-pick"]')]
    .find((b) => Number(b.dataset.value) === target2).click();
  await new Promise((r) => setTimeout(r, 60));

  check('the correct number deletes the node', calls.some((c) => c.method === 'DELETE' && c.url.indexOf('/api/printers/') >= 0),
    calls.map((c) => c.method + ' ' + c.url).join(' | '));
  check('nodes are not double-deleted', calls.filter((c) => c.method === 'DELETE').length === 1);

  // the remaining printer takes over
  await new Promise((r) => setTimeout(r, 40));
  check('another printer is mounted after removal', Boolean(card()));
  check('the survivors card is the other node', card() && card().dataset.cardId === 'bravo', card() && card().dataset.cardId);

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });