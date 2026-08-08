import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class AccountBindingWorkflowTest(unittest.TestCase):
    def _run_cli(
        self, args: list[str], *, cwd: Path, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _setup_workspace(self, temporary_root: str) -> Path:
        root = Path(temporary_root) / "money"
        result = self._run_cli(["setup", "--root", str(root)], cwd=REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        return root

    def _bind(
        self,
        root: Path,
        binding_id: str,
        pattern: str,
        owner: str,
        account_id: str,
        account: str,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_cli(
            [
                "profile",
                "bind",
                binding_id,
                "--pattern",
                pattern,
                "--profile",
                "starter_csv",
                "--owner",
                owner,
                "--account",
                f"starter_csv={account_id}={account}",
                "--json",
            ],
            cwd=root,
        )

    def test_cli_creates_lists_and_reuses_two_bindings_for_one_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statements = root / "shared-layout"
            statements.mkdir()
            (statements / "justin-may.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-01,SYNTHETIC JUSTIN,-10.00,HKD\n",
                encoding="utf-8",
            )
            (statements / "franchesca-may.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-02,SYNTHETIC FRANCHESCA,-20.00,HKD\n",
                encoding="utf-8",
            )
            parser_profile = root / "profiles" / "starter_csv.json"
            parser_profile_before = parser_profile.read_bytes()

            justin = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            franchesca = self._bind(
                root,
                "franchesca-local",
                "franchesca-*.csv",
                "Franchesca",
                "franchesca_local",
                "Franchesca Local Account",
            )

            self.assertEqual(justin.returncode, 0, justin.stderr)
            self.assertEqual(franchesca.returncode, 0, franchesca.stderr)
            justin_payload = json.loads(justin.stdout)
            self.assertEqual(justin_payload["command"], "profile.bind")
            self.assertEqual(justin_payload["status"], "success")
            self.assertEqual(
                justin_payload["data"]["binding"],
                {
                    "id": "justin-local",
                    "profile": "starter_csv",
                    "owner": "Justin",
                    "accounts": [
                        {
                            "source_account_id": "starter_csv",
                            "account_id": "justin_local",
                            "account": "Justin Local Account",
                        }
                    ],
                    "patterns": ["justin-*.csv"],
                },
            )
            self.assertEqual(
                justin_payload["artifacts"]["profile_mappings_json"],
                str((root / "profile_mappings.json").resolve()),
            )
            self.assertEqual(parser_profile.read_bytes(), parser_profile_before)
            listed = self._run_cli(["profile", "bindings", "--json"], cwd=root)
            self.assertEqual(listed.returncode, 0, listed.stderr)
            payload = json.loads(listed.stdout)
            self.assertEqual(payload["command"], "profile.bindings")
            self.assertEqual(
                [item["id"] for item in payload["data"]["bindings"]],
                ["franchesca-local", "justin-local"],
            )

            interactive_import = self._run_cli(
                ["import", str(statements / "justin-may.csv")],
                cwd=root,
                input_text="q\n",
            )
            self.assertEqual(
                interactive_import.returncode, 0, interactive_import.stderr
            )
            imported = self._run_cli(
                [
                    "import",
                    str(statements / "franchesca-may.csv"),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            import_payload = json.loads(imported.stdout)
            self.assertEqual(
                import_payload["data"]["files"][0]["binding_id"],
                "franchesca-local",
            )
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {(row["owner"], row["account_id"], row["account"]) for row in rows},
                {
                    ("Justin", "justin_local", "Justin Local Account"),
                    (
                        "Franchesca",
                        "franchesca_local",
                        "Franchesca Local Account",
                    ),
                },
            )

    def test_incomplete_and_colliding_bindings_fail_without_changing_mappings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            mappings_path = root / "profile_mappings.json"
            before = mappings_path.read_bytes()

            incomplete = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-hsbc-one",
                    "--pattern",
                    "justin-hsbc-*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Justin",
                    "--account",
                    "hsbc_one_hkd_savings=justin_hsbc_savings=Justin HSBC Savings",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(incomplete.returncode, 2, incomplete.stderr)
            incomplete_error = json.loads(incomplete.stdout)["errors"][0]["message"]
            self.assertIn("does not cover profile hsbc_one_pdf", incomplete_error)
            self.assertIn("hsbc_one_hkd_current", incomplete_error)
            self.assertEqual(mappings_path.read_bytes(), before)

            first = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "Shared_Account_ID",
                "Justin Local Account",
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            after_first = mappings_path.read_bytes()
            collision = self._bind(
                root,
                "franchesca-local",
                "franchesca-*.csv",
                "Franchesca",
                "shared_account_id",
                "Franchesca Local Account",
            )
            self.assertEqual(collision.returncode, 2, collision.stderr)
            collision_error = json.loads(collision.stdout)["errors"][0]["message"]
            self.assertIn("Account identity collision", collision_error)
            self.assertNotIn("Shared_Account_ID", collision_error)
            self.assertNotIn("shared_account_id", collision_error)
            self.assertNotIn("Local Account", collision_error)
            self.assertEqual(mappings_path.read_bytes(), after_first)

    def test_sectioned_profile_binds_every_emitted_account_to_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statement = root / "justin-hsbc-one.pdf"
            fixture = (
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "import_profiles"
                / "hsbc_one_pdf"
                / "accepted_statement"
                / "input.pdf"
            )
            statement.write_bytes(fixture.read_bytes())

            bound = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-hsbc-one",
                    "--pattern",
                    "justin-hsbc-*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Justin",
                    "--account",
                    "hsbc_one_hkd_savings=justin_hsbc_savings=Justin HSBC Savings",
                    "--account",
                    "hsbc_one_hkd_current=justin_hsbc_current=Justin HSBC Current",
                    "--account",
                    "hsbc_one_fcy_savings=justin_hsbc_foreign=Justin HSBC Foreign Currency",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            imported = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows)
            self.assertEqual({row["owner"] for row in rows}, {"Justin"})
            self.assertEqual(
                {row["account_id"] for row in rows},
                {
                    "justin_hsbc_savings",
                    "justin_hsbc_current",
                    "justin_hsbc_foreign",
                },
            )
            self.assertEqual(
                {row["account"] for row in rows},
                {
                    "Justin HSBC Savings",
                    "Justin HSBC Current",
                    "Justin HSBC Foreign Currency",
                },
            )

    def test_replace_under_new_binding_preserves_review_and_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statement = root / "justin-existing.csv"
            statement.write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-03,SYNTHETIC SALARY,100.00,HKD\n",
                encoding="utf-8",
            )
            first_import = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(first_import.returncode, 0, first_import.stderr)
            ledger_path = root / "output" / "categorized.csv"
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                imported_row = next(csv.DictReader(handle))

            reviewed = self._run_cli(
                [
                    "review",
                    "--transaction",
                    imported_row["transaction_id"],
                    "--as",
                    "income",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                reviewed_row = next(csv.DictReader(handle))
            owner_correction_path = root / "owner-correction.json"
            owner_correction_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": reviewed_row["transaction_id"],
                            "owner": "Franchesca",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            owner_corrected = self._run_cli(
                ["correct", "--file", str(owner_correction_path), "--json"],
                cwd=root,
            )
            self.assertEqual(owner_corrected.returncode, 0, owner_corrected.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                reviewed_row = next(csv.DictReader(handle))
            self.assertEqual(reviewed_row["owner"], "Franchesca")
            protected_fields = {
                field: reviewed_row[field]
                for field in (
                    "category",
                    "flow_type",
                    "flow_source",
                    "needs_review",
                    "review_reasons",
                    "reason",
                    "confidence",
                )
            }
            self.assertEqual(protected_fields["category"], "Income")
            self.assertEqual(protected_fields["flow_type"], "income")

            bound = self._bind(
                root,
                "justin-local",
                "justin-existing.csv",
                "Justin",
                "justin_local",
                "Justin Local Account",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            replaced = self._run_cli(
                [
                    "import",
                    str(statement),
                    "--replace",
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                rebound_row = next(csv.DictReader(handle))
            self.assertEqual(rebound_row["owner"], "Justin")
            self.assertEqual(rebound_row["account_id"], "justin_local")
            self.assertEqual(rebound_row["account"], "Justin Local Account")
            self.assertEqual(
                {field: rebound_row[field] for field in protected_fields},
                protected_fields,
            )
            self.assertNotEqual(
                rebound_row["transaction_id"], reviewed_row["transaction_id"]
            )
            with (root / "corrections.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                corrections = list(csv.DictReader(handle))
            self.assertEqual(
                [row["transaction_id"] for row in corrections],
                [rebound_row["transaction_id"]],
            )
            self.assertEqual(corrections[0]["owner"], "Justin")

            notes_path = root / "notes-correction.json"
            notes_path.write_text(
                json.dumps(
                    [
                        {
                            "transaction_id": rebound_row["transaction_id"],
                            "notes": "Synthetic note",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            notes_corrected = self._run_cli(
                ["correct", "--file", str(notes_path), "--json"],
                cwd=root,
            )
            self.assertEqual(notes_corrected.returncode, 0, notes_corrected.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                notes_row = next(csv.DictReader(handle))
            self.assertEqual(notes_row["owner"], "Justin")
            self.assertEqual(notes_row["notes"], "Synthetic note")

    def test_binding_owner_does_not_change_an_unbound_matching_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statements = root / "same-account-id"
            statements.mkdir()
            (statements / "justin-bound.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-05,SYNTHETIC BOUND,-10.00,HKD\n",
                encoding="utf-8",
            )
            (statements / "plain-unbound.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-05-06,SYNTHETIC UNBOUND,-20.00,HKD\n",
                encoding="utf-8",
            )
            bound = self._bind(
                root,
                "justin-local",
                "justin-*.csv",
                "Justin",
                "starter_csv",
                "Starter Account",
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)

            imported = self._run_cli(
                ["import", str(statements), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            with (root / "output" / "categorized.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                {row["original_description"]: row["owner"] for row in rows},
                {
                    "SYNTHETIC BOUND": "Justin",
                    "SYNTHETIC UNBOUND": "Household",
                },
            )

    def test_import_rejects_uncovered_dynamic_account_and_conflicting_matches(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            profile_path = root / "profiles" / "starter_csv.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["csv"]["columns"]["account_id"] = "Account ID"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            statement = root / "dynamic-may.csv"
            statement.write_text(
                "Date,Description,Amount,Currency,Account ID\n"
                "2026-05-04,PRIVATE SENTINEL,-987.65,HKD,uncovered_account\n",
                encoding="utf-8",
            )
            bound = self._run_cli(
                [
                    "profile",
                    "bind",
                    "dynamic-one",
                    "--pattern",
                    "dynamic-*.csv",
                    "--profile",
                    "starter_csv",
                    "--owner",
                    "Justin",
                    "--account",
                    "known_source=justin_known=Justin Known Account",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)

            incomplete = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(incomplete.returncode, 2, incomplete.stderr)
            incomplete_message = json.loads(incomplete.stdout)["errors"][0]["message"]
            self.assertIn("does not cover 1 emitted account id", incomplete_message)
            self.assertNotIn("uncovered_account", incomplete.stdout)
            self.assertNotIn("PRIVATE SENTINEL", incomplete.stdout)
            self.assertNotIn("987.65", incomplete.stdout)
            self.assertFalse((root / "output" / "categorized.csv").exists())

            second = self._run_cli(
                [
                    "profile",
                    "bind",
                    "dynamic-two",
                    "--pattern",
                    "*.csv",
                    "--profile",
                    "starter_csv",
                    "--owner",
                    "Franchesca",
                    "--account",
                    "uncovered_account=franchesca_dynamic=Franchesca Dynamic Account",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            conflicting = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(conflicting.returncode, 2, conflicting.stderr)
            conflict_message = json.loads(conflicting.stdout)["errors"][0]["message"]
            self.assertEqual(
                conflict_message,
                "Conflicting filename mappings for dynamic-may.csv",
            )
            self.assertNotIn("PRIVATE SENTINEL", conflicting.stdout)
            self.assertFalse((root / "output" / "categorized.csv").exists())

    def test_pdf_binding_conflict_is_an_error_not_a_skipped_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = self._setup_workspace(temporary_root)
            statement = root / "justin-conflict.pdf"
            fixture = (
                REPO_ROOT
                / "tests"
                / "fixtures"
                / "import_profiles"
                / "hsbc_one_pdf"
                / "accepted_statement"
                / "input.pdf"
            )
            statement.write_bytes(fixture.read_bytes())
            first = self._run_cli(
                [
                    "profile",
                    "bind",
                    "justin-hsbc",
                    "--pattern",
                    "justin-*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Justin",
                    "--account",
                    "hsbc_one_hkd_savings=justin_savings=Justin Savings",
                    "--account",
                    "hsbc_one_hkd_current=justin_current=Justin Current",
                    "--account",
                    "hsbc_one_fcy_savings=justin_foreign=Justin Foreign",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            second = self._run_cli(
                [
                    "profile",
                    "bind",
                    "franchesca-hsbc",
                    "--pattern",
                    "*.pdf",
                    "--profile",
                    "hsbc_one_pdf",
                    "--owner",
                    "Franchesca",
                    "--account",
                    "hsbc_one_hkd_savings=franchesca_savings=Franchesca Savings",
                    "--account",
                    "hsbc_one_hkd_current=franchesca_current=Franchesca Current",
                    "--account",
                    "hsbc_one_fcy_savings=franchesca_foreign=Franchesca Foreign",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(second.returncode, 0, second.stderr)

            result = self._run_cli(
                ["import", str(statement), "--no-interactive", "--json"],
                cwd=root,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["status"], "error")
            self.assertEqual(
                payload["errors"][0]["message"],
                "Conflicting filename mappings for justin-conflict.pdf",
            )
            self.assertFalse((root / "output" / "categorized.csv").exists())


if __name__ == "__main__":
    unittest.main()
