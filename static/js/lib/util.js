/* Pure helpers, split out of app.js so they can be reasoned about (and unit
 * tested) without the dashboard's DOM and state around them.
 *
 * Loaded as a plain <script> before app.js - these stay top-level globals, so
 * app.js keeps calling them by name. They are declared with \ar\ (not \const\)
 * on purpose: function-level \const\ does not survive window.eval, which is how the
 * jsdom suites load this file.
 * evaluating this file first. Nothing here may touch `state`, the DOM or fetch.
 */

/* Escape a value for interpolation into markup. */
var escapeHtml = (v) => String(v == null ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

/* Coerce to a finite number, or the fallback. Telemetry is not always typed. */
var num = (v, fallback = 0) => (typeof v === 'number' && isFinite(v) ? v : fallback);

/* The last path segment. We often hold a full SD path while the printer
 * reports a bare subtask_name, so job matching strips folders first. */
function baseName(p) {
  const parts = String(p == null ? '' : p).split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

/* Speed profile levels, as the firmware numbers them (verified on an X1C). */
var SPEED_LABELS = { 1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous' };
var SPEED_SHORT = { 1: 'S', 2: 'N', 3: 'Sp', 4: 'L' };
var SPEED_PERCENT = { 1: 50, 2: 100, 3: 124, 4: 166 };

function speedLabel(printer) {
  const level = printer.speedLevel;
  if (!level || !SPEED_LABELS[level]) return '';
  return `${SPEED_LABELS[level]} ${SPEED_PERCENT[level]}%`;
}

/* Human duration: "2d 3h", "1h 05m", "42m". */
function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
  return `${m}m`;
}

function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

function formatDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' +
    d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/* Is this something the printer can actually be sent? */
function printableJob(job) {
  return Boolean(job) && job !== 'None' && /\.(3mf|gcode)$/i.test(baseName(job));
}