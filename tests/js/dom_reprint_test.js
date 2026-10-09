/* DOM-level test of the Control tab's Reprint button (jsdom).
 *
 * Reprint re-opens the normal print preview for the file the printer is
 * currently on, so the user can re-run the plate without digging through the
 * SD file browser. It is only offered for a real printable job. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
const doc = window.document;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });
window.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
window.WebSocket = class { constructor() { window.__ws = this; } close() {} send() {} };

const FLEET = [{ id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'SN', bed_type: 'cool_plate', ams_count: 1 }];
const PLAN = {
  name: 'Cube', path: '/Cube_plate_1.gcode.3mf',
  plates: [{ index: 1, filaments: [], time_sec: 0, weight_g: 0 }], trays: [], external: null
};

const calls = [];
window.fetch = (url, opts = {}) => {
  const method = (opts.method || 'GET').toUpperCase();
  const u = String(url);
  calls.push({ url: u, method });
  let payload = {};
  if (u.indexOf('/files/plan') >= 0) payload = PLAN;
  else if (u.indexOf('/print-remote') >= 0) payload = { job: 'started', printer: 'X1C' };
  else if (u.endsWith('/api/printers') && method === 'GET') payload = FLEET;
  else if (u.indexOf('/api/settings') >= 0) payload = { allow_motion: false, current_port: 8000 };
  else if (u.indexOf('/api/uploads') >= 0) payload = [];
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload), text: () => Promise.resolve('1.11.0') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const card = () => doc.querySelector('[data-card-id]');
const panel = () => card() && card().querySelector('[data-r="panel"]');
const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true, cancelable: true }));
const planCalls = () => calls.filter((c) => c.url.indexOf('/files/plan') >= 0).length;

/* --- controlHtml markup --------------------------------------------------- */

const base = {
  id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'S', status: 'idle',
  job: 'Cube_plate_1.gcode.3mf', objects: [], objectIndex: null, skippedIds: []
};

const wrap = doc.createElement('div');
wrap.innerHTML = window.controlHtml(base);
const reprint = wrap.querySelector('[data-action="reprint"]');
check('controlHtml renders a Reprint button', Boolean(reprint));
check('Reprint is enabled when a printable job is present',
  Boolean(reprint) && reprint.disabled === false, reprint && String(reprint.disabled));

const noJob = doc.createElement('div');
noJob.innerHTML = window.controlHtml(Object.assign({}, base, { job: 'None' }));
check('Reprint is disabled with no job', noJob.querySelector('[data-action="reprint"]').disabled === true);

const bare = doc.createElement('div');
bare.innerHTML = window.controlHtml(Object.assign({}, base, { job: 'notes.txt' }));
check('Reprint is disabled for a non-printable job', bare.querySelector('[data-action="reprint"]').disabled === true);

/* --- clicking opens the print preview ------------------------------------ */

(async () => {
  window.wireStatic();
  await window.loadFleet();
  window.renderFleet();
  await sleep(40);
  window.initWebSocket();
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: { gcode_state: 'IDLE', subtask_name: 'Cube_plate_1.gcode.3mf' }
    })
  });
  await sleep(30);
  check('a printer is mounted', Boolean(card()));
  const btn = panel().querySelector('[data-action="reprint"]');
  check('the mounted card has an enabled Reprint',
    Boolean(btn) && btn.disabled === false, btn && String(btn.disabled));

  calls.length = 0;
  click(btn);
  await sleep(60);
  check('clicking Reprint opens the print preview',
    doc.getElementById('modal-print').classList.contains('flex'));
  check('clicking Reprint fetches the plan for the current job', planCalls() === 1, String(planCalls()));
  check('the plan request names the current job',
    calls.some((c) => c.url.indexOf('/files/plan') >= 0 && c.url.indexOf('Cube_plate_1.gcode.3mf') >= 0));
  window.closePrintDialog();

  /* --- FINISH without subtask_name must not hide Reprint ----------------- */
  // Repro of the field report: start a real print (which remembers the path in
  // state.printPath), then a late FINISH report arrives with subtask_name
  // dropped to '' so printer.job becomes 'None'. Reprint must stay visible
  // because it also sources from the remembered last-printed file. The exact
  // telemetry quirk could not be reproduced against a real printer locally;
  // this models the reported report shape.
  click(panel().querySelector('[data-action="reprint"]'));
  await sleep(60);
  doc.getElementById('print-start').click();
  await sleep(60);
  check('the print started (print-remote sent)',
    calls.some((c) => c.url.indexOf('/print-remote') >= 0));
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: { gcode_state: 'FINISH', subtask_name: '' }
    })
  });
  await sleep(30);
  const reprintAfterFinish = panel().querySelector('[data-job-btn="reprint"]');
  check('Reprint survives a FINISH with no subtask_name',
    Boolean(reprintAfterFinish) && !reprintAfterFinish.classList.contains('hidden'),
    reprintAfterFinish && reprintAfterFinish.className);
  check('Reprint stays enabled after that FINISH',
    Boolean(reprintAfterFinish) && reprintAfterFinish.disabled === false,
    reprintAfterFinish && String(reprintAfterFinish.disabled));

  // while running/paused it stays hidden
  window.__ws.onmessage({
    data: JSON.stringify({
      event: 'telemetry', printer_id: 'n1',
      data: { gcode_state: 'RUNNING', subtask_name: 'Cube_plate_1.gcode.3mf', mc_percent: 5 }
    })
  });
  await sleep(30);
  check('Reprint hidden while running',
    panel().querySelector('[data-job-btn="reprint"]').classList.contains('hidden'));

  window.closePrintDialog();

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });
