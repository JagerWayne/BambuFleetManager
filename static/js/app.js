/* =============================================================
 * Bambu Fleet Manager - dashboard controller
 *
 * Rendering: cards are created once and mutated through cached
 * element references, coalesced on requestAnimationFrame. Telemetry
 * arrives several times a second, so nothing here may re-serialise
 * a card's markup from the live feed.
 * ============================================================= */

const state = {
  fleet: [],
  staged: [],
  dragFile: null,
  socket: null,
  filter: '',
  allowMotion: false,
  selectedId: null,
  speedModalPrinter: null,
  cameraEnabled: true,
  print: { printerId: null, path: null, plan: null, plate: 1, mapping: {} },
  // full path of the file each printer is running, so the skip bed can find it
  printPath: {},
  // plate geometry per "path|plate"; null means "tried, unavailable" so we do not refetch
  plateGeom: {},
  plateGeomPending: {},
  // pending skip selection + the printer/path/plate the skip modal is editing
  skipSelection: new Set(),
  skipCtx: null,
  // Pre-armed skips: printerId -> { job, ids }. In-memory only, never
  // persisted, and applied once the matching job starts printing.
  preSkip: {},
  trayPicker: null,
  filament: null,
  filaments: [],
  filamentCategories: []
};

const cards = new Map(); // printer id -> { root, refs, expanded, tab, fileCache }
let renderQueued = false;

/* ------------------------------------------------------------- utilities */

const $ = (id) => document.getElementById(id);

const escapeHtml = (v) => String(v == null ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const num = (v, fallback = 0) => (typeof v === 'number' && isFinite(v) ? v : fallback);

// The last path segment. We often hold a full SD path while the printer
// reports a bare subtask_name, so job matching strips folders first.
function baseName(p) {
  const parts = String(p == null ? '' : p).split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

/* Speed profile levels, as the firmware numbers them (verified on an X1C). */
const SPEED_LABELS = { 1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous' };
const SPEED_SHORT = { 1: 'S', 2: 'N', 3: 'Sp', 4: 'L' };
const SPEED_PERCENT = { 1: 50, 2: 100, 3: 124, 4: 166 };

function speedLabel(printer) {
  const level = printer.speedLevel;
  if (!level || !SPEED_LABELS[level]) return '';
  return `${SPEED_LABELS[level]} ${SPEED_PERCENT[level]}%`;
}

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

function printerById(id) { return state.fleet.find((p) => p.id === id); }

function isOnline(p) { return p.seenAt && Date.now() - p.seenAt < 30000; }

/* ---------------------------------------------------------------- toasts */

function toast(message, kind = 'info') {
  const palette = {
    info: 'line-accent t-accent',
    error: 'border-danger t-danger',
    warn: 'border-warn t-warn'
  };
  const el = document.createElement('div');
  el.className = `pointer-events-auto card max-w-xs px-3.5 py-2.5 text-[11px] font-mono font-bold ${palette[kind] || palette.info}`;
  el.style.animation = 'cardIn .2s ease-out';
  el.textContent = message;
  $('toasts').appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .3s ease';
    setTimeout(() => el.remove(), 320);
  }, 3600);
}

/* ------------------------------------------------------------------ API */

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (res.status === 401) { window.location.href = '/login'; throw new Error('authentication required'); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not JSON */ }
    const error = new Error(detail);
    error.status = res.status;
    throw error;
  }
  return res.status === 204 ? null : res.json();
}

async function post(path, body) {
  try {
    await api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    return true;
  } catch (err) {
    toast(err.message, 'error');
    return false;
  }
}

async function sendCommand(printerId, command, params = {}, confirm = false) {
  if (await post(`/api/printers/${printerId}/command`, { command, params, confirm })) {
    toast(`${command.replace(/_/g, ' ')} sent`);
    return true;
  }
  return false;
}

/* ------------------------------------------------------- telemetry model */

// Skipped ids describe a single job. Clear them when the job changes or the
// printer leaves the running/paused states, so a previous print's skips cannot
// bleed into the next.
//
// A pre-armed skip is only dropped once it can no longer apply: the armed job
// itself reached a terminal state, or the printer is actively running a
// different, *named* job. An empty / 'None' subtask_name is "unknown", not a
// different job - during idle -> prepare the printer's name is transiently
// blank, and treating that as a change deleted the arm at the exact moment it
// was supposed to fire.
function clearSkipState(printer, t) {
  const job = t.subtask_name !== undefined ? (t.subtask_name || 'None') : printer.job;
  const changed = printer._skipJob !== undefined && printer._skipJob !== job;
  printer._skipJob = job;
  const st = printer.status;
  const ended = st === 'finish' || st === 'failed' || st === 'idle';
  if (changed || ended) printer.skippedIds = [];
  const arm = state.preSkip[printer.id];
  if (!arm) return;
  const named = (n) => !!n && n !== 'None';
  const armedJob = baseName(arm.job);
  const currentJob = named(job) ? baseName(job) : null;
  const armedEnded = named(job) && currentJob === armedJob && (st === 'finish' || st === 'failed');
  const otherJob = st !== 'idle' && currentJob !== null && currentJob !== armedJob;
  if (armedEnded || otherJob) delete state.preSkip[printer.id];
}

function applyTelemetry(printer, t) {
  if (t.gcode_state) printer.status = String(t.gcode_state).toLowerCase();
  if (t.mc_percent !== undefined) printer.progress = num(t.mc_percent);
  if (t.mc_remaining_time !== undefined) printer.remainingSec = num(t.mc_remaining_time) * 60;
  if (t.nozzle_temper !== undefined) printer.nozzleTemp = Math.round(num(t.nozzle_temper));
  if (t.nozzle_target_temper !== undefined) printer.nozzleTarget = Math.round(num(t.nozzle_target_temper));
  if (t.bed_temper !== undefined) printer.bedTemp = Math.round(num(t.bed_temper));
  if (t.bed_target_temper !== undefined) printer.bedTarget = Math.round(num(t.bed_target_temper));
  if (t.subtask_name !== undefined) printer.job = t.subtask_name || 'None';
  if (t.layer_num !== undefined) printer.layer = num(t.layer_num);
  if (t.total_layer_num !== undefined) printer.totalLayers = num(t.total_layer_num);
  if (t.mc_print_stage !== undefined) printer.stage = t.mc_print_stage;

  // The live object list. On firmware that reports real per-object data,
  // job.stage[] (with job.cur_stage.idx) carries it; the X1 firmware instead
  // sends one generic stage record with no names, so only trust job.stage when
  // it actually looks like an object list (more than one entry, or a named
  // one). Otherwise leave printer.objects alone and fall back to the sliced
  // plate geometry for the skip UI.
  const stages = (t.job && Array.isArray(t.job.stage)) ? t.job.stage : null;
  if (stages && (stages.length > 1
      || stages.some((s) => String((s || {}).name || '').trim()))) {
    printer.objects = stages.map((s, i) => ({
      id: num((s || {}).idx, i),
      name: String((s || {}).name || '').trim(),
      tool: Array.isArray((s || {}).tool) ? s.tool.filter(Boolean).join(' / ') : '',
      estMin: num((s || {}).est_time, 0)
    }));
  }

  // s_obj is the list of ids the printer has ALREADY skipped, not the object
  // list. Merge it into skippedIds so an applied skip stays locked.
  if (Array.isArray(t.s_obj) && t.s_obj.length) {
    const skipped = t.s_obj.filter((v) => typeof v === 'number');
    if (skipped.length) {
      printer.skippedIds = [...new Set([...(printer.skippedIds || []), ...skipped])];
    }
  }

  const curStage = t.job && t.job.cur_stage ? t.job.cur_stage.idx : undefined;
  if (curStage !== undefined && curStage !== null) printer.objectIndex = num(curStage, 0);
  else if (t.mc_print_sub_stage !== undefined) printer.objectIndex = num(t.mc_print_sub_stage, 0);

  // Skipped ids belong to one job; drop them when the job changes or ends so a
  // previous print cannot leak into the next.
  clearSkipState(printer, t);
  if (t.print_error !== undefined) printer.printError = t.print_error ? num(t.print_error) : 0;
  if (t.nozzle_diameter !== undefined) printer.nozzleDiameter = t.nozzle_diameter;
  if (t.nozzle_type) printer.nozzleType = t.nozzle_type;
  if (t.wifi_signal) printer.wifi = t.wifi_signal;
  if (t.sdcard !== undefined) printer.sdcard = Boolean(t.sdcard);
  if (t.model_id !== undefined) printer.modelId = t.model_id;
  if (t.spd_lvl !== undefined) printer.speedLevel = num(t.spd_lvl);

  if (Array.isArray(t.lights_report)) {
    const lights = {};
    t.lights_report.forEach((l) => { if (l && l.node) lights[l.node] = l.mode; });
    printer.lights = lights;
  }

  if (Array.isArray(t.sp_speed)) {
    printer.speed = `${Math.round(num(t.sp_speed[0]))} mm/s`;
  } else if (t.spd_mag !== undefined) {
    printer.speed = `${Math.round(num(t.spd_mag))}%`;
  }
  if (t.cooling_fan_speed !== undefined) printer.fan = Math.round(num(t.cooling_fan_speed));
  if (t.big_fan1_speed !== undefined) printer.auxFan = Math.round(num(t.big_fan1_speed));
  if (t.big_fan2_speed !== undefined) printer.chamberFan = Math.round(num(t.big_fan2_speed));

  if (t.ams) printer.ams = t.ams;
  if (t.ams_status !== undefined) printer.amsStatus = num(t.ams_status);
  if (t.vt_tray) printer.vtTray = t.vt_tray;
  if (t.ipcam) printer.ipcam = t.ipcam;
  if (t.xcam_status !== undefined) printer.xcamStatus = t.xcam_status;

  // Chamber temperature lives under device.ctc on X1/active-chamber models.
  if (t.device && t.device.ctc && t.device.ctc.info && t.device.ctc.info.temp !== undefined) {
    printer.chamberTemp = Math.round(num(t.device.ctc.info.temp));
  }
  if (t.ctt_val !== undefined) printer.chamberTarget = Math.round(num(t.ctt_val));

  if (Array.isArray(t.hms)) {
    printer.hms = t.hms.filter((h) => num(h.attr) !== 0);
  }
  // A FAILED state with no code and no HMS is a stale leftover, not an error.
  printer.activeError = Boolean(printer.printError) || Boolean((printer.hms || []).length);
  printer.seenAt = Date.now();

  // An armed skip fires as soon as the matching job starts printing.
  applyPreSkip(printer);
}

// Pre-armed skips ride along with the print request (see startPrintFromDialog);
// this is the FALLBACK for a print started outside the app - from the printer's
// own screen, say - where no request carried the selection. It fires once the
// job is actually running, exactly once, guarded against the several telemetry
// ticks a second. It cannot fight the primary path: when the backend applied
// the skip it echoes the ids back, the arm is deleted and nothing is left to
// send here.
async function applyPreSkip(printer) {
  const arm = state.preSkip[printer.id];
  if (!arm) return;
  const st = printer.status;
  if (st !== 'running') {
    // A stale arm never fires on a later, different job: anything still armed
    // when the job ends (finish/failed) was never applied, so drop it.
    if (st === 'finish' || st === 'failed') delete state.preSkip[printer.id];
    return;
  }
  if (printer._preSkipSending) return;
  if (baseName(arm.job) !== baseName(printer.job)) return;
  const ids = Array.isArray(arm.ids) ? arm.ids : [];
  if (!ids.length) { delete state.preSkip[printer.id]; return; }

  printer._preSkipSending = true;
  const ok = await post(`/api/printers/${printer.id}/skip-objects`, { object_ids: ids });
  printer._preSkipSending = false;
  if (!ok) return;  // leave the arm in place for a retry
  printer.skippedIds = [...new Set([...(printer.skippedIds || []), ...ids])];
  delete state.preSkip[printer.id];
  toast(`Pre-armed skip sent for ${ids.length} object${ids.length === 1 ? '' : 's'}.`, 'ok');
  scheduleRender();
}

function tone(status, online) {
  if (!online) return { dot: 'bg-slate-500', chip: '' };
  switch (status) {
    case 'running': return { dot: 'bg-accent pulse-dot t-accent', chip: 'chip-ok' };
    case 'idle': return { dot: 'bg-sky-400', chip: 'chip-info' };
    case 'finish': return { dot: 'bg-amber-400', chip: 'chip-warn' };
    case 'failed': return { dot: 'bg-red-500', chip: 'chip-danger' };
    case 'paused': return { dot: 'bg-orange-400', chip: 'chip-warn' };
    default: return { dot: 'bg-slate-500', chip: '' };
  }
}

/* ------------------------------------------------------------- fleet sync */

async function loadFleet() {
  try {
    const data = await api('/api/printers');
    const previous = new Map(state.fleet.map((p) => [p.id, p]));
    state.fleet = data.map((cfg) => {
      const prev = previous.get(cfg.id);
      const base = {
        status: 'idle', progress: 0, remainingSec: 0, nozzleTemp: 0, nozzleTarget: 0,
        bedTemp: 0, bedTarget: 0, job: 'None', speed: '-', fan: 0, auxFan: 0, ams: null,
        lights: null, light: false, fan: 0, auxFan: 0, chamberFan: 0, printError: 0, homed: null, seenAt: null
      };
      return Object.assign({}, base, prev || {}, {
        id: cfg.id, name: cfg.name, ip: cfg.ip, sn: cfg.sn,
        bed_type: cfg.bed_type, ams_count: cfg.ams_count, __prev: prev || null
      });
    });
    scheduleRender();
  } catch (err) {
    toast(`Could not load fleet: ${err.message}`, 'error');
  }
}

/* -------------------------------------------------------------- websocket */

function setWsBadge(text, ok) {
  const badge = $('ws-badge');
  if (!badge) return;
  badge.className = `chip border ${ok
    ? 'chip-ok'
    : 'line surface t-mut'}`;
  badge.innerHTML = `<span class="h-1.5 w-1.5 rounded-full ${ok ? 'bg-accent' : 'bg-slate-500'}"></span> ${text}`;
}

function initWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  state.socket = new WebSocket(`${protocol}//${window.location.host}/ws`);

  state.socket.onopen = () => setWsBadge('live', true);
  state.socket.onmessage = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch (_) { return; }

    if (payload.event === 'hello' && payload.printers) {
      Object.entries(payload.printers).forEach(([id, data]) => {
        const printer = printerById(id);
        if (!printer) return;
        applyTelemetry(printer, data);
        if ('homed' in data) printer.homed = data.homed;
      });
      scheduleRender();
    } else if (payload.event === 'telemetry') {
      const printer = printerById(payload.printer_id);
      if (!printer) return;
      applyTelemetry(printer, payload.data || {});
      if ('homed' in payload) printer.homed = payload.homed;
      scheduleRender();
    }
  };
  state.socket.onclose = () => {
    setWsBadge('offline', false);
    setTimeout(initWebSocket, 3000);
  };
  state.socket.onerror = () => state.socket.close();
}

/* --------------------------------------------------- incremental rendering */

function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => { renderQueued = false; renderFleet(); });
}

/* ------------------------------------------------------- printer selection */

// One printer is mounted at a time. The dropdown picks which one; the choice is
// remembered so a reload (or a phone coming back to life) lands on the same node.
function selectedPrinter() {
  const found = state.fleet.find((p) => p.id === state.selectedId);
  if (found) return found;
  return state.fleet[0] || null;
}

function selectPrinter(id, remember = true) {
  if (!id || state.selectedId === id) { renderFleet(); return; }
  state.selectedId = id;
  if (remember) {
    try { localStorage.setItem('bfm-printer', id); } catch (_) { /* ignore */ }
  }
  // the previous card's camera and observer go with it
  cards.forEach((entry, key) => {
    if (key !== id) { entry.destroy(); entry.root.remove(); cards.delete(key); }
  });
  renderFleet();
}

function restoreSelection() {
  let saved = null;
  try { saved = localStorage.getItem('bfm-printer'); } catch (_) { /* ignore */ }
  if (saved) state.selectedId = saved;
}

/* Fill the dropdown from the fleet. Each option carries the printer's live
   status so the list itself reads at a glance. */
function renderPrinterSelect(list, selectedId) {
  const select = $('printer-select');
  if (!select) return;
  const dot = $('printer-dot');

  const options = list.map((p) => {
    const online = isOnline(p);
    const mark = online
      ? (p.status === 'running' ? '● printing'
        : p.status === 'failed' || p.printError ? '● error'
          : p.status === 'idle' ? '● idle' : `● ${p.status}`)
      : '● offline';
    return `<option value="${escapeHtml(p.id)}"${p.id === selectedId ? ' selected' : ''}>`
      + `${escapeHtml(p.name)} — ${mark}</option>`;
  }).join('');

  const sig = options;
  if (select.dataset.sig !== sig) {
    select.dataset.sig = sig;
    select.innerHTML = options || '<option value="">No printers registered</option>';
  }
  if (select.value !== selectedId) select.value = selectedId || '';

  const current = list.find((p) => p.id === selectedId);
  if (dot) {
    const t = tone(current ? current.status : '', Boolean(current && isOnline(current)));
    dot.className = `h-2 w-2 shrink-0 rounded-full ${current ? t.dot.split(' ')[0] : 'bg-slate-500'}`;
    dot.classList.add(...(current ? t.dot.split(' ').slice(1) : []));
  }
}

