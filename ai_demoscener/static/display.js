'use strict';

// ── DOM refs ──────────────────────────────────────────────────────────────────
const demoFrame   = document.getElementById('demoFrame');
const mockOS      = document.getElementById('mockOS');
const idleOverlay = document.getElementById('idleOverlay');
const editorCode  = document.getElementById('editorCode');
const lineNumbers = document.getElementById('lineNumbers');
const editorContent = document.getElementById('editorContent');
const statusText  = document.getElementById('statusText');
const posInfo     = document.getElementById('posInfo');
const filenameInfo= document.getElementById('filenameInfo');
const modeChip    = document.getElementById('modeChip');
const cpuBar      = document.getElementById('cpuBar');
const cpuPct      = document.getElementById('cpuPct');
const memBar      = document.getElementById('memBar');
const memPct      = document.getElementById('memPct');
const titleCard      = document.getElementById('titleCard');
const titleCardTitle = document.getElementById('titleCardTitle');
const titleCardDesc  = document.getElementById('titleCardDesc');
const titleCardFile  = document.getElementById('titleCardFile');
const titleCardModel = document.getElementById('titleCardModel');
const titleCardStats = document.getElementById('titleCardStats');
const modelInfo      = document.getElementById('modelInfo');
const progressBar    = document.getElementById('progressBar');
const spinnerChar   = document.getElementById('spinnerChar');
const spinnerVerb   = document.getElementById('spinnerVerb');
const cursorEl      = document.getElementById('cursor');

// Sync line numbers scroll position with editor content
editorContent.addEventListener('scroll', () => {
  lineNumbers.scrollTop = editorContent.scrollTop;
});

// ── Spinner ───────────────────────────────────────────────────────────────────
const SPINNER_FRAMES = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
const SPINNER_VERBS = [
  'Accomplishing','Actioning','Actualizing','Baking','Brewing','Calculating',
  'Cerebrating','Churning','Clauding','Coalescing','Cogitating','Computing',
  'Conjuring','Considering','Cooking','Crafting','Creating','Crunching',
  'Deliberating','Determining','Doing','Effecting','Finagling','Forging',
  'Forming','Generating','Hatching','Herding','Honking','Hustling','Ideating',
  'Inferring','Manifesting','Marinating','Moseying','Mulling','Mustering',
  'Musing','Noodling','Percolating','Pondering','Processing','Puttering',
  'Reticulating','Ruminating','Schlepping','Shucking','Simmering','Smooshing',
  'Spinning','Stewing','Synthesizing','Thinking','Transmuting','Vibing','Working',
];

let _spinFrame = 0, _spinInterval = null, _spinVerbInterval = null;

function startSpinner() {
  if (_spinInterval) return;  // already running
  _spinFrame = 0;
  spinnerChar.textContent = SPINNER_FRAMES[0];
  spinnerVerb.textContent = SPINNER_VERBS[Math.floor(Math.random() * SPINNER_VERBS.length)] + '…';
  spinnerChar.style.display = 'inline';
  spinnerVerb.style.display = 'inline';
  cursorEl.style.display = 'none';
  _spinInterval = setInterval(() => {
    _spinFrame = (_spinFrame + 1) % SPINNER_FRAMES.length;
    spinnerChar.textContent = SPINNER_FRAMES[_spinFrame];
  }, 80);
  _spinVerbInterval = setInterval(() => {
    spinnerVerb.textContent = SPINNER_VERBS[Math.floor(Math.random() * SPINNER_VERBS.length)] + '…';
  }, 2500);
}

function stopSpinner() {
  if (!_spinInterval) return;  // already stopped
  spinnerChar.style.display = 'none';
  spinnerVerb.style.display = 'none';
  cursorEl.style.display = '';
  if (_spinInterval)     { clearInterval(_spinInterval);     _spinInterval = null; }
  if (_spinVerbInterval) { clearInterval(_spinVerbInterval); _spinVerbInterval = null; }
}

// ── State ─────────────────────────────────────────────────────────────────────
let rawCode = '';
let displayQueue = [];
let cfg = {};
let currentArchiveFilename = null;  // non-null only when showing an /archive/ file
let currentTempFilename = null;     // non-null only when showing a /temp/ file (generate mode)
let currentDisplayMeta = null;      // meta from the active DISPLAY message

