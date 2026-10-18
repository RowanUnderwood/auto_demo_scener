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


@dataclass
class SessionHooks:
    llm: callable          # (messages, tools|None) -> lm_client.chat_collect() result dict
    run: callable          # (html, seconds) -> audit_result dict, or None if no display could run it
    on_source: callable    # (html, revision, (first_line, last_line), summary) -> None
    on_status: callable    # (text) -> None
    should_abort: callable # () -> bool


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


class IterativeSession:
    MAX_FORMAT_FAILURES = 2   # one corrective response is allowed, then the session stops
    STUCK_REPEATS = 3         # identical failures in a row with nothing new learned

    def __init__(self, brief: str, html: str, system_prompt: str, budgets: Budgets,
                 interaction: str, hooks: SessionHooks):
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
        self.started = time.time()

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
        if self._passed() and not self._attention_reasons():
            self._log("revision 1 passed with clean diagnostics; no iteration needed")
            return "first_draft_ok"

        while True:
            if self.hooks.should_abort():
                return "cancelled"
            if self.turns >= self.budgets.max_turns:
                return "budget"
            if self.tool_calls >= self.budgets.max_tool_calls:
                return "budget"
            if time.time() - self.started > self.budgets.deadline_seconds:
                self._log("session deadline reached")
                return "budget"
            if self._passed():
                if self.attention_used or not self._attention_reasons():
                    return "finished"
                self.attention_used = True

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

    def _ask_model(self) -> tuple[list[dict] | None, str | None]:
        """One model turn. Returns (actions, None), (None, None) after a recoverable format
        problem (recorded as feedback for the next turn), or (None, outcome) to stop."""
        self.turns += 1
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
                    self._log(f"finish accepted at revision {self.store.current}")
                    return "finished"
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

    def _attention_reasons(self) -> list[str]:
        """Signs that a passing revision may still be broken in ways the pass/fail rules don't
        catch. They earn the model one look (never a failure by themselves)."""
        diag = self.checks.get(self.store.current, {}).get("diagnostics") or {}
        reasons = []
        beats = diag.get("heartbeats") or 0
        if beats >= 3 and (diag.get("frozen_seconds") or 0) >= beats - 1:
            reasons.append("the sampled pixels barely changed during the run (frozen or very slow motion)")
        lit, cells = diag.get("visible_cells"), diag.get("grid_cells")
        if lit is not None and cells and lit / cells < 0.1:
            reasons.append(f"only {lit} of {cells} sampled screen cells were lit (mostly dark or tiny)")
        if diag.get("console"):
            reasons.append("the browser console reported warnings or errors")
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
        if res is not None:
            head = f"LATEST CHECK of revision {rev}: {res.get('result')}"
            if res.get("error"):
                head += f" — {res.get('error')}"
            diag = validator.format_diagnostics(res.get("diagnostics") or {})
            parts.append(head + (f"\n{diag}" if diag else ""))
            if res.get("result") == "OK":
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
        if outcome in ("first_draft_ok", "finished") and self._passed():
            best = self.store.current
        elif passing and outcome != "cancelled":
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
        )
