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


def _entry(stats: dict, effect: str) -> dict:
    return stats.setdefault(effect, {
        "crashes": 0, "blanks": 0, "total": 0,
        "runs": 0, "deletions": 0, "last_fail": None, "by_model": {},
    })


def record_failure(effect: str, kind: str, model: str = "") -> None:
    stats = load()
    e = _entry(stats, effect)
    e.setdefault("runs", 0)
    e.setdefault("by_model", {})
    if kind == "CRASHED":
        e["crashes"] += 1
    elif kind == "BLANK":
        e["blanks"] += 1
    e["total"] += 1
    e["runs"]  += 1
    e["last_fail"] = datetime.now().isoformat()
    if model:
        m = e["by_model"].setdefault(model, {"crashes": 0, "blanks": 0, "total": 0, "runs": 0})
        if kind == "CRASHED":
            m["crashes"] += 1
        elif kind == "BLANK":
            m["blanks"] += 1
        m["total"] += 1
        m["runs"]  += 1
    save(stats)
    log.info("Recorded failure for %r: kind=%s total=%d", effect, kind, e["total"])


def record_run(effect: str, model: str = "") -> None:
    stats = load()
    e = _entry(stats, effect)
    e.setdefault("runs", 0)
    e.setdefault("by_model", {})
    e["runs"] += 1
    if model:
        m = e["by_model"].setdefault(model, {"crashes": 0, "blanks": 0, "total": 0, "runs": 0})
        m["runs"] += 1
    save(stats)


def record_deletion(effect: str, model: str = "") -> None:
    stats = load()
    e = _entry(stats, effect)
    e.setdefault("deletions", 0)
    e.setdefault("by_model", {})
    e["deletions"] += 1
    if model:
        m = e["by_model"].setdefault(model, {"crashes": 0, "blanks": 0, "total": 0, "runs": 0, "deletions": 0})
        m.setdefault("deletions", 0)
        m["deletions"] += 1
    save(stats)
    log.info("Recorded deletion for %r", effect)
