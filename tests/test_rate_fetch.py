import csv
import io
import json
import socket
import ssl
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import BadStatusLine, IncompleteRead, LineTooLong
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlsplit

from honeymoney.cli import main, run
from honeymoney.rate_fetch import (
    HKMA_API_ENDPOINT,
    HKMA_MAX_RESPONSE_BYTES,
    RateFetchError,
    RateFetchRequest,
    RateFetchResult,
    _https_get,
    build_hkma_request_url,
    fetch_hkma_daily_rates,
    prepare_hkma_fetch,
)
from honeymoney.rates import parse_hkma_daily_document


def _document(records: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {
            "header": {
                "success": True,
                "err_code": "0000",
                "err_msg": "No error found",
            },
            "result": {
                "datasize": len(records),
                "records": records,
            },
        },
        sort_keys=True,
    ).encode()


class _InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class RateFetchBoundaryTest(unittest.TestCase):
    def test_request_contains_only_public_rate_query_fields(self) -> None:
        request = prepare_hkma_fetch(
            ["usd", "EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )

        url = build_hkma_request_url(request, offset=0)

        self.assertTrue(url.startswith(HKMA_API_ENDPOINT + "?"))
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(
            query,
            {
                "pagesize": ["1000"],
                "offset": ["0"],
                "fields": ["end_of_day,eur,usd"],
                "choose": ["end_of_day"],
                "from": ["2026-07-01"],
                "to": ["2026-07-03"],
                "sortby": ["end_of_day"],
                "sortorder": ["asc"],
            },
        )
        lowered = url.casefold()
        for forbidden in (
            "transaction",
            "amount",
            "description",
            "account",
            "statement",
            "source_file",
            "merchant",
            "prompt",
            "private-sentinel",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_http_boundary_rejects_other_hosts_and_extra_query_data(self) -> None:
        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        valid_url = build_hkma_request_url(request, offset=0)
        connection = Mock()
        with (
            patch("honeymoney.rate_fetch.HTTPSConnection", connection),
            self.assertRaises(RateFetchError) as other_host,
        ):
            _https_get(
                valid_url.replace("api.hkma.gov.hk", "private.example"),
                1,
            )
        self.assertEqual(other_host.exception.code, "rate_fetch_request_invalid")
        connection.assert_not_called()

        with (
            patch("honeymoney.rate_fetch.HTTPSConnection", connection),
            self.assertRaises(RateFetchError) as extra_data,
        ):
            _https_get(valid_url + "&amount=100", 1)
        self.assertEqual(extra_data.exception.code, "rate_fetch_request_invalid")
        connection.assert_not_called()

    def test_http_boundary_reads_one_bounded_success_and_closes(self) -> None:
        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        url = build_hkma_request_url(request, offset=0)
        response = Mock(status=200)
        response.read.return_value = b"checked response"
        connection = Mock()
        connection.getresponse.return_value = response

        with patch(
            "honeymoney.rate_fetch.HTTPSConnection",
            return_value=connection,
        ) as constructor:
            content = _https_get(url, 4)

        self.assertEqual(content, b"checked response")
        constructor.assert_called_once()
        self.assertEqual(constructor.call_args.args, ("api.hkma.gov.hk",))
        self.assertEqual(constructor.call_args.kwargs["timeout"], 4)
        context = constructor.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        connection.request.assert_called_once()
        request_args = connection.request.call_args
        self.assertEqual(request_args.args[0], "GET")
        self.assertTrue(request_args.args[1].startswith("/public/"))
        self.assertEqual(
            request_args.kwargs["headers"],
            {
                "Accept": "application/json",
                "User-Agent": "honeymoney-public-rate-fetch/1",
            },
        )
        response.read.assert_called_once_with(HKMA_MAX_RESPONSE_BYTES + 1)
        connection.close.assert_called_once()

    def test_http_boundary_uses_system_and_packaged_verified_trust(self) -> None:
        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        url = build_hkma_request_url(request, offset=0)
        response = Mock(status=200)
        response.read.return_value = b"checked response"
        connection = Mock()
        connection.getresponse.return_value = response
        context = Mock()
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED

        with (
            patch(
                "honeymoney.rate_fetch.ssl.create_default_context",
                return_value=context,
            ) as create_context,
            patch(
                "honeymoney.rate_fetch.certifi.where",
                return_value="/synthetic/certifi.pem",
            ),
            patch(
                "honeymoney.rate_fetch.HTTPSConnection",
                return_value=connection,
            ) as constructor,
        ):
            self.assertEqual(_https_get(url, 4), b"checked response")

        create_context.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(
            cafile="/synthetic/certifi.pem"
        )
        constructor.assert_called_once_with(
            "api.hkma.gov.hk",
            timeout=4,
            context=context,
        )
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_http_boundary_falls_back_to_packaged_verified_trust(self) -> None:
        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        url = build_hkma_request_url(request, offset=0)
        response = Mock(status=200)
        response.read.return_value = b"checked response"
        connection = Mock()
        connection.getresponse.return_value = response
        system_context = Mock()
        system_context.load_verify_locations.side_effect = ssl.SSLError(
            "synthetic system trust failure"
        )
        packaged_context = Mock()
        packaged_context.check_hostname = True
        packaged_context.verify_mode = ssl.CERT_REQUIRED

        with (
            patch(
                "honeymoney.rate_fetch.ssl.create_default_context",
                side_effect=[system_context, packaged_context],
            ) as create_context,
            patch(
                "honeymoney.rate_fetch.certifi.where",
                return_value="/synthetic/certifi.pem",
            ),
            patch(
                "honeymoney.rate_fetch.HTTPSConnection",
                return_value=connection,
            ) as constructor,
        ):
            self.assertEqual(_https_get(url, 4), b"checked response")

        self.assertEqual(
            create_context.call_args_list,
            [
                call(),
                call(cafile="/synthetic/certifi.pem"),
            ],
        )
        self.assertIs(constructor.call_args.kwargs["context"], packaged_context)
        self.assertTrue(packaged_context.check_hostname)
        self.assertEqual(packaged_context.verify_mode, ssl.CERT_REQUIRED)

    def test_http_boundary_wraps_status_size_and_transport_failures(self) -> None:
        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        url = build_hkma_request_url(request, offset=0)

        error_response = Mock(status=503)
        failed_connection = Mock()
        failed_connection.getresponse.return_value = error_response
        with (
            patch(
                "honeymoney.rate_fetch.HTTPSConnection",
                return_value=failed_connection,
            ),
            self.assertRaises(RateFetchError) as status_error,
        ):
            _https_get(url, 1)
        self.assertEqual(status_error.exception.code, "rate_fetch_http_status")
        failed_connection.close.assert_called_once()

        large_response = Mock(status=200)
        large_response.read.return_value = b"x" * 4
        large_connection = Mock()
        large_connection.getresponse.return_value = large_response
        with (
            patch("honeymoney.rate_fetch.HKMA_MAX_RESPONSE_BYTES", 3),
            patch(
                "honeymoney.rate_fetch.HTTPSConnection",
                return_value=large_connection,
            ),
            self.assertRaises(RateFetchError) as large_error,
        ):
            _https_get(url, 1)
        self.assertEqual(
            large_error.exception.code,
            "rate_fetch_response_too_large",
        )
        large_connection.close.assert_called_once()

        failures = (
            (
                ssl.SSLCertVerificationError(1, "PRIVATE CERTIFICATE DETAIL"),
                "rate_fetch_certificate_verification",
            ),
            (
                socket.gaierror(-2, "PRIVATE DNS DETAIL"),
                "rate_fetch_name_resolution",
            ),
            (TimeoutError("PRIVATE TIMEOUT DETAIL"), "rate_fetch_timeout"),
            (
                ConnectionRefusedError("PRIVATE CONNECTION DETAIL"),
                "rate_fetch_connection",
            ),
            (
                BadStatusLine("PRIVATE STATUS DETAIL"),
                "rate_fetch_response_malformed",
            ),
            (
                LineTooLong("PRIVATE HEADER DETAIL"),
                "rate_fetch_response_malformed",
            ),
            (
                IncompleteRead(b"PRIVATE BODY DETAIL", 100),
                "rate_fetch_response_malformed",
            ),
        )
        for raised, code in failures:
            with self.subTest(code=code):
                broken_connection = Mock()
                broken_connection.request.side_effect = raised
                with (
                    patch(
                        "honeymoney.rate_fetch.HTTPSConnection",
                        return_value=broken_connection,
                    ),
                    self.assertRaises(RateFetchError) as transport_error,
                ):
                    _https_get(url, 1)
                self.assertEqual(transport_error.exception.code, code)
                self.assertNotIn("PRIVATE", str(transport_error.exception))
                broken_connection.close.assert_called_once()

    def test_mocked_pages_are_complete_ordered_and_filtered(self) -> None:
        request = prepare_hkma_fetch(
            ["USD", "EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        calls: list[tuple[str, float]] = []
        pages = [
            _document(
                [
                    {"end_of_day": "2026-07-01", "eur": 9.2, "usd": 7.8},
                    {"end_of_day": "2026-07-02", "eur": 9.3, "usd": 7.81},
                ]
            ),
            _document([{"end_of_day": "2026-07-03", "eur": 9.4, "usd": 7.82}]),
        ]

        def transport(url: str, timeout: float) -> bytes:
            calls.append((url, timeout))
            return pages[len(calls) - 1]

        result = fetch_hkma_daily_rates(
            request,
            transport=transport,
            page_size=2,
            timeout_seconds=3,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [parse_qs(urlsplit(url).query)["offset"] for url, _ in calls],
            [["0"], ["2"]],
        )
        self.assertEqual({timeout for _, timeout in calls}, {3})
        self.assertEqual(len(result.observations), 6)
        self.assertEqual(
            {
                (item["quote_currency"], item["observed_rate_date"])
                for item in result.observations
            },
            {
                ("EUR", "2026-07-01"),
                ("EUR", "2026-07-02"),
                ("EUR", "2026-07-03"),
                ("USD", "2026-07-01"),
                ("USD", "2026-07-02"),
                ("USD", "2026-07-03"),
            },
        )
        self.assertEqual(result.request_urls, tuple(url for url, _ in calls))

    def test_timeout_bad_second_page_and_page_overlap_fail(self) -> None:
        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )

        def timeout(_url: str, _seconds: float) -> bytes:
            raise TimeoutError

        with self.assertRaises(RateFetchError) as timed_out:
            fetch_hkma_daily_rates(request, transport=timeout)
        self.assertEqual(timed_out.exception.code, "rate_fetch_timeout")

        first_page = _document(
            [
                {"end_of_day": "2026-07-01", "eur": 9.2},
                {"end_of_day": "2026-07-02", "eur": 9.3},
            ]
        )
        malformed = Mock(side_effect=[first_page, b"not-json"])
        with self.assertRaises(RateFetchError) as bad_response:
            fetch_hkma_daily_rates(
                request,
                transport=malformed,
                page_size=2,
            )
        self.assertEqual(
            bad_response.exception.code,
            "rate_fetch_response_malformed",
        )

        overlapping = Mock(
            side_effect=[
                first_page,
                _document([{"end_of_day": "2026-07-02", "eur": 9.3}]),
            ]
        )
        with self.assertRaises(RateFetchError) as bad_pages:
            fetch_hkma_daily_rates(
                request,
                transport=overlapping,
                page_size=2,
            )
        self.assertEqual(
            bad_pages.exception.code,
            "rate_fetch_pagination_incomplete",
        )

        reversed_pages = Mock(
            side_effect=[
                _document(
                    [
                        {"end_of_day": "2026-07-02", "eur": 9.3},
                        {"end_of_day": "2026-07-03", "eur": 9.4},
                    ]
                ),
                _document([{"end_of_day": "2026-07-01", "eur": 9.2}]),
            ]
        )
        with self.assertRaises(RateFetchError) as reversed_order:
            fetch_hkma_daily_rates(
                request,
                transport=reversed_pages,
                page_size=2,
            )
        self.assertEqual(
            reversed_order.exception.code,
            "rate_fetch_pagination_incomplete",
        )

        unordered = Mock(
            return_value=_document(
                [
                    {"end_of_day": "2026-07-02", "eur": 9.3},
                    {"end_of_day": "2026-07-01", "eur": 9.2},
                ]
            )
        )
        with self.assertRaises(RateFetchError) as bad_order:
            fetch_hkma_daily_rates(
                request,
                transport=unordered,
                page_size=3,
            )
        self.assertEqual(
            bad_order.exception.code,
            "rate_fetch_pagination_incomplete",
        )

        outside = Mock(
            return_value=_document([{"end_of_day": "2026-07-04", "eur": 9.4}])
        )
        with self.assertRaises(RateFetchError) as bad_date:
            fetch_hkma_daily_rates(
                request,
                transport=outside,
                page_size=3,
            )
        self.assertEqual(bad_date.exception.code, "rate_fetch_response_malformed")

        partial_request = prepare_hkma_fetch(
            ["EUR", "USD"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        partial = Mock(
            return_value=_document([{"end_of_day": "2026-07-01", "eur": 9.2}])
        )
        with self.assertRaises(RateFetchError) as incomplete_currency:
            fetch_hkma_daily_rates(
                partial_request,
                transport=partial,
                page_size=3,
            )
        self.assertEqual(
            incomplete_currency.exception.code,
            "rate_fetch_response_malformed",
        )

    def test_invalid_range_currency_and_timeout_stop_before_transport(self) -> None:
        for currencies, start, end, code in (
            ([], "2026-07-01", "2026-07-03", "rate_fetch_currency_required"),
            (["HKD"], "2026-07-01", "2026-07-03", "rate_fetch_currency_unsupported"),
            (["EUR"], "bad", "2026-07-03", "rate_fetch_range_invalid"),
            (["EUR"], "2026-07-04", "2026-07-03", "rate_fetch_range_invalid"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(RateFetchError) as raised:
                    prepare_hkma_fetch(
                        currencies,
                        start=start,
                        end=end,
                        base_currency="HKD",
                    )
                self.assertEqual(raised.exception.code, code)

        with self.assertRaises(RateFetchError) as wrong_base:
            prepare_hkma_fetch(
                ["EUR"],
                start="2026-07-01",
                end="2026-07-03",
                base_currency="USD",
            )
        self.assertEqual(wrong_base.exception.code, "rate_direction_unsupported")

        normalized = prepare_hkma_fetch(
            ["EUR"],
            start="20260701",
            end="20260703",
            base_currency="HKD",
        )
        self.assertEqual(normalized.start, "2026-07-01")
        self.assertEqual(normalized.end, "2026-07-03")

        request = prepare_hkma_fetch(
            ["EUR"],
            start="2026-07-01",
            end="2026-07-03",
            base_currency="HKD",
        )
        transport = Mock()
        with self.assertRaises(RateFetchError) as invalid_timeout:
            fetch_hkma_daily_rates(
                request,
                transport=transport,
                timeout_seconds=0,
            )
        self.assertEqual(
            invalid_timeout.exception.code,
            "rate_fetch_timeout_invalid",
        )
        transport.assert_not_called()

        with self.assertRaises(RateFetchError) as bad_page:
            build_hkma_request_url(request, offset=-1)
        self.assertEqual(bad_page.exception.code, "rate_fetch_pagination_invalid")

        altered_request = RateFetchRequest(
            currencies=("EUR",),
            start="2026-07-01",
            end="2026-07-03",
            base_currency="hkd",
        )
        with self.assertRaises(RateFetchError) as altered:
            fetch_hkma_daily_rates(
                altered_request,
                transport=transport,
            )
        self.assertEqual(altered.exception.code, "rate_fetch_request_invalid")
        transport.assert_not_called()


class LegacyRateFetchCliContract:
    def test_fetch_uses_workspace_cache_default_for_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            config_path = root / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.pop("rate_cache")
            config_path.write_text(
                json.dumps(config, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            legacy_config_bytes = config_path.read_bytes()
            cache_path = root / "rates.json"
            cache_path.unlink()
            observations = parse_hkma_daily_document(
                _document([{"end_of_day": "2026-07-03", "eur": 9.25}]),
                base_currency="HKD",
            )
            stdout = io.StringIO()
            with (
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    return_value=RateFetchResult(
                        observations=observations,
                        request_urls=(HKMA_API_ENDPOINT + "?public-query",),
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(io.StringIO()),
            ):
                result = main(
                    [
                        "rates",
                        "fetch",
                        "EUR",
                        "--start",
                        "2026-07-01",
                        "--end",
                        "2026-07-03",
                        "--allow-network",
                        "--config",
                        str(config_path),
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["data"]["rate_cache"],
                {
                    "defaulted": True,
                    "path": str(cache_path.resolve()),
                },
            )
            self.assertTrue(cache_path.exists())
            self.assertEqual(config_path.read_bytes(), legacy_config_bytes)

    def test_noninteractive_fetch_requires_opt_in_before_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            fetch = Mock()
            stderr = io.StringIO()
            with (
                patch("honeymoney.cli.fetch_hkma_daily_rates", fetch),
                patch("sys.stdin", io.StringIO()),
                redirect_stderr(stderr),
                self.assertRaises(RateFetchError) as raised,
            ):
                main(
                    [
                        "rates",
                        "fetch",
                        "EUR",
                        "--start",
                        "2026-07-01",
                        "--end",
                        "2026-07-03",
                        "--config",
                        str(root / "config.json"),
                        "--json",
                    ]
                )

            self.assertEqual(raised.exception.code, "rate_fetch_opt_in_required")
            fetch.assert_not_called()
            self.assertIn("Provider: Hong Kong Monetary Authority", stderr.getvalue())
            self.assertIn(
                "Requested range: 2026-07-01 to 2026-07-03",
                stderr.getvalue(),
            )

    def test_json_opt_in_error_uses_the_fetch_command_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "honeymoney",
                "rates",
                "fetch",
                "EUR",
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-03",
                "--config",
                str(root / "config.json"),
                "--json",
            ]
            with (
                patch.object(sys, "argv", argv),
                patch("sys.stdin", io.StringIO()),
                patch("honeymoney.cli.fetch_hkma_daily_rates") as fetch,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = run()

            self.assertEqual(result, 2)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["command"], "rates.fetch")
            self.assertEqual(
                payload["errors"][0]["code"],
                "rate_fetch_opt_in_required",
            )
            fetch.assert_not_called()

    def test_human_fetch_error_includes_the_stable_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            stderr = io.StringIO()
            argv = [
                "honeymoney",
                "rates",
                "fetch",
                "EUR",
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-03",
                "--allow-network",
                "--config",
                str(root / "config.json"),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    side_effect=RateFetchError(
                        "rate_fetch_timeout",
                        "The HKMA rate request timed out.",
                    ),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                result = run()

            self.assertEqual(result, 2)
            self.assertIn(
                "rate_fetch_timeout: The HKMA rate request timed out.",
                stderr.getvalue(),
            )

    def test_interactive_consent_follows_the_public_request_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            stdout = io.StringIO()

            def fetched(_request: object) -> RateFetchResult:
                self.assertIn(
                    "Requested range: 2026-07-01 to 2026-07-03",
                    stdout.getvalue(),
                )
                return RateFetchResult(observations=[], request_urls=())

            with (
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    side_effect=fetched,
                ) as fetch,
                patch("sys.stdin", _InteractiveInput("yes\n")),
                redirect_stdout(stdout),
            ):
                result = main(
                    [
                        "rates",
                        "fetch",
                        "EUR",
                        "--start",
                        "2026-07-01",
                        "--end",
                        "2026-07-03",
                        "--config",
                        str(root / "config.json"),
                    ]
                )

            self.assertEqual(result, 0)
            fetch.assert_called_once()
            self.assertIn("Fetch these public rates now?", stdout.getvalue())

    def test_fetch_uses_shared_cache_then_later_runs_stay_offline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            observations = parse_hkma_daily_document(
                _document([{"end_of_day": "2026-07-03", "eur": 9.25}]),
                base_currency="HKD",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            def fetched(request: object) -> RateFetchResult:
                self.assertIn(
                    "Provider: Hong Kong Monetary Authority",
                    stderr.getvalue(),
                )
                self.assertEqual(
                    getattr(request, "currencies"),
                    ("EUR",),
                )
                return RateFetchResult(
                    observations=observations,
                    request_urls=(HKMA_API_ENDPOINT + "?public-query",),
                )

            with (
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    side_effect=fetched,
                ) as fetch,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = main(
                    [
                        "rates",
                        "fetch",
                        "EUR",
                        "--start",
                        "2026-07-01",
                        "--end",
                        "2026-07-03",
                        "--allow-network",
                        "--config",
                        str(root / "config.json"),
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["command"], "rates.fetch")
            self.assertTrue(payload["data"]["network_access"])
            self.assertEqual(payload["data"]["fetched_page_count"], 1)
            fetch.assert_called_once()
            cache = json.loads((root / "rates.json").read_text(encoding="utf-8"))
            self.assertEqual(cache["observations"][0]["quote_currency"], "EUR")

            (root / "input" / "foreign.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-05,SYNTHETIC OFFLINE PURCHASE,-10.00,EUR\n",
                encoding="utf-8",
            )
            with (
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    side_effect=AssertionError("ordinary commands must stay offline"),
                ) as no_network,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "run",
                            "--config",
                            str(root / "config.json"),
                            "--no-interactive",
                            "--json",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(
                        [
                            "report",
                            "--month",
                            "2026-07",
                            "--config",
                            str(root / "config.json"),
                            "--no-open",
                            "--json",
                        ]
                    ),
                    0,
                )
            no_network.assert_not_called()
            with (root / "output" / "categorized.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["amount_hkd"], "-92.50")
            self.assertEqual(row["valuation_source"], "hkma_daily_reference_rate")

    def test_fetch_failure_leaves_cache_and_ledger_bytes_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "money"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["setup", "--root", str(root), "--json"]), 0)
            (root / "input" / "foreign.csv").write_text(
                "Date,Description,Amount,Currency\n"
                "2026-07-05,SYNTHETIC RETRY PURCHASE,-10.00,EUR\n",
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "run",
                            "--config",
                            str(root / "config.json"),
                            "--no-interactive",
                            "--json",
                        ]
                    ),
                    0,
                )
            generation_paths = (
                root / "rates.json",
                root / "output" / "categorized.csv",
                root / "output" / "review_needed.csv",
                root / "output" / "import_report.json",
                root / "output" / ".honeymoney-identity-manifest.json",
                root / "output" / ".honeymoney-source-occurrences.csv",
                root / "output" / ".honeymoney-overlap-manifest.json",
            )
            before = {path: path.read_bytes() for path in generation_paths}
            with (
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    side_effect=RateFetchError(
                        "rate_fetch_timeout",
                        "Synthetic provider failure.",
                    ),
                ),
                redirect_stdout(io.StringIO()),
                self.assertRaises(RateFetchError),
            ):
                main(
                    [
                        "rates",
                        "fetch",
                        "EUR",
                        "--start",
                        "2026-07-01",
                        "--end",
                        "2026-07-03",
                        "--allow-network",
                        "--config",
                        str(root / "config.json"),
                    ]
                )
            self.assertEqual(
                {path: path.read_bytes() for path in generation_paths},
                before,
            )

            observations = parse_hkma_daily_document(
                _document([{"end_of_day": "2026-07-03", "eur": 9.25}]),
                base_currency="HKD",
            )
            with (
                patch(
                    "honeymoney.cli.fetch_hkma_daily_rates",
                    return_value=RateFetchResult(
                        observations=observations,
                        request_urls=(HKMA_API_ENDPOINT + "?public-query",),
                    ),
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "rates",
                            "fetch",
                            "EUR",
                            "--start",
                            "2026-07-01",
                            "--end",
                            "2026-07-03",
                            "--allow-network",
                            "--config",
                            str(root / "config.json"),
                            "--json",
                        ]
                    ),
                    0,
                )
            with (root / "output" / "categorized.csv").open(
                newline="",
                encoding="utf-8",
            ) as handle:
                [row] = list(csv.DictReader(handle))
            self.assertEqual(row["amount_hkd"], "-92.50")