function renderFleet() {
  const grid = $('fleet-grid');
  const needle = state.filter.toLowerCase();
  const visible = state.fleet.filter((p) =>
    !needle || `${p.name} ${p.ip} ${p.sn}`.toLowerCase().includes(needle));

  const online = state.fleet.filter(isOnline).length;
  $('v-online').textContent = online;
  $('v-printing').textContent = state.fleet.filter((p) => p.status === 'running').length;
  $('v-idle').textContent = state.fleet.filter((p) => p.status === 'idle').length;
  $('v-error').textContent = state.fleet.filter((p) => p.status === 'failed' || p.printError).length;

  renderPrinterSelect(visible, state.selectedId);

  // only bother with a filter box once the fleet is big enough to need one
  const filter = $('fleet-filter');
  if (filter) filter.classList.toggle('hidden', state.fleet.length <= 8);

  if (!visible.length) {
    cards.forEach((entry) => { entry.destroy(); entry.root.remove(); });
    cards.clear();
    grid.innerHTML = `<div class="card col-span-full p-12 text-center">
      <p class="text-sm font-bold t-body">${state.fleet.length ? 'No node matches that filter' : 'No printer nodes yet'}</p>
      <p class="mt-1 text-xs t-mut">${state.fleet.length ? 'Clear the filter to see every node.' : 'Register a node with its LAN IP, serial number and access code.'}</p>
      <button class="btn btn-primary mt-4" data-empty-add>Register a node</button>
    </div>`;
    const add = grid.querySelector('[data-empty-add]');
    if (add) add.addEventListener('click', () => openModal());
    return;
  }

  // keep the selection valid as the fleet changes, *before* drawing the dropdown
  // so the control never renders blank on the first pass
  const selected = visible.find((p) => p.id === state.selectedId) || visible[0];
  if (selected.id !== state.selectedId) {
    state.selectedId = selected.id;
    try { localStorage.setItem('bfm-printer', selected.id); } catch (_) { /* ignore */ }
  }
  renderPrinterSelect(visible, selected.id);

  cards.forEach((entry, id) => {
    if (id !== selected.id) { entry.destroy(); entry.root.remove(); cards.delete(id); }
  });

  let entry = cards.get(selected.id);
  if (!entry) { entry = createCard(selected); cards.set(selected.id, entry); }
  updateCard(selected, entry);
  if (grid.firstChild !== entry.root) grid.replaceChildren(entry.root);
}

/* --------------------------------------------------------------- card DOM */

function createCard(printer) {
  const root = document.createElement('article');
  root.dataset.cardId = printer.id;
  root.className = 'card card-enter flex flex-col overflow-hidden lg:h-[540px] 2xl:h-[600px]';

  root.innerHTML = `
    <!-- identity above the camera; the controls below are always shown -->
    <div class="flex items-start justify-between gap-x-3 gap-y-2 border-b line px-3 py-2.5 sm:px-4">
      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-2">
          <span class="h-2 w-2 shrink-0 rounded-full" data-r="dot"></span>
          <h3 class="min-w-0 truncate text-[15px] font-bold t-strong" data-r="name"></h3>
          <span class="chip shrink-0" data-r="chip"></span>
          <span class="chip shrink-0 hidden" data-r="homing"></span>
        </div>
        <div class="mt-0.5 truncate font-mono text-[10px] t-mut" data-r="meta"></div>
      </div>
    </div>

    <div class="flex min-h-0 flex-1 flex-col lg:flex-row">

      <!-- left: camera + everything at a glance -->
      <div class="card-aside flex shrink-0 flex-col gap-2 border-b p-3 line surface-2 lg:h-full lg:overflow-y-auto lg:border-b-0 lg:border-r">

        <div data-role="cam-box"
             class="cam-box relative aspect-video w-full shrink-0 overflow-hidden rounded-lg border line bg-black">
          <img data-role="cam-img" alt="printer camera" loading="eager"
               class="hidden h-full w-full object-contain">
          <img data-role="cam-snap" alt="snapshot"
               class="pointer-events-none absolute inset-0 hidden h-full w-full object-contain">
          <div data-role="cam-overlay"
               class="absolute inset-0 grid place-items-center px-3 text-center font-mono text-[10px] t-mut">
            Connecting to camera…
          </div>
          <div data-role="cam-hud" class="cam-hud">
            <span class="t-strong" data-r="hudNozzle"></span>
            <span class="t-strong" data-r="hudBed"></span>
            <span class="t-accent" data-r="hudProgress"></span>
            <span class="min-w-0 flex-1 truncate t-body" data-r="hudJob"></span>
            <span class="t-mut" data-r="hudRemaining"></span>
            <button type="button" class="icon-btn shrink-0" data-action="cam-normal" aria-label="Exit fullscreen">✕</button>
          </div>
        </div>

        <div class="flex shrink-0 items-center gap-1">
          <button class="icon-btn" data-action="cam-start" title="Start stream">▶</button>
          <button class="icon-btn" data-action="cam-stop" title="Stop stream">■</button>
          <button class="icon-btn" data-action="cam-snapshot" title="Save snapshot">◉</button>
          <button class="icon-btn" data-action="cam-fullscreen" title="Fullscreen">⛶</button>
          <span data-role="cam-status" class="ml-auto font-mono text-[9px] t-mut"></span>
        </div>

        <div class="grid shrink-0 grid-cols-2 gap-2">
          <button class="tile tile-inline p-2 text-left transition hover:line" data-action="open-temp" title="Set temperatures">
            <span class="block text-[9px] uppercase tracking-wider t-mut">Nozzle</span>
            <span class="font-mono text-base font-bold leading-tight" data-r="nozzle"></span>
            <span class="block font-mono text-[10px] t-mut" data-r="nozzleTarget"></span>
          </button>
          <button class="tile tile-inline p-2 text-left transition hover:line" data-action="open-temp" title="Set temperatures">
            <span class="block text-[9px] uppercase tracking-wider t-mut">Bed</span>
            <span class="font-mono text-base font-bold leading-tight" data-r="bed"></span>
            <span class="block font-mono text-[10px] t-mut" data-r="bedTarget"></span>
          </button>
        </div>

        <button class="tile flex shrink-0 items-center justify-between gap-2 p-2 text-left transition hover:line"
                data-action="speed-modal" title="Change speed profile">
          <span class="min-w-0">
            <span class="block text-[9px] uppercase tracking-wider t-mut">Speed</span>
            <span class="block truncate font-mono text-sm font-bold t-strong" data-r="speed"></span>
          </span>
          <span class="shrink-0 font-mono text-[10px] t-accent">change ▸</span>
        </button>

        <button class="tile shrink-0 p-2 text-left transition hover:line" data-action="open-control" title="Job details">
          <span class="mb-1 flex items-baseline justify-between gap-2 font-mono text-[11px]">
            <span class="truncate t-body" data-r="job"></span>
            <span class="shrink-0 font-bold t-accent" data-r="progress"></span>
          </span>
          <span class="block h-2 w-full overflow-hidden rounded-full surface-3">
            <span class="block h-full rounded-full bg-gradient-to-r from-bambu-dark to-bambu transition-all duration-500" data-r="bar" style="width:0%"></span>
          </span>
          <span class="mt-1 flex justify-between font-mono text-[10px] t-mut">
            <span data-r="stage"></span><span data-r="remaining"></span>
          </span>
        </button>

        <div class="hidden shrink-0 space-y-0.5 pt-1 font-mono text-[10px]" data-r="alerts"></div>

      </div>

      <!-- right: the tabbed control panel -->
      <div class="flex min-w-0 flex-1 flex-col">
        <div class="flex items-center gap-1 overflow-x-auto border-b line px-3 pb-2 sm:px-4" data-r="tabbar"></div>
        <div class="min-h-0 flex-1 overflow-y-auto p-3" data-r="panel"></div>
      </div>
    </div>
  `;

  const refs = {};
  root.querySelectorAll('[data-r]').forEach((n) => { refs[n.dataset.r] = n; });

  refs.tabbar.innerHTML = TABS.map(([key, label]) =>
    `<button class="tab" data-tab="${key}" aria-selected="false">${label}</button>`).join('');

  const entry = {
    root, refs, tab: 'control', jogStep: 1, camera: null,
    printerId: printer.id, inView: false,
    fileCache: { path: '/', listing: null, loading: false, error: null },
    camRefs: {
      img: root.querySelector('[data-role="cam-img"]'),
      overlay: root.querySelector('[data-role="cam-overlay"]'),
      status: root.querySelector('[data-role="cam-status"]'),
      box: root.querySelector('[data-role="cam-box"]'),
      snap: root.querySelector('[data-role="cam-snap"]')
    },
    destroy() { stopCamera(this); if (this.observer) this.observer.disconnect(); }
  };

  function selectTab(key) {
    entry.tab = key;  // controls are always shown, so a tab never collapses
    renderPanel(printerById(printer.id), entry, true);
  }

  refs.tabbar.addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]');
    if (b) selectTab(b.dataset.tab);
  });

  // clicks inside the card (the summary column and the panel) are handled by
  // the single listener below - a second one on the panel would fire twice
  root.addEventListener('click', (e) => {
    const act = e.target.closest('[data-action]');
    if (!act) return;
    const action = act.dataset.action;
    if (action === 'open-temp') { entry.tab = 'temperature'; renderPanel(printerById(printer.id), entry, true); }
    else if (action === 'open-control') { entry.tab = 'control'; renderPanel(printerById(printer.id), entry, true); }
    else if (action === 'speed-modal') { openSpeedModal(printer.id); }
    else { handleAction(printer.id, act, entry); }
  });

  root.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; root.classList.add('drag-target-hover'); });
  root.addEventListener('dragleave', () => root.classList.remove('drag-target-hover'));
  root.addEventListener('drop', (e) => {
    e.preventDefault();
    root.classList.remove('drag-target-hover');
    const filename = state.dragFile || e.dataTransfer.getData('text/plain');
    if (filename) dispatchToPrinter(printer.id, filename);
  });

// Start the stream only while the card is on screen (one ffmpeg per visible
// card, not per configured printer).
  if ('IntersectionObserver' in window) {
    entry.observer = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        entry.inView = e.isIntersecting;
        // A fullscreen box leaves the normal flow, so the observer reports the
        // card as off screen right as the user goes fullscreen. Stopping here
        // would kill a stream that is plainly still running.
        if (e.isIntersecting && state.cameraEnabled) startCamera(printer, entry);
        else if (!e.isIntersecting && !isCamFullscreen(entry)) stopCamera(entry);
      });
    }, { rootMargin: '250px' });
    entry.observer.observe(root);
    if (state.cameraEnabled) startCamera(printer, entry);
  } else if (state.cameraEnabled) {
    startCamera(printer, entry);
  }

  return entry;
}

function updateCard(printer, entry) {
  const { refs } = entry;
  const online = isOnline(printer);
  const t = tone(printer.status, online);
  const printing = printer.status === 'running';

  entry.root.classList.toggle('is-printing', printing);

  refs.dot.className = `h-2 w-2 shrink-0 rounded-full ${t.dot}`;
  refs.name.textContent = printer.name;
  refs.chip.className = `chip ${t.chip}`;
  refs.chip.textContent = online ? printer.status : 'offline';
  refs.meta.textContent = `${printer.ip} · ${printer.sn}`;
  if (printer.homed === false) {
    refs.homing.className = 'chip border-warn bg-warn-soft t-warn';
    refs.homing.textContent = 'unhomed';
  } else {
    refs.homing.className = 'chip hidden';
  }

  refs.nozzle.textContent = `${printer.nozzleTemp}°`;
  refs.nozzle.className = `font-mono text-base font-bold leading-tight ${printer.nozzleTemp > 45 ? 't-ok' : 't-body'}`;
  refs.nozzleTarget.textContent = printer.nozzleTarget ? `→ ${printer.nozzleTarget}°` : 'idle';
  refs.bed.textContent = `${printer.bedTemp}°`;
  refs.bed.className = `font-mono text-base font-bold leading-tight ${printer.bedTemp > 40 ? 't-warn' : 't-body'}`;
  refs.bedTarget.textContent = printer.bedTarget ? `→ ${printer.bedTarget}°` : 'idle';
  refs.speed.textContent = speedLabel(printer) || '–';

  refs.job.textContent = printer.job || 'None';
  refs.progress.textContent = `${Math.round(printer.progress || 0)}%`;
  refs.bar.style.width = `${Math.max(0, Math.min(100, printer.progress || 0))}%`;
  refs.stage.textContent = (printer.layer && printer.totalLayers) ? `layer ${printer.layer}/${printer.totalLayers}` : '';
  refs.remaining.textContent = printing ? `~${formatDuration(printer.remainingSec)} left` : '';

  // fullscreen HUD readouts (only visible when the camera box is fullscreen)
  if (refs.hudNozzle) {
    refs.hudNozzle.textContent = `${printer.nozzleTemp}°`;
    refs.hudBed.textContent = `${printer.bedTemp}°`;
    refs.hudProgress.textContent = `${Math.round(printer.progress || 0)}%`;
    refs.hudJob.textContent = printer.job || 'None';
    refs.hudRemaining.textContent = printing ? `~${formatDuration(printer.remainingSec)} left` : '';
  }

  // errors / HMS codes, formatted the way the printer shows them
  if (refs.alerts) {
    const lines = [];
    if (printer.status === 'failed' && printer.activeError) {
      lines.push('<div class="t-danger">Job failed — press Clear error (Control)</div>');
    } else if (printer.status === 'failed') {
      lines.push('<div class="t-dim">leftover failed state (no active error)</div>');
    }
    if (state.allowMotion) lines.push('<div class="t-warn">moves unlocked</div>');
    if (printer.printError) lines.push(`<div class="t-danger">print error ${printer.printError}</div>`);
    (printer.hms || []).slice(0, 3).forEach((h) => {
      const hx = (v) => (v & 0xFFFF).toString(16).padStart(4, '0');
      const code = `HMS_${hx(h.attr >> 16)}_${hx(h.attr)}_${hx(h.code >> 16)}_${hx(h.code)}`.toUpperCase();
      lines.push(`<div class="t-warn">${code}</div>`);
    });
    const html = lines.join('');
    if (refs.alerts.dataset.sig !== html) {
      refs.alerts.dataset.sig = html;
      refs.alerts.innerHTML = html;
      refs.alerts.className = html
        ? 'shrink-0 space-y-0.5 pt-1 font-mono text-[10px]'
        : 'hidden shrink-0 space-y-0.5 pt-1 font-mono text-[10px]';
    }
  }

  renderPanel(printer, entry);
}

const TABS = [
  ['control', 'Control'],
  ['jog', 'Jog'],
  ['temperature', 'Temp'],
  ['files', 'Files'],
  ['ams', 'AMS'],
  ['system', 'System'],
  ['staging', 'Staging']
];

function renderPanel(printer, entry, force = false) {
  if (!printer || !entry.refs.panel) return;
  const body = entry.refs.panel;

  entry.refs.tabbar.querySelectorAll('[data-tab]').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.tab === entry.tab)));

  if (entry.tab === 'staging') {
    body.dataset.built = 'staging';
    body.innerHTML = stagingHtml();
    wireStaging(body);
    return renderStaged();
  }
  if (entry.tab === 'files') { body.dataset.built = 'files'; return renderFiles(printer, body); }
  if (entry.tab === 'ams') { body.dataset.built = 'ams'; return renderAms(printer, body, force); }

  if (force || body.dataset.built !== entry.tab) {
    body.dataset.built = entry.tab;
    if (entry.tab === 'control') body.innerHTML = controlHtml(printer);
    else if (entry.tab === 'jog') body.innerHTML = jogHtml(printer, entry);
    else if (entry.tab === 'temperature') body.innerHTML = temperatureHtml(printer);
    else body.innerHTML = systemHtml(printer);
  }
  if (entry.tab !== 'system') refreshLive(body, printer);
}

/* Patch the dynamic values in place (temperatures, homes, reported states). */
function refreshLive(body, printer) {
  const set = (key, value, cls) => {
    body.querySelectorAll(`[data-live="${key}"]`).forEach((node) => {
      node.textContent = value;
      if (cls) node.className = cls;
    });
  };
  set('nozzle-now', `${printer.nozzleTemp || 0}°`);
  set('bed-now', `${printer.bedTemp || 0}°`);
  set('chamber-now', `${printer.chamberTemp != null ? printer.chamberTemp : 0}°`);
  set('homed',
    printer.homed == null ? 'homing unknown' : printer.homed ? 'axes homed' : 'axes NOT homed',
    `font-mono text-[10px] ${printer.homed === true ? 't-accent' : 't-danger'}`);
  set('speed-current', speedLabel(printer) || 'unknown');
  set('light-chamber', (printer.lights && printer.lights.chamber_light) || 'unknown');
  set('light-work', (printer.lights && printer.lights.work_light) || 'unknown');
  set('fan-part', `${printer.fan != null ? printer.fan : 0}%`);
  set('fan-aux', `${printer.auxFan != null ? printer.auxFan : 0}%`);
  set('fan-chamber', `${printer.chamberFan != null ? printer.chamberFan : 0}%`);
  set('status', printer.status || 'unknown');

  const hot = (printer.nozzleTemp || 0) >= 170 || (printer.nozzleTarget || 0) >= 170;
  set('hot',
    `Nozzle ${printer.nozzleTemp || 0}°C ${hot ? '— ready to extrude' : '— heat above 170°C first'}`,
    `font-mono text-[10px] ${hot ? 't-accent' : 't-warn'}`);

  const level = printer.speedLevel || 0;
  body.querySelectorAll('[data-action="speed"][data-value]').forEach((b) => {
    b.classList.toggle('is-active', Number(b.dataset.value) === level);
  });
  body.querySelectorAll('[data-action="light"]').forEach((b) => {
    const current = (printer.lights && printer.lights[b.dataset.node]) || '';
    b.setAttribute('aria-pressed', String(current === b.dataset.value));
  });

  // Reprint follows the reported job, which changes under telemetry.
  const reprintBtn = body.querySelector('[data-live="reprint"]');
  if (reprintBtn) reprintBtn.disabled = !printableJob(printer.job);

  // The skip list only needs rebuilding when the objects, the current object or
  // the queued skips actually change - telemetry arrives several times a second.
  const skipBox = body.querySelector('[data-live="skip-objects"]');
  if (skipBox) {
    const sig = skipSignature(printer);
    if (skipBox.dataset.sig !== sig) {
      const holder = document.createElement('div');
      holder.innerHTML = skipObjectsHtml(printer);
      const fresh = holder.firstElementChild;
      if (fresh) skipBox.replaceWith(fresh);
    }
  }
}

