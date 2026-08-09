"""Synthetic branch coverage for importer and account-binding boundaries."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from honeymoney import importers, normalization
from honeymoney.account_bindings import (
    AccountBindingError,
    apply_binding,
    binding_by_id,
    binding_for_source,
    binding_views,
    canonical_bound_owners,
    enforce_bound_owners,
    matching_filename_mapping,
    normalized_account_id,
    remove_binding_pattern,
    replace_binding_pattern,
    static_profile_account_ids,
    upsert_binding,
    validate_bindings_for_profiles,
    validate_profile_mappings,
)


def _config() -> dict[str, object]:
    return {
        "base_currency": "HKD",
        "exchange_rates": {"HKD": 1, "USD": "7.8"},
        "owners": ["Household", "Justin", "Franchesca"],
        "payment_methods": ["Bank Account", "Credit Card"],
        "pdf": {"enabled": True},
    }


def _binding(
    binding_id: str = "binding-one",
    *,
    profile: str = "profile-one",
    owner: str = "Justin",
    source_account_id: str = "source-account",
    account_id: str = "justin-account",
    account: str = "Justin account",
) -> dict[str, object]:
    return {
        "id": binding_id,
        "profile": profile,
        "owner": owner,
        "accounts": [
            {
                "source_account_id": source_account_id,
                "account_id": account_id,
                "account": account,
            }
        ],
    }


def _mappings() -> dict[str, object]:
    return {
        "account_bindings": [_binding()],
        "filename_patterns": [
            {
                "pattern": "source-*.csv",
                "profile": "profile-one",
                "binding": "binding-one",
            }
        ],
    }


def _csv_profile(profile_id: str = "profile-one") -> dict[str, object]:
    return {
        "id": profile_id,
        "account_id": "source-account",
        "account": "Synthetic account",
        "institution": "Synthetic institution",
        "country": "HK",
        "account_currency": "HKD",
        "owner": "Justin",
        "payment_method": "Bank Account",
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


def _pdf_profile(profile_id: str = "profile-one") -> dict[str, object]:
    profile = _csv_profile(profile_id)
    profile.pop("csv")
    profile["pdf"] = {
        "columns": {
            "transaction_date": "Date",
            "description": "Description",
            "amount": "Amount",
        }
    }
    return profile


class AccountBindingCoverageTest(unittest.TestCase):
    def test_upsert_binding_replaces_compatible_pattern_and_rejects_conflicts(
        self,
    ) -> None:
        binding = _binding()
        compatible = {
            "account_bindings": [dict(binding)],
            "filename_patterns": [
                "retained non-mapping",
                {"pattern": "source-*.csv", "profile": "profile-one"},
            ],
            "removed_filename_patterns": [
                {
                    "binding": "binding-one",
                    "pattern": "old-*.csv",
                    "profile": "profile-one",
                },
                {
                    "binding": "binding-other",
                    "pattern": "other-*.csv",
                    "profile": "profile-two",
                },
            ],
        }

        replaced = upsert_binding(compatible, binding, "source-*.csv")

        self.assertEqual(
            replaced["filename_patterns"],
            [
                "retained non-mapping",
                {
                    "pattern": "source-*.csv",
                    "profile": "profile-one",
                    "binding": "binding-one",
                },
            ],
        )
        self.assertEqual(
            replaced["removed_filename_patterns"],
            [
                {
                    "binding": "binding-other",
                    "pattern": "other-*.csv",
                    "profile": "profile-two",
                }
            ],
        )

        appended = upsert_binding({}, binding, "new-*.csv")
        self.assertEqual(
            appended["filename_patterns"],
            [
                {
                    "pattern": "new-*.csv",
                    "profile": "profile-one",
                    "binding": "binding-one",
                }
            ],
        )
        with self.assertRaisesRegex(AccountBindingError, "already selects another"):
            upsert_binding(
                {
                    "filename_patterns": [
                        {
                            "pattern": "source-*.csv",
                            "profile": "profile-two",
                            "binding": "binding-two",
                        }
                    ]
                },
                binding,
                "source-*.csv",
            )

    def test_replace_binding_pattern_handles_replays_conflicts_and_moves(self) -> None:
        binding = _binding()
        base = _mappings()
        with self.assertRaisesRegex(AccountBindingError, "Unknown account binding"):
            replace_binding_pattern({}, "missing", "old", "new")

        replay_input = {
            "account_bindings": [binding],
            "filename_patterns": [
                {
                    "pattern": "new-*.csv",
                    "profile": "profile-one",
                    "binding": "binding-one",
                }
            ],
            "replaced_filename_patterns": [
                {
                    "binding": "binding-one",
                    "old_pattern": "old-*.csv",
                    "new_pattern": "new-*.csv",
                    "profile": "profile-one",
                }
            ],
        }
        replayed, replay_changed = replace_binding_pattern(
            replay_input, "binding-one", "old-*.csv", "new-*.csv"
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replayed, replay_input)
        with self.assertRaisesRegex(AccountBindingError, "does not use filename"):
            replace_binding_pattern(base, "binding-one", "absent", "new")

        unchanged, changed = replace_binding_pattern(
            base, "binding-one", "source-*.csv", "source-*.csv"
        )
        self.assertFalse(changed)
        self.assertEqual(unchanged, base)
        conflicting = copy.deepcopy(base)
        conflicting["filename_patterns"] = [
            *conflicting["filename_patterns"],
            {
                "pattern": "other-*.csv",
                "profile": "profile-two",
                "binding": "binding-two",
            },
        ]
        with self.assertRaisesRegex(AccountBindingError, "already selects another"):
            replace_binding_pattern(
                conflicting, "binding-one", "source-*.csv", "other-*.csv"
            )

        duplicate = copy.deepcopy(base)
        duplicate["filename_patterns"] = [
            *duplicate["filename_patterns"],
            {
                "pattern": "same-*.csv",
                "profile": "profile-one",
                "binding": "binding-one",
            },
        ]
        deduplicated, changed = replace_binding_pattern(
            duplicate, "binding-one", "source-*.csv", "same-*.csv"
        )
        self.assertTrue(changed)
        self.assertEqual(len(deduplicated["filename_patterns"]), 1)

        renamed, changed = replace_binding_pattern(
            base, "binding-one", "source-*.csv", "renamed-*.csv"
        )
        self.assertTrue(changed)
        self.assertEqual(
            renamed["filename_patterns"],
            [
                {
                    "pattern": "renamed-*.csv",
                    "profile": "profile-one",
                    "binding": "binding-one",
                }
            ],
        )
        self.assertEqual(
            renamed["replaced_filename_patterns"],
            [
                {
                    "binding": "binding-one",
                    "old_pattern": "source-*.csv",
                    "new_pattern": "renamed-*.csv",
                    "profile": "profile-one",
                }
            ],
        )

    def test_remove_binding_pattern_handles_confirmation_and_receipts(self) -> None:
        with self.assertRaisesRegex(AccountBindingError, "Unknown account binding"):
            remove_binding_pattern({}, "missing", "missing", confirm_final=True)

        receipt_only = {
            "removed_filename_patterns": [
                {
                    "binding": "gone",
                    "pattern": "gone-*.csv",
                    "profile": "profile-one",
                }
            ]
        }
        replayed, changed, removed_binding, profile_id = remove_binding_pattern(
            receipt_only, "gone", "gone-*.csv", confirm_final=True
        )
        self.assertEqual(replayed, receipt_only)
        self.assertFalse(changed)
        self.assertTrue(removed_binding)
        self.assertEqual(profile_id, "profile-one")

        base = _mappings()
        base["removed_filename_patterns"] = [
            {
                "binding": "binding-one",
                "pattern": "already-removed.csv",
                "profile": "profile-one",
            }
        ]
        replayed, changed, removed_binding, profile_id = remove_binding_pattern(
            base, "binding-one", "already-removed.csv", confirm_final=True
        )
        self.assertEqual(replayed, base)
        self.assertFalse(changed)
        self.assertFalse(removed_binding)
        self.assertEqual(profile_id, "profile-one")
        with self.assertRaisesRegex(AccountBindingError, "does not use filename"):
            remove_binding_pattern(
                base, "binding-one", "absent.csv", confirm_final=True
            )
        with self.assertRaisesRegex(AccountBindingError, "pass --yes"):
            remove_binding_pattern(
                _mappings(), "binding-one", "source-*.csv", confirm_final=False
            )

        two_patterns = _mappings()
        two_patterns["filename_patterns"] = [
            *two_patterns["filename_patterns"],
            {
                "pattern": "second-*.csv",
                "profile": "profile-one",
                "binding": "binding-one",
            },
        ]
        kept, changed, removed_binding, profile_id = remove_binding_pattern(
            two_patterns, "binding-one", "source-*.csv", confirm_final=True
        )
        self.assertTrue(changed)
        self.assertFalse(removed_binding)
        self.assertEqual(profile_id, "profile-one")
        self.assertEqual(len(kept["account_bindings"]), 1)

        removed, changed, removed_binding, profile_id = remove_binding_pattern(
            _mappings(), "binding-one", "source-*.csv", confirm_final=True
        )
        self.assertTrue(changed)
        self.assertTrue(removed_binding)
        self.assertEqual(profile_id, "profile-one")
        self.assertEqual(removed["account_bindings"], [])

    def test_validate_profile_mappings_rejects_each_public_shape_error(self) -> None:
        valid = _mappings()
        self.assertEqual(
            validate_profile_mappings(copy.deepcopy(valid), _config()), valid
        )

        second = _binding(
            "binding-two",
            owner="Franchesca",
            source_account_id="source-two",
            account_id="franchesca-account",
        )
        cases: list[tuple[str, object, str]] = [
            ("document", [], "JSON object"),
            (
                "patterns array",
                {"filename_patterns": {}, "account_bindings": []},
                "filename_patterns must be a JSON array",
            ),
            (
                "bindings array",
                {"filename_patterns": [], "account_bindings": {}},
                "account_bindings must be a JSON array",
            ),
            (
                "binding object",
                {"filename_patterns": [], "account_bindings": ["bad"]},
                "must be a JSON object",
            ),
            (
                "binding id",
                {
                    "filename_patterns": [],
                    "account_bindings": [{**_binding(), "id": ""}],
                },
                "id must be a non-empty string",
            ),
            (
                "duplicate binding",
                {"filename_patterns": [], "account_bindings": [_binding(), _binding()]},
                "Duplicate account binding id",
            ),
            (
                "unsupported owner",
                {
                    "filename_patterns": [],
                    "account_bindings": [{**_binding(), "owner": "Unknown person"}],
                },
                "Unsupported owner",
            ),
            (
                "empty accounts",
                {
                    "filename_patterns": [],
                    "account_bindings": [{**_binding(), "accounts": []}],
                },
                "accounts must be a non-empty JSON array",
            ),
            (
                "account object",
                {
                    "filename_patterns": [],
                    "account_bindings": [{**_binding(), "accounts": ["bad"]}],
                },
                "JSON object",
            ),
            (
                "account field",
                {
                    "filename_patterns": [],
                    "account_bindings": [
                        {
                            **_binding(),
                            "accounts": [
                                {
                                    "source_account_id": "",
                                    "account_id": "x",
                                    "account": "x",
                                }
                            ],
                        }
                    ],
                },
                "source_account_id must be a non-empty string",
            ),
            (
                "duplicate source account",
                {
                    "filename_patterns": [],
                    "account_bindings": [
                        {
                            **_binding(),
                            "accounts": [
                                _binding()["accounts"][0],
                                {
                                    "source_account_id": "source-account",
                                    "account_id": "other",
                                    "account": "Other",
                                },
                            ],
                        }
                    ],
                },
                "maps source account",
            ),
            (
                "target collision",
                {
                    "filename_patterns": [],
                    "account_bindings": [
                        _binding(),
                        {
                            **second,
                            "accounts": [
                                {
                                    "source_account_id": "source-two",
                                    "account_id": " JUSTIN-account ",
                                    "account": "Other",
                                }
                            ],
                        },
                    ],
                },
                "Account identity collision",
            ),
            (
                "removed array",
                {**valid, "removed_filename_patterns": {}},
                "removed_filename_patterns must be a JSON array",
            ),
            (
                "removed object",
                {**valid, "removed_filename_patterns": ["bad"]},
                "JSON object",
            ),
            (
                "removed value",
                {
                    **valid,
                    "removed_filename_patterns": [
                        {"binding": "", "pattern": "old", "profile": "profile-one"}
                    ],
                },
                "binding must be a non-empty string",
            ),
            (
                "duplicate removed receipt",
                {
                    **valid,
                    "removed_filename_patterns": [
                        {
                            "binding": "binding-one",
                            "pattern": "old",
                            "profile": "profile-one",
                        },
                        {
                            "binding": "binding-one",
                            "pattern": "old",
                            "profile": "profile-one",
                        },
                    ],
                },
                "Duplicate removed filename pattern receipt",
            ),
            (
                "replaced array",
                {**valid, "replaced_filename_patterns": {}},
                "replaced_filename_patterns must be a JSON array",
            ),
            (
                "replaced object",
                {**valid, "replaced_filename_patterns": ["bad"]},
                "JSON object",
            ),
            (
                "replaced value",
                {
                    **valid,
                    "replaced_filename_patterns": [
                        {
                            "binding": "binding-one",
                            "old_pattern": "",
                            "new_pattern": "new",
                            "profile": "profile-one",
                        }
                    ],
                },
                "old_pattern must be a non-empty string",
            ),
            (
                "duplicate replaced receipt",
                {
                    **valid,
                    "replaced_filename_patterns": [
                        {
                            "binding": "binding-one",
                            "old_pattern": "old",
                            "new_pattern": "new",
                            "profile": "profile-one",
                        },
                        {
                            "binding": "binding-one",
                            "old_pattern": "old",
                            "new_pattern": "new",
                            "profile": "profile-one",
                        },
                    ],
                },
                "Duplicate replaced filename pattern receipt",
            ),
            (
                "filename object",
                {"account_bindings": [_binding()], "filename_patterns": ["bad"]},
                "JSON object",
            ),
            (
                "filename field",
                {
                    "account_bindings": [_binding()],
                    "filename_patterns": [{"pattern": "", "profile": "profile-one"}],
                },
                "pattern must be a non-empty string",
            ),
            (
                "duplicate filename",
                {
                    "account_bindings": [_binding()],
                    "filename_patterns": [
                        {"pattern": "same", "profile": "profile-one"},
                        {"pattern": "same", "profile": "profile-one"},
                    ],
                },
                "Duplicate filename mapping pattern",
            ),
            (
                "filename binding",
                {
                    "account_bindings": [_binding()],
                    "filename_patterns": [
                        {"pattern": "same", "profile": "profile-one", "binding": ""}
                    ],
                },
                "binding must be a non-empty string",
            ),
            (
                "unknown mapping binding",
                {
                    "account_bindings": [_binding()],
                    "filename_patterns": [
                        {
                            "pattern": "same",
                            "profile": "profile-one",
                            "binding": "missing",
                        }
                    ],
                },
                "Unknown account binding",
            ),
            (
                "mismatched mapping profile",
                {
                    "account_bindings": [_binding()],
                    "filename_patterns": [
                        {
                            "pattern": "same",
                            "profile": "profile-two",
                            "binding": "binding-one",
                        }
                    ],
                },
                "requires profile",
            ),
        ]
        for name, document, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    validate_profile_mappings(copy.deepcopy(document), _config())

    def test_binding_selection_and_profile_shapes_cover_dynamic_accounts(self) -> None:
        self.assertEqual(
            static_profile_account_ids(
                {
                    "pdf": {
                        "word_rows": "sectioned",
                        "sectioned_word_rows": {
                            "accounts": {
                                "One": {"account_id": "one"},
                                "Two": {"account_id": "two"},
                            }
                        },
                    }
                }
            ),
            {"one", "two"},
        )
        self.assertIsNone(
            static_profile_account_ids(
                {
                    "account_id": "fallback",
                    "csv": {"columns": {"account_id": "Account"}},
                }
            )
        )
        self.assertEqual(
            static_profile_account_ids({"account_id": "fallback"}), {"fallback"}
        )

        validate_bindings_for_profiles({"account_bindings": "not-a-list"}, [])
        validate_bindings_for_profiles({"account_bindings": ["not-a-binding"]}, [])
        with self.assertRaisesRegex(ValueError, "uses unknown profile"):
            validate_bindings_for_profiles(_mappings(), [])
        validate_bindings_for_profiles(
            _mappings(),
            [
                {
                    "id": "profile-one",
                    "account_id": "source-account",
                    "csv": {"columns": {"account_id": "Account"}},
                }
            ],
        )
        with self.assertRaisesRegex(
            ValueError, "missing other; unknown source-account"
        ):
            validate_bindings_for_profiles(
                _mappings(),
                [{"id": "profile-one", "account_id": "other"}],
            )
        validate_bindings_for_profiles(
            _mappings(),
            [{"id": "profile-one", "account_id": "source-account"}],
        )

        source = Path("source-may.csv")
        self.assertIsNone(matching_filename_mapping(source, {}))
        with self.assertRaisesRegex(AccountBindingError, "Conflicting filename"):
            matching_filename_mapping(
                source,
                {
                    "filename_patterns": [
                        {"pattern": "source-*.csv", "profile": "profile-one"},
                        {"pattern": "*.csv", "profile": "profile-two"},
                    ]
                },
            )
        self.assertIsNone(
            binding_for_source(
                source,
                {"id": "profile-one"},
                {
                    "filename_patterns": [
                        {"pattern": "source-*.csv", "profile": "profile-one"}
                    ]
                },
            )
        )
        self.assertIsNone(binding_for_source(source, {"id": "other"}, _mappings()))
        self.assertEqual(
            binding_for_source(source, {"id": "profile-one"}, _mappings())["id"],
            "binding-one",
        )
        with self.assertRaisesRegex(AccountBindingError, "Unknown account binding"):
            binding_for_source(
                source,
                {"id": "profile-one"},
                {
                    "filename_patterns": [
                        {
                            "pattern": "source-*.csv",
                            "profile": "profile-one",
                            "binding": "missing",
                        }
                    ]
                },
            )
        trimmed = _mappings()
        trimmed["account_bindings"] = [
            {
                **_binding(),
                "id": " binding-one ",
                "profile": " profile-one ",
                "owner": " Justin ",
                "accounts": [
                    {
                        "source_account_id": " source-account ",
                        "account_id": " justin-account ",
                        "account": " Justin account ",
                    }
                ],
            }
        ]
        self.assertEqual(binding_by_id(trimmed, "binding-one")["owner"], "Justin")
        with self.assertRaisesRegex(AccountBindingError, "Unknown account binding"):
            binding_by_id(_mappings(), "missing")

    def test_apply_and_project_bound_owners_without_filename_guessing(self) -> None:
        binding = _binding()
        row = {"account_id": "source-account", "account": "Raw", "owner": "Household"}
        apply_binding([row], None)
        self.assertEqual(row["owner"], "Household")
        with self.assertRaisesRegex(
            AccountBindingError, "does not cover 2 emitted account ids"
        ):
            apply_binding(
                [{"account_id": "unknown-one"}, {"account_id": "unknown-two"}], binding
            )
        apply_binding([row], binding)
        self.assertEqual(row["account_id"], "justin-account")
        self.assertEqual(row["owner"], "Justin")
        self.assertEqual(row["account_binding_id"], "binding-one")

        source_rows = [
            {
                "transaction_id": "source-direct",
                "_honeymoney_bound_owner": "Justin",
            },
            {
                "transaction_id": "source-saved",
                "account_id": "justin-account",
                "source_file": "saved.csv",
            },
        ]
        saved_mapping = {
            "filename_patterns": [
                {
                    "pattern": "saved.csv",
                    "profile": "profile-one",
                    "binding": "binding-one",
                }
            ],
            "account_bindings": [binding],
        }
        updates = canonical_bound_owners(
            source_rows,
            [
                {
                    "source_occurrence_pools": [["source-direct"]],
                    "canonical_transaction_ids": ["canonical-direct"],
                },
                {
                    "source_occurrence_pools": [["source-saved"]],
                    "canonical_transaction_ids": ["canonical-saved"],
                },
                {"source_occurrence_pools": "invalid", "canonical_transaction_ids": []},
            ],
            saved_mapping,
        )
        self.assertEqual(
            updates,
            {"canonical-direct": "Justin", "canonical-saved": "Justin"},
        )
        with self.assertRaisesRegex(
            AccountBindingError, "Conflicting account binding owners"
        ):
            canonical_bound_owners(
                [
                    {"transaction_id": "one", "_honeymoney_bound_owner": "Justin"},
                    {"transaction_id": "two", "_honeymoney_bound_owner": "Franchesca"},
                ],
                [
                    {
                        "source_occurrence_pools": [["one", "two"]],
                        "canonical_transaction_ids": ["canonical"],
                    }
                ],
                {},
            )
        canonical_rows = [
            {"transaction_id": "canonical-direct", "owner": "Household"},
            {"transaction_id": "unbound", "owner": "Household"},
        ]
        enforce_bound_owners(canonical_rows, updates)
        self.assertEqual(canonical_rows[0]["owner"], "Justin")
        self.assertEqual(canonical_rows[1]["owner"], "Household")
        self.assertEqual(
            normalized_account_id("  Cafe\u0301   ACCOUNT  "), "café account"
        )
        self.assertEqual(
            binding_views(
                {
                    "filename_patterns": [
                        "ignored",
                        {"binding": "binding-one", "pattern": "z-*.csv"},
                        {"binding": "binding-one", "pattern": "a-*.csv"},
                    ],
                    "account_bindings": ["ignored", binding],
                }
            )[0]["patterns"],
            ["a-*.csv", "z-*.csv"],
        )


class ImporterCoverageTest(unittest.TestCase):
    def test_snapshot_discovery_and_cached_extraction_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            csv_path = root / "input.csv"
            csv_path.write_text(
                "Date,Description,Amount\n2026-01-01,SYNTHETIC,1\n", encoding="utf-8"
            )
            pdf_path = root / "input.PDF"
            pdf_path.write_bytes(b"synthetic pdf")
            (root / "ignored.txt").write_text("ignored", encoding="utf-8")
            outside_root = Path(tempfile.mkdtemp())
            outside = outside_root / "outside.csv"
            outside.write_text("Date\n", encoding="utf-8")
            try:
                self.assertEqual(
                    importers._relative_source(csv_path, root), "input.csv"
                )
                self.assertEqual(
                    importers._relative_source(outside, root), "outside.csv"
                )
                source_bytes, resolved = importers._read_stable_source_bytes(
                    csv_path, max_input_bytes=1024, input_kind="CSV"
                )
                self.assertTrue(source_bytes.startswith(b"Date,"))
                self.assertEqual(resolved, csv_path.resolve())
                with self.assertRaisesRegex(ValueError, "CSV input exceeds 2 bytes"):
                    importers._read_stable_source_bytes(
                        csv_path, max_input_bytes=2, input_kind="CSV"
                    )
                snapshot = importers._capture_input_source(
                    csv_path, {"_identity_workspace_root": root}
                )
                self.assertEqual(snapshot.locator_kind, "workspace")
                self.assertEqual(snapshot.locator, "input.csv")
                self.assertEqual(importers._source_text(csv_path, b"a,b\n"), "a,b\n")
                self.assertEqual(
                    importers._csv_headers(csv_path, source_bytes=b""), set()
                )
                self.assertEqual(importers._discover_input_files(csv_path), [csv_path])
                self.assertEqual(
                    importers._discover_input_files(root), sorted([csv_path, pdf_path])
                )
                self.assertEqual(importers._discover_input_files(root / "missing"), [])
                stream = importers._PdfSnapshotStream(b"pdf", pdf_path)
                self.assertEqual(os.fspath(stream), str(pdf_path))
            finally:
                for child in outside_root.iterdir():
                    child.unlink()
                outside_root.rmdir()

        budget = importers._PdfImportBudget()
        budget.record_extraction({"items": ["one", ("two",)]})
        self.assertEqual(budget.extracted_text_chars, 6)
        budget.extracted_text_chars = importers.MAX_PDF_EXTRACTED_TEXT_CHARS
        with self.assertRaisesRegex(ValueError, "PDF extracted text exceeds"):
            budget.record_text_chars(1)
        budget.transaction_rows = importers.MAX_PDF_TRANSACTION_ROWS
        with self.assertRaisesRegex(ValueError, "PDF transaction rows exceed"):
            budget.record_transaction()

        class Page:
            label = "synthetic"

            def __init__(self) -> None:
                self.calls = 0

            def extract_words(self, **kwargs: object) -> list[dict[str, str]]:
                self.calls += 1
                return [{"text": str(kwargs.get("label", "word"))}]

        page = Page()
        cached = importers._CachedPdfPage(page, importers._PdfImportBudget())
        self.assertEqual(cached.label, "synthetic")
        self.assertEqual(cached.extract_words(label="word"), [{"text": "word"}])
        self.assertEqual(cached.extract_words(label="word"), [{"text": "word"}])
        self.assertEqual(page.calls, 1)

    def test_profile_preview_selection_and_mapping_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            csv_path = root / "source.csv"
            csv_path.write_text(
                "Date,Description,Amount\n2026-01-01,SYNTHETIC,1\n", encoding="utf-8"
            )
            pdf_path = root / "source.pdf"
            pdf_path.write_bytes(b"synthetic")
            text_path = root / "source.txt"
            text_path.write_text("synthetic", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not define csv"):
                importers.preview_profile_input({}, "profile", csv_path, _config())
            with self.assertRaisesRegex(ValueError, "does not define pdf"):
                importers.preview_profile_input({}, "profile", pdf_path, _config())
            with self.assertRaisesRegex(ValueError, "Unsupported preview input type"):
                importers.preview_profile_input({}, "profile", text_path, _config())
            rows, warnings = importers.preview_profile_input(
                _csv_profile(), "profile-one", csv_path, _config()
            )
            self.assertEqual(warnings, [])
            self.assertEqual(rows[0]["original_description"], "SYNTHETIC")

            first = _csv_profile("first")
            second = _csv_profile("second")
            first["csv"] = {"detect_headers": ["Date"]}
            second["csv"] = {"detect_headers": ["Unmatched"]}
            self.assertEqual(importers._pdf_adapter_tag(_pdf_profile()), 2)
            word_profile = _pdf_profile()
            word_profile["pdf"] = {"word_rows": True}
            self.assertEqual(importers._pdf_adapter_tag(word_profile), 3)
            word_profile["pdf"] = {"word_rows": "sectioned"}
            self.assertEqual(importers._pdf_adapter_tag(word_profile), 4)
            self.assertEqual(
                importers._select_pdf_profile(
                    pdf_path, [], False, {}, None, lambda: None
                )["account_type"],
                "unknown",
            )
            mapped = {
                "filename_patterns": [{"pattern": "source.pdf", "profile": "first"}]
            }
            self.assertIs(
                importers._select_pdf_profile(
                    pdf_path, [first, second], False, mapped, None, lambda: None
                ),
                first,
            )
            self.assertIs(
                importers._select_pdf_profile(
                    pdf_path, [first], False, {}, None, lambda: None
                ),
                first,
            )
            with self.assertRaisesRegex(ValueError, "Could not detect profile"):
                importers._select_pdf_profile(
                    pdf_path, [first, second], False, {}, None, lambda: None
                )
            with patch.object(importers, "_prompt_for_profile", return_value=second):
                self.assertIs(
                    importers._select_pdf_profile(
                        pdf_path, [first, second], True, {}, None, lambda: None
                    ),
                    second,
                )

            self.assertEqual(
                importers._select_csv_profile(
                    csv_path, [], False, {}, lambda: None, source_bytes=b"Date\n"
                )[1],
                False,
            )
            mapped_csv = {
                "filename_patterns": [{"pattern": "source.csv", "profile": "first"}]
            }
            self.assertIs(
                importers._select_csv_profile(
                    csv_path, [first, second], False, mapped_csv, lambda: None
                )[0],
                first,
            )
            self.assertIs(
                importers._select_csv_profile(
                    csv_path,
                    [first, second],
                    False,
                    {},
                    lambda: None,
                    source_bytes=b"Date,Description,Amount\n",
                )[0],
                first,
            )
            ambiguous_one = _csv_profile("ambiguous-one")
            ambiguous_two = _csv_profile("ambiguous-two")
            ambiguous_one["csv"] = {"detect_headers": ["Date"]}
            ambiguous_two["csv"] = {"detect_headers": ["Date"]}
            with self.assertRaisesRegex(ValueError, "Ambiguous profile detection"):
                importers._select_csv_profile(
                    csv_path,
                    [ambiguous_one, ambiguous_two],
                    False,
                    {},
                    lambda: None,
                    source_bytes=b"Date\n",
                )
            with patch.object(
                importers, "_prompt_for_profile", return_value=ambiguous_two
            ):
                selected, prompted = importers._select_csv_profile(
                    csv_path,
                    [ambiguous_one, ambiguous_two],
                    True,
                    {},
                    lambda: None,
                    source_bytes=b"Date\n",
                )
            self.assertIs(selected, ambiguous_two)
            self.assertTrue(prompted)
            with self.assertRaisesRegex(ValueError, "Could not detect profile"):
                importers._select_csv_profile(
                    csv_path,
                    [first, second],
                    False,
                    {},
                    lambda: None,
                    source_bytes=b"Other\n",
                )
            with patch.object(importers, "_prompt_for_profile", return_value=second):
                selected, prompted = importers._select_csv_profile(
                    csv_path,
                    [first, second],
                    True,
                    {},
                    lambda: None,
                    source_bytes=b"Other\n",
                )
            self.assertIs(selected, second)
            self.assertTrue(prompted)
            self.assertIs(
                importers._select_csv_profile(
                    csv_path, [first], False, {}, lambda: None, source_bytes=b"Other\n"
                )[0],
                first,
            )
            self.assertIsNone(importers._mapped_profile(csv_path, [first], mapped))

            with (
                patch("builtins.input", side_effect=["not-a-number", "9", "2"]),
                patch("builtins.print"),
            ):
                self.assertIs(
                    importers._prompt_for_profile(
                        csv_path, [first, second], None, lambda: None
                    ),
                    second,
                )
            importers._maybe_save_profile_mapping(csv_path, first, None)
            remembered = root / "nested" / "profile-mappings.json"
            with patch("builtins.input", return_value="no"):
                importers._maybe_save_profile_mapping(csv_path, first, str(remembered))
            self.assertFalse(remembered.exists())
            with patch("builtins.input", return_value="yes"):
                importers._maybe_save_profile_mapping(csv_path, first, str(remembered))
            self.assertEqual(
                json.loads(remembered.read_text(encoding="utf-8"))["filename_patterns"][
                    0
                ]["profile"],
                "first",
            )

    def test_profile_validation_csv_import_and_profile_mapping_errors(self) -> None:
        profile = _csv_profile()
        importers._validate_profile(
            copy.deepcopy(profile), Path("profile.json"), _config()
        )
        cases: list[tuple[str, object, str]] = [
            ("document", [], "must be a JSON object"),
            ("id", {**profile, "id": ""}, "profile.id"),
            ("account", {**profile, "account_id": ""}, "account_id"),
            ("required", {**profile, "country": ""}, "profile.country"),
            ("owner", {**profile, "owner": "Other"}, "Unsupported owner"),
            (
                "payment",
                {**profile, "payment_method": "Cash"},
                "Unsupported payment_method",
            ),
            (
                "account type",
                {**profile, "account_type": "wallet"},
                "Unsupported account_type",
            ),
            ("parser count", {**profile, "pdf": {}}, "exactly one"),
            ("date array", {**profile, "date_formats": []}, "date_formats"),
            ("date value", {**profile, "date_formats": [""]}, "date_formats"),
            (
                "date contents",
                {**profile, "date_formats": ["%Y"]},
                "include day and month",
            ),
            (
                "yearless",
                {**profile, "date_formats": ["%m-%d"]},
                "requires profile.statement_year",
            ),
            ("year", {**profile, "statement_year": True}, "statement_year"),
            ("settings", {**profile, "csv": []}, "profile.csv must be a JSON object"),
            (
                "columns",
                {**profile, "csv": {"columns": []}},
                "csv.columns must be a JSON object",
            ),
            (
                "column name",
                {**profile, "csv": {"columns": {"": "Date"}}},
                "columns keys",
            ),
            (
                "column value",
                {**profile, "csv": {"columns": {"transaction_date": True}}},
                "must be a non-empty string or column index",
            ),
            (
                "dates",
                {
                    **profile,
                    "csv": {
                        "columns": {"description": "Description", "amount": "Amount"}
                    },
                },
                "transaction_date or posting_date",
            ),
            (
                "description",
                {
                    **profile,
                    "csv": {
                        "columns": {"transaction_date": "Date", "amount": "Amount"}
                    },
                },
                "columns.description",
            ),
            (
                "amount missing",
                {
                    **profile,
                    "csv": {
                        "columns": {
                            "transaction_date": "Date",
                            "description": "Description",
                        }
                    },
                },
                "amount strategy",
            ),
            (
                "amount conflicting",
                {
                    **profile,
                    "csv": {
                        "columns": {
                            "transaction_date": "Date",
                            "description": "Description",
                            "amount": "Amount",
                            "debit": "Debit",
                        }
                    },
                },
                "exactly one amount strategy",
            ),
            (
                "amount incomplete",
                {
                    **profile,
                    "csv": {
                        "columns": {
                            "transaction_date": "Date",
                            "description": "Description",
                            "debit": "Debit",
                        }
                    },
                },
                "requires both",
            ),
            (
                "default sign",
                {
                    **profile,
                    "csv": {**profile["csv"], "amount_default_sign": "sideways"},
                },
                "amount_default_sign",
            ),
            (
                "indicator values",
                {
                    **profile,
                    "csv": {
                        **profile["csv"],
                        "columns": {
                            **profile["csv"]["columns"],
                            "credit_debit": "Type",
                        },
                    },
                },
                "requires debit_values",
            ),
            (
                "orphan signs",
                {**profile, "csv": {**profile["csv"], "debit_values": ["debit"]}},
                "require columns.credit_debit",
            ),
            (
                "csv index",
                {
                    **profile,
                    "csv": {
                        **profile["csv"],
                        "columns": {**profile["csv"]["columns"], "amount": 2},
                    },
                },
                "csv.columns.amount",
            ),
            (
                "headers",
                {**profile, "csv": {**profile["csv"], "detect_headers": []}},
                "csv.detect_headers",
            ),
        ]
        for name, candidate, message in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, message):
                    importers._validate_profile(
                        copy.deepcopy(candidate), Path("profile.json"), _config()
                    )

        pdf_cases: list[tuple[str, dict[str, object], str]] = [
            ("parser", {"parser": "other", "columns": {}}, "pdf.parser"),
            ("boolean", {"has_header": "yes", "columns": {}}, "pdf.has_header"),
            ("regex empty", {"row_regex": "", "columns": {}}, "pdf.row_regex"),
            (
                "regex invalid",
                {"row_regex": "[", "columns": {}},
                "valid regular expression",
            ),
            ("word rows", {"word_rows": "other", "columns": {}}, "pdf.word_rows"),
            (
                "word bounds",
                {"word_rows": True, "word_columns": {}, "columns": {}},
                "pdf.word_columns",
            ),
            (
                "join fields",
                {"row_regex": "(?P<Date>.*)", "join_fields": [], "columns": {}},
                "pdf.join_fields",
            ),
        ]
        for name, settings, message in pdf_cases:
            with self.subTest(pdf=name):
                with self.assertRaisesRegex(ValueError, message):
                    importers._validate_pdf_profile("profile", settings)
        with self.assertRaisesRegex(ValueError, "two increasing numbers"):
            importers._validate_pdf_bounds_map("profile", "pdf.bounds", {"x": [2, 1]})
        with self.assertRaisesRegex(ValueError, "sectioned_word_rows"):
            importers._validate_sectioned_pdf_profile("profile", [])
        with self.assertRaisesRegex(ValueError, "balance_mappings"):
            importers._validate_pdf_balance_mappings(
                "profile", {"balance_mappings": []}
            )

        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            empty = root / "empty.csv"
            empty.write_text("", encoding="utf-8")
            with patch.object(importers, "_validate_selected_csv_headers"):
                imported = importers._import_csv(
                    empty, profile, _config(), root, include_identity_records=True
                )
            self.assertEqual(imported, ([], ()))
            csv_path = root / "source.csv"
            csv_path.write_text(
                "Date,Description,Amount\n"
                "2026-01-01,OPENING BALANCE,10\n"
                "2026-01-02,SYNTHETIC,5\n",
                encoding="utf-8",
            )
            rows, records = importers._import_csv(
                csv_path, profile, _config(), root, include_identity_records=True
            )
            self.assertEqual(
                [row["original_description"] for row in rows], ["SYNTHETIC"]
            )
            self.assertEqual(records[0].locator.components, (3,))
            with self.assertRaisesRegex(ValueError, "maps to missing header"):
                importers._validate_selected_csv_headers(
                    csv_path,
                    profile,
                    {"columns": {"merchant": "Missing"}},
                )
            mappings_path = root / "mappings.json"
            self.assertEqual(importers._load_profile_mappings({}), {})
            self.assertEqual(
                importers._load_profile_mappings(
                    {"profile_mappings": str(mappings_path)}
                ),
                {},
            )
            mappings_path.write_text(json.dumps(_mappings()), encoding="utf-8")
            self.assertEqual(
                importers._load_profile_mappings(
                    {"profile_mappings": str(mappings_path), **_config()}
                )["account_bindings"][0]["id"],
                "binding-one",
            )
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            self.assertEqual(
                importers._load_profiles(
                    {"profiles": [str(profile_path)], **_config()}
                )[0]["id"],
                "profile-one",
            )

    def test_import_transaction_paths_and_identity_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            disabled_pdf = root / "disabled.pdf"
            failed_pdf = root / "failed.pdf"
            broken_pdf = root / "broken.pdf"
            processed_pdf = root / "processed.pdf"
            csv_path = root / "processed.csv"
            ignored = root / "ignored.txt"
            for path in (
                disabled_pdf,
                failed_pdf,
                broken_pdf,
                processed_pdf,
                csv_path,
                ignored,
            ):
                path.write_bytes(b"synthetic")
            profile = _csv_profile()
            pdf_profile = _pdf_profile()
            row = {"transaction_id": "", "account_id": "source-account"}

            _, warnings, reports = importers._import_transactions(
                [disabled_pdf], [], {"pdf": {"enabled": False}}, root, False, {}, None
            )
            self.assertEqual(reports[0]["status"], "skipped")
            self.assertIn("PDF parsing disabled", warnings[0])

            with (
                patch.object(
                    importers, "_select_pdf_profile", return_value=pdf_profile
                ),
                patch.object(importers, "_import_pdf", side_effect=ImportError),
            ):
                _, warnings, reports = importers._import_transactions(
                    [failed_pdf], [pdf_profile], _config(), root, False, {}, None
                )
            self.assertEqual(reports[0]["status"], "failed")
            self.assertIn("requires pdfplumber", warnings[0])

            with (
                patch.object(
                    importers, "_select_pdf_profile", return_value=pdf_profile
                ),
                patch.object(
                    importers,
                    "_import_pdf",
                    side_effect=ValueError("synthetic failure"),
                ),
            ):
                _, warnings, reports = importers._import_transactions(
                    [broken_pdf], [pdf_profile], _config(), root, False, {}, None
                )
            self.assertEqual(reports[0]["status"], "failed")
            self.assertIn("synthetic failure", warnings[0])

            with (
                patch.object(
                    importers, "_select_pdf_profile", return_value=pdf_profile
                ),
                patch.object(
                    importers,
                    "_import_pdf",
                    return_value=([dict(row)], ["pdf warning"]),
                ),
                patch.object(
                    importers,
                    "_apply_source_account_binding",
                    return_value={"binding_id": "bound"},
                ),
            ):
                transactions, warnings, reports = importers._import_transactions(
                    [processed_pdf, ignored],
                    [pdf_profile],
                    _config(),
                    root,
                    False,
                    {},
                    None,
                )
            self.assertEqual(transactions, [row])
            self.assertEqual(warnings, ["pdf warning"])
            self.assertEqual(reports[0]["parser"], "pdfplumber")
            self.assertEqual(len(reports), 1)

            with (
                patch.object(
                    importers, "_select_csv_profile", return_value=(profile, True)
                ),
                patch.object(importers, "_import_csv", return_value=[dict(row)]),
                patch.object(
                    importers,
                    "_apply_source_account_binding",
                    return_value={"binding_id": "bound"},
                ),
            ):
                transactions, warnings, reports = importers._import_transactions(
                    [csv_path], [profile], _config(), root, False, {}, None
                )
            self.assertEqual(transactions, [row])
            self.assertEqual(warnings, [])
            self.assertEqual(reports[0]["binding_id"], "bound")

            binding = _binding()
            with (
                patch.object(importers, "_import_csv", return_value=[dict(row)]),
                patch.object(
                    importers,
                    "_apply_source_account_binding",
                    return_value={
                        "binding_id": "binding-one",
                        "binding_selection": "explicit",
                    },
                ),
            ):
                _, _, reports = importers._import_transactions(
                    [csv_path],
                    [profile],
                    _config(),
                    root,
                    False,
                    {},
                    None,
                    explicit_binding=binding,
                )
            self.assertEqual(reports[0]["binding_selection"], "explicit")

            snapshot = importers._InputSourceSnapshot(
                b"synthetic", csv_path.resolve(), "workspace", "processed.csv"
            )
            source_identity = SimpleNamespace(name="identity")
            with (
                patch.object(importers, "_capture_input_source", return_value=snapshot),
                patch.object(
                    importers, "_select_csv_profile", return_value=(profile, False)
                ),
                patch.object(importers, "_import_csv", return_value=([dict(row)], ())),
                patch.object(
                    importers, "_apply_source_account_binding", return_value={}
                ),
                patch.object(
                    importers, "_incoming_source_identity", return_value=source_identity
                ),
            ):
                result = importers._import_transactions(
                    [csv_path],
                    [profile],
                    _config(),
                    root,
                    False,
                    {},
                    None,
                    include_identity_sources=True,
                )
            self.assertEqual(result[3], (source_identity,))

            with self.assertRaisesRegex(AccountBindingError, "uses unknown profile"):
                importers._explicit_binding_profile(csv_path, [], binding)
            with self.assertRaisesRegex(AccountBindingError, "does not support .pdf"):
                importers._explicit_binding_profile(processed_pdf, [profile], binding)
            self.assertIs(
                importers._explicit_binding_profile(csv_path, [profile], binding),
                profile,
            )

            direct_identity = importers._incoming_source_identity(
                csv_path, profile, _config(), 1, (), root, snapshot
            )
            self.assertEqual(direct_identity.source_display, "processed.csv")
            candidates = importers._candidate_source_ids(
                [csv_path], root, {"_identity_workspace_root": root}
            )
            self.assertTrue(candidates["processed.csv"].startswith("src_"))
            diagnostic = SimpleNamespace(
                code="synthetic",
                source_display="source.csv",
                action="retain",
                remediation="fix it",
                affected_count=2,
            )
            self.assertIn("count=2", importers._identity_diagnostic_warning(diagnostic))
            fallback = SimpleNamespace(
                code="synthetic",
                source_display="source.csv",
                action="retain",
                remediation="fix it",
                candidate_count=3,
            )
            self.assertIn("count=3", importers._identity_diagnostic_warning(fallback))

    def test_pdf_balance_and_row_helpers_cover_safe_fallbacks(self) -> None:
        self.assertEqual(
            importers._skip_descriptions({"skip_descriptions": ["", " Fee "]}),
            [" fee "],
        )
        self.assertTrue(
            importers._row_is_skipped({"original_description": "Closing balance"}, [])
        )
        self.assertTrue(importers._row_is_skipped({"merchant": "Monthly fee"}, ["fee"]))
        self.assertFalse(importers._row_is_skipped({"merchant": "Purchase"}, []))
        self.assertEqual(
            importers._strict_pdf_balance("1,234.50", "CR"), Decimal("1234.50")
        )
        self.assertEqual(importers._strict_pdf_balance("1.00", "DR"), Decimal("-1.00"))
        with self.assertRaisesRegex(ValueError, "invalid balance"):
            importers._strict_pdf_balance("NaN")
        self.assertIsNone(
            importers._single_pdf_balance_per_page(
                [
                    importers._PdfBalanceCandidate(1, 1, Decimal("1")),
                    importers._PdfBalanceCandidate(1, 2, Decimal("2")),
                ]
            )
        )


class NormalizationCoverageTest(unittest.TestCase):
    def test_normalization_helpers_cover_invalid_and_signed_inputs(self) -> None:
        self.assertEqual(normalization._default_profile()["account_type"], "unknown")
        self.assertEqual(
            normalization._optional_decimal_value({"value": ""}, "value"), ""
        )
        self.assertEqual(
            normalization._optional_decimal_value({"value": "bad"}, "value"), ""
        )
        self.assertEqual(
            normalization._optional_decimal_value({"value": "NaN"}, "value"), ""
        )
        self.assertEqual(
            normalization._optional_decimal_value({"value": "1,234.5"}, "value"),
            "1234.50",
        )
        self.assertFalse(normalization._date_format_has_year("%"))
        self.assertFalse(normalization._date_format_has_year("%%Y"))
        self.assertTrue(normalization._date_format_has_year("%y-%m-%d"))
        with self.assertRaisesRegex(ValueError, "requires a fallback year"):
            normalization._parse_profile_date("01-02", "%m-%d")
        self.assertEqual(
            normalization._parse_profile_date("01-02", "%m-%d", fallback_year=2026)
            .date()
            .isoformat(),
            "2026-01-02",
        )
        self.assertEqual(normalization._normalize_date("", {}), "")
        self.assertEqual(
            normalization._normalize_date("2026-01-02", {"date_formats": []}),
            "2026-01-02",
        )
        self.assertEqual(normalization._normalize_date("not-a-date", {}), "not-a-date")

        invalid: list[str] = []
        self.assertEqual(
            normalization._signed_amount(
                {"Debit": "", "Credit": ""},
                {"debit": "Debit", "credit": "Credit"},
                invalid,
            ),
            Decimal("0"),
        )
        self.assertEqual(invalid, ["Debit", "Credit"])
        invalid = []
        self.assertEqual(
            normalization._signed_amount(
                {"Debit": "2", "Credit": "1"},
                {"debit": "Debit", "credit": "Credit"},
                invalid,
            ),
            Decimal("-2"),
        )
        self.assertEqual(invalid, ["Debit", "Credit"])
        self.assertEqual(
            normalization._signed_amount(
                {"Debit": "2", "Credit": ""}, {"debit": "Debit", "credit": "Credit"}, []
            ),
            Decimal("-2"),
        )
        self.assertEqual(
            normalization._signed_amount(
                {"Debit": "", "Credit": "2"}, {"debit": "Debit", "credit": "Credit"}, []
            ),
            Decimal("2"),
        )
        self.assertEqual(
            normalization._posted_amount(
                {"Posted": ""}, {"posted_amount": "Posted"}, Decimal("4"), []
            ),
            Decimal("0"),
        )
        self.assertEqual(
            normalization._apply_amount_sign(
                "2",
                Decimal("2"),
                {"Type": "debit"},
                {"credit_debit": "Type", "debit_values": ["debit"]},
            ),
            Decimal("-2"),
        )
        self.assertEqual(
            normalization._apply_amount_sign(
                "2",
                Decimal("-2"),
                {"Type": "credit"},
                {"credit_debit": "Type", "credit_values": ["credit"]},
            ),
            Decimal("2"),
        )
        self.assertEqual(
            normalization._apply_amount_sign("2 DR", Decimal("-2"), {}, {}),
            Decimal("-2"),
        )
        self.assertEqual(
            normalization._apply_amount_sign(
                "2", Decimal("2"), {}, {"amount_default_sign": "expense"}
            ),
            Decimal("-2"),
        )
        self.assertEqual(
            normalization._apply_amount_sign(
                "2", Decimal("-2"), {}, {"amount_default_sign": "income"}
            ),
            Decimal("2"),
        )

        self.assertEqual(
            normalization._amount_hkd(Decimal("2"), "HKD", {}), (Decimal("2"), [], "")
        )
        self.assertEqual(
            normalization._amount_hkd(Decimal("2"), "USD", {"exchange_rates": []}),
            (None, ["missing_exchange_rate"], "Missing exchange rate for USD"),
        )
        self.assertEqual(
            normalization._amount_hkd(
                Decimal("2"), "USD", {"exchange_rates": {"USD": "7.8"}}
            ),
            (Decimal("15.6"), [], ""),
        )
        invalid = []
        self.assertEqual(
            normalization._parse_decimal("", invalid, "Amount", blank_is_invalid=True),
            Decimal("0"),
        )
        self.assertEqual(normalization._parse_decimal("4 CR"), Decimal("4"))
        self.assertEqual(normalization._parse_decimal("4 dr"), Decimal("-4"))
        self.assertEqual(
            normalization._parse_decimal("bad", invalid, "Amount"), Decimal("0")
        )
        self.assertEqual(
            normalization._parse_decimal("Infinity", invalid, "Amount"), Decimal("0")
        )
        self.assertEqual(invalid, ["Amount", "Amount", "Amount"])
        self.assertIsNone(normalization._parse_iso_date("bad"))
        self.assertEqual(
            normalization._parse_iso_date("2026-01-02").isoformat(), "2026-01-02"
        )
        self.assertEqual(normalization._append_reason("", "first"), "first")
        self.assertEqual(normalization._append_reason("first", "first"), "first")
        self.assertEqual(
            normalization._append_reason("first", "second"), "first; second"
        )
        self.assertEqual(normalization._unique(["one", "two", "one"]), ["one", "two"])
        self.assertEqual(normalization._remove_flag("one;two;one", "one"), "two")


if __name__ == "__main__":
    unittest.main()
