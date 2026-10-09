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

const tick = () => new Promise((r) => setTimeout(r, 25));
window.CAMERA_FIRST_FRAME_MS = 60;   // do not wait 12s in the suite
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
  check('and says so plainly (no false "press Start")',
    overlay().textContent === 'Camera stopped.', overlay().textContent);

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

  /* --- a failed stream must explain itself, not say "press Start" ------- */
  // the failure message used to be set and then immediately overwritten by
  // stopCamera, so a broken camera always looked like "press Start"
  const img = $q('[data-card-id="n1"] [data-role="cam-img"]');
  img.onerror();
  await tick();
  check('a failed stream stops it', !streaming());
  check('the failure reason is kept on screen',
    overlay().textContent.indexOf('Could not open the live stream') === 0, overlay().textContent);
  check('no misleading "press Start"', overlay().textContent.indexOf('Press Start') < 0);

  /* --- there is a real way to start it again ---------------------------- */
  const startBtn = $q('[data-card-id="n1"] [data-action="cam-start"]');
  check('the camera row offers a Start button', Boolean(startBtn));
  startBtn.click();
  await tick();
  check('Start restarts the stream', streaming());
  check('overlay hidden again while streaming', overlay().classList.contains('hidden'));

  /* --- a silent stream times out rather than hanging forever ------------ */
  // ffmpeg can connect and then deliver nothing (printer off / camera busy);
  // the button path is used here because `cards` is not reachable from a test
  $q('[data-card-id="n1"] [data-action="cam-stop"]').click();
  await tick();
  check('the Stop button stops it', !streaming());
  $q('[data-card-id="n1"] [data-action="cam-start"]').click();
  await tick();
  check('streaming again before the timeout', streaming());
  await new Promise((r) => setTimeout(r, 200));   // CAMERA_FIRST_FRAME_MS is 60 in this test
  check('a silent stream times out', !streaming());
  check('and explains the likely cause',
    overlay().textContent.indexOf('No frames from the camera') === 0, overlay().textContent);

  console.log('\n' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
})().catch((err) => { console.log('ERROR ' + err.stack); process.exit(1); });