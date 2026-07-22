import json
import unittest
from pathlib import Path

from honeymoney.categorization_memory import (
    apply_local_categorization_memory,
    build_local_categorization_memory,
)
from honeymoney.corrections import apply_corrections
from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    extractor_contract_id,
    has_stable_v2_identity,
    ownership_record,
    record_fingerprint,
    source_id,
    source_namespace_id,
    source_ownership,
    source_revision,
    validate_ledger_manifest_agreement,
)
from honeymoney.ollama import OllamaHttpRequest, apply_ollama_fallback
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.rules import apply_rules
from honeymoney.schema import CATEGORIZED_COLUMNS
from tests.golden_helpers import FIXTURE_DIR, assert_rows_match, base_config, load_json


def categorization_case(*parts: str) -> Path:
    return FIXTURE_DIR / "categorization" / Path(*parts)


def assert_report_subset(
    test_case: unittest.TestCase,
    actual: dict,
    expected: dict,
) -> None:
    for key, expected_value in expected.items():
        test_case.assertEqual(actual.get(key), expected_value, f"report field {key}")


class DeterministicCategorizationTest(unittest.TestCase):
    def test_rules_cover_priority_match_types_fields_and_review_threshold(self) -> None:
        self.assert_rule_case("rule_priority")

    def test_already_categorized_rows_only_change_when_a_rule_matches(self) -> None:
        self.assert_rule_case("already_categorized_stability")

    def assert_rule_case(self, fixture_name: str) -> None:
        case_dir = categorization_case("deterministic", fixture_name)
        rows = load_json(case_dir / "rows.json")
        rules = load_json(case_dir / "rules.json")
        config = load_json(case_dir / "config.json")
        expected = load_json(case_dir / "expected.json")

        apply_rules(rows, rules, config)

        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))


