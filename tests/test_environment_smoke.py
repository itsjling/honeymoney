import importlib
import json
import unittest
from pathlib import Path

from honeymoney.importers import _load_profiles

REPO_ROOT = Path(__file__).resolve().parents[1]


class SyntheticEnvironmentSmokeTest(unittest.TestCase):
    def test_package_profiles_and_goldens_load_without_private_workspaces(self) -> None:
        for module in [
            "honeymoney.cli",
            "honeymoney.corrections",
            "honeymoney.ollama",
            "honeymoney.reconciliation",
            "honeymoney.report",
            "honeymoney.rules",
            "honeymoney.schema",
        ]:
            with self.subTest(module=module):
                importlib.import_module(module)

        profile_paths = sorted((REPO_ROOT / "honeymoney/data/profiles").glob("*.json"))
        self.assertGreater(len(profile_paths), 0)
        profiles = _load_profiles({"profiles": [str(path) for path in profile_paths]})
        self.assertEqual(len(profiles), len(profile_paths))

        fixture_root = (REPO_ROOT / "tests/fixtures").resolve()
        golden_paths = sorted((fixture_root / "categorization").rglob("*.json"))
        self.assertGreater(len(golden_paths), 0)
        for path in golden_paths:
            with self.subTest(golden=path.name):
                self.assertIn(fixture_root, path.resolve().parents)
                json.loads(path.read_text(encoding="utf-8"))
                self.assertFalse(
                    {"samples", "private_samples", "money"}.intersection(path.parts)
                )


if __name__ == "__main__":
    unittest.main()
