// Evaluation page: start/stop paired runs, show per-arm metrics, blind A/B review.

let runs = [];
let selectedRun = null;
let pairs = [];
let pairIdx = 0;

function toggle(header) {
  const body = header.nextElementSibling;
  const hidden = body.style.display === 'none';
  body.style.display = hidden ? '' : 'none';
  header.querySelector('.arrow').textContent = hidden ? '▾' : '▸';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const fmt = v => (v === null || v === undefined) ? '—' : v;

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// ── Start form + runs list ────────────────────────────────────────────────────
async function loadRuns() {
  const data = await api('/api/eval/runs');
  runs = data.runs;
  const checks = document.getElementById('armChecks');
  if (!checks.dataset.filled) {
    checks.innerHTML = data.arms.map(a =>
      `<label class="check-row"><input type="checkbox" value="${esc(a)}" ${data.default_arms.includes(a) ? 'checked' : ''}> ${esc(a)}</label>`
    ).join('');
    checks.dataset.filled = '1';
    document.getElementById('trialList').innerHTML = data.smoke.map(t =>
      `${esc(t.category)}: <b>${esc(t.mode === 'stock' ? t.effect : t.mode)}</b>`).join(' · ');
  }
  const busy = runs.some(r => r.active && ['pending', 'running'].includes(r.status));
  document.getElementById('startBtn').disabled = busy;

  const rows = runs.map(r => {
    const actions = [`<button class="btn btn-sm" onclick="selectRun('${esc(r.run_id)}')">view</button>`];
    if (['pending', 'running'].includes(r.status)) {
      actions.push(`<button class="btn btn-sm btn-danger" onclick="stopRun('${esc(r.run_id)}')">stop</button>`);
    } else if (r.next < r.trials && !busy) {
      actions.push(`<button class="btn btn-sm btn-muted" onclick="resumeRun('${esc(r.run_id)}')">resume</button>`);
    }
    return `<tr${r.run_id === selectedRun ? ' class="chosen"' : ''}>
      <td>${esc(r.name)}<div class="muted">${esc(r.run_id)}</div></td>
      <td>${esc(r.provider)} / ${esc(r.model)}</td>
      <td>${esc(r.arms.join(' vs '))}</td>
      <td>${esc(r.status)}${r.active ? ' ●' : ''}</td>
      <td>${Math.min(r.next, r.trials)}/${r.trials}</td>
      <td>${actions.join(' ')}</td></tr>`;
  });
  document.getElementById('runsTable').innerHTML =
    '<tr><th>Run</th><th>Provider / model</th><th>Arms</th><th>Status</th><th>Trials</th><th></th></tr>' +
    (rows.join('') || '<tr><td colspan="6" class="muted">No runs yet.</td></tr>');
}

async function startRun() {
  const arms = [...document.querySelectorAll('#armChecks input:checked')].map(i => i.value);
  const msg = document.getElementById('startMsg');
  try {
    const r = await api('/api/eval/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: document.getElementById('runName').value, arms,
                             repeats: +document.getElementById('repeats').value || 1 }),
    });
    msg.textContent = `started ${r.run_id}`;
    await loadRuns();
    selectRun(r.run_id);
  } catch (e) { msg.textContent = e.message; }
}

async function stopRun(id) {
  if (!confirm('Stop this run? The current trial is abandoned and the kiosk returns to its previous mode.')) return;
  await api(`/api/eval/${id}/stop`, { method: 'POST' });
  loadRuns();
}

async function resumeRun(id) {
  try { await api(`/api/eval/${id}/resume`, { method: 'POST' }); } catch (e) { alert(e.message); }
  loadRuns();
}

// ── Results ───────────────────────────────────────────────────────────────────
async function selectRun(id) {
  selectedRun = id;
  pairIdx = 0;
  await Promise.all([loadSummary(), loadPairs(), loadRuns()]);
}

async function loadSummary() {
  if (!selectedRun) return;
  const s = await api(`/api/eval/${selectedRun}/summary`);
  document.getElementById('resultsEmpty').style.display = 'none';
  document.getElementById('resultsBody').style.display = '';
  document.getElementById('resultsRun').textContent =
    `— ${s.plan.name} (${s.plan.trials_done}/${s.plan.trials} trials, ${s.plan.status})`;
  const cols = [
    ['n', 'n'], ['initial_pass_pct', 'draft pass %'], ['final_pass_pct', 'final pass %'],
    ['changed_from_draft_pct', 'changed %'], ['median_seconds', 'median s'], ['mean_responses', 'responses'],
    ['mean_tool_calls', 'tool calls'], ['mean_vision_calls', 'vision calls'], ['mean_output_chars', 'output chars'],
    ['mean_completion_tokens', 'tokens'], ['fallbacks', 'fallbacks'], ['budget_exhausted', 'budget out'],
    ['polish_kept', 'polish kept'], ['polish_rejected', 'polish rejected'], ['brief_ok_pct', 'brief ok %'],
  ];
  document.getElementById('armTable').innerHTML =
    '<tr><th>Arm</th>' + cols.map(c => `<th>${c[1]}</th>`).join('') + '</tr>' +
    s.plan.arms.map(a => {
      const m = s.arms[a] || {};
      return `<tr><td>${esc(a)}</td>` + cols.map(c => `<td>${fmt(m[c[0]])}</td>`).join('') + '</tr>';
    }).join('');
  const pref = Object.entries(s.preference);
  document.getElementById('prefTable').innerHTML =
    '<tr><th>Arm</th><th>vs</th><th>wins</th><th>losses</th><th>ties</th><th>both bad</th><th>identical</th><th>unrated</th></tr>' +
    pref.map(([a, p]) => `<tr><td>${esc(a)}</td><td>${esc(p.vs)}</td><td>${p.wins}</td><td>${p.losses}</td>
      <td>${p.ties}</td><td>${p.both_bad}</td><td>${p.identical}</td><td>${p.unrated}</td></tr>`).join('');
  document.getElementById('runNotes').innerHTML = (s.plan.notes || []).map(n => `<li>${esc(n)}</li>`).join('');
}