/* --------------------------------------------------------------- control */

// ---------------------------------------------------------------- skip object
// The printer reports its object list and the object it is on; the dashboard
// shows the same thing the printer's own skip screen does, so it is always
// obvious which object a skip will drop before the button is pressed.

function objectText(obj, index) {
  if (obj && obj.name) return obj.name;
  return `Object ${(obj ? obj.id : index) + 1}`;
}

function objectSubtitle(obj) {
  const bits = [];
  if (obj && obj.tool) bits.push(obj.tool);
  if (obj && obj.estMin > 0) bits.push(`~${Math.round(obj.estMin)} min`);
  return bits.join(' · ');
}

function skipSignature(p) {
  const objects = Array.isArray(p.objects) ? p.objects : [];
  const geom = plateGeomFor(p, {});
  const geomKey = geom && Array.isArray(geom.objects)
    ? geom.objects.map((o) => `${o.id}:${o.name}`).join(',')
    : '';
  const arm = state.preSkip[p.id];
  const armed = arm && Array.isArray(arm.ids) ? arm.ids.join('.') : '';
  return [
    objects.map((o) => `${o.id}:${o.name}:${o.tool}`).join(','),
    geomKey,
    p.objectIndex == null ? '' : p.objectIndex,
    (p.skippedIds || []).join('.'),
    armed,
    p.status
  ].join('|');
}

// The plate number is baked into the file name ("..._plate_2.3mf"); sliced
// projects always have plate 1.
function plateFromJob(filename) {
  const match = /_plate_(\d+)/.exec(filename || '');
  return match ? Number(match[1]) : 1;
}

// Cache key used by plateGeomFor so callers can tell "still loading" from
// "the project has no plate map".
function plateGeomKey(printer, opts = {}) {
  const path = opts.path
    || (state.printPath && state.printPath[printer.id])
    || ('/' + (printer.job || ''));
  const plate = opts.plate != null ? opts.plate : plateFromJob(printer.job);
  return `${path}|${plate}`;
}

// Fetch the sliced plate geometry once per (path, plate) so telemetry ticks do
// not refetch it. Returns the cached data, or null while it is loading or when
// the project has none (the caller then falls back to the live object list).
function plateGeomFor(printer, opts = {}) {
  if (!printer) return null;
  const key = plateGeomKey(printer, opts);
  if (Object.prototype.hasOwnProperty.call(state.plateGeom, key)) {
    return state.plateGeom[key];
  }
  if (state.plateGeomPending[key]) return null;
  state.plateGeomPending[key] = true;
  const path = opts.path
    || (state.printPath && state.printPath[printer.id])
    || ('/' + (printer.job || ''));
  const plate = opts.plate != null ? opts.plate : plateFromJob(printer.job);
  api(`/api/printers/${printer.id}/files/objects?path=${encodeURIComponent(path)}&plate=${plate}`)
    .then((data) => {
      state.plateGeom[key] = (data && Array.isArray(data.objects) && data.objects.length)
        ? data : null;
    })
    .catch(() => { state.plateGeom[key] = null; })
    .then(() => {
      scheduleRender();
      if (state.skipCtx && state.skipCtx.printerId === printer.id) renderSkipModal();
    });
  return null;
}

// The Control tab summarises the object position and hands the choosing off to
// the skip modal. The count comes from the sliced plate geometry when the
// project has one (the printer itself reports no per-object list on the X1);
// the live object list is only a fallback. Nothing here sends a skip.
function skipObjectsHtml(p) {
  const geom = plateGeomFor(p, {});
  const key = plateGeomKey(p, {});
  const tried = Object.prototype.hasOwnProperty.call(state.plateGeom, key);
  const geomCount = geom && Array.isArray(geom.objects) ? geom.objects.length : 0;
  const live = Array.isArray(p.objects) ? p.objects : [];
  const count = geomCount || live.length;
  const loading = !count && !tried && p.job && p.job !== 'None';
  const printing = p.status === 'running' || p.status === 'paused';
  const cur = p.objectIndex;
  const skipped = Array.isArray(p.skippedIds) ? p.skippedIds.length : 0;
  const arm = state.preSkip[p.id];
  const armed = arm && Array.isArray(arm.ids) ? arm.ids.length : 0;

  if (!count && !loading) {
    return `
      <div data-live="skip-objects" data-sig="${escapeHtml(skipSignature(p))}">
        <p class="font-mono text-[10px] t-mut">
          This job reports no object list, so there is nothing to skip from here.
          A multi-object print fills the list below from the printer's own report.
        </p>
      </div>`;
  }

  const summary = loading
    ? 'Object list reading…'
    : `Object ${cur == null ? '-' : cur + 1} of ${count}`;

  return `
    <div data-live="skip-objects" data-sig="${escapeHtml(skipSignature(p))}">
      <div class="flex items-center justify-between gap-2">
        <span class="chip chip-ok">${summary}</span>
        <span class="font-mono text-[10px] t-mut">${printing ? 'job active' : 'start a job to skip'}</span>
      </div>

      <div class="mt-2">
        ${btn('Skip objects…', 'open-skip', { cls: 'w-full btn-primary' })}
      </div>

      <p class="font-mono text-[10px] t-mut">
        Choose the objects to drop from the plate; the printer keeps the rest.
        ${skipped ? `${skipped} already skipped.` : ''}
        ${armed ? `${armed} armed.` : ''}
      </p>
    </div>`;
}

/* ------------------------------------------------------------- skip modal */

function skipItemState(printer, id) {
  const skipped = Array.isArray(printer.skippedIds) ? printer.skippedIds : [];
  if (skipped.indexOf(id) >= 0) return 'is-skipped';
  return state.skipSelection.has(id) ? 'is-pending' : 'is-todo';
}

// One toggle per object: geometry when the sliced plate map is available,
// otherwise the printer's own live object report.
function skipItemsFor(ctx, printer) {
  const geom = plateGeomFor(printer, { path: ctx.path, plate: ctx.plate });
  const live = Array.isArray(printer.objects) ? printer.objects : [];
  const key = `${ctx.path}|${ctx.plate}`;
  const tried = Object.prototype.hasOwnProperty.call(state.plateGeom, key);

  if (geom && Array.isArray(geom.objects) && geom.objects.length) {
    return {
      kind: 'bed',
      bed: geom.bed || [256, 256],
      items: geom.objects.map((g, i) => {
        const l = live[i];
        const geomName = (g.name || '').trim();
        return {
          // The geometry id is already the slice_info identify_id the skip
          // command wants; never override it with the live (bbox) id.
          id: g.id,
          name: geomName || ((l && l.name) || `Object ${i + 1}`),
          sub: geomName ? '' : (l ? objectSubtitle(l) : ''),
          bbox: g.bbox || [0, 0, 1, 1]
        };
      })
    };
  }
  if (live.length) {
    return {
      kind: 'list',
      items: live.map((o, i) => ({ id: o.id, name: objectText(o, i), sub: objectSubtitle(o), bbox: null }))
    };
  }
  if (!tried && printer.job && printer.job !== 'None') return { kind: 'loading', items: [] };
  return { kind: 'empty', items: [] };
}

function renderSkipModal() {
  const ctx = state.skipCtx;
  if (!ctx) return;
  const printer = printerById(ctx.printerId);
  if (!printer) return;
  const running = printer.status === 'running' || printer.status === 'paused';
  const info = skipItemsFor(ctx, printer);

  $('skip-title').textContent = 'Skip objects';
  $('skip-sub').textContent = `${printer.name || ctx.printerId} · ${running
    ? 'job active'
    : 'not printing — selection will skip when the print starts'}`;

  const count = state.skipSelection.size;
  const confirmBtn = $('skip-confirm');
  confirmBtn.textContent = count ? `Skip selected (${count})` : 'Skip selected';
  confirmBtn.disabled = count === 0;

  const box = $('skip-bed');
  if (info.kind === 'loading') {
    box.innerHTML = '<p class="font-mono text-[10px] t-mut">reading the plate map…</p>';
    return;
  }
  if (info.kind === 'empty') {
    box.innerHTML = '<p class="font-mono text-[10px] t-mut">This job reports no objects to skip.</p>';
    return;
  }

  // Only already-sent skips are locked; everything else is selectable, even
  // while the printer is idle so a selection can be armed ahead of the job.
  const disabled = (id) => skipItemState(printer, id) === 'is-skipped';

  if (info.kind === 'bed') {
    const bedW = info.bed[0] || 256;
    const bedH = info.bed[1] || 256;
    const rects = info.items.map((it) => {
      const bb = it.bbox;
      const w = Math.max(1, bb[2] - bb[0]);
      const h = Math.max(1, bb[3] - bb[1]);
      const x = bb[0];
      // plate Y grows away from the viewer, screen Y grows down -> flip it
      const y = Math.max(0, bedH - bb[3]);
      const cls = skipItemState(printer, it.id);
      const label = escapeHtml(it.name) + (cls === 'is-skipped' ? ' · skipped' : '');
      return `<rect class="bed-obj ${cls}" x="${x}" y="${y}" width="${w}" height="${h}" rx="2"
                data-skip-id="${it.id}" ${disabled(it.id) ? 'data-disabled="1"' : ''}
                role="button" tabindex="0" aria-label="${label}"><title>${label}</title></rect>`;
    }).join('');
    box.innerHTML = `<svg class="bed-svg" viewBox="0 0 ${bedW} ${bedH}" role="img"
        aria-label="Build plate with ${info.items.length} objects">${rects}</svg>`;
    return;
  }

  box.innerHTML = `<div class="obj-list">${info.items.map((it) => {
    const cls = skipItemState(printer, it.id);
    return `<button type="button" class="obj-row ${cls}" data-skip-id="${it.id}"
              ${disabled(it.id) ? 'disabled' : ''}>
      <span class="obj-badge">${it.id + 1}</span>
      <span class="min-w-0 flex-1 text-left">
        <span class="obj-title">${escapeHtml(it.name)}</span>
        ${it.sub ? `<span class="obj-sub">${escapeHtml(it.sub)}</span>` : ''}
      </span>
      ${cls === 'is-skipped' ? '<span class="obj-tag">skipped</span>' : ''}
    </button>`;
  }).join('')}</div>`;
}

// Entry point shared by the Control tab and the print preview. opts may carry
// an explicit { path, plate } when the caller knows the file being printed.
function openSkipModal(printerId, opts = {}) {
  const printer = printerById(printerId);
  if (!printer) { toast('Printer not found', 'error'); return; }
  const path = opts.path
    || (state.printPath && state.printPath[printerId])
    || ('/' + (printer.job || ''));
  const plate = opts.plate != null ? opts.plate : plateFromJob(printer.job);
  const job = baseName(path) || printer.job;
  state.skipCtx = { printerId, path, plate, job };
  // Re-open with the already-armed set preloaded so it renders red (pending).
  const arm = state.preSkip[printerId];
  state.skipSelection = (arm && baseName(arm.job) === baseName(job))
    ? new Set(arm.ids || [])
    : new Set();
  $('modal-skip').classList.replace('hidden', 'flex');
  renderSkipModal();
}

function closeSkipModal() {
  state.skipCtx = null;
  state.skipSelection = new Set();
  $('modal-skip').classList.replace('flex', 'hidden');
}

// Skipped objects are locked: re-clicking one can never unskip it.
function toggleSkipObject(id) {
  const ctx = state.skipCtx;
  if (!ctx) return;
  const printer = printerById(ctx.printerId);
  if (!printer) return;
  if ((printer.skippedIds || []).indexOf(id) >= 0) return;
  if (state.skipSelection.has(id)) state.skipSelection.delete(id);
  else state.skipSelection.add(id);
  renderSkipModal();
}

async function confirmSkip() {
  const ctx = state.skipCtx;
  if (!ctx) return;
  const printer = printerById(ctx.printerId);
  if (!printer) return;
  const ids = [...state.skipSelection];
  if (!ids.length) return;
  const running = printer.status === 'running' || printer.status === 'paused';
  if (!running) {
    // Arm the selection; it is applied once the matching job starts printing.
    const job = ctx.job || baseName(ctx.path) || printer.job;
    state.preSkip[ctx.printerId] = { job, ids };
    toast(`Will skip ${ids.length} object${ids.length === 1 ? '' : 's'} when the print starts.`, 'ok');
    state.skipSelection = new Set();
    // The print dialog (if it is open for this project) now shows the armed count.
    if (state.print && state.print.plan && state.print.printerId === ctx.printerId) {
      renderPrintDialog();
    }
    scheduleRender();
    closeSkipModal();
    return;
  }
  const ok = await post(`/api/printers/${ctx.printerId}/skip-objects`, { object_ids: ids });
  if (!ok) return;  // error toasts in post(); keep the modal open so a retry is possible
  printer.skippedIds = [...new Set([...(printer.skippedIds || []), ...ids])];
  toast(`Skip queued for ${ids.length} object${ids.length === 1 ? '' : 's'}. Watch the printer screen.`, 'ok');
  state.skipSelection = new Set();
  renderSkipModal();
  closeSkipModal();
}

function btn(label, action, opts = {}) {
  const attrs = Object.entries(opts.data || {}).map(([k, v]) => `data-${k}="${v}"`).join(' ');
  return `<button class="btn ${opts.cls || ''}" data-action="${action}" ${attrs} ${opts.disabled ? 'disabled' : ''}>${label}</button>`;
}

function section(title, body, cls = '') {
  return `<section class="tile p-3 ${cls}">
    <p class="mb-2 text-[10px] font-bold uppercase tracking-wider t-mut">${title}</p>
    <div class="space-y-2">${body}</div>
  </section>`;
}

function choice(action, value, title, sub, extra = '', active = false) {
  return `<button class="choice${active ? ' is-active' : ''}" data-action="${action}" data-value="${value}" ${extra}>
    <span class="choice-title">${title}</span>${sub ? `<span class="choice-sub">${sub}</span>` : ''}
  </button>`;
}

function motionNotice(label = 'I have cleared the build plate and confirm the printer may move') {
  if (state.allowMotion) {
    return '<p class="font-mono text-[10px] t-warn">Moves unlocked in settings — '
      + 'no confirmation needed (homing and idle checks still apply).</p>';
  }
  return `<label class="flex items-center gap-2 font-mono text-[11px] t-body">
    <input type="checkbox" data-role="confirm-motion" class="accent-bambu">${label}</label>`;
}

function motionConfirmed(el) {
  if (state.allowMotion) return true;
  const panel = el.closest('[data-r="panel"]');
  const box = panel && panel.querySelector('[data-role="confirm-motion"]');
  return Boolean(box && box.checked);
}

// A job we can re-open in the print preview: a real file name, not the
// printer's "None" placeholder and one of the printable extensions.
function printableJob(job) {
  return Boolean(job) && job !== 'None' && /\.(3mf|gcode)$/i.test(baseName(job));
}

