import io
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import check_distribution

REPO_ROOT = Path(__file__).resolve().parents[1]


class PythonSupportPolicyTest(unittest.TestCase):
    def test_release_version_and_python_policy_are_consistent_across_live_gates(
        self,
    ) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        refresh = (REPO_ROOT / "scripts/refresh-constraints.sh").read_text(
            encoding="utf-8"
        )
        constraints = (REPO_ROOT / "constraints/dev.txt").read_text(encoding="utf-8")

        self.assertIn('version = "0.2.0"', pyproject)
        self.assertIn('requires-python = ">=3.14,<3.15"', pyproject)
        self.assertIn('target-version = "py314"', pyproject)
        self.assertIn('python_version = "3.14"', pyproject)
        self.assertIn('python-version: ["3.14.6"]', workflow)
        self.assertIn('python-version: "3.14.6"', workflow)
        self.assertIn('python_bin="${PYTHON:-python3.14}"', refresh)
        self.assertIn('if [[ "${python_version}" != "3.14.6" ]]', refresh)
        self.assertIn(
            "# Validate on Python 3.14.6 before accepting a refresh.", constraints
        )
        self.assertNotIn("3.13", refresh)

    def test_constraint_refresh_rejects_a_non_release_patch_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            interpreter = Path(tmp) / "python"
            interpreter.write_text(
                "#!/usr/bin/env bash\n"
                'if [[ "$1" == "-c" ]]; then\n'
                '  printf "3.14.5\\n"\n'
                "  exit 0\n"
                "fi\n"
                "exit 1\n",
                encoding="utf-8",
            )
            interpreter.chmod(0o700)
            environment = os.environ | {"PYTHON": str(interpreter)}

            result = subprocess.run(
                ["bash", str(REPO_ROOT / "scripts/refresh-constraints.sh")],
                cwd=REPO_ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn(
                "Constraint refresh must use Python 3.14.6; got 3.14.5.",
                result.stderr,
            )

    def test_packaging_is_a_direct_development_dependency(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('"packaging>=26,<27"', pyproject)

    def test_workspace_storage_modules_are_in_strict_mypy_scope(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")

        for module in (
            "honeymoney/doctor.py",
            "honeymoney/import_records.py",
            "honeymoney/periods.py",
            "honeymoney/workspace_derivation.py",
            "honeymoney/workspace_index.py",
            "honeymoney/workspace_paths.py",
            "honeymoney/workspace_publication.py",
            "honeymoney/workspace_commands.py",
            "honeymoney/workspace_views.py",
        ):
            self.assertIn(f'"{module}"', pyproject)


class DistributionPayloadTest(unittest.TestCase):
    def test_release_metadata_requires_the_0_2_0_python_contract(self) -> None:
        artifact = Path("honeymoney-0.2.0-py3-none-any.whl")
        metadata = (
            "Metadata-Version: 2.4\n"
            "Name: honeymoney\n"
            "Version: 0.2.0\n"
            "Requires-Python: >=3.14,<3.15\n"
        )

        check_distribution._assert_release_metadata(metadata, artifact)
        check_distribution._assert_release_metadata(
            metadata.replace(">=3.14,<3.15", "<3.15,>=3.14"), artifact
        )

        with self.assertRaisesRegex(ValueError, "release version"):
            check_distribution._assert_release_metadata(
                metadata.replace("Version: 0.2.0", "Version: 0.2.1"), artifact
            )
        with self.assertRaisesRegex(ValueError, "requires-python"):
            check_distribution._assert_release_metadata(
                metadata.replace(">=3.14,<3.15", ">=3.14"), artifact
            )

    def test_wheel_payload_requires_console_script_and_all_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "honeymoney-0.2.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(
                    "honeymoney-0.2.0.dist-info/entry_points.txt",
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
                    "honeymoney-0.2.0.dist-info/entry_points.txt",
                    "[console_scripts]\nhoneymoney = honeymoney.cli:run\n",
                )

            with self.assertRaisesRegex(ValueError, "bundled profiles"):
                check_distribution._assert_wheel_payload(wheel)

    def test_source_archive_payload_requires_console_script_and_all_profiles(
        self,
    ) -> None:
        def add_text(archive: tarfile.TarFile, name: str, content: str) -> None:
            data = content.encode("utf-8")
            member = tarfile.TarInfo(f"honeymoney-0.2.0/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))

        with tempfile.TemporaryDirectory() as tmp:
            source_archive = Path(tmp) / "honeymoney-0.2.0.tar.gz"
            with tarfile.open(source_archive, "w:gz") as archive:
                add_text(
                    archive,
                    "pyproject.toml",
                    '[project]\n[project.scripts]\nhoneymoney = "honeymoney.cli:run"\n',
                )
                add_text(archive, "honeymoney/cli.py", "")
                for profile in check_distribution.EXPECTED_BUNDLED_PROFILES:
                    add_text(archive, f"honeymoney/data/profiles/{profile}", "{}")

            check_distribution._assert_sdist_payload(source_archive)

            with tarfile.open(source_archive, "w:gz") as archive:
                add_text(
                    archive,
                    "pyproject.toml",
                    '[project]\n[project.scripts]\nhoneymoney = "honeymoney.cli:run"\n',
                )
                add_text(archive, "honeymoney/cli.py", "")

            with self.assertRaisesRegex(ValueError, "bundled profiles"):
                check_distribution._assert_sdist_payload(source_archive)


if __name__ == "__main__":
    unittest.main()
