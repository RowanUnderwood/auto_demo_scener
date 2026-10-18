"""Comparable per-cycle generation metrics (milestone 4 of the iterative plan).

Kept apart from prompt_stats.json on purpose: validation changes alter what counts as a
pass, so these numbers aren't comparable with the historical failure statistics. Rows are
appended as JSON lines — live generate cycles to session_metrics.jsonl, evaluation trials
to eval/<run>/results.jsonl — and summarized per strategy (live) or per arm (evaluation).

Row fields (absent or None when not applicable):
  ts, run ("live" or an eval run id), trial, arm, strategy, mode, effect, provider, model,
  draft_seconds, draft_output_chars, draft_completion_tokens,
  seconds (validation/iteration time after the draft), initial_result (first check: OK /
  CRASHED / BLANK), final_ok, final_kind, outcome, checks, repairs, responses (model
  responses after the draft: classic repairs or iterative turns), tool_calls, vision_calls,
  llm_calls, output_chars, completion_tokens, tokens_known, fallback, budget_exhausted,
  changed_from_draft, visual_decisions, archived, file.
"""
import json
import statistics
import threading
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LIVE_PATH = BASE / "session_metrics.jsonl"
_lock = threading.Lock()


def append(path: Path, row: dict) -> None:
    row = {"ts": datetime.now().isoformat(timespec="seconds"), **row}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _mean(values) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.mean(vals), 1) if vals else None


def _median(values) -> float | None:
    vals = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.median(vals), 1) if vals else None


def _rate(flags) -> float | None:
    flags = [bool(f) for f in flags if f is not None]
    return round(100 * sum(flags) / len(flags), 1) if flags else None


def summarize(rows: list[dict], key: str = "arm") -> dict[str, dict]:
    """Aggregate rows per `key`. Token figures only average rows whose server reported
    usage (`tokens_known`); output_chars is always available as a fallback measure."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get(key) or "?"), []).append(r)
    out = {}
    for name, rs in groups.items():
        known = [r for r in rs if r.get("tokens_known")]
        decisions = [d for r in rs for d in (r.get("visual_decisions") or [])]
        out[name] = {
            "n": len(rs),
            "initial_pass_pct": _rate(r.get("initial_result") == "OK" for r in rs if r.get("initial_result")),
            "final_pass_pct": _rate(r.get("final_ok") for r in rs),
            "changed_from_draft_pct": _rate(r.get("changed_from_draft") for r in rs if r.get("final_ok")),
            "median_seconds": _median(r.get("seconds") for r in rs),
            "mean_seconds": _mean(r.get("seconds") for r in rs),
            "mean_responses": _mean(r.get("responses") for r in rs),
            "mean_tool_calls": _mean(r.get("tool_calls") for r in rs),
            "mean_vision_calls": _mean(r.get("vision_calls") for r in rs),
            "mean_output_chars": _mean(r.get("output_chars") for r in rs),
            "mean_completion_tokens": _mean(r.get("completion_tokens") for r in known),
            "tokens_known_n": len(known),
            "fallbacks": sum(1 for r in rs if r.get("fallback")),
            "budget_exhausted": sum(1 for r in rs if r.get("budget_exhausted")),
            "polish_kept": sum(1 for d in decisions if str(d).startswith("after")),
            "polish_rejected": sum(1 for d in decisions if str(d).startswith("before")),
            "outcomes": _counts(r.get("outcome") for r in rs),
        }
    return out


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out