class LocalCategorizationMemoryTest(unittest.TestCase):
    def _config(self) -> dict:
        return load_json(categorization_case("memory", "config.json"))

    def _v2_rows(self, merchants: list[str]) -> list[dict[str, str]]:
        namespace = source_namespace_id("workspace", "synthetic-memory.csv")
        source = source_id(namespace)
        revision = source_revision(b"synthetic categorization memory fixture\n")
        contract = extractor_contract_id(
            1, {"id": "synthetic-memory", "csv": {"columns": {"date": "Date"}}}
        )
        rows: list[dict[str, str]] = []
        records: list[dict[str, object]] = []
        for index, merchant in enumerate(merchants, start=1):
            row = {column: "" for column in CATEGORIZED_COLUMNS}
            row.update(
                {
                    "date": "2026-07-01",
                    "transaction_date": "2026-07-01",
                    "account_id": "synthetic-account",
                    "account_type": "bank",
                    "institution": "Synthetic Bank",
                    "original_amount": "-10.00",
                    "original_currency": "HKD",
                    "posted_amount": "-10.00",
                    "posted_currency": "HKD",
                    "amount_hkd": "-10.00",
                    "merchant": merchant,
                    "original_description": merchant,
                    "category": "Unknown",
                    "flow_type": "unresolved",
                    "owner": "Household",
                    "confidence": "0.00",
                    "needs_review": "true",
                    "flags": "uncategorized",
                    "source_file": "synthetic-memory.csv",
                }
            )
            record = ownership_record(
                source_id_value=source,
                fingerprint=record_fingerprint(row),
                origin=AllocationOrigin(
                    revision, contract, AllocationLocator(1, (index,)), 1
                ),
            )
            row.update(
                {
                    "transaction_id": str(record["transaction_id"]),
                    "source_id": source,
                    "source_namespace_id": namespace,
                    "source_revision": revision,
                    "source_record_id": str(record["source_record_id"]),
                }
            )
            rows.append(row)
            records.append(record)
        manifest = {
            "schema_version": 1,
            "sources": [
                source_ownership(
                    source_id_value=source,
                    namespace_id=namespace,
                    revision=revision,
                    contract_id=contract,
                    records=sorted(
                        records, key=lambda record: str(record["source_record_id"])
                    ),
                )
            ],
        }
        validate_ledger_manifest_agreement(rows, manifest)
        return rows

    def test_memory_uses_valid_v2_evidence_and_keeps_unsafe_rows_out(self) -> None:
        first, second, target, excluded_one, excluded_two = self._v2_rows(
            ["Park-N-Shop", "PARK N SHOP", "park.n.shop", "APPLE", "apple"]
        )
        corrections = {
            first["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
            },
            second["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
            },
            excluded_one["transaction_id"]: {
                "category": "Shopping",
                "needs_review": "false",
            },
            excluded_two["transaction_id"]: {
                "category": "Shopping",
                "needs_review": "false",
            },
        }

        memory = build_local_categorization_memory(
            [first, second, target, excluded_one, excluded_two],
            corrections,
            self._config(),
        )
        apply_local_categorization_memory([target], memory, self._config())

        self.assertTrue(has_stable_v2_identity(first))
        self.assertEqual(target["category"], "Groceries")
        self.assertEqual(target["confidence"], "0.90")
        self.assertEqual(target["needs_review"], "false")
        self.assertIn("local_memory_categorized", target["flags"])
        self.assertIn("2 reviewed transactions", target["reason"])
        self.assertEqual(len(memory), 1)

    def test_memory_excludes_ambiguous_migration_evidence_and_legacy_targets(
        self,
    ) -> None:
        first, second, target = self._v2_rows(
            ["Park-N-Shop", "PARK N SHOP", "park.n.shop"]
        )
        first["flags"] = "identity_migration_ambiguous"
        second["flags"] = "identity_migration_ambiguous"
        corrections = {
            first["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
            },
            second["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
            },
        }

        memory = build_local_categorization_memory(
            [first, second, target], corrections, self._config()
        )
        self.assertEqual(memory, {})

        legacy_target = dict(target)
        legacy_target["transaction_id"] = "txn_0123456789abcdef"
        for field in (
            "source_id",
            "source_namespace_id",
            "source_revision",
            "source_record_id",
        ):
            legacy_target[field] = ""
        apply_local_categorization_memory(
            [legacy_target],
            {
                ("synthetic-account", "Synthetic Bank", "HKD", "park n shop"): {
                    "category": "Groceries",
                    "observation_count": 2,
                }
            },
            self._config(),
        )
        self.assertEqual(legacy_target["category"], "Unknown")
        self.assertNotIn("local_memory_categorized", legacy_target["flags"])

    def test_rules_and_exact_corrections_keep_precedence(self) -> None:
        first, second, target = self._v2_rows(
            ["RULED SHOP", "ruled.shop", "RULED SHOP"]
        )
        memory = build_local_categorization_memory(
            [first, second, target],
            {
                first["transaction_id"]: {
                    "category": "Groceries",
                    "needs_review": "false",
                },
                second["transaction_id"]: {
                    "category": "Groceries",
                    "needs_review": "false",
                },
            },
            self._config(),
        )
        apply_rules(
            [target],
            [
                {
                    "id": "explicit-memory-rule",
                    "match_type": "exact",
                    "patterns": ["RULED SHOP"],
                    "fields": ["merchant"],
                    "category": "Dining",
                    "confidence": 0.99,
                }
            ],
            self._config(),
        )
        apply_local_categorization_memory([target], memory, self._config())

        self.assertEqual(target["category"], "Dining")
        self.assertIn("matched_rule:explicit-memory-rule", target["flags"])
        self.assertNotIn("local_memory_categorized", target["flags"])

        _, _, corrected_target = self._v2_rows(
            ["RULED SHOP", "ruled.shop", "RULED SHOP"]
        )
        apply_local_categorization_memory([corrected_target], memory, self._config())
        self.assertEqual(corrected_target["category"], "Groceries")
        apply_corrections(
            [corrected_target],
            {
                corrected_target["transaction_id"]: {
                    "category": "Shopping",
                    "needs_review": "false",
                    "reason": "Exact correction wins",
                }
            },
        )
        self.assertEqual(corrected_target["category"], "Shopping")
        self.assertIn("manual_correction", corrected_target["flags"])

    def test_memory_requires_explicit_resolved_evidence_and_exact_scope(self) -> None:
        first, second, target = self._v2_rows(
            ["Park-N-Shop", "PARK N SHOP", "park.n.shop"]
        )
        omitted_review = {
            first["transaction_id"]: {"category": "Groceries"},
            second["transaction_id"]: {"category": "Groceries"},
        }
        self.assertEqual(
            build_local_categorization_memory(
                [first, second, target], omitted_review, self._config()
            ),
            {},
        )

        resolved = {
            transaction["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
            }
            for transaction in (first, second)
        }
        memory = build_local_categorization_memory(
            [first, second, target], resolved, self._config()
        )
        target["account_id"] = "Synthetic-Account"
        apply_local_categorization_memory([target], memory, self._config())
        self.assertEqual(target["category"], "Unknown")
        self.assertNotIn("local_memory_categorized", target["flags"])

    def test_memory_rejects_conflicts_removal_and_low_confidence(self) -> None:
        first, second, target = self._v2_rows(
            ["Park-N-Shop", "PARK N SHOP", "park.n.shop"]
        )
        resolved = {
            first["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
                "confidence": "0.80",
            },
            second["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
                "confidence": "0.80",
            },
        }
        self.assertTrue(
            build_local_categorization_memory(
                [first, second, target], resolved, self._config()
            )
        )

        conflict = {
            **resolved,
            second["transaction_id"]: {
                "category": "Dining",
                "needs_review": "false",
                "confidence": "0.80",
            },
        }
        self.assertEqual(
            build_local_categorization_memory(
                [first, second, target], conflict, self._config()
            ),
            {},
        )
        self.assertEqual(
            build_local_categorization_memory(
                [first, second, target],
                {first["transaction_id"]: resolved[first["transaction_id"]]},
                self._config(),
            ),
            {},
        )
        low_confidence = {
            **resolved,
            second["transaction_id"]: {
                "category": "Groceries",
                "needs_review": "false",
                "confidence": "0.79",
            },
        }
        self.assertEqual(
            build_local_categorization_memory(
                [first, second, target], low_confidence, self._config()
            ),
            {},
        )

    def test_memory_never_learns_or_applies_accounting_or_manual_categories(
        self,
    ) -> None:
        first, second, target = self._v2_rows(
            ["POLICY SHOP", "policy.shop", "policy-shop"]
        )
        for category in ("Income", "Other"):
            corrections = {
                transaction["transaction_id"]: {
                    "category": category,
                    "needs_review": "false",
                }
                for transaction in (first, second)
            }
            self.assertEqual(
                build_local_categorization_memory(
                    [first, second, target], corrections, self._config()
                ),
                {},
            )
            target["category"] = "Unknown"
            apply_local_categorization_memory(
                [target],
                {
                    (
                        "synthetic-account",
                        "Synthetic Bank",
                        "HKD",
                        "policy shop",
                    ): {"category": category, "observation_count": 2}
                },
                self._config(),
            )
            self.assertEqual(target["category"], "Unknown")


