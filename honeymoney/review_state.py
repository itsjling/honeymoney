"""Keep human-review state explicit and internally consistent."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

REVIEW_REASON_CATEGORY = "category_decision"
REVIEW_REASON_CATEGORY_SUGGESTION = "category_suggestion"
REVIEW_REASON_ACCOUNTING_FLOW = "accounting_flow"
REVIEW_REASON_IDENTITY = "identity_conflict"
REVIEW_REASON_SOURCE_DATA = "source_data_issue"
REVIEW_REASON_OWNERSHIP = "ownership_decision"
REVIEW_REASON_OTHER = "other_decision"

ALLOWED_REVIEW_REASONS = frozenset(
    {
        REVIEW_REASON_CATEGORY,
        REVIEW_REASON_CATEGORY_SUGGESTION,
        REVIEW_REASON_ACCOUNTING_FLOW,
        REVIEW_REASON_IDENTITY,
        REVIEW_REASON_SOURCE_DATA,
        REVIEW_REASON_OWNERSHIP,
        REVIEW_REASON_OTHER,
    }
)

REVIEW_REASON_LABELS = {
    REVIEW_REASON_CATEGORY: "Choose a category",
    REVIEW_REASON_CATEGORY_SUGGESTION: "Approve the suggested category",
    REVIEW_REASON_ACCOUNTING_FLOW: "Resolve the accounting flow",
    REVIEW_REASON_IDENTITY: "Resolve a transaction identity conflict",
    REVIEW_REASON_SOURCE_DATA: "Fix source data",
    REVIEW_REASON_OWNERSHIP: "Choose an owner",
    REVIEW_REASON_OTHER: "Make another recorded decision",
}

_IDENTITY_FLAGS = {
    "duplicate_suspected",
    "identity_migration_ambiguous",
    "overlap_count_ambiguous",
    "overlap_history_ambiguous",
}
SOURCE_DATA_FLAGS = frozenset(
    {
        "invalid_amount",
        "source_provenance_ambiguous",
        "source_provenance_inconsistent",
        "statement_opening_balance_conflict",
        "statement_closing_balance_conflict",
    }
)


def review_reason_tokens(value: str) -> list[str]:
    """Return checked, de-duplicated review reason tokens."""
    tokens = [item.strip() for item in value.split(";") if item.strip()]
    unknown = set(tokens) - ALLOWED_REVIEW_REASONS
    if unknown:
        raise ValueError("Unsupported review reasons: " + ", ".join(sorted(unknown)))
    return list(dict.fromkeys(tokens))


def review_reason_labels(value: str) -> list[str]:
    """Return plain labels for a stored token list."""
    return [REVIEW_REASON_LABELS[item] for item in review_reason_tokens(value)]


def has_identity_review_evidence(row: Mapping[str, str]) -> bool:
    """Return whether current flags require an identity decision."""
    flags = set(filter(None, str(row.get("flags", "")).split(";")))
    return bool(flags & _IDENTITY_FLAGS)


def set_review_reason(
    row: dict[str, str],
    reason: str,
    active: bool,
) -> None:
    """Add or remove one reason and derive ``needs_review`` from the result."""
    if reason not in ALLOWED_REVIEW_REASONS:
        raise ValueError(f"Unsupported review reason: {reason}")
    reasons = review_reason_tokens(row.get("review_reasons", ""))
    if active and reason not in reasons:
        reasons.append(reason)
    if not active:
        reasons = [item for item in reasons if item != reason]
    row["review_reasons"] = ";".join(reasons)
    row["needs_review"] = str(bool(reasons)).lower()


def replace_review_reasons(
    row: dict[str, str],
    reasons: Iterable[str],
) -> None:
    """Replace all reasons and derive the boolean state."""
    checked = list(dict.fromkeys(reasons))
    unknown = set(checked) - ALLOWED_REVIEW_REASONS
    if unknown:
        raise ValueError("Unsupported review reasons: " + ", ".join(sorted(unknown)))
    row["review_reasons"] = ";".join(checked)
    row["needs_review"] = str(bool(checked)).lower()


def synchronize_review_state(
    row: dict[str, str],
    *,
    legacy: bool = False,
) -> None:
    """Repair a row so only current, explicit reasons keep it pending."""
    was_pending = row.get("needs_review", "").casefold() == "true"
    reasons = set(review_reason_tokens(row.get("review_reasons", "")))
    pending_evidence = was_pending or bool(reasons)
    flags = set(filter(None, row.get("flags", "").split(";")))
    manual = "manual_correction" in flags
    category = row.get("category", "")
    flow_type = row.get("flow_type", "")

    if pending_evidence and category in {"", "Unknown"}:
        reasons.add(REVIEW_REASON_CATEGORY)
    else:
        reasons.discard(REVIEW_REASON_CATEGORY)

    if (
        legacy
        and was_pending
        and not manual
        and category not in {"", "Unknown"}
        and "ollama_categorized" in flags
    ):
        reasons.add(REVIEW_REASON_CATEGORY_SUGGESTION)
    elif manual or not pending_evidence or category in {"", "Unknown"}:
        reasons.discard(REVIEW_REASON_CATEGORY_SUGGESTION)

    if pending_evidence and flow_type in {"", "unresolved"}:
        reasons.add(REVIEW_REASON_ACCOUNTING_FLOW)
    else:
        reasons.discard(REVIEW_REASON_ACCOUNTING_FLOW)

    if has_identity_review_evidence(row):
        reasons.add(REVIEW_REASON_IDENTITY)
    else:
        reasons.discard(REVIEW_REASON_IDENTITY)

    if flags & SOURCE_DATA_FLAGS:
        reasons.add(REVIEW_REASON_SOURCE_DATA)
    else:
        reasons.discard(REVIEW_REASON_SOURCE_DATA)

    if not pending_evidence or row.get("owner", "") not in {"", "Unknown"}:
        reasons.discard(REVIEW_REASON_OWNERSHIP)

    # A legacy true boolean is not evidence for an untyped human decision.
    # Only the new structured field can carry ``other_decision`` forward.
    if legacy and not row.get("review_reasons"):
        reasons.discard(REVIEW_REASON_OTHER)

    ordered = [item for item in ALLOWED_REVIEW_REASONS if item in reasons]
    replace_review_reasons(row, sorted(ordered))
    if category not in {"", "Unknown"}:
        row["flags"] = ";".join(
            item for item in row.get("flags", "").split(";") if item != "uncategorized"
        )


def synchronize_review_states(
    rows: Iterable[dict[str, str]],
    *,
    legacy: bool = False,
) -> None:
    for row in rows:
        synchronize_review_state(row, legacy=legacy)


def validate_review_reason_value(value: str) -> None:
    tokens = [item.strip() for item in value.split(";") if item.strip()]
    unknown = set(tokens) - ALLOWED_REVIEW_REASONS
    if unknown:
        raise ValueError("Unsupported review reasons: " + ", ".join(sorted(unknown)))


def review_summary(rows: Iterable[Mapping[str, str]]) -> dict[str, int]:
    counts = {reason: 0 for reason in sorted(ALLOWED_REVIEW_REASONS)}
    for row in rows:
        for reason in review_reason_tokens(str(row.get("review_reasons", ""))):
            counts[reason] += 1
    return counts
