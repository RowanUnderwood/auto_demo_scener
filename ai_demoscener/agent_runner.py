"""Bounded model/tool loop for the iterative generation strategy.

The session starts from a first draft that the orchestrator already streamed the classic
way (revision 1), runs it, and — only if it failed or its diagnostics look suspicious —
asks the model for targeted edits until a revision passes, the model finishes, or a budget
runs out.

Each turn is rebuilt from session state (brief, budget, change log, latest evidence, current
numbered source) rather than appended to a growing transcript. That keeps requests small
enough for local models' context windows and avoids provider differences in how tool
results and reasoning metadata have to be threaded back into a conversation.

Everything that touches the outside world is injected as a callback, so the loop can be
exercised with simulated model responses and audition results.
"""
import logging
import time
from dataclasses import dataclass, field

import demo_tools
import lm_client
import validator

log = logging.getLogger(__name__)


@dataclass
class Budgets:
    max_turns: int = 6              # model responses per session
    max_tool_calls: int = 24        # executed actions per session
    deadline_seconds: float = 600   # wall clock for the whole session
    run_seconds: float = 5          # ordinary check length
    max_run_seconds: float = 15     # cap for a run_demo(seconds=…) request
    max_polish_rounds: int = 1      # visual critique → polish → compare cycles


@dataclass
class SessionHooks:
    llm: callable          # (messages, tools|None) -> lm_client.chat_collect() result dict
    run: callable          # (html, seconds) -> audit_result dict, or None if no display could run it
    on_source: callable    # (html, revision, (first_line, last_line), summary) -> None
    on_status: callable    # (text) -> None
    should_abort: callable # () -> bool
    # () -> {"parts": [content parts for a multimodal message], "note": str} capturing the
    # revision currently running in the display, or None if no capture could be made.
    # Leave as None to disable visual review.
    capture: callable = None


@dataclass
class SessionResult:
    html: str | None                 # revision to display/archive, or None to discard
    outcome: str                     # see IterativeSession.run()
    revision: int | None = None      # revision number of `html`
    revisions: int = 1               # revisions created in total
    turns: int = 0
    tool_calls: int = 0
    last_error: dict | None = None   # {"kind", "msg"} of the latest failed check
    trace: list[str] = field(default_factory=list)
    vision_calls: int = 0
    visual: dict | None = None       # critique / compare record when visual review ran


class IterativeSession:
    MAX_FORMAT_FAILURES = 2   # one corrective response is allowed, then the session stops
    STUCK_REPEATS = 3         # identical failures in a row with nothing new learned

    def __init__(self, brief: str, html: str, system_prompt: str, budgets: Budgets,
                 interaction: str, hooks: SessionHooks, visual_prompts: dict | None = None):
        self.brief = brief
        self.store = demo_tools.SourceStore(html)
        self.system_prompt = system_prompt
        self.budgets = budgets
        # "native_tools" | "json_actions" | "auto" (native tools; JSON parsed from content if
        # the model answers in text; switches to JSON actions if the server rejects `tools`)
        self.interaction = interaction if interaction in ("native_tools", "json_actions") else "auto"
        self.use_tools = self.interaction != "json_actions"
        self.hooks = hooks
        self.checks: dict[int, dict] = {}      # revision -> latest audit result
        self.history: list[str] = []           # change log shown to the model
        self.feedback: list[str] = []          # results of the last response's actions
        self.trace: list[str] = []
        self.turns = 0
        self.tool_calls = 0
        self.format_failures = 0
        self.attention_used = False
        self.failure_streak: list[str] = []
        self.last_checked: int | None = None   # revision the browser ran most recently
        self.finish_requested = False          # model called finish_demo this turn
        self.started = time.time()
        # Visual review (needs a capture hook and the critique/compare prompts)
        self.visual_prompts = visual_prompts or {}
        self.visual_enabled = bool(hooks.capture and self.visual_prompts.get("critique")
                                   and self.visual_prompts.get("compare"))
        self.phase = "repair"                  # "repair" | "polish"
        self.polish_rounds_left = budgets.max_polish_rounds
        self.baseline_rev: int | None = None   # passing revision the polish started from
        self.baseline_capture: dict | None = None
        self.issues: list[dict] = []           # critique being applied in the polish phase
        self.selected_rev: int | None = None   # revision chosen by the before/after compare
        self.vision_calls = 0
        self.visual: dict = {"enabled": self.visual_enabled, "rounds": []}

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self) -> SessionResult:
        """Outcomes: first_draft_ok, finished, budget, stuck, format_error, llm_error,
        cancelled, infra (no display could run a check), no_actions."""
        outcome = self._loop()
        return self._result(outcome)

    # ── Loop ──────────────────────────────────────────────────────────────────

    def _loop(self) -> str:
        if not self._check(self.budgets.run_seconds):
            return self._interrupted()
        if self._passed() and not self._attention_reasons() and not self.visual_enabled:
            self._log("revision 1 passed with clean diagnostics; no iteration needed")
            return "first_draft_ok"

        while True:
            if self.hooks.should_abort():
                return "cancelled"
            # A passing revision is handled before the budget check: a before/after compare
            # needs no model turn, so a polish that passes on the last turn still gets judged.
            if self._passed():
                done = self._on_passing()
                if done:
                    return done
            if not self._budget_left():
                if time.time() - self.started > self.budgets.deadline_seconds:
                    self._log("session deadline reached")
                return "budget"

            actions, stop = self._ask_model()
            if stop:
                return stop
            if actions is None:
                if self.format_failures >= self.MAX_FORMAT_FAILURES:
                    return "format_error"
                continue

            outcome = self._execute(actions)
            if outcome:
                return outcome
            if self._stuck():
                return "stuck"

    def _on_passing(self) -> str | None:
        """The current revision passed its runtime check. Returns an outcome to stop the
        session, or None if the model gets another turn (attention look or polish)."""
        if self.visual_enabled:
            if self.phase == "repair":
                # Only start a review if there are turns left to act on its findings
                if self.polish_rounds_left > 0 and self._budget_left() and self._start_visual_review():
                    return None
                return "finished"
            if self.store.current == self.baseline_rev:
                if self.finish_requested:
                    self._log("model kept the reviewed revision unchanged")
                    self._record_round(decision="kept (model made no changes)")
                    self.selected_rev = self.baseline_rev
                    self.phase = "repair"
                    return "finished"
                return None   # polish edits still to come
            self._compare_polish()
            if self.polish_rounds_left > 0 and self.visual_enabled:
                return self._on_passing()
            return "finished"
        if self.finish_requested or self.attention_used or not self._attention_reasons():
            return "finished"
        self.attention_used = True
        return None

    def _budget_left(self) -> bool:
        return (self.turns < self.budgets.max_turns
                and self.tool_calls < self.budgets.max_tool_calls
                and time.time() - self.started <= self.budgets.deadline_seconds)

    # ── Visual review ─────────────────────────────────────────────────────────

    def _start_visual_review(self) -> bool:
        """Capture the current (passing) revision and ask for a critique. True if there are
        issues to polish (switches to the polish phase), False to finish as is."""
        rev = self.store.current
        if self.last_checked != rev and not self._check(self.budgets.run_seconds):
            return False   # the capture must show this exact revision
        if not self._passed():
            return False
        self.hooks.on_status(f"INSPECTING VISUALS — REV {rev}")
        cap = self.hooks.capture()
        if not cap:
            self._disable_visual("no capture of the running demo was possible")
            return False
        self.hooks.on_status(f"REVIEWING FRAMES — REV {rev}")
        intro = (f"BRIEF:\n{self.brief}\n\nCapture of revision {rev}: {cap.get('note', '')}")
        reasons = self._attention_reasons(visual_hints=True)
        if reasons:
            intro += "\nAutomatic checks also noticed: " + "; ".join(reasons) + "."
        obj = self._vision_call(self.visual_prompts["critique"],
                                [{"type": "text", "text": intro}, *cap["parts"]])
        if obj is None:
            return False
        issues = []
        for it in (obj.get("issues") or [])[:3]:
            if isinstance(it, dict) and (it.get("problem") or it.get("change")):
                issues.append({k: str(it.get(k) or "")[:300] for k in ("problem", "evidence", "change")})
        self.polish_rounds_left -= 1
        if not issues:
            self._log(f"visual review of revision {rev}: no issues")
            self.visual["rounds"].append({"baseline_revision": rev, "issues": [],
                                          "decision": "kept (no issues found)"})
            self.selected_rev = rev
            return False
        self._log(f"visual review of revision {rev}: {len(issues)} issue(s) — "
                  + "; ".join(i["problem"][:80] for i in issues))
        self.phase = "polish"
        self.baseline_rev = rev
        self.baseline_capture = cap
        self.issues = issues
        self.selected_rev = None
        self.history.append(f"visual review of rev {rev}: {len(issues)} issue(s) to polish")
        return True

    def _compare_polish(self) -> None:
        """The polished revision passed its runtime check: capture it and compare with the
        baseline. Keeps the baseline unless the model clearly prefers the polished one."""
        rev, base = self.store.current, self.baseline_rev
        decision, compare = "before", None
        if self.last_checked != rev and not self._check(self.budgets.run_seconds):
            cap = None
        else:
            self.hooks.on_status(f"INSPECTING POLISHED REV {rev}")
            cap = self.hooks.capture()
        if not cap:
            reason = "no capture of the polished revision"
        else:
            self.hooks.on_status(f"COMPARING REV {base} / REV {rev}")
            issues = "\n".join(f"- {i['problem']} → {i['change']}" for i in self.issues)
            obj = self._vision_call(self.visual_prompts["compare"], [
                {"type": "text", "text": f"BRIEF:\n{self.brief}\n\nIssues the polish tried to fix:\n{issues}"},
                {"type": "text", "text": f"BEFORE (revision {base}): {self.baseline_capture.get('note', '')}"},
                *self.baseline_capture["parts"],
                {"type": "text", "text": f"AFTER (revision {rev}): {cap.get('note', '')}"},
                *cap["parts"],
            ])
            if obj is None:
                reason = "compare request failed"
            else:
                compare = {
                    "choice": str(obj.get("choice") or "").lower(),
                    "improved": [str(x)[:200] for x in (obj.get("improved") or [])][:5],
                    "regressions": [str(x)[:200] for x in (obj.get("regressions") or [])][:5],
                    "reason": str(obj.get("reason") or "")[:400],
                }
                decision = "after" if compare["choice"] == "after" else "before"
                reason = compare["reason"]
        chosen = rev if decision == "after" else base
        self._log(f"before/after compare: kept revision {chosen} ({decision}) — {reason[:160]}")
        self._record_round(polished=rev, decision=f"{decision}: {reason[:200]}", compare=compare)
        self.selected_rev = chosen
        self.phase = "repair"
        if decision == "before" and self.polish_rounds_left > 0 and self.visual_enabled:
            # Another round starts from the kept revision, not the rejected polish
            new_rev, lines = self.store.write(self.store.revisions[base])
            self.checks[new_rev] = self.checks[base]
            self.hooks.on_source(self.store.source, new_rev, lines, f"restored revision {base}")
            self.history.append(f"rev {new_rev}: restored revision {base} (polish rejected)")
            self.selected_rev = new_rev

    def _record_round(self, decision: str, polished: int | None = None, compare: dict | None = None) -> None:
        self.visual["rounds"].append({
            "baseline_revision": self.baseline_rev, "issues": self.issues,
            "polished_revision": polished, "compare": compare, "decision": decision,
        })

    def _vision_call(self, system: str, parts: list[dict]) -> dict | None:
        """A text-only (no tools) multimodal request expecting a JSON object back. Any failure
        disables visual review for the rest of the session rather than ending it."""
        self.vision_calls += 1
        try:
            resp = self.hooks.llm([{"role": "system", "content": system},
                                   {"role": "user", "content": parts}], None)
        except lm_client.LMClientError as e:
            self._disable_visual(f"vision request failed ({e}); the model may not accept image/video input")
            return None
        if resp.get("aborted"):
            return None
        try:
            return demo_tools.parse_json_object(resp.get("content", ""))
        except demo_tools.ActionParseError as e:
            self._disable_visual(f"vision reply unusable ({e})")
            return None

    def _disable_visual(self, why: str) -> None:
        self._log(f"visual review skipped: {why}")
        self.visual_enabled = False
        self.visual["skipped"] = why

    def _ask_model(self) -> tuple[list[dict] | None, str | None]:
        """One model turn. Returns (actions, None), (None, None) after a recoverable format
        problem (recorded as feedback for the next turn), or (None, outcome) to stop."""
        self.turns += 1
        self.finish_requested = False
        self.hooks.on_status(f"THINKING — TURN {self.turns}/{self.budgets.max_turns}")
        messages = self._build_messages()
        self.feedback = []
        try:
            resp = self.hooks.llm(messages, demo_tools.TOOLS if self.use_tools else None)
        except lm_client.LMHTTPError as e:
            if self.use_tools and self.interaction == "auto" and 400 <= e.status < 500:
                self._log(f"server rejected native tools ({e}); switching to JSON actions")
                self.use_tools = False
                self.turns -= 1   # the request never reached the model
                return None, None
            self._log(f"LLM request failed: {e}")
            return None, "llm_error"
        except lm_client.LMClientError as e:
            self._log(f"LLM request failed: {e}")
            return None, "llm_error"

        if resp.get("aborted") or self.hooks.should_abort():
            return None, "cancelled"
        if resp.get("finish_reason") == "length":
            self.format_failures += 1
            self.feedback.append("Your previous response was cut off at the output token limit, so "
                                 "nothing in it was applied. Use small edit_demo snippets instead of "
                                 "rewriting large sections.")
            self._log(f"turn {self.turns}: response truncated at token limit")
            return None, None
        try:
            if resp.get("tool_calls"):
                actions = demo_tools.actions_from_tool_calls(resp["tool_calls"])
            else:
                actions = demo_tools.actions_from_content(resp.get("content", ""))
        except demo_tools.ActionParseError as e:
            self.format_failures += 1
            hint = ("Call the provided tools." if self.use_tools
                    else 'Reply with only {"actions": [...]} as described.')
            self.feedback.append(f"Your previous response could not be used: {e}. Nothing was applied. {hint}")
            self._log(f"turn {self.turns}: unusable response ({e})")
            return None, None
        self.format_failures = 0
        self._log(f"turn {self.turns}: " + ", ".join(a["action"] for a in actions))
        return actions, None

    def _execute(self, actions: list[dict]) -> str | None:
        """Apply one response's actions in order. Returns an outcome to stop the session, or
        None to continue. A changed source is always checked before the next turn."""
        changed = False
        for i, act in enumerate(actions, 1):
            if self.hooks.should_abort():
                return "cancelled"
            if self.tool_calls >= self.budgets.max_tool_calls:
                self.feedback.append("Tool-call budget exhausted; remaining actions were skipped.")
                break
            self.tool_calls += 1
            name, args = act["action"], act["arguments"]

            if name in ("edit_demo", "write_demo"):
                summary = str(args.get("change_summary") or "").strip()[:200] or "(no summary)"
                try:
                    if name == "edit_demo":
                        rev, lines = self.store.edit(str(args.get("old_text") or ""),
                                                     str(args.get("new_text") or ""))
                    else:
                        html = validator.strip_fences(str(args.get("html") or ""))
                        rev, lines = self.store.write(html)
                except demo_tools.EditError as e:
                    self.feedback.append(f"{name} #{i} failed [{e.code}]: {e.message} "
                                         "The remaining actions in that response were skipped.")
                    self._log(f"{name} rejected: {e.code}")
                    break
                changed = True
                self.hooks.on_status(f"APPLYING FIX — REV {rev}")
                self.hooks.on_source(self.store.source, rev, lines, summary)
                self.history.append(f"rev {rev} (turn {self.turns}, {name}): {summary}")
                self.feedback.append(f"{name} #{i} applied → revision {rev} (lines {lines[0]}–{lines[1]}).")

            elif name == "run_demo":
                try:
                    secs = float(args.get("seconds") or self.budgets.run_seconds)
                except (TypeError, ValueError):
                    secs = self.budgets.run_seconds
                secs = max(1.0, min(secs, self.budgets.max_run_seconds))
                if not self._check(secs):
                    return self._interrupted()
                changed = False

            elif name == "finish_demo":
                if changed:
                    if not self._check(self.budgets.run_seconds):
                        return self._interrupted()
                    changed = False
                if self._passed():
                    # The loop decides what a passing revision still needs (visual review,
                    # before/after compare) — finishing just ends the model's edits.
                    self._log(f"finish requested at revision {self.store.current}")
                    self.finish_requested = True
                    break
                res = self.checks.get(self.store.current, {})
                self.feedback.append(
                    f"finish_demo rejected: revision {self.store.current} did not pass its latest "
                    f"check ({res.get('result')}: {self._first_line(res.get('error'))}). Fix it first.")

        if changed and not self._check(self.budgets.run_seconds):
            return self._interrupted()
        if not actions:
            return "no_actions"
        return None

    # ── Checks ────────────────────────────────────────────────────────────────

    def _check(self, seconds: float) -> bool:
        """Run the current revision. False if the run couldn't happen (cancelled or no display)."""
        rev = self.store.current
        self.hooks.on_status(f"RUNNING CHECK — REV {rev}")
        res = self.hooks.run(self.store.source, seconds)
        if res is None:
            return False
        self.checks[rev] = res
        self.last_checked = rev
        kind = res.get("result")
        note = f"check rev {rev}: {kind}" + (f" — {self._first_line(res.get('error'))}" if res.get("error") else "")
        self._log(note)
        if self.history and self.history[-1].startswith(f"rev {rev} "):
            self.history[-1] += f" → {kind}"
        if kind == "OK":
            self.failure_streak = []
        else:
            self.failure_streak.append(f"{kind}:{self._first_line(res.get('error'))}")
        return True

    def _passed(self, rev: int | None = None) -> bool:
        return self.checks.get(rev or self.store.current, {}).get("result") == "OK"

    def _attention_reasons(self, visual_hints: bool = False) -> list[str]:
        """Signs that a passing revision may still be broken in ways the pass/fail rules don't
        catch. Frozen output and console warnings earn a text-only model one look (never a
        failure by themselves). "Mostly dark" and "renders on few frames" are only passed
        to the visual critique (visual_hints=True): without seeing the frames a model can't
        tell a sparse scene or slow hardware from a bug, and in evaluation it edited a
        working dark scene blind on the "mostly dark" signal. ("Frozen" is grid-based too
        and fires on sparse scenes, but its one rated look in evaluation was a win, so it
        stays until more ratings say otherwise.)"""
        diag = self.checks.get(self.store.current, {}).get("diagnostics") or {}
        reasons = []
        beats = diag.get("heartbeats") or 0
        if beats >= 3 and (diag.get("frozen_seconds") or 0) >= beats - 1:
            reasons.append("the sampled pixels barely changed during the run (frozen or very slow motion)")
        if diag.get("console"):
            reasons.append("the browser console reported warnings or errors")
        if not visual_hints:
            return reasons
        lit, cells = diag.get("visible_cells"), diag.get("grid_cells")
        if lit is not None and cells and lit / cells < 0.1:
            reasons.append(f"only {lit} of {cells} sampled screen cells were lit (mostly dark or tiny)")
        rfps, hfps = diag.get("render_fps"), diag.get("heartbeat_fps")
        if rfps is not None and hfps and rfps < 0.5 * hfps:
            reasons.append(f"it rendered on only {rfps} of {hfps} frames per second")
        return reasons

    def _stuck(self) -> bool:
        tail = self.failure_streak[-self.STUCK_REPEATS:]
        if len(tail) == self.STUCK_REPEATS and len(set(tail)) == 1:
            self._log(f"same failure {self.STUCK_REPEATS} times in a row: {tail[0]}")
            return True
        return False

    def _interrupted(self) -> str:
        return "cancelled" if self.hooks.should_abort() else "infra"

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _build_messages(self) -> list[dict]:
        system = self.system_prompt
        if not self.use_tools:
            system += "\n" + demo_tools.JSON_ACTIONS_INSTRUCTIONS
        rev = self.store.current
        res = self.checks.get(rev)
        left = max(0, int(self.budgets.deadline_seconds - (time.time() - self.started)))
        parts = [
            f"BRIEF:\n{self.brief}",
            (f"STATUS: current revision {rev}; turn {self.turns} of {self.budgets.max_turns}; "
             f"{self.budgets.max_tool_calls - self.tool_calls} tool calls and about {left // 60} min left."),
        ]
        if self.history:
            parts.append("CHANGE LOG:\n" + "\n".join(f"- {h}" for h in self.history[-8:]))
        if self.phase == "polish":
            lines = "\n".join(f"{n}. {i['problem']} (seen: {i['evidence']}) → {i['change']}"
                              for n, i in enumerate(self.issues, 1))
            parts.append(
                f"VISUAL REVIEW of revision {self.baseline_rev} (frames captured while it ran):\n{lines}\n"
                "Polish these with targeted edits and keep everything else as it is. Skip an issue "
                "if fixing it would hurt the demo. To keep revision "
                f"{self.baseline_rev} unchanged, call finish_demo without edits.")
        if res is not None:
            head = f"LATEST CHECK of revision {rev}: {res.get('result')}"
            if res.get("error"):
                head += f" — {res.get('error')}"
            diag = validator.format_diagnostics(res.get("diagnostics") or {})
            parts.append(head + (f"\n{diag}" if diag else ""))
            if res.get("result") == "OK" and self.phase == "repair":
                reasons = self._attention_reasons()
                if reasons:
                    parts.append("The demo runs, but: " + "; ".join(reasons) + ". Fix this with targeted "
                                 "edits if it is a real problem; if it is intentional, call finish_demo.")
        if self.feedback:
            parts.append("RESULT OF YOUR PREVIOUS RESPONSE:\n" + "\n".join(f"- {f}" for f in self.feedback))
        parts.append(f"CURRENT SOURCE (revision {rev}; the 'NNNN| ' prefixes are line numbers, "
                     f"not part of the file):\n{self.store.numbered()}")
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _first_line(text) -> str:
        return str(text or "").strip().splitlines()[0][:200] if text else ""

    def _log(self, msg: str) -> None:
        self.trace.append(msg)
        log.info("iterative: %s", msg)

    def _result(self, outcome: str) -> SessionResult:
        passing = [r for r, res in self.checks.items() if res.get("result") == "OK"]
        best = None
        if outcome == "cancelled":
            pass
        elif self.phase == "polish":
            # The polish never reached an accepted before/after compare: keep the reviewed revision
            best = self.baseline_rev
            self._log(f"polish unfinished ({outcome}); keeping reviewed revision {best}")
            self._record_round(decision=f"before: polish unfinished ({outcome})")
        elif self.selected_rev is not None and outcome == "finished":
            best = self.selected_rev
        elif outcome in ("first_draft_ok", "finished") and self._passed():
            best = self.store.current
        elif passing:
            best = max(passing)   # latest runtime-valid revision
            self._log(f"falling back to revision {best} (latest that passed)")
        last_fail = None
        for rev in sorted(self.checks, reverse=True):
            res = self.checks[rev]
            if res.get("result") != "OK":
                last_fail = {"kind": res.get("result"), "msg": res.get("error", "")}
                break
        return SessionResult(
            html=self.store.revisions[best] if best else None,
            outcome=outcome,
            revision=best,
            revisions=self.store.current,
            turns=self.turns,
            tool_calls=self.tool_calls,
            last_error=last_fail,
            trace=self.trace,
            vision_calls=self.vision_calls,
            visual=self.visual if (self.visual["enabled"] or self.visual.get("skipped")) else None,
        )
