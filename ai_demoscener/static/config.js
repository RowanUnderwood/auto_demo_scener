'use strict';

let currentCfg = {};
let promptRows = [];
let promptStats = {};
let previewFiles = [];
let titleCardRows = [];
let _tcPollTimer = null;

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', async () => {
  await loadConfig();
  await loadPrompts();
  await loadArchive();
  await loadTitleCards();
  await loadFailureRate();
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
  setChecked('lmFallback',    c.lm_fallback_to_replay       ?? true);
  setChecked('lmSurpriseMe',  c.lm_studio?.surprise_me      ?? false);
  document.getElementById('surpriseLastField').style.display =
    (c.lm_studio?.surprise_me) ? '' : 'none';
  // Model dropdown gets populated by refreshModels() if called
  populateModelDropdown([c.lm_studio?.model ?? ''], c.lm_studio?.model ?? '');

  // Display
  setVal('demoRuntime',        c.display?.demo_runtime_seconds ?? 60);
  setVal('minTyping',          c.display?.min_typing_seconds   ?? 8);
  setVal('displayCharsPerSec', c.display?.display_chars_per_sec ?? 400);
  setChecked('showTitleCard',      c.display?.show_title_card       ?? true);
  setChecked('showProgressBar',    c.display?.show_progress_bar     ?? true);
  setChecked('showTitleCardStats', c.display?.show_title_card_stats ?? true);
  setVal('statsOverlayFontSize',   c.display?.title_card_stats_font_size ?? 11);
  setVal('titleCardSecs',          c.display?.title_card_seconds    ?? 4);
  setVal('palette',                c.display?.palette ?? 'amber_crt');
  setChecked('showStatusBar',      c.display?.show_status_bar       ?? true);
  setChecked('showRetry',          c.display?.show_retry_messages   ?? true);
  setChecked('jitTitling',         c.display?.jit_titling           ?? false);

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
      cfg.lm_studio.surprise_me              = getChecked('lmSurpriseMe');
      cfg.lm_fallback_to_replay = getChecked('lmFallback');
      break;
    case 'display':
      cfg.display.demo_runtime_seconds  = +getVal('demoRuntime');
      cfg.display.min_typing_seconds    = +getVal('minTyping');
      cfg.display.display_chars_per_sec = +getVal('displayCharsPerSec');
      cfg.display.show_title_card        = getChecked('showTitleCard');
      cfg.display.show_progress_bar      = getChecked('showProgressBar');
      cfg.display.show_title_card_stats      = getChecked('showTitleCardStats');
      cfg.display.title_card_stats_font_size = +getVal('statsOverlayFontSize');
      cfg.display.title_card_seconds         = +getVal('titleCardSecs');
      cfg.display.palette               = getVal('palette');
      cfg.display.show_status_bar       = getChecked('showStatusBar');
      cfg.display.show_retry_messages   = getChecked('showRetry');
      cfg.display.jit_titling           = getChecked('jitTitling');
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
  previewFiles = data.files || [];
  document.getElementById('archiveCount').textContent = `${previewFiles.length} files`;
  const sel = document.getElementById('previewSelect');
  sel.innerHTML = previewFiles.length === 0
    ? '<option value="">No archive files</option>'
    : previewFiles.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('');
}

// ── Skip button ───────────────────────────────────────────────────────────────
async function skip() {
  await fetch('/api/skip', { method: 'POST' });
}

// ── Shutdown button ───────────────────────────────────────────────────────────
async function shutdown() {
  if (!confirm('Shut down the server?')) return;
  const btn = document.getElementById('shutdownBtn');
  btn.textContent = 'Shutting down…';
  btn.disabled = true;
  await fetch('/api/shutdown', { method: 'POST' });
}

// ── Preview section ───────────────────────────────────────────────────────────
function togglePreview(header) {
  toggle(header);
  if (!header.classList.contains('collapsed')) loadArchive();
}

function previewLoad(filename) {
  if (!filename) return;
  document.getElementById('previewSelect').value = filename;
  document.getElementById('previewFrame').src = `/archive/${encodeURIComponent(filename)}`;
}

function previewNav(dir) {
  const sel = document.getElementById('previewSelect');
  const idx = previewFiles.indexOf(sel.value);
  if (idx === -1 && previewFiles.length) { previewLoad(previewFiles[0]); return; }
  if (!previewFiles.length) return;
  previewLoad(previewFiles[(idx + dir + previewFiles.length) % previewFiles.length]);
}

async function previewDelete() {
  const filename = document.getElementById('previewSelect').value;
  if (!filename || !confirm(`Delete ${filename}?`)) return;
  const r = await fetch(`/api/archive/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const data = await r.json();
  if (data.ok) {
    const nextIdx = Math.max(0, Math.min(previewFiles.indexOf(filename), previewFiles.length - 2));
    previewFiles = previewFiles.filter(f => f !== filename);
    const sel = document.getElementById('previewSelect');
    sel.innerHTML = previewFiles.length
      ? previewFiles.map(f => `<option value="${esc(f)}">${esc(f)}</option>`).join('')
      : '<option value="">No archive files</option>';
    document.getElementById('archiveCount').textContent = `${previewFiles.length} files`;
    if (previewFiles.length) previewLoad(previewFiles[nextIdx]);
    else document.getElementById('previewFrame').src = 'about:blank';
  }
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
      if (msg.type === 'model_selected') {
        document.getElementById('surpriseLastModel').textContent = msg.model;
      }
    } catch (e) {}
  };
  ws.onclose = () => setTimeout(connectStatusWS, 3000);
}

function toggleSurpriseLastField(cb) {
  document.getElementById('surpriseLastField').style.display = cb.checked ? '' : 'none';
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

// ── Title Cards ───────────────────────────────────────────────────────────────
async function loadTitleCards() {
  const r = await fetch('/api/title_cards');
  titleCardRows = await r.json();
  renderTitleCardsTable();
}

function renderTitleCardsTable() {
  const tbody = document.getElementById('titleCardsTbody');
  tbody.innerHTML = '';
  for (let i = 0; i < titleCardRows.length; i++) {
    const row = titleCardRows[i];
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="tc-filename">${esc(row.filename || '')}</td>
      <td><input type="text" value="${esc(row.title || '')}" data-i="${i}" data-f="title"></td>
      <td><input type="text" value="${esc(row.description || '')}" data-i="${i}" data-f="description"></td>
    `;
    tbody.appendChild(tr);
  }
  tbody.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', () => {
      titleCardRows[+inp.dataset.i][inp.dataset.f] = inp.value;
    });
  });
  const count = titleCardRows.filter(r => r.title).length;
  document.getElementById('titleCardRowCount').textContent =
    `${count} / ${titleCardRows.length} titled`;
}

