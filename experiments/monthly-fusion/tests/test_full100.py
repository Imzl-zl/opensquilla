import copy
import json
import tempfile
import unittest
from pathlib import Path

from _load import load

accounting = load("monthly_accounting_test", "full100/recompute.py")
renderer = load("monthly_renderer_test", "full100/render_report.py")
verifier = load("monthly_verifier_test", "full100/verify_results.py")


def fixture():
    def task(group, quality, cost, elapsed):
        return {
            "group": group,
            "task_id": "task-1",
            "generation_status": "completed",
            "generation_elapsed_ms": elapsed,
            "original_exact_cost": True,
            "calls": [
                {
                    "call_id": f"{group}-1",
                    "call_index": 1,
                    "model": "model",
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "payload_sha256": "payload",
                    "input_token_estimate": 100,
                    "tools_count": 0,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "reasoning_tokens": 5,
                        "cached_tokens": 40,
                        "cache_write_tokens": 0,
                        "billed_cost": cost,
                    },
                    "receipts": [],
                }
            ],
            "judge_attempts": [],
        }

    snapshot = {
        "generation_catalog": {
            "fetched_at": "2026-01-01T00:00:00Z",
            "models": [
                {
                    "id": "model",
                    "pricing": {
                        "prompt": "0.01",
                        "completion": "0.02",
                        "input_cache_read": "0.001",
                    },
                }
            ],
        },
        "judge_model": {"id": "judge", "pricing": {"prompt": "0.01", "completion": "0.02"}},
        "tasks": [task("baseline", 0.5, 1.0, 100), task("candidate", 0.6, 0.5, 50)],
    }
    comparison = {
        "tasks": [
            {
                "group": "baseline",
                "task_id": "task-1",
                "quality": 0.5,
                "criterion_pass_rate": 0.4,
                "judge_errors": 0,
                "tool_calls": 1,
                "llm_requests": 1,
                "judge_request_count": 0,
                "generation_elapsed_ms": 100,
            },
            {
                "group": "candidate",
                "task_id": "task-1",
                "quality": 0.6,
                "criterion_pass_rate": 0.5,
                "judge_errors": 0,
                "tool_calls": 1,
                "llm_requests": 1,
                "judge_request_count": 0,
                "generation_elapsed_ms": 50,
            },
        ]
    }
    return snapshot, comparison


class FullSetTests(unittest.TestCase):
    def test_cache_aware_cost_does_not_double_count_cached_input(self):
        rates = {
            "input": 0.01,
            "output": 0.02,
            "cache_read": 0.001,
            "cache_write": 0.03,
            "request": 0,
        }
        tokens = {
            "input_tokens": 100,
            "output_tokens": 20,
            "cached_tokens": 40,
            "cache_write_tokens": 0,
        }
        self.assertAlmostEqual(accounting.token_cost(tokens, rates), 1.04)

    def test_compute_verify_and_render(self):
        snapshot, comparison = fixture()
        report = accounting.compute(snapshot, comparison, ("baseline", "candidate"), 1, "baseline")
        self.assertEqual(report["groups"][0]["metrics"]["AvgQ"], 50.0)
        self.assertEqual(report["groups"][1]["metrics"]["AvgQ"], 60.0)
        delta = report["groups"][1]["comparison_vs_baseline_full100"]
        self.assertAlmostEqual(delta["quality_change_percent"], 20.0)
        self.assertAlmostEqual(delta["generation_cost_change_percent"], -50.0)
        self.assertAlmostEqual(delta["p50_change_percent"], -50.0)
        self.assertEqual(report["groups"][1]["metrics"]["Tool%"], 100.0)
        self.assertTrue(verifier.verify(report)["verified"])
        markdown = renderer.render(report, "Example")
        self.assertIn("AvgQ较baseline", markdown)
        self.assertIn("+20.00%", markdown)

    def test_verifier_rejects_tampered_cost(self):
        snapshot, comparison = fixture()
        report = accounting.compute(snapshot, comparison, ("baseline", "candidate"), 1, "baseline")
        report = copy.deepcopy(report)
        report["groups"][0]["metrics"]["Total Gen$"] += 1
        with self.assertRaises(ValueError):
            verifier.verify(report)

    def test_verifier_rejects_tampered_token_metric_and_delta(self):
        snapshot, comparison = fixture()
        report = accounting.compute(snapshot, comparison, ("baseline", "candidate"), 1, "baseline")
        bad_metric = copy.deepcopy(report)
        bad_metric["groups"][1]["metrics"]["Avg Cache"] += 1
        with self.assertRaises(ValueError):
            verifier.verify(bad_metric)
        bad_delta = copy.deepcopy(report)
        bad_delta["groups"][1]["comparison_vs_baseline_full100"]["quality_change_percent"] += 1
        with self.assertRaises(ValueError):
            verifier.verify(bad_delta)

    def test_historical_baseline_is_verified_and_rendered_as_first_row(self):
        snapshot, comparison = fixture()
        baseline_report = accounting.compute(
            snapshot, comparison, ("baseline", "candidate"), 1, "baseline"
        )
        candidate_snapshot = copy.deepcopy(snapshot)
        candidate_snapshot["tasks"] = [
            task for task in candidate_snapshot["tasks"] if task["group"] == "candidate"
        ]
        candidate_comparison = {
            "tasks": [task for task in comparison["tasks"] if task["group"] == "candidate"]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text(json.dumps(baseline_report))
            report = accounting.compute(
                candidate_snapshot,
                candidate_comparison,
                ("candidate",),
                1,
                "baseline",
                path,
            )
            self.assertTrue(verifier.verify(report)["verified"])
            markdown = renderer.render(report, "Example")
            self.assertLess(markdown.index("| baseline |"), markdown.index("| candidate |"))
            self.assertIn("| baseline | 50.000 | 0.00%", markdown)


if __name__ == "__main__":
    unittest.main()
