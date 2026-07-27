from typing import Mapping

MANAGED_RULE_MARKER: str

def canonical_rule_amount(value: str) -> str | None: ...
def normalize_exact_text(value: str) -> str: ...
def validate_rules(
    rules: list[dict[str, object]],
    config: Mapping[str, object] | None = None,
) -> None: ...
