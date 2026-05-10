# AI Demoscener — Development Plan

## 1. Goals & scope

A self-running creative coding station. A local LLM (LM Studio on a separate machine) writes single-file three.js demos; the system displays the act of writing as a stylized mock-OS scene, then runs the demo for a configurable duration, then loops. Successful demos are archived. A web-based config GUI controls everything. The system runs on Linux, Windows, and Raspberry Pi, and the Pi target is a zero-touch full-screen kiosk.

There are two top-level run modes selectable from the GUI and saveable as the boot default:

- **Generate** — for each cycle, randomly pick `creative`, `update`, or `stock` (sub-mode probabilities are configurable), generate, validate, display, archive.
- **Replay** — pick random files from the archive and display them in a loop. No generation, no LLM traffic.

## 2. Library and language choices

**Backend:** Python 3.11+ with **Flask** for the config GUI HTTP server and **flask-sock** (or `simple-websocket`) for the WebSocket channel that pushes state to the display page. Python is cross-platform, runs cleanly on the Pi, has a low-friction story for HTTP + JSON + file I/O, and matches your existing comfort zone.

**Generation orchestrator:** a single Python process running the state machine in a background thread. Talks to LM Studio over its OpenAI-compatible HTTP API (`/v1/models`, `/v1/chat/completions` with `stream: true`).

**Frontend (config GUI and main display):** plain HTML + CSS + vanilla JS, served by Flask. No React, no build step — keeps the Pi happy and the dependency surface tiny. The main display loads the AI-generated demo inside a sandboxed `<iframe>`.

**Visualization library for the demos themselves:** **three.js stays.** It's the right call for this project — AI models generate it well from training data, it covers both scene-graph effects and fullscreen shader effects (just render a `PlaneGeometry` quad with a `ShaderMaterial`), and the WebGL-on-Pi story is mature. We will *vendor* three.js locally rather than relying on a CDN, because the kiosk Pi may not have reliable internet, and CDN domains are a runtime dependency we don't want.

We won't use Babylon.js (heavier, less AI-trained), PixiJS (2D only), or raw WebGL (more boilerplate, more AI failure modes). One useful refinement: the prompt repo can include a "shader-only" tag so the AI knows when to reach for a single fragment shader vs. a full scene graph.

## 3. Architecture

```
┌─────────────────────────────────────┐         ┌──────────────────────┐
│  Display browser (Chromium kiosk)   │◀──WS────│  Flask backend       │
│  http://localhost:8080/display      │         │  (Python)            │
│   ├─ mock-OS editor view            │         │   ├─ config server   │
│   └─ <iframe sandbox> demo view     │         │   ├─ orchestrator    │
└─────────────────────────────────────┘         │   ├─ LM Studio client│
                                                │   └─ archive/log mgr │
┌─────────────────────────────────────┐         │                      │
│  Config browser (any device on LAN) │◀──HTTP──│                      │
│  http://localhost:8080/config       │         │                      │
└─────────────────────────────────────┘         └──────────┬───────────┘
                                                           │
                                                       HTTP│
                                                           ▼
                                                ┌──────────────────────┐
                                                │  LM Studio           │
                                                │  192.168.2.192:1234  │
                                                └──────────────────────┘
```

Everything runs from one Python process. The display page and the config page are siblings on the same Flask server. Display state (current mode, streaming tokens, current demo URL, retry count, status text) is pushed over WebSocket so the display always reflects what the orchestrator is doing.

## 4. Project layout

```
ai_demoscener/
├── app.py                 # Flask entry point, route wiring
├── orchestrator.py        # State machine, generation loop, mode logic
├── lm_client.py           # LM Studio API wrapper (models list, streaming chat)
├── validator.py           # Crash/blank detection helpers (server-side bits)
├── archive.py             # Save, version, list, prune archived demos
├── config.py              # Load/save config.json, defaults, schema migration
├── logging_setup.py       # Rotating file handler
├── prompts.csv            # The effect/prompt repository (editable in GUI)
├── config.json            # Persisted user config (created on first run)
├── debug.log              # Rolling log
├── static/
│   ├── three/three.min.js # Vendored
│   ├── display.html       # Main display page
│   ├── display.js
│   ├── display.css        # All palette CSS variables live here
│   ├── config.html        # Config GUI page
│   ├── config.js
│   └── config.css
└── archive/
    ├── 2026-05-10_plasma_a3f1.html
    ├── 2026-05-10_plasma_a3f1_v2.html
    └── ...
```

## 5. Configuration file (`config.json`)

