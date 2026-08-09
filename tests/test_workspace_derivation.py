from __future__ import annotations

import unittest
from unittest.mock import patch

import honeymoney.workspace_derivation as derivation_module
from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    extractor_contract_id,
    ownership_record,
    record_fingerprint,
    source_id,
    source_namespace_id,
    source_revision,
)
from honeymoney.overlap import empty_overlap_manifest
from honeymoney.schema import SOURCE_OCCURRENCE_COLUMNS
from honeymoney.workspace_derivation import derive_workspace_rows, view_report_inputs


def _source_row(label: str, token: str) -> dict[str, str]:
    namespace = source_namespace_id("workspace", label)
    source = source_id(namespace)
    revision = source_revision(("synthetic " + token).encode())
    contract = extractor_contract_id(
        1,
        {
            "id": "synthetic",
            "account_id": "checking",
            "csv": {"columns": {"date": "Date"}},
        },
    )
    facts = {
        "account_id": "checking",
        "date": "2026-08-08",
        "transaction_date": "2026-08-07",
        "posting_date": "2026-08-08",
        "original_amount": "-12",
        "original_currency": "HKD",
        "posted_amount": "-12",
        "posted_currency": "HKD",
        "merchant": "Synthetic Shop",
        "original_description": "Synthetic Shop",
    }
    fingerprint = record_fingerprint(facts)
    record = ownership_record(
        source_id_value=source,
        fingerprint=fingerprint,
        origin=AllocationOrigin(revision, contract, AllocationLocator(1, (2,)), 1),
    )
    row = {column: "" for column in SOURCE_OCCURRENCE_COLUMNS}
    row.update(
        facts,
        transaction_id=record["transaction_id"],
        source_id=source,
        source_namespace_id=namespace,
        source_revision=revision,
        source_record_id=record["source_record_id"],
        account="Checking",
        account_type="bank",
        institution="Synthetic Bank",
        country="HK",
        category="Unknown",
        flow_type="unresolved",
        flow_source="deterministic",
        owner="Household",
        payment_method="Bank Account",
        confidence="0.00",
        needs_review="true",
        review_reasons="category_decision;accounting_flow",
        reason="No categorization rules have been applied",
        flags="uncategorized",
        source_file=label,
        source_row="2",
    )
    return row