function controlHtml(p) {
  const homed = p.homed === true;
  const printing = p.status === 'running';

  const speeds = [1, 2, 3, 4]
    .map((l) => choice('speed', l, SPEED_LABELS[l], `${SPEED_PERCENT[l]}%`, '', (p.speedLevel || 0) === l))
    .join('');

  const lightCard = (label, node, modes, liveKey) => `
    <div class="light-card">
      <div class="min-w-0">
        <div class="light-card-title">${label}</div>
        <div class="light-card-state">reported:
          <span data-live="${liveKey}" class="t-body">${
            escapeHtml((p.lights && p.lights[node]) || 'unknown')}</span>
        </div>
      </div>
      <div class="seg shrink-0">
        ${modes.map((m) => `<button data-action="light" data-node="${node}" data-value="${m.value}"
          aria-pressed="${(p.lights && p.lights[node]) === m.value}">${m.label}</button>`).join('')}
      </div>
    </div>`;

  const lightSection = `
    <div class="light-grid">
      ${lightCard('Chamber light', 'chamber_light', [
        { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }, { value: 'flashing', label: 'Flash' }
      ], 'light-chamber')}
      ${lightCard('Work light', 'work_light', [
        { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }
      ], 'light-work')}
    </div>`;

  const skipSection = skipObjectsHtml(p);

  // Fans: each slider sits next to that fan's reported speed, so a firmware
  // that numbers its M106 P indices differently is immediately visible.
  const fanRow = (name, label, liveKey, value) => `
    <div class="space-y-1">
      <div class="flex items-baseline justify-between gap-2">
        <span class="text-[11px] font-bold t-body">${label}</span>
        <span class="font-mono text-[10px] t-mut">reported:
          <span data-live="${liveKey}" class="t-body">–</span></span>
      </div>
      <div class="flex items-center gap-3">
        <input type="range" class="slider flex-1" min="0" max="100" step="5" value="${value}"
               data-role="fan-slider-${name}" aria-label="${label} speed">
        <span class="w-11 shrink-0 text-right font-mono text-[11px] font-bold t-strong"
              data-role="fan-value-${name}">${value}%</span>
      </div>
      <div class="flex flex-wrap gap-1.5">
        ${[0, 50, 100].map((v) => `<button class="btn btn-ghost" data-action="fan"
          data-fan="${name}" data-speed="${v}">${v === 0 ? 'Off' : `${v}%`}</button>`).join('')}
      </div>
    </div>`;

  const fanSection = `
    ${fanRow('part', 'Part cooling fan', 'fan-part', p.fan || 0)}
    <div class="h-px surface-3"></div>
    ${fanRow('aux', 'Aux part fan', 'fan-aux', p.auxFan || 0)}
    <div class="h-px surface-3"></div>
    ${fanRow('chamber', 'Chamber fan', 'fan-chamber', p.chamberFan || 0)}
    <p class="font-mono text-[10px] t-mut">
      Sent as an M106 G-code line. The reported value is what the printer sends back — if it does
      not follow the slider, your firmware numbers the fans differently.
    </p>`;

  const calibration = `
    <div class="mb-2 flex items-center justify-between gap-2">
      <span data-live="homed" class="font-mono text-[10px] ${homed ? 't-accent' : 't-danger'}">
        ${p.homed == null ? 'homing unknown' : homed ? 'axes homed' : 'axes NOT homed'}
      </span>
      ${btn('Refresh state', 'refresh', { cls: 'btn-ghost' })}
    </div>
    <p class="mb-2 text-[10px] leading-relaxed t-mut">
      All axes are <span class="t-body">homed automatically</span> first, then the routine runs.
      Clear the plate before starting. Axis self-test and bridge-test are deliberately not exposed.
    </p>
    ${motionNotice()}
    <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
      ${choice('calibrate', 'bed_leveling', 'Bed levelling', 'Probe the mesh')}
      ${choice('calibrate', 'flow_calibration', 'Flow · normal', 'Extrusion', 'data-mode="1"')}
      ${choice('calibrate', 'flow_calibration', 'Flow · first layer', 'First layer', 'data-mode="2"')}
      ${choice('calibrate', 'vibration_calibration', 'Vibration', 'Input shaping')}
    </div>`;

  return `
    <div class="grid gap-3 xl:grid-cols-2">
      ${section('Job control', `
        <div class="job-toolbar">
          <div class="job-group job-group-primary">
            ${btn('Pause', 'pause', { cls: 'btn-warn' })}
            ${btn('Resume', 'resume', { cls: 'btn-primary' })}
            ${btn('Stop', 'stop', { cls: 'btn-danger' })}
          </div>
          <div class="job-group job-group-reprint">
            ${btn('Reprint', 'reprint', { cls: 'btn-ghost', disabled: !printableJob(p.job), data: { live: 'reprint' } })}
          </div>
          <div class="job-group job-group-recovery">
            ${btn('Recover', 'retry', { cls: 'btn-ghost' })}
            ${btn('Clear error', 'clear-error', { cls: 'btn-ghost' })}
          </div>
        </div>
        <p class="font-mono text-[10px] t-mut">State: <span data-live="status" class="t-body">${escapeHtml(p.status || 'unknown')}</span></p>
        ${p.status === 'failed' ? '<p class="font-mono text-[10px] t-warn">The last job failed. Press <b>Clear error</b>, then dismiss the message on the printer screen — a printer in FAILED refuses new jobs.</p>' : ''}`)}


      ${section('Lighting', lightSection, 'xl:col-span-2')}

      ${section('Fans', fanSection, 'xl:col-span-2')}

      ${section('Skip object', skipSection)}

      ${section('Calibration · moves the machine', calibration, 'border-warn')}
    </div>`;
}

/* ------------------------------------------------------------------- jog */

function axisRow(label, axis, step, feed) {
  return `
    <div class="jog-row${axis === 'z' ? ' is-z' : ''}">
      <span class="jog-badge" aria-hidden="true">${label}</span>
      <button class="btn jog-btn" data-action="jog" data-axis="${axis}" data-dir="-1" data-feed="${feed}"
        aria-label="${label} minus ${step} mm">−</button>
      <span class="jog-step">${step} mm${axis === 'z' ? ' · slow' : ''}</span>
      <button class="btn jog-btn" data-action="jog" data-axis="${axis}" data-dir="1" data-feed="${feed}"
        aria-label="${label} plus ${step} mm">+</button>
    </div>`;
}

function jogHtml(p, entry) {
  const homed = p.homed === true;
  const hot = (p.nozzleTemp || 0) >= 170 || (p.nozzleTarget || 0) >= 170;
  const step = entry.jogStep || 1;

  const steps = [0.1, 1, 10, 50].map((s) =>
    `<button data-action="jog-step" data-step="${s}" aria-pressed="${s === step}">${s} mm</button>`).join('');

  return `
    <div class="grid gap-3 xl:grid-cols-2">
      ${section('Homing', `
        <div class="flex flex-wrap gap-1.5">
          ${btn('Home all', 'home', { data: { axes: '' }, cls: 'btn-primary' })}
          ${btn('Home X', 'home', { data: { axes: 'x' } })}
          ${btn('Home Y', 'home', { data: { axes: 'y' } })}
          ${btn('Home Z', 'home', { data: { axes: 'z' } })}
        </div>
        <p class="font-mono text-[10px] t-mut">
          Homing is allowed while unhomed — everything below it needs.
        </p>`)}

      ${section('Step size', `
        <div class="seg">${steps}</div>
        <p class="font-mono text-[10px] t-mut">Each press moves one step (${step} mm).</p>`)}

      ${section('Move axes', `
        <div class="jog-grid">
          ${axisRow('X', 'x', step, 3000)}
          ${axisRow('Y', 'y', step, 3000)}
          ${axisRow('Z', 'z', step, 600)}
        </div>`, 'xl:col-span-2')}

      ${section('Extruder', `
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
          ${choice('extrude', 5, 'Extrude', '5 mm')}
          ${choice('extrude', 10, 'Extrude', '10 mm')}
          ${choice('extrude', -5, 'Retract', '5 mm')}
          ${choice('extrude', -10, 'Retract', '10 mm')}
        </div>
        <span data-live="hot" class="font-mono text-[10px] ${hot ? 't-accent' : 't-warn'}">
          Nozzle ${p.nozzleTemp || 0}°C ${hot ? '— ready to extrude' : '— heat above 170°C first'}
        </span>`)}

      ${section('Safety', `
        <span data-live="homed" class="font-mono text-[10px] ${homed ? 't-accent' : 't-danger'}">
          ${p.homed == null ? 'homing unknown' : homed ? 'axes homed' : 'axes NOT homed'}
        </span>
        ${motionNotice('I confirm the printer may move (plate clear, nothing in the way)')}
        <p class="font-mono text-[10px] leading-relaxed t-mut">
          Moves are refused unless the printer reports all axes homed and no job is running.
          Extruding also needs a nozzle above 170°C. Z always moves at the slow feed rate.
        </p>`, 'xl:col-span-2 border-warn')}
    </div>`;
}

/* ----------------------------------------------------------- temperature */

const THERMAL_PRESETS = [
  { label: 'Cooldown', nozzle: 0, bed: 0, chamber: 0 },
  { label: 'PLA', nozzle: 220, bed: 55, chamber: 0 },
  { label: 'PETG', nozzle: 240, bed: 70, chamber: 0 },
  { label: 'ABS / ASA', nozzle: 260, bed: 100, chamber: 45 }
];

function temperatureHtml(p) {
  const row = (key, label, max, setpoint, now) => `
    <div class="tile p-3">
      <div class="mb-2 flex items-baseline justify-between">
        <span class="text-[10px] uppercase tracking-wider t-mut">${label}</span>
        <span class="font-mono text-[10px] t-mut">now
          <span data-live="${key}-now" class="t-body">${now}°</span></span>
      </div>
      <div class="flex items-center gap-3">
        <input type="range" class="slider flex-1" min="0" max="${max}" value="${setpoint}"
               data-role="slider-${key}">
        <div class="flex items-center gap-1">
          <input type="number" min="0" max="${max}" value="${setpoint}"
                 class="field !w-20 text-center" data-role="input-${key}">
          <span class="font-mono text-[11px] t-mut">°C</span>
        </div>
      </div>
    </div>`;

  return `
    <div class="space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <p class="text-[11px] t-mut">Set targets manually. Changes heat the machine and are capped for safety.</p>
        <div class="flex flex-wrap gap-1.5">
          ${THERMAL_PRESETS.map((preset) =>
            `<button class="btn btn-ghost" data-action="preset"
               data-nozzle="${preset.nozzle}" data-bed="${preset.bed}" data-chamber="${preset.chamber}">${preset.label}</button>`).join('')}
        </div>
      </div>

      ${row('nozzle', 'Nozzle', 300, p.nozzleTarget || 0, p.nozzleTemp || 0)}
      ${row('bed', 'Bed', 130, p.bedTarget || 0, p.bedTemp || 0)}

      <div class="flex flex-wrap items-center justify-between gap-3 rounded-xl border line surface-2 p-3">
        <p class="font-mono text-[10px] t-mut">
          Drag a slider, or tap a preset — it is sent when you let go, with no separate apply step.
        </p>
        <button class="btn btn-ghost" data-action="cooldown">All to 0°</button>
      </div>
      <p class="font-mono text-[10px] t-mut">
        Caps: nozzle ≤ 300°C, bed ≤ 130°C, chamber ≤ 60°C. Changing targets mid-print needs the
        extra override below.
      </p>
      <label class="flex items-center gap-2 font-mono text-[10px] t-mut">
        <input type="checkbox" data-role="allow-while-printing" class="accent-amber-400">
        Allow while a job is running (it will fight the running job)
      </label>
    </div>`;
}

/* --------------------------------------------------------------- SD files */

/* ------------------------------------------------------------ file browser */

// Sort + filter are remembered per browser; the search box is per session.
const fileView = (() => {
  const read = (key, fallback) => {
    try { return localStorage.getItem(`bfm-files-${key}`) || fallback; } catch (_) { return fallback; }
  };
  return {
    sort: read('sort', 'name'),
    dir: read('dir', 'asc'),
    filter: read('filter', 'all'),
    q: '',
    set(key, value) {
      this[key] = value;
      try { localStorage.setItem(`bfm-files-${key}`, value); } catch (_) { /* ignore */ }
    }
  };
})();

// Folders Windows/printers create that should never be shown.
const HIDDEN_SD_NAMES = new Set(['system volume information']);

const is3mf = (name) => String(name).toLowerCase().endsWith('.3mf');
const isMp4 = (name) => String(name).toLowerCase().endsWith('.mp4');

function sortFiles(entries) {
  const flip = fileView.dir === 'desc' ? -1 : 1;
  return [...entries].sort((a, b) => {
    // folders always lead, whatever the sort, so the tree stays navigable
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    let cmp;
    if (fileView.sort === 'size') cmp = (a.size || 0) - (b.size || 0);
    else if (fileView.sort === 'date') cmp = String(a.modified || '').localeCompare(String(b.modified || ''));
    else cmp = a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' });
    return cmp * flip;
  });
}

function filterFiles(entries) {
  const needle = fileView.q.trim().toLowerCase();
  return sortFiles((entries || []).filter((f) => {
    if (HIDDEN_SD_NAMES.has(String(f.name).toLowerCase())) return false;
    if (needle && !String(f.name).toLowerCase().includes(needle)) return false;
    if (fileView.filter === 'print') return !f.is_dir && is3mf(f.name);
    if (fileView.filter === 'video') return !f.is_dir && isMp4(f.name);
    return true;
  }));
}

function folderTotals(entries) {
  let files = 0, folders = 0, bytes = 0;
  entries.forEach((f) => {
    if (f.is_dir) folders += 1; else { files += 1; bytes += f.size || 0; }
  });
  return { files, folders, bytes };
}

// A row's actions: Print is the default for a project file, Get for anything
// else, and the rest live behind the caret.
function fileActions(f, printingBusy) {
  const path = escapeHtml(f.path);
  const primary = f.is_printable
    ? `<button class="btn btn-primary" data-action="print-sd" data-path="${path}">Print</button>`
    : `<button class="btn btn-ghost" data-action="download-sd" data-path="${path}">Get</button>`;

  const items = [];
  if (f.is_printable) items.push(`<button data-action="download-sd" data-path="${path}">Get a copy</button>`);
  if (is3mf(f.name)) {
    items.push(`<button data-action="rename-sd" data-path="${path}" data-name="${escapeHtml(f.name)}"
      ${printingBusy ? 'disabled title="Stop the job before renaming"' : ''}>Rename</button>`);
  }
  items.push(`<button class="t-danger" data-action="delete-sd" data-path="${path}" data-dir="0"
    ${printingBusy ? 'disabled title="Stop the job before deleting"' : ''}>Delete</button>`);

  return `
    <div class="file-actions">
      ${primary}
      <details class="file-menu">
        <summary class="btn btn-ghost file-menu-toggle" title="More actions" aria-label="More actions">▾</summary>
        <div class="file-menu-panel">
          <div class="file-menu-card" role="dialog" aria-label="Actions for ${escapeHtml(f.name)}">
            <p class="file-menu-title">${escapeHtml(f.name)}</p>
            ${items.join('')}
            <button class="file-menu-cancel" data-action="file-menu-close">Cancel</button>
          </div>
        </div>
      </details>
    </div>`;
}

function filesHtml(cache, printer) {
  const parts = cache.path.split('/').filter(Boolean);
  let acc = '';
  const crumbs = [`<button class="hover:t-accent" data-action="cd" data-path="/">SD root</button>`];
  parts.forEach((part) => {
    acc += `/${part}`;
    crumbs.push(`<span class="t-dim">/</span><button class="hover:t-accent" data-action="cd" data-path="${escapeHtml(acc)}">${escapeHtml(part)}</button>`);
  });

  const row = (f) => {
    const printing = !f.is_dir && printer && printer.job && String(f.name) === String(printer.job);
    return `
      <div class="file-row${f.is_dir ? ' is-dir' : ''}${printing ? ' is-printing' : ''}">
        <button class="flex min-w-0 flex-1 items-center gap-2.5 text-left" data-action="cd"
                data-path="${escapeHtml(f.path)}" data-dir="${f.is_dir ? 1 : 0}">
          <span class="grid h-8 w-8 shrink-0 place-items-center rounded-lg border line-soft ${
            f.is_dir ? 'bg-accent-soft t-accent' : 'surface-3 t-mut'}">
            ${f.is_dir ? '▣' : isMp4(f.name) ? '▶' : '▤'}
          </span>
          <span class="min-w-0">
            <span class="block truncate font-mono text-[12px] ${f.is_dir ? 'font-bold t-strong' : 't-body'}">${escapeHtml(f.name)}</span>
            <span class="block truncate font-mono text-[10px] t-mut">
              ${f.is_dir ? 'folder' : formatBytes(f.size)}${f.modified ? ' · ' + formatDate(f.modified) : ''}${
                printing ? ' · <span class="t-accent">printing</span>' : ''}
            </span>
          </span>
        </button>
        ${f.is_dir ? '' : fileActions(f, printer && printer.status === 'running')}
      </div>`;
  };

  let body;
  if (cache.loading) {
    body = `<p class="p-4 text-center font-mono text-[11px] t-mut">Reading SD card over FTPS…</p>`;
  } else if (cache.error) {
    body = `<p class="p-4 text-center font-mono text-[11px] t-danger">${escapeHtml(cache.error)}</p>`;
  } else if (!cache.listing) {
    body = '';
  } else if (!cache.listing.length) {
    body = `<p class="p-4 text-center font-mono text-[11px] t-mut">This folder is empty.</p>`;
  } else {
    const shown = filterFiles(cache.listing);
    const total = folderTotals(shown);
    const filtered = fileView.filter !== 'all' || fileView.q.trim();
    body = shown.length
      ? shown.map(row).join('')
      : `<p class="p-4 text-center font-mono text-[11px] t-mut">
           Nothing here matches ${fileView.q.trim() ? 'that search' : 'this filter'}.
         </p>`;
    body += filtered
      ? `<p class="px-1 pt-1 font-mono text-[10px] t-dim">showing ${total.files} of ${
          folderTotals(cache.listing).files} file(s)</p>`
      : '';
  }

  const totals = cache.listing ? folderTotals(cache.listing) : null;
  const options = (pairs, current) => pairs.map(([v, l]) =>
    `<option value="${v}"${v === current ? ' selected' : ''}>${l}</option>`).join('');

  return `
    <div class="file-browser">
      <div class="file-toolbar">
        ${cache.parent ? `<button class="btn btn-ghost shrink-0" data-action="cd" data-path="${escapeHtml(cache.parent)}"
          title="Up one level">↑ Up</button>` : ''}
        <div class="file-crumbs">${crumbs.join('')}</div>
      </div>

      <div class="file-controls">
        <input type="search" class="field file-search" data-role="files-search"
               placeholder="Search this folder…" value="${escapeHtml(fileView.q)}" aria-label="Search this folder">
        <select class="field file-select" data-role="files-sort" aria-label="Sort by">
          ${options([['name', 'Name'], ['date', 'Date'], ['size', 'Size']], fileView.sort)}
        </select>
        <button class="btn btn-ghost" data-action="files-dir"
                title="Sort ${fileView.dir === 'asc' ? 'descending' : 'ascending'}">${fileView.dir === 'asc' ? '↑ A-Z' : '↓ Z-A'}</button>
        <select class="field file-select" data-role="files-filter" aria-label="Filter">
          ${options([['all', 'All files'], ['print', 'Print files (3MF)'], ['video', 'Video files (MP4)']], fileView.filter)}
        </select>
        <label class="btn btn-ghost" title="Upload a project into this folder">
          Upload
          <input type="file" accept=".3mf,.gcode" class="hidden" data-role="folder-upload">
        </label>
        <button class="btn btn-ghost" data-action="refresh-files">Refresh</button>
      </div>

      <p class="font-mono text-[10px] t-dim">
        ${totals ? `${totals.folders} folder(s), ${totals.files} file(s) · ${formatBytes(totals.bytes)}` : ''}
      </p>
      <div class="file-list" data-role="file-list">${body}</div>
    </div>`;
}