Loaded at startup, hot-reloaded when the GUI saves. Atomic write (temp file + rename) so a crash mid-save can't corrupt it. Schema migration on load: any missing keys are filled with defaults so adding fields in future versions doesn't break existing installs.

```json
{
  "schema_version": 1,
  "boot_mode": "generate",
  "generate_mode_weights": {"creative": 1, "update": 1, "stock": 2},
  "lm_studio": {
    "base_url": "http://192.168.2.192:1234",
    "model": "",
    "max_tokens": 8192,
    "temperature": 0.9,
    "request_timeout_seconds": 180
  },
  "display": {
    "fullscreen": true,
    "demo_runtime_seconds": 60,
    "min_typing_seconds": 8,
    "palette": "amber_crt",
    "show_status_bar": true,
    "show_retry_messages": true
  },
  "validation": {
    "max_repair_attempts": 2,
    "blank_detection_seconds": 4,
    "min_fps": 5
  },
  "archive": {
    "directory": "archive",
    "max_files": 500
  },
  "system_prompts": {
    "creative": "You are an expert creative coder. Based on the attached list of stock effects, invent a brand new demoscene idea. Be creative. Output ONLY a single self-contained HTML file using three.js. No commentary, no markdown fences.",
    "stock":    "You are an expert creative coder. Implement the requested effect. Output ONLY a single self-contained HTML file using three.js. No commentary, no markdown fences.",
    "update":   "You are an expert creative coder. Below is an existing demo. Produce a fresh new take on the same concept — different aesthetic, different parameters, different feel — while staying faithful to the core idea. Output ONLY a single self-contained HTML file using three.js. No commentary, no markdown fences.",
    "repair":   "The HTML below failed to run. The error or symptom is: {error}. Fix the file and output the corrected complete HTML. Output ONLY the HTML, no commentary, no markdown fences."
  }
}
```

All `system_prompts` strings are editable in the GUI. The `generate_mode_weights` are integer weights for weighted random selection (0 disables a sub-mode).

## 6. Prompts CSV

The xlsx from earlier is converted to `prompts.csv` (UTF-8, all fields quoted) at first run. CSV is easier to edit by hand, easier to diff in git, and easier to round-trip through the GUI. Two columns:

```
"Effect","Three.js prompt"
"Plasma","Single HTML file using three.js from CDN. Render a fullscreen quad..."
...
```

The GUI gets a CSV editor view: a table with add-row, delete-row, and inline-edit. Saves write atomically. The orchestrator reloads the file on each generation cycle so edits take effect immediately.

## 7. Generation state machine

```
IDLE → PICK_MODE → GENERATE → VALIDATE → DISPLAY ──→ ARCHIVE → IDLE
                       ▲           │
                       └─REPAIR ◀──┘ (up to N attempts; then DISCARD → IDLE)
```

**PICK_MODE** (when `boot_mode == generate`):
- Weighted random across `creative` / `update` / `stock`.
- If `update` is picked but the archive is empty, fall back to `stock`.
- If `stock` is picked, choose a row from `prompts.csv` (configurable: pure random, or shuffle-deck so each prompt gets used once before repeats).

**GENERATE:**
- Build the message list from the system prompt + mode-specific user payload.
- For `creative`: include the entire CSV in the user message.
- For `stock`: include the single prompt text.
- For `update`: pick a random archive file under the size budget, include its full text in the user message.
- Stream the response. Push tokens to the display over WebSocket. Display will type them out in the mock-OS editor with a small per-character delay (rate-limited so a fast model still looks like typing — e.g., 200 chars/sec ceiling).
- Strip markdown fences (```html ... ``` or ``` ... ```) defensively even though we tell the AI not to use them. Models often disobey.

**VALIDATE:** see §9.

**DISPLAY:** swap the iframe to the new file URL. Run for `demo_runtime_seconds`. While running, keep monitoring for crashes — if it dies mid-display, end early and move on (no repair after the demo started running successfully).

**ARCHIVE:** save with a unique filename (see §10).

**Replay mode:** skips generation entirely. On each cycle, picks a random `.html` from the archive directory and shows it for `demo_runtime_seconds`. No mock-OS interlude — straight transitions, optionally with a brief title card showing the filename.

## 8. LM Studio integration

`lm_client.py` exposes:

- `list_models() -> [str]` — GETs `/v1/models`, returns ids. Used to populate the dropdown in the GUI.
- `chat_stream(messages, model, max_tokens, temperature) -> iterator[str]` — POSTs to `/v1/chat/completions` with `stream: true`, yields content deltas. Errors raise typed exceptions so the orchestrator can react.

**Token safeguards:**
- Approximate token count = `len(text) // 4` (cheap heuristic; fine for budgeting).
- Before sending, compute `estimated_input + max_tokens`. If it exceeds a configurable context ceiling (default 16k, adjustable per model in GUI), trip a guard:
  - In `update` mode: skip files whose size already pushes us over budget. The orchestrator pre-filters the archive to candidates that fit.
  - In `creative` mode: if the CSV itself is too big, drop the longest prompts until it fits, and log a warning.
  - In `stock` mode: prompts are short, this won't trigger, but the check is unconditional.
- If the model returns a `finish_reason: length`, treat the output as suspect and run the repair path.

**Network failure handling:** any request error (timeout, connection refused, HTTP error) is caught, logged, surfaced on the display as "LLM unreachable — retrying in 30s", and the cycle is retried with backoff. Repeated failures don't crash the orchestrator.

## 9. Crash / blank detection

This is one of the trickier pieces. The validator runs in the display page itself (the only place that actually executes the AI's code) and reports back to the backend.

The AI's HTML is loaded into a sandboxed iframe with `sandbox="allow-scripts"` (no `allow-same-origin` — the demo can't read or modify the parent). Communication is via `postMessage`. We inject a tiny **probe script** into the iframe at load time by intercepting the response and prepending it. The probe:

1. **Captures runtime errors:** wraps `window.onerror` and `window.addEventListener('unhandledrejection', ...)`, postMessages each event to the parent.
2. **Tracks rendering activity:** patches `WebGLRenderingContext.prototype.drawArrays` and `drawElements` to count calls per second, postMessages a heartbeat.
3. **Samples the canvas:** every `blank_detection_seconds`, reads back a small region (`gl.readPixels` on a 16x16 patch) and hashes it. If two consecutive samples are identical AND the framebuffer is uniform (single color), flag as "blank."
4. **FPS check:** counts requestAnimationFrame ticks. If FPS drops below `min_fps` for 3 seconds, flag as "frozen."

The display page collects probe messages and decides:
- Any thrown error before the first successful frame → **CRASHED**.
- Blank + frozen for > `blank_detection_seconds` → **BLANK**.
- Otherwise → **OK**.

Detection happens during a brief **"audition" window** (default 5 seconds) before the full display window starts. If the demo passes audition, we let it run for the remaining `demo_runtime_seconds - 5`. If a crash happens *after* audition (some demos die after a while), we end the cycle but still archive — it ran for at least 5 good seconds, which is what we wanted to verify.

**Repair loop:** when audition fails, the backend sends the file back to the LLM with the repair system prompt and the captured error/symptom string. Up to `max_repair_attempts` tries; then discard. The mock-OS displays "ERROR DETECTED — REQUESTING REPAIR — ATTEMPT 2/3" if `show_retry_messages` is true.

## 10. Archive and versioning

Filename pattern for new files (creative and stock):

```
{YYYY-MM-DD}_{effect_slug}_{4-char-hash}.html
2026-05-10_plasma_a3f1.html
```

For `update` mode, derive from the source filename:

```
{source_basename}_v{N}.html
2026-05-10_plasma_a3f1_v2.html
2026-05-10_plasma_a3f1_v3.html
```

`N` = max existing `_vN` for that basename + 1, starting at 2 (the original is implicit v1). Update mode always picks the *latest* version of a chosen lineage as its source, so updates form a chain.

`archive.max_files` is a soft cap. When archiving past the cap, the oldest file is deleted. Updates inherit the timestamp of their source for sort-by-creation purposes — actually, let's keep them by their own creation time so newer updates aren't accidentally pruned. Either policy works; we'll go with own-timestamp for simplicity and document it.

Effect slug is taken from the prompt row's first column (lowercased, non-alphanum stripped). For `creative` mode, the slug is just `creative`. We can later have the AI return a one-word title and use that, but it's not worth the parsing complexity in v1.

## 11. The mock-OS view

A single full-viewport DOM scene built with CSS Grid. Three regions:

1. **Title bar** — fake window chrome, app name ("DEMOWRITER 1.0" or similar), fake minimize/maximize/close buttons (decorative).
2. **Editor pane** — the streaming code, with line numbers, monospace font, naive syntax highlighting (regex-based: keywords, strings, comments, numbers — three.js-flavored). Auto-scrolls to follow the cursor.
3. **Status bar** — "WRITING…", "COMPILING…", "ERROR — REPAIRING (2/3)…", filename, line/col, mock CPU/MEM gauges that wiggle pleasantly.

**Resolution-agnostic layout:** everything sized in `vw`/`vh`/`fr`. The grid template auto-flips based on aspect ratio:

