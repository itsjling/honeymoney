"""Direct synthetic coverage for domain logic used by workspace derivation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from honeymoney.account_bindings import (
    AccountBindingError,
    apply_binding,
    binding_by_id,
    binding_for_source,
    binding_views,
    canonical_bound_owners,
    enforce_bound_owners,
    matching_filename_mapping,
    remove_binding_pattern,
    replace_binding_pattern,
    upsert_binding,
    validate_bindings_for_profiles,
    validate_profile_mappings,
)
from honeymoney.corrections import (
    apply_corrections,
    load_corrections,
    prepare_corrections_document,
    review_state_correction_updates,
    validate_correction,
)
from honeymoney.identity import empty_manifest, manifest_document, source_namespace_id
from honeymoney.identity_state import IdentityState
from honeymoney.learning import plan_learned_rules
from honeymoney.manual_pairs import (
    ManualPairError,
    manual_pair_id,
    manual_pair_marker,
    validate_manual_pair_facts,
    with_manual_pair_marker,
    without_manual_pair_marker,
)
from honeymoney.normalization import _normalized_row
from honeymoney.overlap import canonicalize_overlaps, empty_overlap_manifest
from honeymoney.provenance import safe_source_location
from honeymoney.source_data_review import (
    SourceDataReviewError,
    inspect_source_data_review,
    repair_source_data_review_state,
)
from honeymoney.valuation_inspection import (
    ValuationInspectionError,
    inspect_missing_valuations,
)


class WorkspaceDomainCoverageTest(unittest.TestCase):
    def _source_row(
        self,
        *,
        token: str,
        namespace_id: str,
        source_file: str,
        flags: str = "",
    ) -> dict[str, str]:
        return {
            "transaction_id": "txn_" + token * 32,
            "source_id": "src_" + token * 64,
            "source_namespace_id": namespace_id,
            "source_revision": "rev_" + token * 64,
            "source_record_id": "rec_" + token * 64,
            "date": "2026-05-04",
            "transaction_date": "2026-05-04",
            "posting_date": "",
            "account_id": "synthetic_account",
            "account": "Synthetic account",
            "account_type": "bank",
            "institution": "Synthetic Bank",
            "country": "HK",
            "original_amount": "-10.00",
            "original_currency": "EUR",
            "posted_amount": "-10.00",
            "posted_currency": "EUR",
            "amount_hkd": "",
            "valuation_source": "",
            "valuation_status": "",
            "valuation_rate_date": "",
            "valuation_provider": "",
            "statement_opening_balance": "",
            "statement_closing_balance": "",
            "statement_section": "Synthetic section",
            "merchant": "Synthetic merchant",
            "original_description": "SYNTHETIC PURCHASE",
            "category": "Unknown",
            "flow_type": "unresolved",
            "flow_source": "deterministic",
            "transfer_group_id": "",
            "paired_transaction_id": "",
            "reconciliation_status": "not_applicable",
            "reconciliation_confidence": "",
            "owner": "Household",
            "payment_method": "Bank Account",
            "confidence": "0.00",
            "needs_review": "true",
            "review_reasons": "category_decision;accounting_flow",
            "reason": "",
            "flags": flags,
            "notes": "",
            "source_file": source_file,
            "source_page": "1",
            "source_row": "2",
        }

    def _state(self, source_rows: list[dict[str, str]]) -> IdentityState:
        overlap = canonicalize_overlaps(
            source_rows,
            [],
            empty_overlap_manifest("ovns_" + "0" * 64),
        )
        manifest = empty_manifest()
        return IdentityState(
            rows=overlap.rows,
            manifest=manifest,
            manifest_document=manifest_document(manifest),
            source_rows=source_rows,
            source_evidence_rows=source_rows,
            overlap_manifest=overlap.manifest,
        )

    def test_normalization_preserves_signed_facts_and_marks_bad_split_amounts(
        self,
    ) -> None:
        profile = {
            "account_id": "synthetic_card",
            "account": "Synthetic card",
            "account_currency": "HKD",
            "owner": "Household",
            "payment_method": "Credit Card",
            "date_formats": ["%b %d"],
            "statement_year": 2026,
        }
        row = _normalized_row(
            {
                "Date": "May 04",
                "Post date": "May 05",
                "Description": "  SYNTHETIC\x00 CARD  ",
                "Merchant": "Synthetic merchant",
                "Amount": "8.50",
                "Direction": " debit ",
                "Original currency": "EUR",
                "Posted amount": "9.00",
                "Posted currency": "HKD",
            },
            2,
            profile,
            {"base_currency": "HKD"},
            {
                "transaction_date": "Date",
                "posting_date": "Post date",
                "description": "Description",
                "merchant": "Merchant",
                "amount": "Amount",
                "credit_debit": "Direction",
                "debit_values": ["debit"],
                "credit_values": ["credit"],
                "original_currency": "Original currency",
                "posted_amount": "Posted amount",
                "posted_currency": "Posted currency",
            },
            "synthetic.csv",
        )

        self.assertEqual(row["date"], "2026-05-04")
        self.assertEqual(row["posting_date"], "2026-05-05")
        self.assertEqual(row["original_amount"], "-8.50")
        self.assertEqual(row["posted_amount"], "-9.00")
        self.assertEqual(row["original_description"], "SYNTHETIC CARD")
        self.assertEqual(row["account_type"], "credit_card")

        invalid = _normalized_row(
            {
                "Date": "May 04",
                "Description": "SYNTHETIC SPLIT",
                "Debit": "10.00",
                "Credit": "1.00",
            },
            3,
            profile,
            {"base_currency": "HKD"},
            {
                "transaction_date": "Date",
                "description": "Description",
                "debit": "Debit",
                "credit": "Credit",
            },
            "synthetic.csv",
        )

        self.assertEqual(invalid["original_amount"], "-10.00")
        self.assertIn("invalid_amount", invalid["flags"].split(";"))
        self.assertIn("source_data_issue", invalid["review_reasons"].split(";"))

    def test_bindings_project_owner_to_canonical_rows_and_reject_unknown_accounts(
        self,
    ) -> None:
        binding = {
            "id": "justin-synthetic",
            "profile": "synthetic",
            "owner": "Justin",
            "accounts": [
                {
                    "source_account_id": "raw-account",
                    "account_id": "justin_account",
                    "account": "Justin synthetic account",
                }
            ],
        }
        source_rows = [
            {
                "transaction_id": "source-occurrence",
                "account_id": "raw-account",
                "account": "Raw account",
                "owner": "Household",
            }
        ]
        apply_binding(source_rows, binding)
        updates = canonical_bound_owners(
            source_rows,
            [
                {
                    "source_occurrence_pools": [["source-occurrence"]],
                    "canonical_transaction_ids": ["canonical-transaction"],
                }
            ],
            {},
        )
        canonical_rows = [
            {"transaction_id": "canonical-transaction", "owner": "Household"}
        ]
        enforce_bound_owners(canonical_rows, updates)

        self.assertEqual(source_rows[0]["account_id"], "justin_account")
        self.assertEqual(updates, {"canonical-transaction": "Justin"})
        self.assertEqual(canonical_rows[0]["owner"], "Justin")
        with self.assertRaisesRegex(AccountBindingError, "does not cover"):
            apply_binding(
                [{"account_id": "unknown-account"}],
                binding,
            )

    def test_profile_mapping_lifecycle_keeps_one_deterministic_binding(self) -> None:
        binding = {
            "id": "justin-synthetic",
            "profile": "synthetic",
            "owner": "Justin",
            "accounts": [
                {
                    "source_account_id": "raw-account",
                    "account_id": "justin_account",
                    "account": "Justin synthetic account",
                }
            ],
        }
        mappings = upsert_binding({}, binding, "synthetic-*.csv")
        validate_profile_mappings(mappings, {"owners": ["Justin"]})
        validate_bindings_for_profiles(
            mappings,
            [
                {
                    "id": "synthetic",
                    "account_id": "raw-account",
                    "csv": {"columns": {"description": "Description"}},
                }
            ],
        )

        source = Path("synthetic-may.csv")
        self.assertEqual(
            matching_filename_mapping(source, mappings),
            {
                "pattern": "synthetic-*.csv",
                "profile": "synthetic",
                "binding": "justin-synthetic",
            },
        )
        self.assertEqual(
            binding_for_source(source, {"id": "synthetic"}, mappings)["owner"],
            "Justin",
        )

        self.assertEqual(
            binding_by_id(mappings, "justin-synthetic")["accounts"][0]["account_id"],
            "justin_account",
        )

        renamed, changed = replace_binding_pattern(
            mappings,
            "justin-synthetic",
            "synthetic-*.csv",
            "synthetic-may-*.csv",
        )
        self.assertTrue(changed)
        replayed, replay_changed = replace_binding_pattern(
            renamed,
            "justin-synthetic",
            "synthetic-*.csv",
            "synthetic-may-*.csv",
        )
        self.assertFalse(replay_changed)
        self.assertEqual(replayed, renamed)

        removed, removed_changed, removed_binding, selected_profile = (
            remove_binding_pattern(
                renamed,
                "justin-synthetic",
                "synthetic-may-*.csv",
                confirm_final=True,
            )
        )
        self.assertTrue(removed_changed)
        self.assertTrue(removed_binding)
        self.assertEqual(selected_profile, "synthetic")
        self.assertEqual(binding_views(removed), [])
        replay_removed, replay_changed, replay_binding, replay_profile = (
            remove_binding_pattern(
                removed,
                "justin-synthetic",
                "synthetic-may-*.csv",
                confirm_final=True,
            )
        )
        self.assertEqual(replay_removed, removed)
        self.assertFalse(replay_changed)
        self.assertTrue(replay_binding)
        self.assertEqual(replay_profile, "synthetic")

    def test_binding_owner_rederives_from_the_stored_binding_id(self) -> None:
        mappings = {
            "account_bindings": [
                {
                    "id": "justin-synthetic",
                    "profile": "synthetic",
                    "owner": "Justin",
                    "accounts": [
                        {
                            "source_account_id": "raw-account",
                            "account_id": "justin_account",
                            "account": "Justin synthetic account",
                        }
                    ],
                }
            ],
            "filename_patterns": [],
        }
        source_rows = [
            {
                "transaction_id": "source-occurrence",
                "source_file": "CSV source 0123456789ab",
                "account_binding_id": "justin-synthetic",
                "account_id": "justin_account",
                "account": "Justin synthetic account",
            }
        ]

        updates = canonical_bound_owners(
            source_rows,
            [
                {
                    "source_occurrence_pools": [["source-occurrence"]],
                    "canonical_transaction_ids": ["canonical-transaction"],
                }
            ],
            mappings,
        )

        self.assertEqual(updates, {"canonical-transaction": "Justin"})

    def test_corrections_keep_the_human_choice_and_manual_pair_marker(self) -> None:
        pair_id = manual_pair_id(["transaction-one", "transaction-two"])
        row = {
            "transaction_id": "transaction-one",
            "category": "Unknown",
            "flow_type": "unresolved",
            "flow_source": "deterministic",
            "owner": "Household",
            "payment_method": "Bank Account",
            "confidence": "0.00",
            "reason": "",
            "notes": "",
            "needs_review": "true",
            "review_reasons": "category_decision;accounting_flow",
            "flags": "uncategorized",
        }

        apply_corrections(
            [row],
            {
                "transaction-one": {
                    "category": "Dining",
                    "flow_type": "expense",
                    "owner": "Justin",
                    "confidence": "1.00",
                    "needs_review": "false",
                    "review_reasons": "",
                    "manual_pair_id": pair_id,
                }
            },
        )

        self.assertEqual(row["category"], "Dining")
        self.assertEqual(row["flow_source"], "correction")
        self.assertEqual(row["owner"], "Justin")
        self.assertEqual(row["needs_review"], "false")
        self.assertNotIn("uncategorized", row["flags"].split(";"))
        self.assertEqual(manual_pair_marker(row), pair_id)
        self.assertIn("manual_correction", row["flags"].split(";"))

    def test_correction_documents_validate_and_merge_user_owned_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            corrections_path = Path(temporary_root) / "corrections.csv"
            corrections_path.write_text(
                "transaction_id,category,notes,needs_review,review_reasons\n"
                "saved,Dining,  synthetic note  ,false,\n"
                "reviewed,Transport,,false,accounting_flow\n",
                encoding="utf-8",
            )
            config = {"corrections": str(corrections_path)}
            loaded = load_corrections(config)

            self.assertEqual(loaded["saved"]["notes"], "synthetic note")
            self.assertEqual(loaded["saved"]["needs_review"], "false")
            self.assertEqual(loaded["reviewed"]["needs_review"], "true")
            with self.assertRaisesRegex(ValueError, "Unsupported category"):
                validate_correction("invalid", {"category": "Not a category"}, config)

            output_path, document, merged = prepare_corrections_document(
                config,
                {
                    "saved": {"notes": ""},
                    "new": {
                        "category": "Groceries",
                        "needs_review": "false",
                    },
                },
            )
            self.assertEqual(output_path, corrections_path)
            self.assertEqual(merged["saved"]["notes"], "")
            self.assertEqual(merged["new"]["category"], "Groceries")
            self.assertIn("new,Groceries", document)
            self.assertEqual(
                review_state_correction_updates(
                    merged,
                    [
                        {
                            "transaction_id": "saved",
                            "needs_review": "true",
                            "review_reasons": "other_decision",
                        }
                    ],
                ),
                {
                    "saved": {
                        "needs_review": "true",
                        "review_reasons": "other_decision",
                    }
                },
            )

    def test_learning_uses_amount_rules_for_conflicting_reviewed_history(
        self,
    ) -> None:
        rows = [
            {
                "transaction_id": "first",
                "institution": "Synthetic Bank",
                "account_id": "synthetic-account",
                "original_description": "SYNTHETIC SHOP",
                "posted_amount": "-10.00",
                "posted_currency": "HKD",
                "flags": "",
            },
            {
                "transaction_id": "second",
                "institution": "Synthetic Bank",
                "account_id": "synthetic-account",
                "original_description": "SYNTHETIC SHOP",
                "posted_amount": "-20.00",
                "posted_currency": "HKD",
                "flags": "",
            },
        ]
        plan = plan_learned_rules(
            rows,
            {
                "first": {
                    "category": "Dining",
                    "flow_type": "expense",
                    "needs_review": "false",
                },
                "second": {
                    "category": "Transport",
                    "flow_type": "expense",
                    "needs_review": "false",
                },
            },
        )

        self.assertEqual(plan.broad_rule_count, 0)
        self.assertEqual(plan.amount_rule_count, 2)
        self.assertEqual(plan.conflict_count, 1)
        self.assertEqual(plan.historical_rows_covered, 2)
        self.assertEqual(
            {rule["conditions"][-2]["patterns"][0] for rule in plan.rules},
            {"-10", "-20"},
        )

        ambiguous = dict(
            rows[0], transaction_id="ambiguous", flags="overlap_count_ambiguous"
        )
        skipped = plan_learned_rules(
            [ambiguous],
            {"ambiguous": {"category": "Dining", "needs_review": "false"}},
        )
        self.assertEqual(skipped.rules, [])
        self.assertEqual(skipped.skipped_count, 1)

    def test_manual_pairs_preserve_only_one_marker_and_reject_bad_facts(self) -> None:
        pair_id = manual_pair_id(["left", "right"])
        flags = with_manual_pair_marker(
            "uncategorized;manual_transfer_pair:mpair_00000000000000000000000000000000",
            pair_id,
        )
        self.assertEqual(manual_pair_marker({"flags": flags}), pair_id)
        self.assertEqual(without_manual_pair_marker(flags), "uncategorized")

        left = {
            "transaction_id": "left",
            "account_id": "synthetic-account",
            "posted_currency": "HKD",
            "posted_amount": "-10.00",
            "owner": "Justin",
        }
        right = {
            "transaction_id": "right",
            "account_id": "synthetic-account",
            "posted_currency": "HKD",
            "posted_amount": "10.00",
            "owner": "Justin",
        }
        validate_manual_pair_facts(left, right)
        with self.assertRaisesRegex(ManualPairError, "ownership"):
            validate_manual_pair_facts(
                left,
                {**right, "owner": "Franchesca"},
            )
        with self.assertRaisesRegex(ManualPairError, "two distinct"):
            manual_pair_id(["left"])
        with self.assertRaisesRegex(ValueError, "identifier format"):
            with_manual_pair_marker("", "not-a-pair")
        with self.assertRaisesRegex(ManualPairError, "same posted currency"):
            validate_manual_pair_facts(
                left,
                {**right, "posted_currency": "USD"},
            )

    def test_source_data_repair_and_inspection_use_active_workspace_evidence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            source_path = root / "imports" / "synthetic.csv"
            source_path.parent.mkdir()
            source_path.write_text("synthetic\n", encoding="utf-8")
            namespace_id = source_namespace_id("workspace", "imports/synthetic.csv")
            source = self._source_row(
                token="1",
                namespace_id=namespace_id,
                source_file="imports/synthetic.csv",
                flags=(
                    "invalid_amount;statement_opening_balance_conflict;"
                    "statement_opening_balance_conflict_page_2"
                ),
            )
            state = self._state([source])
            [row] = state.rows
            inspection = inspect_source_data_review(
                state,
                row["transaction_id"],
                workspace_root=root,
            )

            self.assertEqual(inspection["evidence_status"], "active")
            self.assertEqual(
                inspection["active_evidence_flags"],
                ["invalid_amount", "statement_opening_balance_conflict"],
            )
            self.assertEqual(
                {
                    (item["source_file"], item["source_display"])
                    for item in inspection["evidence"]
                },
                {("imports/synthetic.csv", "synthetic.csv")},
            )
            self.assertIn(
                "2",
                {
                    item["source_page"]
                    for item in inspection["evidence"]
                    if item["flag"] == "statement_opening_balance_conflict"
                },
            )

            source["flags"] = ""
            row["flags"] = (
                "kept_flag;invalid_amount;statement_opening_balance_conflict_page_2"
            )
            row["review_reasons"] = "source_data_issue"
            row["needs_review"] = "true"
            changed = repair_source_data_review_state(
                state.rows,
                state.source_rows or [],
                state.overlap_manifest or empty_overlap_manifest("ovns_" + "0" * 64),
            )

            self.assertEqual(changed, {row["transaction_id"]})
            self.assertEqual(row["flags"], "kept_flag")
            self.assertEqual(row["review_reasons"], "")
            self.assertEqual(row["needs_review"], "false")
            cleared = inspect_source_data_review(
                state,
                row["transaction_id"],
                workspace_root=root,
            )
            self.assertEqual(cleared["evidence_status"], "clear")
            self.assertEqual(cleared["evidence"][0]["evidence_status"], "no_support")
            with self.assertRaises(SourceDataReviewError) as raised:
                inspect_source_data_review(state, "unknown", workspace_root=root)
            self.assertEqual(raised.exception.code, "source_data_transaction_unknown")
            self.assertEqual(
                safe_source_location(
                    "imports/synthetic.csv",
                    "ns_" + "f" * 64,
                    root,
                    None,
                ),
                ("", "synthetic.csv"),
            )

    def test_missing_valuation_inspection_filters_and_fails_closed_on_count_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            source_path = root / "imports" / "synthetic.csv"
            source_path.parent.mkdir()
            source_path.write_text("synthetic\n", encoding="utf-8")
            namespace_id = source_namespace_id("workspace", "imports/synthetic.csv")
            state = self._state(
                [
                    self._source_row(
                        token="2",
                        namespace_id=namespace_id,
                        source_file="imports/synthetic.csv",
                    )
                ]
            )
            [row] = state.rows
            rows = inspect_missing_valuations(
                state,
                workspace_root=root,
                start="2026-05-01",
                end="2026-05-31",
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["valuation_status"], "missing")
            self.assertEqual(rows[0]["source_occurrence_count"], 1)
            self.assertEqual(
                rows[0]["source_evidence"],
                [
                    {
                        "source_file": "imports/synthetic.csv",
                        "source_display": "synthetic.csv",
                        "source_page": "1",
                        "source_row": "2",
                    }
                ],
            )
            self.assertEqual(
                inspect_missing_valuations(
                    state,
                    workspace_root=root,
                    start="2026-06-01",
                ),
                [],
            )

            row["source_occurrence_count"] = "2"
            with self.assertRaises(ValuationInspectionError) as raised:
                inspect_missing_valuations(state, workspace_root=root)
            self.assertEqual(raised.exception.code, "valuation_provenance_inconsistent")


if __name__ == "__main__":
    unittest.main()
