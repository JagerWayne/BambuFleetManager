/* Runs every tests/js/*_test.js as its own child process and fails if any
   fails. Test files exit non-zero themselves; they are spawned (not required)
   because each one calls process.exit() and builds its own jsdom. Auto-
   discovers new suites, so dropping a dom_*_test.js here is enough for CI. */
const { spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const tests = fs.readdirSync(__dirname)
  .filter((f) => f.endsWith('_test.js'))
  .sort();

if (tests.length === 0) {
  console.error('no *_test.js files found in tests/js');
  process.exit(1);
}

/* Stream each suite's output live, but keep the summary ordered. */
let failed = 0;
for (const name of tests) {
  const t0 = Date.now();
  const res = spawnSync(process.execPath, [path.join(__dirname, name)], {
    stdio: ['ignore', 'pipe', 'pipe'],
    encoding: 'utf8',
  });
  const ok = res.status === 0;
  if (!ok) failed += 1;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  (${((Date.now() - t0) / 1000).toFixed(1)}s)`);
  const out = (res.stdout || '') + (res.stderr || '');
  if (out.trim()) {
    for (const line of out.trimEnd().split(/\r?\n/)) console.log(`      ${line}`);
  }
}

console.log(`\n${tests.length - failed}/${tests.length} suites passed`);
process.exit(failed ? 1 : 0);
