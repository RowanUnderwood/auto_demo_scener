import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

STATS_PATH = Path(__file__).parent / "prompt_stats.json"


def load() -> dict:
    try:
        if STATS_PATH.exists():
            return json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to load prompt_stats.json")
    return {}


def save(stats: dict) -> None:
    try:
        tmp = STATS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        tmp.replace(STATS_PATH)
    except Exception:
        log.exception("Failed to save prompt_stats.json")


def record_failure(effect: str, kind: str) -> None:
    stats = load()
    e = stats.setdefault(effect, {"crashes": 0, "blanks": 0, "total": 0, "last_fail": None})
    if kind == "CRASHED":
        e["crashes"] += 1
    elif kind == "BLANK":
        e["blanks"] += 1
    e["total"] += 1
    e["last_fail"] = datetime.now().isoformat()
    save(stats)
    log.info("Recorded failure for %r: kind=%s total=%d", effect, kind, e["total"])
