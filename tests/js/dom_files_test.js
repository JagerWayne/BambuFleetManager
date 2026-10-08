/* Reproduce the SD-card tab through the real code path (loadFleet -> card -> click). */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.WebSocket = class { close() {} send() {} };

const printer = {
  id: 'n1', name: 'X1C', ip: '192.168.0.80', sn: '00M09A', access_code: 'c', bed_type: 'textured_pei',
  ams_count: 1
};
const ENTRIES = [
  { name: 'apps', path: '/apps', is_dir: true, is_printable: false, size: 0, modified: null },
  { name: '004.gcode.3mf', path: '/004.gcode.3mf', is_dir: false, is_printable: true, size: 518798, modified: '2026-06-08T21:18:00+00:00' },
];
const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url, method: (opts.method || 'GET').toUpperCase(), body: opts.body });
  let payload = {};
  if (url.includes('/files/plan')) payload = { path: '/004.gcode.3mf', name: '004.gcode.3mf', trays: [], external: null,
    plates: [{ index: 4, time_sec: 600, weight_g: 12.5, filaments: [{ id: 1, type: 'PLA', color: '000000', used_g: 12.5 }] }] };
  else if (url.includes('/files')) payload = { path: '/', parent: null, entries: ENTRIES };
  else if (url.includes('/api/settings')) payload = { allow_motion: false, current_port: 8000 };
  else if (url.endsWith('/api/printers')) payload = [printer];
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.8.1') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const doc = window.document;
const grid = doc.getElementById('fleet-grid');

(async () => {
  await window.loadFleet();
  await new Promise((r) => setTimeout(r, 30));

  const card = grid.querySelector('[data-card-id="n1"]');
  check('card rendered by loadFleet', Boolean(card));
  if (!card) { console.log('aborting'); process.exit(1); }

  const filesTab = card.querySelector('[data-tab="files"]');
  check('SD card tab present', Boolean(filesTab));

  filesTab.click();
  await new Promise((r) => setTimeout(r, 40));

  const panel = card.querySelector('[data-r="panel"]');
  check('files listing requested', calls.some((c) => c.url.includes('/files')));
  check('breadcrumb rendered', panel.textContent.includes('SD root'));
  check('folder row rendered', panel.textContent.includes('apps'));
  check('file row rendered', panel.textContent.includes('004.gcode.3mf'));
  check('Print button rendered', Boolean(panel.querySelector('[data-action="print-sd"]')));

  // click Print -> preview dialog (not an immediate dispatch)
  const printBtn = panel.querySelector('[data-action="print-sd"]');
  if (printBtn) {
    printBtn.click();
    await new Promise((r) => setTimeout(r, 60));
    check('plan requested', calls.some((c) => c.url.includes('/files/plan')));
    const modal = doc.getElementById('modal-print');
    check('print dialog opened', !modal.classList.contains('hidden'));
    check('plate preview rendered', doc.getElementById('print-img').getAttribute('src').includes('/files/plate'));
    check('filament mapping shown', doc.getElementById('print-filaments').textContent.includes('PLA'));
    check('mapping uses colour chips', doc.querySelectorAll('#print-filaments [data-map-filament]').length >= 1);
    check('chip has a colour swatch', /background:#?[0-9A-Fa-f]{6}/.test(doc.getElementById('print-filaments').innerHTML));
  }

  // navigate into a folder
  calls.length = 0;
  const dirBtn = panel.querySelector('[data-action="cd"][data-dir="1"]');
  if (dirBtn) {
    dirBtn.click();
    await new Promise((r) => setTimeout(r, 40));
    check('folder navigation requested /apps', calls.some((c) => c.url.includes('%2Fapps') || c.url.includes('/apps')));
  }

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