/* -------------------------------------------------------------------- AMS */

function amsColor(raw) {
  const hex = String(raw || '').replace('#', '').trim();
  if (hex.length >= 6) return '#' + hex.slice(0, 6).toUpperCase();
  return '';
}

function swatchStyle(color) {
  return color ? `background:${color}` : 'background:repeating-linear-gradient(45deg,var(--surface-3),var(--surface-3) 5px,var(--line-soft) 5px,var(--line-soft) 10px)';
}

function amsTrayCard(unitIndex, trayLabel, tray, active) {
  const color = amsColor(tray.tray_color);
  const type = tray.tray_type || '';
  const empty = !type;
  const remain = typeof tray.remain === 'number' && tray.remain >= 0 && tray.remain <= 100 ? tray.remain : null;
  const sub = tray.tray_sub_brands || tray.tray_id_name || '';

  return `
    <div class="tile border-2 p-2 ${active ? 'line-accent' : 'line-soft'}">
      <div class="flex items-center gap-2">
        <span class="h-9 w-9 shrink-0 rounded-lg border line" style="${swatchStyle(color)}"></span>
        <span class="min-w-0 flex-1">
          <span class="flex items-center gap-1.5">
            <span class="font-mono text-[10px] t-mut">${trayLabel}</span>
            ${active ? '<span class="chip chip-ok !px-1.5 !text-[9px]">loaded</span>' : ''}
            ${empty ? '' : `<button class="t-mut hover:t-accent" title="Edit filament"
              data-action="filament-edit" data-ams-id="${unitIndex}" data-tray="${tray.id}">✎</button>`}
          </span>
          <span class="block truncate font-mono text-[12px] ${empty ? 't-dim' : 'font-bold t-strong'}">
            ${empty ? 'Empty' : escapeHtml(type)}
          </span>
          ${sub && !empty ? `<span class="block truncate font-mono text-[10px] t-mut">${escapeHtml(sub)}</span>` : ''}
        </span>
      </div>
      ${remain !== null ? `
        <div class="mt-2 h-1.5 overflow-hidden rounded-full surface-3">
          <div class="h-1.5 rounded-full bg-accent" style="width:${remain}%"></div>
        </div>
        <div class="mt-0.5 text-right font-mono text-[10px] t-mut">${remain}% left</div>` : ''}
      <div class="mt-2 flex gap-1">
        <button class="btn btn-primary flex-1" data-action="ams" data-ams="feed"
                data-ams-id="${unitIndex}" data-tray="${tray.id}" ${empty ? 'disabled' : ''}>Feed</button>
        <button class="btn flex-1" data-action="ams" data-ams="unload"
                data-ams-id="${unitIndex}" data-tray="${tray.id}" ${empty ? 'disabled' : ''}>Unload</button>
        <button class="btn btn-ghost flex-1" data-action="ams" data-ams="tray_select"
                data-ams-id="${unitIndex}" data-tray="${tray.id}" ${empty ? 'disabled' : ''}>Use</button>
      </div>
    </div>`;
}

function renderAms(printer, body, force) {
  const ams = printer.ams;
  const sig = `ams:${JSON.stringify(ams)}`;
  if (!force && body.dataset.sig === sig) return;
  body.dataset.sig = sig;

  const units = (ams && Array.isArray(ams.ams)) ? ams.ams : [];
  if (!units.length) {
    body.innerHTML = `
      <div class="grid place-items-center gap-1 py-8 text-center">
        <p class="font-mono text-[12px] t-mut">No AMS data reported yet.</p>
        <p class="font-mono text-[10px] t-dim">The printer reports its AMS state with every telemetry update.</p>
      </div>`;
    return;
  }

  const activeIndex = Number(ams.tray_now);

  const unitBlocks = units.map((unit, ui) => {
    const unitId = Number(unit.id !== undefined ? unit.id : ui);
    const trays = Array.isArray(unit.tray) ? unit.tray : [];
    const humidity = unit.humidity_raw ?? unit.humidity;
    const temp = unit.temp;
    const loaded = trays.find((t) => trayFlatIndex(unitId, t) === activeIndex);

    return `
      <div class="space-y-2">
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="text-[11px] font-bold t-strong">AMS ${unitId + 1}</span>
          <span class="flex items-center gap-3 font-mono text-[10px] t-mut">
            ${humidity !== undefined ? `<span>humidity ${escapeHtml(String(humidity))}%</span>` : ''}
            ${temp !== undefined ? `<span>${escapeHtml(String(temp))}°C</span>` : ''}
            <span>${loaded ? 'feeding ' + escapeHtml(loaded.tray_type || '') : trays.length + ' trays'}</span>
          </span>
        </div>
        <div class="grid grid-cols-2 gap-2 xl:grid-cols-4">
          ${trays.map((tray) => amsTrayCard(unitId, `T${Number(tray.id ?? 0) + 1}`, tray,
            trayFlatIndex(unitId, tray) === activeIndex)).join('')
            || '<p class="col-span-full font-mono text-[11px] t-mut">No trays in this unit.</p>'}
        </div>
      </div>`;
  }).join('');

  const vt = printer.vtTray || {};
  const vtActive = activeIndex === 255;
  const external = `
    <div class="space-y-2">
      <div class="flex items-center justify-between">
        <span class="text-[11px] font-bold t-strong">External spool</span>
        <span class="font-mono text-[10px] t-mut">spool holder</span>
      </div>
      <div class="grid grid-cols-2 gap-2 xl:grid-cols-4">
        ${amsTrayCard(255, 'EXT', vt, vtActive)}
      </div>
    </div>`;

  body.innerHTML = `
    <div class="space-y-4">
      ${unitBlocks}
      ${external}
      <div class="flex flex-wrap gap-1.5 border-t line pt-3">
        <button class="btn btn-ghost" data-action="ams" data-ams="resume">Resume AMS</button>
        <button class="btn btn-ghost" data-action="ams" data-ams="current_tray">Which tray is loaded?</button>
        <button class="btn btn-ghost" data-action="ams" data-ams="auto">Auto feed</button>
      </div>
      <p class="font-mono text-[10px] t-mut">
        Feed / unload heat the hotend and move filament — you are asked to confirm. Feed picks up a
        spool, Unload retracts it, Use selects it for the next print.
      </p>
    </div>`;
}

function trayFlatIndex(unitId, tray) {
  const id = Number(tray && tray.id !== undefined ? tray.id : 0);
  return Number(unitId) * 4 + id;
}

/* ----------------------------------------------------------------- camera */

//: How long to wait for the first frame before calling the camera unreachable.
//: (jsdom tests set window.CAMERA_FIRST_FRAME_MS to avoid a 12s wait.)
const CAMERA_FIRST_FRAME_MS = window.CAMERA_FIRST_FRAME_MS || 12000;
const CAMERA_NO_FRAMES_MSG =
  'No frames from the camera. Check the printer is powered on and on the network, and that '
  + 'no other viewer is already using its camera.';
const CAMERA_FAILED_MSG =
  'Could not open the live stream. Check the printer is on and reachable.';

function startCamera(printer, entry) {
  const refs = entry.camRefs;
  if (!refs || !printer || entry.camera) return;
  const img = refs.img;

  // Reveal the image immediately - browsers do not reliably fire 'load' for a
  // multipart/x-mixed-replace stream, so visibility must not depend on it.
  refs.overlay.classList.add('hidden');
  img.classList.remove('hidden');
  refs.status.textContent = 'connecting…';

  let gotFrame = false;
  // A stream can connect and then deliver nothing at all (printer off, camera
  // taken by another viewer, RTSP blocked). Without a watchdog the frame just
  // stays blank with no explanation, so say something useful.
  const watchdog = setTimeout(() => {
    if (gotFrame || !entry.camera) return;
    stopCamera(entry, CAMERA_NO_FRAMES_MSG);
  }, CAMERA_FIRST_FRAME_MS);

  const settle = (message) => {
    if (!entry.camera) return;
    stopCamera(entry, message);
  };

  img.onload = () => {
    gotFrame = true;
    clearTimeout(watchdog);
    refs.status.textContent = 'live (MJPEG)';
  };
  img.onerror = () => settle(CAMERA_FAILED_MSG);

  img.src = `/api/printers/${printer.id}/camera/mjpeg?width=1280&fps=12&t=${Date.now()}`;
  entry.camera = { img, watchdog };
}

// ``message`` keeps a failure explanation on screen; without one this is a
// deliberate stop, so the overlay offers the Start button instead.
function stopCamera(entry, message) {
  if (!entry || !entry.camera) return;
  const { img, watchdog } = entry.camera;
  if (watchdog) clearTimeout(watchdog);
  try { img.onload = null; img.onerror = null; img.src = ''; } catch (_) { /* ignore */ }
  img.classList.add('hidden');
  if (entry.camRefs) {
    entry.camRefs.overlay.textContent = message || 'Camera stopped.';
    entry.camRefs.overlay.classList.remove('hidden');
    entry.camRefs.status.textContent = message ? 'no stream' : 'stopped';
  }
  entry.camera = null;
}

function cameraFullscreen(entry, on) {
  const box = entry && entry.camRefs && entry.camRefs.box;
  if (!box) return;
  if (on) {
    if (box.requestFullscreen) box.requestFullscreen().catch(() => { /* denied */ });
  } else if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => { /* ignore */ });
  }
}

function isCamFullscreen(entry) {
  const box = entry && entry.camRefs && entry.camRefs.box;
  return Boolean(box && document.fullscreenElement === box);
}

// Going fullscreen takes the camera box out of the flow, so the observer calls
// the card off screen and the stream would be torn down. Re-sync on both edges
// of the transition: keep it running while fullscreen, and bring it back when
// the card is still visible after leaving fullscreen.
document.addEventListener('fullscreenchange', () => {
  cards.forEach((entry) => {
    if (!entry || !entry.camRefs || !entry.camRefs.box) return;
    const full = isCamFullscreen(entry);
    if (full && state.cameraEnabled && !entry.camera) {
      startCamera(printerById(entry.printerId), entry);
    } else if (!full && state.cameraEnabled && entry.inView && !entry.camera) {
      startCamera(printerById(entry.printerId), entry);
    }
  });
});

/* ------------------------------------------------------------- interactions */

async function handleAction(printerId, el, entry) {
  const a = el.dataset.action;
  const success = (m) => toast(m);

  switch (a) {
    case 'pause': case 'resume':
      await post(`/api/printers/${printerId}/${a}`, {}); success(`${a} sent`); break;
    case 'stop':
      if (!window.confirm('Stop the current print? This aborts the job.')) break;
      await post(`/api/printers/${printerId}/stop`, {}); success('stop sent'); break;
    case 'retry':
      await post(`/api/printers/${printerId}/retry`, {}); success('recovery sent'); break;
    case 'reprint': {
      const printer = printerById(printerId);
      if (!printer || !printableJob(printer.job)) { toast('Nothing to reprint', 'warn'); break; }
      const path = (state.printPath && state.printPath[printerId]) || ('/' + printer.job);
      await openPrintDialog(printerId, path);
      break;
    }
    case 'clear-error': {
      if (await post(`/api/printers/${printerId}/clear-error`, {})) {
        toast('Error cleared. If the printer still shows FAILED, dismiss it on its screen.', 'warn');
      }
      break;
    }
    case 'refresh':
      await post(`/api/printers/${printerId}/refresh`, {}); success('state dump requested'); break;
    case 'speed':
      await post(`/api/printers/${printerId}/speed`, { speed_level: Number(el.dataset.value) });
      success('speed profile set'); break;
    case 'light': {
      const ok = await post(`/api/printers/${printerId}/light-mode`, {
        node: el.dataset.node, mode: el.dataset.value,
        times: el.dataset.value === 'flashing' ? 3 : 1
      });
      if (ok) { const p = printerById(printerId); if (p) p.lights = Object.assign({}, p.lights || {}, { [el.dataset.node]: el.dataset.value }); scheduleRender(); success('light updated'); }
      break;
    }
    case 'open-skip':
      openSkipModal(printerId);
      break;
    case 'calibrate': {
      if (!motionConfirmed(el)) { toast('Tick the confirmation box first — this moves the printer', 'warn'); return; }
      const label = el.dataset.value.replace(/_/g, ' ');
      if (!window.confirm(`Start "${label}"?\n\nThis physically moves the printer.`)) return;
      await post(`/api/printers/${printerId}/calibrate`, {
        kind: el.dataset.value, mode: Number(el.dataset.mode || 1), confirm_motion: true
      });
      success('calibration started'); break;
    }
    case 'jog-step':
      entry.jogStep = Number(el.dataset.step) || 1;
      renderPanel(printerById(printerId), entry, true);
      break;
    case 'home': {
      if (!motionConfirmed(el)) { toast('Tick the confirmation box first — homing moves the printer', 'warn'); return; }
      const axes = (el.dataset.axes || '').toUpperCase();
      if (await post(`/api/printers/${printerId}/home`, { axes, confirm_motion: true })) {
        success(axes ? `homing ${axes}` : 'homing all axes');
      }
      break;
    }
    case 'jog': {
      if (!motionConfirmed(el)) { toast('Tick the confirmation box first — jogging moves the printer', 'warn'); return; }
      const step = entry.jogStep || 1;
      const dir = Number(el.dataset.dir);
      const axis = el.dataset.axis.toUpperCase();
      const feed = Number(el.dataset.feed) || 3000;
      const distance = dir * step;
      if (await post(`/api/printers/${printerId}/jog`, {
        axis, distance, feedrate: feed, confirm_motion: true
      })) {
        success(`jog ${axis} ${distance > 0 ? '+' : ''}${distance} mm`);
      }
      break;
    }
    case 'filament-edit':
      openFilamentEditor(printerId, Number(el.dataset.amsId), Number(el.dataset.tray));
      break;
    case 'extrude': {
      if (!motionConfirmed(el)) { toast('Tick the confirmation box first', 'warn'); return; }
      const amount = Number(el.dataset.value);
      if (await post(`/api/printers/${printerId}/extrude`, { amount, confirm_thermal: true })) {
        success(amount > 0 ? `extruding ${amount} mm` : `retracting ${Math.abs(amount)} mm`);
      }
      break;
    }
    case 'preset': {
      const body = el.closest('[data-r="panel"]');
      const values = {
        nozzle: Number(el.dataset.nozzle), bed: Number(el.dataset.bed), chamber: Number(el.dataset.chamber)
      };
      setTempInputs(body, values);
      if (await sendTemps(printerId, values, body)) success(`${el.textContent.trim()} sent`);
      break;
    }
    case 'cooldown': {
      const body = el.closest('[data-r="panel"]');
      const values = { nozzle: 0, bed: 0, chamber: 0 };
      setTempInputs(body, values);
      if (await sendTemps(printerId, values, body)) success('cool-down sent');
      break;
    }
    case 'refresh-files': loadFiles(printerId, entry.fileCache.path, true); break;
    case 'files-dir':
      fileView.set('dir', fileView.dir === 'asc' ? 'desc' : 'asc');
      refreshFileView(el);
      break;
    case 'cd':
      // Directory rows carry data-dir; breadcrumb buttons do not.
      if (el.dataset.dir === '1' || el.dataset.dir === undefined) loadFiles(printerId, el.dataset.path);
      break;
    case 'print-sd':
      openPrintDialog(printerId, el.dataset.path);
      break;
    case 'download-sd':
      window.location.href = `/api/printers/${printerId}/files/download?path=${encodeURIComponent(el.dataset.path)}`;
      break;
    case 'delete-sd': {
      if (!window.confirm(`Delete ${el.dataset.path} from the SD card?`)) return;
      try {
        await api(`/api/printers/${printerId}/files`, {
          method: 'DELETE', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: el.dataset.path, is_dir: el.dataset.dir === '1' })
        });
        toast('deleted');
        loadFiles(printerId, entry.fileCache.path, true);
      } catch (err) { toast(err.message, 'error'); }
      break;
    }
    case 'file-menu-close': {
      const menu = el.closest('details.file-menu');
      if (menu) menu.removeAttribute('open');
      break;
    }
    case 'print-staged': {
      const file = el.dataset.file;
      const printer = printerById(printerId);
      el.disabled = true;
      try {
        // send it to the printer first, then reuse the normal print preview
        const r = await api(`/api/printers/${printerId}/files/upload-staged`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: file, dir_path: '/' })
        });
        toast(`sent ${r.name} to ${printer ? printer.name : printerId}`);
        state.printPath[printerId] = r.path;
        await openPrintDialog(printerId, r.path);
      } catch (err) {
        toast(err.message, 'error');
      } finally {
        el.disabled = false;
      }
      break;
    }
    case 'fan': {
      const fan = el.dataset.fan;
      const speed = Number(el.dataset.speed);
      const ok = await post(`/api/printers/${printerId}/fan`, { fan, speed });
      if (ok) {
        const p = printerById(printerId);
        if (p) setFanLocally(p, fan, speed);
        success(`${fan} fan set to ${speed}%`);
      }
      break;
    }
    case 'rename-sd': {
      const current = el.dataset.name || '';
      const suggested = current.replace(/\.3mf$/i, '');
      const typed = window.prompt(
        `Rename "${current}"\n\nEnter a new name (the .3mf extension is kept):`, suggested);
      if (typed === null) return;
      let name = typed.trim();
      if (!name) return;
      if (!/\.3mf$/i.test(name)) name += '.3mf';
      if (/[\\/]/.test(name)) { toast('A file name cannot contain a path', 'error'); return; }
      try {
        const r = await api(`/api/printers/${printerId}/files/rename`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: el.dataset.path, new_name: name })
        });
        toast(r.status === 'unchanged' ? 'name unchanged' : `renamed to ${r.name}`);
        loadFiles(printerId, entry.fileCache.path, true);
      } catch (err) { toast(err.message, 'error'); }
      break;
    }
    case 'remove-node-start': {
      const gate = el.closest('[data-role="remove-gate"]');
      if (gate) gate.innerHTML = removeChallengeHtml();
      break;
    }
    case 'remove-node-cancel': {
      const challenge = el.closest('[data-role="remove-challenge"]');
      const gate = challenge && challenge.parentElement;
      if (gate) {
        gate.innerHTML = `<button class="btn btn-danger w-full" data-action="remove-node-start"
            >Remove this printer…</button>`;
      }
      break;
    }
    case 'remove-node-pick': {
      const challenge = el.closest('[data-role="remove-challenge"]');
      if (!challenge) return;
      const target = Number(challenge.dataset.target);
      if (Number(el.dataset.value) !== target) {
        // wrong number: do not confirm, just re-roll the challenge
        toast(`That was ${el.dataset.value}, not ${target}. Try again.`, 'error');
        challenge.outerHTML = removeChallengeHtml();
        return;
      }
      await removePrinter(printerId);
      break;
    }
    case 'ams': {
      const action = el.dataset.ams;
      const heavy = ['feed', 'unload', 'resume', 'filament_setting', 'auto'].includes(action);
      if (heavy && !window.confirm(`AMS ${action} heats the hotend and moves filament. Continue?`)) return;
      try {
        await api(`/api/printers/${printerId}/ams`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            action,
            ams_id: el.dataset.amsId === undefined ? 0 : Number(el.dataset.amsId),
            tray_id: Number(el.dataset.tray || 0),
            confirm_thermal: heavy
          })
        });
        success(`AMS ${action} sent`);
      } catch (err) { toast(err.message, 'error'); }
      break;
    }
    case 'printer_info':
      await sendCommand(printerId, 'printer_info'); break;
    case 'edit-node':
      openModal(printerById(printerId)); break;
    case 'reboot':
      if (!window.confirm('Reboot the printer? The MQTT link will drop and reconnect.')) return;
      await post(`/api/printers/${printerId}/reboot`, { module: 'esp32' }); success('reboot sent'); break;
    case 'cam-start': startCamera(printerById(printerId), entry); break;
    case 'cam-stop': stopCamera(entry); toast('camera stopped'); break;
    case 'cam-fullscreen': cameraFullscreen(entry, true); break;
    case 'cam-normal': cameraFullscreen(entry, false); break;
    case 'cam-snapshot': {
      try {
        const res = await fetch(`/api/printers/${printerId}/camera/snapshot?t=${Date.now()}`);
        if (!res.ok) throw new Error('snapshot unavailable');
        const url = URL.createObjectURL(await res.blob());
        const snap = (cards.get(printerId) || {}).camRefs;
        if (snap && snap.snap) {
          snap.snap.src = url;
          snap.snap.classList.remove('hidden');
          setTimeout(() => { snap.snap.classList.add('hidden'); snap.snap.src = ''; URL.revokeObjectURL(url); }, 5000);
          toast('Snapshot captured');
        } else { window.open(url, '_blank'); }
      } catch (err) { toast('Snapshot unavailable', 'error'); }
      break;
    }
    default:
      break;
  }
}

