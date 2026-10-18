import json
from pathlib import Path

BASE = Path(__file__).parent

DEFAULTS: dict = {
    "schema_version": 1,
    "boot_mode": "generate",
    "lm_fallback_to_replay": True,
    "generate_mode_weights": {"creative": 1, "update": 1, "stock": 2},
    # "classic": stream a full file, then whole-file repairs. "iterative": stream the first
    # draft the same way, then fix it with targeted edits checked after every change
    # (agent_runner.py). Replay mode is unaffected.
    "generation_strategy": "classic",
    "iterative": {
        "interaction": "auto",          # auto | native_tools | json_actions
        "max_turns": 6,                 # model responses per demo
        "max_tool_calls": 24,           # executed edits/runs/finishes per demo
        "session_deadline_seconds": 600,
        "max_run_seconds": 15,          # cap on a model-requested longer run
        # Visual review (milestone 3): once a revision passes, capture what it looks like,
        # ask the model for at most three concrete issues, let it polish them with edits,
        # then compare before/after and keep the earlier revision unless the polish is
        # clearly better. Needs a model that accepts image (or, for Ninfer, video) input.
        "visual_review": False,
        "vision_input": "frames",       # frames | video (video: Ninfer only, needs ffmpeg)
        "capture_frames": 4,            # frames per capture
        "capture_interval_ms": 1500,    # time between frames
        "capture_max_px": 768,          # long edge of each frame
        "video_capture_seconds": 8,
        "max_polish_rounds": 1,
    },
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
    },
    "strata": {
        "base_url": "http://192.168.2.192:8080",
        "api_key": "",
        "model": "",
        "max_tokens": 8192,
        "temperature": 0.9,
        "request_timeout_seconds": 300,
        "context_ceiling_tokens": 131072,
        "surprise_me": False,
        "thinking_effort": "low",
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
        "iterative": (
            "You are an expert creative coder fixing and polishing a single-file three.js demo "
            "that runs full-screen in a kiosk browser. You work in rounds: each round you see the "
            "brief, a change log, the latest automatic check of the current revision (errors, "
            "shader logs and render statistics from actually running it) and the current source "
            "with line numbers. Respond with tool actions only.\n"
            "Rules:\n"
            "- Prefer small targeted edit_demo calls. old_text must be copied exactly from the "
            "current source without the line-number prefixes, and must match only one place.\n"
            "- Use write_demo (a complete file) only when targeted edits cannot work.\n"
            "- The app runs the demo automatically after your changes; you will see the result "
            "next round. Never claim something works without a passing check.\n"
            "- Call finish_demo once the latest check passed and nothing needs fixing.\n"
            "- Keep the effect described in the brief. Fix the cause of an error, don't delete "
            "the feature that raised it.\n"
            "Environment: three.js r160 is available via the import map as 'three'; the page is a "
            "sandboxed iframe (no localStorage, no network requests other than CDN imports); the "
            "canvas should fill the window and animate continuously."
        ),
        "visual_critique": (
            "You review a running three.js demo for a kiosk demoscene show. You get the brief and "
            "frames captured from the demo while it runs. Name at most three concrete visual "
            "problems you can actually see in the frames — e.g. an overexposed or washed-out "
            "image, the subject clipped or too small in frame, imperceptible or jerky motion, poor "
            "contrast, an empty or static-looking scene — each with the change that would fix it. "
            "Do not ask for a different effect or a rewrite. If it already looks good, return no "
            "issues. Output ONLY a JSON object, no markdown: "
            "{\"issues\": [{\"problem\": str, \"evidence\": str, \"change\": str}]}"
        ),
        "visual_compare": (
            "You compare two captures of the same three.js demo: BEFORE and AFTER a polish pass "
            "that tried to fix the listed issues. The scenes may differ slightly between runs "
            "because of randomness or timing; judge the overall look, not exact pixels. Choose "
            "AFTER only if it is clearly better and has no obvious regression (broken framing, "
            "lost detail, darker or emptier image, less motion, the effect changed). If unsure, "
            "choose BEFORE. Output ONLY a JSON object, no markdown: "
            "{\"choice\": \"after\" | \"before\", \"improved\": [str], \"regressions\": [str], "
            "\"reason\": str}"
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
