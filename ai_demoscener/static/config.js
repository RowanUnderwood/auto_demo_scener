'use strict';

let currentCfg = {};
let promptRows = [];
let promptStats = {};

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  await loadPrompts();
  await loadArchive();
  refreshLog();
  setInterval(refreshLog, 5000);
  connectStatusWS();
});

// ── Config load/save ──────────────────────────────────────────────────────────
async function loadConfig() {
  const r = await fetch('/api/config');
  currentCfg = await r.json();
  populateForm(currentCfg);
}

function populateForm(c) {
  // Mode
  setMode(c.boot_mode || 'generate', false);
  set('wCreative', c.generate_mode_weights?.creative ?? 1, 'wCreativeV');
  set('wUpdate',   c.generate_mode_weights?.update   ?? 1, 'wUpdateV');
  set('wStock',    c.generate_mode_weights?.stock    ?? 2, 'wStockV');

  // LM Studio
  setVal('lmUrl',       c.lm_studio?.base_url ?? '');
  setVal('lmMaxTokens', c.lm_studio?.max_tokens ?? 8192);
  setVal('lmTimeout',   c.lm_studio?.request_timeout_seconds ?? 180);
  setVal('lmCeiling',   c.lm_studio?.context_ceiling_tokens ?? 16384);
  set('lmTemp', c.lm_studio?.temperature ?? 0.9, 'lmTempV');
  // Model dropdown gets populated by refreshModels() if called
  populateModelDropdown([c.lm_studio?.model ?? ''], c.lm_studio?.model ?? '');

  // Display
  setVal('demoRuntime',        c.display?.demo_runtime_seconds ?? 60);
  setVal('minTyping',          c.display?.min_typing_seconds   ?? 8);
  setVal('displayCharsPerSec', c.display?.display_chars_per_sec ?? 400);
  setVal('palette',            c.display?.palette ?? 'amber_crt');
  setChecked('showStatusBar',  c.display?.show_status_bar  ?? true);
  setChecked('showRetry',      c.display?.show_retry_messages ?? true);

  // Validation
  setVal('maxRepair',    c.validation?.max_repair_attempts    ?? 2);
  setVal('auditionSecs', c.validation?.audition_seconds       ?? 5);
  setVal('blankSecs',    c.validation?.blank_detection_seconds ?? 4);
  setVal('minFps',       c.validation?.min_fps                ?? 5);

  // Archive
  setVal('archiveDir', c.archive?.directory ?? 'archive');
  setVal('maxFiles',   c.archive?.max_files ?? 500);

  // System prompts
  setVal('promptStock',    c.system_prompts?.stock    ?? '');
  setVal('promptCreative', c.system_prompts?.creative ?? '');
  setVal('promptUpdate',   c.system_prompts?.update   ?? '');
  setVal('promptRepair',   c.system_prompts?.repair   ?? '');
}

// ── Section save ──────────────────────────────────────────────────────────────
async function saveSection(section) {
  const cfg = deepClone(currentCfg);

  switch (section) {
    case 'mode':
      cfg.boot_mode = document.querySelector('.mode-btn.active')?.id === 'modeReplay' ? 'replay' : 'generate';
      cfg.generate_mode_weights = {
        creative: +getVal('wCreative'),
        update:   +getVal('wUpdate'),
        stock:    +getVal('wStock'),
      };
      break;
    case 'lmStudio':
      cfg.lm_studio.base_url                 = getVal('lmUrl');
      cfg.lm_studio.model                    = getVal('lmModel');
      cfg.lm_studio.max_tokens               = +getVal('lmMaxTokens');
      cfg.lm_studio.temperature              = +getVal('lmTemp');
      cfg.lm_studio.request_timeout_seconds  = +getVal('lmTimeout');
      cfg.lm_studio.context_ceiling_tokens   = +getVal('lmCeiling');
      break;
    case 'display':
      cfg.display.demo_runtime_seconds  = +getVal('demoRuntime');
      cfg.display.min_typing_seconds    = +getVal('minTyping');
      cfg.display.display_chars_per_sec = +getVal('displayCharsPerSec');
      cfg.display.palette               = getVal('palette');
      cfg.display.show_status_bar       = getChecked('showStatusBar');
      cfg.display.show_retry_messages   = getChecked('showRetry');
      break;
    case 'validation':
      cfg.validation.max_repair_attempts    = +getVal('maxRepair');
      cfg.validation.audition_seconds       = +getVal('auditionSecs');
      cfg.validation.blank_detection_seconds = +getVal('blankSecs');
      cfg.validation.min_fps                = +getVal('minFps');
      break;
    case 'archive':
      cfg.archive.directory  = getVal('archiveDir');
      cfg.archive.max_files  = +getVal('maxFiles');
      break;
    case 'prompts':
      cfg.system_prompts.stock    = getVal('promptStock');
      cfg.system_prompts.creative = getVal('promptCreative');
      cfg.system_prompts.update   = getVal('promptUpdate');
      cfg.system_prompts.repair   = getVal('promptRepair');
      break;
  }

  const r = await fetch('/api/config', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  if (r.ok) {
    const modeChanged = section === 'mode' && cfg.boot_mode !== currentCfg.boot_mode;
    currentCfg = cfg;
    flash(`${section}Saved`);
    if (modeChanged) {
      fetch('/api/skip', { method: 'POST' });
    }
  }
}

// ── Mode UI ───────────────────────────────────────────────────────────────────
function setMode(mode, save = false) {
  document.getElementById('modeGenerate').classList.toggle('active', mode === 'generate');
  document.getElementById('modeReplay').classList.toggle('active',   mode === 'replay');
  if (save) saveSection('mode');
}

// ── Models dropdown ───────────────────────────────────────────────────────────
async function refreshModels() {
  const r = await fetch('/api/models');
  const data = await r.json();
  if (data.error) { alert('LM Studio error: ' + data.error); return; }
  populateModelDropdown(data.models, currentCfg.lm_studio?.model ?? '');
}

function populateModelDropdown(models, selected) {
  const sel = document.getElementById('lmModel');
  sel.innerHTML = '';
  const opts = models.length ? models : [selected || '(none)'];
  for (const m of opts) {
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === selected) o.selected = true;
    sel.appendChild(o);
  }
}

