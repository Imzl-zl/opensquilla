import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from _load import load

judge = load("monthly_judge_test", "judge_and_aggregate.py")


class FakeClock:
    def __init__(self):
        self.value = 10.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class JudgeTests(unittest.TestCase):
    def test_judge_model_record_binds_model_pricing_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "judge.json"
            path.write_text(
                json.dumps(
                    {
                        "id": "provider/model",
                        "pricing": {"prompt": "0.1", "completion": "0.2"},
                    }
                )
            )
            evidence = judge.judge_model_record(path, "provider/model")
            self.assertEqual(evidence["selected_model"]["id"], "provider/model")
            self.assertEqual(len(evidence["sha256"]), 64)
            with self.assertRaises(ValueError):
                judge.judge_model_record(path, "provider/other")

    def test_shared_gate_spaces_requests_and_honors_cooldown(self):
        clock = FakeClock()
        gate = judge.SharedRequestGate(2, clock=clock, sleep=clock.sleep)
        self.assertEqual(gate.acquire(), 10.0)
        self.assertEqual(gate.acquire(), 10.5)
        gate.defer(3)
        self.assertEqual(gate.acquire(), 13.5)

    def test_retry_after_parses_seconds_and_http_date(self):
        self.assertEqual(judge.parse_retry_after("2.5"), 2.5)
        stamp = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        self.assertEqual(judge.parse_retry_after("Thu, 01 Jan 2026 00:00:05 GMT", stamp), 5.0)
        self.assertIsNone(judge.parse_retry_after("invalid"))

    def test_generation_classification_does_not_score_failed_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            (task / "agent").mkdir()
            path = task / "generation.json"
            path.write_text("{}")
            failed = {
                "generation_status": "generation_failed",
                "identity_status": "valid",
                "judge_eligible": False,
            }
            result = judge.generation_classification(path, failed)
            self.assertTrue(result["valid_generation_failure"])
            self.assertFalse(result["judge_eligible"])

    def test_discovers_frozen_group_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for group in ("candidate", "baseline"):
                (root / "groups" / group / "tasks").mkdir(parents=True)
            (root / "generation-campaign-lock.json").write_text(
                json.dumps({"groups": ["baseline", "candidate"]})
            )
            self.assertEqual(judge.discover_groups(root), ("baseline", "candidate"))


if __name__ == "__main__":
    unittest.main()