class FakeOllamaTransport:
    def __init__(self, response_factory):
        self.requests: list[dict] = []
        self.response_factory = response_factory

    def __enter__(self) -> "FakeOllamaTransport":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    @property
    def url(self) -> str:
        return "http://localhost:11434/api/generate"

    def request(self, request: OllamaHttpRequest) -> bytes:
        assert request.body is not None
        body = json.loads(request.body)
        self.requests.append(body)
        return json.dumps(self.response_factory(body)).encode()


class OllamaCategorizationTest(unittest.TestCase):
    def test_success_sends_only_unresolved_minimal_payload_and_applies_response(
        self,
    ) -> None:
        case_dir = categorization_case("ollama", "success_minimal_payload")
        rows = load_json(case_dir / "rows.json")
        response_payload = load_json(case_dir / "response.json")
        expected = load_json(case_dir / "expected.json")

        with FakeOllamaTransport(
            lambda request: {"response": json.dumps(response_payload)}
        ) as server:
            report, warnings = apply_ollama_fallback(
                rows,
                {**base_config(), "ollama": {"enabled": True, "url": server.url}},
                transport=server,
            )

        self.assertEqual(warnings, expected["warnings"])
        assert_report_subset(self, report, expected["report"])
        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))

        prompt = json.loads(server.requests[0]["prompt"])
        self.assertEqual(
            [item["id"] for item in prompt["transactions"]],
            expected["request_transaction_ids"],
        )
        sent = prompt["transactions"][0]
        for field in expected["request_absent_fields"]:
            self.assertNotIn(field, sent)
        self.assertEqual(
            prompt["accounting_boundaries"],
            [
                "A credit card is a payment method, not a purchase purpose.",
                "Cashback or a cash rebate is not cash spending.",
                "Food delivery belongs to Dining, not Transport.",
                "Internet or broadband service belongs to Utilities, not Transport.",
            ],
        )

    def test_invalid_response_keeps_transaction_reviewable_and_flags_it(self) -> None:
        case_dir = categorization_case("ollama", "invalid_response")
        rows = load_json(case_dir / "rows.json")
        response_payload = load_json(case_dir / "response.json")
        expected = load_json(case_dir / "expected.json")

        with FakeOllamaTransport(
            lambda request: {"response": json.dumps(response_payload)}
        ) as server:
            report, warnings = apply_ollama_fallback(
                rows,
                {**base_config(), "ollama": {"enabled": True, "url": server.url}},
                transport=server,
            )

        self.assertEqual(warnings, expected["warnings"])
        assert_report_subset(self, report, expected["report"])
        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))

    def test_unavailable_ollama_keeps_transaction_reviewable_and_flags_it(self) -> None:
        case_dir = categorization_case("ollama", "unavailable")
        rows = load_json(case_dir / "rows.json")
        expected = load_json(case_dir / "expected.json")

        class UnavailableTransport:
            def request(self, request: OllamaHttpRequest) -> bytes:
                raise ConnectionError("synthetic unavailable")

        report, warnings = apply_ollama_fallback(
            rows,
            {
                **base_config(),
                "ollama": {
                    "enabled": True,
                    "url": "http://127.0.0.1:9/api/generate",
                    "timeout_seconds": 0.1,
                },
            },
            transport=UnavailableTransport(),
        )

        assert_report_subset(self, report, expected["report"])
        self.assertTrue(warnings[0].startswith(expected["warnings_prefix"]))
        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))

    def test_batches_are_sent_deterministically(self) -> None:
        case_dir = categorization_case("ollama", "batching")
        rows = load_json(case_dir / "rows.json")
        template = load_json(case_dir / "response_template.json")
        expected = load_json(case_dir / "expected.json")

        def response(request: dict) -> dict:
            prompt = json.loads(request["prompt"])
            return {
                "response": json.dumps(
                    [{"id": item["id"], **template} for item in prompt["transactions"]]
                )
            }

        with FakeOllamaTransport(response) as server:
            report, warnings = apply_ollama_fallback(
                rows,
                {
                    **base_config(),
                    "ollama": {
                        "enabled": True,
                        "url": server.url,
                        "batch_size": 2,
                    },
                },
                transport=server,
            )

        self.assertEqual(warnings, expected["warnings"])
        assert_report_subset(self, report, expected["report"])
        batches = [
            [item["id"] for item in json.loads(request["prompt"])["transactions"]]
            for request in server.requests
        ]
        self.assertEqual(batches, expected["request_batches"])
        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))

    def test_protected_model_categories_are_rejected_without_owner_changes(
        self,
    ) -> None:
        case_dir = categorization_case("ollama", "accounting_safety")
        base_row = load_json(case_dir / "rows.json")[0]
        protected_categories = [
            "Income",
            "Credit Card Payment",
            "Internal Transfer",
            "Savings",
            "Investments",
        ]
        rows = []
        response = []
        for index, category in enumerate(protected_categories, start=1):
            row = dict(base_row)
            row["transaction_id"] = f"txn_protected_{index}"
            row["owner"] = "Justin"
            row["flow_type"] = "unresolved"
            rows.append(row)
            response.append(
                {
                    "id": row["transaction_id"],
                    "category": category,
                    "confidence": 1.0,
                    "reason": "Dangerous accounting suggestion",
                }
            )
        with FakeOllamaTransport(
            lambda request: {"response": json.dumps(response)}
        ) as server:
            report, warnings = apply_ollama_fallback(
                rows,
                {**base_config(), "ollama": {"enabled": True, "url": server.url}},
                transport=server,
            )
        self.assertEqual(warnings, [])
        self.assertEqual(report["rejected_count"], len(protected_categories))
        self.assertEqual(report["applied_count"], 0)
        reconcile_ledger(rows, {})
        for row, category in zip(rows, protected_categories):
            self.assertEqual(row["category"], "Unknown")
            self.assertEqual(row["flow_type"], "unresolved")
            self.assertEqual(row["needs_review"], "true")
            self.assertEqual(row["owner"], "Justin")
            self.assertIn("ollama_policy_rejected", row["flags"])
            self.assertIn(category, row["reason"])
        item_schema = server.requests[0]["format"]["properties"]["categorizations"][
            "items"
        ]
        for category in protected_categories:
            self.assertNotIn(category, item_schema["properties"]["category"]["enum"])
        self.assertNotIn("owner", item_schema["properties"])


if __name__ == "__main__":
    unittest.main()