function readTemp(body, key) {
  const input = body.querySelector(`[data-role="input-${key}"]`);
  return input ? Math.max(0, Math.min(Number(input.max), Number(input.value) || 0)) : 0;
}

function setTempInputs(body, values) {
  if (!body) return;
  Object.entries(values).forEach(([key, value]) => {
    const slider = body.querySelector(`[data-role="slider-${key}"]`);
    const input = body.querySelector(`[data-role="input-${key}"]`);
    if (slider) slider.value = value;
    if (input) input.value = value;
  });
}

// Send one or more setpoints. The UI always sets confirm_thermal: the user
// confirmed by releasing a slider or tapping a preset. The allow-while-printing
// tick rides along on every send.
async function sendTemps(printerId, values, body) {
  if (!body) return false;
  const payload = { confirm_thermal: true };
  ['nozzle', 'bed', 'chamber'].forEach((key) => {
    if (values[key] === undefined || values[key] === null) return;
    // only send chamber when that tile exists on this printer
    if (key === 'chamber' && !body.querySelector('[data-role="input-chamber"]')) return;
    payload[key] = values[key];
  });
  if (Object.keys(payload).length === 1) return false;   // nothing but the flag
  const allow = body.querySelector('[data-role="allow-while-printing"]');
  payload.allow_while_printing = Boolean(allow && allow.checked);
  try {
    await api(`/api/printers/${printerId}/temperature`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    });
    return true;
  } catch (err) { toast(err.message, 'error'); return false; }
}

function systemHtml(p) {
  const cells = [
    ['Model', p.modelId], ['Nozzle', `${p.nozzleDiameter || '—'} ${p.nozzleType || ''}`],
    ['Wi-Fi', p.wifi], ['SD card', p.sdcard == null ? '—' : (p.sdcard ? 'present' : 'absent')],
    ['IP', p.ip], ['Serial', p.sn], ['AMS units', p.ams_count], ['Homed', p.homed == null ? '—' : (p.homed ? 'yes' : 'no')]
  ];
  return `
    <div class="space-y-3">
      <div class="grid grid-cols-2 gap-2 font-mono text-[11px] sm:grid-cols-4">
        ${cells.map(([k, v]) => `<div class="tile p-2"><span class="block text-[9px] uppercase tracking-wider t-mut">${k}</span>${escapeHtml(v || '—')}</div>`).join('')}
      </div>
      <div>
        <p class="mb-1.5 text-[10px] uppercase tracking-wider t-mut">Maintenance</p>
        <div class="flex flex-wrap gap-1.5">
          <button class="btn btn-ghost" data-action="edit-node">Edit name / IP / code</button>
          <button class="btn btn-ghost" data-action="printer_info">Firmware query</button>
              <button class="btn btn-danger" data-action="reboot">Reboot printer</button>
        </div>
      </div>
      <div>
        <p class="mb-1.5 text-[10px] uppercase tracking-wider t-mut">Push a project to the SD root</p>
        <input type="file" accept=".3mf,.gcode" data-role="sd-upload"
               class="w-full font-mono text-[11px] t-mut file:mr-2 file:rounded-lg file:border-0 file:surface-3 file:px-2.5 file:py-1.5 file:font-mono file:text-[11px] file:t-accent">
      </div>
      <div>
        <p class="mb-1.5 text-[10px] uppercase tracking-wider t-danger">Danger zone</p>
        <div data-role="remove-gate">
          <button class="btn btn-danger w-full" data-action="remove-node-start">Remove this printer…</button>
          <p class="mt-1.5 font-mono text-[10px] t-mut">
            Forgets this node and its access code. The printer itself is untouched.
          </p>
        </div>
      </div>
    </div>`;
}

// Three numbers, one of which the user must tap to prove intent - the same
// shape as reading a code off an authenticator app, and hard to dismiss by
// reflex the way a plain "are you sure?" dialog can be. This is a guard against
// misclicks, not a security control.
function removeChallengeHtml() {
  const pick = () => 1 + Math.floor(Math.random() * 99);
  const target = pick();
  const options = new Set([target]);
  while (options.size < 3) options.add(pick());
  const shuffled = [...options].sort(() => Math.random() - 0.5);

  return `
    <div data-role="remove-challenge" data-target="${target}" class="space-y-2">
      <p class="font-mono text-[11px] t-body">
        Tap <span class="text-base font-bold t-danger">${target}</span> to confirm.
      </p>
      <div class="grid grid-cols-3 gap-2">
        ${shuffled.map((n) => `<button class="btn btn-ghost" data-action="remove-node-pick"
          data-value="${n}">${n}</button>`).join('')}
      </div>
      <button class="btn btn-ghost w-full" data-action="remove-node-cancel">Cancel</button>
    </div>`;
}

/* ---------------------------------------------------------------- SD load */

async function renderFiles(printer, body) {
  const entry = cards.get(printer.id);
  if (!entry) return;
  const cache = entry.fileCache;
  const sig = `files:${cache.path}:${cache.loading}:${cache.error || ''}:` +
    `${fileView.sort}:${fileView.dir}:${fileView.filter}:${fileView.q}:` +
    `${printer.status}:${printer.job || ''}:` +
    (cache.listing || []).map((f) => `${f.path}:${f.size}`).join('|');
  if (body.dataset.sig === sig) return;
  body.dataset.sig = sig;
  body.innerHTML = filesHtml(cache, printer);
  if (!cache.listing && !cache.loading && !cache.error) loadFiles(printer.id, cache.path);
}

async function loadFiles(printerId, path, force = false) {
  const entry = cards.get(printerId);
  if (!entry) return;
  const cache = entry.fileCache;
  if (!force && cache.path === path && cache.listing) {
    const body = entry.refs.panel;
    if (body) { body.dataset.sig = ''; renderFiles(printerById(printerId), body); }
    return;
  }
  cache.path = path; cache.loading = true; cache.error = null;
  const body = entry.refs.panel;
  if (body) { body.dataset.sig = ''; renderFiles(printerById(printerId), body); }

  try {
    const data = await api(`/api/printers/${printerId}/files?path=${encodeURIComponent(path)}`);
    cache.listing = data.entries;
    cache.parent = data.parent;   // drives the "up one level" button
  } catch (err) {
    cache.error = err.message;
    cache.listing = null;
  }
  cache.loading = false;
  if (cards.get(printerId) === entry && entry.tab === 'files') {
    const live = entry.refs.panel;
    if (live) { live.dataset.sig = ''; renderFiles(printerById(printerId), live); }
  }
}

/* ------------------------------------------------------- pre-armed skips */

// The armed ids for a printer, but only when the arm is for this exact project.
// Anything else belongs to another file and must not ride along.
function armedSkipIds(printerId, jobName) {
  const arm = state.preSkip[printerId];
  const want = baseName(jobName || '');
  if (!arm || !want || baseName(arm.job) !== want) return [];
  return Array.isArray(arm.ids) ? arm.ids.slice() : [];
}

// Clear the arm once the backend has applied it (echoed back as `skipped`). If it
// could not be applied the arm stays, and applyPreSkip() remains the fallback.
function consumePreSkip(printerId, jobName, applied) {
  const arm = state.preSkip[printerId];
  if (!arm) return;
  if (baseName(arm.job) !== baseName(jobName || '')) return;
  if (applied) delete state.preSkip[printerId];
}

function markSkipped(printerId, ids) {
  const printer = printerById(printerId);
  if (!printer || !ids || !ids.length) return;
  printer.skippedIds = [...new Set([...(printer.skippedIds || []), ...ids])];
}

/* ------------------------------------------------------------ dispatch */

async function dispatchToPrinter(printerId, filename) {
  const entry = cards.get(printerId);
  if (entry) entry.root.classList.add('opacity-60');
  const skipIds = armedSkipIds(printerId, filename);
  try {
    const res = await api('/api/dispatch-print', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        printer_id: printerId, filename, plate_index: 1,
        bed_levelling: true, flow_cali: true, vibration_cali: true,
        timelapse: true, use_ams: true,
        skip_object_ids: skipIds.length ? skipIds : null
      })
    });
    const applied = Array.isArray(res.skipped) && res.skipped.length ? res.skipped : [];
    consumePreSkip(printerId, filename, applied.length > 0 || skipIds.length === 0);
    markSkipped(printerId, applied);
    toast(`${res.job} → ${res.printer}`
      + (applied.length ? ` (${applied.length} object${applied.length === 1 ? '' : 's'} skipped)` : ''));
    scheduleRender();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    if (entry) entry.root.classList.remove('opacity-60');
    state.dragFile = null;
  }
}

/* ------------------------------------------------------------- quick bits */

async function setProfile(id, level) {
  const ok = await post(`/api/printers/${id}/speed`, { speed_level: level });
  if (ok) { const p = printerById(id); if (p) p.speedLevel = level; scheduleRender(); }
}

/* ------------------------------------------------------- staging + drop */

// The staging queue lives inside the selected printer's Staging tab. The markup
// is built here rather than shipped in index.html because the panel is rebuilt
// per tab, so the listeners are re-attached each time it is rendered.
function stagingHtml() {
  const current = selectedPrinter();
  const target = current && current.name ? ` (${escapeHtml(current.name)})` : '';
  const recipient = current ? ` to this printer${target}` : '';
  return `
    <div class="space-y-3">
      <div class="section-head">
        <div class="section-head-text">
          <h2 class="section-title">Staging queue</h2>
          <p class="section-sub">Drop a sliced project here, then send it to the selected printer.</p>
        </div>
        <span id="staged-count" class="chip border line surface-2 t-mut">0 files</span>
      </div>

      <div class="staging-grid">
        <div id="drop-zone" class="drop-zone">
          <input type="file" id="file-input" class="hidden" accept=".3mf,.gcode" multiple>
          <div class="drop-inner">
            <svg class="mx-auto mb-1 h-6 w-6 t-mut" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.7" d="M7 16a4 4 0 01-.88-7.9A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"/>
            </svg>
            <span class="drop-title">Drop or click to stage</span>
            <span class="drop-sub">.3mf / .gcode</span>
          </div>
        </div>
        <div id="staged-container" class="staged-list"></div>
      </div>

      <p class="font-mono text-[10px] t-mut">
        Staged files stay on this machine. <span class="t-body">Print…</span> sends one to the
        selected printer${recipient} and opens the usual preview, where you pick the plate and map
        the filaments. Dragging a file onto the card still works with a mouse.
      </p>
    </div>`;
}

function wireStaging(body) {
  const drop = body.querySelector('#drop-zone');
  const input = body.querySelector('#file-input');
  const list = body.querySelector('#staged-container');
  if (!drop || !input || !list) return;

  drop.addEventListener('click', () => input.click());
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('drag-target-hover'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-target-hover'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('drag-target-hover');
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
  input.addEventListener('change', (e) => {
    if (e.target.files.length) uploadFiles(e.target.files);
    e.target.value = '';
  });
  list.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-remove]');
    if (btn) { e.preventDefault(); removeStaged(btn.dataset.remove); }
  });
  list.addEventListener('dragstart', (e) => {
    const item = e.target.closest('[data-file]');
    if (!item) return;
    state.dragFile = item.dataset.file;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', state.dragFile);
  });
}

function renderStaged() {
  const box = $('staged-container');
  if (!box) return;  // the Staging tab is not open
  $('staged-count').textContent = `${state.staged.length} file${state.staged.length === 1 ? '' : 's'}`;
  if (!state.staged.length) {
    box.innerHTML = '<div class="grid h-full place-items-center font-mono text-[11px] t-dim">Nothing staged yet</div>';
    return;
  }
  box.innerHTML = state.staged.map((fn) => `
    <div class="file-row" draggable="true" data-file="${escapeHtml(fn)}">
      <span class="grid h-8 w-8 shrink-0 place-items-center rounded-lg border line-soft surface-3 t-mut"
            aria-hidden="true">▤</span>
      <span class="min-w-0 flex-1">
        <span class="block truncate font-mono text-[12px] t-body">${escapeHtml(fn)}</span>
        <span class="block truncate font-mono text-[10px] t-mut">staged on this server</span>
      </span>
      <div class="file-actions">
        <button class="btn btn-primary" data-action="print-staged" data-file="${escapeHtml(fn)}">Print…</button>
        <span class="drag-hint font-mono text-[10px] font-bold t-accent">DRAG →</span>
        <button class="btn btn-ghost" data-remove="${escapeHtml(fn)}"
                title="Remove from staging">✕</button>
      </div>
    </div>`).join('');
}

async function loadStaged() {
  try {
    const files = await api('/api/uploads');
    state.staged = files.map((f) => f.filename);
  } catch (_) {
    state.staged = [];
  }
  renderStaged();
}

