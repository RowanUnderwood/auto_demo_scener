"""Paired evaluation runs comparing generation strategies (milestone 4 of the iterative plan).

A run is a list of trials (effect prompts) and a list of arms (config overrides such as
classic vs iterative). For each trial the orchestrator generates ONE first draft and then
runs every arm on that same draft, so the comparison isolates what each strategy does with
a draft from draft-to-draft randomness. Results and outputs live in eval/<run_id>/:

  plan.json      run definition and progress
  results.jsonl  one metrics row per (trial, arm) — see metrics.py
  <trial>_draft.html, <trial>_<arm>.html   the shared draft and each arm's final demo
  ratings.json   blind A/B preference ratings from the /eval page

eval/active.json points at the run the orchestrator is working through while
boot_mode == "evaluate".
"""
import copy
import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path

import archive as archive_mod
import metrics

BASE = Path(__file__).parent
EVAL_DIR = BASE / "eval"
_ACTIVE = EVAL_DIR / "active.json"
_lock = threading.Lock()
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

# The plan's six smoke-trial categories, mapped onto prompts.csv effects
SMOKE_TRIALS = [
    {"mode": "stock", "effect": "Plasma", "category": "shader effect"},
    {"mode": "stock", "effect": "Solar System", "category": "3D scene"},
    {"mode": "stock", "effect": "Boids Flocking", "category": "motion-heavy"},
    {"mode": "stock", "effect": "Auto-Play Tetris", "category": "complex stateful"},
    {"mode": "creative", "effect": "creative", "category": "creative"},
    {"mode": "update", "effect": "update", "category": "archive update"},
]

ARM_PRESETS = {
    "classic": {"generation_strategy": "classic"},
    # Classic given as many repair responses as iterative has turns (matched budget)
    "classic_matched": {"generation_strategy": "classic"},
    "iterative": {"generation_strategy": "iterative", "iterative": {"visual_review": False}},
    "iterative_visual": {"generation_strategy": "iterative", "iterative": {"visual_review": True}},
}
DEFAULT_ARMS = ["classic", "iterative"]


class EvalError(Exception):
    pass


# ── Runs ───────────────────────────────────────────────────────────────────────

def run_dir(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id or ""):
        raise EvalError(f"invalid run id {run_id!r}")
    return EVAL_DIR / run_id


def create_run(cfg: dict, provider: str, name: str = "", arms: list[str] | None = None,
               repeats: int = 1, trials: list[dict] | None = None) -> dict:
    arms = [a for a in (arms or DEFAULT_ARMS) if a in ARM_PRESETS]
    if len(arms) < 2:
        raise EvalError("choose at least two arms")
    repeats = max(1, min(int(repeats or 1), 10))
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "smoke").lower()).strip("-")[:30] or "run"
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"

    arm_defs = []
    for a in arms:
        overrides = copy.deepcopy(ARM_PRESETS[a])
        if a == "classic_matched":
            turns = int(cfg.get("iterative", {}).get("max_turns", 6))
            overrides["validation"] = {"max_repair_attempts": turns}
        arm_defs.append({"name": a, "overrides": overrides})

    lm = cfg[provider]
    expanded = []
    for rep in range(repeats):
        for t in (trials or SMOKE_TRIALS):
            trial = dict(t)
            if trial["mode"] == "update":
                # Fix the source now so every arm (and a resumed run) uses the same one
                max_input = max(0, lm["context_ceiling_tokens"] - lm["max_tokens"])
                src = archive_mod.pick_for_update(cfg, max_input)
                trial["source"] = src.name if src else None
            trial["id"] = f"t{len(expanded) + 1:02d}"
            trial["repeat"] = rep + 1
            expanded.append(trial)

    plan = {
        "run_id": run_id,
        "name": name or "smoke",
        "created": datetime.now().isoformat(timespec="seconds"),
        "provider": provider,
        "model": lm.get("model", ""),
        "arms": arm_defs,
        "trials": expanded,
        "next": 0,
        "status": "pending",          # pending → running → done | stopped
        "resume_boot_mode": cfg.get("boot_mode", "generate"),
        "notes": [],
    }
    save_plan(plan)
    set_active(run_id)
    return plan


def save_plan(plan: dict) -> None:
    d = run_dir(plan["run_id"])
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / "plan.json.tmp"
    tmp.write_text(json.dumps(plan, indent=1), encoding="utf-8")
    tmp.replace(d / "plan.json")


def load_plan(run_id: str) -> dict | None:
    p = run_dir(run_id) / "plan.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def active_run() -> dict | None:
    with _lock:
        if not _ACTIVE.exists():
            return None
        try:
            run_id = json.loads(_ACTIVE.read_text(encoding="utf-8")).get("run")
        except (json.JSONDecodeError, OSError):
            return None
    try:
        return load_plan(run_id) if run_id else None
    except EvalError:
        return None


def set_active(run_id: str) -> None:
    run_dir(run_id)   # validates the id
    with _lock:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        _ACTIVE.write_text(json.dumps({"run": run_id}), encoding="utf-8")


def clear_active() -> None:
    with _lock:
        _ACTIVE.unlink(missing_ok=True)


def list_runs() -> list[dict]:
    if not EVAL_DIR.exists():
        return []
    active = (active_run() or {}).get("run_id")
    runs = []
    for d in sorted(EVAL_DIR.iterdir(), reverse=True):
        if d.is_dir() and (d / "plan.json").exists():
            p = json.loads((d / "plan.json").read_text(encoding="utf-8"))
            runs.append({k: p.get(k) for k in ("run_id", "name", "created", "provider", "model", "status", "next")}
                        | {"trials": len(p["trials"]), "arms": [a["name"] for a in p["arms"]],
                           "active": p["run_id"] == active})
    return runs


