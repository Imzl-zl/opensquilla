import json
import tempfile
import unittest
from pathlib import Path

from _load import load

generate = load("monthly_generate_test", "generate.py")
profiles = load("monthly_profiles_generate_test", "profiles.py")


class GenerateTests(unittest.TestCase):
    def test_physical_usage_and_identity_are_derived_from_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = root / "agent"
            agent.mkdir()
            rows = [
                {"event": "llm.request", "call_id": "one", "model": "p1"},
                {
                    "event": "llm.response",
                    "call_id": "one",
                    "model": "p1",
                    "actual_model": "p1",
                    "response_ids": ["id"],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "reasoning_tokens": 5,
                        "cached_tokens": 40,
                        "billed_cost": 0.1,
                        "cost_source": "provider_billed",
                    },
                },
            ]
            (agent / "provider-calls.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            (agent / "transcript.jsonl").write_text(
                json.dumps({"message": {"role": "toolResult"}}) + "\n"
            )
            (agent / "usage.json").write_text(json.dumps({"request_count": 1}))
            metrics = generate.metrics_from_artifacts(agent)
            self.assertEqual(metrics["input_tokens"], 100)
            self.assertEqual(metrics["visible_tokens"], 15)
            self.assertEqual(metrics["tool_calls"], 1)
            self.assertTrue(metrics["generation_cost_exact"])

            spec = {
                "schema_version": profiles.LINEUP_SCHEMA,
                "provider": "openrouter",
                "fallback_model": "fallback",
                "groups": {"g": {"proposers": ["p1"], "aggregator": "agg"}},
            }
            config = profiles.build_profile("g", spec)
            config_path = root / "config.toml"
            config_path.write_text(profiles.dumps_toml(config))
            usage = {
                "ensemble_trace": {
                    "mode": "b5_fusion",
                    "selection_plan": {
                        "strategy": "custom_b5",
                        "proposers": [{"model": "p1"}],
                        "aggregator": {"model": "agg"},
                    },
                }
            }
            identity = generate.audit_fusion_identity(agent, config_path, usage)
            self.assertEqual(identity["status"], "passed")


if __name__ == "__main__":
    unittest.main()
