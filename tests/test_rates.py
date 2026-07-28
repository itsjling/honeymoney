import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from honeymoney.corrections import CORRECTION_COLUMNS
from honeymoney.csv_artifacts import csv_document
from honeymoney.identity_state import load_configured_identity_state
from honeymoney.rates import (
    HKMA_PROVIDER,
    RateImportError,
    empty_rate_cache,
    merge_rate_cache,
    parse_hkma_daily_document,
    rate_cache_document,
    resolve_cached_rate,
    validate_rate_cache,
)
from honeymoney.schema import SOURCE_OCCURRENCE_COLUMNS
from honeymoney.valuation import value_transaction

REPO_ROOT = Path(__file__).resolve().parents[1]
FAULT_HOOK = REPO_ROOT / "tests" / "fault_injection"


def _hkma_document(
    records: list[dict[str, object]],
    *,
    datasize: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "header": {
                "success": True,
                "err_code": "0000",
                "err_msg": "No error found",
            },
            "result": {
                "datasize": len(records) if datasize is None else datasize,
                "records": records,
            },
        },
        sort_keys=True,
    ).encode()


class RateCacheTest(unittest.TestCase):
    def test_official_document_normalizes_supported_positive_rates(self) -> None:
        content = _hkma_document(
            [
                {
                    "end_of_day": "2026-07-03",
                    "eur": 9.25,
                    "idr": 0.000438,
                    "jpy": 0.048325,
                    "krw": 0.005065,
                    "usd": 7.8,
                    "unsupported": 2,
                }
            ]
        )

        observations = parse_hkma_daily_document(content, base_currency="hkd")

        self.assertEqual(
            [
                (
                    item["quote_currency"],
                    item["observed_rate_date"],
                    item["raw_rate"],
                    item["provider"],
                )
                for item in observations
            ],
            [
                ("EUR", "2026-07-03", "9.25", HKMA_PROVIDER),
                ("IDR", "2026-07-03", "0.000438", HKMA_PROVIDER),
                ("JPY", "2026-07-03", "0.048325", HKMA_PROVIDER),
                ("KRW", "2026-07-03", "0.005065", HKMA_PROVIDER),
                ("USD", "2026-07-03", "7.8", HKMA_PROVIDER),
            ],
        )
        self.assertTrue(
            all(len(item["import_provenance"][0]) == 64 for item in observations)
        )

    def test_official_document_rejects_bad_shape_direction_and_values(self) -> None:
        cases = [
            (
                _hkma_document([{"end_of_day": "bad", "eur": 9.25}]),
                "HKD",
                "hkma_date_invalid",
            ),
            (
                _hkma_document([{"end_of_day": "2026-07-03", "eur": -1}]),
                "HKD",
                "hkma_rate_invalid",
            ),
            (
                _hkma_document(
                    [
                        {
                            "end_of_day": "2026-07-03",
                            "eur": None,
                            "usd": 7.8,
                        }
                    ]
                ),
                "HKD",
                "hkma_rate_invalid",
            ),
            (
                _hkma_document(
                    [
                        {"end_of_day": "2026-07-03", "eur": 9.25},
                        {"end_of_day": "2026-07-03", "eur": 9.25},
                    ]
                ),
                "HKD",
                "hkma_duplicate_observation",
            ),
            (
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}]),
                "USD",
                "rate_direction_unsupported",
            ),
            (
                _hkma_document(
                    [{"end_of_day": "2026-07-03", "eur": 9.25}],
                    datasize=2,
                ),
                "HKD",
                "hkma_document_invalid",
            ),
        ]
        for content, base_currency, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(RateImportError) as raised:
                    parse_hkma_daily_document(
                        content,
                        base_currency=base_currency,
                    )
                self.assertEqual(raised.exception.code, code)

        with self.assertRaises(RateImportError) as non_finite:
            parse_hkma_daily_document(
                b'{"header":{"success":true,"err_code":"0000"},'
                b'"result":{"datasize":1,"records":['
                b'{"end_of_day":"2026-07-03","eur":NaN}]}}',
                base_currency="HKD",
            )
        self.assertEqual(non_finite.exception.code, "hkma_document_invalid")

    def test_cache_uses_exact_then_recent_prior_but_never_future_or_stale(self) -> None:
        observations = parse_hkma_daily_document(
            _hkma_document(
                [
                    {"end_of_day": "2026-07-02", "eur": 9.2},
                    {"end_of_day": "2026-07-03", "eur": 9.25},
                    {"end_of_day": "2026-07-06", "eur": 9.3},
                ]
            ),
            base_currency="HKD",
        )
        cache = merge_rate_cache(
            empty_rate_cache(),
            observations,
            [
                ("EUR", "2026-07-03"),
                ("EUR", "2026-07-05"),
                ("EUR", "2026-06-30"),
                ("EUR", "2026-07-14"),
            ],
        )

        exact = resolve_cached_rate(cache, "EUR", "2026-07-03")
        weekend = resolve_cached_rate(cache, "EUR", "2026-07-05")
        self.assertIsNotNone(exact)
        self.assertIsNotNone(weekend)
        self.assertEqual(exact["observed_rate_date"], "2026-07-03")  # type: ignore[index]
        self.assertEqual(weekend["observed_rate_date"], "2026-07-03")  # type: ignore[index]
        self.assertIsNone(resolve_cached_rate(cache, "EUR", "2026-06-30"))
        self.assertIsNone(resolve_cached_rate(cache, "EUR", "2026-07-14"))
        self.assertEqual(len(cache["resolutions"]), 2)

    def test_cache_merge_is_deterministic_and_rejects_conflicts_or_tampering(
        self,
    ) -> None:
        first_document = _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}])
        observations = parse_hkma_daily_document(
            first_document,
            base_currency="HKD",
        )
        first = merge_rate_cache(
            empty_rate_cache(),
            observations,
            [("EUR", "2026-07-05")],
        )
        second = merge_rate_cache(
            first,
            observations,
            [("EUR", "2026-07-05")],
        )
        self.assertEqual(rate_cache_document(first), rate_cache_document(second))

        equivalent = parse_hkma_daily_document(
            first_document.replace(b"9.25", b"9.2500"),
            base_currency="HKD",
        )
        numerically_merged = merge_rate_cache(first, equivalent, [])
        self.assertEqual(
            numerically_merged["observations"][0]["raw_rate"],
            "9.25",
        )
        self.assertEqual(
            len(numerically_merged["observations"][0]["import_provenance"]),
            2,
        )

        conflict = parse_hkma_daily_document(
            _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.26}]),
            base_currency="HKD",
        )
        with self.assertRaises(RateImportError) as raised:
            merge_rate_cache(first, conflict, [])
        self.assertEqual(raised.exception.code, "hkma_observation_conflict")

        tampered = deepcopy(first)
        tampered["resolutions"][0]["raw_rate"] = "1"
        with self.assertRaises(RateImportError) as invalid:
            validate_rate_cache(tampered)
        self.assertEqual(invalid.exception.code, "rate_cache_invalid")

    def test_valuation_precedence_and_rate_metadata(self) -> None:
        cache = merge_rate_cache(
            empty_rate_cache(),
            parse_hkma_daily_document(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}]),
                base_currency="HKD",
            ),
            [("EUR", "2026-07-05")],
        )
        row = {
            "date": "2026-07-05",
            "posted_amount": "-10",
            "posted_currency": "EUR",
            "amount_hkd": "",
            "flags": "",
        }
        config = {
            "base_currency": "HKD",
            "exchange_rates": {"EUR": 8.0},
            "_rate_cache": cache,
        }

        value_transaction(row, config)

        self.assertEqual(row["amount_hkd"], "-92.50")
        self.assertEqual(row["valuation_source"], "hkma_daily_reference_rate")
        self.assertEqual(row["valuation_status"], "estimated")
        self.assertEqual(row["valuation_rate_date"], "2026-07-03")
        self.assertEqual(row["valuation_provider"], HKMA_PROVIDER)

        row["amount_hkd"] = ""
        config["dated_exchange_rates"] = {"EUR": {"2026-07-05": Decimal("9.5")}}
        value_transaction(row, config)
        self.assertEqual(row["amount_hkd"], "-95.00")
        self.assertEqual(row["valuation_source"], "configured_dated_rate")


