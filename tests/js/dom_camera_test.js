/* Regression: going fullscreen must not tear down the camera stream.

   An IntersectionObserver pauses the stream when a card scrolls out of view.
   A fullscreen box leaves the normal flow, so the observer reports the card as
   off screen at the exact moment the user goes fullscreen - which used to stop
   the stream and flash "Press Start to open the live view." over a picture that
   was still live.

   The test drives the real code path (loadFleet -> renderFleet) so the live
   `cards` registry is populated, and asserts on the DOM, because `state` and
   `cards` are top-level consts and not reachable from an eval scope. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('templates/index.html', 'utf8');
const appJs = fs.readFileSync('static/js/app.js', 'utf8');

const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/' });
const { window } = dom;
window.requestAnimationFrame = (fn) => setTimeout(fn, 0);
window.matchMedia = (q) => ({ matches: false, media: q, addEventListener: () => {}, removeEventListener: () => {} });

const FLEET = [{
  id: 'n1', name: 'X1C', ip: '1.2.3.4', sn: 'SN', bed_type: 'cool_plate', ams_count: 1
}];
window.fetch = (url) => {
  if (String(url).indexOf('/api/printers') >= 0) {
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(FLEET), text: () => Promise.resolve('') });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}), text: () => Promise.resolve('1.2.0') });
};
window.WebSocket = class { constructor() {} close() {} send() {} addEventListener() {} };

// a controllable stand-in for IntersectionObserver
let observerCallback = null;
let observedCount = 0;
window.IntersectionObserver = class {
  constructor(cb) { observerCallback = cb; }
  observe() { observedCount++; }
  unobserve() {}
  disconnect() {}
};

// jsdom implements no fullscreen, so model the one bit the guard reads
let fullscreenElement = null;
Object.defineProperty(window.document, 'fullscreenElement', {
  get: () => fullscreenElement, configurable: true
});

window.eval(appJs);

let pass = 0, fail = 0;
const check = (l, ok, x = '') => { console.log((ok ? 'PASS  ' : 'FAIL  ') + l + (x ? ' -> ' + x : '')); ok ? pass++ : fail++; };

const $q = (s) => window.document.querySelector(s);
const overlay = () => $q('[data-card-id="n1"] [data-role="cam-overlay"]');
const camImg = () => $q('[data-card-id="n1"] [data-role="cam-img"]');
const camBox = () => $q('[data-card-id="n1"] [data-role="cam-box"]');
const streaming = () => Boolean(camImg().getAttribute('src'));
const fire = (isIntersecting) => observerCallback([{ isIntersecting }]);

(async () => {
  await window.loadFleet();
  window.renderFleet();
  // rAF-coalesced render
  await new Promise((r) => setTimeout(r, 30));

  check('card rendered into the fleet grid', Boolean($q('[data-card-id="n1"]')));
  check('card is observed by the observer', observedCount > 0);
  check('stream starts with the card', streaming());
  check('overlay hidden while streaming', overlay().classList.contains('hidden'));

  // scrolling the card out of view legitimately stops the stream
  fire(false);
  check('scrolling out of view stops the stream', !streaming());
  check('and shows the prompt', overlay().textContent === 'Press Start to open the live view.');

  // back in view
  fire(true);
  check('scrolling back into view restarts it', streaming());

  // THE BUG: fullscreen makes the observer report the card off screen
  fullscreenElement = camBox();
  fire(false);
  check('fullscreen does NOT stop the stream', streaming());
  check('no "Press Start" overlay while fullscreen', overlay().classList.contains('hidden'));

  // a browser that reports the card off-screen *before* fullscreenchange
  // fires is still covered: genuinely stop it, then go fullscreen
  fullscreenElement = null;
  fire(false);
  check('forced stop really stopped it', !streaming());
  fullscreenElement = camBox();
  window.document.dispatchEvent(new window.Event('fullscreenchange'));
  await new Promise((r) => setTimeout(r, 10));
  check('fullscreenchange restarts a stopped stream', streaming());

  // leaving fullscreen while the card is still in view keeps it running
  fullscreenElement = null;
  fire(true);
  await new Promise((r) => setTimeout(r, 10));
  check('still streaming after leaving fullscreen', streaming());

  // but it must not resurrect the stream when the user turned cameras off
  window.setCameraEnabled(false);
  check('cameras off stops the stream', !streaming());
  fullscreenElement = camBox();
  window.document.dispatchEvent(new window.Event('fullscreenchange'));
  await new Promise((r) => setTimeout(r, 10));
  check('fullscreenchange does not fight cameras-off', !streaming());

  // and the normal stop path is untouched
  window.setCameraEnabled(true);
  await new Promise((r) => setTimeout(r, 10));
  check('cameras on restarts the stream', streaming());
  check('status text reports the stream', $q('[data-card-id="n1"] [data-role="cam-status"]').textContent.length > 0);

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });