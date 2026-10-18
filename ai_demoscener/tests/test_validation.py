"""Tests for audition correlation, timeout handling and brief-preserving repairs.
Run from ai_demoscener/:  python3 -m unittest discover tests
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import orchestrator  # noqa: E402
import validator  # noqa: E402


class CorrelationTests(unittest.TestCase):
    def test_stale_reply_ignored(self):
        validator.start_validation("a1")
        validator.receive_result({"audition_id": "old", "result": "OK"})
        self.assertIsNone(validator.wait_for_result(0.05))

    def test_first_matching_reply_wins(self):
        validator.start_validation("a2")
        validator.receive_result({"audition_id": "a2", "result": "CRASHED"})
        validator.receive_result({"audition_id": "a2", "result": "OK"})   # second display tab
        self.assertEqual(validator.wait_for_result(0.05)["result"], "CRASHED")

    def test_reply_after_timeout_does_not_leak(self):
        validator.start_validation("a3")
        self.assertIsNone(validator.wait_for_result(0.05))
        validator.receive_result({"audition_id": "a3", "result": "OK"})
        validator.start_validation("a4")
        self.assertIsNone(validator.wait_for_result(0.05))

    def test_recording_and_capture_ids(self):
        validator.start_recording("r1")
        self.assertTrue(validator.is_pending_recording("r1"))
        self.assertFalse(validator.is_pending_recording("../etc"))
        validator.receive_recording("zz", {"status": "uploaded"})
        self.assertIsNone(validator.wait_for_recording(0.05))
        validator.start_capture("c1")
        validator.receive_capture({"capture_id": "c1", "frames": [1]})
        self.assertEqual(validator.wait_for_capture(0.05)["frames"], [1])

    def test_line_offset_maps_back_to_source(self):
        html = "<html><head><title>x</title></head>\n<body>\n<script>throw 1</script></body></html>"
        out = validator.inject_probe(html, {"three": "/x"})
        offset = out.count("\n") - html.count("\n")
        self.assertIn(f"LINE_OFFSET={offset};", out)


class ScriptedOrchestrator(orchestrator.Orchestrator):
    def __init__(self, results, displays):
        super().__init__(lambda m: None, display_count_fn=lambda: displays[0])
        self.results = list(results)
        self.repairs = []

    def _audition(self, path, secs):
        return self.results.pop(0)

    def _do_repair(self, cfg, html, error, model="", brief="", history=None, diagnostics=None):
        self.repairs.append({"error": error, "brief": brief, "history": list(history or [])})
        return html + "<!--fixed-->"


class TimeoutAndRepairTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.html"

    def tearDown(self):
        self.tmp.cleanup()

    def test_timeout_is_retried_not_repaired(self):
        o = ScriptedOrchestrator([{"result": "TIMEOUT"}, {"result": "OK"}], [1])
        self.assertEqual(o._validate_and_repair(self.cfg, self.path, "<h>", "m", 2, 1), ("<h>", None))
        self.assertEqual(o.repairs, [])

    def test_two_timeouts_give_up_without_kind(self):
        o = ScriptedOrchestrator([{"result": "TIMEOUT"}, {"result": "TIMEOUT"}], [1])
        html, err = o._validate_and_repair(self.cfg, self.path, "<h>", "m", 2, 1)
        self.assertIsNone(html)
        self.assertIsNone(err["kind"])
        self.assertEqual(o.repairs, [])

    def test_waits_for_display_then_resumes(self):
        displays = [0]
        o = ScriptedOrchestrator([{"result": "TIMEOUT"}, {"result": "OK"}], displays)
        threading.Timer(0.3, lambda: displays.__setitem__(0, 1)).start()
        start = time.time()
        html, _ = o._validate_and_repair(self.cfg, self.path, "<h>", "m", 2, 1)
        self.assertEqual(html, "<h>")
        self.assertGreater(time.time() - start, 0.25)

    def test_repairs_carry_brief_and_history(self):
        o = ScriptedOrchestrator([{"result": "CRASHED", "error": "boom"}, {"result": "BLANK", "error": "dark"},
                                  {"result": "OK"}], [1])
        html, _ = o._validate_and_repair(self.cfg, self.path, "<h>", "m", 2, 1, brief="Effect: Plasma")
        self.assertTrue(html)
        self.assertEqual([r["brief"] for r in o.repairs], ["Effect: Plasma"] * 2)
        self.assertEqual(o.repairs[1]["history"], ["Attempt 1: CRASHED — boom"])

    def test_skip_while_waiting_for_display(self):
        o = ScriptedOrchestrator([], [0])
        threading.Timer(0.3, o.skip).start()
        o._generate_cycle(self.cfg)   # returns instead of generating

    def test_creative_brief_uses_draft_title(self):
        o = ScriptedOrchestrator([], [1])
        brief = o._build_brief(self.cfg, "creative", {"effect": "creative"}, "<title>Neon Koi</title>")
        self.assertIn('"Neon Koi"', brief)


if __name__ == "__main__":
    unittest.main()
