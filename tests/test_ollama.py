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
        prompt = json.loads(captured_requests[0]["prompt"])
        sent_transaction = prompt["transactions"][0]
        self.assertNotIn("source_file", sent_transaction)
        self.assertNotIn("notes", sent_transaction)

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
        self.assertEqual([row["category"] for row in transactions], ["Dining", "Dining"])

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
        self.assertEqual(warnings, ["Ollama returned invalid categorizations"])
        self.assertEqual([row["category"] for row in transactions], ["Unknown", "Unknown"])
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
                self.assertEqual(warnings, ["Ollama returned invalid categorizations"])
                self.assertIn("ollama_invalid_response", transaction["flags"])


if __name__ == "__main__":
    unittest.main()
