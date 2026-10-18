"""Tests for metrics aggregation, evaluation runs/blind pairing, and the orchestrator's
evaluate cycle (with generation and strategies stubbed). Everything writes to temp dirs.
Run from ai_demoscener/:  python3 -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import evaluation  # noqa: E402
import metrics  # noqa: E402
import orchestrator  # noqa: E402


class MetricsTests(unittest.TestCase):
    def test_summarize(self):
        rows = [
            {"arm": "classic", "initial_result": "CRASHED", "final_ok": True, "seconds": 10, "responses": 1,
             "output_chars": 4000, "tokens_known": False, "outcome": "passed", "changed_from_draft": True},
            {"arm": "classic", "initial_result": "OK", "final_ok": True, "seconds": 2, "responses": 0,
             "output_chars": 0, "tokens_known": True, "completion_tokens": 0, "outcome": "passed",
             "changed_from_draft": False},
            {"arm": "iterative", "initial_result": "CRASHED", "final_ok": False, "seconds": 30, "responses": 6,
             "tool_calls": 6, "fallback": False, "budget_exhausted": True, "outcome": "budget",
             "visual_decisions": ["before: lost detail", "after: brighter"]},
        ]
        s = metrics.summarize(rows)
        self.assertEqual(s["classic"]["n"], 2)
        self.assertEqual(s["classic"]["initial_pass_pct"], 50.0)
        self.assertEqual(s["classic"]["final_pass_pct"], 100.0)
        self.assertEqual(s["classic"]["changed_from_draft_pct"], 50.0)
        self.assertEqual(s["classic"]["median_seconds"], 6.0)
        self.assertEqual(s["classic"]["tokens_known_n"], 1)
        self.assertEqual(s["iterative"]["budget_exhausted"], 1)
        self.assertEqual((s["iterative"]["polish_kept"], s["iterative"]["polish_rejected"]), (1, 1))

    def test_append_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.jsonl"
            metrics.append(p, {"a": 1})
            metrics.append(p, {"a": 2})
            p.open("a").write("not json\n")
            self.assertEqual([r["a"] for r in metrics.load(p)], [1, 2])


class _TempEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(evaluation, "EVAL_DIR", root / "eval"),
            mock.patch.object(evaluation, "_ACTIVE", root / "eval" / "active.json"),
            mock.patch.object(config, "CONFIG_PATH", root / "config.json"),
            mock.patch.object(evaluation.archive_mod, "pick_for_update", lambda cfg, n: Path("src_demo.html")),
        ]
        for p in self.patches:
            p.start()
        self.cfg = config.load()
        self.cfg["boot_mode"] = "generate"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class EvaluationTests(_TempEval):
    def test_create_run(self):
        plan = evaluation.create_run(self.cfg, "lm_studio", "My Smoke!", ["classic", "classic_matched", "iterative"],
                                     repeats=2)
        self.assertEqual(len(plan["trials"]), 12)
        self.assertEqual(plan["trials"][-1]["id"], "t12")
        self.assertEqual(plan["trials"][5]["source"], "src_demo.html")
        matched = plan["arms"][1]["overrides"]
        self.assertEqual(matched["validation"]["max_repair_attempts"], self.cfg["iterative"]["max_turns"])
        self.assertEqual(evaluation.active_run()["run_id"], plan["run_id"])
        self.assertTrue(plan["run_id"].endswith("-my-smoke"))
        with self.assertRaises(evaluation.EvalError):
            evaluation.create_run(self.cfg, "lm_studio", arms=["classic"])

    def test_run_id_validation(self):
        with self.assertRaises(evaluation.EvalError):
            evaluation.run_dir("../../etc")

    def test_pairs_ratings_and_summary(self):
        plan = evaluation.create_run(self.cfg, "lm_studio", arms=["classic", "iterative"],
                                     trials=[{"mode": "stock", "effect": "Plasma"},
                                             {"mode": "stock", "effect": "Fire"},
                                             {"mode": "stock", "effect": "Starfield"}])
        rid = plan["run_id"]
        d = evaluation.run_dir(rid)
        for t in ("t01", "t02", "t03"):
            (d / f"{t}_draft.html").write_text("draft")
        (d / "t01_classic.html").write_text("same")          # identical outputs → auto-tie
        (d / "t01_iterative.html").write_text("same")
        (d / "t02_classic.html").write_text("c")             # different → needs a rating
        (d / "t02_iterative.html").write_text("i")
        (d / "t03_iterative.html").write_text("only iterative")   # classic discarded
        for t, a, ok in [("t01", "classic", True), ("t01", "iterative", True), ("t02", "classic", False),
                         ("t02", "classic", True), ("t02", "iterative", True), ("t03", "classic", False),
                         ("t03", "iterative", True)]:
            evaluation.append_result(rid, {"trial": t, "arm": a, "final_ok": ok})
        self.assertEqual(len(evaluation.results(rid)), 6)   # t02/classic deduplicated (latest wins)

        ps = {p["key"]: p for p in evaluation.pairs(rid)}
        self.assertTrue(ps["t01:iterative"]["identical"])
        self.assertFalse(ps["t02:iterative"]["identical"])
        public = evaluation.public_pairs(rid)
        self.assertTrue(all("_sides" not in p and "arms" not in p for p in public))

        # Prefer the iterative side of t02 and t03, whichever letter it got
        for key in ("t02:iterative", "t03:iterative"):
            side = "A" if ps[key]["_sides"]["A"] == "iterative" else "B"
            evaluation.rate(rid, key, side, brief_a=True, brief_b=False)
        with self.assertRaises(evaluation.EvalError):
            evaluation.rate(rid, "t02:iterative", "maybe", None, None)
        s = evaluation.summary(rid)
        pref = s["preference"]["iterative"]
        self.assertEqual((pref["wins"], pref["losses"], pref["identical"], pref["unrated"]), (2, 0, 1, 0))
        self.assertEqual(s["arms"]["classic"]["final_pass_pct"], round(200 / 3, 1))
        self.assertEqual(s["plan"]["trials_done"], 3)


class StubOrchestrator(orchestrator.Orchestrator):
    """Draft generation and strategies are stubbed; everything else is the real eval cycle."""

    def __init__(self, outcomes):
        super().__init__(lambda m: None, display_count_fn=lambda: 1)
        self.outcomes = outcomes   # strategy -> html suffix or None (discard)
        self.drafts = 0

    def _generate_draft(self, cfg, mode, prompt_data, model):
        self.drafts += 1
        return f"<html>{prompt_data['effect']}</html>", {"draft_seconds": 1.0}

    def _run_strategy(self, cfg, temp_path, html, model, brief):
        strategy = cfg["generation_strategy"]
        suffix = self.outcomes[strategy]
        out = None if suffix is None else html + suffix
        return out, None, None, {"strategy": strategy, "final_ok": out is not None,
                                 "max_repairs": cfg["validation"]["max_repair_attempts"]}


class EvalCycleTests(_TempEval):
    def test_runs_every_arm_on_one_draft_then_restores_mode(self):
        config.save(self.cfg)
        plan = evaluation.create_run(self.cfg, "lm_studio", arms=["classic", "classic_matched", "iterative"],
                                     trials=[{"mode": "stock", "effect": "Plasma"},
                                             {"mode": "stock", "effect": "No Such Effect"}])
        self.cfg["boot_mode"] = "evaluate"
        o = StubOrchestrator({"classic": None, "iterative": "<!--it-->"})
        o._eval_cycle(self.cfg)   # trial 1
        self.assertEqual(o.drafts, 1)
        rid = plan["run_id"]
        rows = {r["arm"]: r for r in evaluation.results(rid)}
        self.assertEqual(set(rows), {"classic", "classic_matched", "iterative"})
        self.assertEqual(rows["classic_matched"]["max_repairs"], self.cfg["iterative"]["max_turns"])
        self.assertEqual(rows["iterative"]["file"], "t01_iterative.html")
        self.assertIsNone(rows["classic"]["file"])
        d = evaluation.run_dir(rid)
        self.assertEqual((d / "t01_iterative.html").read_text(), "<html>Plasma</html><!--it-->")
        self.assertTrue((d / "t01_draft.html").exists())

        o._eval_cycle(self.cfg)   # trial 2: unknown effect → skipped with a note
        p = evaluation.load_plan(rid)
        self.assertEqual((p["next"], p["status"]), (2, "done"))
        self.assertIn("skipped", p["notes"][0])

        o._eval_cycle(self.cfg)   # run finished → leave evaluate mode
        self.assertIsNone(evaluation.active_run())
        self.assertEqual(config.load()["boot_mode"], "generate")

    def test_stopped_run_restores_mode(self):
        self.cfg["boot_mode"] = "replay"
        config.save(self.cfg)
        plan = evaluation.create_run(self.cfg, "lm_studio", arms=["classic", "iterative"])
        plan["status"] = "stopped"
        evaluation.save_plan(plan)
        StubOrchestrator({})._eval_cycle(self.cfg)
        self.assertEqual(config.load()["boot_mode"], "replay")
        self.assertIsNone(evaluation.active_run())


if __name__ == "__main__":
    unittest.main()