async function saveTitleCards() {
  const r = await fetch('/api/title_cards', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entries: titleCardRows }),
  });
  if (r.ok) flash('titleCardsSaved');
}

async function generateAllTitles() {
  const r = await fetch('/api/title_cards/generate_all', { method: 'POST' });
  const data = await r.json();
  if (!data.ok && data.reason === 'already running') return;
  document.getElementById('tcProgress').style.display = '';
  document.getElementById('tcProgressBar').style.width = '0%';
  document.getElementById('tcProgressText').textContent = 'Starting…';
  if (_tcPollTimer) clearInterval(_tcPollTimer);
  _tcPollTimer = setInterval(pollTcProgress, 1000);
}

async function pollTcProgress() {
  const r = await fetch('/api/title_cards/progress');
  const data = await r.json();
  const pct = data.total > 0 ? Math.round(data.done / data.total * 100) : 0;
  document.getElementById('tcProgressBar').style.width = `${pct}%`;
  document.getElementById('tcProgressText').textContent =
    data.current ? `${data.done}/${data.total} — ${data.current}` : `${data.done}/${data.total}`;
  if (!data.running) {
    clearInterval(_tcPollTimer);
    _tcPollTimer = null;
    document.getElementById('tcProgress').style.display = 'none';
    await loadTitleCards();
  }
}

async function stopGenerateTitles() {
  await fetch('/api/title_cards/stop_generate', { method: 'POST' });
  if (_tcPollTimer) { clearInterval(_tcPollTimer); _tcPollTimer = null; }
  document.getElementById('tcProgress').style.display = 'none';
}

async function clearAllTitles() {
  if (!confirm('Clear all title cards? This cannot be undone.')) return;
  await fetch('/api/title_cards/clear_all', { method: 'POST' });
  await loadTitleCards();
}

// ── Failure Rate ──────────────────────────────────────────────────────────────
let _failRateData = { effects: [], modes: [], models: [] };

async function loadFailureRate() {
  const r = await fetch('/api/failure_rate');
  _failRateData = await r.json();
  renderFailureRateTables();
}

function renderFailureRateTables() {
  renderFRTable('failRateEffectsTbody', _failRateData.effects,
    row => [row.effect, row.runs, row.fails, row.deletions, row.fail_pct]);
  renderFRTable('failRateModesTbody', _failRateData.modes,
    row => [row.mode.toUpperCase(), row.runs, row.fails, row.deletions, row.fail_pct]);
  renderFRTable('failRateModelsTbody', _failRateData.models,
    row => [row.model, row.runs, row.fails, row.deletions, row.fail_pct]);
}

function renderFRTable(tbodyId, rows, mapper) {
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML = '';
  if (!rows || !rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="color:var(--c-muted);font-size:0.8rem">No data yet</td></tr>';
    return;
  }
  for (const row of rows) {
    const [name, runs, fails, dels, pct] = mapper(row);
    const color = pct === null || pct === undefined ? ''
                : pct >= 30 ? 'var(--c-err)'
                : pct >= 15 ? 'var(--c-warn)'
                :              'var(--c-ok)';
    const pctStr = pct === null || pct === undefined ? '—' : pct.toFixed(1) + '%';
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${esc(String(name))}</td><td>${runs}</td><td>${fails}</td><td>${dels ?? 0}</td>
      <td style="color:${color};font-weight:bold">${pctStr}</td>`;
    tbody.appendChild(tr);
  }
}

async function clearFailureData() {
  if (!confirm('Clear all failure rate data? This cannot be undone.')) return;
  await fetch('/api/failure_rate/clear', { method: 'POST' });
  await loadFailureRate();
}

function exportFailureCsv() {
  const lines = ['Type,Name,Runs,Fails,Crashes,Blanks,Deletions,Fail%'];
  for (const r of _failRateData.effects)
    lines.push(`stock,${r.effect},${r.runs},${r.fails},${r.crashes},${r.blanks},${r.deletions ?? 0},${r.fail_pct ?? ''}`);
  for (const r of _failRateData.modes)
    lines.push(`mode,${r.mode},${r.runs},${r.fails},${r.crashes},${r.blanks},${r.deletions ?? 0},${r.fail_pct ?? ''}`);
  for (const r of _failRateData.models)
    lines.push(`model,${r.model},${r.runs},${r.fails},${r.crashes},${r.blanks},${r.deletions ?? 0},${r.fail_pct ?? ''}`);
  const blob = new Blob([lines.join('\n')], { type: 'text/csv' });
  const a = Object.assign(document.createElement('a'),
    { href: URL.createObjectURL(blob), download: 'failure_rate.csv' });
  a.click();
}
