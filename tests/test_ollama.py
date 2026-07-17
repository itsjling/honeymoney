import json
import threading
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

from honeymoney.ollama import (
    OllamaHttpRequest,
    apply_ollama_fallback,
    list_ollama_models,
)


def unresolved_transaction(transaction_id: str = "txn_1") -> dict[str, str]:
    return {
        "transaction_id": transaction_id,
        "date": "2026-05-01",
        "merchant": "MYSTERY",
        "original_description": "MYSTERY RAW",
        "original_amount": "-10.00",
        "original_currency": "HKD",
        "posted_amount": "-10.00",
        "posted_currency": "HKD",
        "amount_hkd": "-10.00",
        "institution": "HSBC HK",
        "payment_method": "Credit Card",
        "category": "Unknown",
        "owner": "Household",
        "confidence": "0.00",
        "needs_review": "true",
        "reason": "No categorization rules have been applied",
        "flags": "uncategorized",
        "source_file": "private/statement.pdf",
        "notes": "",
    }


def model_response(categorizations: object) -> bytes:
    return json.dumps({"response": json.dumps(categorizations)}).encode()


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[OllamaHttpRequest] = []

    def request(self, request: OllamaHttpRequest) -> bytes:
        self.requests.append(request)
        return self.handler(request)


def prompt_for(request: OllamaHttpRequest) -> dict:
    assert request.body is not None
    return json.loads(json.loads(request.body)["prompt"])


def successful_handler(request: OllamaHttpRequest) -> bytes:
    rows = prompt_for(request)["transactions"]
    return model_response(
        [
            {
                "id": row["id"],
                "category": "Dining",
                "confidence": 0.91,
                "reason": "Synthetic merchant",
            }
            for row in rows
        ]
    )


