/* One printer at a time: the dropdown picks it, only that card is mounted,
   and the choice is remembered. */
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
  { id: 'bravo', name: 'X1C - Bravo', ip: '10.0.0.2', sn: 'BBB', bed_type: 'cool_plate', ams_count: 1 },
  { id: 'charlie', name: 'P1S - Charlie', ip: '10.0.0.3', sn: 'CCC', bed_type: 'cool_plate', ams_count: 0 }
];
window.fetch = (url) => {
  if (String(url).indexOf('/api/printers') >= 0) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(FLEET), text: () => Promise.resolve('') });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('1.3.0') });
};

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };

const select = () => window.document.getElementById('printer-select');
const mounted = () => [...window.document.querySelectorAll('[data-card-id]')].map((n) => n.dataset.cardId);

(async () => {
  await window.loadFleet();
  window.renderFleet();
  await new Promise((r) => setTimeout(r, 30));

  check('dropdown lists every printer', select().querySelectorAll('option').length === 3,
    select().querySelectorAll('option').length);
  check('dropdown labels carry status', select().innerHTML.indexOf('offline') >= 0);
  check('exactly one printer is mounted', mounted().length === 1, mounted().join(','));
  check('the first printer is selected by default', mounted()[0] === 'alpha', mounted()[0]);
  check('the select reflects the mounted printer', select().value === 'alpha', select().value);

  // pick a different printer
  select().value = 'bravo';
  select().dispatchEvent(new window.Event('change'));
  await new Promise((r) => setTimeout(r, 30));
  check('switching mounts exactly one card', mounted().length === 1, mounted().join(','));
  check('the chosen printer is the mounted one', mounted()[0] === 'bravo', mounted()[0]);
  check('the old card is gone', !window.document.querySelector('[data-card-id="alpha"]'));
  check('choice is remembered', window.localStorage.getItem('bfm-printer') === 'bravo');

  // a reload comes back to the same printer
  window.document.getElementById('fleet-grid').innerHTML = '';
  window.renderFleet();
  await new Promise((r) => setTimeout(r, 30));
  check('selection survives a re-render', mounted()[0] === 'bravo', mounted()[0]);

  // filtering away the selection falls back to a visible printer
  window.document.getElementById('fleet-filter').value = 'charlie';
  window.document.getElementById('fleet-filter').dispatchEvent(new window.Event('input'));
  await new Promise((r) => setTimeout(r, 30));
  check('filter narrows to the match', mounted()[0] === 'charlie', mounted()[0]);

  const opts = [...select().querySelectorAll('option')].map((o) => o.value);
  check('dropdown only offers filtered printers', opts.length === 1 && opts[0] === 'charlie', opts.join(','));

  // clearing the filter brings the fleet back and keeps the selection valid
  window.document.getElementById('fleet-filter').value = '';
  window.document.getElementById('fleet-filter').dispatchEvent(new window.Event('input'));
  await new Promise((r) => setTimeout(r, 30));
  check('clearing the filter mounts one card', mounted().length === 1, mounted().join(','));
  check('selection held across the filter change', mounted()[0] === 'charlie', mounted()[0]);

  // the removed duplicates really are gone from the card
  const card = window.document.querySelector('[data-card-id]');
  check('no quick Pause/Stop/Recover row', !card.querySelector('[data-cmd]'));
  check('no card pencil (edit)', !card.querySelector('[data-act="edit"]'));
  check('no card bulb (light)', !card.querySelector('[data-act="light"]'));
  check('no camera restart button', !card.querySelector('[data-action="cam-restart"]'));
  check('panel + remove buttons kept', Boolean(card.querySelector('[data-act="panel"]'))
    && Boolean(card.querySelector('[data-act="menu"]')));

  // and the state-dump duplicate is gone from the System tab, kept in Calibration
  const tabs = [...card.querySelectorAll('[data-tab]')].map((b) => b.dataset.tab);
  check('staging is now a tab', tabs.indexOf('staging') >= 0, tabs.join(','));

  // the staging tab mounts its own markup and counts
  const stagingTab = card.querySelector('[data-tab="staging"]');
  stagingTab.click();
  await new Promise((r) => setTimeout(r, 30));
  check('staging tab renders the drop zone', Boolean(window.document.getElementById('drop-zone')));
  check('staging tab renders the file input', Boolean(window.document.getElementById('file-input')));
  check('staging tab renders the staged list', Boolean(window.document.getElementById('staged-container')));
  check('staging tab renders the count', Boolean(window.document.getElementById('staged-count')));

  // switching away and back re-mounts the staging markup (listeners re-attached)
  card.querySelector('[data-tab="control"]').click();
  await new Promise((r) => setTimeout(r, 20));
  check('leaving staging removes the drop zone', !window.document.getElementById('drop-zone'));
  card.querySelector('[data-tab="staging"]').click();
  await new Promise((r) => setTimeout(r, 20));
  check('returning to staging re-mounts it', Boolean(window.document.getElementById('drop-zone')));

  // the System tab no longer carries a second state-dump button
  card.querySelector('[data-tab="system"]').click();
  await new Promise((r) => setTimeout(r, 20));
  const systemHtml = card.querySelector('[data-r="panel"]').innerHTML;
  check('no "Full state dump" in System', systemHtml.indexOf('Full state dump') < 0);

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });