# AI Demoscener

A self-running kiosk that uses a local LLM to endlessly generate and display Three.js demoscene effects. The system writes code on screen in a retro mock-OS editor, runs the demo, archives the good ones, and loops forever.

![Mock-OS editor mid-generation](ai_demoscener/coding_in_progress.jpg)

![Demo running full-screen](ai_demoscener/demo_in_progress.jpg)

---

## How it works

1. **Generate** — picks an effect from the prompt library (or invents one), streams the LLM output to a retro code-editor display
2. **Validate** — loads the generated HTML in a sandboxed iframe, checks for crashes and blank frames
3. **Repair** — if the demo fails, sends the error back to the LLM for a fix (up to N attempts)
4. **Display** — runs the working demo full-screen for a configurable duration
5. **Archive** — saves it with a timestamped filename, then loops

If all repair attempts are exhausted a styled **CRASHED / BLANK FRAME** overlay appears with a 5-second countdown before the next cycle starts automatically.

**Replay mode** plays back archived demos without any LLM traffic — useful for showcasing a collection.

---

## Quick start (Windows)

```
pip install flask flask-sock requests
```

Then double-click **`ai_demoscener/launch.bat`** — it downloads Three.js, starts the server, and opens the display page in your browser.

**Display:** `http://localhost:8080/display`  
**Config GUI:** `http://localhost:8080/config`

### Manual start (any OS)

```bash
cd ai_demoscener
python download_vendor.py      # one-time: vendor three.js locally
python app.py --launch
```

---

## Requirements

- Python 3.10+
- [LM Studio](https://lmstudio.ai/) running on the local network with a model loaded (tested with `gemma-4-27b-a4b` and similar code-capable models)
- A modern browser with WebGL support (Chromium recommended for kiosk use)

No GPU required on the host machine — LM Studio handles inference, the browser handles WebGL rendering.

---

## Configuration

Edit settings at `http://localhost:8080/config` while the server is running. All changes take effect on the next cycle without a restart.

Key settings:

| Setting | Default | Description |
|---|---|---|
| LM Studio URL | `http://192.168.2.192:1234` | Address of your LM Studio instance |
| Demo runtime | 60s | How long each demo runs before the next cycle |
| Mode weights | creative 1 / update 1 / stock 2 | Probability of each generation sub-mode |
| Palette | Amber CRT | Display colour scheme |
| Max repair attempts | 2 | How many times to ask the LLM to fix a broken demo |
| Prompt fail tracking | — | Config GUI → Prompts CSV shows a **Fails** column per effect (amber = 1–2 failures, red = 3+) |

### Generation modes

- **Stock** — implements a specific effect from `prompts.csv`
- **Creative** — given the full prompt list as inspiration, invents something new
- **Update** — takes a previous archive file and produces a fresh aesthetic take on it
- **Replay** — no generation; cycles through the archive

---

## Display keyboard shortcuts

| Key | Action |
|---|---|
| `d` | (Replay mode) Show delete confirmation for the current demo |
| `Escape` | Dismiss confirmation dialog |

---

## Static archive player

`ai_demoscener/archive/index.html` is a self-contained page that plays back your archived demos with no Python backend required — just a web server serving the `archive/` directory.

Open it at the root of your archive folder:

```
http://localhost/archive/       # any static server
https://yourname.github.io/auto_demo_scener/ai_demoscener/archive/   # GitHub Pages
```

It reads `archive_index.json`, shuffles the demos, and cycles through them automatically. Keyboard shortcuts:

| Key | Action |
|---|---|
| `→` / `Space` | Skip to next demo |
| `←` | Go back to previous demo |

Add `?runtime=N` to the URL to change how long each demo displays (default: 60 seconds).

---

## Palettes

Seven built-in colour schemes selectable from the config GUI:

| Key | Vibe |
|---|---|
| `amber_crt` | Amber phosphor on near-black |
| `green_phosphor` | Classic VT220 green |
| `borland_blue` | White on Borland blue, yellow keywords |
| `solarized_dark` | Modern dark |
| `notepad_pp` | Light background, Notepad++ colours |
| `synthwave` | Magenta/cyan/purple with glow |
| `mac_classic` | Black & white Chicago-style |

---

## Project layout

```
ai_demoscener/
├── app.py              Flask server + WebSocket hub
├── orchestrator.py     Generation state machine
├── lm_client.py        LM Studio streaming API client
├── validator.py        Probe injection, fence-stripping, validation sync
├── archive.py          Save / version / prune / delete archived demos
├── stats.py            Per-prompt failure tracking → prompt_stats.json
├── config.py           Config load/save with schema migration
├── prompts.csv         26+ effect specifications fed to the LLM
├── launch.bat          Windows one-click launcher
├── download_vendor.py  Fetches three.js into static/three/
├── archive/
│   └── index.html      Static archive player (no backend needed)
└── static/
    ├── display.html/js/css   Kiosk display page (mock-OS + iframe)
    └── config.html/js/css    Web config GUI
```

`config.json`, `debug.log`, `temp/`, and `static/three/` are created at runtime and excluded from the repo.

---

## Raspberry Pi kiosk

The plan doc (`ai_demoscener_plan.md`) contains full instructions for running as a zero-touch fullscreen kiosk on a Pi 4/5 using systemd and Chromium in kiosk mode.

---

## Licence

MIT