```css
@media (orientation: portrait) {
  .mock-os { grid-template-rows: auto 1fr auto; }
}
@media (orientation: landscape) {
  .mock-os { grid-template-rows: auto 1fr auto; } /* same; the editor adapts */
}
```

Font size scales off `min(2.2vh, 1.4vw)` so text never overflows on extreme aspect ratios. We test 4:3, 16:9, 9:16, 3:4, and one ultrawide before calling it done.

**Color palettes** — defined as CSS variable sets, switchable by class on `<body>`:

| Palette key | Vibe |
|---|---|
| `amber_crt` | amber phosphor on near-black, scanline overlay |
| `green_phosphor` | classic VT220 green-on-black |
| `borland_blue` | white text on Borland-blue, yellow keywords |
| `solarized_dark` | modern dark, gentle on Pi displays |
| `notepad_pp` | white background, syntax-color keywords (the "Notepad++" feel) |
| `synthwave` | magenta/cyan/purple, glow on text |
| `mac_classic` | b&w, Chicago-style title bar |

Adding a palette later is just adding a CSS class with the right `--bg`, `--fg`, `--accent`, etc. The GUI dropdown reads available palettes from a small JS list that mirrors the CSS.

**Transitions:** when generation finishes, the editor view fades to a brief "RUNNING" splash, then the iframe fades in. Crash → fade back to mock-OS with the error highlighted. Replay mode skips all of this and just hard-cuts between iframes (or fades, configurable).

## 12. Config GUI

Single-page, served at `/config`. Sections:

- **Mode** — boot mode (generate/replay), sub-mode weights with sliders.
- **LM Studio** — base URL (text), "Refresh models" button → populates model dropdown, max_tokens, temperature, request timeout.
- **Display** — fullscreen toggle, demo runtime seconds, min typing seconds, palette dropdown, status bar toggle, retry-messages toggle.
- **Validation** — max repair attempts, blank detection seconds, min FPS.
- **Archive** — path, max files, "Open archive folder" link.
- **System prompts** — four textareas (creative, stock, update, repair).
- **Prompts CSV** — embedded editable table.
- **Live status** — what mode/file is running right now, last 20 log lines, "Skip current cycle" button.

"Save" buttons everywhere write through to `config.json` atomically. The orchestrator reads config at the top of each cycle, so edits land on the next demo without a restart. Restart-required fields (the bind port, basically) are clearly marked.

## 13. Cross-platform & Pi kiosk

The Python code uses only `pathlib`, `os`, and `requests` — all cross-platform. Three.js is vendored. No native deps.

**Windows / Linux desktop:** run `python app.py`. Open `http://localhost:8080/display` in a browser, or use the included `--launch` flag that opens the default browser via `webbrowser.open()`.

**Pi kiosk setup** (documented in `README.md`, scripted in `setup_pi.sh`):

1. Raspberry Pi OS Lite or Desktop (Bookworm).
2. Install Chromium and Python deps.
3. `systemd` user service (`ai-demoscener.service`) that runs `python app.py` on boot.
4. `systemd` user service (`ai-demoscener-display.service`) that launches Chromium with:
   ```
   chromium-browser --kiosk --noerrdialogs --disable-infobars \
     --check-for-update-interval=31536000 \
     --autoplay-policy=no-user-gesture-required \
     http://localhost:8080/display
   ```
5. Display rotation handled by `/boot/firmware/config.txt` (`display_rotate=1` for portrait) plus a CSS class the user can toggle in the GUI for the rare case where the OS-level rotation isn't desired.
6. Disable screen blanking (`xset s off`, `xset -dpms` for X; `wlr-randr` or kanshi for Wayland).
7. Auto-login enabled via `raspi-config`.

**Performance note for Pi:** Pi 4 / Pi 5 handle WebGL fine for most of the prompts in our list, but ray-marched scenes and heavy fragment shaders can drop below 30 FPS at 1080p. The `min_fps` validator catches the worst offenders and the repair prompt can ask for a simpler version. We could later tag prompts with a "Pi-friendly" flag and filter accordingly.

## 14. Logging

`logging` stdlib with `RotatingFileHandler` (5 MB × 5 files) writing to `debug.log` in the project root. Levels: DEBUG to file, INFO to stdout. Every state transition, every LLM request (prompt size and finish reason, *not* full bodies — those go to a separate optional `llm_traffic.log` only when a debug toggle is on), every validation result, every archive write. Exceptions log full tracebacks.

The config GUI's "live status" panel tails the last 20 lines of `debug.log`.

## 15. Logical inconsistencies and gotchas — resolved

