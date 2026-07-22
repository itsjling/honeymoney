import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.importers import _import_transactions
from honeymoney.normalization import _normalized_row

REPO_ROOT = Path(__file__).resolve().parents[1]


class ModuleBoundaryTest(unittest.TestCase):
    def test_normalization_is_stdlib_only_and_avoids_filesystem_calls(self) -> None:
        module_path = REPO_ROOT / "honeymoney" / "normalization.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        forbidden_attributes = {
            "cwd",
            "exists",
            "expanduser",
            "is_dir",
            "is_file",
            "iterdir",
            "open",
            "resolve",
            "walk",
        }
        imports = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]
        violations = sorted(
            {
                f"{node.func.attr} (line {node.lineno})"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden_attributes
            }
        )

        self.assertTrue(all(not module.startswith("honeymoney") for module in imports))
        self.assertEqual(violations, [])
        source = module_path.read_text(encoding="utf-8")
        self.assertNotIn("src_v1", source)
        self.assertNotIn("occurrence", source)

    def test_importers_do_not_depend_on_cli(self) -> None:
        module_path = REPO_ROOT / "honeymoney" / "importers.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        imported_modules = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ]

        self.assertNotIn("honeymoney.cli", imported_modules)

    def test_normalization_accepts_the_importer_computed_source_display(self) -> None:
        profile = {
            "account_id": "test",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
        }
        row = _normalized_row(
            {"date": "2026-01-01", "description": "Synthetic", "amount": "-1"},
            2,
            profile,
            {"base_currency": "HKD", "exchange_rates": {"HKD": 1}},
            {
                "transaction_date": "date",
                "description": "description",
                "amount": "amount",
            },
            "nested/synthetic.csv",
        )

        self.assertEqual(row["source_file"], "nested/synthetic.csv")

    def test_importer_reports_progress_through_injected_callback(self) -> None:
        profile = {
            "id": "synthetic",
            "account_id": "test",
            "account_currency": "HKD",
            "date_formats": ["%Y-%m-%d"],
            "csv": {
                "columns": {
                    "transaction_date": "Date",
                    "description": "Description",
                    "amount": "Amount",
                },
                "detect_headers": ["Date", "Description", "Amount"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "synthetic.csv"
            statement.write_text(
                "Date,Description,Amount\n2026-01-01,Synthetic,-1.00\n",
                encoding="utf-8",
            )
            progress: list[str] = []
            _import_transactions(
                [statement],
                [profile],
                {"base_currency": "HKD", "exchange_rates": {"HKD": 1}},
                root,
                False,
                {},
                None,
                status=progress.append,
            )

        self.assertEqual(progress, ["Importing statements... (1/1) synthetic.csv"])

    def test_interactive_profile_prompt_clears_status_before_prompt_output(
        self,
    ) -> None:
        profiles = [
            {
                "id": "first",
                "account_id": "first",
                "account_currency": "HKD",
                "date_formats": ["%Y-%m-%d"],
                "csv": {
                    "columns": {
                        "transaction_date": "Date",
                        "description": "Description",
                        "amount": "Amount",
                    }
                },
            },
            {
                "id": "second",
                "account_id": "second",
                "account_currency": "HKD",
                "date_formats": ["%Y-%m-%d"],
                "csv": {
                    "columns": {
                        "transaction_date": "Date",
                        "description": "Description",
                        "amount": "Amount",
                    }
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            statement = root / "synthetic.csv"
            statement.write_text(
                "Date,Description,Amount\n2026-01-01,Synthetic,-1.00\n",
                encoding="utf-8",
            )
            events: list[str] = []

            with (
                patch("builtins.input", return_value="1"),
                patch(
                    "builtins.print",
                    side_effect=lambda value: events.append(f"prompt: {value}"),
                ),
            ):
                _import_transactions(
                    [statement],
                    profiles,
                    {"base_currency": "HKD", "exchange_rates": {"HKD": 1}},
                    root,
                    True,
                    {},
                    None,
                    status=lambda value: events.append(f"status: {value}"),
                    clear_status=lambda: events.append("clear"),
                )

        self.assertEqual(
            events[:3],
            [
                "status: Importing statements... (1/1) synthetic.csv",
                "clear",
                "prompt: Select profile for synthetic.csv:",
            ],
        )


if __name__ == "__main__":
    unittest.main()