class RateImportCliTest(unittest.TestCase):
    def _run_cli(
        self,
        args: list[str],
        *,
        cwd: Path,
        fault: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        python_paths = [str(REPO_ROOT)]
        if fault:
            python_paths.insert(0, str(FAULT_HOOK))
            env["HONEYMONEY_TEST_FS_FAULT"] = fault
        env["PYTHONPATH"] = os.pathsep.join(python_paths)
        return subprocess.run(
            [sys.executable, "-m", "honeymoney.cli", *args],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_legacy_config_uses_stable_workspace_rate_cache_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.pop("rate_cache")
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            cache_path = root / "rates.json"
            cache_path.unlink()
            legacy_config_bytes = config_path.read_bytes()
            provider_path = root / "hkma.json"
            provider_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}])
            )

            imported = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=REPO_ROOT,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            payload = json.loads(imported.stdout)
            self.assertEqual(
                payload["data"]["rate_cache"],
                {
                    "defaulted": True,
                    "path": str(cache_path.resolve()),
                },
            )
            self.assertEqual(
                payload["artifacts"]["rate_cache_json"],
                str(cache_path.resolve()),
            )
            self.assertEqual(config_path.read_bytes(), legacy_config_bytes)
            self.assertTrue(cache_path.exists())
            stable_cache_bytes = cache_path.read_bytes()

            repeated = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                ],
                cwd=REPO_ROOT,
            )

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn(
                f"Rate cache: {cache_path.resolve()} (default)", repeated.stdout
            )
            self.assertEqual(cache_path.read_bytes(), stable_cache_bytes)
            self.assertEqual(config_path.read_bytes(), legacy_config_bytes)

            (root / "input" / "foreign.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-05,SYNTHETIC LEGACY CACHE PURCHASE,-10.00,EUR\n",
                encoding="utf-8",
            )
            first_run = self._run_cli(
                [
                    "run",
                    "--config",
                    str(config_path),
                    "--no-interactive",
                    "--json",
                ],
                cwd=REPO_ROOT,
            )
            self.assertEqual(first_run.returncode, 0, first_run.stderr)
            ledger_path = root / "output" / "categorized.csv"
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                [ledger_row] = list(csv.DictReader(handle))
            self.assertEqual(ledger_row["amount_hkd"], "-92.50")
            self.assertEqual(
                len(json.loads(cache_path.read_text(encoding="utf-8"))["resolutions"]),
                1,
            )
            first_reconcile = self._run_cli(
                [
                    "reconcile",
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=REPO_ROOT,
            )
            self.assertEqual(first_reconcile.returncode, 0, first_reconcile.stderr)
            stable_generation = {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            second_run = self._run_cli(
                [
                    "reconcile",
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=REPO_ROOT,
            )
            self.assertEqual(second_run.returncode, 0, second_run.stderr)
            self.assertEqual(
                {
                    str(path.relative_to(root)): path.read_bytes()
                    for path in sorted(root.rglob("*"))
                    if path.is_file()
                },
                stable_generation,
            )

            provider_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25, "usd": 7.8}])
            )
            before_failed_import = {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            failed = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=REPO_ROOT,
                fault="replace-before:categorized.csv",
            )
            self.assertEqual(failed.returncode, 2)
            self.assertEqual(
                {
                    str(path.relative_to(root)): path.read_bytes()
                    for path in sorted(root.rglob("*"))
                    if path.is_file()
                },
                before_failed_import,
            )

    def test_explicit_rate_cache_path_takes_precedence_over_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            custom_path = root / "local-cache" / "official-rates.json"
            custom_path.parent.mkdir()
            config["rate_cache"] = str(custom_path)
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            provider_path = root / "hkma.json"
            provider_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}])
            )

            imported = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=REPO_ROOT,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(
                json.loads(imported.stdout)["data"]["rate_cache"],
                {
                    "defaulted": False,
                    "path": str(custom_path.resolve()),
                },
            )
            self.assertTrue(custom_path.exists())
            self.assertEqual(
                json.loads((root / "rates.json").read_text(encoding="utf-8")),
                empty_rate_cache(),
            )

    def test_import_revalues_and_persists_as_one_repeatable_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            (root / "input" / "foreign.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-05,SYNTHETIC WEEKEND PURCHASE,-10.00,EUR\n",
                encoding="utf-8",
            )
            imported = self._run_cli(
                [
                    "run",
                    "--config",
                    str(config_path),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            ledger_path = root / "output" / "categorized.csv"
            cache_path = root / "rates.json"
            source_path = root / "output" / ".honeymoney-source-occurrences.csv"
            before = {
                path: path.read_bytes()
                for path in (ledger_path, cache_path, source_path)
            }
            provider_path = root / "hkma.json"
            provider_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}])
            )

            failed = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=root,
                fault="replace-before:categorized.csv",
            )

            self.assertEqual(failed.returncode, 2)
            self.assertEqual(
                before,
                {
                    path: path.read_bytes()
                    for path in (ledger_path, cache_path, source_path)
                },
            )

            result = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["command"], "rates.import")
            self.assertEqual(payload["data"]["resolved_transaction_date_count"], 1)
            self.assertEqual(payload["data"]["valued_transaction_count"], 1)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["original_amount"], "-10.00")
            self.assertEqual(row["posted_amount"], "-10.00")
            self.assertEqual(row["amount_hkd"], "-92.50")
            self.assertEqual(row["valuation_source"], "hkma_daily_reference_rate")
            self.assertEqual(row["valuation_status"], "estimated")
            self.assertEqual(row["valuation_rate_date"], "2026-07-03")
            self.assertEqual(row["valuation_provider"], HKMA_PROVIDER)
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(
                cache["resolutions"][0]["requested_transaction_date"],
                "2026-07-05",
            )

            stable = {
                path: path.read_bytes()
                for path in (ledger_path, cache_path, source_path)
            }
            repeated = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["data"]["changed"])
            self.assertEqual(
                stable,
                {
                    path: path.read_bytes()
                    for path in (ledger_path, cache_path, source_path)
                },
            )

            reconciled = self._run_cli(
                ["reconcile", "--config", str(config_path), "--json"],
                cwd=root,
            )
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            with ledger_path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(next(csv.DictReader(handle))["amount_hkd"], "-92.50")

            report_path = root / "output" / "rates-report.html"
            report = self._run_cli(
                [
                    "report",
                    "--month",
                    "2026-07",
                    "--config",
                    str(config_path),
                    "--output",
                    str(report_path),
                    "--no-open",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            html = report_path.read_text(encoding="utf-8")
            self.assertIn("-10.00", html)
            self.assertIn("-92.5", html)
            self.assertIn("2026-07-03", html)
            self.assertIn(HKMA_PROVIDER, html)
            self.assertIn("HKMA reference estimate", html)

    def test_import_before_ledger_records_later_transaction_resolutions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            provider_path = root / "hkma.json"
            provider_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}])
            )
            rates = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(rates.returncode, 0, rates.stderr)
            self.assertEqual(
                json.loads((root / "rates.json").read_text(encoding="utf-8"))[
                    "resolutions"
                ],
                [],
            )
            (root / "input" / "later.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-05,SYNTHETIC LATER PURCHASE,-10.00,EUR\n",
                encoding="utf-8",
            )

            imported = self._run_cli(
                [
                    "run",
                    "--config",
                    str(config_path),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(imported.returncode, 0, imported.stderr)
            cache = json.loads((root / "rates.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [
                    (
                        item["quote_currency"],
                        item["requested_transaction_date"],
                        item["observed_rate_date"],
                    )
                    for item in cache["resolutions"]
                ],
                [("EUR", "2026-07-05", "2026-07-03")],
            )

    def test_rate_import_projects_corrections_during_canonical_migration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            config_path = root / "config.json"
            (root / "input" / "legacy.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-05,SYNTHETIC LEGACY PURCHASE,-10.00,EUR\n",
                encoding="utf-8",
            )
            imported = self._run_cli(
                [
                    "run",
                    "--config",
                    str(config_path),
                    "--no-interactive",
                    "--json",
                ],
                cwd=root,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            state = load_configured_identity_state(
                Path(config["paths"]["output"]),
                config,
            )
            [source_row] = state.source_rows
            categorized_path = root / "output" / "categorized.csv"
            categorized_path.write_text(
                csv_document(SOURCE_OCCURRENCE_COLUMNS, state.source_rows),
                encoding="utf-8",
            )
            (root / "output" / ".honeymoney-source-occurrences.csv").unlink()
            (root / "output" / ".honeymoney-overlap-manifest.json").unlink()
            (root / "corrections.csv").write_text(
                csv_document(
                    CORRECTION_COLUMNS,
                    [
                        {
                            "transaction_id": source_row["transaction_id"],
                            "category": "Dining",
                            "flow_type": "expense",
                            "confidence": "1.00",
                            "reason": "Synthetic legacy correction",
                            "needs_review": "false",
                            "review_reasons": "",
                        }
                    ],
                ),
                encoding="utf-8",
            )
            provider_path = root / "hkma.json"
            provider_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": 9.25}])
            )

            rates = self._run_cli(
                [
                    "rates",
                    "import",
                    str(provider_path),
                    "--config",
                    str(config_path),
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(rates.returncode, 0, rates.stderr)
            with categorized_path.open(newline="", encoding="utf-8") as handle:
                [ledger_row] = list(csv.DictReader(handle))
            self.assertEqual(ledger_row["category"], "Dining")
            self.assertEqual(ledger_row["flow_type"], "expense")
            self.assertEqual(ledger_row["amount_hkd"], "-92.50")
            with (root / "corrections.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                [correction] = list(csv.DictReader(handle))
            self.assertEqual(
                correction["transaction_id"],
                ledger_row["transaction_id"],
            )
            self.assertNotEqual(
                correction["transaction_id"],
                source_row["transaction_id"],
            )

    def test_invalid_import_leaves_cache_and_ledger_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            setup = self._run_cli(
                ["setup", "--root", str(root), "--json"],
                cwd=REPO_ROOT,
            )
            self.assertEqual(setup.returncode, 0, setup.stderr)
            cache_path = root / "rates.json"
            ledger_path = root / "output" / "categorized.csv"
            bad_path = root / "bad-hkma.json"
            bad_path.write_bytes(
                _hkma_document([{"end_of_day": "2026-07-03", "eur": -1}])
            )
            before = cache_path.read_bytes()

            result = self._run_cli(
                [
                    "rates",
                    "import",
                    str(bad_path),
                    "--config",
                    str(root / "config.json"),
                    "--json",
                ],
                cwd=root,
            )

            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["command"], "rates.import")
            self.assertEqual(payload["errors"][0]["code"], "hkma_rate_invalid")
            self.assertEqual(cache_path.read_bytes(), before)
            self.assertFalse(ledger_path.exists())
