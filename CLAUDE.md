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
 ├── /ws                            → WebSocket (push state to display; receive audit_result, probe_keydown)
 └── /api/*                         → config, models, prompts, logs, skip, archive DELETE

Orchestrator (daemon thread)
 └── state machine: IDLE → PICK_MODE → GENERATE → VALIDATE → DISPLAY → ARCHIVE → IDLE
                                            ↑           ↓
                                         REPAIR ←──────┘ (up to N attempts)
```

### Key files

| File | Role |
|---|---|
| `app.py` | Flask entry point; WebSocket registry; route wiring |
| `orchestrator.py` | State machine; calls lm_client, archive, validator |
| `lm_client.py` | LM Studio API: `list_models()`, `chat_stream()` (streaming SSE) |
| `validator.py` | Probe script injection into demo HTML; fence-stripping; validation sync primitives |
| `archive.py` | Save/version/list/prune/delete archived demos; `archive_index.json` |
| `config.py` | `load()` / `save()` with deep-merge defaults; atomic write |
| `logging_setup.py` | Rotating file handler (5 MB × 5) + stdout |
| `download_vendor.py` | One-shot script to fetch three.js into `static/three/` |
| `prompts.csv` | 26+ effect specs fed to LLM; columns: `Effect`, `Three.js prompt` |

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
z=1   <iframe id="demoFrame">   always rendering; opacity:0 during mock-OS
z=2   #mockOS overlay           grid: title bar / editor / status bar; opacity:0 during DISPLAY
z=3   #idleOverlay              shown only on startup
z=20  #deleteConfirm            delete confirmation dialog; shown on 'd' keypress in replay mode
```

State transitions (sent via WebSocket `{type:"state", state:"GENERATE", ...}`) control which layers are visible.

On reconnect, the server replays `orc.last_state_msg` (the full last state dict including URL) so the display page re-syncs correctly without needing another orchestrator cycle.

### Keyboard shortcuts (display page)

| Key | Effect | Condition |
|---|---|---|
| `d` | Show delete confirmation overlay | Replay mode only (archive file showing) |
| `Escape` | Dismiss delete confirmation | Overlay open |

Keys are captured via both the parent `document.keydown` listener and the probe's `probe_keydown` relay (handles iframe focus stealing).

### Config

`config.json` is created on first run from `DEFAULTS` in `config.py`. Deep-merged on load so new keys added to DEFAULTS propagate automatically. Atomic write (`.tmp` rename). The orchestrator re-reads config at the top of each cycle.

### Modes

- **generate** (default): each cycle picks `stock` / `update` / `creative` by weighted random
  - `stock`: picks a row from `prompts.csv`, sends it to LLM
  - `update`: picks a random archive file within token budget, asks LLM to restyle it
  - `creative`: sends entire CSV to LLM, asks for something new
- **replay**: picks random `.html` from `archive/`, displays directly, no LLM. Switching modes via the config GUI immediately saves config and calls `/api/skip` to cut the current cycle short.

### Archive filenames

```
{YYYY-MM-DD}_{effect_slug}_{4-char-md5}.html   # stock / creative
{source_basename}_v{N}.html                     # update (chains from source)
```

---

## html/ — standalone demos

No build, no server. Open `.html` directly in browser. Three.js loads from CDN via importmap. Each file: OrthographicCamera + full-screen PlaneGeometry quad + ShaderMaterial. Fragment shader does all the work. Uniforms: `u_time`, `u_resolution`.