// Audition tracking
let auditActive = false;
let auditErrors = [];
let auditHeartbeats = [];
let auditTimer = null;
let auditSeconds = 0;

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws;
let wsRetryDelay = 1000;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    wsRetryDelay = 1000;
    console.log('[display] WS connected');
  };

  ws.onmessage = (ev) => {
    try { handleMsg(JSON.parse(ev.data)); } catch(e) { console.error(e); }
  };

  ws.onclose = () => {
    console.log('[display] WS closed; reconnecting in', wsRetryDelay, 'ms');
    setTimeout(connectWS, wsRetryDelay);
    wsRetryDelay = Math.min(wsRetryDelay * 2, 15000);
  };
}

function sendWS(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// ── Message handler ───────────────────────────────────────────────────────────
function handleMsg(msg) {
  switch (msg.type) {
    case 'state':          handleState(msg);  break;
    case 'token':          queueToken(msg.token); break;
    case 'status':         setStatus(msg.text); break;
    case 'audition':       startAudition(msg.url, msg.seconds); break;
    case 'config_push':    applyConfig(msg.config); break;
    case 'model_selected': modelInfo.textContent = `[${msg.model}]`; break;
  }
}

function applyConfig(c) {
  cfg = c;
  const palette = c?.display?.palette || 'amber_crt';
  document.body.className = palette;
  const statsPx = c?.display?.title_card_stats_font_size ?? 11;
  titleCardStats.style.fontSize = statsPx + 'px';
}

// ── State transitions ─────────────────────────────────────────────────────────
function handleState(msg) {
  const s = msg.state;
  console.log('[display] state:', s, msg);

  switch (s) {
    case 'IDLE':
      showIdle();
      resetTitleCard();
      stopSpinner();
      modelInfo.textContent = '';
      break;

    case 'PICK_MODE':
      hideIdle();
      showMockOS();
      hideDemoFrame();
      resetEditor();
      resetTitleCard();
      stopSpinner();
      modelInfo.textContent = '';
      setStatus('SELECTING MODE…');
      break;

    case 'GENERATE':
      hideIdle();
      showMockOS();
      hideDemoFrame();
      resetEditor();
      resetTitleCard();
      modeChip.textContent = (msg.mode || '').toUpperCase();
      modelInfo.textContent = msg.model ? `[${msg.model}]` : '';
      setStatus('WRITING…');
      startSpinner();
      break;

    case 'VALIDATE':
      stopSpinner();
      setStatus(msg.attempt > 0 ? `VALIDATING (attempt ${msg.attempt})…` : 'VALIDATING…');
      break;

    case 'REPAIR':
      setStatus(`REPAIRING — ATTEMPT ${msg.attempt}/${msg.max}…`);
      startSpinner();
      break;

    case 'DISPLAY': {
      hideIdle();
      stopSpinner();
      const msgUrl = msg.url || '';
      const isNewDemo = msgUrl !== _currentDisplayUrl;
      // Load iframe if not already loaded by audition
      if (msgUrl && demoFrame.src !== location.origin + msgUrl) {
        demoFrame.src = msgUrl;
      }
      const urlFile = msgUrl.split('/').pop();
      currentArchiveFilename = msgUrl.startsWith('/archive/') ? urlFile : null;
      currentTempFilename    = msgUrl.startsWith('/temp/')    ? urlFile : null;
      currentDisplayMeta     = msg.meta || null;
      filenameInfo.textContent = urlFile;
      modelInfo.textContent = msg.meta?.model ? `[${msg.meta.model}]` : '';
      setStatus('RUNNING');
      showDemoFrame();
      hideMockOS();
      if (isNewDemo) {
        _currentDisplayUrl = msgUrl;
        _displayStartTime  = Date.now();
        if (msg.meta && cfg?.display?.show_title_card !== false) {
          showTitleCard(msg.meta, msgUrl.split('/').pop(), msg.runtime || 60);
        } else if (cfg?.display?.show_progress_bar !== false) {
          // Title card disabled but progress bar enabled — start bar immediately
          const runtime = msg.runtime || 60;
          progressBar.style.transition = 'none';
          progressBar.style.width = '0%';
          progressBar.style.opacity = '1';
          progressBar.getBoundingClientRect();
          progressBar.style.transition = `width ${runtime}s linear`;
          progressBar.style.width = '100%';
        }
        showStatsOverlay(msg.meta?.stats, msg.meta?.effect, msg.meta?.model);
      } else {
        // WS reconnect to same demo — restore progress bar to correct position
        if (cfg?.display?.show_progress_bar !== false) {
          restoreProgressBar(msg.runtime || 60);
        }
        showStatsOverlay(msg.meta?.stats, msg.meta?.effect, msg.meta?.model);
      }
      break;
    }

    case 'REPLAY':
      hideIdle();
      hideMockOS();
      stopSpinner();
      setStatus('REPLAY MODE');
      break;

    case 'ARCHIVE':
      showMockOS();
      hideDemoFrame();
      resetTitleCard();
      stopSpinner();
      setStatus('SAVING TO ARCHIVE…');
      break;

    case 'DISCARD':
      showMockOS();
      hideDemoFrame();
      resetTitleCard();
      stopSpinner();
      if (msg.kind) {
        setStatus(`DISCARDING — ${msg.kind}`);
        showErrorOverlay(msg.kind, msg.error || '', 5);
      } else {
        setStatus('DISCARDING — no valid demo produced');
      }
      break;
  }
}

// ── Visibility helpers ────────────────────────────────────────────────────────
function showIdle()    { idleOverlay.classList.remove('hidden'); }
function hideIdle()    { idleOverlay.classList.add('hidden'); }
function showMockOS()  { mockOS.classList.remove('hidden'); }
function hideMockOS()  { mockOS.classList.add('hidden'); }
function showDemoFrame(){ demoFrame.classList.add('visible'); }
function hideDemoFrame(){ demoFrame.classList.remove('visible'); }

function setStatus(text) { statusText.textContent = text; }

// ── Title card + progress bar ─────────────────────────────────────────────────
let _titleCardTimer   = null;
let _currentDisplayUrl = '';
let _displayStartTime  = 0;

function showStatsOverlay(stats, effectKey, modelId) {
  if (!stats || !cfg?.display?.show_title_card_stats) {
    titleCardStats.classList.remove('visible');
    return;
  }
  const fmt = r => `${r.runs ?? 0} runs · ${r.fails ?? 0} fail · ${r.deletions ?? 0} del` +
                   (r.fail_pct !== null && r.fail_pct !== undefined ? ` (${r.fail_pct}%)` : '');
  const lines = [];
  if (stats.effect && (stats.effect.runs > 0 || stats.effect.deletions > 0))
    lines.push(escHtml(effectKey || '') + ': ' + fmt(stats.effect));
  if (modelId && stats.model && (stats.model.runs > 0 || stats.model.deletions > 0))
    lines.push(escHtml(modelId) + ': ' + fmt(stats.model));
  if (lines.length === 0) {
    titleCardStats.classList.remove('visible');
    return;
  }
  titleCardStats.innerHTML = lines.join('<br>');
  titleCardStats.classList.add('visible');
}

function showTitleCard(meta, filename, runtime) {
  const titleSecs = cfg?.display?.title_card_seconds ?? 4;
  const title = meta?.title || meta?.effect || filename.replace(/\.[^.]+$/, '').replace(/_/g, ' ');
  titleCardTitle.textContent = title.toUpperCase();
  titleCardDesc.textContent  = meta?.description || '';
  titleCardFile.textContent  = filename;
  titleCardModel.textContent = meta?.model ? `by ${meta.model}` : 'author unknown';
  titleCard.classList.remove('hidden');

  // Reset progress bar (cancel any running animation first)
  progressBar.style.transition = 'none';
  progressBar.style.width = '0%';
  progressBar.style.opacity = '0';

  // Cancel any pending title-card timer so there is never more than one queued
  if (_titleCardTimer) { clearTimeout(_titleCardTimer); _titleCardTimer = null; }

  _titleCardTimer = setTimeout(() => {
    _titleCardTimer = null;
    titleCard.classList.add('hidden');
    if (cfg?.display?.show_progress_bar !== false) {
      const remaining = Math.max(1, runtime - titleSecs);
      progressBar.style.opacity = '1';
      progressBar.getBoundingClientRect(); // force reflow so transition fires from 0%
      progressBar.style.transition = `width ${remaining}s linear`;
      progressBar.style.width = '100%';
    }
  }, titleSecs * 1000);
}

function restoreProgressBar(runtime) {
  // Called on WS reconnect when the same demo is still playing.
  // Jump the progress bar to the correct elapsed position.
  const titleSecs = cfg?.display?.title_card_seconds ?? 4;
  if (_displayStartTime === 0) return;
  const elapsed    = (Date.now() - _displayStartTime) / 1000;
  if (elapsed < titleSecs) return;  // still in title-card window; nothing to restore
  const totalAnim  = Math.max(1, runtime - titleSecs);
  const doneFrac   = Math.min(1, (elapsed - titleSecs) / totalAnim);
  const remaining  = Math.max(0, totalAnim * (1 - doneFrac));
  progressBar.style.transition = 'none';
  progressBar.style.width      = `${(doneFrac * 100).toFixed(1)}%`;
  progressBar.style.opacity    = '1';
  progressBar.getBoundingClientRect(); // force reflow
  progressBar.style.transition = `width ${remaining}s linear`;
  progressBar.style.width      = '100%';
}

function resetTitleCard() {
  _currentDisplayUrl = '';
  _displayStartTime  = 0;
  currentArchiveFilename = null;
  currentTempFilename    = null;
  currentDisplayMeta     = null;
  if (_titleCardTimer) { clearTimeout(_titleCardTimer); _titleCardTimer = null; }
  titleCard.classList.add('hidden');
  titleCardStats.classList.remove('visible');
  progressBar.style.transition = 'none';
  progressBar.style.width = '0%';
  progressBar.style.opacity = '0';
}

// ── Token queue + typing animation ────────────────────────────────────────────
const CHARS_PER_TICK = 20;   // at ~50ms tick = 400 chars/sec
const TICK_MS = 50;

function queueToken(tok) {
  stopSpinner();
  for (const ch of tok) displayQueue.push(ch);
}

function processTick() {
  if (displayQueue.length === 0) return;
  const batch = displayQueue.splice(0, CHARS_PER_TICK).join('');
  rawCode += batch;
  renderEditor();
}

setInterval(processTick, TICK_MS);

function resetEditor() {
  rawCode = '';
  displayQueue = [];
  renderEditor();
}

function renderEditor() {
  const lines = rawCode.split('\n');
  const lineCount = lines.length;

  // Line numbers
  const nums = [];
  for (let i = 1; i <= lineCount; i++) nums.push(i);
  lineNumbers.textContent = nums.join('\n');

  // Syntax-highlighted code
  editorCode.innerHTML = highlight(rawCode);

  // Position info
  const lastLine = lines[lineCount - 1];
  posInfo.textContent = `Ln ${lineCount} Col ${lastLine.length + 1}`;

  // Auto-scroll
  editorContent.scrollTop = editorContent.scrollHeight;
}

// ── Syntax highlighting ───────────────────────────────────────────────────────
function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

const KW = /\b(const|let|var|function|class|extends|new|return|if|else|for|while|do|switch|case|break|continue|import|export|from|default|this|super|typeof|instanceof|void|delete|throw|try|catch|finally|async|await|of|in|true|false|null|undefined|NaN|Infinity|static|get|set)\b/g;
const BUILTIN = /\b(THREE|WebGLRenderer|Scene|PerspectiveCamera|OrthographicCamera|ShaderMaterial|MeshBasicMaterial|MeshStandardMaterial|MeshPhongMaterial|PlaneGeometry|BoxGeometry|SphereGeometry|BufferGeometry|Mesh|Clock|Vector2|Vector3|Vector4|Color|Quaternion|Matrix4|TextureLoader|requestAnimationFrame|cancelAnimationFrame|document|window|console|Math|Date|Object|Array|JSON|Float32Array|performance)\b/g;
const NUM = /\b(\d+\.?\d*(?:[eE][+-]?\d+)?)\b/g;

function highlightCode(text) {
  return escHtml(text)
    .replace(KW,      '<span class="hl-keyword">$1</span>')
    .replace(BUILTIN, '<span class="hl-builtin">$1</span>')
    .replace(NUM,     '<span class="hl-number">$1</span>');
}

function highlight(code) {
  // Tokenise: split into strings, line-comments, block-comments, and plain code
  const segments = [];
  let i = 0;
  while (i < code.length) {
    // Line comment
    if (code[i] === '/' && code[i+1] === '/') {
      const end = code.indexOf('\n', i);
      const s = end === -1 ? code.slice(i) : code.slice(i, end);
      segments.push({ t: 'comment', s });
      i = end === -1 ? code.length : end;

    // Block comment
    } else if (code[i] === '/' && code[i+1] === '*') {
      const end = code.indexOf('*/', i + 2);
      const s = end === -1 ? code.slice(i) : code.slice(i, end + 2);
      segments.push({ t: 'comment', s });
      i = end === -1 ? code.length : end + 2;

    // String
    } else if (code[i] === '"' || code[i] === "'" || code[i] === '`') {
      const q = code[i];
      let j = i + 1;
      while (j < code.length) {
        if (code[j] === '\\') { j += 2; continue; }
        if (code[j] === q)   { j++; break; }
        j++;
      }
      segments.push({ t: 'string', s: code.slice(i, j) });
      i = j;

    // Plain code (until next string/comment delimiter)
    } else {
      let j = i + 1;
      while (j < code.length) {
        const c = code[j];
        if (c === '/' || c === '"' || c === "'" || c === '`') break;
        j++;
      }
      segments.push({ t: 'code', s: code.slice(i, j) });
      i = j;
    }
  }

  return segments.map(({ t, s }) => {
    if (t === 'comment') return `<span class="hl-comment">${escHtml(s)}</span>`;
    if (t === 'string')  return `<span class="hl-string">${escHtml(s)}</span>`;
    return highlightCode(s);
  }).join('');
}

// ── Audition (iframe validation) ──────────────────────────────────────────────
function startAudition(url, seconds) {
  auditErrors = [];
  auditHeartbeats = [];
  auditActive = true;
  auditSeconds = seconds;

  if (auditTimer) clearTimeout(auditTimer);
  demoFrame.src = url;
  setStatus('AUDITING…');

  auditTimer = setTimeout(() => concludeAudition(), seconds * 1000);
}

window.addEventListener('message', (ev) => {
  if (!auditActive) return;
  const msg = ev.data;
  if (!msg || typeof msg.type !== 'string') return;

  if (msg.type === 'probe_error') {
    auditErrors.push(msg.error || 'unknown error');
    // Fail fast on first error
    if (auditTimer) { clearTimeout(auditTimer); auditTimer = null; }
    concludeAudition();
  } else if (msg.type === 'probe_heartbeat') {
    auditHeartbeats.push({ fps: msg.fps, draws: msg.draws, hasDrawn: msg.hasDrawn,
                            brightPixels: msg.brightPixels ?? null });
  }
});

function concludeAudition() {
  auditActive = false;
  const minFps   = cfg?.validation?.min_fps ?? 5;
  const blankSec = cfg?.validation?.blank_detection_seconds ?? 4;

  let result, error;

  if (auditErrors.length > 0) {
    result = 'CRASHED';
    error  = auditErrors[0];
  } else if (auditHeartbeats.length === 0) {
    // No heartbeat at all within audition window
    result = 'BLANK';
    error  = 'No activity detected within audition window';
  } else {
    const noDraws = auditHeartbeats.filter(h => !h.hasDrawn);
    const avgFps  = auditHeartbeats.reduce((a, h) => a + h.fps, 0) / auditHeartbeats.length;
    if (noDraws.length >= blankSec && !auditHeartbeats.some(h => h.hasDrawn)) {
      result = 'BLANK';
      error  = 'No WebGL draw calls detected';
    } else if (avgFps < minFps && auditHeartbeats.length >= blankSec) {
      result = 'BLANK';
      error  = `Average FPS ${avgFps.toFixed(1)} below minimum ${minFps}`;
    } else {
      const pixelData = auditHeartbeats.filter(h => h.brightPixels !== null);
      if (pixelData.length >= blankSec && pixelData.every(h => h.brightPixels === 0)) {
        result = 'BLANK';
        error  = 'WebGL active but no visible content detected';
      } else {
        result = 'OK';
      }
    }
  }

  console.log('[display] audit result:', result, error || '');
  sendWS({ type: 'audit_result', result, error: error || null });
}

// ── Fake CPU/MEM gauges ───────────────────────────────────────────────────────
let cpuVal = 20, memVal = 35;
function wiggleGauges() {
  cpuVal = Math.max(5, Math.min(95, cpuVal + (Math.random() - 0.5) * 15));
  memVal = Math.max(20, Math.min(80, memVal + (Math.random() - 0.5) * 5));
  cpuBar.style.width = `${cpuVal.toFixed(0)}%`;
  cpuPct.textContent = `${cpuVal.toFixed(0)}%`;
  memBar.style.width = `${memVal.toFixed(0)}%`;
  memPct.textContent = `${memVal.toFixed(0)}%`;
}
setInterval(wiggleGauges, 1200);

// ── Error overlay ─────────────────────────────────────────────────────────────
const errorOverlay   = document.getElementById('errorOverlay');
const errorKind      = document.getElementById('errorKind');
const errorMsg       = document.getElementById('errorMsg');
const errorCountdown = document.getElementById('errorCountdown');
let _errorTimer = null;

function showErrorOverlay(kind, msg, seconds) {
  errorKind.textContent = kind === 'BLANK' ? 'BLANK FRAME' : 'CRASHED';
  errorMsg.textContent  = msg || '';
  errorCountdown.textContent = `NEXT CYCLE IN ${seconds}`;
  errorOverlay.classList.remove('hidden');

  let remaining = seconds;
  if (_errorTimer) clearInterval(_errorTimer);
  _errorTimer = setInterval(() => {
    remaining--;
    errorCountdown.textContent = `NEXT CYCLE IN ${remaining}`;
    if (remaining <= 0) {
      clearInterval(_errorTimer);
      _errorTimer = null;
      errorOverlay.classList.add('hidden');
    }
  }, 1000);
}

// ── Delete confirm ────────────────────────────────────────────────────────────
const deleteConfirm  = document.getElementById('deleteConfirm');
const deleteFilename = document.getElementById('deleteFilename');

document.getElementById('deleteNo').onclick = () =>
  deleteConfirm.classList.add('hidden');

document.getElementById('deleteYes').onclick = async () => {
  const archiveFile = currentArchiveFilename;
  const isTemp      = !!currentTempFilename;
  deleteConfirm.classList.add('hidden');
  currentArchiveFilename = null;
  currentTempFilename    = null;
  setStatus('DEMO DELETED — loading next…');
  if (archiveFile) {
    await fetch(`/api/archive/${encodeURIComponent(archiveFile)}`, { method: 'DELETE' });
  } else if (isTemp) {
    await fetch('/api/discard_current', { method: 'POST' });
  }
};

function handleKey(key) {
  if (key === 'Escape') {
    deleteConfirm.classList.add('hidden');
    return;
  }
  const canDelete = (currentArchiveFilename || currentTempFilename)
                    && deleteConfirm.classList.contains('hidden');
  if (key === 'd' && canDelete) {
    if (currentArchiveFilename) {
      deleteFilename.textContent = currentArchiveFilename;
    } else {
      const m = currentDisplayMeta;
      deleteFilename.textContent = m?.title || m?.effect || currentTempFilename || '';
    }
    deleteConfirm.classList.remove('hidden');
  }
}

// Catch keys when parent document has focus
document.addEventListener('keydown', (e) => handleKey(e.key));

// Catch keys relayed from inside the iframe via the probe (iframe steals focus when demo loads)
window.addEventListener('message', (ev) => {
  if (ev.data?.type === 'probe_keydown') handleKey(ev.data.key);
});

// Refocus parent after each iframe load so the parent keydown listener also works
demoFrame.addEventListener('load', () => { if (demoFrame.src !== 'about:blank') window.focus(); });

// ── Boot ──────────────────────────────────────────────────────────────────────
connectWS();
