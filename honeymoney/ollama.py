from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Callable, Mapping, NamedTuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from honeymoney.classification_policy import (
    evaluate_model_suggestion,
    model_boundary_guidance,
    model_category_descriptions,
)
from honeymoney.schema import allowed_categories

_TICK_INTERVAL_SECONDS = 1.0
_DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5


class OllamaHttpRequest(NamedTuple):
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: float


class OllamaHttpResponse(NamedTuple):
    status: int
    reason: str
    headers: Mapping[str, str]
    body: bytes


class _ValidatedEndpoint(NamedTuple):
    pinned_url: str
    host_header: str


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _default_sender(request: OllamaHttpRequest) -> OllamaHttpResponse:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    wire_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with opener.open(wire_request, timeout=request.timeout) as response:
            return OllamaHttpResponse(
                int(response.status),
                str(response.reason),
                dict(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return OllamaHttpResponse(
            error.code,
            str(error.reason),
            dict(error.headers.items()) if error.headers is not None else {},
            error.read(),
        )


class LoopbackOllamaTransport:
    """HTTP transport that can connect only to validated loopback addresses."""

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
        sender: Callable[[OllamaHttpRequest], OllamaHttpResponse] | None = None,
        max_redirects: int = _MAX_REDIRECTS,
    ) -> None:
        self._resolver = resolver or socket.getaddrinfo
        self._sender = sender or _default_sender
        self._max_redirects = max_redirects

    def request(self, request: OllamaHttpRequest) -> bytes:
        current = request
        for redirect_count in range(self._max_redirects + 1):
            endpoint = validate_ollama_endpoint(current.url, resolver=self._resolver)
            headers = dict(current.headers)
            headers["Host"] = endpoint.host_header
            pinned = current._replace(url=endpoint.pinned_url, headers=headers)
            response = self._sender(pinned)
            if response.status not in _REDIRECT_STATUSES:
                if response.status >= 400:
                    raise urllib.error.HTTPError(
                        current.url,
                        response.status,
                        response.reason,
                        response.headers,
                        BytesIO(response.body),
                    )
                return response.body

            location = _header(response.headers, "location")
            if not location:
                raise ValueError("Ollama endpoint redirect did not include a location")
            if redirect_count >= self._max_redirects:
                raise ValueError("Ollama endpoint exceeded the redirect limit")
            redirected_url = urljoin(current.url, location)
            method, body, redirected_headers = _redirect_request_parts(
                current, response.status
            )
            current = OllamaHttpRequest(
                method,
                redirected_url,
                redirected_headers,
                body,
                current.timeout,
            )
        raise AssertionError("redirect loop accounting failed")


def validate_ollama_endpoint(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
) -> _ValidatedEndpoint:
    """Validate and pin an HTTP Ollama URL to one resolved loopback address."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as error:
        raise ValueError(f"Ollama endpoint URL is malformed: {url!r}") from error

    if parsed.scheme.casefold() != "http":
        raise ValueError("Ollama endpoint must use http on a local loopback address")
    if not parsed.netloc or not hostname:
        raise ValueError("Ollama endpoint must include a local loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Ollama endpoint must not include credentials")
    if parsed.fragment:
        raise ValueError("Ollama endpoint must not include a URL fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Ollama endpoint port must be between 1 and 65535")

    resolved_port = port or 80
    try:
        records = (resolver or socket.getaddrinfo)(
            hostname,
            resolved_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise ValueError(
            f"Ollama endpoint host {hostname!r} could not be resolved locally"
        ) from error

    addresses: list[str] = []
    for record in records:
        try:
            address = str(record[4][0]).split("%", 1)[0]
            parsed_address = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError) as error:
            raise ValueError(
                f"Ollama endpoint host {hostname!r} returned an invalid address"
            ) from error
        if not parsed_address.is_loopback:
            raise ValueError(
                "Ollama endpoint must resolve only to local loopback addresses"
            )
        canonical_address = str(parsed_address)
        if canonical_address not in addresses:
            addresses.append(canonical_address)
    if not addresses:
        raise ValueError("Ollama endpoint did not resolve to a usable loopback address")

    selected = addresses[0]
    pinned_host = f"[{selected}]" if ":" in selected else selected
    pinned_netloc = f"{pinned_host}:{port}" if port is not None else pinned_host
    path = parsed.path or "/"
    pinned_url = urlunsplit(("http", pinned_netloc, path, parsed.query, ""))
    return _ValidatedEndpoint(pinned_url, parsed.netloc)


def _header(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.casefold() == name.casefold():
            return str(value)
    return ""


def _redirect_request_parts(
    request: OllamaHttpRequest, status: int
) -> tuple[str, bytes | None, dict[str, str]]:
    if status in {301, 302, 303} and request.method not in {"GET", "HEAD"}:
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.casefold() not in {"content-length", "content-type", "host"}
        }
        return "GET", None, headers
    headers = {
        key: value for key, value in request.headers.items() if key.casefold() != "host"
    }
    return request.method, request.body, headers


class OllamaProgress(NamedTuple):
    batch_number: int
    batch_count: int
    start_index: int
    end_index: int
    total: int
    elapsed_seconds: float


def list_ollama_models(
    ollama_config: dict[str, Any],
    transport: LoopbackOllamaTransport | None = None,
) -> list[str]:
    """Return model names installed at the configured Ollama endpoint."""
    generate_url = str(ollama_config.get("url", _DEFAULT_OLLAMA_URL))
    parsed = urlsplit(generate_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/generate"):
        path = f"{path.rsplit('/', 1)[0]}/tags"
    else:
        path = f"{path}/api/tags" if path else "/api/tags"
    tags_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

    http = transport or LoopbackOllamaTransport()
    body = http.request(
        OllamaHttpRequest(
            "GET",
            tags_url,
            {"Accept": "application/json"},
            None,
            min(_timeout_seconds(ollama_config), 10.0),
        )
    )
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Ollama model list response did not contain a models array")

    names = []
    for model in payload["models"]:
        if not isinstance(model, dict):
            continue
        name = model.get("name") or model.get("model")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return sorted(set(names), key=str.casefold)


def apply_ollama_fallback(
    transactions: list[dict[str, str]],
    config: dict[str, Any],
    progress: Callable[[OllamaProgress], None] | None = None,
    transport: LoopbackOllamaTransport | None = None,
) -> tuple[dict[str, Any], list[str]]:
    ollama_config = config.get("ollama", {})
    if not ollama_config.get("enabled", False):
        return {"status": "disabled"}, []

    unresolved = [
        transaction
        for transaction in transactions
        if transaction.get("category") == "Unknown"
        and transaction.get("needs_review") == "true"
    ]
    if not unresolved:
        return {
            "status": "skipped",
            "reason": "no unresolved transactions",
            "candidate_count": 0,
            "accepted_count": 0,
            "reviewable_count": 0,
            "rejected_count": 0,
            "applied_count": 0,
            "invalid_count": 0,
        }, []

    batch_size = _batch_size(ollama_config)
    chunks = _chunks(unresolved, batch_size)
    batch_count = len(chunks)
    accepted = 0
    reviewable = 0
    rejected = 0
    invalid = 0
    processed = 0
    details: list[str] = []
    for batch_number, batch in enumerate(chunks, start=1):
        start_index = processed + 1
        end_index = processed + len(batch)
        processed = end_index

        def tick(
            elapsed: float,
            _batch_number: int = batch_number,
            _start: int = start_index,
            _end: int = end_index,
        ) -> None:
            if progress is not None:
                progress(
                    OllamaProgress(
                        _batch_number,
                        batch_count,
                        _start,
                        _end,
                        len(unresolved),
                        elapsed,
                    )
                )

        tick(0.0)
        try:
            response_body = _request_ollama(
                batch,
                ollama_config,
                config,
                tick=tick if progress is not None else None,
                transport=transport,
            )
        except (
            OSError,
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
        ) as error:
            error_text = _error_text(error)
            warning = f"Ollama unavailable: {error_text}"
            pending = unresolved[start_index - 1 :]
            for transaction in pending:
                if "ollama_categorized" in transaction.get("flags", ""):
                    continue
                transaction["flags"] = _append_flag(
                    transaction["flags"], "ollama_unavailable"
                )
                transaction["reason"] = _append_reason(
                    transaction["reason"], "Ollama unavailable"
                )
            return {
                "status": "unavailable",
                "error": error_text,
                "candidate_count": len(unresolved),
                "accepted_count": accepted,
                "reviewable_count": reviewable,
                "rejected_count": rejected,
                "applied_count": accepted + reviewable,
                "invalid_count": invalid,
            }, [warning]

        batch_counts, batch_invalid, batch_details = _apply_ollama_response(
            batch, response_body, config
        )
        accepted += batch_counts["accepted"]
        reviewable += batch_counts["reviewable"]
        rejected += batch_counts["rejected"]
        invalid += batch_invalid
        details.extend(batch_details)

    applied = accepted + reviewable
    status = "success" if not invalid else "invalid_response"
    warnings = []
    if invalid:
        warnings = ["Ollama returned invalid categorizations"]
        warnings.extend(details[:5])
        if len(details) > 5:
            warnings.append(
                f"...and {len(details) - 5} more invalid Ollama categorizations"
            )
        applied_ids = {
            transaction["transaction_id"]
            for transaction in unresolved
            if "ollama_categorized" in transaction.get("flags", "")
        }
        for transaction in unresolved:
            if transaction["transaction_id"] in applied_ids:
                continue
            transaction["flags"] = _append_flag(
                transaction["flags"], "ollama_invalid_response"
            )
            transaction["reason"] = _append_reason(
                transaction["reason"], "Ollama returned invalid categorization"
            )
    return {
        "status": status,
        "candidate_count": len(unresolved),
        "accepted_count": accepted,
        "reviewable_count": reviewable,
        "rejected_count": rejected,
        "applied_count": applied,
        "invalid_count": invalid,
    }, warnings


def _batch_size(ollama_config: dict[str, Any]) -> int:
    try:
        batch_size = int(ollama_config.get("batch_size", 5))
    except (TypeError, ValueError):
        return 5
    return max(1, batch_size)


def _timeout_seconds(ollama_config: dict[str, Any]) -> float:
    try:
        timeout = float(ollama_config.get("timeout_seconds", 120))
    except (TypeError, ValueError):
        return 120.0
    return timeout if timeout > 0 else 120.0


def _error_text(error: Exception) -> str:
    if isinstance(error, urllib.error.HTTPError):
        message = ""
        try:
            body = json.loads(error.read().decode("utf-8", "replace"))
            message = str(body.get("error", ""))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        detail = f": {message}" if message else ""
        return f"HTTP {error.code} {error.reason}{detail}"
    return str(error)


def _chunks(rows: list[dict[str, str]], size: int) -> list[list[dict[str, str]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def _request_ollama(
    transactions: list[dict[str, str]],
    ollama_config: dict[str, Any],
    config: dict[str, Any],
    tick: Callable[[float], None] | None = None,
    transport: LoopbackOllamaTransport | None = None,
) -> dict[str, Any]:
    descriptions = model_category_descriptions(config)
    categories = sorted(descriptions)
    payload = {
        "model": ollama_config.get("model", "qwen2.5:7b-instruct"),
        "stream": False,
        "think": bool(ollama_config.get("think", False)),
        "format": _response_format(categories),
        "prompt": json.dumps(
            {
                "task": (
                    "Categorize each household transaction. Reply with a JSON object "
                    '{"categorizations": [...]} containing one item per transaction '
                    "with: id copied from the transaction, category from "
                    "allowed_categories, confidence between 0 and 1, and a short "
                    "reason. Categories are merchant spending labels only: never "
                    "infer income, transfers, card payments, savings, investments, "
                    "or an owner."
                ),
                "allowed_categories": categories,
                "category_definitions": descriptions,
                "accounting_boundaries": model_boundary_guidance(),
                "transactions": [
                    _ollama_transaction_payload(row) for row in transactions
                ],
            }
        ),
    }

    request = OllamaHttpRequest(
        "POST",
        str(ollama_config.get("url", _DEFAULT_OLLAMA_URL)),
        {"Content-Type": "application/json"},
        json.dumps(payload).encode("utf-8"),
        _timeout_seconds(ollama_config),
    )

    http = transport or LoopbackOllamaTransport()
    if tick is None:
        return json.loads(http.request(request).decode("utf-8"))

    result: dict[str, Any] = {}
    error: dict[str, Exception] = {}

    def worker() -> None:
        try:
            result["body"] = json.loads(http.request(request).decode("utf-8"))
        except Exception as exc:  # re-raised on the caller's thread below
            error["exc"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    start = time.monotonic()
    thread.start()
    while thread.is_alive():
        thread.join(_TICK_INTERVAL_SECONDS)
        if thread.is_alive():
            tick(time.monotonic() - start)
    if "exc" in error:
        raise error["exc"]
    return result["body"]


def _response_format(categories: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "categorizations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "category": {"type": "string", "enum": categories},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reason": {"type": "string"},
                    },
                    "required": ["id", "category", "confidence", "reason"],
                },
            }
        },
        "required": ["categorizations"],
    }


def _ollama_transaction_payload(transaction: dict[str, str]) -> dict[str, str]:
    return {
        "id": transaction["transaction_id"],
        "date": transaction["date"],
        "merchant": transaction["merchant"],
        "description": transaction["original_description"],
        "original_amount": transaction["original_amount"],
        "original_currency": transaction["original_currency"],
        "posted_amount": transaction["posted_amount"],
        "posted_currency": transaction["posted_currency"],
        "amount_hkd": transaction["amount_hkd"],
        "institution": transaction["institution"],
        "payment_method": transaction["payment_method"],
    }


def _apply_ollama_response(
    unresolved: list[dict[str, str]],
    response_body: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, int], int, list[str]]:
    raw_response = response_body.get("response", "")
    try:
        categorizations = json.loads(raw_response)
    except (TypeError, json.JSONDecodeError):
        detail = f"Ollama response was not JSON: {_snippet(raw_response)}"
        return (
            {"accepted": 0, "reviewable": 0, "rejected": 0},
            len(unresolved),
            [detail],
        )
    if isinstance(categorizations, dict):
        categorizations = categorizations.get("categorizations")
    if not isinstance(categorizations, list):
        detail = f"Ollama response was not a JSON list: {_snippet(raw_response)}"
        return (
            {"accepted": 0, "reviewable": 0, "rejected": 0},
            len(unresolved),
            [detail],
        )

    by_id = {transaction["transaction_id"]: transaction for transaction in unresolved}
    categories = allowed_categories(config)
    counts = {"accepted": 0, "reviewable": 0, "rejected": 0}
    invalid = 0
    details: list[str] = []
    handled_ids: set[str] = set()
    mentioned_ids: set[str] = set()
    for categorization in categorizations:
        if not isinstance(categorization, dict):
            continue
        transaction_id = str(categorization.get("id", ""))
        transaction = by_id.get(transaction_id)
        if transaction is not None:
            mentioned_ids.add(transaction_id)
        category = str(categorization.get("category", ""))
        reason = str(categorization.get("reason", ""))
        try:
            confidence = Decimal(str(categorization.get("confidence", "")))
        except InvalidOperation:
            confidence = Decimal("-1")

        problem = ""
        if transaction is None:
            problem = f"unknown transaction id {transaction_id or '(missing)'}"
        elif transaction_id in handled_ids:
            problem = "duplicate categorization for the same transaction"
        elif category not in categories:
            problem = f"category {category or '(missing)'!r} is not allowed"
        elif not reason:
            problem = "missing reason"
        elif (
            not confidence.is_finite()
            or confidence < Decimal("0")
            or confidence > Decimal("1")
        ):
            problem = f"confidence {categorization.get('confidence')!r} is not between 0 and 1"
        if problem:
            invalid += 1
            subject = transaction.get("merchant", "") if transaction else transaction_id
            details.append(
                f"Ollama categorization rejected ({subject or 'unknown'}): {problem}"
            )
            continue

        handled_ids.add(transaction_id)
        outcome = evaluate_model_suggestion(transaction, category, confidence, config)
        if outcome.outcome == "rejected":
            transaction["flags"] = _append_flag(
                transaction["flags"], "ollama_policy_rejected"
            )
            transaction["reason"] = _append_reason(
                transaction["reason"], outcome.reason
            )
            transaction["needs_review"] = "true"
            counts["rejected"] += 1
            continue
        if outcome.outcome == "invalid":
            invalid += 1
            details.append(
                f"Ollama categorization rejected ({transaction.get('merchant', '') or transaction_id}): category {category!r} is not allowed"
            )
            continue
        transaction["category"] = category
        transaction["confidence"] = _format_decimal(confidence)
        transaction["reason"] = reason
        transaction["flags"] = _remove_flag(transaction["flags"], "uncategorized")
        transaction["flags"] = _append_flag(transaction["flags"], "ollama_categorized")
        transaction["needs_review"] = (
            "false" if outcome.outcome == "accepted" else "true"
        )
        counts[outcome.outcome] += 1

    unanswered = len(set(by_id) - mentioned_ids)
    if unanswered:
        invalid += unanswered
        details.append(
            f"Ollama returned no categorization for {unanswered} transaction(s)"
        )
    return counts, invalid, details


def _snippet(value: Any, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return f"{text[:limit]}…"
    return text or "(empty)"


def _append_flag(existing: str, flag: str) -> str:
    flags = [item for item in existing.split(";") if item]
    if flag not in flags:
        flags.append(flag)
    return ";".join(flags)


def _remove_flag(existing: str, flag: str) -> str:
    return ";".join(item for item in existing.split(";") if item and item != flag)


def _append_reason(existing: str, reason: str) -> str:
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing}; {reason}"


def _format_decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))
