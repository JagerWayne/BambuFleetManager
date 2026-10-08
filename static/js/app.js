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
  speedModalPrinter: null,
  cameraEnabled: true,
  print: { printerId: null, path: null, plan: null, plate: 1, mapping: {} },
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

const isLightOn = (p) => (p.lights && p.lights.chamber_light
  ? p.lights.chamber_light === 'on'
  : Boolean(p.light));

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
        lights: null, light: false, printError: 0, homed: null, seenAt: null
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

  if (!visible.length) {
    cards.forEach((entry) => entry.root.remove());
    cards.clear();
    grid.innerHTML = `<div class="card col-span-full p-12 text-center">
      <p class="text-sm font-bold t-body">${state.fleet.length ? 'No node matches that filter' : 'No printer nodes yet'}</p>
      <p class="mt-1 text-xs t-mut">${state.fleet.length ? 'Clear the search box to see everything.' : 'Register a node with its LAN IP, serial number and access code.'}</p>
    </div>`;
    return;
  }
  const empty = grid.querySelector(':scope > .col-span-full');
  if (empty) empty.remove();

  const keep = new Set(visible.map((p) => p.id));
  cards.forEach((entry, id) => {
    if (!keep.has(id)) { entry.destroy(); entry.root.remove(); cards.delete(id); }
  });

  visible.forEach((printer, index) => {
    let entry = cards.get(printer.id);
    if (!entry) { entry = createCard(printer); cards.set(printer.id, entry); }
    updateCard(printer, entry);
    const sibling = grid.children[index];
    if (sibling !== entry.root) grid.insertBefore(entry.root, sibling || null);
  });
}

/* --------------------------------------------------------------- card DOM */