async function removeStaged(filename) {
  if (!window.confirm(`Remove ${filename} from the staging queue?`)) return;
  try {
    await api(`/api/uploads/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    state.staged = state.staged.filter((f) => f !== filename);
    renderStaged();
    toast('Removed from staging');
  } catch (err) {
    toast(err.message, 'error');
  }
}

async function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch('/api/stage-upload', { method: 'POST', body: form });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const data = await res.json();
      state.staged.push(data.filename);
      toast(`staged ${data.filename}`);
    } catch (err) {
      toast(`${file.name}: ${err.message}`, 'error');
    }
  }
  renderStaged();
}

/* ------------------------------------------------------- printer management */

function openModal(printer = null) {
  const editing = Boolean(printer);
  $('modal-title').textContent = editing ? 'Edit printer node' : 'Register printer node';
  $('modal-subtitle').textContent = editing
    ? `Node ${printer.id} · saving reconnects this printer`
    : 'Credentials stay on this host.';
  $('form-id').value = editing ? printer.id : '';
  $('form-name').value = editing ? printer.name : '';
  $('form-ip').value = editing ? printer.ip : '';
  $('form-sn').value = editing ? printer.sn : '';
  $('form-code').value = editing ? (printer.access_code || '') : '';
  $('form-bed-type').value = editing ? (printer.bed_type || 'textured_pei') : 'textured_pei';
  $('form-ams').value = editing ? (printer.ams_count != null ? printer.ams_count : 1) : 1;
  $('modal-save').textContent = editing ? 'Save changes' : 'Save node';
  const del = $('modal-delete');
  del.classList.toggle('hidden', !editing);
  del.onclick = editing ? () => { closeModal(); removePrinter(printer.id); } : null;
  $('modal-printer').classList.replace('hidden', 'flex');
  setTimeout(() => $('form-name').focus(), 30);
}

function closeModal() { $('modal-printer').classList.replace('flex', 'hidden'); }

async function savePrinter(event) {
  event.preventDefault();
  const existingId = $('form-id').value.trim();
  const printer = {
    id: existingId || `node_${Math.random().toString(36).substring(2, 9)}`,
    name: $('form-name').value.trim(),
    ip: $('form-ip').value.trim(),
    sn: $('form-sn').value.trim().toUpperCase(),
    access_code: $('form-code').value.trim(),
    bed_type: $('form-bed-type').value,
    ams_count: parseInt($('form-ams').value, 10) || 0
  };
  try {
    await api('/api/printers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(printer)
    });
    closeModal();
    toast(existingId ? `${printer.name} updated` : `${printer.name} registered`);
    loadFleet();
  } catch (err) { toast(err.message, 'error'); }
}

// The number challenge in the System tab is the confirmation, so there is no
// second browser dialog here.
async function removePrinter(id) {
  try {
    await api(`/api/printers/${id}`, { method: 'DELETE' });
    state.fleet = state.fleet.filter((p) => p.id !== id);
    if (state.selectedId === id) state.selectedId = null;  // renderFleet picks the next one
    toast('node removed');
    scheduleRender();
  } catch (err) { toast(err.message, 'error'); }
}


// The fan name maps onto the telemetry field the printer reports it in.
function setFanLocally(printer, fan, speed) {
  const key = fan === 'part' ? 'fan' : fan === 'aux' ? 'auxFan' : 'chamberFan';
  printer[key] = speed;
}

// Re-render the Files panel after a view change (sort / filter / search).
function refreshFileView(target) {
  const card = target.closest && target.closest('[data-card-id]');
  if (!card) return;
  const entry = cards.get(card.dataset.cardId);
  if (!entry || entry.tab !== 'files' || !entry.refs.panel) return;
  entry.refs.panel.dataset.sig = '';
  renderFiles(printerById(card.dataset.cardId), entry.refs.panel);
}


// Close an open per-file menu. A tap on the modal backdrop (the panel itself,
// not the card inside it) closes it too, so the backdrop is a real dismiss
// surface while taps inside the card do nothing.
function closeFileMenus(except) {
  document.querySelectorAll('details.file-menu[open]').forEach((d) => {
    const panel = d.querySelector('.file-menu-panel');
    const onBackdrop = except && panel && except === panel;
    if (onBackdrop || !except || !d.contains(except)) d.removeAttribute('open');
  });
}


/* --------------------------------------------------------------- theme */

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') || 'dark';
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('bfm-theme', theme); } catch (_) { /* private mode */ }
  const icon = $('theme-icon');
  if (icon) icon.textContent = theme === 'dark' ? '🌙' : '☀️';
}

function toggleTheme() {
  applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('bfm-theme'); } catch (_) { /* ignore */ }
  let theme = saved;
  if (!theme) {
    const light = window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches;
    theme = light ? 'light' : 'dark';
  }
  applyTheme(theme);

  // follow the OS while the user has not chosen explicitly
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
      let chosen = null;
      try { chosen = localStorage.getItem('bfm-theme'); } catch (_) { /* ignore */ }
      if (!chosen) applyTheme(e.matches ? 'light' : 'dark');
    });
  }
}

/* ------------------------------------------------------------ settings */

function applyMotionBadge() {
  const badge = $('motion-badge');
  if (badge) badge.classList.toggle('hidden', !state.allowMotion);
}

function setCameraEnabled(enabled) {
  state.cameraEnabled = enabled;
  try { localStorage.setItem('bfm-cameras', enabled ? '1' : '0'); } catch (_) { /* ignore */ }
  updateCamToggle();
  cards.forEach((entry, id) => {
    if (enabled) startCamera(printerById(id), entry);
    else stopCamera(entry);
  });
}

function updateCamToggle() {
  const btn = $('cam-toggle');
  if (!btn) return;
  btn.textContent = state.cameraEnabled ? '📷' : '🚫';
  btn.title = state.cameraEnabled ? 'Cameras on (click to disable)' : 'Cameras off (click to enable)';
  btn.classList.toggle('t-danger', !state.cameraEnabled);
}

function initCameraPref() {
  let saved = null;
  try { saved = localStorage.getItem('bfm-cameras'); } catch (_) { /* ignore */ }
  state.cameraEnabled = saved === null ? true : saved === '1';
  updateCamToggle();
}

/* --------------------------------------------------------- speed modal */

function openSpeedModal(printerId) {
  const printer = printerById(printerId);
  if (!printer) return;
  state.speedModalPrinter = printerId;
  $('speed-modal-sub').textContent =
    `${printer.name} · now ${speedLabel(printer) || 'unknown'} (${printer.status || '?'})`;
  const level = printer.speedLevel || 0;
  $('speed-options').innerHTML = [1, 2, 3, 4].map((l) => `
    <button class="choice ${level === l ? 'is-active' : ''}" data-speed="${l}">
      <span class="choice-title">${SPEED_LABELS[l]}</span>
      <span class="choice-sub">${SPEED_PERCENT[l]}%</span>
    </button>`).join('');
  $('modal-speed').classList.replace('hidden', 'flex');
}

function closeSpeedModal() { $('modal-speed').classList.replace('flex', 'hidden'); }

async function pickSpeed(level) {
  const id = state.speedModalPrinter;
  if (!id) return;
  const ok = await post(`/api/printers/${id}/speed`, { speed_level: level });
  if (ok) {
    const printer = printerById(id);
    if (printer) printer.speedLevel = level;
    toast(`Speed: ${SPEED_LABELS[level]} ${SPEED_PERCENT[level]}%`);
    scheduleRender();
  }
  closeSpeedModal();
}


async function loadSettings() {
  try {
    const cfg = await api('/api/settings');
    state.allowMotion = Boolean(cfg.allow_motion);
    applyMotionBadge();
  } catch (_) { /* keep the safe default */ }
}

function openSettings() {
  api('/api/settings').then((cfg) => {
    $('settings-host').value = cfg.host || '0.0.0.0';
    $('settings-port').value = cfg.port || 8000;
    $('settings-current-port').textContent = cfg.current_port || '—';
    $('settings-allow-motion').checked = Boolean(cfg.allow_motion);
    $('settings-token').value = '';
    $('settings-clear-auth').checked = false;
    $('settings-auth-state').textContent = cfg.auth_required
      ? 'A token is required for this server.' : 'No token set — anyone on the LAN can control the printers.';
    $('settings-url').textContent = `http://${location.hostname}:${cfg.port || 8000}`;
    $('update-current').textContent = `v${($('app-version').textContent || '').trim()}`;
    $('update-result').classList.add('hidden');
    $('update-download').classList.add('hidden');
    $('update-staged-actions').classList.add('hidden');
    $('update-install').disabled = false;
    $('update-install').textContent = 'Run installer';
    $('update-download').disabled = false;
    $('modal-settings').classList.replace('hidden', 'flex');
  }).catch((err) => toast(err.message, 'error'));
}

function closeSettings() { $('modal-settings').classList.replace('flex', 'hidden'); }

async function saveSettings(event) {
  event.preventDefault();
  const body = {
    host: $('settings-host').value.trim() || '0.0.0.0',
    port: parseInt($('settings-port').value, 10) || 8000,
    allow_motion: $('settings-allow-motion').checked,
    auth_token: $('settings-token').value.trim(),
    clear_auth: $('settings-clear-auth').checked
  };
  try {
    await api('/api/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    state.allowMotion = body.allow_motion;
    applyMotionBadge();
    // rebuild any open panels so the tick / "unlocked" note reflects the change
    cards.forEach((entry, id) => renderPanel(printerById(id), entry, true));
    scheduleRender();
    closeSettings();
    toast('Settings saved.', 'warn');
  } catch (err) { toast(err.message, 'error'); }
}


/* ------------------------------------------------------------ updater */

function setUpdateResult(text, kind) {
  const el = $('update-result');
  el.textContent = text;
  el.classList.remove('hidden', 'text-emerald-400', 'text-amber-400', 'text-red-400');
  if (kind === 'ok') el.classList.add('text-emerald-400');
  else if (kind === 'warn') el.classList.add('text-amber-400');
  else if (kind === 'error') el.classList.add('text-red-400');
}

async function checkForUpdates() {
  const btn = $('update-check');
  btn.disabled = true;
  btn.textContent = 'Checking…';
  $('update-download').classList.add('hidden');
  try {
    const r = await api('/api/update');
    if (r.current) $('update-current').textContent = `v${r.current}`;
    if (r.update_available) {
      const size = r.asset_size ? ` (${formatBytes(r.asset_size)})` : '';
      setUpdateResult(`Version ${r.latest} is available${size}.`, 'ok');
      const download = $('update-download');
      download.dataset.version = r.latest;
      download.textContent = `Download installer for ${r.latest}`;
      download.classList.remove('hidden');
    } else if (r.error) {
      setUpdateResult(r.error, 'warn');
    } else {
      setUpdateResult(`You're on the latest version (${r.current}).`, 'ok');
    }
  } catch (err) {
    setUpdateResult(err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check for updates';
  }
}

// Step 1: fetch the installer and leave it on disk. Nothing is launched, so a
// failed or slow download can simply be retried without touching the install.
async function downloadUpdate() {
  const download = $('update-download');
  const version = download.dataset.version || 'the new version';
  download.disabled = true;
  download.textContent = 'Downloading… (~45 MB)';
  try {
    const r = await api('/api/update/download', { method: 'POST' });
    // filename + size only: a full Windows path busts the narrow modal
    const name = String(r.installer || '').split(/[\\/]/).pop() || 'installer';
    setUpdateResult(`Downloaded ${name} (${formatBytes(r.size)}). Run it whenever you are ready.`, 'ok');
    $('update-save').setAttribute('download', `BambuFleetManagerSetup-${r.version}.exe`);
    $('update-install').dataset.version = r.version;
    $('update-staged-actions').classList.remove('hidden');
  } catch (err) {
    setUpdateResult(`${err.message}`, 'error');
  } finally {
    download.disabled = false;
    download.textContent = `Download installer for ${version}`;
  }
}

// Step 2: launch the installer that is already on disk, then let the app quit.
async function installUpdate() {
  const install = $('update-install');
  const version = install.dataset.version || $('update-download').dataset.version || 'the new version';
  if (!window.confirm(`Run the ${version} installer?\n\nThe app will close and restart when it's done.`)) return;
  install.disabled = true;
  install.textContent = 'Starting…';
  try {
    const r = await api('/api/update/run', { method: 'POST' });
    setUpdateResult(`Installing ${r.version}… the app will restart in a moment.`, 'ok');
    install.textContent = 'Installing…';
  } catch (err) {
    setUpdateResult(err.message, 'error');
    install.disabled = false;
    install.textContent = 'Run installer';
  }
}


/* ----------------------------------------------------- filament editor */

/* Bambu material catalogue - loaded from /api/filaments (ids read off genuine
   spool RFID tags). This small list keeps the editor usable before the fetch
   resolves or if the endpoint is unavailable. */
const FILAMENT_FALLBACK = [
  { key: 'PLA Basic', label: 'PLA Basic', category: 'PLA', id: 'GFA00', type: 'PLA', min: 190, max: 230 },
  { key: 'PETG Basic', label: 'PETG Basic', category: 'PETG', id: 'GFG00', type: 'PETG', min: 230, max: 260 },
  { key: 'ABS', label: 'ABS', category: 'ABS', id: 'GFB00', type: 'ABS', min: 240, max: 270 },
  { key: 'ASA', label: 'ASA', category: 'ASA', id: 'GFB01', type: 'ASA', min: 240, max: 270 }
];

function filamentList() {
  return (state.filaments && state.filaments.length) ? state.filaments : FILAMENT_FALLBACK;
}

async function loadFilaments() {
  try {
    const res = await api('/api/filaments');
    if (res && Array.isArray(res.filaments) && res.filaments.length) {
      state.filaments = res.filaments;
      state.filamentCategories = res.categories || [];
    }
  } catch (err) { /* keep the built-in fallback */ }
}

function filamentByLabel(label) {
  const list = filamentList();
  const key = String(label || '').toLowerCase();
  return list.find((f) => f.label.toLowerCase() === key)
    || list.find((f) => f.key.toLowerCase() === key)
    || list[0];
}

function filamentForTray(tray) {
  const list = filamentList();
  const idx = String((tray && tray.tray_info_idx) || '').toUpperCase();
  if (idx) {
    const byId = list.find((f) => f.id === idx);
    if (byId) return byId;
  }
  const type = (tray && tray.tray_type) || '';
  if (type) {
    const byType = list.find((f) => f.type === type);
    if (byType) return byType;
  }
  return list[0];
}

function openFilamentEditor(printerId, amsId, trayId) {
  const printer = printerById(printerId);
  if (!printer) return;
  const unit = ((printer.ams && printer.ams.ams) || []).find((u) => Number(u.id) === Number(amsId));
  const tray = unit && (unit.tray || []).find((t) => Number(t.id) === Number(trayId));
  state.filament = { printerId, amsId: Number(amsId), trayId: Number(trayId) };

  $('filament-sub').textContent = `AMS ${Number(amsId) + 1} · tray T${Number(trayId) + 1} · ${printer.name}`;
  const list = filamentList();
  const categories = (state.filamentCategories && state.filamentCategories.length)
    ? state.filamentCategories
    : [...new Set(list.map((f) => f.category))];
  $('filament-type').innerHTML = categories.map((cat) => {
    const options = list.filter((f) => f.category === cat)
      .map((f) => `<option value="${escapeHtml(f.label)}">${escapeHtml(f.label)}</option>`).join('');
    return options ? `<optgroup label="${escapeHtml(cat)}">${options}</optgroup>` : '';
  }).join('');

  $('filament-type').value = filamentForTray(tray).label;
  const color = amsColor(tray && tray.tray_color) || '#00AE42';
  $('filament-color').value = color;
  const remain = tray && typeof tray.remain === 'number' && tray.remain >= 0 && tray.remain <= 100
    ? tray.remain : 100;
  $('filament-remaining').value = remain;
  updateFilamentPreset();
  $('modal-filament').classList.replace('hidden', 'flex');
}

function updateFilamentPreset() {
  const preset = filamentByLabel($('filament-type').value);
  $('filament-preset').textContent = `profile ${preset.id} · ${preset.type} · nozzle ${preset.min}-${preset.max}°C`;
}

function closeFilamentEditor() { $('modal-filament').classList.replace('flex', 'hidden'); }

async function saveFilament() {
  const { printerId, amsId, trayId } = state.filament || {};
  if (printerId === undefined) return;
  const preset = filamentByLabel($('filament-type').value);
  try {
    await api(`/api/printers/${printerId}/filament`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ams_id: amsId, tray_id: trayId, tray_type: preset.type,
        color: $('filament-color').value,
        remaining: Number($('filament-remaining').value),
        setting_id: preset.id, sub_brands: preset.key,
        temp_min: preset.min, temp_max: preset.max
      })
    });
    toast(`Tray T${trayId + 1} set to ${preset.label}`);
    closeFilamentEditor();
  } catch (err) {
    toast(err.message, 'error');
  }
}

/* --------------------------------------------------- print preview dialog */

