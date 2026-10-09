/* DOM test for the AMS tab + filament editor (jsdom), real X1C payload shape. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const utilJs = fs.readFileSync('static/js/lib/util.js', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.WebSocket = class { close() {} send() {} };

// exactly the structure the printer sent (tray 0 empty, 1 PLA, 2/3 ASA)
const ams = {
  ams: [{
    id: '0', humidity_raw: '39', temp: '29.5',
    tray: [
      { id: '0', state: 0 },
      { id: '1', tray_type: 'PLA', tray_color: 'FFFFFFFF', remain: -1, tray_sub_brands: 'Basic' },
      { id: '2', tray_type: 'ASA', tray_color: '161616FF', remain: 80 },
      { id: '3', tray_type: 'ASA', tray_color: '161616FF', remain: -1 },
    ],
  }],
  tray_now: '2',
};
const config = { id: 'n1', name: 'X1C', ip: '192.168.0.80', sn: 'S', bed_type: 'textured_pei', ams_count: 1 };

const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url, body: opts.body });
  let payload = {};
  if (url.endsWith('/api/printers')) payload = [config];
  else if (url.includes('/api/settings')) payload = { allow_motion: false, auth_required: false };
  else if (url.includes('/api/uploads')) payload = [];
  else if (url.includes('/api/filaments')) payload = {
    categories: ['PLA', 'PETG', 'ABS', 'ASA'],
    filaments: [
      { key: 'PLA Basic', label: 'PLA Basic', category: 'PLA', id: 'GFA00', type: 'PLA', min: 190, max: 230 },
      { key: 'PETG Basic', label: 'PETG Basic', category: 'PETG', id: 'GFG00', type: 'PETG', min: 230, max: 260 },
      { key: 'ABS', label: 'ABS', category: 'ABS', id: 'GFB00', type: 'ABS', min: 240, max: 270 },
      { key: 'ASA', label: 'ASA', category: 'ASA', id: 'GFB01', type: 'ASA', min: 240, max: 270 },
    ],
  };
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.11.0') });
};
window.eval(utilJs);
window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const doc = window.document;

(async () => {
  await window.loadFleet();
  await window.loadFilaments();
  const printer = window.printerById('n1');
  check('fleet loaded', Boolean(printer));
  window.applyTelemetry(printer, { ams, vt_tray: { id: '255', tray_type: '', tray_color: 'FFFFFFFF' }, gcode_state: 'IDLE', home_flag: 0 });

  // ---- AMS rendering -------------------------------------------------
  const body = doc.createElement('div');
  body.dataset.built = 'ams';
  window.renderAms(printer, body, true);
  const text = body.textContent;
  const out = body.innerHTML;

  check('unit header', text.includes('AMS 1'));
  check('humidity + temp', text.includes('39') && text.includes('29.5'));
  check('5 tray cards (4 AMS + external)', (out.match(/data-ams="feed"/g) || []).length === 5);
  check('types shown', text.includes('PLA') && text.includes('ASA'));
  check('empty tray labelled', text.includes('Empty'));
  check('white + dark swatches', out.includes('#FFFFFF') && out.includes('#161616'));
  check('loaded tray highlighted', body.querySelectorAll('.line-accent').length === 1);
  check('remaining bar where known', (out.match(/80% left/g) || []).length === 1);
  check('feed disabled on empty slots', body.querySelectorAll('button[data-ams="feed"][disabled]').length === 2);
  check('edit button on a real tray', !!body.querySelector('button[data-action="filament-edit"][data-tray="2"]'));
  check('no edit on the empty tray', !body.querySelector('button[data-action="filament-edit"][data-tray="0"]'));

  // ---- filament editor ----------------------------------------------
  window.openFilamentEditor('n1', 0, 2);
  check('editor opens', !doc.getElementById('modal-filament').classList.contains('hidden'));
  check('prefilled from the tray (ASA)', doc.getElementById('filament-type').value === 'ASA',
    doc.getElementById('filament-type').value);
  check('prefilled colour', doc.getElementById('filament-color').value.toUpperCase() === '#161616');
  check('prefilled remaining', doc.getElementById('filament-remaining').value === '80');

  doc.getElementById('filament-type').value = 'PETG Basic';
  doc.getElementById('filament-color').value = '#ff8800';
  window.saveFilament();
  await new Promise((r) => setTimeout(r, 30));

  const post = calls.find((c) => c.url.endsWith('/filament'));
  check('posts to /filament', Boolean(post), post ? post.url : 'none');
  if (post) {
    const sent = JSON.parse(post.body);
    check('  tray 2 + ams 0', sent.tray_id === 2 && sent.ams_id === 0);
    check('  type PETG', sent.tray_type === 'PETG');
    check('  profile from catalog', sent.setting_id === 'GFG00', sent.setting_id);
    check('  colour passed', sent.color === '#ff8800');
    check('  editor closed', doc.getElementById('modal-filament').classList.contains('hidden'));
  }

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})();
