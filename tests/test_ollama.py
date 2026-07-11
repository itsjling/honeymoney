import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from honeymoney.ollama import apply_ollama_fallback


def unresolved_transaction() -> dict[str, str]:
    return {
        "transaction_id": "txn_1",
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


class OllamaTest(unittest.TestCase):
    def test_payload_is_minimized_and_response_is_applied(self) -> None:
        captured_requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                captured_requests.append(json.loads(self.rfile.read(length)))
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": "txn_1",
                                "category": "Dining",
                                "owner": "Household",
                                "confidence": 0.86,
                                "reason": "Restaurant-like merchant",
                            }
                        ]
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        transactions = [unresolved_transaction()]

        report, warnings = apply_ollama_fallback(
            transactions,
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                    "model": "qwen2.5:7b-instruct",
                }
            },
        )

        self.assertEqual(warnings, [])
        self.assertEqual(report["status"], "success")
        self.assertEqual(transactions[0]["category"], "Dining")
        self.assertEqual(transactions[0]["needs_review"], "false")
        request = captured_requests[0]
        self.assertIs(request["think"], False)
        item_schema = request["format"]["properties"]["categorizations"]["items"]
        self.assertIn("Dining", item_schema["properties"]["category"]["enum"])
        self.assertIn("Household", item_schema["properties"]["owner"]["enum"])
        self.assertEqual(
            item_schema["required"], ["id", "category", "owner", "confidence", "reason"]
        )
        prompt = json.loads(request["prompt"])
        self.assertIn("Dining", prompt["allowed_categories"])
        self.assertIn("Household", prompt["allowed_owners"])
        sent_transaction = prompt["transactions"][0]
        self.assertNotIn("source_file", sent_transaction)
        self.assertNotIn("notes", sent_transaction)

    def test_object_wrapped_categorizations_response_is_accepted(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                body = {
                    "response": json.dumps(
                        {
                            "categorizations": [
                                {
                                    "id": "txn_1",
                                    "category": "Transport",
                                    "owner": "Household",
                                    "confidence": 0.95,
                                    "reason": "Ride hailing merchant",
                                }
                            ]
                        }
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        transaction = unresolved_transaction()
        report, warnings = apply_ollama_fallback(
            [transaction],
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                }
            },
        )

        self.assertEqual(warnings, [])
        self.assertEqual(report["status"], "success")
        self.assertEqual(transaction["category"], "Transport")
        self.assertEqual(transaction["needs_review"], "false")

    def test_unresolved_transactions_are_sent_in_configured_batches(self) -> None:
        captured_batches = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request_body = json.loads(self.rfile.read(length))
                prompt = json.loads(request_body["prompt"])
                captured_batches.append(prompt["transactions"])
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": transaction["id"],
                                "category": "Dining",
                                "owner": "Household",
                                "confidence": 0.91,
                                "reason": "Restaurant-like merchant",
                            }
                            for transaction in prompt["transactions"]
                        ]
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        first = unresolved_transaction()
        second = unresolved_transaction()
        second["transaction_id"] = "txn_2"
        transactions = [first, second]

        report, warnings = apply_ollama_fallback(
            transactions,
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                    "model": "qwen2.5:7b-instruct",
                    "batch_size": 1,
                }
            },
        )

        self.assertEqual(warnings, [])
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["applied_count"], 2)
        self.assertEqual([len(batch) for batch in captured_batches], [1, 1])
        self.assertEqual(
            [row["category"] for row in transactions], ["Dining", "Dining"]
        )

    def test_progress_callback_reports_each_batch(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                request_body = json.loads(self.rfile.read(length))
                prompt = json.loads(request_body["prompt"])
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": transaction["id"],
                                "category": "Dining",
                                "owner": "Household",
                                "confidence": 0.91,
                                "reason": "Restaurant-like merchant",
                            }
                            for transaction in prompt["transactions"]
                        ]
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        first = unresolved_transaction()
        second = unresolved_transaction()
        second["transaction_id"] = "txn_2"
        second["merchant"] = "OTHER MERCHANT"
        transactions = [first, second]
        progress_calls = []

        apply_ollama_fallback(
            transactions,
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                    "model": "qwen2.5:7b-instruct",
                    "batch_size": 1,
                }
            },
            progress=lambda event: progress_calls.append(
                (
                    event.batch_number,
                    event.batch_count,
                    event.start_index,
                    event.end_index,
                )
            ),
        )

        self.assertEqual(progress_calls, [(1, 2, 1, 1), (2, 2, 2, 2)])

    def test_missing_ollama_ids_and_reasons_are_invalid_responses(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": "txn_1",
                                "category": "Dining",
                                "owner": "Household",
                                "confidence": 0.91,
                            }
                        ]
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        first = unresolved_transaction()
        second = unresolved_transaction()
        second["transaction_id"] = "txn_2"
        transactions = [first, second]

        report, warnings = apply_ollama_fallback(
            transactions,
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                }
            },
        )

        self.assertEqual(report["status"], "invalid_response")
        self.assertEqual(report["applied_count"], 0)
        self.assertEqual(report["invalid_count"], 2)
        self.assertEqual(warnings[0], "Ollama returned invalid categorizations")
        self.assertIn(
            "Ollama categorization rejected (MYSTERY): missing reason", warnings
        )
        self.assertIn(
            "Ollama returned no categorization for 1 transaction(s)", warnings
        )
        self.assertEqual(
            [row["category"] for row in transactions], ["Unknown", "Unknown"]
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
                    "owner": "Household",
                    "confidence": "NaN",
                    "reason": "Not finite",
                }
            ],
        ]:
            with self.subTest(response=response):

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self) -> None:
                        length = int(self.headers["Content-Length"])
                        self.rfile.read(length)
                        body = {"response": json.dumps(response)}
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps(body).encode("utf-8"))

                    def log_message(self, format: str, *args: object) -> None:
                        return

                server = HTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self.addCleanup(server.shutdown)
                self.addCleanup(server.server_close)

                transaction = unresolved_transaction()
                report, warnings = apply_ollama_fallback(
                    [transaction],
                    {
                        "ollama": {
                            "enabled": True,
                            "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                        }
                    },
                )

                self.assertEqual(report["status"], "invalid_response")
                self.assertEqual(report["applied_count"], 0)
                self.assertEqual(report["invalid_count"], 1)
                self.assertEqual(warnings[0], "Ollama returned invalid categorizations")
                self.assertGreater(len(warnings), 1)
                self.assertIn("ollama_invalid_response", transaction["flags"])

    def test_http_error_body_is_surfaced_in_warning(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                body = json.dumps({"error": 'model "qwen2.5:7b-instruct" not found'})
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        transaction = unresolved_transaction()
        report, warnings = apply_ollama_fallback(
            [transaction],
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                }
            },
        )

        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(
            warnings,
            [
                "Ollama unavailable: HTTP 404 Not Found: "
                'model "qwen2.5:7b-instruct" not found'
            ],
        )
        self.assertIn("ollama_unavailable", transaction["flags"])

    def test_disallowed_category_is_named_in_warning(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                body = {
                    "response": json.dumps(
                        [
                            {
                                "id": "txn_1",
                                "category": "Ride Sharing",
                                "owner": "Household",
                                "confidence": 0.9,
                                "reason": "Uber trip",
                            }
                        ]
                    )
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode("utf-8"))

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        transaction = unresolved_transaction()
        report, warnings = apply_ollama_fallback(
            [transaction],
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                }
            },
        )

        self.assertEqual(report["status"], "invalid_response")
        self.assertIn(
            "Ollama categorization rejected (MYSTERY): "
            "category 'Ride Sharing' is not allowed",
            warnings,
        )

    def test_request_timeout_is_configurable(self) -> None:
        import time

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                time.sleep(1.5)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"response": "[]"}')

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        transaction = unresolved_transaction()
        report, warnings = apply_ollama_fallback(
            [transaction],
            {
                "ollama": {
                    "enabled": True,
                    "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                    "timeout_seconds": 0.2,
                }
            },
        )

        self.assertEqual(report["status"], "unavailable")
        self.assertIn("timed out", warnings[0])

    def test_progress_ticks_with_elapsed_time_while_waiting(self) -> None:
        import time
        from unittest.mock import patch

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                time.sleep(0.35)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"response": "[]"}')

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        transaction = unresolved_transaction()
        elapsed_readings = []

        with patch("honeymoney.ollama._TICK_INTERVAL_SECONDS", 0.1):
            apply_ollama_fallback(
                [transaction],
                {
                    "ollama": {
                        "enabled": True,
                        "url": f"http://127.0.0.1:{server.server_port}/api/generate",
                    }
                },
                progress=lambda event: elapsed_readings.append(event.elapsed_seconds),
            )

        self.assertEqual(elapsed_readings[0], 0.0)
        self.assertGreater(len(elapsed_readings), 1)
        self.assertTrue(
            all(a <= b for a, b in zip(elapsed_readings, elapsed_readings[1:]))
        )
        self.assertGreater(elapsed_readings[-1], 0.0)


if __name__ == "__main__":
    unittest.main()
