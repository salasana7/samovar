"""Offline regressions for validated agent output and honest run outcomes."""
import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import samovar
from lib.state import State
from lib.validation import (OutputValidationError, validate_classifications,
                            validate_reviews, validate_investigation, validate_plan)

CONFIG = {"taxonomy": {"categories": [{"id": "risk"}, {"id": "none"}],
                        "severity": ["low", "medium", "high"],
                        "confidence": ["low", "medium", "high"]}}


def classification(post_id, **updates):
    row = {"post_id": post_id, "label": "risk", "severity": "high",
           "confidence": "high", "evidence_en": "Evidence", "unknown_terms": []}
    row.update(updates)
    return row


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "lexicon").mkdir()
        self.state = State(self.root)
        self.run_id = self.state.start_run()
        self.state.add_posts([{"post_id": str(i), "source": "test", "text": "Post"} for i in range(10)])
        self.posts = self.state.get_unclassified_posts()
        self.ids = [p["post_id"] for p in self.posts]

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def output(self):
        return {"classifications": [classification(pid) for pid in self.ids]}

    def test_setup_is_deterministic_and_never_launches_an_agent(self):
        import shutil
        import yaml
        from samovar_resources import TEMPLATES_DIR
        setup_root = self.root / "setup"
        shutil.copytree(TEMPLATES_DIR, setup_root)
        with patch("builtins.input", side_effect=["Research", "/path/to/existing.py", "Analyst", "Term means X"]), \
             patch("samovar.subprocess.run") as launch:
            samovar._run_setup_agent("unused skill", str(setup_root))
        launch.assert_not_called()
        config = yaml.safe_load((setup_root / "samovar.yaml").read_text())
        self.assertEqual(config["project"]["description"], "Research")
        self.assertEqual(config["project"]["analyst"], "Analyst")
        self.assertEqual(config["setup"]["source_path_hint"], "/path/to/existing.py")
        self.assertIn("Term means X", (setup_root / "lexicon/corrections.md").read_text())

    def test_agent_override_works_before_or_after_subcommand(self):
        parser = samovar.build_parser()
        self.assertEqual(parser.parse_args(["--agent", "codex", "classify"]).agent, "codex")
        self.assertEqual(parser.parse_args(["classify", "--agent", "claude"]).agent, "claude")

    def test_investigation_receives_only_collected_context_and_true_label(self):
        self.state.add_classifications([classification(self.ids[0])], self.run_id)
        result = {"post_id": self.ids[0], "original_label": "risk", "recommendation": "confirm",
                  "confidence": "high", "thread_context_summary": "Context confirmed"}
        with patch("samovar.spawn_agent", return_value=result) as spawn:
            samovar._run_investigate({"post_id": self.ids[0], "label": "forged", "thread_url": "https://forged.example"},
                                    CONFIG, {}, self.root, self.state, self.run_id)
        context = spawn.call_args.kwargs["context"]
        self.assertEqual(context["post"]["current_label"], "risk")
        self.assertIsNone(context["thread_url"])
        self.assertEqual(context["thread_posts"][0]["post_id"], self.ids[0])
        self.assertIn("disabled", context["network_access"])

    def test_classification_rejects_missing_duplicate_foreign_and_invalid_fields(self):
        variants = []
        r = self.output(); r["classifications"].pop(); variants.append(r)
        r = self.output(); r["classifications"][1]["post_id"] = self.ids[0]; variants.append(r)
        r = self.output(); r["classifications"][0]["post_id"] = "forged"; variants.append(r)
        for key, value in [("label", "made_up"), ("severity", "critical"), ("confidence", 1),
                           ("evidence_en", ""), ("unknown_terms", [42])]:
            r = self.output(); r["classifications"][0][key] = value; variants.append(r)
        for result in variants:
            with self.subTest(result=result), patch("samovar.spawn_agent", return_value=result) as spawn:
                with self.assertRaises(OutputValidationError):
                    samovar._run_classify(CONFIG, {}, self.root, self.state, self.run_id)
                self.assertEqual(spawn.call_count, 1)
                self.assertEqual(self.state.summary()["classified"], 0)

    def test_empty_classifications_fail_instead_of_reporting_success(self):
        with patch("samovar.spawn_agent", return_value={"classifications": []}):
            with self.assertRaises(OutputValidationError):
                samovar._run_classify(CONFIG, {}, self.root, self.state, self.run_id)

    def test_flagged_terms_are_persisted_and_force_low_confidence(self):
        result = self.output()
        result["flagged"] = [{"post_id": self.ids[0], "term": "unknown"}]
        with patch("samovar.spawn_agent", return_value=result) as spawn:
            samovar._run_classify(CONFIG, {}, self.root, self.state, self.run_id)
        self.assertEqual(spawn.call_count, 1)
        flags = self.state.get_flagged_classifications()
        self.assertEqual(flags[0]["confidence"], "low")
        self.assertEqual(json.loads(flags[0]["unknown_terms_json"]), ["unknown"])

    def test_review_batch_rejects_invalid_status_and_missing_items(self):
        self.state.add_classifications(self.output()["classifications"], self.run_id)
        findings = self.state.get_unreviewed_findings()
        reviews = [{"post_id": p["post_id"], "status": "confirmed", "original_label": "risk",
                    "original_severity": "high", "reason": "Evidence supports finding"} for p in findings]
        for bad in [{"reviews": reviews[:-1]}, {"reviews": [{**r, "status": "invented"} for r in reviews]}]:
            with patch("samovar.spawn_agent", return_value=bad):
                with self.assertRaises(OutputValidationError):
                    samovar._run_review(CONFIG, {}, self.root, self.state, self.run_id)
        self.assertEqual(self.state.summary()["reviewed"], 0)
        good = validate_reviews({"reviews": reviews}, findings, CONFIG)
        self.assertEqual(good[0]["classification_id"], findings[0]["id"])

    def test_review_failure_is_not_swallowed(self):
        self.state.add_classifications(self.output()["classifications"], self.run_id)
        with patch("samovar.spawn_agent", side_effect=RuntimeError("backend unavailable")):
            with self.assertRaisesRegex(RuntimeError, "backend unavailable"):
                samovar._run_review(CONFIG, {}, self.root, self.state, self.run_id)

    def test_plan_requires_explicit_valid_actions(self):
        for plan in [{}, {"steps": "run"}, {"steps": [{"action": "shell"}]},
                     {"steps": [{"action": "collect", "params": {"source": "unknown"}}]}]:
            with self.assertRaises(OutputValidationError):
                validate_plan(plan, CONFIG)
        self.assertEqual(validate_plan({"steps": []}, CONFIG), [])

    def test_investigation_wrong_identity_cannot_mutate(self):
        result = {"post_id": self.ids[1], "recommendation": "reclassify",
                  "revised_label": "none", "confidence": "high", "thread_context_summary": "Context"}
        with patch("samovar.spawn_agent", return_value=result):
            with self.assertRaises(OutputValidationError):
                samovar._run_investigate({"post_id": self.ids[0]}, CONFIG, {}, self.root, self.state, self.run_id)
        self.assertEqual(self.state.summary()["investigated"], 0)

    def test_investigation_human_request_is_durable(self):
        result = {"post_id": self.ids[0], "recommendation": "needs_human",
                  "confidence": "low", "thread_context_summary": "Unable to obtain context"}
        with patch("samovar.spawn_agent", return_value=result):
            samovar._run_investigate({"post_id": self.ids[0]}, CONFIG, {}, self.root, self.state, self.run_id)
        self.assertEqual(self.state.summary()["pending_escalations"], 1)

    def test_checkpoint_shows_all_reasons_before_resolving_escalations(self):
        self.state.add_escalation(self.ids[0], "Urgent original concern")
        self.state.add_escalation(self.ids[0], "New lexicon proposal")
        flagged = self.state.get_flagged_classifications()
        self.assertEqual(len(flagged), 1)
        self.assertIn("Urgent original concern", flagged[0]["escalation_reason"])
        self.assertIn("New lexicon proposal", flagged[0]["escalation_reason"])

    def test_discovered_terms_are_proposals_not_trusted_lexicon(self):
        result = {"post_id": self.ids[0], "recommendation": "confirm", "confidence": "high",
                  "thread_context_summary": "Observed", "new_lexicon_entries": [
                      {"term": "X", "meaning": "Hypothesis", "confidence": "low"}]}
        with patch("samovar.spawn_agent", return_value=result):
            samovar._run_investigate({"post_id": self.ids[0]}, CONFIG, {}, self.root, self.state, self.run_id)
        self.assertFalse((self.root / "lexicon/slang.md").exists())
        self.assertIn("Hypothesis", self.state.get_pending_escalations()[0]["reason"])
        row = self.state.conn.execute("SELECT new_lexicon_json FROM investigations").fetchone()
        self.assertEqual(json.loads(row[0])[0]["term"], "X")

    def test_collector_failure_and_bad_json_do_not_ingest_partial_batch(self):
        script = self.root / "collector.py"; script.write_text("# Never executed")
        config = {"sources": {"test": {"script": "collector.py"}}}
        outcomes = [subprocess.CompletedProcess([], 1, "", "failure"),
                    subprocess.CompletedProcess([], 0, '{"post_id":"new","text":"Post"}\nnot json', ""),
                    subprocess.CompletedProcess([], 0, '{"post_id":"new","text":"Post","source":"other"}', "")]
        for result in outcomes:
            with patch("samovar.subprocess.run", return_value=result):
                with self.assertRaises((ValueError, RuntimeError)):
                    samovar._run_collect({"source": "test"}, config, self.root, self.state)
            self.assertEqual(self.state.summary()["total_posts"], 10)

    def test_collector_rejects_nonfinite_and_duplicate_json_atomically(self):
        script = self.root / "collector.py"
        script.write_text("# Never executed")
        config = {"sources": {"test": {"script": "collector.py"}}}
        valid = '{"post_id":"new-valid","text":"Post"}'
        invalid = ['{"post_id":"bad","text":"Post","metadata":{"score":' + value + '}}'
                   for value in ("NaN", "Infinity", "-Infinity", "1e999")]
        invalid.append('{"post_id":"bad","text":"Post","metadata":{"score":1,"score":2}}')
        for bad in invalid:
            result = subprocess.CompletedProcess([], 0, valid + "\n" + bad, "")
            with self.subTest(bad=bad), patch("samovar.subprocess.run", return_value=result):
                with self.assertRaisesRegex(ValueError, "Invalid collector JSON on line 2"):
                    samovar._run_collect({"source": "test"}, config, self.root, self.state)
            self.assertEqual(self.state.summary()["total_posts"], 10)

    def test_checkpoint_quit_eof_or_interrupt_halts_entire_pipeline(self):
        from lib.checkpoint import CheckpointInterrupted
        self.state.add_classifications([
            classification(self.ids[0], confidence="low", unknown_terms=["X"]),
            classification(self.ids[1], confidence="low"),
        ], self.run_id)
        plan = {"steps": [{"action": action} for action in ("checkpoint", "review", "report")]}
        for outcome in ("q", EOFError(), KeyboardInterrupt()):
            with self.subTest(outcome=outcome), \
                 patch("samovar._project_context", return_value=(self.root, CONFIG, {})), \
                 patch("samovar.spawn_agent", return_value=plan) as spawn, \
                 patch("lib.checkpoint._prompt", side_effect=[outcome]) as prompt:
                with self.assertRaises(CheckpointInterrupted):
                    samovar.cmd_run(SimpleNamespace())
                self.assertEqual(spawn.call_count, 1, "No model review/report may run after interrupted checkpoint")
                self.assertEqual(prompt.call_count, 1, "Quit must halt every review queue")
            self.assertEqual(self.state.summary()["last_run"]["status"], "interrupted")
            self.assertEqual(len(self.state.get_flagged_classifications()), 2)

    def test_checkpoint_interruption_exits_cli_nonzero(self):
        self.state.add_classifications([classification(self.ids[0], confidence="low")], self.run_id)
        with patch("sys.argv", ["samovar", "run"]), \
             patch("samovar._project_context", return_value=(self.root, CONFIG, {})), \
             patch("samovar.spawn_agent", return_value={"steps": [{"action": "checkpoint"}]}), \
             patch("lib.checkpoint._prompt", return_value="q"), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                samovar.main()
        self.assertEqual(raised.exception.code, 130)

    def test_failed_and_interrupted_runs_are_terminal(self):
        for exc, expected in [(RuntimeError("failure"), "failed"), (KeyboardInterrupt(), "interrupted")]:
            with self.assertRaises(type(exc)):
                with samovar._tracked_run(self.root) as (_, run_id):
                    raise exc
            row = self.state.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            self.assertEqual(row["status"], expected)
            self.assertTrue(row["finished_at"])
        with samovar._tracked_run(self.root) as (_, run_id):
            pass
        self.assertEqual(self.state.conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[0], "completed")

    def test_full_pipeline_uses_validated_versions_and_completes(self):
        calls = []
        def respond(*, skill, context, **kwargs):
            calls.append((skill, kwargs["backend"]))
            if skill == "coordinator":
                return {"steps": [{"action": action} for action in ("classify", "review", "report")]}
            if skill == "classify":
                return {"classifications": [classification(post["post_id"]) for post in context["posts"]]}
            if skill == "review":
                return {"reviews": [{"post_id": item["post_id"], "status": "confirmed",
                         "original_label": item["label"], "original_severity": item["severity"],
                         "reason": "Checked against source evidence"} for item in context["findings"]]}
            return {"report_content": "# Validated report"}
        config = {**CONFIG, "agents": {"default": "claude", "classify": "codex"}}
        with patch("samovar._project_context", return_value=(self.root, config, {})), \
             patch("samovar.spawn_agent", side_effect=respond):
            samovar.cmd_run(SimpleNamespace())
        summary = self.state.summary()
        self.assertEqual(summary["last_run"]["status"], "completed")
        self.assertEqual(summary["reviewed"], 10)
        self.assertEqual(summary["unreviewed_medium_high"], 0)
        self.assertEqual(calls, [("coordinator", "claude"), ("classify", "codex"), ("review", "claude"), ("report", "claude")])
        self.assertEqual(len(list((self.root / "reports").glob("*.md"))), 1)

    def test_coordinator_failure_marks_run_failed(self):
        with patch("samovar._project_context", return_value=(self.root, CONFIG, {})), \
             patch("samovar.spawn_agent", return_value={"steps": [{"action": "invalid"}]}):
            with self.assertRaises(OutputValidationError):
                samovar.cmd_run(SimpleNamespace())
        self.assertEqual(self.state.summary()["last_run"]["status"], "failed")

    def test_report_rejects_empty_and_preserves_urgent_escalations(self):
        self.state.add_escalation(self.ids[0], "Time sensitive")
        with patch("samovar.spawn_agent", return_value={"report_content": ""}):
            with self.assertRaises(ValueError):
                samovar._run_report(CONFIG, {}, self.root, self.state)
        self.assertFalse(list((self.root / "reports").glob("*.md")))
        with patch("samovar.spawn_agent", return_value={"report_content": "# Report\nBody"}):
            samovar._run_report(CONFIG, {}, self.root, self.state)
        content = next((self.root / "reports").glob("*.md")).read_text()
        self.assertIn("# Unresolved analyst escalations", content)
        self.assertIn("Time sensitive", content)


if __name__ == "__main__":
    unittest.main()