function createCard(printer) {
  const root = document.createElement('article');
  root.dataset.cardId = printer.id;
  root.className = 'card card-enter overflow-hidden lg:h-[540px] 2xl:h-[600px]';

  root.innerHTML = `
    <div class="flex h-full flex-col lg:flex-row">

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
        </div>

        <div class="flex shrink-0 items-center gap-1">
          <button class="icon-btn" data-action="cam-restart" title="Restart stream">⟳</button>
          <button class="icon-btn" data-action="cam-stop" title="Stop stream">■</button>
          <button class="icon-btn" data-action="cam-snapshot" title="Save snapshot">◉</button>
          <button class="icon-btn" data-action="cam-fullscreen" title="Fullscreen">⛶</button>
          <span data-role="cam-status" class="ml-auto font-mono text-[9px] t-mut"></span>
        </div>

        <div class="grid shrink-0 grid-cols-2 gap-2">
          <button class="tile p-2 text-left transition hover:line" data-action="open-temp" title="Set temperatures">
            <span class="block text-[9px] uppercase tracking-wider t-mut">Nozzle</span>
            <span class="font-mono text-base font-bold leading-tight" data-r="nozzle"></span>
            <span class="block font-mono text-[10px] t-mut" data-r="nozzleTarget"></span>
          </button>
          <button class="tile p-2 text-left transition hover:line" data-action="open-temp" title="Set temperatures">
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

        <div class="grid shrink-0 grid-cols-3 gap-1.5" data-r="quick"></div>

        <div class="hidden shrink-0 space-y-0.5 pt-1 font-mono text-[10px]" data-r="alerts"></div>

        <div class="mt-auto space-y-0.5 pt-1 font-mono text-[9px] t-mut">
          <div data-role="cam-line-1" class="truncate"></div>
          <div data-role="cam-line-2" class="truncate"></div>
        </div>
      </div>

      <!-- right: identity + collapsible tabs -->
      <div class="flex min-w-0 flex-1 flex-col">

        <div class="flex items-start justify-between gap-3 px-4 pt-3 pb-2">
          <div class="min-w-0">
            <div class="flex items-center gap-2">
              <span class="h-2 w-2 shrink-0 rounded-full" data-r="dot"></span>
              <h3 class="truncate text-[15px] font-bold t-strong" data-r="name"></h3>
              <span class="chip" data-r="chip"></span>
              <span class="chip hidden" data-r="homing"></span>
            </div>
            <div class="mt-0.5 truncate font-mono text-[10px] t-mut" data-r="meta"></div>
          </div>
          <div class="flex shrink-0 items-center gap-1.5">
            <button class="icon-btn" data-act="light" title="Toggle chamber light">💡</button>
            <button class="icon-btn" data-act="edit" title="Edit name / IP / access code">✎</button>
            <button class="icon-btn" data-act="panel" title="Show / hide controls">▾</button>
            <button class="icon-btn" data-act="menu" title="Remove node">⋯</button>
          </div>
        </div>

        <div class="flex items-center gap-1 overflow-x-auto border-b line px-4 pb-2" data-r="tabbar"></div>

        <div class="min-h-0 flex-1 overflow-y-auto p-3" data-r="panel"></div>
      </div>
    </div>
  `;

  const refs = {};
  root.querySelectorAll('[data-r]').forEach((n) => { refs[n.dataset.r] = n; });

  refs.tabbar.innerHTML = TABS.map(([key, label]) =>
    `<button class="tab" data-tab="${key}" aria-selected="false">${label}</button>`).join('');

  const entry = {
    root, refs, tab: null, lastTab: 'control', jogStep: 1, camera: null,
    fileCache: { path: '/', listing: null, loading: false, error: null },
    camRefs: {
      img: root.querySelector('[data-role="cam-img"]'),
      overlay: root.querySelector('[data-role="cam-overlay"]'),
      status: root.querySelector('[data-role="cam-status"]'),
      box: root.querySelector('[data-role="cam-box"]'),
      snap: root.querySelector('[data-role="cam-snap"]'),
      line1: root.querySelector('[data-role="cam-line-1"]'),
      line2: root.querySelector('[data-role="cam-line-2"]')
    },
    destroy() { stopCamera(this); if (this.observer) this.observer.disconnect(); }
  };

  function selectTab(key) {
    entry.tab = (entry.tab === key) ? null : key;
    if (entry.tab) entry.lastTab = entry.tab;
    renderPanel(printerById(printer.id), entry, true);
  }

  refs.tabbar.addEventListener('click', (e) => {
    const b = e.target.closest('[data-tab]');
    if (b) selectTab(b.dataset.tab);
  });

  refs.quick.addEventListener('click', (e) => {
    const b = e.target.closest('[data-cmd]');
    if (b) quickControl(printer.id, b.dataset.cmd);
  });

  root.querySelector('[data-act="light"]').addEventListener('click', (e) => {
    e.stopPropagation(); toggleLight(printer.id);
  });
  root.querySelector('[data-act="edit"]').addEventListener('click', (e) => {
    e.stopPropagation(); openModal(printerById(printer.id));
  });
  root.querySelector('[data-act="panel"]').addEventListener('click', (e) => {
    e.stopPropagation();
    entry.tab = entry.tab ? null : (entry.lastTab || 'control');
    renderPanel(printerById(printer.id), entry, true);
  });
  root.querySelector('[data-act="menu"]').addEventListener('click', (e) => {
    e.stopPropagation(); removePrinter(printer.id);
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
        if (e.isIntersecting && state.cameraEnabled) startCamera(printer, entry);
        else if (!e.isIntersecting) stopCamera(entry);
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

  const quick = printing
    ? `<button class="btn btn-warn" data-cmd="pause">Pause</button>
       <button class="btn btn-danger" data-cmd="stop">Stop</button>
       <button class="btn btn-ghost" data-cmd="retry">Recover</button>`
    : `<button class="btn" data-cmd="resume">Resume</button>
       <button class="btn btn-danger" data-cmd="stop">Stop</button>
       <button class="btn btn-ghost" data-cmd="retry">Recover</button>`;
  if (refs.quick.dataset.sig !== quick) { refs.quick.innerHTML = quick; refs.quick.dataset.sig = quick; }

  entry.root.querySelector('[data-act="light"]').textContent = isLightOn(printer) ? '💡' : '🌑';
  entry.root.querySelector('[data-act="panel"]').textContent = entry.tab ? '▴' : '▾';

  if (entry.camRefs) {
    entry.camRefs.line1.textContent = `${printer.ip} · ${printer.sn}`;
    entry.camRefs.line2.textContent = (printer.ipcam && printer.ipcam.resolution)
      ? `${printer.ipcam.resolution} · ${printer.ipcam.ipcam_record || ''}` : '';
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
  ['temperature', 'Temperatures'],
  ['files', 'SD card'],
  ['ams', 'AMS'],
  ['system', 'System']
];

function renderPanel(printer, entry, force = false) {
  if (!printer || !entry.refs.panel) return;
  const body = entry.refs.panel;

  entry.refs.tabbar.querySelectorAll('[data-tab]').forEach((b) =>
    b.setAttribute('aria-selected', String(b.dataset.tab === entry.tab)));

  if (!entry.tab) {
    if (body.dataset.built !== 'none') {
      body.dataset.built = 'none';
      body.innerHTML = `<div class="grid h-full place-items-center px-6 py-4 text-center font-mono text-[11px] t-dim">
        Pick a tab above for controls, temperatures, SD card, AMS or system.
      </div>`;
    }
    return;
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
}

/* --------------------------------------------------------------- control */

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

function controlHtml(p) {
  const homed = p.homed === true;
  const printing = p.status === 'running';

  const speeds = [1, 2, 3, 4]
    .map((l) => choice('speed', l, SPEED_LABELS[l], `${SPEED_PERCENT[l]}%`, '', (p.speedLevel || 0) === l))
    .join('');

  const lightRow = (label, node, modes, liveKey) => `
    <div class="flex items-center justify-between gap-2">
      <div class="min-w-0">
        <div class="text-[11px] font-bold t-body">${label}</div>
        <div class="font-mono text-[10px] t-mut">reported:
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
    ${lightRow('Chamber light', 'chamber_light', [
      { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }, { value: 'flashing', label: 'Flash' }
    ], 'light-chamber')}
    <div class="h-px surface-3"></div>
    ${lightRow('Work light', 'work_light', [
      { value: 'on', label: 'On' }, { value: 'off', label: 'Off' }
    ], 'light-work')}`;

  const skipSection = `
    <div class="grid grid-cols-4 gap-2 sm:grid-cols-8">
      ${[0, 1, 2, 3, 4, 5, 6, 7].map((i) => choice('skip', i, String(i + 1), '')).join('')}
    </div>
    <p class="font-mono text-[10px] t-mut">
      Drops one part from the plate and keeps printing the rest — only meaningful during a job.
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
        <div class="flex flex-wrap gap-1.5">
          ${btn('Pause', 'pause', { cls: 'btn-warn' })}
          ${btn('Resume', 'resume', { cls: 'btn-primary' })}
          ${btn('Stop', 'stop', { cls: 'btn-danger' })}
          ${btn('Recover', 'retry', { cls: 'btn-ghost' })}
          ${btn('Clear error', 'clear-error', { cls: 'btn-ghost' })}
        </div>
        <p class="font-mono text-[10px] t-mut">State: <span data-live="status" class="t-body">${escapeHtml(p.status || 'unknown')}</span></p>
        ${p.status === 'failed' ? '<p class="font-mono text-[10px] t-warn">The last job failed. Press <b>Clear error</b>, then dismiss the message on the printer screen — a printer in FAILED refuses new jobs.</p>' : ''}`)}


      ${section('Lighting', lightSection, 'xl:col-span-2')}

      ${section('Skip object', skipSection)}

      ${section('Calibration · moves the machine', calibration, 'border-warn')}
    </div>`;
}

/* ------------------------------------------------------------------- jog */

function axisRow(label, axis, step, feed) {
  return `
    <div class="flex items-center justify-between gap-3">
      <span class="w-8 font-mono text-base font-bold t-body">${label}</span>
      <div class="flex gap-1.5">
        <button class="btn" data-action="jog" data-axis="${axis}" data-dir="-1" data-feed="${feed}">
          − ${step} mm
        </button>
        <button class="btn" data-action="jog" data-axis="${axis}" data-dir="1" data-feed="${feed}">
          + ${step} mm
        </button>
      </div>
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
        <div class="space-y-2">
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
        <label class="flex items-center gap-2 font-mono text-[11px] t-body">
          <input type="checkbox" data-role="confirm-thermal" class="accent-bambu">
          Confirm: apply these setpoints
        </label>
        <div class="flex gap-1.5">
          <button class="btn btn-ghost" data-action="cooldown">All to 0°</button>
          <button class="btn btn-primary" data-action="apply-temps">Apply targets</button>
        </div>
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

function filesHtml(cache) {
  const parts = cache.path.split('/').filter(Boolean);
  let acc = '';
  const crumbs = [`<button class="hover:t-accent" data-action="cd" data-path="/">SD root</button>`];
  parts.forEach((part) => {
    acc += `/${part}`;
    crumbs.push(`<span class="t-dim">/</span><button class="hover:t-accent" data-action="cd" data-path="${escapeHtml(acc)}">${escapeHtml(part)}</button>`);
  });

  let body = '';
  if (cache.loading) {
    body = `<p class="p-4 text-center font-mono text-[11px] t-mut">Reading SD card over FTPS…</p>`;
  } else if (cache.error) {
    body = `<p class="p-4 text-center font-mono text-[11px] t-danger">${escapeHtml(cache.error)}</p>`;
  } else if (cache.listing && !cache.listing.length) {
    body = `<p class="p-4 text-center font-mono text-[11px] t-mut">This folder is empty.</p>`;
  } else if (cache.listing) {
    body = cache.listing.map((f) => `
      <div class="rounded-xl border line surface-2 p-2">
        <div class="flex items-center gap-2">
          <button class="flex min-w-0 flex-1 items-center gap-2.5 text-left"
                  data-action="cd" data-path="${escapeHtml(f.path)}" data-dir="${f.is_dir ? 1 : 0}">
            <span class="grid h-8 w-8 shrink-0 place-items-center rounded-lg border line-soft ${
              f.is_dir ? 'bg-accent-soft t-accent' : 'surface-3 t-mut'}">
              ${f.is_dir ? '▣' : '▤'}
            </span>
            <span class="min-w-0">
              <span class="block truncate font-mono text-[12px] ${
                f.is_dir ? 'font-bold t-strong' : 't-body'}">${escapeHtml(f.name)}</span>
              <span class="block truncate font-mono text-[10px] t-mut">
                ${f.is_dir ? 'folder' : formatBytes(f.size)}${f.modified ? ' · ' + formatDate(f.modified) : ''}
              </span>
            </span>
          </button>
          <div class="flex shrink-0 items-center gap-1">
            ${f.is_printable ? `<button class="btn btn-primary" data-action="print-sd" data-path="${escapeHtml(f.path)}">Print</button>` : ''}
            ${f.is_dir ? '' : `<button class="btn btn-ghost" data-action="download-sd" data-path="${escapeHtml(f.path)}" title="Download to this computer">Get</button>`}
            <button class="btn btn-danger" data-action="delete-sd" data-path="${escapeHtml(f.path)}" data-dir="${f.is_dir ? 1 : 0}" title="Delete">Del</button>
          </div>
        </div>
      </div>`).join('');
  }

  const count = cache.listing ? cache.listing.length : 0;

  return `
    <div class="flex flex-col gap-2">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex min-w-0 items-center gap-1 overflow-x-auto font-mono text-[11px] t-mut">${crumbs.join('')}</div>
        <div class="flex shrink-0 items-center gap-1.5">
          <label class="btn btn-ghost" title="Upload a project into this folder">
            Upload
            <input type="file" accept=".3mf,.gcode" class="hidden" data-role="folder-upload">
          </label>
          <button class="btn btn-ghost" data-action="refresh-files">Refresh</button>
        </div>
      </div>
      ${cache.listing ? `<p class="font-mono text-[10px] t-dim">${count} item${count === 1 ? '' : 's'}</p>` : ''}
      <div class="max-h-[46vh] space-y-1.5 overflow-y-auto lg:max-h-[340px]" data-role="file-list">${body}</div>
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

function startCamera(printer, entry) {
  const refs = entry.camRefs;
  if (!refs || !printer || entry.camera) return;
  const img = refs.img;

  // Reveal the image immediately - browsers do not reliably fire 'load' for a
  // multipart/x-mixed-replace stream, so visibility must not depend on it.
  refs.overlay.classList.add('hidden');
  img.classList.remove('hidden');
  refs.status.textContent = 'connecting…';

  img.onload = () => { refs.status.textContent = 'live (MJPEG)'; };
  img.onerror = () => {
    refs.status.textContent = 'stream failed';
    refs.overlay.textContent = 'Could not open the live stream. Check the printer is on and ' +
      'reachable, then press Start again — or use the RTSPS URL below in VLC.';
    refs.overlay.classList.remove('hidden');
    stopCamera(entry);
  };

  img.src = `/api/printers/${printer.id}/camera/mjpeg?width=1280&fps=12&t=${Date.now()}`;
  entry.camera = { img };
}

function stopCamera(entry) {
  if (!entry || !entry.camera) return;
  const { img } = entry.camera;
  try { img.onload = null; img.onerror = null; img.src = ''; } catch (_) { /* ignore */ }
  img.classList.add('hidden');
  if (entry.camRefs) {
    entry.camRefs.overlay.textContent = 'Press Start to open the live view.';
    entry.camRefs.overlay.classList.remove('hidden');
    entry.camRefs.status.textContent = 'stopped';
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
    case 'skip':
      await post(`/api/printers/${printerId}/skip-objects`, { object_ids: [Number(el.dataset.value)] });
      success('skip sent'); break;
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
      setTempInputs(body, {
        nozzle: Number(el.dataset.nozzle), bed: Number(el.dataset.bed), chamber: Number(el.dataset.chamber)
      });
      toast('Preset filled — press Apply'); break;
    }
    case 'cooldown':
      setTempInputs(el.closest('[data-r="panel"]'), { nozzle: 0, bed: 0, chamber: 0 });
      toast('Cool-down filled — press Apply'); break;
    case 'apply-temps': {
      const body = el.closest('[data-r="panel"]');
      const confirm = body.querySelector('[data-role="confirm-thermal"]');
      if (!confirm || !confirm.checked) { toast('Tick "Confirm: apply these setpoints" first', 'warn'); return; }
      const allow = body.querySelector('[data-role="allow-while-printing"]');
      const payload = {
        nozzle: readTemp(body, 'nozzle'), bed: readTemp(body, 'bed'),
        confirm_thermal: true, allow_while_printing: Boolean(allow && allow.checked)
      };
      // only send chamber when the tile is present
      if (body.querySelector('[data-role="input-chamber"]')) payload.chamber = readTemp(body, 'chamber');
      try {
        await api(`/api/printers/${printerId}/temperature`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
        });
        success('setpoints applied');
      } catch (err) { toast(err.message, 'error'); }
      break;
    }
    case 'refresh-files': loadFiles(printerId, entry.fileCache.path, true); break;
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
    case 'cam-restart':
      stopCamera(entry);
      setTimeout(() => startCamera(printerById(printerId), entry), 200);
      break;
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
          <button class="btn btn-ghost" data-action="refresh">Full state dump</button>
          <button class="btn btn-danger" data-action="reboot">Reboot printer</button>
        </div>
      </div>
      <div>
        <p class="mb-1.5 text-[10px] uppercase tracking-wider t-mut">Push a project to the SD root</p>
        <input type="file" accept=".3mf,.gcode" data-role="sd-upload"
               class="w-full font-mono text-[11px] t-mut file:mr-2 file:rounded-lg file:border-0 file:surface-3 file:px-2.5 file:py-1.5 file:font-mono file:text-[11px] file:t-accent">
      </div>
    </div>`;
}

/* ---------------------------------------------------------------- SD load */

async function renderFiles(printer, body) {
  const entry = cards.get(printer.id);
  if (!entry) return;
  const cache = entry.fileCache;
  const sig = `files:${cache.path}:${cache.loading}:${cache.error || ''}:` +
    (cache.listing || []).map((f) => `${f.path}:${f.size}`).join('|');
  if (body.dataset.sig === sig) return;
  body.dataset.sig = sig;
  body.innerHTML = filesHtml(cache);
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

/* ------------------------------------------------------------ dispatch */

async function dispatchToPrinter(printerId, filename) {
  const entry = cards.get(printerId);
  if (entry) entry.root.classList.add('opacity-60');
  try {
    const res = await api('/api/dispatch-print', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        printer_id: printerId, filename, plate_index: 1,
        bed_levelling: true, flow_cali: true, vibration_cali: true,
        timelapse: true, use_ams: true
      })
    });
    toast(`${res.job} → ${res.printer}`);
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    if (entry) entry.root.classList.remove('opacity-60');
    state.dragFile = null;
  }
}

/* ------------------------------------------------------------- quick bits */

async function quickControl(id, cmd) {
  if (cmd === 'retry') return void (await post(`/api/printers/${id}/retry`, {}));
  if (cmd === 'stop' && !window.confirm('Stop the current print? This aborts the job.')) return;
  await post(`/api/printers/${id}/${cmd}`, {});
}

async function setProfile(id, level) {
  const ok = await post(`/api/printers/${id}/speed`, { speed_level: level });
  if (ok) { const p = printerById(id); if (p) p.speedLevel = level; scheduleRender(); }
}

async function toggleLight(id) {
  const printer = printerById(id);
  if (!printer) return;
  const next = !isLightOn(printer);
  const ok = await post(`/api/printers/${id}/light`, { state: next });
  if (ok) {
    printer.light = next;
    printer.lights = Object.assign({}, printer.lights || {}, { chamber_light: next ? 'on' : 'off' });
    scheduleRender();
  }
}

/* ------------------------------------------------------- staging + drop */

function renderStaged() {
  const box = $('staged-container');
  $('staged-count').textContent = `${state.staged.length} file${state.staged.length === 1 ? '' : 's'}`;
  if (!state.staged.length) {
    box.innerHTML = '<div class="grid h-full place-items-center font-mono text-[11px] t-dim">Nothing staged yet</div>';
    return;
  }
  box.innerHTML = state.staged.map((fn) => `
    <div draggable="true" data-file="${escapeHtml(fn)}"
         class="flex cursor-grab items-center justify-between gap-2 rounded-xl border line surface-2 px-3 py-2 active:cursor-grabbing">
      <span class="min-w-0 truncate font-mono text-[11px] t-strong">${escapeHtml(fn)}</span>
      <span class="flex shrink-0 items-center gap-2">
        <span class="font-mono text-[10px] font-bold t-accent">DRAG →</span>
        <button data-remove="${escapeHtml(fn)}" class="t-mut hover:t-danger" title="Remove from staging">✕</button>
      </span>
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

async function removePrinter(id) {
  const printer = printerById(id);
  if (!window.confirm(`Remove ${printer ? printer.name : 'this node'} from the fleet?`)) return;
  try {
    await api(`/api/printers/${id}`, { method: 'DELETE' });
    state.fleet = state.fleet.filter((p) => p.id !== id);
    toast('node removed');
    scheduleRender();
  } catch (err) { toast(err.message, 'error'); }
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
    $('update-install').classList.add('hidden');
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
  $('update-install').classList.add('hidden');
  try {
    const r = await api('/api/update');
    if (r.current) $('update-current').textContent = `v${r.current}`;
    if (r.update_available) {
      const size = r.asset_size ? ` (${formatBytes(r.asset_size)})` : '';
      setUpdateResult(`Version ${r.latest} is available${size}.`, 'ok');
      const install = $('update-install');
      install.dataset.version = r.latest;
      install.textContent = `Download & install ${r.latest}`;
      install.classList.remove('hidden');
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

async function installUpdate() {
  const install = $('update-install');
  const version = install.dataset.version || 'the new version';
  if (!window.confirm(`Download and install ${version}?\n\nThe app will close and restart when it's done.`)) return;
  install.disabled = true;
  install.textContent = 'Downloading…';
  try {
    await api('/api/update/install', { method: 'POST' });
    setUpdateResult('Installing… the app will restart in a moment.', 'ok');
    install.textContent = 'Installing…';
  } catch (err) {
    setUpdateResult(err.message, 'error');
    install.disabled = false;
    install.textContent = 'Download & install';
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
  const options = [{ value: -1, short: 'None', label: 'None', color: '' }];
  ((plan && plan.trays) || []).forEach((t) => {
    options.push({
      value: t.index,
      short: `T${t.tray_id + 1}`,
      label: `AMS${t.ams_id + 1} T${t.tray_id + 1} · ${t.type || '?'}`,
      color: t.color ? '#' + t.color : ''
    });
  });
  if (plan && plan.external) {
    options.push({
      value: 255,
      short: 'EXT',
      label: `External spool · ${plan.external.type || '?'}`,
      color: plan.external.color ? '#' + plan.external.color : ''
    });
  }
  return options;
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
  $('print-info').innerHTML = `
    <div class="flex justify-between"><span class="t-mut">Plate</span><span>${plate}</span></div>
    <div class="flex justify-between"><span class="t-mut">Est. time</span><span>${mins ? mins + ' min' : '—'}</span></div>
    <div class="flex justify-between"><span class="t-mut">Filament</span><span>${info.weight_g ? info.weight_g.toFixed(1) + ' g' : '—'}</span></div>`;

  // filaments + mapping (colour chips, auto-mapped to the closest tray)
  const options = trayOptions();
  const filaments = info.filaments || [];
  $('print-filaments').innerHTML = filaments.length ? filaments.map((f) => {
    const chosen = state.print.mapping[f.id] !== undefined ? state.print.mapping[f.id] : autoMapFilament(f);
    state.print.mapping[f.id] = chosen;
    const chips = options.map((o) => `
      <button class="btn ${o.value === chosen ? 'btn-primary' : 'btn-ghost'}"
              data-map-filament="${f.id}" data-map-index="${o.value}" title="${escapeHtml(o.label)}">
        <span class="h-3 w-3 shrink-0 rounded-full border line"
              style="background:${o.color ? escapeHtml(o.color) : 'transparent'}"></span>
        ${escapeHtml(o.short)}
      </button>`).join('');
    return `
      <div class="tile p-2">
        <div class="flex items-center gap-2">
          <span class="h-7 w-7 shrink-0 rounded-lg border line"
                style="background:#${escapeHtml(f.color || '888888')}"></span>
          <span class="min-w-0 flex-1">
            <span class="block truncate font-mono text-[12px] font-bold t-strong">
              ${escapeHtml(f.type || 'filament')} #${escapeHtml(String(f.id || 1))}</span>
            <span class="block font-mono text-[10px] t-mut">
              #${escapeHtml(f.color || '------')}${f.used_g ? ' · ' + f.used_g.toFixed(1) + ' g' : ''}</span>
          </span>
        </div>
        <div class="mt-2 flex flex-wrap gap-1.5">${chips}</div>
      </div>`;
  }).join('') : '<p class="font-mono text-[10px] t-mut">No filament information in this project.</p>';

  $('print-filaments').querySelectorAll('[data-map-filament]').forEach((chip) => {
    chip.addEventListener('click', () => {
      state.print.mapping[Number(chip.dataset.mapFilament)] = Number(chip.dataset.mapIndex);
      renderPrintDialog();
    });
  });

  // options
  $('print-options').innerHTML = `
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="use_ams" class="accent-bambu" checked> Use AMS mapping</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="bed_levelling" class="accent-bambu" checked> Bed levelling</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="flow_cali" class="accent-bambu" checked> Flow calibration</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="vibration_cali" class="accent-bambu" checked> Vibration calibration</label>
    <label class="flex items-center gap-2"><input type="checkbox" data-opt="timelapse" class="accent-bambu" checked> Timelapse</label>`;
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
        flow_cali: opt('flow_cali', true),
        vibration_cali: opt('vibration_cali', true),
        timelapse: opt('timelapse', true),
      })
    });
    toast(`${res.job} → ${res.printer}`);
    closePrintDialog();
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    $('print-start').disabled = false;
  }
}

