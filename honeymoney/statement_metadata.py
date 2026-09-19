from __future__ import annotations

import hashlib
import io
import re
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, TypedDict

from honeymoney import importers

ENGINE_VERSION = "0.2.4"
MAX_PDF_INPUT_BYTES = 64 * 1024 * 1024
MAX_PDF_PAGES = 500
MAX_PDF_EXTRACTED_TEXT_CHARS = 20_000_000


class StatementMetadata(TypedDict):
    statement_date: str | None
    period_start: str | None
    period_end: str | None
    source_page: int | None


class StatementMetadataError(ValueError):
    """A bounded failure that does not expose statement text or paths."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_NAMED_DATE_4 = rf"\d{{1,2}}\s*{_MONTH}\s*,?\s*\d{{4}}"
_NAMED_DATE_2 = rf"\d{{1,2}}\s*{_MONTH}\s*\d{{2}}"
_NUMERIC_DATE_4 = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})"
_DATE_4 = rf"(?:{_NAMED_DATE_4}|{_NUMERIC_DATE_4})"
_RANGE_4 = rf"(?P<start>{_DATE_4})\s*[-–—]\s*(?P<end>{_DATE_4})"
_INVALID_DATE = "<invalid>"


def empty_statement_metadata() -> StatementMetadata:
    return {
        "statement_date": None,
        "period_start": None,
        "period_end": None,
        "source_page": None,
    }


def statement_metadata_document(input_path: Path) -> dict[str, object]:
    """Return metadata from one stable PDF without selecting a parser profile."""
    if input_path.suffix.casefold() != ".pdf":
        raise StatementMetadataError(
            "statement_metadata_unsupported_type", "Expected a PDF file."
        )
    if input_path.is_symlink() or not input_path.is_file():
        raise StatementMetadataError(
            "statement_metadata_invalid_source", "Source must be a regular file."
        )
    try:
        snapshot = importers._capture_input_source(input_path, {})
        page_count, metadata = statement_metadata_from_snapshot(snapshot.source_bytes)
    except StatementMetadataError:
        raise
    except ModuleNotFoundError as error:
        raise StatementMetadataError(
            "statement_metadata_dependency_missing",
            "Install Honeymoney with its PDF dependencies.",
        ) from error
    except Exception as error:
        raise StatementMetadataError(
            "statement_metadata_failed", "Could not read statement metadata."
        ) from error
    return {
        "source_sha256": hashlib.sha256(snapshot.source_bytes).hexdigest(),
        "page_count": page_count,
        "engine": {"name": "honeymoney", "version": ENGINE_VERSION},
        "statement_metadata": metadata,
    }


def statement_metadata_from_snapshot(
    source_bytes: bytes,
) -> tuple[int, StatementMetadata]:
    """Extract metadata and page count from captured PDF bytes."""
    if len(source_bytes) > MAX_PDF_INPUT_BYTES:
        raise ValueError(f"PDF input exceeds {MAX_PDF_INPUT_BYTES} bytes")

    import pdfplumber

    page_texts: list[str] = []
    extracted_chars = 0
    with pdfplumber.open(io.BytesIO(source_bytes)) as pdf:
        page_count = len(pdf.pages)
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF page count exceeds {MAX_PDF_PAGES}")
        for page in pdf.pages:
            text = str(page.extract_text() or "")
            extracted_chars += len(text)
            if extracted_chars > MAX_PDF_EXTRACTED_TEXT_CHARS:
                raise ValueError(
                    "PDF extracted text exceeds "
                    f"{MAX_PDF_EXTRACTED_TEXT_CHARS} characters"
                )
            page_texts.append(text)
    return page_count, extract_statement_metadata(page_texts)


def extract_statement_metadata(page_texts: Iterable[str]) -> StatementMetadata:
    """Extract only dates tied to known statement header layouts."""
    observations: dict[str, list[tuple[str, int]]] = {
        "statement_date": [],
        "period_start": [],
        "period_end": [],
    }
    for page_number, raw_text in enumerate(page_texts, start=1):
        text = _normalized_page_text(raw_text)
        for values in _page_observations(text):
            for field, value in values.items():
                observations[field].append((value, page_number))

    result = empty_statement_metadata()
    for field in ("statement_date", "period_start", "period_end"):
        field_observations = observations[field]
        distinct_values = {value for value, _page in field_observations}
        if len(distinct_values) == 1 and _INVALID_DATE not in distinct_values:
            value = next(iter(distinct_values))
            if field == "statement_date":
                result["statement_date"] = value
            elif field == "period_start":
                result["period_start"] = value
            else:
                result["period_end"] = value

    start = result["period_start"]
    end = result["period_end"]
    if (start is None) != (end is None) or (
        start is not None and end is not None and start > end
    ):
        result["period_start"] = None
        result["period_end"] = None
    if any(
        value is not None
        for value in (
            result["statement_date"],
            result["period_start"],
            result["period_end"],
        )
    ):
        evidence_page_sets = [
            {page for observed, page in observations[field] if observed == value}
            for field, value in result.items()
            if field != "source_page" and value is not None
        ]
        common_pages = set.intersection(*evidence_page_sets)
        if common_pages:
            result["source_page"] = min(common_pages)
    return result


def _normalized_page_text(text: str) -> str:
    return "\n".join(
        re.sub(r"[ \t]+", " ", line.replace("\u00a0", " ")).strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    )


def _page_observations(text: str) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []

    direct_statement_dates = re.finditer(
        rf"(?im)^Statement\s+date\s*:?\s*(?P<date>{_DATE_4})\s*$", text
    )
    _append_statement_dates(observations, direct_statement_dates)

    hsbc_page_dates = re.finditer(
        rf"(?im)^.*\bPage\s+1\s+of\s+\d+\s*$\n"
        rf"(?:[^\n]*\n){{0,2}}[^\n]*?(?P<date>{_DATE_4})\s*$",
        text,
    )
    _append_statement_dates(observations, hsbc_page_dates)

    hsbc_card_dates = re.finditer(
        rf"(?is)\bStatement\s+date\s+Statement\s+balance\b"
        rf"(?:(?!\bStatement\s+date\b).){{0,240}}?(?P<date>{_DATE_4})",
        text,
    )
    _append_statement_dates(observations, hsbc_card_dates)

    hang_seng_card_dates = re.finditer(
        rf"(?is)\bACCOUNT\s+NO\.?\s+CREDIT\s+LIMIT\s+CLOSING\s+DATE\s+"
        rf"PAYMENT\s+DUE\s+DATE\b.{{0,240}}?(?P<date>{_NAMED_DATE_2}|{_DATE_4})",
        text,
    )
    _append_statement_dates(observations, hang_seng_card_dates, allow_two_digit=True)

    if re.search(r"(?i)\bHang\s+Seng\b|恒生", text):
        hang_seng_bank_dates = re.finditer(
            rf"(?im)^(?:[A-Z]\s+)?Date\s*:\s*(?P<date>{_DATE_4})\s*$", text
        )
        _append_statement_dates(observations, hang_seng_bank_dates)

    if re.search(
        r"(?i)Consolidated\s+statement\s+for\s+savings\s+account", text
    ) or re.search(r"(?i)INVSTM0011", text):
        hsbc_unsupported_dates = re.finditer(
            rf"(?im)^(?:[A-Z]\s+)?Date\s*:\s*(?P<date>{_DATE_4})\s*$", text
        )
        _append_statement_dates(observations, hsbc_unsupported_dates)

    observations.extend(_mox_bank_observations(text))
    observations.extend(_mox_credit_observations(text))
    return observations


def _append_statement_dates(
    observations: list[dict[str, str]],
    matches: Iterable[re.Match[str]],
    *,
    allow_two_digit: bool = False,
) -> None:
    for match in matches:
        parsed = _parse_printed_date(
            match.group("date"), allow_two_digit=allow_two_digit
        )
        if parsed is not None:
            observations.append({"statement_date": parsed.isoformat()})
        else:
            observations.append({"statement_date": _INVALID_DATE})


def _mox_bank_observations(text: str) -> list[dict[str, str]]:
    if not (
        re.search(r"(?i)\bStatement\s+period\b", text)
        and re.search(r"(?i)\bStatement\s+date\b", text)
    ):
        return []

    compact = text.replace("\n", " ")
    periods = [
        period
        for match in re.finditer(_RANGE_4, compact, flags=re.IGNORECASE)
        if (period := _period_values(match)) is not None
    ]
    if not periods:
        return []

    direct_date = re.search(
        rf"(?im)^Statement\s+date\s*:?\s*(?P<date>{_DATE_4})\s*$", text
    )
    statement_date = (
        _parse_printed_date(direct_date.group("date"))
        if direct_date is not None
        else None
    )
    if statement_date is None:
        headers_then_values = re.search(
            rf"(?is)Statement\s+period\s+Statement\s+date\s+{_RANGE_4}"
            rf"\s+(?P<date>{_DATE_4})",
            compact,
        )
        if headers_then_values is not None:
            statement_date = _parse_printed_date(headers_then_values.group("date"))

    if statement_date is not None:
        periods.append({"statement_date": statement_date.isoformat()})
    return periods


def _mox_credit_observations(text: str) -> list[dict[str, str]]:
    title = re.search(
        r"(?i)(?:Mox.*Credit.*Statement|Credit\s+Card\s+Statement|信用卡月結單)",
        text,
    )
    if title is None:
        return []
    return [
        period
        for match in re.finditer(_RANGE_4, text[title.end() :], flags=re.IGNORECASE)
        if (period := _period_values(match)) is not None
    ]


def _period_values(match: re.Match[str]) -> dict[str, str] | None:
    start = _parse_printed_date(match.group("start"))
    end = _parse_printed_date(match.group("end"))
    if start is None or end is None or start > end:
        return None
    return {"period_start": start.isoformat(), "period_end": end.isoformat()}


def _parse_printed_date(value: str, *, allow_two_digit: bool = False) -> date | None:
    cleaned = re.sub(r"\s+", " ", value.strip().replace(",", ""))
    formats = ["%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"]
    compact = cleaned.replace(" ", "")
    formats_and_values = [(date_format, cleaned) for date_format in formats]
    formats_and_values.extend([("%d%b%Y", compact), ("%d%B%Y", compact)])
    if allow_two_digit:
        formats_and_values.extend(
            [("%d %b %y", cleaned), ("%d %B %y", cleaned), ("%d%b%y", compact)]
        )
    parsed: set[date] = set()
    for date_format, candidate in formats_and_values:
        try:
            parsed.add(datetime.strptime(candidate, date_format).date())
        except ValueError:
            continue
    if len(parsed) != 1:
        return None
    return next(iter(parsed))
