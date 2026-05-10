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

// ── State ─────────────────────────────────────────────────────────────────────
let rawCode = '';
let displayQueue = [];
let cfg = {};
let currentArchiveFilename = null;  // non-null only when showing an /archive/ file

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
    case 'state':       handleState(msg);  break;
    case 'token':       queueToken(msg.token); break;
    case 'status':      setStatus(msg.text); break;
    case 'audition':    startAudition(msg.url, msg.seconds); break;
    case 'config_push': applyConfig(msg.config); break;
  }
}

function applyConfig(c) {
  cfg = c;
  const palette = c?.display?.palette || 'amber_crt';
  document.body.className = palette;
}

// ── State transitions ─────────────────────────────────────────────────────────
function handleState(msg) {
  const s = msg.state;
  console.log('[display] state:', s, msg);

  switch (s) {
    case 'IDLE':
      showIdle();
      break;

    case 'PICK_MODE':
      hideIdle();
      showMockOS();
      hideDemoFrame();
      resetEditor();
      setStatus('SELECTING MODE…');
      break;

    case 'GENERATE':
      hideIdle();
      showMockOS();
      hideDemoFrame();
      resetEditor();
      modeChip.textContent = (msg.mode || '').toUpperCase();
      setStatus('WRITING…');
      break;

    case 'VALIDATE':
      setStatus(msg.attempt > 0 ? `VALIDATING (attempt ${msg.attempt})…` : 'VALIDATING…');
      break;

    case 'REPAIR':
      setStatus(`REPAIRING — ATTEMPT ${msg.attempt}/${msg.max}…`);
      break;

    case 'DISPLAY':
      hideIdle();
      // Load iframe if not already loaded by audition
      if (msg.url && demoFrame.src !== location.origin + msg.url) {
        demoFrame.src = msg.url;
      }
      currentArchiveFilename = msg.url?.startsWith('/archive/')
        ? msg.url.split('/').pop()
        : null;
      filenameInfo.textContent = (msg.url || '').split('/').pop();
      setStatus('RUNNING');
      showDemoFrame();
      hideMockOS();
      break;

    case 'REPLAY':
      hideIdle();
      hideMockOS();
      setStatus('REPLAY MODE');
      break;

    case 'ARCHIVE':
      showMockOS();
      hideDemoFrame();
      setStatus('SAVING TO ARCHIVE…');
      break;

    case 'DISCARD':
      showMockOS();
      hideDemoFrame();
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

// ── Token queue + typing animation ────────────────────────────────────────────
const CHARS_PER_TICK = 20;   // at ~50ms tick = 400 chars/sec
const TICK_MS = 50;

function queueToken(tok) {
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
  const filename = currentArchiveFilename;
  deleteConfirm.classList.add('hidden');
  currentArchiveFilename = null;
  setStatus('DEMO DELETED — loading next…');
  await fetch(`/api/archive/${encodeURIComponent(filename)}`, { method: 'DELETE' });
};

function handleKey(key) {
  if (key === 'Escape') {
    deleteConfirm.classList.add('hidden');
    return;
  }
  if (key === 'd' && currentArchiveFilename && deleteConfirm.classList.contains('hidden')) {
    deleteFilename.textContent = currentArchiveFilename;
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
