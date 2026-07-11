import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from honeymoney.ollama import apply_ollama_fallback
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


class FakeOllamaServer:
    def __init__(self, response_factory):
        self.requests: list[dict] = []
        captured = self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(length))
                captured.append(request)
                body = response_factory(request)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeOllamaServer":
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/api/generate"


class OllamaCategorizationTest(unittest.TestCase):
    def test_success_sends_only_unresolved_minimal_payload_and_applies_response(
        self,
    ) -> None:
        case_dir = categorization_case("ollama", "success_minimal_payload")
        rows = load_json(case_dir / "rows.json")
        response_payload = load_json(case_dir / "response.json")
        expected = load_json(case_dir / "expected.json")

        with FakeOllamaServer(
            lambda request: {"response": json.dumps(response_payload)}
        ) as server:
            report, warnings = apply_ollama_fallback(
                rows,
                {**base_config(), "ollama": {"enabled": True, "url": server.url}},
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

    def test_invalid_response_keeps_transaction_reviewable_and_flags_it(self) -> None:
        case_dir = categorization_case("ollama", "invalid_response")
        rows = load_json(case_dir / "rows.json")
        response_payload = load_json(case_dir / "response.json")
        expected = load_json(case_dir / "expected.json")

        with FakeOllamaServer(
            lambda request: {"response": json.dumps(response_payload)}
        ) as server:
            report, warnings = apply_ollama_fallback(
                rows,
                {**base_config(), "ollama": {"enabled": True, "url": server.url}},
            )

        self.assertEqual(warnings, expected["warnings"])
        assert_report_subset(self, report, expected["report"])
        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))

    def test_unavailable_ollama_keeps_transaction_reviewable_and_flags_it(self) -> None:
        case_dir = categorization_case("ollama", "unavailable")
        rows = load_json(case_dir / "rows.json")
        expected = load_json(case_dir / "expected.json")

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

        with FakeOllamaServer(response) as server:
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
            )

        self.assertEqual(warnings, expected["warnings"])
        assert_report_subset(self, report, expected["report"])
        batches = [
            [item["id"] for item in json.loads(request["prompt"])["transactions"]]
            for request in server.requests
        ]
        self.assertEqual(batches, expected["request_batches"])
        assert_rows_match(self, rows, expected["rows"], context=str(case_dir))


if __name__ == "__main__":
    unittest.main()
