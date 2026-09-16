import json
import tempfile
import unittest
from pathlib import Path

from _load import ROOT, load

profiles = load("monthly_profiles_test", "profiles.py")


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.spec = profiles.load_spec(ROOT / "lineups.example.json")

    def test_example_materializes_and_validates(self):
        config = profiles.build_profile("candidate", self.spec)
        profiles.assert_profile_config(config, "candidate", self.spec)
        candidates = config["llm_ensemble"]["candidates"]
        self.assertEqual([row["role"] for row in candidates], ["proposer"] * 4 + ["aggregator"])
        self.assertEqual(candidates[-1]["model"], self.spec["groups"]["candidate"]["aggregator"])

    def test_manifest_uses_relative_paths_and_refuses_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = profiles.write_profiles(root, self.spec, source_revision="abc123")
            self.assertEqual(
                manifest["profiles"]["baseline"]["config_path"], "baseline/config.toml"
            )
            parsed = profiles.tomllib.loads((root / "baseline/config.toml").read_text())
            profiles.assert_profile_config(parsed, "baseline", self.spec)
            (root / "baseline/config.toml").write_text("drift = true\n")
            with self.assertRaises(FileExistsError):
                profiles.write_profiles(root, self.spec, source_revision="abc123")

    def test_manifest_refuses_metadata_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profiles.write_profiles(root, self.spec, source_revision="abc123")
            with self.assertRaises(FileExistsError):
                profiles.write_profiles(root, self.spec, source_revision="different")

    def test_rejects_literal_secret_and_bad_group(self):
        with self.assertRaises(ValueError):
            profiles.build_profile("baseline", self.spec, {"llm": {"api_key": "secret"}})
        invalid = json.loads(json.dumps(self.spec))
        invalid["groups"]["bad/name"] = invalid["groups"].pop("baseline")
        with self.assertRaises(ValueError):
            profiles.validate_spec(invalid)


if __name__ == "__main__":
    unittest.main()
