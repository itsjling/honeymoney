import json
import unittest
from pathlib import Path

from honeymoney.ollama import OllamaHttpRequest, apply_ollama_fallback
from honeymoney.reconciliation import reconcile_ledger
from honeymoney.rules import apply_rules
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
