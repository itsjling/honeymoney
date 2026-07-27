"""Explicit public-data-only HTTP boundary for official HKMA rates."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from http.client import HTTPException, HTTPSConnection
from math import isfinite
from urllib.parse import parse_qsl, urlencode, urlsplit

from honeymoney.rates import (
    HKMA_BASE_CURRENCY,
    SUPPORTED_HKMA_CURRENCIES,
    RateImportError,
    RateObservation,
    parse_hkma_daily_page,
)

HKMA_API_HOST = "api.hkma.gov.hk"
HKMA_API_PATH = (
    "/public/market-data-and-statistics/"
    "monthly-statistical-bulletin/er-ir/er-eeri-daily"
)
HKMA_API_ENDPOINT = f"https://{HKMA_API_HOST}{HKMA_API_PATH}"
HKMA_FETCH_PAGE_SIZE = 1000
HKMA_FETCH_TIMEOUT_SECONDS = 15.0
HKMA_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_QUERY_FIELDS = (
    "pagesize",
    "offset",
    "fields",
    "choose",
    "from",
    "to",
    "sortby",
    "sortorder",
)

RateTransport = Callable[[str, float], bytes]


class RateFetchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class RateFetchRequest:
    currencies: tuple[str, ...]
    start: str
    end: str
    base_currency: str = HKMA_BASE_CURRENCY


@dataclass(frozen=True)
class RateFetchResult:
    observations: list[RateObservation]
    request_urls: tuple[str, ...]


def prepare_hkma_fetch(
    currencies: Iterable[str],
    *,
    start: str,
    end: str,
    base_currency: str,
) -> RateFetchRequest:
    base = base_currency.strip().upper()
    if base != HKMA_BASE_CURRENCY:
        raise RateFetchError(
            "rate_direction_unsupported",
            "HKMA daily rates support HKD as the configured base currency.",
        )
    quotes = tuple(sorted({currency.strip().upper() for currency in currencies}))
    if not quotes or any(not currency for currency in quotes):
        raise RateFetchError(
            "rate_fetch_currency_required",
            "Name at least one foreign currency to fetch.",
        )
    unsupported = [
        currency for currency in quotes if currency not in SUPPORTED_HKMA_CURRENCIES
    ]
    if unsupported:
        raise RateFetchError(
            "rate_fetch_currency_unsupported",
            "The requested currency is not supported by the HKMA daily data set.",
        )
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    if start_date is None or end_date is None or start_date > end_date:
        raise RateFetchError(
            "rate_fetch_range_invalid",
            "The fetch range must use ordered ISO dates.",
        )
    return RateFetchRequest(
        currencies=quotes,
        start=start_date.isoformat(),
        end=end_date.isoformat(),
    )


def build_hkma_request_url(
    request: RateFetchRequest,
    *,
    offset: int,
    page_size: int = HKMA_FETCH_PAGE_SIZE,
) -> str:
    if offset < 0 or not 1 <= page_size <= HKMA_FETCH_PAGE_SIZE:
        raise RateFetchError(
            "rate_fetch_pagination_invalid",
            "The HKMA page controls are invalid.",
        )
    query = urlencode(
        [
            ("pagesize", str(page_size)),
            ("offset", str(offset)),
            (
                "fields",
                ",".join(
                    ("end_of_day", *(item.casefold() for item in request.currencies))
                ),
            ),
            ("choose", "end_of_day"),
            ("from", request.start),
            ("to", request.end),
            ("sortby", "end_of_day"),
            ("sortorder", "asc"),
        ]
    )
    return f"{HKMA_API_ENDPOINT}?{query}"


def fetch_hkma_daily_rates(
    request: RateFetchRequest,
    *,
    transport: RateTransport | None = None,
    page_size: int = HKMA_FETCH_PAGE_SIZE,
    timeout_seconds: float = HKMA_FETCH_TIMEOUT_SECONDS,
) -> RateFetchResult:
    checked_request = prepare_hkma_fetch(
        request.currencies,
        start=request.start,
        end=request.end,
        base_currency=request.base_currency,
    )
    if checked_request != request:
        raise RateFetchError(
            "rate_fetch_request_invalid",
            "The HKMA rate request is invalid.",
        )
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise RateFetchError(
            "rate_fetch_timeout_invalid",
            "The HKMA request timeout must be positive.",
        )
    get = transport or _https_get
    observations: list[RateObservation] = []
    urls: list[str] = []
    seen_dates: set[str] = set()
    previous_page_final_date: str | None = None
    offset = 0
    while True:
        url = build_hkma_request_url(
            request,
            offset=offset,
            page_size=page_size,
        )
        urls.append(url)
        try:
            content = get(url, timeout_seconds)
        except RateFetchError:
            raise
        except (HTTPException, OSError, TimeoutError) as error:
            raise RateFetchError(
                "rate_fetch_failed",
                "The HKMA rate request failed before a complete response arrived.",
            ) from error
        try:
            page = parse_hkma_daily_page(
                content,
                base_currency=request.base_currency,
                allow_empty=True,
            )
        except RateImportError as error:
            raise RateFetchError(
                "rate_fetch_response_invalid",
                "The HKMA rate response failed local validation.",
            ) from error
        if tuple(sorted(page.record_dates)) != page.record_dates:
            raise RateFetchError(
                "rate_fetch_pagination_invalid",
                "The HKMA response pages are not in the requested order.",
            )
        if (
            previous_page_final_date is not None
            and page.record_dates
            and page.record_dates[0] <= previous_page_final_date
        ):
            raise RateFetchError(
                "rate_fetch_pagination_invalid",
                "The HKMA response pages are not in the requested order.",
            )
        if seen_dates.intersection(page.record_dates):
            raise RateFetchError(
                "rate_fetch_pagination_invalid",
                "The HKMA response pages overlap.",
            )
        if any(
            observed_date < request.start or observed_date > request.end
            for observed_date in page.record_dates
        ):
            raise RateFetchError(
                "rate_fetch_response_invalid",
                "The HKMA response includes a date outside the requested range.",
            )
        observation_keys = {
            (item["quote_currency"], item["observed_rate_date"])
            for item in page.observations
        }
        if any(
            (currency, observed_date) not in observation_keys
            for observed_date in page.record_dates
            for currency in request.currencies
        ):
            raise RateFetchError(
                "rate_fetch_response_invalid",
                "The HKMA response omits a requested currency.",
            )
        seen_dates.update(page.record_dates)
        if page.record_dates:
            previous_page_final_date = page.record_dates[-1]
        observations.extend(
            item
            for item in page.observations
            if item["quote_currency"] in request.currencies
        )
        record_count = len(page.record_dates)
        if record_count < page_size:
            return RateFetchResult(
                observations=sorted(
                    observations,
                    key=lambda item: (
                        item["quote_currency"],
                        item["observed_rate_date"],
                    ),
                ),
                request_urls=tuple(urls),
            )
        offset += record_count


def _https_get(url: str, timeout_seconds: float) -> bytes:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if (
        parts.scheme != "https"
        or parts.hostname != HKMA_API_HOST
        or parts.port is not None
        or parts.path != HKMA_API_PATH
        or not _valid_public_query(query)
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
    ):
        raise RateFetchError(
            "rate_fetch_request_invalid",
            "The HKMA request boundary rejected an invalid URL.",
        )
    connection = HTTPSConnection(HKMA_API_HOST, timeout=timeout_seconds)
    try:
        connection.request(
            "GET",
            f"{parts.path}?{parts.query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "honeymoney-public-rate-fetch/1",
            },
        )
        response = connection.getresponse()
        if response.status != 200:
            raise RateFetchError(
                "rate_fetch_failed",
                "The HKMA rate request returned an error.",
            )
        content = response.read(HKMA_MAX_RESPONSE_BYTES + 1)
        if len(content) > HKMA_MAX_RESPONSE_BYTES:
            raise RateFetchError(
                "rate_fetch_response_too_large",
                "The HKMA rate response is too large.",
            )
        return content
    except RateFetchError:
        raise
    except (HTTPException, OSError, TimeoutError) as error:
        raise RateFetchError(
            "rate_fetch_failed",
            "The HKMA rate request failed before a complete response arrived.",
        ) from error
    finally:
        connection.close()


def _valid_public_query(query: list[tuple[str, str]]) -> bool:
    if tuple(key for key, _ in query) != _QUERY_FIELDS:
        return False
    values = dict(query)
    try:
        page_size = int(values["pagesize"])
        offset = int(values["offset"])
    except (KeyError, ValueError):
        return False
    fields = values.get("fields", "").split(",")
    start = _parse_date(values.get("from", ""))
    end = _parse_date(values.get("to", ""))
    return (
        1 <= page_size <= HKMA_FETCH_PAGE_SIZE
        and offset >= 0
        and len(fields) >= 2
        and fields[0] == "end_of_day"
        and len(fields) == len(set(fields))
        and all(field.upper() in SUPPORTED_HKMA_CURRENCIES for field in fields[1:])
        and values.get("choose") == "end_of_day"
        and start is not None
        and end is not None
        and values["from"] == start.isoformat()
        and values["to"] == end.isoformat()
        and start <= end
        and values.get("sortby") == "end_of_day"
        and values.get("sortorder") == "asc"
    )


def _parse_date(value: str) -> date | None:
    if len(value) == 8 and value.isdigit():
        value = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    elif not (
        len(value) == 10
        and value[4] == "-"
        and value[7] == "-"
        and value.replace("-", "").isdigit()
    ):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
