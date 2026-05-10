import csv
import json
import logging
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from flask_sock import Sock

import config as cfg_mod
import lm_client
import logging_setup
import validator
from orchestrator import Orchestrator

BASE = Path(__file__).parent
STATIC = BASE / "static"
TEMP_DIR = BASE / "temp"
TEMP_DIR.mkdir(exist_ok=True)

logging_setup.setup()
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
sock = Sock(app)

# ── WebSocket registry ─────────────────────────────────────────────────────────
_ws_clients: set = set()
_ws_lock = threading.Lock()


def broadcast(msg: dict) -> None:
    data = json.dumps(msg)
    dead: set = set()
    with _ws_lock:
        clients = set(_ws_clients)
    for ws in clients:
        try:
            ws.send(data)
        except Exception:
            dead.add(ws)
    if dead:
        with _ws_lock:
            _ws_clients.difference_update(dead)


orc = Orchestrator(broadcast_fn=broadcast)


@sock.route("/ws")
def ws_handler(ws):
    with _ws_lock:
        _ws_clients.add(ws)
    # Sync new client to current state (full message, so DISPLAY includes url/runtime)
    ws.send(json.dumps(orc.last_state_msg))
    cfg = cfg_mod.load()
    ws.send(json.dumps({"type": "config_push", "config": cfg}))
    try:
        while True:
            raw = ws.receive(timeout=30)
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("type") == "audit_result":
                validator.receive_result(msg)
    except Exception:
        pass
    finally:
        with _ws_lock:
            _ws_clients.discard(ws)


# ── HTML pages ─────────────────────────────────────────────────────────────────
@app.route("/")
@app.route("/display")
def display_page():
    return send_from_directory(STATIC, "display.html")


@app.route("/config")
def config_page():
    return send_from_directory(STATIC, "config.html")


# ── Demo file serving (with probe injection) ───────────────────────────────────
def _three_importmap() -> dict | None:
    three_js = STATIC / "three" / "three.module.js"
    if not three_js.exists():
        return None
    local = "/static/three/three.module.js"
    return {
        "three": local,
        "https://unpkg.com/three@0.160.0/build/three.module.js": local,
        "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js": local,
        "https://unpkg.com/three/build/three.module.js": local,
        "https://cdn.skypack.dev/three": local,
    }


def _serve_with_probe(path: Path) -> Response:
    if not path.exists():
        return Response("Not found", status=404)
    html = path.read_text(encoding="utf-8")
    html = validator.inject_probe(html, extra_imports=_three_importmap())
    return Response(html, mimetype="text/html")


@app.route("/temp/<filename>")
def serve_temp(filename):
    return _serve_with_probe(TEMP_DIR / filename)


@app.route("/archive/<filename>")
def serve_archive(filename):
    cfg = cfg_mod.load()
    archive_dir = BASE / cfg["archive"]["directory"]
    return _serve_with_probe(archive_dir / filename)


# ── API: config ────────────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(cfg_mod.load())


@app.route("/api/config", methods=["POST"])
def api_set_config():
    data = request.get_json(force=True)
    cfg_mod.save(data)
    log.info("Config saved via GUI")
    broadcast({"type": "config_push", "config": data})
    return jsonify({"ok": True})


# ── API: LM Studio models ──────────────────────────────────────────────────────
@app.route("/api/models")
def api_models():
    cfg = cfg_mod.load()
    try:
        models = lm_client.list_models(cfg["lm_studio"]["base_url"])
        return jsonify({"models": models})
    except lm_client.LMClientError as e:
        return jsonify({"error": str(e)}), 502


# ── API: orchestrator control ──────────────────────────────────────────────────
@app.route("/api/skip", methods=["POST"])
def api_skip():
    orc.skip()
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    data = request.get_json(force=True)
    cfg = cfg_mod.load()
    cfg["boot_mode"] = data.get("mode", "generate")
    cfg_mod.save(cfg)
    return jsonify({"ok": True})


# ── API: archive ───────────────────────────────────────────────────────────────
@app.route("/api/archive")
def api_archive():
    import archive as arch_mod
    cfg = cfg_mod.load()
    files = arch_mod.list_files(cfg)
    return jsonify({"files": [f.name for f in files]})


@app.route("/api/archive/<filename>", methods=["DELETE"])
def api_delete_archive(filename):
    import archive as arch_mod
    cfg = cfg_mod.load()
    ok = arch_mod.delete_file(filename, cfg)
    if ok:
        orc.skip()
    return jsonify({"ok": ok})


# ── API: prompts CSV ──────────────────────────────────────────────────────────
@app.route("/api/prompts", methods=["GET"])
def api_get_prompts():
    csv_path = BASE / "prompts.csv"
    if not csv_path.exists():
        return jsonify({"rows": []})
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return jsonify({"rows": rows})


@app.route("/api/prompts", methods=["POST"])
def api_set_prompts():
    data = request.get_json(force=True)
    rows = data.get("rows", [])
    csv_path = BASE / "prompts.csv"
    tmp = csv_path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["Effect", "Three.js prompt"], quoting=csv.QUOTE_ALL
        )
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(csv_path)
    return jsonify({"ok": True})


# ── API: logs ──────────────────────────────────────────────────────────────────
@app.route("/api/logs")
def api_logs():
    log_path = BASE / "debug.log"
    if not log_path.exists():
        return jsonify({"lines": []})
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    n = int(request.args.get("n", 50))
    return jsonify({"lines": lines[-n:]})


# ── API: status ────────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({
        "state": orc.state,
        "current_file": str(orc.current_file) if orc.current_file else None,
    })


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    import webbrowser

    port = int(next((sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--port"), 8080))
    orc.start()
    if "--launch" in sys.argv:
        webbrowser.open(f"http://localhost:{port}/display")
    log.info("AI Demoscener running at http://0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
