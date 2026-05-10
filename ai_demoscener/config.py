import json
from pathlib import Path

BASE = Path(__file__).parent

DEFAULTS: dict = {
    "schema_version": 1,
    "boot_mode": "generate",
    "generate_mode_weights": {"creative": 1, "update": 1, "stock": 2},
    "lm_studio": {
        "base_url": "http://192.168.2.192:1234",
        "model": "",
        "max_tokens": 8192,
        "temperature": 0.9,
        "request_timeout_seconds": 180,
        "context_ceiling_tokens": 16384,
    },
    "display": {
        "fullscreen": True,
        "demo_runtime_seconds": 60,
        "min_typing_seconds": 8,
        "palette": "amber_crt",
        "show_status_bar": True,
        "show_retry_messages": True,
        "display_chars_per_sec": 400,
    },
    "validation": {
        "max_repair_attempts": 2,
        "blank_detection_seconds": 4,
        "min_fps": 5,
        "audition_seconds": 5,
    },
    "archive": {
        "directory": "archive",
        "max_files": 500,
    },
    "system_prompts": {
        "creative": (
            "You are an expert creative coder. Based on the attached list of stock effects, "
            "invent a brand new demoscene idea. Be creative and original. "
            "Output ONLY a single self-contained HTML file using three.js. "
            "No commentary, no markdown fences."
        ),
        "stock": (
            "You are an expert creative coder. Implement the requested effect. "
            "Output ONLY a single self-contained HTML file using three.js. "
            "No commentary, no markdown fences."
        ),
        "update": (
            "You are an expert creative coder. Below is an existing demo. "
            "Produce a fresh new take on the same concept — different aesthetic, different parameters, "
            "different feel — while staying faithful to the core idea. "
            "Output ONLY a single self-contained HTML file using three.js. "
            "No commentary, no markdown fences."
        ),
        "repair": (
            "The HTML below failed to run. The error or symptom is: {error}. "
            "Fix the file and output the corrected complete HTML. "
            "Output ONLY the HTML, no commentary, no markdown fences."
        ),
    },
}

CONFIG_PATH = BASE / "config.json"


def _deep_merge(defaults: dict, loaded: dict) -> dict:
    result = dict(defaults)
    for k, v in loaded.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        return _deep_merge(DEFAULTS, loaded)
    return dict(DEFAULTS)


def save(cfg: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    tmp.replace(CONFIG_PATH)
