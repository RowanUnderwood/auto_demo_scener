# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Two things coexist here:

1. **`html/`** — standalone hand-coded Three.js effect demos (reference implementations, no server needed)
2. **`ai_demoscener/`** — the main application: a self-running kiosk that uses a local LLM to generate new Three.js demos in a loop

---

## ai_demoscener — running

**Windows:** double-click `launch.bat` — downloads three.js if missing, starts server, opens browser.

**Manual:**
```
cd ai_demoscener
python download_vendor.py   # one-time: downloads three.js into static/three/
python app.py --launch      # starts server on :8080, opens display in browser
```

- Display page: `http://localhost:8080/display`
- Config GUI:   `http://localhost:8080/config`

Dependencies (already available): `flask`, `flask-sock`, `requests`

`ffmpeg` on PATH is required only for iterative visual review with **video** input (Ninfer only; transcodes the browser's recording to H.264 MP4 before sending it to the vision model). Not a pip dependency — install via the system package manager (`sudo apt install ffmpeg`). Everything else runs fine without it; missing `ffmpeg` just makes `_transcode_for_vision()` log a warning, and visual review is skipped for that demo.

---

## ai_demoscener — architecture

### Process model

One Python process. The Flask server handles HTTP + WebSocket. The orchestrator runs in a background daemon thread. State is broadcast to all connected display WebSocket clients.

```
Flask (main thread)
 ├── /display, /config              → serve static HTML
 ├── /temp/<f>, /archive/<f>        → serve demo HTML with probe injection
 ├── /ws                            → WebSocket (push state to display; receive audit_result)
 └── /api/*                         → config, models, prompts, logs, skip, discard, archive DELETE,
                                       failure_rate, title_cards

Orchestrator (daemon thread)
 └── state machine:
       IDLE → PICK_MODE → GENERATE → VALIDATE → DISPLAY ──→ ARCHIVE → IDLE
                               ↑          ↓          │
                            REPAIR ←──────┘          └─→ IDLE  (user pressed 'd' + confirmed)

     generation_strategy = "iterative" replaces VALIDATE/REPAIR above with:
       GENERATE (classic stream = revision 1) → ITERATE (check → model turn → edits → check …)
                                              → DISPLAY → ARCHIVE → IDLE
```

### Key files

| File | Role |
|---|---|
| `app.py` | Flask entry point; WebSocket registry; all route wiring |
| `orchestrator.py` | State machine; LLM calls; title/meta generation; discard logic |
| `lm_client.py` | OpenAI-compatible client shared by all providers: `list_models()`, `chat()`, `chat_stream()` (the latter two accept multimodal `content` lists unmodified, and an `extra_params` kwarg for provider-specific fields like `reasoning_effort`), and `chat_collect()` — a streamed request collected into `{content, tool_calls, finish_reason, usage, aborted}`, assembling tool-call argument fragments by `index`; raises `LMHTTPError` (with `.status`) on HTTP errors |
| `validator.py` | Probe script injection into demo HTML; fence-stripping; validation sync primitives; `format_diagnostics()` |
| `agent_runner.py` | Iterative strategy: `IterativeSession` bounded model/tool loop with injected hooks (LLM, run, display), budgets, stopping rules |
| `demo_tools.py` | Iterative strategy: `SourceStore` (revisions, exact/whitespace-tolerant unique edits), tool schemas, native/JSON action parsing |
| `metrics.py` | Comparable per-cycle metrics rows (JSONL) and `summarize()`; live cycles → `session_metrics.jsonl` |
| `evaluation.py` | Paired evaluation runs: plans, results, outputs and blind A/B ratings under `eval/<run_id>/`; `summary()` |
| `static/eval.html` / `eval.js` | `/eval` page: start/stop/resume runs, per-arm results, blind review, live per-strategy metrics |
| `tests/` | stdlib `unittest` tests: `test_iterative.py` (session loop, tools, visual review), `test_validation.py` (reply correlation, timeouts, brief-preserving repairs), `test_evaluation.py` (metrics, runs, blind pairing, evaluate cycle). Run from `ai_demoscener/`: `python3 -m unittest discover tests` |
| `archive.py` | Save/version/list/prune/delete archived demos; `archive_index.json` |
| `stats.py` | Run/failure/deletion tracking per effect+model; `load()`, `save()`, `record_run()`, `record_failure()`, `record_deletion()` → `prompt_stats.json` |
| `config.py` | `load()` / `save()` with deep-merge defaults; atomic write |
| `logging_setup.py` | Rotating file handler (5 MB × 5) + stdout |
| `download_vendor.py` | One-shot script to fetch three.js into `static/three/` |
| `prompts.csv` | 83 effect specs fed to LLM; columns: `Effect`, `Three.js prompt`. Rewritten 2026-09-05 — see note under "Stats tracking" |
| `archive/index.html` | Self-contained static archive player (no backend); reads `archive_index.json`; hostable on GitHub Pages |

### Validation flow (the tricky part)

0. Display pages register by sending `{type:"hello", role:"display"}` on WebSocket connect (`app.py` keeps `_display_clients`; the config page shares `/ws` but never registers). With no display registered, `_generate_cycle` pauses in `_wait_for_display()` before spending an LLM call (status `WAITING FOR DISPLAY`), since nothing could validate the result.
1. Orchestrator saves generated HTML to `temp/`, broadcasts `{type:"audition", url:"/temp/<f>?aid=<id>", seconds, audition_id}` to display pages
2. Display page loads the URL in a hidden iframe (iframe is always full-viewport; mock-OS overlay covers it). The later `DISPLAY` state compares paths without the query string, so the audited iframe keeps running rather than reloading.
3. Flask serves `/temp/<f>` by injecting `validator.PROBE_JS` into `<head>`. The probe patches WebGL draw calls, reports every error (bounded: `window.onerror` with stack, `unhandledrejection`, `webglcontextlost`, and `console.error` lines from three.js containing `THREE.WebGLProgram`/`Shader Error`, which is how three.js reports shader compile failures — it doesn't throw), relays other `console.error`/`warn` as evidence, and heartbeats via `postMessage`. Every message carries the `aid` read from `location.search`; the display page ignores messages from an earlier iframe load. Error locations are rewritten to the demo's own line numbers (`inject_probe()` substitutes the number of lines it injected for `__PROBE_LINE_OFFSET__`).
4. Display page collects postMessages for `audition_seconds` (on the first error it waits a further 750 ms for follow-on errors, then concludes), then sends `{type:"audit_result", audition_id, result:"OK"/"CRASHED"/"BLANK", error, diagnostics}` back over WebSocket. `diagnostics` = `{errors[{category,error}], console[], heartbeats, heartbeat_fps, render_fps, draws_per_sec, visible_cells, grid_cells, frozen_seconds, context_lost}`. `heartbeat_fps` is the browser's rAF rate; `render_fps` counts only frames in which the demo issued draw calls. Visibility samples an 8×8 grid; when every grid sample is dark the probe also reads the whole frame once (only in the first 20 s after load, the audition window, to keep it off long displays) and reports `anyLit`, and BLANK requires that to be false too. This keeps sparse scenes, like a small sun on black, from failing at random. `frozen_seconds` is the longest run of heartbeats with an unchanged sample signature (evidence only — never a failure by itself).
5. `validator.receive_result()` unblocks the orchestrator's `wait_for_result()` call — but only for a result whose `audition_id` matches the pending one (first match wins; late replies and duplicates from a second display tab are ignored). Recordings use the same pattern keyed by `record_id`, and `/api/upload_recording` rejects uploads for any record ID that isn't pending.
6. A BLANK with zero heartbeats and no errors means the page never ran: the probe heartbeats from its own frame loop whatever the demo does. `_audition_with_retry()` re-runs it once before treating it as a real result, so a load hiccup isn't sent for repair; seen once on the kiosk in an evaluation run.
7. No reply within `audition_seconds + 15` → `{"result":"TIMEOUT"}`, which is an infrastructure problem, never a pass and never a repair trigger: `_validate_and_repair()` waits for a display if none is connected, otherwise retries once and then gives up with `kind=None` (DISCARD with no error overlay, nothing archived, nothing recorded in stats).

**Repairs keep the brief.** `_build_brief()` produces a compact statement of the cycle's request (stock: effect name + CSV prompt; creative: the first draft's `<title>` as the concept; update: the source filename/title, never its full source). `_do_repair()` keeps the user-editable `repair` system prompt (`{error}` placeholder) and sends a user message of: original brief → earlier failed attempts → `_format_diagnostics()` text → current HTML.

The probe also relays `keydown` events from inside the iframe to the parent via `postMessage({type:"probe_keydown", key})`. This is necessary because the iframe steals focus from the parent document when a WebGL demo loads, which would otherwise block keyboard shortcuts.

### Importmap override (CDN → local)

`app.py:_three_importmap()` builds a dict mapping common CDN URLs for three.js to `/static/three/three.module.js`. `validator.inject_probe()` merges this into any existing importmap in the AI's HTML, or injects a new `<script type="importmap">` tag. Caveats: the demo's own entries win the merge, and import maps don't chain, so a demo that maps `"three"` to a CDN URL (most of the archive) still loads three.js from the CDN — the local copy is used for bare `'three'` imports without their own mapping. Because the demo iframe is sandboxed (opaque `null` origin), module imports of the local copy are cross-origin; `app.py:_cors_for_vendored_three` adds `Access-Control-Allow-Origin: *` to `/static/three/` responses so they aren't blocked.

### Display page layers

```
z=1   <iframe id="demoFrame">     always rendering; opacity:0 during mock-OS
z=2   #mockOS overlay             grid: title bar / editor / status bar; opacity:0 during DISPLAY
z=3   #idleOverlay                shown only on startup
z=5   #titleCard                  brief overlay at start of DISPLAY (title, desc, model byline)
z=6   #titleCardStats             persistent lower-left stats overlay during entire DISPLAY phase
z=6   #progressBar                sweeps across bottom during DISPLAY
z=20  #deleteConfirm              delete confirmation dialog; shown on 'd' keypress
z=21  #errorOverlay               crash/blank overlay; auto-dismisses after 5s countdown
```

The error overlay appears on `DISCARD` state messages that include a `kind` field (`"CRASHED"` or `"BLANK"`). LLM-failure DISCARDs (no `kind`) show plain status text only.

Only stock-mode validation failures are recorded in `prompt_stats.json` (crashes can't be attributed to a specific prompt for creative/update modes).

State transitions (sent via WebSocket `{type:"state", state:"GENERATE", ...}`) control which layers are visible. The GENERATE state now includes a `model` field so the status bar can show the active model immediately.

On reconnect, the server replays `orc.last_state_msg` (the full last state dict including URL) so the display page re-syncs correctly without needing another orchestrator cycle.

### Title / meta generation

`orchestrator._generate_title_meta(cfg, html, model="")` makes a non-streaming LLM call that returns `{"title": "...", "description": "..."}`. The system prompt instructs the model to output only valid JSON (no markdown fences). The response parser extracts the first `{` to last `}` to handle models that wrap JSON in fences anyway.

Two modes controlled by `cfg.display.jit_titling`:

- **JIT ON** — `_generate_title_meta` runs synchronously before `_tx("DISPLAY")`. If the LM call fails, title falls back to the effect name. In replay mode the result is also written to `archive_index.json` for future cycles.
- **JIT OFF** (default) — a background thread runs during the display timer; `meta_thread.join(timeout=10)` after the timer so the result is captured before archiving. In replay mode a daemon thread enriches the archive entry without blocking display.

In replay mode, `entry_model` from `archive_index.json` is passed directly to `_generate_title_meta`; if that model is no longer available on the active provider, the LM client raises `LMClientError` which is caught and returns `None` — graceful degradation.

### LLM providers (LM Studio / Ninfer / Strata)

`cfg["llm_provider"]` (`"lm_studio"` default, `"ninfer"`, or `"strata"`) selects which of the `cfg["lm_studio"]` / `cfg["ninfer"]` / `cfg["strata"]` sub-dicts is active. `orchestrator._active_provider_cfg(cfg) -> (provider, provider_cfg)` is the single lookup point — every call site that used to read `cfg["lm_studio"]` directly (`_probe_lm`, the Surprise-Me block, `_stream`, `_generate_title_meta`, `_pick_mode`, `_csv_text_for_creative`) goes through it instead. `lm_client.list_models()`/`chat()`/`chat_stream()` are provider-agnostic (all three providers expose the same OpenAI-compatible `/v1/models` and `/v1/chat/completions` shape) — only the `base_url` and optional `api_key` passed in differ. `lm_client._root()` strips a trailing `/v1` from `base_url` (Strata documents its base URL that way), and `api_key`, when non-empty, is sent as `Authorization: Bearer <key>`. Only Strata has an `api_key` config field; every call site passes `provider_cfg.get("api_key", "")`, so it's a no-op for the other two.

**Thinking effort** (`cfg["ninfer"]["thinking_effort"]`, one of `none`/`low`/`medium`/`xhigh`) applies to Ninfer and Strata (`cfg["strata"]["thinking_effort"]`, one of `none`/`low`/`medium`/`high`): `_stream` passes it as `extra_params={"reasoning_effort": ...}` to `chat_stream`, which merges it into the request JSON via `lm_client.chat`/`chat_stream`'s `extra_params` kwarg (a no-op on LM Studio, which never sets it). `_generate_title_meta` always hardcodes `reasoning_effort="none"` on the Ninfer/Strata branch regardless of the configured value — title/description generation is cheap and low-stakes, mirroring the existing `"/no_think\n"` prompt-prefix convention already used on the LM Studio branch for the same reason.

**Video capture** (used only by iterative visual review with `vision_input: "video"`, Ninfer only — the config page disables the option for other providers, and the orchestrator falls back to frames if it's set anyway). The classic Ninfer "improve demo via video check" pass was removed in favour of visual review, which adds the before/after regression check; `ninfer.video_check_enabled`, `ninfer.video_max_tokens` and `system_prompts.video_improve` are gone (stale keys in an existing `config.json` are ignored).

1. `orchestrator._capture_video()` calls `_record_video()`, which broadcasts `{type:"record_video", seconds, fps:2, record_id}`; `display.js` relays this into the sandboxed demo iframe via `demoFrame.contentWindow.postMessage(...)` (the iframe is `sandbox="allow-scripts"` with no `allow-same-origin`, so `postMessage` is the only channel in or out).
2. The probe (`validator.PROBE_JS`) listens for `start_recording`, finds the demo's canvas via its `_glCtxs` array, and records with `canvas.captureStream(2)` + `MediaRecorder` at 2 fps. On stop it `fetch()`es the blob directly to `POST /api/upload_recording` from inside the iframe. That route needs explicit CORS headers (and handles the browser's preflight `OPTIONS` itself), because the iframe's opaque sandboxed origin makes this same-app request cross-origin. It accepts only the pending `record_id`.
3. The route calls `validator.receive_recording(...)`, unblocking `validator.wait_for_recording()`. `_transcode_for_vision()` then shells out to `ffmpeg` to produce a 768px-long-edge, H.264, no-audio MP4, sent as a `video_url` content part.

### Iterative strategy (`generation_strategy: "iterative"`)

Milestone 2 of `auto_demo_scener_iterative_plan.md`. The first draft is still streamed the classic way (typing animation intact); `orchestrator._iterative_session()` then hands it to `agent_runner.IterativeSession` as revision 1.

- **Check first.** Revision 1 is run. If it passes and its diagnostics look clean, the session ends with zero model turns (`first_draft_ok`) — iterative costs nothing extra for good drafts. A passing revision with suspicious diagnostics (output frozen for the whole run, or console warnings) earns the model exactly one look; it may fix or `finish_demo`. "Mostly dark" (under 10% of sampled cells lit) and "renders on under half the browser's frames" are passed only to the visual critique (`_attention_reasons(visual_hints=True)`); in the first evaluation a text-only model edited a working sparse scene blind on the "mostly dark" signal. "Frozen" is grid-based too and fires on sparse scenes, but its one rated look in evaluation was a win, so it stays a text-only trigger for now.
- **Stateless turns.** Every request is rebuilt from session state: system prompt (`system_prompts.iterative`, plus JSON-action format instructions in JSON mode) and one user message with the brief, status/budget, change log, latest check of the current revision (`validator.format_diagnostics`), feedback on the previous response's actions, and the current source with `NNNN| ` line numbers. No transcript grows, so tool results never need threading back into a conversation.
- **Tools** (`demo_tools.TOOLS`): `edit_demo(old_text, new_text, change_summary)`, `write_demo(html, change_summary)`, `run_demo(seconds?)`, `finish_demo(summary)`. No revision arguments: turns are serial and always act on the current revision. Edits need a unique match (exact, else whitespace-tolerant); a failed edit skips the rest of that response. The app runs the demo after any response that changed the source; `finish_demo` is accepted only if the current revision's latest check passed (a revision changed in the same response is checked first).
- **Interaction** (`iterative.interaction`): `native_tools`, `json_actions`, or `auto` (native tools; JSON parsed from the content if the model replies in text; switches to JSON actions for the rest of the session if the server answers the `tools` request with a 4xx).
- **Stopping:** finished; budget (`max_turns`, `max_tool_calls`, `session_deadline_seconds`); `stuck` (same failure 3 checks in a row); `format_error` (two unusable responses in a row — truncated at `length`, no parseable actions); `llm_error`; `cancelled` (skip/stop, polled during streaming too); `infra` (no display could run a check). Anything but cancelled falls back to the latest passing revision; that fallback is re-checked before display (it loads the exact source into the iframe and ties validation to it).
- **Display:** `ITERATE` state (`text`, `busy`) drives status/spinner without resetting the editor; `{type:"source", html, rev, lines, summary}` replaces the editor content, highlights the changed lines in the gutter and scrolls to them.
- **Records:** archive entries get `strategy` and, for iterative, `session: {outcome, turns, tool_calls, revisions, final_revision, interaction}`. Stats failures are recorded as for classic (the session's last failed check), so per-effect failure rates currently mix strategies — Milestone 4 adds separate comparative metrics.

**Visual review** (`iterative.visual_review`, Milestone 3; needs a model that accepts image input, or video on Ninfer):

- When a revision passes in the repair phase (and turns remain), `_start_visual_review()` captures it and makes a tool-free multimodal request with `system_prompts.visual_critique`, expecting `{"issues": [{problem, evidence, change}]}` (at most 3 kept). No issues → finish with that revision.
- Issues switch the session to the `polish` phase: the baseline revision is remembered, and ordinary edit turns get a `VISUAL REVIEW` section listing the issues (critique and editing stay separate; edit turns never carry images). `finish_demo` without edits keeps the baseline.
- When a polished revision passes its runtime check, `_compare_polish()` captures it and sends `system_prompts.visual_compare` the BEFORE and AFTER captures, expecting `{"choice": "after"|"before", improved, regressions, reason}`. Anything but `"after"` keeps the baseline. If the polish never passes (budget, stuck…), the baseline is kept. With `max_polish_rounds` > 1 a rejected polish is reverted (new revision with the baseline source) before the next round.
- `finish_demo` no longer ends the session directly: it sets `finish_requested`, and `_on_passing()` decides what a passing revision still needs. Passing revisions are handled before the budget check, so a polish that passes on the last turn still gets compared; a new critique only starts if turns remain.
- Vision calls don't count as turns (recorded as `vision_calls`). Any capture failure, HTTP rejection (e.g. Strata started without its vision encoder) or unusable reply disables visual review for the rest of the session and keeps the current passing revision.
- Capture (`orchestrator._capture_frames`): `{type:"capture_frames", capture_id, count, interval_ms, max_px}` → `display.js` relays it into the iframe → the probe draws the most recent WebGL canvas into a scaled 2D canvas every `interval_ms` and posts JPEG data URLs (`probe_frame`, then `probe_capture_done` with the canvas count) → `display.js` returns `{type:"capture_result", capture_id, frames, canvases|error}` over the WebSocket → `validator.receive_capture()`. Frames are taken from the revision still running in the hidden iframe after its check, so their timestamps start about 5 s after load (a slow opening isn't visible). Multiple WebGL canvases are noted in the capture description, since only the last is captured. With `vision_input: "video"` on Ninfer, `_capture_video()` reuses `_record_video()` + `_transcode_for_vision()` instead.
- `validator._PendingReply` is the shared ID-correlated request/reply waiter behind auditions, recordings and captures.
- The session record (`session.visual`) keeps every round's issues, compare result and decision, for later review in `archive_index.json`.

### Metrics and evaluation (Milestone 4)

**Metrics.** `orchestrator._generate_draft()` and `_run_strategy()` wrap draft generation and the strategy run (classic `_validate_and_repair` or `_iterative_session`) with `_acct_begin()` accounting: LLM calls, output characters, completion tokens (only when the server reports `usage`; `chat_stream(stats=)` and `chat_collect` capture it), audition results (`_audition` appends each), and classic repairs. `_run_strategy()` returns comparable fields (see `metrics.py`'s docstring): initial and final result, outcome, responses (classic repairs or iterative turns), tool and vision calls, fallback, budget exhaustion, changed-from-draft, visual decisions. Live generate cycles append a row to `session_metrics.jsonl` (`run: "live"`, `arm` = strategy, plus `archived`/`user_discarded`). These are deliberately separate from `prompt_stats.json`, since validation changes alter what a pass means.

**Evaluation runs** (`boot_mode: "evaluate"`, started from `/eval`). `evaluation.create_run()` writes `eval/<run_id>/plan.json` (provider and model snapshot, arms as config overrides from `ARM_PRESETS`, trials from `SMOKE_TRIALS` × repeats; an update trial's source is fixed at creation) and points `eval/active.json` at it, and `/api/eval/start` switches `boot_mode` to `evaluate`. `orchestrator._eval_cycle()` then runs one trial per cycle: it writes **one shared first draft** (`<trial>_draft.html`), runs every arm on that same draft via `_run_strategy()` with the arm's overrides deep-merged into the config, writes each arm's final demo as `<trial>_<arm>.html` (never archived, no DISPLAY phase), and appends a metrics row to `results.jsonl`. `results()` keeps the latest row per (trial, arm), because a stopped trial is rerun from scratch on resume. Skip abandons the current trial with a note; stop (`/api/eval/<run>/stop`) marks the run stopped. When the run is done or stopped, `_finish_eval()` clears the active pointer and restores the `boot_mode` it started from. The config page keeps `boot_mode` as `evaluate` while a run owns it.

- Arms: `classic`; `classic_matched` (classic with `max_repair_attempts` = `iterative.max_turns`, the matched-budget condition); `iterative` (visual review off); `iterative_visual`. The first arm is the baseline.
- Blind review: `pairs()` makes one pair per (trial, non-baseline arm) against the baseline. A/B order is fixed by a hash of the run, trial and arm, and the mapping is revealed only with `?reveal=1`. Byte-identical outputs (both arms accepted the draft unchanged) are auto-ties and aren't shown. Ratings (`A`/`B`/`tie`/`both_bad`, plus per-side "satisfies the brief") go to `ratings.json`. `summary()` combines `metrics.summarize()` per arm with wins/losses/ties against the baseline and brief-satisfaction rates.
- Eval demos are served by `/eval/demo/<run>/<file>` (probe-injected, so the vendored three.js import map applies); filenames are restricted to `tNN_<arm>.html`.

### Delete / discard

**Replay mode** — the archive file already exists. Pressing `d` + confirming calls `DELETE /api/archive/<filename>`, which deletes the file, updates `archive_index.json`, calls `stats_mod.record_deletion()`, and calls `orc.skip()` to advance to the next demo.

**Generate mode** — the demo has passed validation and is showing from `/temp/`. Pressing `d` + confirming calls `POST /api/discard_current`, which sets `orc._discard_current` and `orc._skip`. After `_run_timer` returns, the orchestrator checks `_discard_current.is_set()`, deletes the temp file, broadcasts `IDLE`, and returns **without archiving**. The `_discard_current` flag is always cleared at the top of each `_generate_cycle` to prevent stale state.

### Stats tracking

`prompt_stats.json` structure:
```json
{
  "effect_or_mode_key": {
    "runs": 0, "crashes": 0, "blanks": 0, "total": 0,
    "deletions": 0, "last_fail": null,
    "by_model": {
      "model-id": { "runs": 0, "crashes": 0, "blanks": 0, "total": 0, "deletions": 0 }
    }
  }
}
```

Keys: effect name (stock), `"creative"`, `"update"`. `record_run()` increments `runs`; `record_failure(effect, kind, model="")` increments `crashes` or `blanks` and `total`; `record_deletion(effect, model="")` increments `deletions`. All three update the `by_model` sub-dict.

**2026-09-05 prompts.csv rewrite — benchmark discontinuity.** `prompts.csv` was substantially rewritten to add explicit setup/numerical detail (camera framing, robust smooth-min formulas, ping-pong render-target recipes, perf caps on per-frame loop counts) that weaker local models were getting wrong, while keeping aesthetic choices (palettes, exact shapes/patterns) open-ended. Pre-rewrite `prompt_stats.json` was snapshotted to `prompt_stats_pre_2026-09-05.json` and the live file was reset to `{}`. **Failure-rate numbers from before this date are not comparable to numbers from after it** — the prompt text an effect name refers to changed, so an "effect" in the old snapshot and the same-named effect going forward are different specs with different difficulty.

### Keyboard shortcuts (display page)

| Key | Effect | Condition |
|---|---|---|
| `d` | Show delete confirmation overlay | Any demo playing (archive **or** temp file) |
| `Escape` | Dismiss delete confirmation | Overlay open |

Keys are captured via both the parent `document.keydown` listener and the probe's `probe_keydown` relay (handles iframe focus stealing). The delete dialog shows the archive filename (replay) or the effect title/name (generate mode).

### Keyboard shortcuts (static archive player)

| Key | Effect |
|---|---|
| `→` / `Space` | Next demo |
| `←` | Previous demo |

Same probe_keydown relay used — the static player also listens for `postMessage({type:"probe_keydown"})` from inside the iframe.

### Config

`config.json` is created on first run from `DEFAULTS` in `config.py`. Deep-merged on load so new keys added to DEFAULTS propagate automatically. Atomic write (`.tmp` rename). The orchestrator re-reads config at the top of each cycle.

Key display defaults:
```python
"display": {
    "demo_runtime_seconds": 60,
    "palette": "amber_crt",
    "show_title_card": True,
    "title_card_seconds": 4,
    "show_title_card_stats": True,
    "title_card_stats_font_size": 11,   # px; applied dynamically via display.js
    "show_progress_bar": True,
    "jit_titling": False,
    ...
}
```

Strategy config: `generation_strategy` (`"classic"` default | `"iterative"`) and `iterative: {interaction, max_turns, max_tool_calls, session_deadline_seconds, max_run_seconds, visual_review, vision_input, capture_frames, capture_interval_ms, capture_max_px, video_capture_seconds, max_polish_rounds}`; prompts are `system_prompts.iterative`, `visual_critique` and `visual_compare`.

Provider config: `llm_provider` (`"lm_studio"` | `"ninfer"` | `"strata"`) plus a full sibling config dict per provider (`lm_studio` unchanged; `ninfer` adds `thinking_effort` on top of the same base_url/model/max_tokens/temperature/request_timeout_seconds/context_ceiling_tokens/surprise_me fields; `strata` adds `api_key` and `thinking_effort`, with no video-improve because Strata can't take video input). All three dicts are always present in `DEFAULTS` regardless of which is active, so switching providers in the config GUI never loses the other provider's settings.

### Modes

- **generate** (default): each cycle picks `stock` / `update` / `creative` by weighted random
  - `stock`: picks a row from `prompts.csv`, sends it to LLM
  - `update`: picks a random archive file within token budget, asks LLM to restyle it. Guard: `max_chars = max(4096, max_input_tokens) * 4` prevents zero/negative budget when `max_tokens ≥ context_ceiling`. Falls back to stock with a WARNING log if no candidates found.
  - `creative`: sends entire CSV to LLM, asks for something new
- **replay**: picks random `.html` from `archive/`, displays directly, no LLM. Switching modes via the config GUI immediately saves config and calls `/api/skip` to cut the current cycle short.
- **Surprise Me**: if the active provider's `surprise_me` is true, a random model is selected each cycle via `list_models()` and broadcast as `{type:"model_selected", model}`.

### Archive filenames

```
{YYYY-MM-DD}_{effect_slug}_{4-char-md5}.html   # stock / creative
{source_basename}_v{N}.html                     # update (chains from source)
```

`archive_index.json` stores per-file metadata: `created`, `mode`, `effect`, `model`, `title`, `description`, `passed_validation`.

### API routes (selected)

| Method | Path | Description |
|---|---|---|
| POST | `/api/skip` | Cut current cycle short |
| POST | `/api/discard_current` | Discard generate-mode demo before archiving |
| DELETE | `/api/archive/<f>` | Delete archive file + record deletion in stats |
| GET | `/api/failure_rate` | Returns `{effects, modes, models}` tables |
| POST | `/api/failure_rate/clear` | Wipe `prompt_stats.json` |
| GET | `/api/title_cards` | All archive entries (for batch title management) |
| POST | `/api/title_cards` | Save edited titles/descriptions |
| POST | `/api/title_cards/generate_all` | Start background batch title generation |
| GET | `/api/title_cards/progress` | Poll batch progress `{running, done, total, current}` |
| POST | `/api/title_cards/stop_generate` | Cancel batch generation |
| POST | `/api/title_cards/clear_all` | Strip all titles/descriptions from archive_index.json |
| GET | `/api/eval/runs` | Evaluation runs, available arms and the smoke trial set |
| POST | `/api/eval/start` | `{name, arms, repeats}` → create a paired run and switch `boot_mode` to `evaluate` |
| POST | `/api/eval/<run>/stop` · `/resume` | Stop a run (restores the previous boot mode) / resume a stopped one |
| GET | `/api/eval/<run>/summary` · `/pairs?reveal=` | Per-arm metrics and preferences / blind A/B pairs |
| POST | `/api/eval/<run>/rate` | `{key, choice: A\|B\|tie\|both_bad, brief_A, brief_B}` |
| GET | `/api/metrics/live` | Live generate-cycle metrics per strategy |
| GET | `/api/models?provider=` | List models for `lm_studio`, `ninfer` or `strata` (defaults to the active provider) |
| POST | `/api/upload_recording?record_id=` | Probe uploads a visual-review video recording (Ninfer, video input); unblocks `validator.wait_for_recording()` |

---

## html/ — standalone demos

No build, no server. Open `.html` directly in browser. Three.js loads from CDN via importmap. Each file: OrthographicCamera + full-screen PlaneGeometry quad + ShaderMaterial. Fragment shader does all the work. Uniforms: `u_time`, `u_resolution`.
