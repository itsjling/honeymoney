import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import check_distribution

REPO_ROOT = Path(__file__).resolve().parents[1]


class PythonSupportPolicyTest(unittest.TestCase):
    def test_python_floor_is_consistent_across_live_gates(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        refresh = (REPO_ROOT / "scripts/refresh-constraints.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn('requires-python = ">=3.11"', pyproject)
        self.assertIn('target-version = "py311"', pyproject)
        self.assertIn('python_version = "3.11"', pyproject)
        self.assertIn('python-version: ["3.11", "3.13"]', workflow)
        self.assertIn('python_bin="${PYTHON:-python3.11}"', refresh)
        self.assertIn('if [[ "${python_version}" != "3.11" ]]', refresh)
        self.assertNotIn("3.10", refresh)

    def test_packaging_is_a_direct_development_dependency(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('"packaging>=26,<27"', pyproject)


class DistributionPayloadTest(unittest.TestCase):
    def test_wheel_payload_requires_console_script_and_all_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "honeymoney-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "honeymoney-0.1.0.dist-info/entry_points.txt",
                    "[console_scripts]\nhoneymoney = honeymoney.cli:run\n",
                )
                for profile in check_distribution.EXPECTED_BUNDLED_PROFILES:
                    archive.writestr(
                        f"honeymoney/data/profiles/{profile}",
                        "{}",
                    )

            check_distribution._assert_wheel_payload(wheel)

            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "honeymoney-0.1.0.dist-info/entry_points.txt",
                    "[console_scripts]\nhoneymoney = honeymoney.cli:run\n",
                )

            with self.assertRaisesRegex(ValueError, "bundled profiles"):
                check_distribution._assert_wheel_payload(wheel)


if __name__ == "__main__":
    unittest.main()