These came up while planning. Each has a fix baked into the design above; calling them out so we're explicit.

1. **AI ignores "no markdown fences."** Models output ```html ... ``` constantly. Fix: defensive extractor that pulls the first ```...``` block if present, otherwise uses the whole response.
2. **Update mode with empty archive.** Falls back to stock for that cycle.
3. **CSV cell with newlines/commas.** Use `csv.QUOTE_ALL` on write and `csv.reader` on read — handles all of it.
4. **Concurrent config writes from multiple GUI tabs.** Last-writer-wins with atomic temp-file rename. Acceptable for a single-user kiosk.
5. **CDN unavailable on Pi.** Vendored three.js. Prompt template tells the AI to `import` from a local path — but to keep AI compliance high, we'll inject a small `<base href>` or use an importmap in the iframe wrapper that aliases CDN URLs to the local file. So the AI can output its usual CDN imports and they resolve locally. This is important: don't fight the model's defaults.
6. **Markdown stripping eats `</script>` strings inside the demo.** Use a non-greedy fence match anchored to start/end of message.
7. **Blank detection misfires on intentionally-dark scenes.** Combined with the WebGL `drawArrays` patch counting actual draw calls — a scene rendering all-black is fine if it's making draw calls; we only flag when it's making none AND the framebuffer is uniform.
8. **Sandboxed iframe blocks `localStorage` etc.** Some demos try to use it. The probe wraps these calls so they fail silently rather than throwing; confirmed against the existing prompt set (which doesn't use storage).
9. **Streaming faster than the eye can follow.** Display-side rate limiter caps render to ~200 char/sec regardless of token throughput. If the model finishes before the display catches up, hold "WRITING…" until display drains.
10. **Generation slower than `min_typing_seconds`.** That's fine — we just keep streaming until done. The "min" only matters when the model is *very* fast.
11. **Token-budget overflow on creative mode CSV.** Pre-truncate CSV by dropping longest rows until it fits, log a warning, proceed.
12. **Update mode picks a corrupt or huge file.** Pre-filter archive to files within budget AND that previously passed validation (we set a `passed: true` filename suffix, or maintain `archive_index.json` — index file is cleaner).
13. **Two demos with the same effect_slug archived in the same minute.** 4-char hash suffix prevents collisions.
14. **`max_files` deleting a file currently being shown in replay.** Take a snapshot of the file list at cycle start; only prune at idle.
15. **Browser refresh on display loses WebSocket state.** On reconnect, backend pushes current state immediately so the display re-syncs.
16. **LM Studio on a different machine goes down mid-stream.** Treat partial output as a failure for that cycle, log, retry with backoff.
17. **Audio-reactive demos need user gesture (autoplay).** Chromium kiosk flag `--autoplay-policy=no-user-gesture-required` covers it on Pi. Document for desktop users.
18. **Schema migration when we add config fields.** `config.py` merges loaded JSON over the defaults dict; missing keys get defaults silently; unknown keys are preserved (forward compatibility).
19. **Time zone weirdness in filenames.** Use `datetime.now()` (local time) — kiosks are wall-clock, not UTC, and filenames sort intuitively.
20. **The validator iframe steals focus / fires audio when the user is in the config GUI.** Config and display are separate URLs; the user in the config tab won't see/hear the demo unless they specifically open the display tab.

## 16. Build phases

Suggested order so each milestone is testable on its own:

1. **Skeleton** — Flask app, `config.json` round-trip, debug log, project layout, vendored three.js, blank `display.html` and `config.html`.
2. **LM Studio client** — models list, streaming chat, basic CLI test that prints tokens.
3. **Stock-mode generator (no display yet)** — produces archive files from the CSV. Validate manually by opening files in a browser.
4. **Validator probe** — iframe + postMessage + canvas readback. Test against known-good and known-bad demos.
5. **Display page (mock-OS)** — palettes, layout, streaming token display, iframe swap, audition window, transitions.
6. **Repair loop** — wire in retry path with the repair prompt.
7. **Update mode and creative mode** — including budget pre-filter for update mode and CSV truncation for creative.
8. **Replay mode.**
9. **Config GUI** — every setting, prompts CSV editor, live status.
10. **Pi kiosk packaging** — `setup_pi.sh`, systemd units, README, autostart smoke test on real hardware.
11. **Polish pass** — extra palettes, refined typography, smoother transitions, additional prompts in the CSV.

That's the plan. Two outputs accompany this doc: `demoscene_threejs_prompts.csv` (converted from the earlier xlsx and ready to drop into the project root as `prompts.csv`) and this document itself. Once you sign off, we can start on phase 1.
