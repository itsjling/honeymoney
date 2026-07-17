"""Subprocess-only fake Ollama transport for offline CLI tests."""

import json
import os

from honeymoney.ollama import LoopbackOllamaTransport


def _request(self, request):
    del self
    mode = os.environ.get("HONEYMONEY_TEST_OLLAMA_MODE")
    if not mode:
        raise AssertionError("offline Ollama hook requires an explicit test mode")
    if mode == "unavailable":
        raise ConnectionError("synthetic Ollama unavailable")
    if mode == "invalid":
        categorizations = [
            {
                "id": "not-the-transaction-id",
                "category": "Review Needed",
                "confidence": 1.5,
                "reason": "Bad response",
            }
        ]
    else:
        body = json.loads(request.body)
        transactions = json.loads(body["prompt"])["transactions"]
        categorizations = []
        for transaction in transactions:
            category = "Dining"
            if (
                mode == "accounting"
                and transaction["merchant"] == "UNIDENTIFIED BANK CREDIT"
            ):
                category = "Credit Card Payment"
            categorizations.append(
                {
                    "id": transaction["id"],
                    "category": category,
                    "confidence": 0.91 if mode == "mixed" else 0.86,
                    "reason": (
                        "Local model matched dining-like transaction"
                        if mode == "mixed"
                        else "Synthetic loopback response"
                    ),
                }
            )
    return json.dumps({"response": json.dumps(categorizations)}).encode()


LoopbackOllamaTransport.request = _request
