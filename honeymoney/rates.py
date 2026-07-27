"""Validated local cache for official HKMA daily reference rates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TypedDict

HKMA_PROVIDER = "Hong Kong Monetary Authority"
HKMA_DATASET = "er-eeri-daily"
HKMA_BASE_CURRENCY = "HKD"
HKMA_MAX_RATE_AGE_DAYS = 7
RATE_CACHE_SCHEMA_VERSION = 1
SUPPORTED_HKMA_CURRENCIES = (
    "AUD",
    "CAD",
    "CHF",
    "CNY",
    "EUR",
    "GBP",
    "IDR",
    "INR",
    "JPY",
    "KRW",
    "MYR",
    "PHP",
    "SGD",
    "THB",
    "TWD",
    "USD",
    "ZAR",
)

_CACHE_FIELDS = {
    "schema_version",
    "provider",
    "dataset",
    "base_currency",
    "max_age_days",
    "observations",
    "resolutions",
}
_OBSERVATION_FIELDS = {
    "provider",
    "observed_rate_date",
    "base_currency",
    "quote_currency",
    "raw_rate",
    "import_provenance",
}
_RESOLUTION_FIELDS = {
    *_OBSERVATION_FIELDS,
    "requested_transaction_date",
}


class RateImportError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class RateObservation(TypedDict):
    provider: str
    observed_rate_date: str
    base_currency: str
    quote_currency: str
    raw_rate: str
    import_provenance: list[str]


class RateResolution(RateObservation):
    requested_transaction_date: str


class RateCache(TypedDict):
    schema_version: int
    provider: str
    dataset: str
    base_currency: str
    max_age_days: int
    observations: list[RateObservation]
    resolutions: list[RateResolution]


def empty_rate_cache() -> RateCache:
    return {
        "schema_version": RATE_CACHE_SCHEMA_VERSION,
        "provider": HKMA_PROVIDER,
        "dataset": HKMA_DATASET,
        "base_currency": HKMA_BASE_CURRENCY,
        "max_age_days": HKMA_MAX_RATE_AGE_DAYS,
        "observations": [],
        "resolutions": [],
    }


def rate_cache_document(cache: Mapping[str, object]) -> str:
    checked = validate_rate_cache(cache)
    return json.dumps(checked, indent=2, sort_keys=True) + "\n"


def load_rate_cache(path: Path) -> RateCache:
    if not path.exists():
        return empty_rate_cache()
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise RateImportError(
            "rate_cache_invalid",
            "The local rate cache is invalid.",
        ) from error
    if not isinstance(payload, Mapping):
        raise RateImportError(
            "rate_cache_invalid",
            "The local rate cache is invalid.",
        )
    return validate_rate_cache(payload)


def parse_hkma_daily_document(
    content: bytes,
    *,
    base_currency: str,
) -> list[RateObservation]:
    if base_currency.strip().upper() != HKMA_BASE_CURRENCY:
        raise RateImportError(
            "rate_direction_unsupported",
            "HKMA daily rates support HKD as the configured base currency.",
        )
    try:
        payload = json.loads(
            content.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise RateImportError(
            "hkma_document_invalid",
            "The HKMA daily-rate document is invalid.",
        ) from error
    if not isinstance(payload, Mapping) or set(payload) != {"header", "result"}:
        raise RateImportError(
            "hkma_document_invalid",
            "The HKMA daily-rate document is invalid.",
        )
    header = payload.get("header")
    result = payload.get("result")
    if (
        not isinstance(header, Mapping)
        or header.get("success") is not True
        or header.get("err_code") != "0000"
        or not isinstance(result, Mapping)
        or set(result) != {"datasize", "records"}
    ):
        raise RateImportError(
            "hkma_document_invalid",
            "The HKMA daily-rate document is invalid.",
        )
    records = result.get("records")
    datasize = result.get("datasize")
    if (
        not isinstance(records, list)
        or isinstance(datasize, bool)
        or not isinstance(datasize, int)
        or datasize != len(records)
    ):
        raise RateImportError(
            "hkma_document_invalid",
            "The HKMA daily-rate document size is invalid.",
        )
    document_hash = hashlib.sha256(content).hexdigest()
    seen_dates: set[str] = set()
    observations: list[RateObservation] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise RateImportError(
                "hkma_document_invalid",
                "The HKMA daily-rate document has an invalid record.",
            )
        observed_date = record.get("end_of_day")
        if not isinstance(observed_date, str) or not _iso_date(observed_date):
            raise RateImportError(
                "hkma_date_invalid",
                "The HKMA daily-rate document has an invalid observation date.",
            )
        if observed_date in seen_dates:
            raise RateImportError(
                "hkma_duplicate_observation",
                "The HKMA daily-rate document repeats an observation date.",
            )
        seen_dates.add(observed_date)
        for currency in SUPPORTED_HKMA_CURRENCIES:
            field = currency.casefold()
            if field not in record:
                continue
            raw = record[field]
            rate = _positive_decimal(raw)
            if rate is None:
                raise RateImportError(
                    "hkma_rate_invalid",
                    "The HKMA daily-rate document has an invalid rate.",
                )
            observations.append(
                {
                    "provider": HKMA_PROVIDER,
                    "observed_rate_date": observed_date,
                    "base_currency": HKMA_BASE_CURRENCY,
                    "quote_currency": currency,
                    "raw_rate": _canonical_decimal(rate),
                    "import_provenance": [document_hash],
                }
            )
    if not observations:
        raise RateImportError(
            "hkma_document_empty",
            "The HKMA daily-rate document has no supported rates.",
        )
    return sorted(observations, key=_observation_key)


def merge_rate_cache(
    cache: Mapping[str, object],
    observations: Iterable[Mapping[str, object]],
    requested_pairs: Iterable[tuple[str, str]],
) -> RateCache:
    checked = validate_rate_cache(cache)
    by_key: dict[tuple[str, str], RateObservation] = {
        _observation_key(item): item.copy() for item in checked["observations"]
    }
    for raw in observations:
        item = _validate_observation(raw)
        key = _observation_key(item)
        prior = by_key.get(key)
        if prior is not None and Decimal(prior["raw_rate"]) != Decimal(
            item["raw_rate"]
        ):
            raise RateImportError(
                "hkma_observation_conflict",
                "An imported HKMA observation conflicts with the local cache.",
            )
        if prior is None:
            by_key[key] = item
        else:
            prior["import_provenance"] = sorted(
                set(prior["import_provenance"]) | set(item["import_provenance"])
            )
    merged_observations = sorted(by_key.values(), key=_observation_key)
    requested = {
        (
            str(item["quote_currency"]),
            str(item["requested_transaction_date"]),
        )
        for item in checked["resolutions"]
    }
    requested.update(
        (currency.strip().upper(), transaction_date)
        for currency, transaction_date in requested_pairs
    )
    resolutions = []
    for currency, transaction_date in sorted(requested):
        resolution = resolve_cached_rate(
            {"observations": merged_observations},
            currency,
            transaction_date,
        )
        if resolution is not None:
            resolutions.append(
                {
                    **resolution,
                    "requested_transaction_date": transaction_date,
                }
            )
    return validate_rate_cache(
        {
            **empty_rate_cache(),
            "observations": merged_observations,
            "resolutions": resolutions,
        }
    )


def resolve_cached_rate(
    cache: Mapping[str, object],
    currency: str,
    transaction_date: str,
) -> RateObservation | None:
    requested = _parse_date(transaction_date)
    if requested is None:
        return None
    raw_observations = cache.get("observations")
    if not isinstance(raw_observations, list):
        return None
    candidates: list[tuple[date, Mapping[str, object]]] = []
    for raw in raw_observations:
        if not isinstance(raw, Mapping):
            continue
        if str(raw.get("quote_currency", "")).upper() != currency.strip().upper():
            continue
        observed = _parse_date(str(raw.get("observed_rate_date", "")))
        if observed is None or observed > requested:
            continue
        age = (requested - observed).days
        if age <= HKMA_MAX_RATE_AGE_DAYS:
            candidates.append((observed, raw))
    if not candidates:
        return None
    _, selected = max(candidates, key=lambda item: item[0])
    return _validate_observation(selected)


def validate_rate_cache(cache: Mapping[str, object]) -> RateCache:
    if (
        set(cache) != _CACHE_FIELDS
        or cache.get("schema_version") != RATE_CACHE_SCHEMA_VERSION
        or cache.get("provider") != HKMA_PROVIDER
        or cache.get("dataset") != HKMA_DATASET
        or cache.get("base_currency") != HKMA_BASE_CURRENCY
        or cache.get("max_age_days") != HKMA_MAX_RATE_AGE_DAYS
    ):
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    raw_observations = cache.get("observations")
    raw_resolutions = cache.get("resolutions")
    if not isinstance(raw_observations, list) or not isinstance(raw_resolutions, list):
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    observations = [_validate_observation(item) for item in raw_observations]
    if len({_observation_key(item) for item in observations}) != len(observations):
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    resolutions = [_validate_resolution(item) for item in raw_resolutions]
    resolution_keys = {
        (
            item["quote_currency"],
            item["requested_transaction_date"],
        )
        for item in resolutions
    }
    if len(resolution_keys) != len(resolutions):
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    observation_cache = {"observations": observations}
    for resolution in resolutions:
        expected = resolve_cached_rate(
            observation_cache,
            str(resolution["quote_currency"]),
            str(resolution["requested_transaction_date"]),
        )
        resolved_observation: RateObservation = {
            "provider": resolution["provider"],
            "observed_rate_date": resolution["observed_rate_date"],
            "base_currency": resolution["base_currency"],
            "quote_currency": resolution["quote_currency"],
            "raw_rate": resolution["raw_rate"],
            "import_provenance": resolution["import_provenance"],
        }
        if expected is None or expected != resolved_observation:
            raise RateImportError(
                "rate_cache_invalid",
                "The local rate cache is invalid.",
            )
    return {
        **empty_rate_cache(),
        "observations": sorted(observations, key=_observation_key),
        "resolutions": sorted(
            resolutions,
            key=lambda item: (
                item["quote_currency"],
                item["requested_transaction_date"],
            ),
        ),
    }


def _validate_observation(raw: object) -> RateObservation:
    if not isinstance(raw, Mapping) or set(raw) != _OBSERVATION_FIELDS:
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    provider = raw.get("provider")
    observed_date = raw.get("observed_rate_date")
    base = raw.get("base_currency")
    quote = raw.get("quote_currency")
    rate = raw.get("raw_rate")
    provenance = raw.get("import_provenance")
    if (
        provider != HKMA_PROVIDER
        or not isinstance(observed_date, str)
        or not _iso_date(observed_date)
        or base != HKMA_BASE_CURRENCY
        or quote not in SUPPORTED_HKMA_CURRENCIES
        or _positive_decimal(rate) is None
        or not isinstance(provenance, list)
        or not provenance
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in provenance
        )
    ):
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    return {
        "provider": provider,
        "observed_rate_date": observed_date,
        "base_currency": base,
        "quote_currency": quote,
        "raw_rate": _canonical_decimal(_positive_decimal(rate)),
        "import_provenance": sorted(set(provenance)),
    }


def _validate_resolution(raw: object) -> RateResolution:
    if not isinstance(raw, Mapping) or set(raw) != _RESOLUTION_FIELDS:
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    requested = raw.get("requested_transaction_date")
    observation = _validate_observation(
        {field: raw[field] for field in _OBSERVATION_FIELDS}
    )
    if not isinstance(requested, str) or not _iso_date(requested):
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    observed = date.fromisoformat(str(observation["observed_rate_date"]))
    requested_date = date.fromisoformat(requested)
    age = (requested_date - observed).days
    if age < 0 or age > HKMA_MAX_RATE_AGE_DAYS:
        raise RateImportError("rate_cache_invalid", "The local rate cache is invalid.")
    return {**observation, "requested_transaction_date": requested}


def _observation_key(item: Mapping[str, object]) -> tuple[str, str]:
    return str(item["quote_currency"]), str(item["observed_rate_date"])


def _positive_decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _canonical_decimal(value: Decimal | None) -> str:
    if value is None:
        raise AssertionError("positive decimal validation was skipped")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _iso_date(value: str) -> bool:
    return _parse_date(value) is not None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")
