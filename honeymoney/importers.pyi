from pathlib import Path
from typing import Mapping

from honeymoney.parser_contracts import Profile

def _load_profiles(config: Mapping[str, object]) -> list[Profile]: ...
def _validate_profile(
    profile: object,
    profile_path: Path,
    config: Mapping[str, object],
) -> Profile: ...
