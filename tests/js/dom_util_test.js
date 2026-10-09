/* Unit tests for the pure helpers extracted to static/js/lib/util.js.
 *
 * These have no DOM or app.js at all - that is the point of the split: the
 * formatting and matching rules can be tested directly. */
const fs = require('fs');
const { JSDOM } = require('jsdom');

const utilJs = fs.readFileSync('static/js/lib/util.js', 'utf8');
const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  runScripts: 'outside-only', url: 'http://localhost/',
});
const { window } = dom;
window.eval(utilJs);

let pass = 0, fail = 0;
const check = (label, actual, expected) => {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) { pass += 1; console.log('PASS  ' + label); }
  else { fail += 1; console.log('FAIL  ' + label + ' -> got ' + JSON.stringify(actual) + ', want ' + JSON.stringify(expected)); }
};

check('baseName strips folders', window.baseName('/sdcard/apps/cube.gcode.3mf'), 'cube.gcode.3mf');
check('baseName strips backslashes', window.baseName('folder\\job.3mf'), 'job.3mf');
check('baseName of empty', window.baseName(''), '');
check('baseName of null', window.baseName(null), '');

check('escapeHtml escapes markup', window.escapeHtml('<b>&"x"</b>'), '&lt;b&gt;&amp;&quot;x&quot;&lt;/b&gt;');
check('escapeHtml of null', window.escapeHtml(null), '');
check('escapeHtml of a number', window.escapeHtml(42), '42');

check('num keeps finite numbers', window.num(3.5), 3.5);
check('num falls back on NaN', window.num(NaN, 7), 7);
check('num falls back on strings', window.num('12', 0), 0);

check('formatDuration minutes', window.formatDuration(120), '2m');
check('formatDuration hours', window.formatDuration(3900), '1h 05m');
check('formatDuration days', window.formatDuration(90000), '1d 1h');
check('formatDuration clamps negatives', window.formatDuration(-5), '0m');
check('formatDuration of null', window.formatDuration(null), '0m');

check('formatBytes bytes', window.formatBytes(512), '512 B');
check('formatBytes kilobytes', window.formatBytes(2048), '2.0 KB');
check('formatBytes megabytes', window.formatBytes(5 * 1024 * 1024), '5.0 MB');
check('formatBytes of junk', window.formatBytes(undefined), '0 B');

check('speedLabel maps the firmware levels', window.speedLabel({ speedLevel: 2 }), 'Standard 100%');
check('speedLabel of an unknown level', window.speedLabel({ speedLevel: 9 }), '');
check('speedLabel with no level', window.speedLabel({}), '');

check('printableJob accepts a 3mf', window.printableJob('cube.gcode.3mf'), true);
check('printableJob accepts a full path', window.printableJob('/apps/cube.gcode.3mf'), true);
check('printableJob rejects the None placeholder', window.printableJob('None'), false);
check('printableJob rejects other files', window.printableJob('photo.png'), false);
check('printableJob rejects empty', window.printableJob(''), false);

console.log('\n' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);