class OllamaTest(unittest.TestCase):
    def config(self, **overrides: object) -> dict:
        ollama = {
            "enabled": True,
            "url": "http://localhost:11434/api/generate",
            "model": "qwen2.5:7b-instruct",
        }
        ollama.update(overrides)
        return {"ollama": ollama}

    def test_model_listing_uses_the_shared_transport_boundary(self) -> None:
        def handler(request: OllamaHttpRequest) -> bytes:
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.url, "http://localhost:11434/api/tags")
            self.assertEqual(request.headers, {"Accept": "application/json"})
            self.assertIsNone(request.body)
            self.assertEqual(request.timeout, 10.0)
            return b'{"models": [{"name": "zeta"}, {"name": "alpha"}]}'

        models = list_ollama_models(
            self.config(timeout_seconds=20)["ollama"],
            transport=FakeTransport(handler),
        )

        self.assertEqual(models, ["alpha", "zeta"])

    def test_payload_is_minimized_and_response_is_applied(self) -> None:
        transport = FakeTransport(successful_handler)
        transactions = [unresolved_transaction()]

        report, warnings = apply_ollama_fallback(
            transactions, self.config(), transport=transport
        )

        self.assertEqual(warnings, [])
        self.assertEqual(report["status"], "success")
        self.assertEqual(transactions[0]["category"], "Dining")
        self.assertEqual(transactions[0]["needs_review"], "false")
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url, "http://localhost:11434/api/generate")
        self.assertEqual(request.headers, {"Content-Type": "application/json"})
        self.assertEqual(request.timeout, 120.0)
        body = json.loads(request.body)
        self.assertIs(body["think"], False)
        item_schema = body["format"]["properties"]["categorizations"]["items"]
        self.assertIn("Dining", item_schema["properties"]["category"]["enum"])
        self.assertEqual(
            item_schema["required"], ["id", "category", "confidence", "reason"]
        )
        prompt = json.loads(body["prompt"])
        self.assertIn("Dining", prompt["allowed_categories"])
        self.assertNotIn("allowed_owners", prompt)
        sent_transaction = prompt["transactions"][0]
        self.assertNotIn("source_file", sent_transaction)
        self.assertNotIn("notes", sent_transaction)

    def test_object_wrapped_categorizations_response_is_accepted(self) -> None:
        transport = FakeTransport(
            lambda request: model_response(
                {
                    "categorizations": [
                        {
                            "id": "txn_1",
                            "category": "Transport",
                            "confidence": 0.95,
                            "reason": "Ride hailing merchant",
                        }
                    ]
                }
            )
        )
        transaction = unresolved_transaction()

        report, warnings = apply_ollama_fallback(
            [transaction], self.config(), transport=transport
        )

        self.assertEqual(warnings, [])
        self.assertEqual(report["status"], "success")
        self.assertEqual(transaction["category"], "Transport")

    def test_unresolved_transactions_are_sent_in_configured_batches(self) -> None:
        transport = FakeTransport(successful_handler)
        transactions = [
            unresolved_transaction("txn_1"),
            unresolved_transaction("txn_2"),
        ]

        report, warnings = apply_ollama_fallback(
            transactions,
            self.config(batch_size=1),
            transport=transport,
        )

        self.assertEqual(warnings, [])
        self.assertEqual(report["applied_count"], 2)
        self.assertEqual(
            [
                len(prompt_for(request)["transactions"])
                for request in transport.requests
            ],
            [1, 1],
        )

    def test_unavailable_batch_reports_additive_partial_counts(self) -> None:
        calls = 0

        def handler(request: OllamaHttpRequest) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise urllib.error.HTTPError(
                    request.url,
                    503,
                    "Synthetic unavailable",
                    {},
                    BytesIO(b""),
                )
            return successful_handler(request)

        transport = FakeTransport(handler)
        first = unresolved_transaction("txn_1")
        second = unresolved_transaction("txn_2")

        report, warnings = apply_ollama_fallback(
            [first, second],
            self.config(batch_size=1),
            transport=transport,
        )

        self.assertTrue(warnings[0].startswith("Ollama unavailable: HTTP 503"))
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(report["accepted_count"], 1)
        self.assertEqual(report["applied_count"], 1)
        self.assertNotIn("ollama_unavailable", first["flags"])
        self.assertIn("ollama_unavailable", second["flags"])

    def test_progress_callback_reports_each_batch(self) -> None:
        progress_calls = []

        apply_ollama_fallback(
            [unresolved_transaction("txn_1"), unresolved_transaction("txn_2")],
            self.config(batch_size=1),
            progress=lambda event: progress_calls.append(
                (
                    event.batch_number,
                    event.batch_count,
                    event.start_index,
                    event.end_index,
                )
            ),
            transport=FakeTransport(successful_handler),
        )

        self.assertEqual(progress_calls, [(1, 2, 1, 1), (2, 2, 2, 2)])

    def test_missing_ollama_ids_and_reasons_are_invalid_responses(self) -> None:
        transport = FakeTransport(
            lambda request: model_response(
                [{"id": "txn_1", "category": "Dining", "confidence": 0.91}]
            )
        )
        transactions = [
            unresolved_transaction("txn_1"),
            unresolved_transaction("txn_2"),
        ]

        report, warnings = apply_ollama_fallback(
            transactions, self.config(), transport=transport
        )

        self.assertEqual(report["status"], "invalid_response")
        self.assertEqual(report["invalid_count"], 2)
        self.assertIn(
            "Ollama categorization rejected (MYSTERY): missing reason", warnings
        )
        self.assertIn(
            "Ollama returned no categorization for 1 transaction(s)", warnings
        )
        for transaction in transactions:
            self.assertIn("ollama_invalid_response", transaction["flags"])

    def test_invalid_ollama_json_shape_is_reported_not_raised(self) -> None:
        for response in [
            {"id": "txn_1"},
            [None],
            ["not a categorization"],
            [
                {
                    "id": "txn_1",
                    "category": "Dining",
                    "confidence": "NaN",
                    "reason": "Not finite",
                }
            ],
        ]:
            with self.subTest(response=response):
                transaction = unresolved_transaction()
                report, warnings = apply_ollama_fallback(
                    [transaction],
                    self.config(),
                    transport=FakeTransport(
                        lambda request, response=response: model_response(response)
                    ),
                )

                self.assertEqual(report["status"], "invalid_response")
                self.assertEqual(report["invalid_count"], 1)
                self.assertEqual(warnings[0], "Ollama returned invalid categorizations")
                self.assertIn("ollama_invalid_response", transaction["flags"])

    def test_http_error_body_is_surfaced_in_warning(self) -> None:
        def handler(request: OllamaHttpRequest) -> bytes:
            raise urllib.error.HTTPError(
                request.url,
                404,
                "Not Found",
                {},
                BytesIO(b'{"error": "model \\"qwen2.5:7b-instruct\\" not found"}'),
            )

        transaction = unresolved_transaction()
        report, warnings = apply_ollama_fallback(
            [transaction], self.config(), transport=FakeTransport(handler)
        )

        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(
            warnings,
            [
                "Ollama unavailable: HTTP 404 Not Found: "
                'model "qwen2.5:7b-instruct" not found'
            ],
        )

    def test_disallowed_category_is_named_in_warning(self) -> None:
        transport = FakeTransport(
            lambda request: model_response(
                [
                    {
                        "id": "txn_1",
                        "category": "Ride Sharing",
                        "confidence": 0.9,
                        "reason": "Uber trip",
                    }
                ]
            )
        )

        report, warnings = apply_ollama_fallback(
            [unresolved_transaction()], self.config(), transport=transport
        )

        self.assertEqual(report["status"], "invalid_response")
        self.assertIn(
            "Ollama categorization rejected (MYSTERY): "
            "category 'Ride Sharing' is not allowed",
            warnings,
        )

    def test_request_timeout_is_configurable(self) -> None:
        def handler(request: OllamaHttpRequest) -> bytes:
            self.assertEqual(request.timeout, 0.2)
            raise TimeoutError("timed out")

        report, warnings = apply_ollama_fallback(
            [unresolved_transaction()],
            self.config(timeout_seconds=0.2),
            transport=FakeTransport(handler),
        )

        self.assertEqual(report["status"], "unavailable")
        self.assertIn("timed out", warnings[0])

    def test_progress_ticks_with_elapsed_time_while_waiting(self) -> None:
        release = threading.Event()

        def handler(request: OllamaHttpRequest) -> bytes:
            release.wait(0.08)
            return model_response([])

        elapsed_readings = []
        with patch("honeymoney.ollama._TICK_INTERVAL_SECONDS", 0.02):
            apply_ollama_fallback(
                [unresolved_transaction()],
                self.config(),
                progress=lambda event: elapsed_readings.append(event.elapsed_seconds),
                transport=FakeTransport(handler),
            )

        self.assertEqual(elapsed_readings[0], 0.0)
        self.assertGreater(len(elapsed_readings), 1)
        self.assertTrue(
            all(a <= b for a, b in zip(elapsed_readings, elapsed_readings[1:]))
        )
        self.assertGreater(elapsed_readings[-1], 0.0)


if __name__ == "__main__":
    unittest.main()