function hexToRgb(hex) {
  const clean = String(hex || '').replace('#', '').slice(0, 6);
  if (clean.length !== 6) return null;
  const n = parseInt(clean, 16);
  if (isNaN(n)) return null;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function colorDistance(a, b) {
  const ca = hexToRgb(a);
  const cb = hexToRgb(b);
  if (!ca || !cb) return Number.MAX_SAFE_INTEGER;
  return (ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2 + (ca[2] - cb[2]) ** 2;
}

function trayOptions() {
  const plan = state.print && state.print.plan;
  const options = [{ value: -1, short: 'None', label: 'None', color: '', remain: -1 }];
  ((plan && plan.trays) || []).forEach((t) => {
    options.push({
      value: t.index,
      short: `T${t.tray_id + 1}`,
      label: `AMS${t.ams_id + 1} T${t.tray_id + 1} · ${t.type || '?'}`,
      color: t.color ? '#' + t.color : '',
      remain: t.remain
    });
  });
  if (plan && plan.external) {
    options.push({
      value: 255,
      short: 'EXT',
      label: `External spool · ${plan.external.type || '?'}`,
      color: plan.external.color ? '#' + plan.external.color : '',
      remain: -1
    });
  }
  return options;
}

function trayOptionFor(value) {
  const options = trayOptions();
  return options.find((o) => o.value === Number(value)) || options[0];
}

function autoMapFilament(filament) {
  const plan = state.print && state.print.plan;
  if (!plan) return -1;
  let best = -1;
  let bestScore = Number.MAX_SAFE_INTEGER;
  const consider = (index, type, color) => {
    const rgb = hexToRgb(color);
    if (!rgb || rgb[0] + rgb[1] + rgb[2] === 0) return;   // empty slot
    let score = colorDistance(filament.color, color);
    if (type && filament.type && type.toUpperCase() !== filament.type.toUpperCase()) score += 40000;
    if (score < bestScore) { bestScore = score; best = index; }
  };
  (plan.trays || []).forEach((t) => consider(t.index, t.type, t.color));
  if (plan.external) consider(255, plan.external.type, plan.external.color);
  return best;
}

function renderPrintDialog() {
  const { plan, plate } = state.print;
  if (!plan) return;

  // The Skip objects button always opens the modal; the modal itself reports
  // when the printer is not running a job, so it must not be greyed out here.
  $('print-sub').textContent = `${plan.name} · ${plan.path}`;

  // plate thumbnails
  $('print-plates').innerHTML = (plan.plates || []).map((p) => `
    <button data-print-plate="${p.index}"
      class="btn ${p.index === plate ? 'btn-primary' : 'btn-ghost'}">Plate ${p.index}</button>`).join('');

  // preview image
  const img = $('print-img');
  const loading = $('print-img-loading');
  loading.textContent = 'loading preview…';
  loading.classList.remove('hidden');
  img.classList.add('hidden');
  img.onload = () => { loading.classList.add('hidden'); img.classList.remove('hidden'); };
  img.onerror = () => { loading.textContent = 'no preview for this plate'; };
  img.src = `/api/printers/${state.print.printerId}/files/plate?path=${encodeURIComponent(plan.path)}&plate=${plate}&t=${Date.now()}`;

  // info
  const info = (plan.plates || []).find((p) => p.index === plate) || {};
  const mins = Math.round((info.time_sec || 0) / 60);
  // Say out loud that a selection armed before the start will be injected when
  // the print starts - it is sent with this request, not after the fact.
  const armed = armedSkipIds(state.print.printerId, plan.path);
  const armedRow = armed.length
    ? `<div class="flex justify-between gap-3"><span class="t-mut">Skipped on start</span>`
      + `<span class="text-right">${armed.length} object${armed.length === 1 ? '' : 's'} `
      + `will be skipped when this print starts</span></div>`
    : '';
  $('print-info').innerHTML = `
    <div class="flex justify-between"><span class="t-mut">Plate</span><span>${plate}</span></div>
    <div class="flex justify-between"><span class="t-mut">Est. time</span><span>${mins ? mins + ' min' : '—'}</span></div>
    <div class="flex justify-between"><span class="t-mut">Filament</span><span>${info.weight_g ? info.weight_g.toFixed(1) + ' g' : '—'}</span></div>${armedRow}`;

  // filaments + mapping: one row per filament, each opening a colour-aware tray
  // picker modal. A native <select> cannot show the tray colours, which is
  // exactly what the mapping decision needs.
  const filaments = info.filaments || [];
  $('print-filaments').innerHTML = filaments.length ? filaments.map((f) => {
    const auto = autoMapFilament(f);
    const chosen = state.print.mapping[f.id] !== undefined ? state.print.mapping[f.id] : auto;
    state.print.mapping[f.id] = chosen;
    const option = trayOptionFor(chosen);
    const best = chosen === auto && chosen !== -1;
    return `
      <div class="filament-map">
        <span class="filament-map-swatch" style="background:#${escapeHtml(f.color || '888888')}"></span>
        <span class="filament-map-text">
          <span class="filament-map-title">${escapeHtml(f.type || 'filament')} #${escapeHtml(String(f.id || 1))}</span>
          <span class="filament-map-sub">#${escapeHtml(f.color || '------')}${
            f.used_g ? ' · ' + f.used_g.toFixed(1) + ' g' : ''}</span>
        </span>
        <button type="button" class="filament-map-pick" data-action="pick-tray" data-filament="${f.id}"
                aria-label="Tray for ${escapeHtml(f.type || 'filament')} ${escapeHtml(String(f.id || 1))}">
          <span class="filament-map-swatch"${option.color ? ` style="background:${escapeHtml(option.color)}"` : ''}></span>
          <span class="min-w-0 flex-1">
            <span class="filament-map-pick-label">${escapeHtml(option.label)}</span>
            ${best ? '<span class="filament-map-pick-best">best match</span>' : ''}
          </span>
          <span class="t-accent">▸</span>
        </button>
      </div>`;
  }).join('') : '<p class="font-mono text-[10px] t-mut">No filament information in this project.</p>';

  // options. Honest copy: homing always happens at print start (the printer
  // and the file's own start G-code home the machine); these switches gate the
  // calibration routines only - no dashboard flag can suppress file G-code.
  $('print-options').innerHTML = `
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="use_ams" class="accent-bambu" checked> Use AMS mapping</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="bed_levelling" class="accent-bambu" checked> Bed levelling (auto bed leveling)</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="flow_cali" class="accent-bambu" checked> Flow calibration</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="vibration_cali" class="accent-bambu" checked> Vibration compensation</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="timelapse" class="accent-bambu" checked> Timelapse</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="layer_inspect" class="accent-bambu" checked> First-layer inspection</label>
    <p class="print-options-note">The printer always homes at print start — its start G-code commands it, so homing cannot be turned off here. These switches control the calibration routines (bed levelling, flow, vibration) and extras, not homing.</p>`;
}

function openTrayPicker(filamentId) {
  if (!state.print || !state.print.plan) return;
  state.trayPicker = { filamentId: Number(filamentId) };
  renderTrayPicker();
  $('modal-tray').classList.replace('hidden', 'flex');
}

function closeTrayPicker() {
  state.trayPicker = null;
  $('modal-tray').classList.replace('flex', 'hidden');
}

function renderTrayPicker() {
  const pick = state.trayPicker;
  if (!pick || !state.print || !state.print.plan) return;
  const plan = state.print.plan;
  const plateInfo = (plan.plates || []).find((pl) => pl.index === state.print.plate) || {};
  const filament = (plateInfo.filaments || []).find((f) => f.id === pick.filamentId);
  const auto = filament ? autoMapFilament(filament) : -1;
  const chosen = state.print.mapping[pick.filamentId] !== undefined
    ? state.print.mapping[pick.filamentId] : auto;

  $('tray-title').textContent = filament
    ? `Choose filament for ${filament.type || 'filament'} #${filament.id || 1}`
    : 'Choose filament';
  $('tray-sub').textContent = 'Tap a tray to map this slot. A blank swatch is an empty slot.';

  $('tray-options').innerHTML = trayOptions().map((o) => {
    const best = o.value === auto && o.value !== -1;
    const remain = (o.remain != null && o.remain >= 0) ? `${Math.round(o.remain)}% left` : '';
    return `<button type="button" class="tray-option${o.value === chosen ? ' is-active' : ''}"
              data-tray-option="${o.value}">
      <span class="tray-swatch"${o.color ? ` style="background:${escapeHtml(o.color)}"` : ''}></span>
      <span class="min-w-0 flex-1">
        <span class="tray-option-label">${escapeHtml(o.label)}</span>
        ${remain ? `<span class="tray-option-sub">${escapeHtml(remain)}</span>` : ''}
      </span>
      ${best ? '<span class="tray-best">best match</span>' : ''}
    </button>`;
  }).join('');
}

async function openPrintDialog(printerId, path) {
  const printer = printerById(printerId);
  state.print = { printerId, path, plan: null, plate: 1, mapping: {} };
  $('print-sub').textContent = `${printer ? printer.name : ''}`;
  $('print-info').textContent = 'reading the project…';
  $('print-filaments').innerHTML = '';
  $('print-plates').innerHTML = '';
  $('print-img').classList.add('hidden');
  $('print-img-loading').textContent = 'loading preview…';
  $('print-img-loading').classList.remove('hidden');
  $('modal-print').classList.replace('hidden', 'flex');
  try {
    const plan = await api(`/api/printers/${printerId}/files/plan?path=${encodeURIComponent(path)}`);
    state.print.plan = plan;
    state.print.plate = (plan.plates && plan.plates[0] && plan.plates[0].index) || 1;
    renderPrintDialog();
  } catch (err) {
    $('print-info').textContent = err.message;
    toast(err.message, 'error');
  }
}

function closePrintDialog() { $('modal-print').classList.replace('flex', 'hidden'); }

async function startPrintFromDialog() {
  const { printerId, plan, plate } = state.print;
  if (!plan) return;
  const opt = (name, fallback) => {
    const el = $('print-options').querySelector(`[data-opt="${name}"]`);
    return el ? el.checked : fallback;
  };
  const useAms = opt('use_ams', true);
  const info = (plan.plates || []).find((p) => p.index === plate) || {};
  const mapping = (info.filaments || []).map((f) => state.print.mapping[f.id] ?? -1);
  // A selection armed before the print started is sent WITH the print request,
  // so it is applied by the backend instead of racing the telemetry.
  const skipIds = armedSkipIds(printerId, plan.path);

  $('print-start').disabled = true;
  try {
    const res = await api(`/api/printers/${printerId}/print-remote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: plan.path,
        plate_index: plate,
        use_ams: useAms,
        ams_mapping: useAms && mapping.length ? mapping : null,
        bed_levelling: opt('bed_levelling', true),
        layer_inspect: opt('layer_inspect', true),
        flow_cali: opt('flow_cali', true),
        vibration_cali: opt('vibration_cali', true),
        timelapse: opt('timelapse', true),
        skip_object_ids: skipIds.length ? skipIds : null,
      })
    });
    const applied = Array.isArray(res.skipped) && res.skipped.length ? res.skipped : [];
    consumePreSkip(printerId, plan.path, applied.length > 0 || skipIds.length === 0);
    markSkipped(printerId, applied);
    state.printPath[printerId] = plan.path;
    toast(`${res.job} → ${res.printer}`
      + (applied.length ? ` (${applied.length} object${applied.length === 1 ? '' : 's'} skipped)` : ''));
    closePrintDialog();
    scheduleRender();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    $('print-start').disabled = false;
  }
}

/* ---------------------------------------------------------------- wiring */

function wireStatic() {
  // guard: wiring twice would attach every listener (and form handler) twice
  if (wireStatic.done) return;
  wireStatic.done = true;

  $('printer-select').addEventListener('change', (e) => selectPrinter(e.target.value));
  $('fleet-filter').addEventListener('input', (e) => { state.filter = e.target.value.trim(); renderFleet(); });
  $('add-printer').addEventListener('click', () => openModal());
  $('open-settings').addEventListener('click', openSettings);
  $('theme-toggle').addEventListener('click', toggleTheme);
  $('cam-toggle').addEventListener('click', () => setCameraEnabled(!state.cameraEnabled));
  $('print-close').addEventListener('click', closePrintDialog);
  $('print-cancel').addEventListener('click', closePrintDialog);
  $('print-start').addEventListener('click', startPrintFromDialog);
  $('print-skip').addEventListener('click', () => {
    if (!state.print.printerId) return;
    openSkipModal(state.print.printerId, { path: state.print.path, plate: state.print.plate });
  });
  $('skip-close').addEventListener('click', closeSkipModal);
  $('skip-cancel').addEventListener('click', closeSkipModal);
  $('skip-confirm').addEventListener('click', confirmSkip);
  $('skip-bed').addEventListener('click', (e) => {
    const el = e.target.closest('[data-skip-id]');
    if (el) toggleSkipObject(Number(el.dataset.skipId));
  });
  $('filament-cancel').addEventListener('click', closeFilamentEditor);
  $('filament-save').addEventListener('click', saveFilament);
  $('filament-type').addEventListener('change', updateFilamentPreset);
  $('print-plates').addEventListener('click', (e) => {
    const b = e.target.closest('[data-print-plate]');
    if (b) { state.print.plate = Number(b.dataset.printPlate); renderPrintDialog(); }
  });
  $('print-filaments').addEventListener('click', (e) => {
    const b = e.target.closest('[data-action="pick-tray"]');
    if (b) openTrayPicker(Number(b.dataset.filament));
  });
  $('tray-options').addEventListener('click', (e) => {
    const b = e.target.closest('[data-tray-option]');
    if (!b || !state.trayPicker) return;
    if (state.print) state.print.mapping[state.trayPicker.filamentId] = Number(b.dataset.trayOption);
    renderPrintDialog();
    closeTrayPicker();
  });
  $('tray-close').addEventListener('click', closeTrayPicker);
  $('settings-cancel').addEventListener('click', closeSettings);
  $('update-check').addEventListener('click', checkForUpdates);
  $('update-download').addEventListener('click', downloadUpdate);
  $('update-install').addEventListener('click', installUpdate);
  $('speed-cancel').addEventListener('click', closeSpeedModal);
  $('speed-options').addEventListener('click', (e) => {
    const b = e.target.closest('[data-speed]');
    if (b) pickSpeed(Number(b.dataset.speed));
  });
  $('settings-form').addEventListener('submit', saveSettings);
  $('modal-cancel').addEventListener('click', closeModal);
  $('printer-form').addEventListener('submit', savePrinter);

  // one open per-file menu at a time, and any outside click closes them
  document.addEventListener('click', (e) => closeFileMenus(e.target), true);

  // temperature sliders keep their number inputs in sync (delegated)
  document.addEventListener('input', (e) => {
    const role = e.target.dataset.role || '';
    if (role === 'files-search') { fileView.q = e.target.value; refreshFileView(e.target); return; }
    if (role.startsWith('fan-slider-')) {
      const out = document.querySelector(`[data-role="fan-value-${role.slice('fan-slider-'.length)}"]`);
      if (out) out.textContent = `${e.target.value}%`;
      return;
    }
    const tile = e.target.closest('.tile');
    if (!tile) return;
    if (role.startsWith('slider-')) {
      const input = tile.querySelector(`[data-role="input-${role.slice(7)}"]`);
      if (input) input.value = e.target.value;
    } else if (role.startsWith('input-')) {
      const slider = tile.querySelector(`[data-role="slider-${role.slice(6)}"]`);
      if (slider) slider.value = e.target.value;
    }
  });

  // SD upload from the system tab
  document.addEventListener('change', async (e) => {
    const role = e.target.dataset.role;

    // Files tab: sort + type filter
    if (role === 'files-sort' || role === 'files-filter') {
      fileView.set(role === 'files-sort' ? 'sort' : 'filter', e.target.value);
      refreshFileView(e.target);
      return;
    }

    // Temperature: a slider fires 'change' on release, so one command per drag
    if (role === 'slider-nozzle' || role === 'slider-bed' || role === 'slider-chamber') {
      const card = e.target.closest('[data-card-id]');
      const id = card && card.dataset.cardId;
      if (!id) return;
      const body = card.querySelector('[data-r="panel"]');
      const key = role.slice('slider-'.length);
      await sendTemps(id, { [key]: Number(e.target.value) || 0 }, body);
      return;
    }

    // Fans: a slider fires 'change' on release, so this is one command per drag
    if (role.startsWith('fan-slider-')) {
      const card = e.target.closest('[data-card-id]');
      const id = card && card.dataset.cardId;
      if (!id) return;
      const fan = role.slice('fan-slider-'.length);
      const speed = Number(e.target.value);
      const ok = await post(`/api/printers/${id}/fan`, { fan, speed });
      const printer = printerById(id);
      if (ok && printer) { setFanLocally(printer, fan, speed); scheduleRender(); }
      return;
    }

    if ((role !== 'sd-upload' && role !== 'folder-upload') || !e.target.files.length) return;
    const card = e.target.closest('[data-card-id]');
    if (!card) return;
    const printerId = card.dataset.cardId;
    const entry = cards.get(printerId);
    // the System-tab input targets the SD root; the Files-tab one targets the folder in view
    const dir = role === 'folder-upload' ? ((entry && entry.fileCache.path) || '/') : '/';
    const form = new FormData();
    form.append('file', e.target.files[0]);
    const file = e.target.files[0];
    try {
      const res = await fetch(
        `/api/printers/${printerId}/files/upload-dir?dir_path=${encodeURIComponent(dir)}`,
        { method: 'POST', body: form }
      );
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      toast(`${file.name} uploaded to ${dir}`);
      if (role === 'folder-upload' && entry) loadFiles(printerId, dir, true);
    } catch (err) { toast(err.message, 'error'); }
    e.target.value = '';
  });
}

/* ------------------------------------------------------------------ boot */

window.addEventListener('DOMContentLoaded', () => {
  wireStatic();
  restoreSelection();
  fetch('/VERSION').then((r) => (r.ok ? r.text() : null))
    .then((v) => { if (v) $('app-version').textContent = v.trim(); })
    .catch(() => {});
  initTheme();
  initCameraPref();
  loadSettings().then(() => { loadFleet(); });
  loadFilaments();
  initWebSocket();
  loadStaged();
  setInterval(loadFleet, 60000);
});