from typing import TypeAlias

from honeymoney.contracts import Config

MANAGED_RULE_MARKER: str
Rule: TypeAlias = dict[str, object]

def canonical_rule_amount(value: str) -> str | None: ...
def normalize_exact_text(value: str) -> str: ...
def validate_rules(
    rules: list[Rule],
    config: Config | None = None,
) -> None: ...
def load_rules(config: Config) -> list[Rule]: ...
def apply_rules(
    transactions: list[dict[str, str]],
    rules: list[Rule],
    config: Config,
) -> None: ...