// ── Blind review ──────────────────────────────────────────────────────────────
async function loadPairs() {
  if (!selectedRun) return;
  const reveal = document.getElementById('reveal').checked ? '1' : '0';
  const data = await api(`/api/eval/${selectedRun}/pairs?reveal=${reveal}`);
  pairs = data.pairs.filter(p => !p.identical);
  if (document.getElementById('onlyUnrated').checked) pairs = pairs.filter(p => !p.rating);
  const identical = data.pairs.length - data.pairs.filter(p => !p.identical).length;
  document.getElementById('reviewEmpty').style.display = pairs.length ? 'none' : '';
  document.getElementById('reviewEmpty').textContent = data.pairs.length
    ? `Nothing to rate here (${identical} pair(s) produced identical demos and count as ties).`
    : 'Select a run with finished trials.';
  document.getElementById('reviewBody').style.display = pairs.length ? '' : 'none';
  pairIdx = Math.min(pairIdx, Math.max(0, pairs.length - 1));
  showPair();
}

function sideHtml(p, side) {
  const file = p[side];
  const arm = p.arms ? ` <span class="muted">(${esc(p.arms[side])})</span>` : '';
  const checked = p.rating?.[`brief_${side}`] ? 'checked' : '';
  const view = file
    ? `<iframe sandbox="allow-scripts" src="/eval/demo/${encodeURIComponent(selectedRun)}/${encodeURIComponent(file)}"></iframe>`
    : '<div class="missing">no demo — discarded by this arm</div>';
  return `<b>${side}</b>${arm}${view}
    <label class="check-row"><input type="checkbox" id="brief${side}" ${checked} ${file ? '' : 'disabled'}> satisfies the brief</label>`;
}

function showPair() {
  if (!pairs.length) return;
  const p = pairs[pairIdx];
  const rated = pairs.filter(x => x.rating).length;
  document.getElementById('pairPos').textContent = `pair ${pairIdx + 1} of ${pairs.length} · ${rated} rated`;
  document.getElementById('pairBrief').textContent = p.brief;
  document.getElementById('pairCategory').textContent = p.category ? `(${p.category}, ${p.trial})` : `(${p.trial})`;
  document.getElementById('sideA').innerHTML = sideHtml(p, 'A');
  document.getElementById('sideB').innerHTML = sideHtml(p, 'B');
  document.querySelectorAll('[data-choice]').forEach(b =>
    b.classList.toggle('chosen', p.rating?.choice === b.dataset.choice));
  document.getElementById('rateMsg').textContent = '';
}

function stepPair(d) {
  if (!pairs.length) return;
  pairIdx = (pairIdx + d + pairs.length) % pairs.length;
  showPair();
}

async function rate(choice) {
  const p = pairs[pairIdx];
  const briefA = p.A ? document.getElementById('briefA').checked : null;
  const briefB = p.B ? document.getElementById('briefB').checked : null;
  try {
    await api(`/api/eval/${selectedRun}/rate`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: p.key, choice, brief_A: briefA, brief_B: briefB }),
    });
    p.rating = { choice, brief_A: briefA, brief_B: briefB };
    loadSummary();
    if (pairIdx < pairs.length - 1) stepPair(1); else showPair();
  } catch (e) { document.getElementById('rateMsg').textContent = e.message; }
}

// ── Live metrics ──────────────────────────────────────────────────────────────
async function loadLive() {
  const data = await api('/api/metrics/live');
  const entries = Object.entries(data.strategies);
  document.getElementById('liveTable').innerHTML =
    '<tr><th>Strategy</th><th>n</th><th>draft pass %</th><th>final pass %</th><th>median s</th><th>responses</th><th>tool calls</th><th>output chars</th><th>fallbacks</th></tr>' +
    (entries.map(([k, m]) => `<tr><td>${esc(k)}</td><td>${m.n}</td><td>${fmt(m.initial_pass_pct)}</td>
      <td>${fmt(m.final_pass_pct)}</td><td>${fmt(m.median_seconds)}</td><td>${fmt(m.mean_responses)}</td>
      <td>${fmt(m.mean_tool_calls)}</td><td>${fmt(m.mean_output_chars)}</td><td>${m.fallbacks}</td></tr>`).join('')
     || '<tr><td colspan="9" class="muted">No live cycles recorded yet.</td></tr>');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  await loadRuns();
  const active = runs.find(r => r.active) || runs[0];
  if (active) selectRun(active.run_id);
  loadLive();
  setInterval(async () => {
    await loadRuns();
    if (runs.find(r => r.run_id === selectedRun && ['pending', 'running'].includes(r.status))) loadSummary();
  }, 10000);
})();