// ── Prompts CSV table ─────────────────────────────────────────────────────────
async function loadPrompts() {
  const [pr, sr] = await Promise.all([
    fetch('/api/prompts'),
    fetch('/api/prompt_stats'),
  ]);
  promptRows  = (await pr.json()).rows || [];
  promptStats = await sr.json();
  renderPromptsTable();
}

function renderPromptsTable() {
  const tbody = document.getElementById('promptsTbody');
  tbody.innerHTML = '';
  for (let i = 0; i < promptRows.length; i++) {
    const row = promptRows[i];
    const effect = row.Effect || '';
    const st = promptStats[effect];
    const total = st?.total ?? 0;
    const failColor = total === 0 ? 'var(--c-muted)'
                    : total <= 2  ? 'var(--c-warn)'
                    :               'var(--c-err)';
    const failLabel = total === 0 ? '—' : String(total);
    const failTitle = st ? `${st.crashes} crashed, ${st.blanks} blank` : '';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="text" value="${esc(effect)}" data-i="${i}" data-f="Effect"></td>
      <td><input type="text" value="${esc(row['Three.js prompt'] || '')}" data-i="${i}" data-f="Three.js prompt"></td>
      <td style="text-align:center;color:${failColor};font-weight:bold;cursor:default" title="${esc(failTitle)}">${failLabel}</td>
      <td><button class="btn btn-sm btn-danger" onclick="deleteRow(${i})">✕</button></td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', () => {
      promptRows[+inp.dataset.i][inp.dataset.f] = inp.value;
    });
  });
  document.getElementById('csvRowCount').textContent = `${promptRows.length} rows`;
}

function addPromptRow() {
  promptRows.push({ Effect: '', 'Three.js prompt': '' });
  renderPromptsTable();
}

function deleteRow(i) {
  promptRows.splice(i, 1);
  renderPromptsTable();
}

async function savePrompts() {
  const r = await fetch('/api/prompts', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows: promptRows }),
  });
  if (r.ok) flash('csvSaved');
}

// ── Archive info ──────────────────────────────────────────────────────────────
async function loadArchive() {
  const r = await fetch('/api/archive');
  const data = await r.json();
  document.getElementById('archiveCount').textContent =
    `${data.files.length} files`;
}

// ── Skip button ───────────────────────────────────────────────────────────────
async function skip() {
  await fetch('/api/skip', { method: 'POST' });
}

// ── Log tail ──────────────────────────────────────────────────────────────────
async function refreshLog() {
  const r = await fetch('/api/logs?n=30');
  const data = await r.json();
  const el = document.getElementById('logTail');
  el.textContent = (data.lines || []).join('\n');
  el.scrollTop = el.scrollHeight;
}

// ── Live status via WS ────────────────────────────────────────────────────────
function connectStatusWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'state') updateStateDisplay(msg.state);
    } catch (e) {}
  };
  ws.onclose = () => setTimeout(connectStatusWS, 3000);
}

function updateStateDisplay(state) {
  document.getElementById('stateLabel').textContent = state;
  const dot = document.getElementById('stateDot');
  dot.className = ['GENERATE','DISPLAY','REPLAY'].includes(state) ? 'active'
                : ['DISCARD','REPAIR'].includes(state) ? 'error' : '';
}

// ── Section collapse ──────────────────────────────────────────────────────────
function toggle(header) {
  header.classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('collapsed');
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function getVal(id) { return document.getElementById(id)?.value ?? ''; }
function setVal(id, v) { const el = document.getElementById(id); if (el) el.value = v; }
function getChecked(id) { return document.getElementById(id)?.checked ?? false; }
function setChecked(id, v) { const el = document.getElementById(id); if (el) el.checked = v; }
function showW(inp, valId) { document.getElementById(valId).textContent = inp.value; }
function set(id, v, valId) { setVal(id, v); if (valId) document.getElementById(valId).textContent = v; }
function esc(s) { return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;'); }
function deepClone(o) { return JSON.parse(JSON.stringify(o)); }

function flash(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = '✓ Saved';
  el.style.opacity = '1';
  setTimeout(() => { el.style.opacity = '0'; }, 2000);
}
