/* Staging queue: the mobile-friendly "Print…" path (send to the printer, then
   the normal preview dialog) alongside the desktop drag hint. */
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
window.WebSocket = class { constructor() {} close() {} send() {} addEventListener() {} };

const PRINTER = { id: 'n1', name: 'X1C', ip: '10.0.0.1', sn: 'SN', bed_type: 'cool_plate', ams_count: 1 };
const calls = [];
window.fetch = (url, opts = {}) => {
  const u = String(url);
  calls.push({ url: u, method: (opts.method || 'GET').toUpperCase(), body: opts.body });
  let payload = {};
  if (u.endsWith('/api/printers')) payload = [PRINTER];
  else if (u.indexOf('/files/upload-staged') >= 0) payload = { status: 'sent', path: '/part.3mf', name: 'part.3mf', dir: '/' };
  else if (u.indexOf('/files/plan') >= 0) payload = { path: '/part.3mf', name: 'part.3mf', plates: [{ index: 1, filaments: [{ id: 1, type: 'PLA' }] }] };
  else if (u.indexOf('/api/uploads') >= 0) payload = [{ filename: 'part.3mf' }];
  else if (u.indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.7.0') });
};

window.eval(utilJs);
window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const doc = window.document;

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await new Promise((r) => setTimeout(r, 40));

  const card = doc.querySelector('[data-card-id="n1"]');
  card.querySelector('[data-tab="staging"]').click();
  await new Promise((r) => setTimeout(r, 40));

  const panel = card.querySelector('[data-r="panel"]');
  check('staging tab renders', panel.textContent.includes('Staging queue'));

  const row = panel.querySelector('.file-row');
  check('the staged file has a row', Boolean(row), panel.textContent.trim().slice(0, 60));
  check('the row names the file', row.textContent.includes('part.3mf'));

  /* --- a tap-to-print action, because touch has no drag ---------------- */
  const printBtn = row.querySelector('[data-action="print-staged"]');
  check('each staged file has a Print… action', Boolean(printBtn));
  check('the drag hint is kept for mouse users', Boolean(row.querySelector('.drag-hint')));
  check('the row is still draggable', row.getAttribute('draggable') === 'true');
  check('remove is still offered', Boolean(row.querySelector('[data-remove]')));

  /* --- printing a staged file sends it, then opens the preview --------- */
  calls.length = 0;
  printBtn.click();
  await new Promise((r) => setTimeout(r, 80));

  const send = calls.find((c) => c.url.includes('/files/upload-staged'));
  check('Print… sends the staged file to the printer', Boolean(send), calls.map((c) => c.url).join(' | '));
  check('it sends the filename and target folder',
    send && send.body === JSON.stringify({ filename: 'part.3mf', dir_path: '/' }), send && send.body);

  const plan = calls.find((c) => c.url.includes('/files/plan'));
  check('then it loads the plan for the uploaded path', Boolean(plan), calls.map((c) => c.url).join(' | '));
  check('the plan is for the path the printer reported',
    plan && plan.url.includes(encodeURIComponent('/part.3mf')), plan && plan.url);

  const modal = doc.getElementById('modal-print');
  check('the standard print preview opens', !modal.classList.contains('hidden'));
  check('the preview names the file', doc.getElementById('print-sub').textContent.includes('part.3mf'), JSON.stringify(doc.getElementById('print-sub').textContent));

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });