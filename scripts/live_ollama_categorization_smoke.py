from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from honeymoney.ollama import apply_ollama_fallback  # noqa: E402
from honeymoney.reconciliation import reconcile_ledger  # noqa: E402

BENCHMARK_PATH = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "categorization"
    / "ollama"
    / "live_benchmark.json"
)


def transaction(case: dict[str, str]) -> dict[str, str]:
    amount = case["amount_hkd"]
    return {
        "transaction_id": case["id"],
        "date": "2026-05-01",
        "transaction_date": "2026-05-01",
        "posting_date": "",
        "account_id": "live_smoke",
        "account": "Live Smoke",
        "account_type": case.get("account_type", "credit_card"),
        "institution": "Local",
        "country": "HK",
        "original_amount": amount,
        "original_currency": "HKD",
        "posted_amount": amount,
        "posted_currency": "HKD",
        "amount_hkd": amount,
        "merchant": case["merchant"],
        "original_description": case["merchant"],
        "category": "Unknown",
        "owner": case.get("owner", "Household"),
        "payment_method": case.get("payment_method", "Credit Card"),
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

    cases = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    rows = [transaction(case) for case in cases]
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
    reconciliation = reconcile_ledger(rows, config)
    per_case = []
    safety_passed = 0
    ordinary_correct = 0
    ordinary_total = 0
    for case, row in zip(cases, rows):
        safe = (
            row.get("flow_type") == case["expected_flow"]
            and row.get("needs_review") == case["expected_review"]
        )
        if safe:
            safety_passed += 1
        if case.get("ordinary_category"):
            ordinary_total += 1
            ordinary_correct += row.get("category") == case["ordinary_category"]
        per_case.append(
            {
                "id": case["id"],
                "category": row.get("category"),
                "flow_type": row.get("flow_type"),
                "needs_review": row.get("needs_review"),
                "safe": safe,
            }
        )
    safety = safety_passed / len(cases) if cases else 1.0
    accuracy = ordinary_correct / ordinary_total if ordinary_total else 1.0
    print(
        json.dumps(
            {
                "report": report,
                "warnings": warnings,
                "reconciliation": reconciliation,
                "total": len(cases),
                "safety_compliance": safety,
                "ordinary_accuracy": accuracy,
                "cases": per_case,
            },
            indent=2,
        )
    )

    if report.get("status") != "success":
        print(
            "Live Ollama smoke failed: categorization did not succeed.", file=sys.stderr
        )
        return 1
    if safety < 1.0 or accuracy < 0.9:
        print(
            "Live Ollama smoke failed: safety must be 100% and ordinary accuracy at least 90%.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