class WorkspaceDerivationTest(unittest.TestCase):
    def test_full_workspace_pipeline_uses_the_decided_stage_order(self) -> None:
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0},
            "review_confidence_threshold": 0.8,
            "reconciliation": {"date_window_days": 3},
            "categorization_memory": {"enabled": False},
            "ollama": {"enabled": True},
        }
        baseline = derive_workspace_rows(
            [_source_row("one.csv", "one")],
            empty_overlap_manifest("ovns_" + "c" * 64),
            config,
            rules=[],
            corrections={},
            allow_model=False,
        )
        transaction_id = baseline.rows[0]["transaction_id"]
        stages: list[str] = []
        real_rules = derivation_module.apply_rules
        real_memory = derivation_module.apply_local_categorization_memory
        real_corrections = derivation_module.apply_corrections
        real_valuation = derivation_module.value_transactions
        real_reconciliation = derivation_module.reconcile_ledger
        real_duplicates = derivation_module.refresh_duplicate_candidates
        real_source_repair = derivation_module.repair_source_data_review_state
        real_review = derivation_module.synchronize_review_states

        def mark(name: str, function: object):
            def wrapped(*args: object, **kwargs: object):
                stages.append(name)
                return function(*args, **kwargs)  # type: ignore[operator]

            return wrapped

        def model(rows: list[dict[str, str]], *_args: object, **_kwargs: object):
            stages.append("model")
            self.assertEqual(rows[0]["category"], "Groceries")
            return {}, []

        with (
            patch.object(derivation_module, "apply_rules", mark("rules", real_rules)),
            patch.object(
                derivation_module,
                "apply_local_categorization_memory",
                mark("memory", real_memory),
            ),
            patch.object(
                derivation_module,
                "apply_corrections",
                mark("corrections", real_corrections),
            ),
            patch.object(derivation_module, "apply_ollama_fallback", model),
            patch.object(
                derivation_module,
                "value_transactions",
                mark("valuation", real_valuation),
            ),
            patch.object(
                derivation_module,
                "reconcile_ledger",
                mark("reconciliation", real_reconciliation),
            ),
            patch.object(
                derivation_module,
                "refresh_duplicate_candidates",
                mark("duplicates", real_duplicates),
            ),
            patch.object(
                derivation_module,
                "repair_source_data_review_state",
                mark("source-repair", real_source_repair),
            ),
            patch.object(
                derivation_module,
                "synchronize_review_states",
                mark("review", real_review),
            ),
        ):
            derive_workspace_rows(
                [_source_row("one.csv", "one")],
                baseline.overlap_manifest,
                config,
                rules=[],
                corrections={
                    transaction_id: {
                        "category": "Groceries",
                        "flow_type": "expense",
                    }
                },
            )

        first = {name: stages.index(name) for name in set(stages)}
        self.assertLess(first["rules"], first["memory"])
        self.assertLess(first["memory"], first["corrections"])
        self.assertLess(first["corrections"], first["model"])
        self.assertLess(first["model"], first["valuation"])
        self.assertLess(first["valuation"], first["duplicates"])
        self.assertLess(first["duplicates"], first["source-repair"])
        self.assertLess(first["source-repair"], first["review"])
        self.assertLess(first["review"], first["reconciliation"])

    def test_added_exact_support_keeps_view_transaction_identity(self) -> None:
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0},
            "review_confidence_threshold": 0.8,
            "reconciliation": {"date_window_days": 3},
            "categorization_memory": {"enabled": False},
            "ollama": {"enabled": False},
        }
        first = derive_workspace_rows(
            [_source_row("one.csv", "one")],
            empty_overlap_manifest("ovns_" + "a" * 64),
            config,
            rules=[],
            corrections={},
        )
        identifier = first.rows[0]["transaction_id"]

        second = derive_workspace_rows(
            [_source_row("one.csv", "one"), _source_row("two.csv", "two")],
            first.overlap_manifest,
            config,
            rules=[],
            corrections={},
        )

        self.assertEqual(len(second.rows), 1)
        self.assertEqual(second.rows[0]["transaction_id"], identifier)
        self.assertEqual(second.rows[0]["provenance_status"], "exact_one_to_one")
        self.assertEqual(second.rows[0]["amount_hkd"], "-12.00")

    def test_report_inputs_use_the_complete_contributing_statement(self) -> None:
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0},
            "review_confidence_threshold": 0.8,
            "reconciliation": {"date_window_days": 3},
            "categorization_memory": {"enabled": False},
            "ollama": {"enabled": False},
        }
        may = _source_row("one.csv", "may")
        june = _source_row("one.csv", "june")
        may.update(
            posting_date="2026-05-10",
            transaction_date="2026-05-10",
            date="2026-05-10",
            statement_section="main",
            statement_opening_balance="100",
        )
        june.update(
            posting_date="2026-06-10",
            transaction_date="2026-06-10",
            date="2026-06-10",
            statement_section="main",
            statement_closing_balance="76",
        )
        derivation = derive_workspace_rows(
            [may, june],
            empty_overlap_manifest("ovns_" + "d" * 64),
            config,
            rules=[],
            corrections={},
        )
        selected = [
            row for row in derivation.rows if row["posting_date"].startswith("2026-05")
        ]

        report_inputs = view_report_inputs(derivation, selected)

        self.assertEqual(report_inputs.source_occurrence_count, 2)
        self.assertEqual(
            report_inputs.balance_reconciliation["checking"]["result"],
            "matched",
        )

    def test_saved_correction_wins_and_reconciliation_runs_globally(self) -> None:
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"HKD": 1.0},
            "review_confidence_threshold": 0.8,
            "reconciliation": {"date_window_days": 3},
            "categorization_memory": {"enabled": False},
            "ollama": {"enabled": False},
        }
        baseline = derive_workspace_rows(
            [_source_row("one.csv", "one")],
            empty_overlap_manifest("ovns_" + "b" * 64),
            config,
            rules=[],
            corrections={},
        )
        identifier = baseline.rows[0]["transaction_id"]

        result = derive_workspace_rows(
            [_source_row("one.csv", "one")],
            baseline.overlap_manifest,
            config,
            rules=[
                {
                    "id": "fallback",
                    "enabled": True,
                    "priority": 1,
                    "conditions": [
                        {
                            "field": "merchant",
                            "match_type": "exact",
                            "patterns": ["Synthetic Shop"],
                        }
                    ],
                    "category": "Shopping",
                    "confidence": 0.9,
                }
            ],
            corrections={
                identifier: {
                    "category": "Groceries",
                    "flow_type": "expense",
                    "needs_review": "false",
                    "review_reasons": "",
                }
            },
        )

        self.assertEqual(result.rows[0]["category"], "Groceries")
        self.assertEqual(result.rows[0]["flow_type"], "expense")
        self.assertEqual(result.rows[0]["needs_review"], "false")


if __name__ == "__main__":
    unittest.main()
