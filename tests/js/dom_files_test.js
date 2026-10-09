/* Reproduce the SD-card tab through the real code path (loadFleet -> card -> click),
   plus sorting, filtering, search, the per-file action menu and the printing guard. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');
const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.__ws = null;
window.WebSocket = class { constructor() { window.__ws = this; } close() {} send() {} addEventListener() {} };
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };

const printer = {
  id: 'n1', name: 'X1C', ip: '192.168.0.80', sn: '00M09A', access_code: 'c', bed_type: 'textured_pei',
  ams_count: 1
};
const ENTRIES = [
  { name: 'System Volume Information', path: '/System Volume Information', is_dir: true, is_printable: false, size: 0, modified: null },
  { name: 'apps', path: '/apps', is_dir: true, is_printable: false, size: 0, modified: null },
  { name: 'zeta.3mf', path: '/zeta.3mf', is_dir: false, is_printable: true, size: 900000, modified: '2026-06-09T10:00:00+00:00' },
  { name: '004.gcode.3mf', path: '/004.gcode.3mf', is_dir: false, is_printable: true, size: 518798, modified: '2026-06-08T21:18:00+00:00' },
  { name: 'timelapse.mp4', path: '/timelapse.mp4', is_dir: false, is_printable: false, size: 2048000, modified: '2026-06-07T08:00:00+00:00' },
  { name: 'readme.txt', path: '/readme.txt', is_dir: false, is_printable: false, size: 120, modified: '2026-06-01T08:00:00+00:00' }
];
const calls = [];
window.fetch = (url, opts = {}) => {
  calls.push({ url, method: (opts.method || 'GET').toUpperCase(), body: opts.body });
  let payload = {};
  if (url.includes('/files/plan')) payload = { path: '/004.gcode.3mf', name: '004.gcode.3mf',
    trays: [{ index: 2, ams_id: 0, tray_id: 0, type: 'PLA', color: 'FF0000' }],
    external: { type: 'PETG', color: '00FF00' },
    plates: [{ index: 4, time_sec: 600, weight_g: 12.5, filaments: [{ id: 1, type: 'PLA', color: '000000', used_g: 12.5 }] }] };
  else if (url.includes('/files/rename')) payload = { status: 'renamed', path: '/renamed.3mf', name: 'renamed.3mf' };
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
const rowNames = (panel) => [...panel.querySelectorAll('.file-row')].map((r) => r.querySelector('.font-mono').textContent.trim());
const panelOf = (card) => card.querySelector('[data-r="panel"]');

function telemetry(data) {
  window.__ws.onmessage({ data: JSON.stringify({ event: 'telemetry', printer_id: 'n1', data }) });
}

(async () => {
  window.wireStatic();          // deterministic: do not rely on DOMContentLoaded ordering
  await window.loadFleet();
  await new Promise((r) => setTimeout(r, 30));

  const card = grid.querySelector('[data-card-id="n1"]');
  check('card rendered by loadFleet', Boolean(card));
  if (!card) { console.log('aborting'); process.exit(1); }

  const filesTab = card.querySelector('[data-tab="files"]');
  check('SD card tab present', Boolean(filesTab));

  filesTab.click();
  await new Promise((r) => setTimeout(r, 40));

  let panel = panelOf(card);
  check('files listing requested', calls.some((c) => c.url.includes('/files')));
  check('breadcrumb rendered', panel.textContent.includes('SD root'));
  check('folder row rendered', panel.textContent.includes('apps'));
  check('file row rendered', panel.textContent.includes('004.gcode.3mf'));
  check('Print button rendered', Boolean(panel.querySelector('[data-action="print-sd"]')));

  /* --- folders are open-only ------------------------------------------- */
  const folderRow = [...panel.querySelectorAll('.file-row')].find((r) => r.textContent.includes('apps'));
  check('folder has no delete action', !folderRow.querySelector('[data-action="delete-sd"]'));
  check('folder has no rename action', !folderRow.querySelector('[data-action="rename-sd"]'));
  check('folder has no action menu', !folderRow.querySelector('.file-menu'));

  /* --- System Volume Information is hidden ----------------------------- */
  check('System Volume Information hidden', !panel.textContent.includes('System Volume Information'));

  /* --- actions live behind a menu, Print is the visible default -------- */
  const projectRow = [...panel.querySelectorAll('.file-row')].find((r) => r.textContent.includes('004.gcode.3mf'));
  check('project shows Print as the default', Boolean(projectRow.querySelector('[data-action="print-sd"]')));
  const projectMenu = projectRow.querySelector('.file-menu-panel');
  check('project menu has Rename', Boolean(projectMenu.querySelector('[data-action="rename-sd"]')));
  check('project menu has Delete', Boolean(projectMenu.querySelector('[data-action="delete-sd"]')));
  check('project menu has Get', Boolean(projectMenu.querySelector('[data-action="download-sd"]')));

  /* --- the actions are a centred modal, not a row-anchored dropdown ----- */
  // a dropdown hung off a bottom row was covered by the fixed bottom nav
  check('menu renders as a card, not a bare list', Boolean(projectMenu.querySelector('.file-menu-card')));
  check('menu names the file it acts on', projectMenu.textContent.includes('004.gcode.3mf'));
  check('menu offers a Cancel', Boolean(projectMenu.querySelector('.file-menu-cancel')));

  const projectDetails = projectRow.querySelector('details.file-menu');
  const menuCard = projectMenu.querySelector('.file-menu-card');

  projectDetails.setAttribute('open', '');
  check('menu starts open', projectDetails.hasAttribute('open'));
  projectMenu.querySelector('.file-menu-cancel').click();
  await new Promise((r) => setTimeout(r, 20));
  check('Cancel closes the menu', !projectDetails.hasAttribute('open'));

  projectDetails.setAttribute('open', '');
  projectMenu.click();                       // a tap on the backdrop, not the card
  await new Promise((r) => setTimeout(r, 20));
  check('tapping the backdrop closes the menu', !projectDetails.hasAttribute('open'));

  // a tap inside the card must not be treated as a backdrop tap
  projectDetails.setAttribute('open', '');
  menuCard.click();
  await new Promise((r) => setTimeout(r, 20));
  check('tapping inside the card keeps the menu open', projectDetails.hasAttribute('open'));
  projectDetails.removeAttribute('open');

  const videoRow = [...panel.querySelectorAll('.file-row')].find((r) => r.textContent.includes('timelapse.mp4'));
  check('video shows Get as the default', Boolean(videoRow.querySelector('[data-action="download-sd"]')));
  check('video has no Rename', !videoRow.querySelector('[data-action="rename-sd"]'));
  check('video can be deleted', Boolean(videoRow.querySelector('[data-action="delete-sd"]')));

  /* --- sort ------------------------------------------------------------ */
  const sortSel = panel.querySelector('[data-role="files-sort"]');
  check('sort control present', Boolean(sortSel));
  check('default sort is by name ascending', rowNames(panel).filter((n) => n !== 'apps')[0] === '004.gcode.3mf',
    rowNames(panel).join(','));

  sortSel.value = 'size';
  sortSel.dispatchEvent(new window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);
  const bySize = rowNames(panel);
  check('folders still lead after sorting by size', bySize[0] === 'apps', bySize.join(','));
  check('size ascending puts readme first', bySize[1] === 'readme.txt', bySize.join(','));

  // descending
  panel.querySelector('[data-action="files-dir"]').click();
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);
  const desc = rowNames(panel);
  check('toggling direction flips the order', desc[1] === 'timelapse.mp4', desc.join(','));
  check('direction toggle is remembered', window.localStorage.getItem('bfm-files-dir') === 'desc');

  /* --- filter ---------------------------------------------------------- */
  const filterSel = () => panelOf(card).querySelector('[data-role="files-filter"]');
  filterSel().value = 'print';
  filterSel().dispatchEvent(new window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);
  const printOnly = rowNames(panel);
  check('print filter keeps only 3mf', printOnly.every((n) => n.endsWith('.3mf')), printOnly.join(','));
  check('print filter hides folders', !printOnly.includes('apps'), printOnly.join(','));
  check('print filter excludes mp4', !printOnly.includes('timelapse.mp4'), printOnly.join(','));

  filterSel().value = 'video';
  filterSel().dispatchEvent(new window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);
  const videoOnly = rowNames(panel);
  check('video filter shows only mp4', videoOnly.length === 1 && videoOnly[0] === 'timelapse.mp4', videoOnly.join(','));
  check('filter choice is remembered', window.localStorage.getItem('bfm-files-filter') === 'video');

  filterSel().value = 'all';
  filterSel().dispatchEvent(new window.Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);

  /* --- search ---------------------------------------------------------- */
  const search = panel.querySelector('[data-role="files-search"]');
  search.value = 'zeta';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);
  check('search narrows the list', rowNames(panel).length === 1 && rowNames(panel)[0] === 'zeta.3mf', rowNames(panel).join(','));
  panel.querySelector('[data-role="files-search"]').value = '';
  panel.querySelector('[data-role="files-search"]').dispatchEvent(new window.Event('input', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 30));
  panel = panelOf(card);

  /* --- rename posts the new name --------------------------------------- */
  calls.length = 0;
  window.prompt = () => 'renamed';
  const renameBtn = panel.querySelector('[data-action="rename-sd"]');
  const renamedFrom = renameBtn.dataset.path;
  renameBtn.click();
  await new Promise((r) => setTimeout(r, 40));
  const renameCall = calls.find((c) => c.url.includes('/files/rename'));
  check('rename POSTs to the rename endpoint', Boolean(renameCall));
  check('rename sends the .3mf-suffixed name', renameCall && renameCall.body.includes('renamed.3mf'), renameCall && renameCall.body);
  check('rename sends the clicked file path', renameCall && renameCall.body.includes(renamedFrom), renamedFrom);

  /* --- the printing file is highlighted, rename/delete blocked --------- */
  telemetry({ gcode_state: 'RUNNING', subtask_name: 'zeta.3mf', mc_percent: 12 });
  await new Promise((r) => setTimeout(r, 40));
  panel = panelOf(card);
  const printingRow = [...panel.querySelectorAll('.file-row')].find((r) => r.textContent.includes('zeta.3mf'));
  check('printing file highlighted', Boolean(printingRow && printingRow.classList.contains('is-printing')));
  check('printing file says printing', Boolean(printingRow && printingRow.textContent.includes('printing')));
  check('rename disabled while printing', Boolean(panel.querySelector('[data-action="rename-sd"][disabled]')));
  check('delete disabled while printing', Boolean(panel.querySelector('[data-action="delete-sd"][disabled]')));

  // and re-enabled when the job ends
  telemetry({ gcode_state: 'IDLE', subtask_name: '' });
  await new Promise((r) => setTimeout(r, 40));
  panel = panelOf(card);
  check('rename re-enabled when idle', !panel.querySelector('[data-action="rename-sd"][disabled]'));

  /* --- Print still opens the preview dialog ---------------------------- */
  calls.length = 0;
  panel.querySelector('[data-action="print-sd"]').click();
  await new Promise((r) => setTimeout(r, 60));
  check('plan requested', calls.some((c) => c.url.includes('/files/plan')));
  check('print dialog opened', !doc.getElementById('modal-print').classList.contains('hidden'));

  /* --- filament mapping: one tappable tray button per filament ---------- */
  const picks = doc.querySelectorAll('#print-filaments [data-action="pick-tray"]');
  check('a picker per filament', picks.length >= 1, String(picks.length));
  check('the picker shows the chosen tray', picks[0] && picks[0].textContent.includes('AMS1 T1'),
    picks[0] && picks[0].textContent.trim());
  check('the best match is marked', picks[0] && picks[0].textContent.includes('best match'),
    picks[0] && picks[0].textContent.trim());

  // tapping a row opens the tray modal, which shows every option as a swatch
  picks[0].click();
  await new Promise((r) => setTimeout(r, 20));
  check('tray picker opens', !doc.getElementById('modal-tray').classList.contains('hidden'));
  const trayOpts = doc.querySelectorAll('#tray-options [data-tray-option]');
  check('tray picker lists None + trays + external', trayOpts.length === 3, String(trayOpts.length));
  check('tray picker draws colour swatches', Boolean(doc.querySelector('#tray-options .tray-swatch')));
  check('tray picker marks the best match', [...trayOpts].some((o) => o.textContent.includes('best match')));

  // choosing a different tray must reach the print request
  doc.querySelector('#tray-options [data-tray-option="255"]').click();  // the external spool
  await new Promise((r) => setTimeout(r, 20));
  check('tray picker closes after choosing',
    doc.getElementById('modal-tray').classList.contains('hidden'));
  calls.length = 0;
  doc.getElementById('print-start').click();
  await new Promise((r) => setTimeout(r, 40));
  const start = calls.find((c) => c.url.includes('/print-remote'));
  check('the chosen mapping is sent', Boolean(start) && JSON.parse(start.body).ams_mapping[0] === 255,
    start && start.body);

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });