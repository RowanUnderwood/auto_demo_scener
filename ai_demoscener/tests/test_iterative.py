"""Tests for the iterative strategy's pure parts and session loop, using simulated model
responses and audition results. Run from ai_demoscener/:  python3 -m unittest discover tests
"""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_runner  # noqa: E402
import demo_tools  # noqa: E402
import lm_client  # noqa: E402

SRC = "<html>\n<body>\n<script>\n  const speed = 0.1;\n  const speed2 = 0.1;\n</script>\n</body>\n</html>"


# ── SourceStore ────────────────────────────────────────────────────────────────

class SourceStoreTests(unittest.TestCase):
    def test_exact_edit(self):
        s = demo_tools.SourceStore(SRC)
        rev, lines = s.edit("const speed = 0.1;", "const speed = 0.5;")
        self.assertEqual(rev, 2)
        self.assertEqual(lines, (4, 4))
        self.assertIn("const speed = 0.5;", s.source)
        self.assertIn("const speed2 = 0.1;", s.source)

    def test_ambiguous_and_missing(self):
        s = demo_tools.SourceStore(SRC)
        with self.assertRaises(demo_tools.EditError) as cm:
            s.edit("= 0.1;", "= 1;")
        self.assertEqual(cm.exception.code, "ambiguous_match")
        with self.assertRaises(demo_tools.EditError) as cm:
            s.edit("nope", "x")
        self.assertEqual(cm.exception.code, "no_match")
        with self.assertRaises(demo_tools.EditError) as cm:
            s.edit("   ", "x")
        self.assertEqual(cm.exception.code, "empty_old_text")
        self.assertEqual(s.current, 1)  # nothing applied

    def test_whitespace_tolerant_edit_must_still_be_unique(self):
        s = demo_tools.SourceStore(SRC)
        rev, _ = s.edit("<script>\n const   speed = 0.1;", "<script>\n  const speed = 2;")
        self.assertEqual(rev, 2)
        self.assertIn("const speed = 2;", s.source)

    def test_write(self):
        s = demo_tools.SourceStore(SRC)
        rev, lines = s.write("<html>\n</html>")
        self.assertEqual((rev, lines), (2, (1, 2)))
        with self.assertRaises(demo_tools.EditError):
            s.write("  ")


# ── Action parsing ─────────────────────────────────────────────────────────────

class ParsingTests(unittest.TestCase):
    def test_tool_calls(self):
        acts = demo_tools.actions_from_tool_calls(
            [{"name": "finish_demo", "arguments": '{"summary": "done"}'}])
        self.assertEqual(acts, [{"action": "finish_demo", "arguments": {"summary": "done"}}])

    def test_incomplete_tool_arguments_never_parse(self):
        with self.assertRaises(demo_tools.ActionParseError):
            demo_tools.actions_from_tool_calls([{"name": "edit_demo", "arguments": '{"old_text": "a'}])
        with self.assertRaises(demo_tools.ActionParseError):
            demo_tools.actions_from_tool_calls([{"name": "rm_rf", "arguments": "{}"}])

    def test_json_actions(self):
        text = ('<think>hmm</think>```json\n{"actions": [{"action": "run_demo", "arguments": {}},'
                ' {"action": "finish_demo", "arguments": {"summary": "x"}}]}\n```')
        acts = demo_tools.actions_from_content(text)
        self.assertEqual([a["action"] for a in acts], ["run_demo", "finish_demo"])
        single = demo_tools.actions_from_content('{"action": "finish_demo", "arguments": {"summary": "x"}}')
        self.assertEqual(single[0]["action"], "finish_demo")
        for bad in ("Sure! I fixed it.", '{"actions": []}', '{"actions": [{"action": "shell"}]}'):
            with self.assertRaises(demo_tools.ActionParseError):
                demo_tools.actions_from_content(bad)


# ── Streaming tool-call assembly (real HTTP, fake server) ──────────────────────

class _FragmentedToolServer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        args = json.dumps({"old_text": "const speed = 0.1;", "new_text": "const speed = 0.5;",
                           "change_summary": "faster"})
        chunks = [
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                    "function": {"name": "edit_demo", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[:17]}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "c2", "type": "function",
                                                    "function": {"name": "finish_demo", "arguments": '{"summary"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[17:]}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": ': "ok"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 9}},
        ]
        self.send_response(200)
        self.end_headers()
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class ChatCollectTests(unittest.TestCase):
    def test_fragments_assemble_by_index(self):
        srv = HTTPServer(("127.0.0.1", 0), _FragmentedToolServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            r = lm_client.chat_collect([{"role": "user", "content": "x"}],
                                       f"http://127.0.0.1:{srv.server_port}", "m", 100, 0.5,
                                       tools=demo_tools.TOOLS)
        finally:
            srv.shutdown()
        self.assertEqual(r["finish_reason"], "tool_calls")
        self.assertEqual(r["usage"], {"completion_tokens": 9})
        acts = demo_tools.actions_from_tool_calls(r["tool_calls"])
        self.assertEqual([a["action"] for a in acts], ["edit_demo", "finish_demo"])
        self.assertEqual(acts[0]["arguments"]["new_text"], "const speed = 0.5;")
        self.assertEqual(r["tool_calls"][1]["id"], "c2")


# ── Session loop ───────────────────────────────────────────────────────────────

def tool(name, **args):
    return {"name": name, "arguments": json.dumps(args)}


def reply(*calls, finish="tool_calls", content=""):
    return {"content": content, "tool_calls": list(calls), "finish_reason": finish,
            "usage": None, "aborted": False}


OK = {"result": "OK", "diagnostics": {"heartbeats": 5, "frozen_seconds": 0, "visible_cells": 40,
                                       "grid_cells": 64, "render_fps": 60, "heartbeat_fps": 60}}


def crash(msg="boom"):
    return {"result": "CRASHED", "error": msg, "diagnostics": {"errors": [{"category": "js", "error": msg}]}}


class Harness:
    """Scripted model replies and audition results; run results are chosen by source text."""

    def __init__(self, replies, judge, abort_after_runs=None):
        self.replies = list(replies)
        self.judge = judge
        self.requests = []
        self.runs = []
        self.sources = []
        self.abort_after_runs = abort_after_runs

    def llm(self, messages, tools):
        self.requests.append((messages, tools))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def run(self, html, seconds):
        self.runs.append((html, seconds))
        return self.judge(html)

    def hooks(self):
        return agent_runner.SessionHooks(
            llm=self.llm, run=self.run,
            on_source=lambda html, rev, lines, summary: self.sources.append(rev),
            on_status=lambda text: None,
            should_abort=lambda: self.abort_after_runs is not None and len(self.runs) >= self.abort_after_runs,
        )


def session(h, budgets=None, interaction="auto"):
    return agent_runner.IterativeSession("Effect: test", SRC, "SYSTEM", budgets or agent_runner.Budgets(),
                                         interaction, h.hooks())


def by_speed(passing):
    return lambda html: OK if passing in html else crash()


class SessionTests(unittest.TestCase):
    def test_clean_first_draft_needs_no_turns(self):
        h = Harness([], lambda html: OK)
        r = session(h).run()
        self.assertEqual((r.outcome, r.turns, r.revision), ("first_draft_ok", 0, 1))

    def test_edit_then_auto_check_then_finish(self):
        h = Harness([reply(tool("edit_demo", old_text="const speed = 0.1;", new_text="const speed = 0.5;",
                                change_summary="fix"))],
                    by_speed("speed = 0.5"))
        r = session(h).run()
        self.assertEqual(r.outcome, "finished")
        self.assertEqual(r.revision, 2)
        self.assertEqual(len(h.runs), 2)  # rev 1 failed, rev 2 checked automatically
        # the request carried the brief, the evidence and the numbered source
        user = h.requests[0][0][1]["content"]
        self.assertIn("Effect: test", user)
        self.assertIn("CRASHED — boom", user)
        self.assertIn("   4|   const speed = 0.1;", user)

    def test_finish_on_changed_revision_is_checked_first(self):
        h = Harness([reply(tool("edit_demo", old_text="speed2 = 0.1", new_text="speed2 = 9",
                                change_summary="x"),
                           tool("finish_demo", summary="done")),
                     reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5", change_summary="y"))],
                    by_speed("speed = 0.5"))
        r = session(h).run()
        # rev 2 still failed, so finish was rejected; rev 3 passed
        self.assertEqual((r.outcome, r.revision), ("finished", 3))
        self.assertIn("finish_demo rejected", h.requests[1][0][1]["content"])

    def test_failed_edit_skips_rest_of_response(self):
        h = Harness([reply(tool("edit_demo", old_text="missing", new_text="x", change_summary="a"),
                           tool("finish_demo", summary="b")),
                     reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5", change_summary="c"))],
                    by_speed("speed = 0.5"))
        r = session(h).run()
        self.assertEqual(r.outcome, "finished")
        self.assertIn("[no_match]", h.requests[1][0][1]["content"])

    def test_budget_exhaustion_falls_back_to_latest_passing(self):
        judge = lambda html: OK if ("speed = 0.5" in html and "BROKEN" not in html) else crash()  # noqa: E731
        h = Harness([
            # rev 1 passes with console warnings → one attention turn; the model breaks it, then can't fix it
            reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5 // BROKEN", change_summary="a")),
            reply(tool("edit_demo", old_text="speed2", new_text="speed3", change_summary="b")),
        ], judge)
        noisy = {"result": "OK", "diagnostics": {"heartbeats": 5, "console": [{"level": "warn", "text": "x"}]}}
        h.judge = lambda html: noisy if html == SRC else judge(html)
        r = session(h, agent_runner.Budgets(max_turns=2)).run()
        self.assertEqual(r.outcome, "budget")
        self.assertEqual(r.revision, 1)
        self.assertEqual(r.html, SRC)

    def test_budget_exhaustion_without_passing_revision_discards(self):
        n = iter(range(100))
        h = Harness([reply(tool("run_demo"))] * 3, lambda html: crash(f"error #{next(n)}"))
        r = session(h, agent_runner.Budgets(max_turns=2)).run()
        self.assertEqual(r.outcome, "budget")
        self.assertIsNone(r.html)
        self.assertEqual(r.last_error["kind"], "CRASHED")

    def test_run_seconds_are_capped(self):
        h = Harness([reply(tool("run_demo", seconds=999)), reply(tool("finish_demo", summary="x"))],
                    lambda html: crash())
        session(h, agent_runner.Budgets(max_turns=2, max_run_seconds=15)).run()
        self.assertEqual(h.runs[1][1], 15)

    def test_malformed_responses_stop_after_one_correction(self):
        h = Harness([reply(content="I fixed it!"), reply(content="Really, it works now.")], lambda html: crash())
        r = session(h).run()
        self.assertEqual(r.outcome, "format_error")
        self.assertEqual(len(h.requests), 2)
        self.assertIn("could not be used", h.requests[1][0][1]["content"])

    def test_truncated_response_is_not_applied(self):
        trunc = reply(tool("write_demo", html="<html>", change_summary="x"), finish="length")
        h = Harness([trunc, trunc], lambda html: crash())
        r = session(h).run()
        self.assertEqual(r.outcome, "format_error")
        self.assertEqual(h.sources, [])

    def test_auto_mode_switches_to_json_actions_when_tools_rejected(self):
        h = Harness([lm_client.LMHTTPError(400, "tools not supported"),
                     reply(content='{"actions": [{"action": "edit_demo", "arguments": {"old_text": '
                                   '"speed = 0.1", "new_text": "speed = 0.5", "change_summary": "x"}}]}',
                           finish="stop")],
                    by_speed("speed = 0.5"))
        s = session(h)
        r = s.run()
        self.assertEqual(r.outcome, "finished")
        self.assertFalse(s.use_tools)
        self.assertIsNone(h.requests[1][1])
        self.assertIn('{"actions"', h.requests[1][0][0]["content"])

    def test_identical_failures_stop_as_stuck(self):
        h = Harness([reply(tool("run_demo"))] * 5, lambda html: crash("same"))
        r = session(h).run()
        self.assertEqual(r.outcome, "stuck")

    def test_cancel_prevents_result(self):
        h = Harness([reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5", change_summary="x"))],
                    by_speed("speed = 0.5"), abort_after_runs=2)
        r = session(h).run()
        self.assertEqual(r.outcome, "cancelled")
        self.assertIsNone(r.html)

    def test_no_display_is_infra(self):
        h = Harness([], lambda html: None)
        r = session(h).run()
        self.assertEqual((r.outcome, r.html), ("infra", None))

    def test_llm_error(self):
        h = Harness([lm_client.LMClientError("down")], lambda html: crash())
        self.assertEqual(session(h).run().outcome, "llm_error")



class AttentionTests(unittest.TestCase):
    SPARSE = {"result": "OK", "diagnostics": {"heartbeats": 5, "frozen_seconds": 0, "visible_cells": 1,
                                               "grid_cells": 64, "render_fps": 2, "heartbeat_fps": 6}}

    def test_dark_or_slow_signals_do_not_trigger_blind_edits(self):
        h = Harness([], lambda html: self.SPARSE)
        r = session(h).run()
        self.assertEqual((r.outcome, r.turns), ("first_draft_ok", 0))

    def test_frozen_output_still_gets_one_look(self):
        frozen = {"result": "OK", "diagnostics": {"heartbeats": 5, "frozen_seconds": 4}}
        h = Harness([reply(tool("finish_demo", summary="slow on purpose"))], lambda html: frozen)
        self.assertEqual(session(h).run().turns, 1)

    def test_console_warnings_still_get_one_look(self):
        noisy = {"result": "OK", "diagnostics": {"heartbeats": 5, "console": [{"level": "warn", "text": "x"}]}}
        h = Harness([reply(tool("finish_demo", summary="intended"))], lambda html: noisy)
        r = session(h).run()
        self.assertEqual((r.outcome, r.turns), ("finished", 1))

    def test_visual_critique_gets_the_dark_and_slow_hints(self):
        h = VisualHarness([text({"issues": []})], lambda html: self.SPARSE)
        vsession(h).run()
        intro = h.requests[0][0][1]["content"][0]["text"]
        self.assertIn("only 1 of 64 sampled screen cells were lit", intro)
        self.assertIn("rendered on only 2 of 6 frames", intro)

# ── Visual review ──────────────────────────────────────────────────────────────

PROMPTS = {"critique": "CRITIQUE", "compare": "COMPARE"}
ISSUE = json.dumps({"issues": [{"problem": "too dark", "evidence": "frame 2 is black",
                                "change": "raise brightness"}]})


def text(obj):
    return reply(content=obj if isinstance(obj, str) else json.dumps(obj), finish="stop")


class VisualHarness(Harness):
    def __init__(self, replies, judge, capture_ok=True):
        super().__init__(replies, judge)
        self.captured = []   # source that was running in the display at each capture
        self.capture_ok = capture_ok

    def capture(self):
        if not self.capture_ok:
            return None
        self.captured.append(self.runs[-1][0])
        return {"parts": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}],
                "note": "1 frame"}

    def hooks(self):
        h = super().hooks()
        h.capture = self.capture
        return h


def vsession(h, budgets=None):
    return agent_runner.IterativeSession("Effect: test", SRC, "SYSTEM", budgets or agent_runner.Budgets(),
                                         "auto", h.hooks(), visual_prompts=PROMPTS)


POLISH = reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5", change_summary="brighter"))


class VisualReviewTests(unittest.TestCase):
    def test_polish_accepted(self):
        h = VisualHarness([text(ISSUE), POLISH, text({"choice": "after", "reason": "brighter"})],
                          lambda html: OK)
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision), ("finished", 2))
        self.assertEqual(h.captured, [SRC, h.runs[-1][0]])           # before = rev 1, after = rev 2
        self.assertEqual(r.vision_calls, 2)
        self.assertEqual(r.turns, 1)                                   # vision calls aren't edit turns
        self.assertTrue(r.visual["rounds"][0]["decision"].startswith("after"))
        # critique was multimodal and tool-free; the polish turn carried the issues as text
        crit_msgs, crit_tools = h.requests[0]
        self.assertIsNone(crit_tools)
        self.assertEqual(crit_msgs[1]["content"][1]["type"], "image_url")
        self.assertIn("VISUAL REVIEW of revision 1", h.requests[1][0][1]["content"])
        self.assertIn("too dark", h.requests[1][0][1]["content"])
        # compare saw BEFORE then AFTER images
        cmp_parts = h.requests[2][0][1]["content"]
        self.assertIn("BEFORE (revision 1)", cmp_parts[1]["text"])
        self.assertIn("AFTER (revision 2)", cmp_parts[3]["text"])

    def test_polish_rejected_keeps_baseline(self):
        h = VisualHarness([text(ISSUE), POLISH, text({"choice": "before", "reason": "lost detail"})],
                          lambda html: OK)
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision, r.html), ("finished", 1, SRC))

    def test_unclear_choice_keeps_baseline(self):
        h = VisualHarness([text(ISSUE), POLISH, text({"choice": "both look fine"})], lambda html: OK)
        self.assertEqual(vsession(h).run().revision, 1)

    def test_no_issues_finishes_without_edit_turns(self):
        h = VisualHarness([text({"issues": []})], lambda html: OK)
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision, r.turns, r.vision_calls), ("finished", 1, 0, 1))

    def test_vision_rejected_disables_review(self):
        h = VisualHarness([lm_client.LMHTTPError(400, "image input not supported")], lambda html: OK)
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision), ("finished", 1))
        self.assertIn("vision request failed", r.visual["skipped"])

    def test_capture_failure_skips_review(self):
        h = VisualHarness([], lambda html: OK, capture_ok=False)
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision, r.vision_calls), ("finished", 1, 0))

    def test_broken_polish_out_of_budget_keeps_baseline(self):
        h = VisualHarness([text(ISSUE), POLISH, reply(tool("run_demo"))],
                          lambda html: OK if html == SRC else crash("polish broke it"))
        r = vsession(h, agent_runner.Budgets(max_turns=2)).run()
        self.assertEqual((r.outcome, r.revision, r.html), ("budget", 1, SRC))
        self.assertIn("polish unfinished", r.visual["rounds"][-1]["decision"])

    def test_model_keeps_baseline_with_finish(self):
        h = VisualHarness([text(ISSUE), reply(tool("finish_demo", summary="it's intentional"))],
                          lambda html: OK)
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision, r.vision_calls), ("finished", 1, 1))

    def test_repair_then_review(self):
        fix = reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5", change_summary="fix"))
        polish = reply(tool("edit_demo", old_text="speed2 = 0.1", new_text="speed2 = 0.3", change_summary="p"))
        h = VisualHarness([fix, text(ISSUE), polish, text({"choice": "after", "reason": "ok"})],
                          by_speed("speed = 0.5"))
        r = vsession(h).run()
        self.assertEqual((r.outcome, r.revision), ("finished", 3))
        self.assertEqual(r.visual["rounds"][0]["baseline_revision"], 2)

    def test_no_review_without_turns_left(self):
        fix = reply(tool("edit_demo", old_text="speed = 0.1", new_text="speed = 0.5", change_summary="fix"))
        h = VisualHarness([fix], by_speed("speed = 0.5"))
        r = vsession(h, agent_runner.Budgets(max_turns=1)).run()
        self.assertEqual((r.outcome, r.revision, r.vision_calls), ("finished", 2, 0))

    def test_second_round_starts_from_kept_revision(self):
        h = VisualHarness([text(ISSUE), POLISH, text({"choice": "before", "reason": "worse"}),
                           text({"issues": []})], lambda html: OK)
        r = vsession(h, agent_runner.Budgets(max_polish_rounds=2)).run()
        self.assertEqual(r.html, SRC)
        self.assertEqual(h.captured[-1], SRC)   # second critique looked at the restored source

if __name__ == "__main__":
    unittest.main()
