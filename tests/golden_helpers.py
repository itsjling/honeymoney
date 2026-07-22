import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from honeymoney.cli import _starter_csv_profile
from honeymoney.importers import _import_csv, _import_pdf

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO_ROOT / "honeymoney" / "data" / "profiles"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def load_profile(name: str) -> dict:
    return load_json(PROFILE_DIR / name)  # type: ignore[return-value]


def starter_profile() -> dict:
    return _starter_csv_profile()


def base_config() -> dict:
    return {
        "base_currency": "HKD",
        "exchange_rates": {"HKD": 1.0, "USD": 7.8},
        "pdf": {"enabled": True, "parser": "pdfplumber"},
        "review_confidence_threshold": 0.8,
    }


def import_profile_case(
    profile: dict, case_dir: Path
) -> tuple[list[dict[str, str]], list[str]]:
    if (case_dir / "input.csv").exists():
        return _import_csv_case(profile, case_dir / "input.csv"), []
    if (case_dir / "input.pdf").exists():
        return _import_pdf_byte_case(profile, case_dir / "input.pdf")
    if (case_dir / "tables.json").exists() or (case_dir / "words.json").exists():
        tables = _fixture_pages(case_dir / "tables.json")
        words_pages = _fixture_pages(case_dir / "words.json")
        return _import_pdf_case(profile, tables=tables, words_pages=words_pages)
    raise AssertionError(f"No supported input fixture found in {case_dir}")


def assert_import_case(
    test_case: unittest.TestCase,
    profile: dict,
    fixture_name: str,
) -> None:
    case_dir = FIXTURE_DIR / "import_profiles" / str(profile["id"]) / fixture_name
    expected = load_json(case_dir / "expected.json")
    rows, warnings = import_profile_case(profile, case_dir)

    test_case.assertEqual(warnings, expected.get("warnings", []))
    assert_rows_match(test_case, rows, expected["rows"], context=str(case_dir))


def assert_pdf_byte_import_case(
    test_case: unittest.TestCase,
    profile: dict,
    fixture_name: str,
) -> None:
    case_dir = FIXTURE_DIR / "import_profiles" / str(profile["id"]) / fixture_name
    fixture_path = case_dir / "input.pdf"
    test_case.assertTrue(fixture_path.is_file(), f"Missing PDF fixture: {fixture_path}")
    expected = load_json(case_dir / "expected.json")
    rows, warnings = import_profile_case(profile, case_dir)

    test_case.assertEqual(warnings, expected.get("warnings", []))
    assert_rows_match(test_case, rows, expected["rows"], context=str(case_dir))


def assert_rows_match(
    test_case: unittest.TestCase,
    actual_rows: list[dict[str, str]],
    expected_rows: list[dict[str, object]],
    *,
    context: str = "",
) -> None:
    test_case.assertEqual(
        len(actual_rows),
        len(expected_rows),
        f"{context} row count",
    )
    for index, expected in enumerate(expected_rows):
        actual = actual_rows[index]
        row_context = f"{context} row {index + 1}".strip()
        for key, expected_value in expected.items():
            if key == "_flags_contains":
                for flag in expected_value:
                    test_case.assertIn(str(flag), actual.get("flags", ""), row_context)
                continue
            test_case.assertEqual(
                actual.get(key),
                expected_value,
                f"{row_context} field {key}",
            )


def _import_csv_case(profile: dict, fixture_path: Path) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        csv_path = root / "statement.csv"
        csv_path.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")
        return _import_csv(csv_path, profile, base_config(), root)


def _import_pdf_byte_case(
    profile: dict, fixture_path: Path
) -> tuple[list[dict[str, str]], list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        statement_path = root / "statement.pdf"
        statement_path.write_bytes(fixture_path.read_bytes())
        return _import_pdf(statement_path, profile, base_config(), root)


def _import_pdf_case(
    profile: dict,
    *,
    tables: list[list[list[list[str | None]]]] | None,
    words_pages: list[list[dict[str, object]]] | None,
) -> tuple[list[dict[str, str]], list[str]]:
    class Page:
        def __init__(
            self,
            page_tables: list[list[list[str | None]]] | None = None,
            words: list[dict[str, object]] | None = None,
        ) -> None:
            self._tables = page_tables or []
            self._words = words or []

        def extract_table(self) -> list[list[str | None]] | None:
            return self._tables[0] if self._tables else None

        def extract_tables(self) -> list[list[list[str | None]]]:
            return self._tables

        def extract_words(self, **kwargs: object) -> list[dict[str, object]]:
            return self._words

    class Pdf:
        def __init__(self) -> None:
            table_pages = tables or []
            word_fixture_pages = words_pages or []
            page_count = max(len(table_pages), len(word_fixture_pages), 1)
            self.pages = [
                Page(
                    table_pages[index] if index < len(table_pages) else [],
                    word_fixture_pages[index]
                    if index < len(word_fixture_pages)
                    else [],
                )
                for index in range(page_count)
            ]

        def __enter__(self) -> "Pdf":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

    fake_pdfplumber = types.SimpleNamespace(open=lambda path: Pdf())

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf_path = root / "statement.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 synthetic")
        with patch.dict(sys.modules, {"pdfplumber": fake_pdfplumber}):
            return _import_pdf(pdf_path, profile, base_config(), root)


def _fixture_pages(path: Path) -> object | None:
    if not path.exists():
        return None
    data = load_json(path)
    return data["pages"]
