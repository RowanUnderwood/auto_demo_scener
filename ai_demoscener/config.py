import json
from pathlib import Path

BASE = Path(__file__).parent

DEFAULTS: dict = {
    "schema_version": 1,
    "boot_mode": "generate",
    "lm_fallback_to_replay": True,
    "generate_mode_weights": {"creative": 1, "update": 1, "stock": 2},
    "llm_provider": "lm_studio",
    "lm_studio": {
        "base_url": "http://192.168.2.192:1234",
        "model": "",
        "max_tokens": 8192,
        "temperature": 0.9,
        "request_timeout_seconds": 180,
        "context_ceiling_tokens": 16384,
        "surprise_me": False,
    },
    "ninfer": {
        "base_url": "http://192.168.2.192:8000",
        "model": "",
        "max_tokens": 8192,
        "temperature": 0.9,
        "request_timeout_seconds": 180,
        "context_ceiling_tokens": 16384,
        "surprise_me": False,
        "thinking_effort": "low",
        "video_check_enabled": False,
        "video_max_tokens": 16384,
    },
    "display": {
        "fullscreen": True,
        "demo_runtime_seconds": 60,
        "min_typing_seconds": 8,
        "palette": "amber_crt",
        "show_status_bar": True,
        "show_retry_messages": True,
        "display_chars_per_sec": 400,
        "title_card_seconds": 4,
        "show_title_card": True,
        "show_progress_bar": True,
        "show_title_card_stats": True,
        "title_card_stats_font_size": 11,
        "jit_titling": False,
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
        "video_improve": (
            "You are an expert creative coder reviewing a running three.js demo. "
            "Attached is a short video capture of the demo actually running, followed by its "
            "complete HTML source. Watch how it looks and moves, then make any improvements you "
            "feel are relevant to make it more visually appealing — timing, color, composition, "
            "motion, contrast, or polish — while keeping the core idea and effect intact. "
            "Output ONLY the complete corrected HTML file, no commentary, no markdown fences."
        ),
        "title": (
            "You are a creative naming assistant for demoscene visuals. "
            "Given HTML/WebGL source code, output ONLY a valid JSON object with two fields: "
            "\"title\" (3–6 evocative words, title-cased) and "
            "\"description\" (exactly one poetic sentence, max 12 words). "
            "No other text, no markdown. Do NOT use markdown code fences."
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
