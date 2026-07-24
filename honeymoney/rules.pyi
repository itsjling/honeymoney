from typing import Mapping

def validate_rules(
    rules: list[dict[str, object]],
    config: Mapping[str, object] | None = None,
) -> None: ...
