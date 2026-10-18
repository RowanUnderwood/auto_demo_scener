import base64
import copy
import csv
import hashlib
import logging
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from io import StringIO
from pathlib import Path

import agent_runner
import archive as archive_mod
import config as cfg_mod
import evaluation
import lm_client
import metrics
import stats as stats_mod
import validator

log = logging.getLogger(__name__)
BASE = Path(__file__).parent


class Orchestrator:
    def __init__(self, broadcast_fn, display_count_fn=None):
        self._broadcast = broadcast_fn
        # Number of connected display pages (the only clients that can run auditions).
        # None means "unknown" and is treated as always connected.
        self._display_count = display_count_fn or (lambda: 1)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._discard_current = threading.Event()
        self._lm_unavailable = False
        self._last_display_meta: dict | None = None
        self._stock_deck: list[dict] = []
        # LLM/check accounting for the strategy currently running (see _acct_begin)
        self._acct: dict = {}
        self._replay_deck: list[Path] = []
        self.state = "IDLE"
        self.current_file: Path | None = None
        self.last_state_msg: dict = {"type": "state", "state": "IDLE"}

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="orchestrator")
        self._thread.start()
        log.info("Orchestrator started")

    def stop(self) -> None:
        self._stop.set()

    def skip(self) -> None:
        self._skip.set()

    def discard_current(self) -> None:
        self._discard_current.set()
        self._skip.set()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _tx(self, state: str, **kw) -> None:
        self.state = state
        msg = {"type": "state", "state": state, **kw}
        self.last_state_msg = msg
        log.info("STATE → %s %s", state, kw or "")
        self._broadcast(msg)

    def _push_token(self, tok: str) -> None:
        self._broadcast({"type": "token", "token": tok})

    def _push_status(self, text: str) -> None:
        self._broadcast({"type": "status", "text": text})

    def _wait_for_display(self) -> bool:
        """Block until at least one display page is connected. Returns False if a stop or
        skip arrives first. Without a display nothing can validate a demo, so generating one
        would only waste an LLM call (and archiving it unvalidated isn't an option)."""
        if self._display_count() > 0:
            return True
        log.info("No display connected — pausing generation until one connects")
        self._push_status("WAITING FOR DISPLAY")
        while self._display_count() == 0:
            if self._stop.is_set():
                return False
            if self._skip.is_set():
                self._skip.clear()
                return False
            time.sleep(1)
        log.info("Display connected — resuming generation")
        return True

    # ── Metrics accounting ─────────────────────────────────────────────────────

    def _acct_begin(self) -> None:
        self._acct = {"llm_calls": 0, "output_chars": 0, "completion_tokens": 0,
                      "tokens_known": True, "checks": [], "repairs": 0}

    def _acct_llm(self, output_chars: int, usage: dict | None) -> None:
        a = self._acct
        if not a:
            return
        a["llm_calls"] += 1
        a["output_chars"] += output_chars
        tokens = (usage or {}).get("completion_tokens")
        if isinstance(tokens, int):
            a["completion_tokens"] += tokens
        else:
            a["tokens_known"] = False

    def _active_provider_cfg(self, cfg: dict) -> tuple[str, dict]:
        provider = cfg.get("llm_provider", "lm_studio")
        if provider not in ("lm_studio", "ninfer", "strata"):
            provider = "lm_studio"
        return provider, cfg[provider]

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = cfg_mod.load()
                if cfg["boot_mode"] == "replay":
                    self._replay_cycle(cfg)
                elif cfg["boot_mode"] == "evaluate":
                    self._eval_cycle(cfg)
                elif cfg.get("lm_fallback_to_replay", True):
                    self._probe_lm(cfg)
                    if self._lm_unavailable:
                        provider, _ = self._active_provider_cfg(cfg)
                        self._push_status(f"{provider} unreachable — replaying archive")
                        self._replay_cycle(cfg)
                    else:
                        self._generate_cycle(cfg)
                else:
                    self._generate_cycle(cfg)
            except Exception:
                log.exception("Unhandled error in orchestrator")
                time.sleep(10)

    # ── Replay ─────────────────────────────────────────────────────────────────

    def _replay_cycle(self, cfg: dict) -> None:
        if not self._replay_deck:
            files = archive_mod.list_files(cfg)
            if not files:
                log.info("Archive empty; sleeping")
                self._push_status("ARCHIVE EMPTY — waiting for demos")
                time.sleep(15)
                return
            self._replay_deck = files.copy()
            random.shuffle(self._replay_deck)
            log.info("Replay deck refilled: %d files", len(self._replay_deck))
        path = None
        while self._replay_deck:
            candidate = self._replay_deck.pop()
            if candidate.exists():
                path = candidate
                break
        if path is None:
            return  # all candidates were deleted; next cycle refills
        entry = archive_mod.get_meta(path.name, cfg)
        entry_mode   = entry.get("mode", "")
        entry_effect = entry.get("effect", "")
        entry_model  = entry.get("model", "")
        stats_key = entry_effect if entry_mode == "stock" and entry_effect else entry_mode or "unknown"
        meta = {
            "effect":      entry_effect,
            "title":       entry.get("title", ""),
            "description": entry.get("description", ""),
            "model":       entry_model,
        }
        runtime = cfg["display"]["demo_runtime_seconds"]

        # JIT titling: generate title synchronously before displaying
        if cfg["display"].get("jit_titling") and not meta["title"] and not self._lm_unavailable:
            result = self._generate_title_meta(
                cfg, path.read_text(encoding="utf-8"), model=entry_model
            )
            if result:
                meta.update(result)
                archive_mod.update_meta(path.name, result, cfg)

        stats_summary = self._meta_stats(stats_key, entry_model)
        meta["stats"] = stats_summary
        self._tx("DISPLAY", url=f"/archive/{path.name}", runtime=runtime, meta=meta)

        # Background enrichment only if JIT is off and title still missing
        if not cfg["display"].get("jit_titling") and not meta["title"] and not self._lm_unavailable:
            _path = path
            _entry_model = entry_model
            def _enrich():
                try:
                    html = _path.read_text(encoding="utf-8")
                    result = self._generate_title_meta(cfg_mod.load(), html, model=_entry_model)
                    if result:
                        archive_mod.update_meta(_path.name, result, cfg_mod.load())
                        log.info("Enriched title for %s", _path.name)
                except Exception:
                    log.exception("Title enrichment failed for %s", _path.name)
            threading.Thread(target=_enrich, daemon=True).start()
        self._run_timer(runtime)

    def _probe_lm(self, cfg: dict) -> None:
        provider, lm = self._active_provider_cfg(cfg)
        try:
            lm_client.list_models(lm["base_url"], api_key=lm.get("api_key", ""))
            if self._lm_unavailable:
                log.info("%s reachable again — resuming generate mode", provider)
                self._push_status(f"{provider} reconnected — resuming generate mode")
            self._lm_unavailable = False
        except lm_client.LMClientError:
            self._lm_unavailable = True

    # ── Generate cycle ─────────────────────────────────────────────────────────

    def _generate_cycle(self, cfg: dict) -> None:
        self._skip.clear()
        self._discard_current.clear()

        if not self._wait_for_display():
            return

        # PICK MODE
        self._tx("PICK_MODE")
        mode, prompt_data = self._pick_mode(cfg)
        if mode is None:
            time.sleep(5)
            return

        # Resolve model (surprise me picks at random)
        _, lm_cfg = self._active_provider_cfg(cfg)
        model = lm_cfg["model"]
        if lm_cfg.get("surprise_me"):
            try:
                available = lm_client.list_models(lm_cfg["base_url"], exclude_embedding=True,
                                                  api_key=lm_cfg.get("api_key", ""))
                if available:
                    model = random.choice(available)
                    self._broadcast({"type": "model_selected", "model": model})
                    log.info("Surprise Me selected model: %s", model)
            except lm_client.LMClientError:
                pass  # fall back to configured model

        # GENERATE
        self._tx("GENERATE", mode=mode, model=model)
        html, draft_metrics = self._generate_draft(cfg, mode, prompt_data, model)
        if html is None:
            self._tx("DISCARD")
            return

        # Save to temp for audition
        temp_path = self._save_temp(html)
        brief = self._build_brief(cfg, mode, prompt_data, html)

        # VALIDATE + REPAIR (classic) or the iterative edit/check session
        strategy = cfg.get("generation_strategy", "classic")
        html, last_error, session_meta, run_metrics = self._run_strategy(
            cfg, temp_path, html, model, brief
        )
        provider, _ = self._active_provider_cfg(cfg)
        live_row = {
            "run": "live", "arm": strategy, "mode": mode,
            "effect": prompt_data.get("effect", mode) if isinstance(prompt_data, dict) else mode,
            "provider": provider, "model": model, **draft_metrics, **run_metrics,
        }
        if html is None:
            metrics.append(metrics.LIVE_PATH, {**live_row, "archived": False})
            kind = (last_error or {}).get("kind")
            if kind:
                if mode == "stock" and isinstance(prompt_data, dict):
                    stats_mod.record_failure(
                        prompt_data.get("effect", "unknown"), kind, model=model
                    )
                elif mode in ("creative", "update"):
                    stats_mod.record_failure(mode, kind, model=model)
                self._tx("DISCARD", kind=kind, error=(last_error or {}).get("msg"))
            else:
                self._tx("DISCARD")
            temp_path.unlink(missing_ok=True)
            return

        # DISPLAY
        source_path = prompt_data.get("source_path") if isinstance(prompt_data, dict) else None
        effect_slug = prompt_data.get("effect", mode) if isinstance(prompt_data, dict) else mode

        html = self._display_phase(cfg, html, temp_path, effect_slug, mode, model)
        if html is None:
            log.info("Generate-mode demo discarded by user before archiving")
            metrics.append(metrics.LIVE_PATH, {**live_row, "archived": False, "user_discarded": True})
            self._tx("IDLE")
            return
        display_meta_result = self._last_display_meta

        self._tx("ARCHIVE")
        archive_path = archive_mod.save(html, effect_slug, mode, cfg,
                                        source_path=source_path, model=model)
        temp_path.unlink(missing_ok=True)
        if display_meta_result:
            archive_mod.update_meta(archive_path.name, display_meta_result, cfg)
        archive_mod.update_meta(
            archive_path.name,
            {"strategy": strategy, **({"session": session_meta} if session_meta else {})},
            cfg,
        )

        metrics.append(metrics.LIVE_PATH, {**live_row, "archived": True})

        # Record successful run in stats
        if mode == "stock" and isinstance(prompt_data, dict):
            stats_mod.record_run(prompt_data.get("effect", "unknown"), model=model)
        elif mode in ("creative", "update"):
            stats_mod.record_run(mode, model=model)

        self._tx("IDLE")

    def _generate_draft(self, cfg: dict, mode: str, prompt_data: dict, model: str) -> tuple[str | None, dict]:
        """Stream the first draft; returns (html or None, draft metrics)."""
        self._acct_begin()
        t0 = time.time()
        html = self._do_generate(cfg, mode, prompt_data, model=model)
        a, self._acct = self._acct, {}
        return html, {
            "draft_seconds": round(time.time() - t0, 1),
            "draft_output_chars": a["output_chars"],
            "draft_completion_tokens": a["completion_tokens"] if a["tokens_known"] else None,
        }

    def _run_strategy(self, cfg: dict, temp_path: Path, html: str, model: str,
                      brief: str) -> tuple[str | None, dict | None, dict | None, dict]:
        """Run the configured strategy (classic validate/repair or the iterative session) on
        a draft. Returns (html or None, last_error, session_meta, metrics) — metrics are the
        comparable per-strategy fields described in metrics.py."""
        strategy = cfg.get("generation_strategy", "classic")
        self._acct_begin()
        t0 = time.time()
        session_meta = None
        if strategy == "iterative":
            out, last_error, session_meta = self._iterative_session(cfg, temp_path, html, model, brief)
        else:
            out, last_error = self._validate_and_repair(
                cfg, temp_path, html, model, cfg["validation"]["max_repair_attempts"],
                cfg["validation"]["audition_seconds"], brief=brief)
        a, self._acct = self._acct, {}
        checks = [c for c in a["checks"] if c]
        kind = None if out is not None else (last_error or {}).get("kind")
        if session_meta:
            outcome = session_meta["outcome"]
            visual = session_meta.get("visual") or {}
            decisions = [r.get("decision") for r in visual.get("rounds", [])]
            final_rev = session_meta.get("final_revision")
            fallback = bool(out is not None and final_rev and final_rev < session_meta.get("revisions", 0))
            budget = outcome == "budget"
            responses = session_meta.get("turns", 0)
        else:
            outcome = "passed" if out is not None else ("repairs_exhausted" if kind else
                                                        (last_error or {}).get("msg") or "failed")
            decisions, fallback = [], False
            budget = kind is not None
            responses = a["repairs"]
        return out, last_error, session_meta, {
            "strategy": strategy,
            "seconds": round(time.time() - t0, 1),
            "initial_result": checks[0] if checks else None,
            "final_ok": out is not None,
            "final_kind": kind,
            "outcome": outcome,
            "checks": len(checks),
            "repairs": a["repairs"],
            "responses": responses,
            "tool_calls": (session_meta or {}).get("tool_calls", 0),
            "vision_calls": (session_meta or {}).get("vision_calls", 0),
            "llm_calls": a["llm_calls"],
            "output_chars": a["output_chars"],
            "completion_tokens": a["completion_tokens"] if a["tokens_known"] else None,
            "tokens_known": a["tokens_known"] and a["llm_calls"] > 0,
            "fallback": fallback,
            "budget_exhausted": budget,
            "changed_from_draft": out is not None and out != html,
            "visual_decisions": decisions,
        }

    # ── Evaluation (boot_mode == "evaluate") ───────────────────────────────────

    def _eval_cycle(self, cfg: dict) -> None:
        """Run the next trial of the active evaluation run: one shared first draft, then every
        arm on that same draft, recording metrics and outputs (never archived). When the
        run is finished or gone, restore the boot mode it started from."""
        self._skip.clear()
        self._discard_current.clear()
        plan = evaluation.active_run()
        if (plan is None or plan["status"] in ("done", "stopped")
                or plan["next"] >= len(plan["trials"])):
            self._finish_eval(plan)
            return
        if not self._wait_for_display():
            return
        run_id = plan["run_id"]
        if plan["status"] == "pending":
            plan["status"] = "running"
            evaluation.save_plan(plan)

        idx = plan["next"]
        trial = plan["trials"][idx]
        base_cfg = copy.deepcopy(cfg)
        base_cfg["llm_provider"] = plan["provider"]
        provider, lm = self._active_provider_cfg(base_cfg)
        model = plan.get("model") or lm["model"]
        label = f"EVAL {plan['name']} — TRIAL {idx + 1}/{len(plan['trials'])}"

        prompt_data = self._trial_prompt(trial, base_cfg)
        if prompt_data is None:
            self._advance_eval(run_id, f"{trial['id']}: skipped — prompt or update source unavailable")
            return

        self._tx("GENERATE", mode=trial["mode"], model=model)
        self._push_status(f"{label} — WRITING SHARED DRAFT")
        html, draft_metrics = self._generate_draft(base_cfg, trial["mode"], prompt_data, model)
        if self._stop.is_set():
            return   # resume this trial next time
        if html is None:
            if self._lm_unavailable or self._skip.is_set():
                time.sleep(5)   # provider down or skipped mid-draft: retry the trial
                return
            self._advance_eval(run_id, f"{trial['id']}: no draft produced")
            return

        out_dir = evaluation.run_dir(run_id)
        (out_dir / f"{trial['id']}_draft.html").write_text(html, encoding="utf-8")
        brief = self._build_brief(base_cfg, trial["mode"], prompt_data, html)
        for arm in plan["arms"]:
            arm_cfg = cfg_mod._deep_merge(base_cfg, arm["overrides"])
            temp_path = self._save_temp(html)
            self._push_status(f"{label} — {arm['name'].upper()}")
            try:
                out, _err, _meta, run_metrics = self._run_strategy(arm_cfg, temp_path, html, model, brief)
            finally:
                temp_path.unlink(missing_ok=True)
            if self._stop.is_set():
                return
            if self._skip.is_set():
                self._skip.clear()
                self._advance_eval(run_id, f"{trial['id']}: skipped by user during {arm['name']}")
                return
            out_file = out_dir / evaluation.output_name(trial["id"], arm["name"])
            if out is not None:
                out_file.write_text(out, encoding="utf-8")
            else:
                out_file.unlink(missing_ok=True)
            evaluation.append_result(run_id, {
                "trial": trial["id"], "arm": arm["name"], "mode": trial["mode"],
                "effect": trial["effect"], "category": trial.get("category"),
                "provider": provider, "model": model, **draft_metrics, **run_metrics,
                "file": out_file.name if out is not None else None,
            })
        self._advance_eval(run_id)
        self._tx("IDLE")

    def _trial_prompt(self, trial: dict, cfg: dict) -> dict | None:
        if trial["mode"] == "stock":
            row = self._csv_row(trial["effect"])
            return row
        if trial["mode"] == "update":
            if not trial.get("source"):
                return None
            src = archive_mod._archive_dir(cfg) / trial["source"]
            return {"effect": "update", "source_path": src} if src.exists() else None
        return {"effect": "creative"}

    def _csv_row(self, effect: str) -> dict | None:
        csv_path = BASE / "prompts.csv"
        try:
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("Effect") == effect:
                        return {"effect": effect, "prompt": row.get("Three.js prompt", "")}
        except OSError:
            log.exception("Failed to read prompts.csv")
        return None

    def _advance_eval(self, run_id: str, note: str | None = None) -> None:
        plan = evaluation.load_plan(run_id)
        if not plan:
            return
        plan["next"] += 1
        if note:
            log.warning("Evaluation %s: %s", run_id, note)
            plan.setdefault("notes", []).append(note)
        if plan["next"] >= len(plan["trials"]) and plan["status"] == "running":
            plan["status"] = "done"
        evaluation.save_plan(plan)

    def _finish_eval(self, plan: dict | None) -> None:
        """Leave evaluate mode: mark the run done, clear the active pointer and restore the
        boot mode the run started from."""
        if plan and plan["status"] not in ("done", "stopped"):
            plan["status"] = "done"
            evaluation.save_plan(plan)
        evaluation.clear_active()
        disk = cfg_mod.load()
        resume = (plan or {}).get("resume_boot_mode") or "generate"
        disk["boot_mode"] = resume if resume != "evaluate" else "generate"
        cfg_mod.save(disk)
        self._broadcast({"type": "config_push", "config": disk})
        log.info("Evaluation finished%s; boot mode back to %s",
                 f" ({plan['run_id']})" if plan else "", disk["boot_mode"])
        self._push_status("EVALUATION COMPLETE")
        self._tx("IDLE")

    def _validate_and_repair(self, cfg: dict, temp_path: Path, html: str, model: str,
                              max_repairs: int, audition_secs: float,
                              brief: str = "") -> tuple[str | None, dict | None]:
        """Runs the audition→repair cycle against temp_path/html.

        Returns (final_html, None) on success, or (None, last_error) if repairs were
        exhausted or a repair call itself failed. Does not record stats, does not broadcast
        DISCARD, and does not delete temp_path on failure — the caller decides what
        exhaustion means.

        An audition TIMEOUT is an infrastructure problem, not a demo failure: it never
        triggers a repair. With no display connected it waits for one and re-auditions; with
        a display connected it retries once, then gives up with kind=None (so the caller
        discards without recording a failure in stats).

        `brief` (the original request, see _build_brief) and the history of failed attempts
        are carried into every repair request so repairs stay faithful to what was asked for.
        """
        last_error: dict = {"kind": None, "msg": None}
        history: list[str] = []
        for attempt in range(max_repairs + 1):
            self._tx("VALIDATE", attempt=attempt)
            result = self._audition_with_retry(temp_path, audition_secs)
            if result is None:
                return None, {"kind": None, "msg": "no display could run the audition"}

            if result.get("result") == "OK":
                return html, None

            last_error = {
                "kind": result.get("result", "CRASHED"),
                "msg": result.get("error", ""),
            }
            diagnostics = result.get("diagnostics") or {}

            if attempt >= max_repairs:
                log.info("Max repairs reached")
                return None, last_error

            error_msg = last_error["msg"] or "unknown error"
            if cfg["display"].get("show_retry_messages", True):
                self._push_status(
                    f"ERROR DETECTED — REQUESTING REPAIR — ATTEMPT {attempt + 1}/{max_repairs}"
                )

            self._tx("REPAIR", attempt=attempt + 1, max=max_repairs)
            if self._acct:
                self._acct["repairs"] += 1
            html = self._do_repair(cfg, html, error_msg, model=model, brief=brief,
                                   history=history, diagnostics=diagnostics)
            history.append(
                f"Attempt {attempt + 1}: {last_error['kind']} — {error_msg.splitlines()[0][:200]}"
            )
            if html is None:
                return None, {"kind": None, "msg": "repair call failed"}
            temp_path.write_text(html, encoding="utf-8")

        return None, last_error

    def _audition_with_retry(self, temp_path: Path, seconds: float) -> dict | None:
        """_audition() with the infrastructure rules applied: a TIMEOUT never counts as a
        result. With no display connected, wait for one and re-audition; with a display
        connected, retry once. Returns the audit result, or None if the audition couldn't be
        completed (cancelled while waiting, or the display didn't answer twice)."""
        result = self._audition(temp_path, seconds)
        timeouts = 0
        silent_retried = False
        while True:
            if result.get("result") == "TIMEOUT":
                if self._display_count() == 0:
                    if not self._wait_for_display():
                        return None
                else:
                    timeouts += 1
                    if timeouts > 1:
                        log.warning("Display did not answer the audition twice; abandoning this demo")
                        return None
                    log.warning("Audition timed out with a display connected; retrying once")
            elif self._never_ran(result) and not silent_retried:
                # The probe heartbeats from its own frame loop whatever the demo does, so
                # zero heartbeats and no errors means the page never ran (a load hiccup), not
                # a demo bug. Re-run once rather than sending a working demo for repair.
                silent_retried = True
                if self._acct and self._acct["checks"]:
                    self._acct["checks"].pop()
                log.warning("Audition got no heartbeats at all; re-running it once")
            else:
                return result
            result = self._audition(temp_path, seconds)

    @staticmethod
    def _never_ran(result: dict) -> bool:
        diag = result.get("diagnostics") or {}
        return (result.get("result") == "BLANK" and diag.get("heartbeats") == 0
                and not diag.get("errors"))

    # ── Iterative strategy ─────────────────────────────────────────────────────

    def _iterative_session(self, cfg: dict, temp_path: Path, html: str, model: str,
                           brief: str) -> tuple[str | None, dict | None, dict]:
        """Runs agent_runner.IterativeSession on the classic-streamed first draft.

        Returns (html, last_error, session_meta) with the same contract as
        _validate_and_repair() for the first two: html is the revision to display/archive or
        None, and last_error["kind"] is None when the failure wasn't the demo's fault
        (cancelled, no display, LLM unreachable) so the caller records nothing in stats.
        """
        provider, lm = self._active_provider_cfg(cfg)
        it = cfg.get("iterative", {})
        budgets = agent_runner.Budgets(
            max_turns=int(it.get("max_turns", 6)),
            max_tool_calls=int(it.get("max_tool_calls", 24)),
            deadline_seconds=float(it.get("session_deadline_seconds", 600)),
            run_seconds=float(cfg["validation"]["audition_seconds"]),
            max_run_seconds=float(it.get("max_run_seconds", 15)),
            max_polish_rounds=int(it.get("max_polish_rounds", 1)),
        )
        extra_params = ({"reasoning_effort": lm["thinking_effort"]}
                        if provider in ("ninfer", "strata") else None)

        def should_abort() -> bool:
            return self._skip.is_set() or self._stop.is_set()

        def llm(messages, tools):
            r = lm_client.chat_collect(
                messages=messages,
                base_url=lm["base_url"],
                model=model or lm["model"],
                max_tokens=lm["max_tokens"],
                temperature=lm["temperature"],
                timeout=lm["request_timeout_seconds"],
                extra_params=extra_params,
                api_key=lm.get("api_key", ""),
                tools=tools,
                should_abort=should_abort,
            )
            self._acct_llm(len(r.get("content") or "")
                           + sum(len(tc.get("arguments") or "") for tc in r.get("tool_calls") or []),
                           r.get("usage"))
            return r

        def run(source: str, seconds: float):
            temp_path.write_text(source, encoding="utf-8")
            return self._audition_with_retry(temp_path, seconds)

        def on_source(source: str, rev: int, lines: tuple[int, int], summary: str) -> None:
            self._broadcast({"type": "source", "html": source, "rev": rev,
                             "lines": list(lines), "summary": summary})

        def on_status(text: str) -> None:
            busy = text.startswith(("THINKING", "REVIEWING", "COMPARING"))
            self._tx("ITERATE", text=text, busy=busy)

        capture = None
        visual_prompts = None
        if it.get("visual_review"):
            visual_prompts = {"critique": cfg["system_prompts"].get("visual_critique", ""),
                              "compare": cfg["system_prompts"].get("visual_compare", "")}
            if it.get("vision_input") == "video" and provider == "ninfer":
                def capture():
                    return self._capture_video(float(it.get("video_capture_seconds", 8)))
            else:
                if it.get("vision_input") == "video":
                    log.warning("Video input for visual review is Ninfer-only; using frames on %s", provider)

                def capture():
                    return self._capture_frames(int(it.get("capture_frames", 4)),
                                                int(it.get("capture_interval_ms", 1500)),
                                                int(it.get("capture_max_px", 768)))

        session = agent_runner.IterativeSession(
            brief=brief,
            html=html,
            system_prompt=cfg["system_prompts"].get("iterative", ""),
            budgets=budgets,
            interaction=it.get("interaction", "auto"),
            hooks=agent_runner.SessionHooks(llm=llm, run=run, on_source=on_source,
                                            on_status=on_status, should_abort=should_abort,
                                            capture=capture),
            visual_prompts=visual_prompts,
        )
        result = session.run()
        meta = {
            "outcome": result.outcome,
            "turns": result.turns,
            "tool_calls": result.tool_calls,
            "revisions": result.revisions,
            "final_revision": result.revision,
            "interaction": "native_tools" if session.use_tools else "json_actions",
            "vision_calls": result.vision_calls,
            **({"visual": result.visual} if result.visual else {}),
        }
        log.info("Iterative session: %s", meta)

        if result.outcome == "llm_error":
            self._lm_unavailable = True
        if result.html is None:
            if result.outcome in ("cancelled", "infra", "llm_error"):
                return None, {"kind": None, "msg": result.outcome}, meta
            return None, result.last_error or {"kind": None, "msg": result.outcome}, meta
        if result.revision != session.last_checked:
            # Falling back to an earlier passing revision: the display's iframe still holds the
            # last (failed) one at the same URL, and validation must be tied to the exact source
            # shown — so run the fallback once more, which also loads it into the iframe.
            check = run(result.html, budgets.run_seconds)
            if check is None:
                return None, {"kind": None, "msg": "no display could re-check the fallback"}, meta
            if check.get("result") != "OK":
                log.warning("Fallback revision %s failed its re-check: %s", result.revision, check)
                return None, {"kind": check.get("result"), "msg": check.get("error", "")}, meta
        else:
            temp_path.write_text(result.html, encoding="utf-8")
        return result.html, None, meta

    def _capture_frames(self, count: int, interval_ms: int, max_px: int) -> dict | None:
        """Ask the display page for `count` JPEG frames of the demo currently running in its
        iframe. Returns {"parts": image_url content parts, "note": description} or None."""
        capture_id = uuid.uuid4().hex[:8]
        validator.start_capture(capture_id)
        self._broadcast({"type": "capture_frames", "capture_id": capture_id, "count": count,
                         "interval_ms": interval_ms, "max_px": max_px})
        res = validator.wait_for_capture(timeout=count * interval_ms / 1000 + 10)
        if res is None:
            log.warning("Frame capture %s timed out", capture_id)
            return None
        frames = [f for f in (res.get("frames") or [])
                  if str(f.get("data", "")).startswith("data:image/")]
        if not frames:
            log.warning("Frame capture %s returned no frames: %s", capture_id, res.get("error"))
            return None
        times = ", ".join(f"{float(f.get('t') or 0):.1f}s" for f in frames)
        note = (f"{len(frames)} frames ({frames[0].get('width')}x{frames[0].get('height')}) "
                f"taken {times} after the demo loaded")
        if (res.get("canvases") or 1) > 1:
            note += (f"; the demo uses {res['canvases']} WebGL canvases and only the last one "
                     "is captured, so the frames may not show the whole composition")
        log.info("Captured %s", note)
        return {"parts": [{"type": "image_url", "image_url": {"url": f["data"]}} for f in frames],
                "note": note}

    def _capture_video(self, seconds: float) -> dict | None:
        """Ninfer visual review with video input: record the running demo at 2 fps and
        transcode it to the MP4 format the vision model expects."""
        raw = self._record_video(seconds=seconds, fps=2)
        if raw is None:
            return None
        mp4 = None
        try:
            mp4 = self._transcode_for_vision(raw)
            if mp4 is None:
                return None
            b64 = base64.b64encode(mp4.read_bytes()).decode("ascii")
            return {"parts": [{"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}],
                    "note": f"{seconds:.0f}s video at 2 fps of the demo running"}
        finally:
            raw.unlink(missing_ok=True)
            if mp4 is not None:
                mp4.unlink(missing_ok=True)

    def _display_phase(self, cfg: dict, html: str, temp_path: Path, effect_slug: str,
                        mode: str, model: str) -> str | None:
        """Runs one full display cycle for `html`: title/meta generation (JIT or background,
        per the jit_titling setting), the DISPLAY state broadcast, the runtime timer, and the
        discard check.

        Returns `html` unchanged on normal completion (ready for the caller to archive), or
        None if the user discarded during this display (caller must skip archiving and go to
        IDLE). Sets self._last_display_meta to the generated title/description dict (or None)
        so the caller can use it after this returns.
        """
        runtime = cfg["display"]["demo_runtime_seconds"]
        stats_key = effect_slug if mode == "stock" else mode
        stats_summary = self._meta_stats(stats_key, model)
        self._last_display_meta = None

        jit_titling = cfg["display"].get("jit_titling")
        meta_thread = None
        _meta_result: list = [None]

        if jit_titling:
            jit_result = self._generate_title_meta(cfg, html, model=model)
            self._last_display_meta = jit_result
            display_meta = {
                "effect":      effect_slug,
                "model":       model,
                "title":       jit_result.get("title", "") if jit_result else "",
                "description": jit_result.get("description", "") if jit_result else "",
                "stats":       stats_summary,
            }
        else:
            display_meta = {
                "effect":      effect_slug,
                "model":       model,
                "title":       "",
                "description": "",
                "stats":       stats_summary,
            }

        self._tx("DISPLAY", url=f"/temp/{temp_path.name}", runtime=runtime, meta=display_meta)

        if not jit_titling:
            def _fetch_meta():
                _meta_result[0] = self._generate_title_meta(cfg, html, model=model)
            meta_thread = threading.Thread(target=_fetch_meta, daemon=True)
            meta_thread.start()

        self._run_timer(runtime)

        if self._discard_current.is_set():
            self._discard_current.clear()
            temp_path.unlink(missing_ok=True)
            return None

        if meta_thread is not None:
            meta_thread.join(timeout=10)
            self._last_display_meta = _meta_result[0]

        return html

    # ── Video capture (Ninfer visual review with video input) ─────────────────

    def _record_video(self, seconds: float, fps: int) -> Path | None:
        record_id = uuid.uuid4().hex[:8]
        validator.start_recording(record_id)
        self._broadcast({
            "type": "record_video",
            "seconds": seconds,
            "fps": fps,
            "record_id": record_id,
        })
        result = validator.wait_for_recording(timeout=seconds + 20)
        if result is None:
            log.warning(
                "Video recording timed out after %.0fs waiting for the browser to upload "
                "(no probe_record_done/probe_record_error/upload was received at all — check "
                "whether the record_video WS message and the postMessage relay into the iframe "
                "are actually arriving)",
                seconds + 20,
            )
            return None
        if result.get("status") != "uploaded":
            log.warning("Video recording failed: %s", result.get("error", "unknown error"))
            return None
        path = result.get("path")
        return Path(path) if path else None

    def _transcode_for_vision(self, raw_path: Path) -> Path | None:
        if shutil.which("ffmpeg") is None:
            log.warning("ffmpeg not found on PATH; video-check feature unavailable this cycle")
            return None
        out_path = raw_path.with_suffix(".vision.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", str(raw_path),
            "-vf", "fps=2,scale='min(768,iw)':'min(768,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(out_path),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("ffmpeg transcode failed: %s", e)
            return None
        if r.returncode != 0 or not out_path.exists():
            stderr_tail = r.stderr[-500:].decode(errors="replace") if r.stderr else ""
            log.warning("ffmpeg transcode failed (exit %d): %s", r.returncode, stderr_tail)
            return None
        return out_path

    def _run_timer(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set() or self._skip.is_set():
                break
            time.sleep(0.25)
        self._skip.clear()

    # ── Mode selection ─────────────────────────────────────────────────────────

    def _pick_mode(self, cfg: dict) -> tuple[str | None, dict | None]:
        weights = cfg["generate_mode_weights"]
        pool = [(m, w) for m, w in weights.items() if w > 0]
        if not pool:
            log.warning("All mode weights are 0")
            return None, None

        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        cumulative = 0.0
        chosen = pool[-1][0]
        for m, w in pool:
            cumulative += w
            if r <= cumulative:
                chosen = m
                break

        if chosen == "update":
            _, lm = self._active_provider_cfg(cfg)
            max_input = max(0, lm["context_ceiling_tokens"] - lm["max_tokens"])
            src = archive_mod.pick_for_update(cfg, max_input)
            if src is None:
                log.warning(
                    "Update mode: no suitable archive files (max_input=%d tokens, %d files checked)",
                    max_input, len(archive_mod.list_files(cfg))
                )
                chosen = "stock"
            else:
                return "update", {"effect": "update", "source_path": src}

        if chosen == "stock":
            row = self._pick_csv_row()
            return ("stock", row) if row else (None, None)

        # creative
        return "creative", {"effect": "creative"}

    def _pick_csv_row(self) -> dict | None:
        csv_path = BASE / "prompts.csv"
        if not csv_path.exists():
            log.error("prompts.csv not found")
            return None
        if not self._stock_deck:
            try:
                with open(csv_path, "r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                log.exception("Failed to read prompts.csv")
                return None
            if not rows:
                return None
            self._stock_deck = rows.copy()
            random.shuffle(self._stock_deck)
            log.info("Stock deck refilled: %d prompts", len(self._stock_deck))
        row = self._stock_deck.pop()
        return {
            "effect": row.get("Effect", "unknown"),
            "prompt": row.get("Three.js prompt", ""),
        }

    def _csv_text_for_creative(self, cfg: dict) -> str:
        csv_path = BASE / "prompts.csv"
        if not csv_path.exists():
            return ""
        _, lm = self._active_provider_cfg(cfg)
        max_chars = (lm["context_ceiling_tokens"] - lm["max_tokens"]) * 4 - 500
        content = csv_path.read_text(encoding="utf-8")
        if len(content) <= max_chars:
            return content
        reader = csv.reader(StringIO(content))
        rows = list(reader)
        header = rows[:1]
        data = sorted(rows[1:], key=lambda r: len(",".join(r)))
        out = list(header)
        total = len(",".join(header[0])) if header else 0
        for row in data:
            line = ",".join(row)
            if total + len(line) + 1 > max_chars:
                break
            out.append(row)
            total += len(line) + 1
        log.warning("CSV truncated to %d rows for token budget", len(out) - 1)
        import io
        buf = io.StringIO()
        csv.writer(buf).writerows(out)
        return buf.getvalue()

    # ── Generation ─────────────────────────────────────────────────────────────

    def _build_messages(self, cfg: dict, mode: str, prompt_data: dict) -> list[dict]:
        sys_prompts = cfg["system_prompts"]
        if mode == "stock":
            return [
                {"role": "system", "content": sys_prompts["stock"]},
                {"role": "user", "content": prompt_data.get("prompt", "")},
            ]
        if mode == "creative":
            csv_text = self._csv_text_for_creative(cfg)
            return [
                {"role": "system", "content": sys_prompts["creative"]},
                {"role": "user", "content": f"Stock effects for inspiration:\n\n{csv_text}"},
            ]
        if mode == "update":
            src: Path = prompt_data.get("source_path")
            src_html = src.read_text(encoding="utf-8") if src and Path(src).exists() else ""
            return [
                {"role": "system", "content": sys_prompts["update"]},
                {"role": "user", "content": f"Existing demo to update:\n\n{src_html}"},
            ]
        return []

    def _stream(self, cfg: dict, messages: list[dict], model: str = "") -> tuple[str | None, str | None]:
        """Run the LLM stream; return (html, error_msg)."""
        provider, lm = self._active_provider_cfg(cfg)
        tokens: list[str] = []
        start = time.time()
        extra_params = ({"reasoning_effort": lm["thinking_effort"]}
                        if provider in ("ninfer", "strata") else None)
        max_tokens = lm["max_tokens"]
        stream_stats: dict = {}

        try:
            for tok in lm_client.chat_stream(
                messages=messages,
                base_url=lm["base_url"],
                model=model or lm["model"],
                max_tokens=max_tokens,
                temperature=lm["temperature"],
                timeout=lm["request_timeout_seconds"],
                extra_params=extra_params,
                api_key=lm.get("api_key", ""),
                stats=stream_stats,
            ):
                if self._skip.is_set():
                    log.info("Aborting LLM stream: skip requested")
                    return None, None
                tokens.append(tok)
                self._push_token(tok)
        except lm_client.LengthFinishError:
            log.warning("LLM hit token limit")
            if not tokens:
                return None, "LLM produced no output before token limit"
            # Fall through; repair will fix the truncated output
            html = validator.strip_fences("".join(tokens))
            return html, "output was truncated at token limit"
        except lm_client.LMClientError as e:
            log.error("%s unreachable: %s", provider, e)
            self._lm_unavailable = True
            return None, None
        finally:
            self._acct_llm(sum(len(t) for t in tokens), stream_stats.get("usage"))

        # Wait for display to drain the typing queue
        total_chars = sum(len(t) for t in tokens)
        display_rate = cfg["display"].get("display_chars_per_sec", 400)
        min_typing = cfg["display"].get("min_typing_seconds", 8)
        elapsed = time.time() - start
        drain_time = total_chars / display_rate
        wait = max(0.0, min_typing - elapsed, drain_time - elapsed)
        if wait > 0:
            time.sleep(wait)

        return validator.strip_fences("".join(tokens)), None

    def _do_generate(self, cfg: dict, mode: str, prompt_data: dict, model: str = "") -> str | None:
        messages = self._build_messages(cfg, mode, prompt_data)
        html, err = self._stream(cfg, messages, model=model)
        if err and html:
            log.warning("Generation had error: %s", err)
        return html

    def _do_repair(self, cfg: dict, html: str, error: str, model: str = "", brief: str = "",
                   history: list[str] | None = None, diagnostics: dict | None = None) -> str | None:
        repair_prompt = cfg["system_prompts"]["repair"].format(error=error)
        parts = []
        if brief:
            parts.append(f"Original brief (the fixed demo must still deliver this):\n{brief}")
        if history:
            parts.append("Earlier repair attempts that still failed:\n" + "\n".join(history))
        diag_text = validator.format_diagnostics(diagnostics or {})
        if diag_text:
            parts.append(f"Diagnostics from the failed run:\n{diag_text}")
        parts.append(f"Current HTML:\n{html}")
        messages = [
            {"role": "system", "content": repair_prompt},
            {"role": "user", "content": "\n\n".join(parts)},
        ]
        repaired, _ = self._stream(cfg, messages, model=model)
        return repaired

    _BRIEF_MAX_CHARS = 2000

    def _build_brief(self, cfg: dict, mode: str, prompt_data: dict | None, html: str) -> str:
        """A compact statement of what this cycle was asked to produce, carried into every
        repair so a fix can't quietly drift into a different effect. Never includes the full
        source of an update-mode original (it would eat the repair's context budget)."""
        data = prompt_data if isinstance(prompt_data, dict) else {}
        if mode == "stock":
            brief = f"Effect: {data.get('effect', 'unknown')}\n{data.get('prompt', '')}"
        elif mode == "update":
            src = data.get("source_path")
            name = Path(src).name if src else "an archived demo"
            title = archive_mod.get_meta(name, cfg).get("title", "") if src else ""
            brief = (f"A fresh take (new aesthetic and parameters, same core idea) on the "
                     f"existing demo {name}" + (f" (\"{title}\")" if title else "") + ".")
        else:
            m = re.search(r"<title[^>]*>([^<]{1,120})</title>", html, re.IGNORECASE)
            concept = f" Its working title is \"{m.group(1).strip()}\"." if m else ""
            brief = ("An original demoscene effect invented by the model (creative mode)."
                     + concept + " Keep that concept rather than switching to a different effect.")
        return brief[: self._BRIEF_MAX_CHARS]


    # ── Title / description generation ────────────────────────────────────────

    def _meta_stats(self, effect_key: str, model: str) -> dict:
        raw = stats_mod.load()
        def _row(entry):
            runs  = entry.get("runs", 0)
            fails = entry.get("total", 0)
            return {
                "runs": runs, "fails": fails,
                "deletions": entry.get("deletions", 0),
                "fail_pct": round(fails / runs * 100, 1) if runs else None,
            }
        eff_entry = raw.get(effect_key, {})
        mdl_entry = eff_entry.get("by_model", {}).get(model, {}) if model else {}
        return {"effect": _row(eff_entry), "model": _row(mdl_entry)}

    def _generate_title_meta(self, cfg: dict, html: str, model: str = "") -> dict | None:
        import json as _json
        prompt = cfg["system_prompts"].get("title", "")
        if not prompt:
            return None
        provider, lm = self._active_provider_cfg(cfg)
        use_model = model or lm["model"]
        if provider in ("ninfer", "strata"):
            # Title/description generation is cheap and low-stakes — always use minimal
            # thinking effort here regardless of the user's configured setting, mirroring
            # the /no_think convention used on the lm_studio branch below. Strata takes the same
            # "none" value (its levels are none/low/medium/high).
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": html[:6000]},
            ]
            extra_params = {"reasoning_effort": "none"}
        else:
            messages = [
                {"role": "system", "content": prompt},
                # /no_think disables reasoning mode on Qwen3 thinking models; harmless on others
                {"role": "user", "content": "/no_think\n" + html[:6000]},
            ]
            extra_params = None
        text = None
        try:
            text = lm_client.chat(
                messages=messages,
                base_url=lm["base_url"],
                model=use_model,
                max_tokens=None,
                temperature=0.7,
                timeout=120,
                extra_params=extra_params,
                api_key=lm.get("api_key", ""),
            )
            if not text or not text.strip():
                log.warning("Title generation: empty response (model=%s)", use_model)
                return None
            start = text.find('{')
            end   = text.rfind('}')
            if start == -1 or end == -1:
                log.warning(
                    "Title generation: no JSON object in response (model=%s): %r",
                    use_model, text[:300],
                )
                return None
            data = _json.loads(text[start:end + 1])
            return {
                "title": str(data.get("title", "")),
                "description": str(data.get("description", "")),
            }
        except Exception as e:
            log.warning(
                "Title generation failed (model=%s): %s — response: %r",
                use_model, e, (text or "")[:300],
            )
            return None

    # ── Temp file & audition ───────────────────────────────────────────────────

    def _save_temp(self, html: str) -> Path:
        temp_dir = BASE / "temp"
        temp_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        h = hashlib.md5(html.encode()).hexdigest()[:6]
        path = temp_dir / f"demo_{ts}_{h}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def _audition(self, path: Path, seconds: float) -> dict:
        """Ask the display page to run temp file `path` for `seconds`. Returns the display's
        audit_result, or {"result": "TIMEOUT"} if no display answered (an infrastructure
        problem, never treated as a pass)."""
        audition_id = uuid.uuid4().hex[:8]
        validator.start_validation(audition_id)
        self._broadcast({
            "type": "audition",
            # The probe reads ?aid= and stamps it on every message, so the display page can
            # ignore messages from a previous iframe load.
            "url": f"/temp/{path.name}?aid={audition_id}",
            "seconds": seconds,
            "audition_id": audition_id,
        })
        result = validator.wait_for_result(timeout=seconds + 15)
        if result is None:
            log.warning("Audition %s timed out — no display replied", audition_id)
            return {"result": "TIMEOUT"}
        if self._acct:
            self._acct["checks"].append(result.get("result"))
        diag = result.get("diagnostics") or {}
        log.info(
            "Audition %s: %s%s | render_fps=%s heartbeat_fps=%s lit=%s/%s frozen=%ss errors=%d console=%d",
            audition_id, result.get("result"),
            f" ({result.get('error')})" if result.get("error") else "",
            diag.get("render_fps"), diag.get("heartbeat_fps"),
            diag.get("visible_cells"), diag.get("grid_cells"), diag.get("frozen_seconds"),
            len(diag.get("errors") or []), len(diag.get("console") or []),
        )
        return result
