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
```

### Key files

| File | Role |
|---|---|
| `app.py` | Flask entry point; WebSocket registry; all route wiring |
| `orchestrator.py` | State machine; LLM calls; title/meta generation; discard logic |
| `lm_client.py` | LM Studio API: `list_models()`, `chat()`, `chat_stream()` |
| `validator.py` | Probe script injection into demo HTML; fence-stripping; validation sync primitives |
| `archive.py` | Save/version/list/prune/delete archived demos; `archive_index.json` |
| `stats.py` | Run/failure/deletion tracking per effect+model; `load()`, `save()`, `record_run()`, `record_failure()`, `record_deletion()` → `prompt_stats.json` |
| `config.py` | `load()` / `save()` with deep-merge defaults; atomic write |
| `logging_setup.py` | Rotating file handler (5 MB × 5) + stdout |
| `download_vendor.py` | One-shot script to fetch three.js into `static/three/` |
| `prompts.csv` | 83 effect specs fed to LLM; columns: `Effect`, `Three.js prompt` |
| `archive/index.html` | Self-contained static archive player (no backend); reads `archive_index.json`; hostable on GitHub Pages |

### Validation flow (the tricky part)

1. Orchestrator saves generated HTML to `temp/`, broadcasts `{type:"audition", url, seconds}` to display page
2. Display page loads the URL in a hidden iframe (iframe is always full-viewport; mock-OS overlay covers it)
3. Flask serves `/temp/<f>` by injecting `validator.PROBE_JS` into `<head>` — probe patches WebGL draw calls, catches errors, heartbeats via `postMessage`
4. Display page collects postMessages for `audition_seconds`, then sends `{type:"audit_result", result:"OK"/"CRASHED"/"BLANK", error}` back over WebSocket
5. `validator.receive_result()` unblocks the orchestrator's `wait_for_result()` call

The probe also relays `keydown` events from inside the iframe to the parent via `postMessage({type:"probe_keydown", key})`. This is necessary because the iframe steals focus from the parent document when a WebGL demo loads, which would otherwise block keyboard shortcuts.

### Importmap override (CDN → local)

`app.py:_three_importmap()` builds a dict mapping common CDN URLs for three.js to `/static/three/three.module.js`. `validator.inject_probe()` merges this into any existing importmap in the AI's HTML, or injects a new `<script type="importmap">` tag. This means AI-generated code with CDN imports works offline.

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

In replay mode, `entry_model` from `archive_index.json` is passed directly to `_generate_title_meta`; if that model is no longer available in LM Studio, the LM client raises `LMClientError` which is caught and returns `None` — graceful degradation.

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

### Modes

- **generate** (default): each cycle picks `stock` / `update` / `creative` by weighted random
  - `stock`: picks a row from `prompts.csv`, sends it to LLM
  - `update`: picks a random archive file within token budget, asks LLM to restyle it. Guard: `max_chars = max(4096, max_input_tokens) * 4` prevents zero/negative budget when `max_tokens ≥ context_ceiling`. Falls back to stock with a WARNING log if no candidates found.
  - `creative`: sends entire CSV to LLM, asks for something new
- **replay**: picks random `.html` from `archive/`, displays directly, no LLM. Switching modes via the config GUI immediately saves config and calls `/api/skip` to cut the current cycle short.
- **Surprise Me**: if `lm_studio.surprise_me` is true, a random model is selected each cycle via `list_models()` and broadcast as `{type:"model_selected", model}`.

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

---

## html/ — standalone demos

No build, no server. Open `.html` directly in browser. Three.js loads from CDN via importmap. Each file: OrthographicCamera + full-screen PlaneGeometry quad + ShaderMaterial. Fragment shader does all the work. Uniforms: `u_time`, `u_resolution`.
