from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from honeymoney.ollama import apply_ollama_fallback  # noqa: E402


def transaction(transaction_id: str, merchant: str, amount: str) -> dict[str, str]:
    return {
        "transaction_id": transaction_id,
        "date": "2026-05-01",
        "transaction_date": "2026-05-01",
        "posting_date": "",
        "account_id": "live_smoke",
        "account": "Live Smoke",
        "institution": "Local",
        "country": "HK",
        "original_amount": amount,
        "original_currency": "HKD",
        "posted_amount": amount,
        "posted_currency": "HKD",
        "amount_hkd": amount,
        "merchant": merchant,
        "original_description": merchant,
        "category": "Unknown",
        "owner": "Household",
        "payment_method": "Credit Card",
        "confidence": "0.00",
        "needs_review": "true",
        "reason": "No categorization rules have been applied",
        "flags": "uncategorized",
        "notes": "",
        "source_file": "live-smoke.csv",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a live local Ollama categorization smoke test."
    )
    parser.parse_args()

    rows = [
        transaction("txn_live_1", "PARKNSHOP", "-120.50"),
        transaction("txn_live_2", "MTR", "-12.00"),
    ]
    config = {
        "ollama": {
            "enabled": True,
            "url": os.environ.get(
                "HONEYMONEY_OLLAMA_URL", "http://localhost:11434/api/generate"
            ),
            "model": os.environ.get("HONEYMONEY_OLLAMA_MODEL", "qwen2.5:7b-instruct"),
            "batch_size": 2,
            "timeout_seconds": float(
                os.environ.get("HONEYMONEY_OLLAMA_TIMEOUT", "120")
            ),
        }
    }

    report, warnings = apply_ollama_fallback(rows, config)
    print(json.dumps({"report": report, "warnings": warnings, "rows": rows}, indent=2))

    if report.get("status") != "success":
        print(
            "Live Ollama smoke failed: categorization did not succeed.", file=sys.stderr
        )
        return 1
    if all(row.get("category") == "Unknown" for row in rows):
        print(
            "Live Ollama smoke failed: all rows remained uncategorized.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
