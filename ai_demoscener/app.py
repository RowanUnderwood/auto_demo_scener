import csv
import json
import logging
import os
import time
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

# ── Title card batch generation state ─────────────────────────────────────────
_tc_lock    = threading.Lock()
_tc_running = False
_tc_stop    = False
_tc_done    = 0
_tc_total   = 0
_tc_current = ""


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


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    orc.stop()
    def _exit():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()
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


# ── API: prompt stats ─────────────────────────────────────────────────────────
@app.route("/api/prompt_stats")
def api_prompt_stats():
    import stats as stats_mod
    return jsonify(stats_mod.load())


# ── API: logs ──────────────────────────────────────────────────────────────────
@app.route("/api/logs")
def api_logs():
    log_path = BASE / "debug.log"
    if not log_path.exists():
        return jsonify({"lines": []})
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    n = int(request.args.get("n", 50))
    return jsonify({"lines": lines[-n:]})


# ── API: title cards ──────────────────────────────────────────────────────────
@app.route("/api/title_cards")
def api_get_title_cards():
    import archive as arch_mod
    cfg = cfg_mod.load()
    index = arch_mod.get_all_meta(cfg)
    entries = [{"filename": k, **v} for k, v in sorted(index.items())]
    return jsonify(entries)


@app.route("/api/title_cards", methods=["POST"])
def api_set_title_cards():
    import archive as arch_mod
    cfg = cfg_mod.load()
    data = request.get_json(force=True)
    for entry in data.get("entries", []):
        filename = entry.get("filename")
        if filename:
            arch_mod.update_meta(filename, {
                "title": entry.get("title", ""),
                "description": entry.get("description", ""),
            }, cfg)
    return jsonify({"ok": True})


@app.route("/api/title_cards/clear_all", methods=["POST"])
def api_clear_title_cards():
    import archive as arch_mod
    cfg = cfg_mod.load()
    arch_mod.clear_title_meta(cfg)
    return jsonify({"ok": True})


@app.route("/api/title_cards/generate_all", methods=["POST"])
def api_generate_title_cards():
    global _tc_running, _tc_stop, _tc_done, _tc_total, _tc_current
    with _tc_lock:
        if _tc_running:
            return jsonify({"ok": False, "reason": "already running"})
        _tc_running = True
        _tc_stop = False
        _tc_done = 0
        _tc_total = 0
        _tc_current = ""

    def _run():
        global _tc_running, _tc_stop, _tc_done, _tc_total, _tc_current
        try:
            import archive as arch_mod
            cfg = cfg_mod.load()
            index = arch_mod.get_all_meta(cfg)
            archive_dir = arch_mod._archive_dir(cfg)
            todo = [
                (fname, archive_dir / fname)
                for fname, entry in index.items()
                if not entry.get("title")
            ]
            with _tc_lock:
                _tc_total = len(todo)
            for fname, path in todo:
                with _tc_lock:
                    if _tc_stop:
                        break
                    _tc_current = fname
                try:
                    html = path.read_text(encoding="utf-8")
                    result = orc._generate_title_meta(cfg_mod.load(), html)
                    if result:
                        arch_mod.update_meta(fname, result, cfg_mod.load())
                except Exception:
                    log.exception("Batch title generation failed for %s", fname)
                with _tc_lock:
                    _tc_done += 1
        finally:
            with _tc_lock:
                _tc_running = False
                _tc_current = ""

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/title_cards/stop_generate", methods=["POST"])
def api_stop_title_cards():
    global _tc_stop
    with _tc_lock:
        _tc_stop = True
    return jsonify({"ok": True})


@app.route("/api/title_cards/progress")
def api_title_cards_progress():
    with _tc_lock:
        return jsonify({
            "running": _tc_running,
            "done":    _tc_done,
            "total":   _tc_total,
            "current": _tc_current,
        })


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