def append_result(run_id: str, row: dict) -> None:
    metrics.append(run_dir(run_id) / "results.jsonl", {"run": run_id, **row})


def results(run_id: str) -> list[dict]:
    """Rows for the run, keeping only the latest per (trial, arm): a trial interrupted by a
    stop is rerun from its draft on resume, re-recording arms it had already finished."""
    latest: dict[tuple, dict] = {}
    for r in metrics.load(run_dir(run_id) / "results.jsonl"):
        latest[(r.get("trial"), r.get("arm"))] = r
    return list(latest.values())


def output_name(trial_id: str, arm: str) -> str:
    return f"{trial_id}_{arm}.html"


# ── Blind review ───────────────────────────────────────────────────────────────

def pairs(run_id: str) -> list[dict]:
    """One pair per (trial, non-baseline arm), compared against the first arm. Sides are
    labelled A/B in an order fixed by a hash, so the page never reveals which arm is which.
    Identical outputs (e.g. a draft both arms accepted unchanged) are auto-ties."""
    plan = load_plan(run_id)
    if not plan:
        raise EvalError("no such run")
    d = run_dir(run_id)
    ratings = _load_ratings(run_id)
    base = plan["arms"][0]["name"]
    out = []
    for t in plan["trials"]:
        for arm in [a["name"] for a in plan["arms"][1:]]:
            files = {a: d / output_name(t["id"], a) for a in (base, arm)}
            exists = {a: p.exists() for a, p in files.items()}
            if not any(exists.values()) and not (d / f"{t['id']}_draft.html").exists():
                continue   # trial not run yet
            flip = int(hashlib.sha1(f"{run_id}:{t['id']}:{arm}".encode()).hexdigest(), 16) & 1
            left, right = (arm, base) if flip else (base, arm)
            same = (exists[base] and exists[arm]
                    and files[base].read_bytes() == files[arm].read_bytes()) or not any(exists.values())
            key = f"{t['id']}:{arm}"
            out.append({
                "key": key,
                "trial": t["id"],
                "brief": t["effect"] if t["mode"] == "stock" else f"{t['mode']} mode"
                         + (f" (source {t.get('source')})" if t.get("source") else ""),
                "category": t.get("category", ""),
                "A": output_name(t["id"], left) if exists[left] else None,
                "B": output_name(t["id"], right) if exists[right] else None,
                "identical": same,
                "rating": ratings.get(key),
                "_sides": {"A": left, "B": right},
            })
    return out


def public_pairs(run_id: str, reveal: bool = False) -> list[dict]:
    ps = pairs(run_id)
    for p in ps:
        sides = p.pop("_sides")
        if reveal:
            p["arms"] = sides
    return ps


def rate(run_id: str, key: str, choice: str, brief_a: bool | None, brief_b: bool | None) -> None:
    if choice not in ("A", "B", "tie", "both_bad"):
        raise EvalError("choice must be A, B, tie or both_bad")
    if key not in {p["key"] for p in pairs(run_id)}:
        raise EvalError("unknown pair")
    with _lock:
        ratings = _load_ratings(run_id)
        ratings[key] = {"choice": choice, "brief_A": brief_a, "brief_B": brief_b,
                        "ts": datetime.now().isoformat(timespec="seconds")}
        path = run_dir(run_id) / "ratings.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ratings, indent=1), encoding="utf-8")
        tmp.replace(path)


def _load_ratings(run_id: str) -> dict:
    p = run_dir(run_id) / "ratings.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ── Summary ────────────────────────────────────────────────────────────────────

def summary(run_id: str) -> dict:
    plan = load_plan(run_id)
    if not plan:
        raise EvalError("no such run")
    rows = results(run_id)
    arm_names = [a["name"] for a in plan["arms"]]
    per_arm = metrics.summarize(rows, key="arm")
    base = arm_names[0]

    prefs = {a: {"vs": base, "wins": 0, "losses": 0, "ties": 0, "both_bad": 0,
                 "identical": 0, "unrated": 0} for a in arm_names[1:]}
    brief: dict[str, list[bool]] = {a: [] for a in arm_names}
    for p in pairs(run_id):
        arm = p["key"].split(":", 1)[1]
        pr = prefs[arm]
        if p["identical"]:
            pr["identical"] += 1
            continue
        r = p["rating"]
        if not r:
            pr["unrated"] += 1
            continue
        sides = p["_sides"]
        if r["choice"] in ("A", "B"):
            pr["wins" if sides[r["choice"]] == arm else "losses"] += 1
        elif r["choice"] == "tie":
            pr["ties"] += 1
        else:
            pr["both_bad"] += 1
        for side in ("A", "B"):
            if r.get(f"brief_{side}") is not None and p[side]:
                brief[sides[side]].append(bool(r[f"brief_{side}"]))
    for a in arm_names:
        if a in per_arm:
            vals = brief[a]
            per_arm[a]["brief_ok_pct"] = round(100 * sum(vals) / len(vals), 1) if vals else None
    done = {r["trial"] for r in rows}
    return {
        "plan": {k: plan.get(k) for k in ("run_id", "name", "created", "provider", "model",
                                          "status", "next", "notes")}
                | {"trials": len(plan["trials"]), "trials_done": len(done), "arms": arm_names},
        "arms": per_arm,
        "preference": prefs,
    }
