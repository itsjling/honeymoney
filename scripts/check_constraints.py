#!/usr/bin/env python3
"""Verify the installed development dependency closure against constraints."""

from __future__ import annotations

import argparse
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

BOOTSTRAP_TOOLS = {"pip"}
ROOT_PACKAGE = "honeymoney"
ROOT_EXTRAS = {"dev", "pdf"}


def _constraint_versions(path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if len(specifiers) != 1 or specifiers[0].operator != "==":
            raise ValueError(
                f"{path}:{line_number} must contain one exact == constraint"
            )
        versions[canonicalize_name(requirement.name)] = specifiers[0].version
    return versions


def _requirement_is_active(requirement: Requirement, extras: set[str]) -> bool:
    if requirement.marker is None:
        return True
    environments = []
    for extra in {"", *extras}:
        environment = default_environment()
        environment["extra"] = extra
        environments.append(environment)
    return any(requirement.marker.evaluate(environment) for environment in environments)


def _installed_closure() -> dict[str, metadata.Distribution]:
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in metadata.distributions()
        if distribution.metadata["Name"]
    }
    if ROOT_PACKAGE not in installed:
        raise ValueError("honeymoney is not installed; run ./scripts/bootstrap.sh")

    requested_extras: dict[str, set[str]] = {ROOT_PACKAGE: set(ROOT_EXTRAS)}
    pending = [ROOT_PACKAGE]
    while pending:
        name = pending.pop()
        distribution = installed.get(name)
        if distribution is None:
            raise ValueError(f"Required distribution is not installed: {name}")
        extras = requested_extras[name]
        for raw_requirement in distribution.requires or []:
            requirement = Requirement(raw_requirement)
            if not _requirement_is_active(requirement, extras):
                continue
            dependency = canonicalize_name(requirement.name)
            previous_extras = requested_extras.setdefault(dependency, set())
            discovered_extras = set(requirement.extras) - previous_extras
            if dependency not in installed:
                raise ValueError(
                    f"Required distribution is not installed: {dependency}"
                )
            if dependency not in pending and (not previous_extras or discovered_extras):
                previous_extras.update(requirement.extras)
                pending.append(dependency)

    return {
        name: installed[name]
        for name in requested_extras
        if name not in {ROOT_PACKAGE, *BOOTSTRAP_TOOLS}
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "constraints", nargs="?", default="constraints/dev.txt", type=Path
    )
    args = parser.parse_args()

    expected = _constraint_versions(args.constraints)
    closure = _installed_closure()
    errors = []
    for name, distribution in sorted(closure.items()):
        expected_version = expected.get(name)
        if expected_version is None:
            errors.append(f"{name}=={distribution.version} is not constrained")
        elif distribution.version != expected_version:
            errors.append(
                f"{name} installed at {distribution.version}, "
                f"constrained to {expected_version}"
            )
    if errors:
        raise ValueError("Dependency constraint mismatch:\n- " + "\n- ".join(errors))

    print(f"Dependency constraints verified for {len(closure)} installed packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