/* ---------------------------------------------------------------- wiring */

function wireStatic() {
  const drop = $('drop-zone');
  drop.addEventListener('click', () => $('file-input').click());
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('drag-target-hover'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-target-hover'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); drop.classList.remove('drag-target-hover');
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
  $('file-input').addEventListener('change', (e) => {
    if (e.target.files.length) uploadFiles(e.target.files);
    e.target.value = '';
  });

  $('fleet-filter').addEventListener('input', (e) => { state.filter = e.target.value.trim(); renderFleet(); });
  $('add-printer').addEventListener('click', () => openModal());
  $('open-settings').addEventListener('click', openSettings);
  $('theme-toggle').addEventListener('click', toggleTheme);
  $('cam-toggle').addEventListener('click', () => setCameraEnabled(!state.cameraEnabled));
  $('print-close').addEventListener('click', closePrintDialog);
  $('print-cancel').addEventListener('click', closePrintDialog);
  $('print-start').addEventListener('click', startPrintFromDialog);
  $('filament-cancel').addEventListener('click', closeFilamentEditor);
  $('filament-save').addEventListener('click', saveFilament);
  $('filament-type').addEventListener('change', updateFilamentPreset);
  $('print-plates').addEventListener('click', (e) => {
    const b = e.target.closest('[data-print-plate]');
    if (b) { state.print.plate = Number(b.dataset.printPlate); renderPrintDialog(); }
  });
  $('settings-cancel').addEventListener('click', closeSettings);
  $('update-check').addEventListener('click', checkForUpdates);
  $('update-install').addEventListener('click', installUpdate);
  $('speed-cancel').addEventListener('click', closeSpeedModal);
  $('speed-options').addEventListener('click', (e) => {
    const b = e.target.closest('[data-speed]');
    if (b) pickSpeed(Number(b.dataset.speed));
  });
  $('settings-form').addEventListener('submit', saveSettings);
  $('modal-cancel').addEventListener('click', closeModal);
  $('printer-form').addEventListener('submit', savePrinter);
  $('collapse-all').addEventListener('click', () => {
    const anyOpen = [...cards.values()].some((e) => e.tab);
    cards.forEach((entry) => {
      entry.tab = anyOpen ? null : (entry.lastTab || 'control');
      renderPanel(printerById(entry.root.dataset.cardId), entry, true);
    });
    $('collapse-all').textContent = anyOpen ? 'Expand' : 'Collapse';
  });

  $('staged-container').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-remove]');
    if (btn) { e.preventDefault(); removeStaged(btn.dataset.remove); }
  });
  $('staged-container').addEventListener('dragstart', (e) => {
    const item = e.target.closest('[data-file]');
    if (!item) return;
    state.dragFile = item.dataset.file;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', state.dragFile);
  });

  // temperature sliders keep their number inputs in sync (delegated)
  document.addEventListener('input', (e) => {
    const role = e.target.dataset.role || '';
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