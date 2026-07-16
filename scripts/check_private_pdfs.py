"""Locally verify real PDF parsing against private, ignored snapshots."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from honeymoney.cli import (  # noqa: E402
    _import_pdf,
    _validate_config_document,
    _validate_profile,
)

DEFAULT_ROOT = REPO_ROOT / "private_samples" / "pdf_acceptance"
DEFAULT_CONFIG = REPO_ROOT / "examples" / "config.json"
CASE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

PARSER_COLUMNS = [
    "date",
    "transaction_date",
    "posting_date",
    "account_id",
    "account",
    "account_type",
    "institution",
    "country",
    "original_amount",
    "original_currency",
    "posted_amount",
    "posted_currency",
    "amount_hkd",
    "statement_opening_balance",
    "statement_closing_balance",
    "merchant",
    "original_description",
    "source_page",
    "source_row",
]


class AcceptanceError(ValueError):
    """A safe, user-facing private acceptance suite error."""


@dataclass(frozen=True)
class AcceptanceCase:
    name: str
    pdf_path: Path
    profile_id: str


def _initialize(root: Path) -> None:
    for directory in ("statements", "actual", "expected"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    manifest = root / "cases.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps({"version": 1, "cases": []}, indent=2) + "\n",
            encoding="utf-8",
        )


def _ensure_private_root(root: Path) -> Path:
    private_root = (REPO_ROOT / "private_samples").resolve()
    resolved = root.expanduser().resolve()
    if not resolved.is_relative_to(private_root):
        raise AcceptanceError(
            f"Acceptance root must stay inside the ignored directory {private_root}"
        )
    return resolved


def _load_cases(root: Path) -> list[AcceptanceCase]:
    manifest_path = root / "cases.json"
    if not manifest_path.exists():
        raise AcceptanceError(f"Missing {manifest_path}; run the init command first")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise AcceptanceError(f"Could not read {manifest_path}: {error}") from error
    if not isinstance(document, dict) or document.get("version") != 1:
        raise AcceptanceError("cases.json must be an object with version 1")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise AcceptanceError("cases.json field cases must be an array")

    cases: list[AcceptanceCase] = []
    names: set[str] = set()
    resolved_root = root.resolve()
    for index, raw_case in enumerate(raw_cases, start=1):
        if not isinstance(raw_case, dict):
            raise AcceptanceError(f"case {index} must be an object")
        name = str(raw_case.get("name", "")).strip()
        pdf_value = str(raw_case.get("pdf", "")).strip()
        profile_id = str(raw_case.get("profile", "")).strip()
        if not CASE_NAME_PATTERN.fullmatch(name):
            raise AcceptanceError(
                f"case {index} name must use only letters, numbers, dot, dash, or underscore"
            )
        if name in names:
            raise AcceptanceError(f"duplicate case name: {name}")
        if not pdf_value or not profile_id:
            raise AcceptanceError(f"case {name} requires pdf and profile")
        relative_pdf = Path(pdf_value)
        if relative_pdf.is_absolute():
            raise AcceptanceError(
                f"case {name} pdf must be relative to the acceptance root"
            )
        pdf_path = (root / relative_pdf).resolve()
        if not pdf_path.is_relative_to(resolved_root):
            raise AcceptanceError(
                f"case {name} pdf must stay inside the acceptance root"
            )
        if pdf_path.suffix.lower() != ".pdf":
            raise AcceptanceError(f"case {name} input must be a PDF")
        if not pdf_path.is_file():
            raise AcceptanceError(f"case {name} PDF does not exist: {pdf_path}")
        cases.append(AcceptanceCase(name, pdf_path, profile_id))
        names.add(name)
    return cases


def _add_case(root: Path, pdf_value: Path, profile_id: str, name: str | None) -> str:
    existing = _load_cases(root)
    pdf_path = pdf_value.expanduser()
    if not pdf_path.is_absolute():
        from_cwd = pdf_path.resolve()
        pdf_path = from_cwd if from_cwd.exists() else (root / pdf_path).resolve()
    else:
        pdf_path = pdf_path.resolve()
    if not pdf_path.is_relative_to(root.resolve()):
        raise AcceptanceError("PDF must stay inside the private acceptance root")
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        raise AcceptanceError(f"PDF does not exist: {pdf_path}")

    case_name = name or re.sub(r"[^A-Za-z0-9._-]+", "-", pdf_path.stem).strip(".-_")
    if not case_name or not CASE_NAME_PATTERN.fullmatch(case_name):
        raise AcceptanceError(
            "Case name must use only letters, numbers, dot, dash, or underscore"
        )
    manifest_path = root / "cases.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing_cases = document.get("cases", [])
    if any(case.name == case_name for case in existing):
        raise AcceptanceError(f"duplicate case name: {case_name}")
    existing_cases.append(
        {
            "name": case_name,
            "pdf": pdf_path.relative_to(root.resolve()).as_posix(),
            "profile": profile_id,
        }
    )
    manifest_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return case_name


def _load_config_and_profiles(
    config_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    if not config_path.is_file():
        raise AcceptanceError(
            f"Config file does not exist: {config_path}. "
            "Pass --config with your local workspace config."
        )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise AcceptanceError(
            f"Could not read config {config_path}: {error}"
        ) from error
    if not isinstance(config, dict):
        raise AcceptanceError("Config must be a JSON object")
    _validate_config_document(config)

    profiles: dict[str, dict[str, Any]] = {}
    for configured_path in config.get("profiles", []):
        profile_path = Path(str(configured_path)).expanduser()
        if not profile_path.is_absolute():
            profile_path = (REPO_ROOT / profile_path).resolve()
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            raise AcceptanceError(
                f"Could not read profile {profile_path}: {error}"
            ) from error
        if not isinstance(profile, dict):
            raise AcceptanceError(f"Profile must be an object: {profile_path}")
        _validate_profile(profile, profile_path, config)
        profile_id = str(profile.get("id") or profile.get("account_id") or "")
        if "pdf" not in profile:
            continue
        if profile_id in profiles:
            raise AcceptanceError(f"Duplicate PDF profile id: {profile_id}")
        profiles[profile_id] = profile
    if not profiles:
        raise AcceptanceError(f"No PDF profiles are configured in {config_path}")
    return config, profiles


def _write_snapshot(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=PARSER_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _document_sha256(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_snapshot(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def _compare_snapshots(expected_path: Path, actual_path: Path) -> list[str]:
    expected_columns, expected_rows = _read_snapshot(expected_path)
    actual_columns, actual_rows = _read_snapshot(actual_path)
    differences: list[str] = []
    if expected_columns != actual_columns:
        return ["snapshot columns changed"]
    if len(expected_rows) != len(actual_rows):
        differences.append(
            f"row count changed: expected {len(expected_rows)}, got {len(actual_rows)}"
        )
    for row_number, (expected, actual) in enumerate(
        zip(expected_rows, actual_rows), start=1
    ):
        for field in expected_columns:
            if expected.get(field, "") != actual.get(field, ""):
                differences.append(f"row {row_number}: {field} changed")
    return differences


def _prepare_case(
    root: Path,
    case: AcceptanceCase,
    config: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> tuple[Path, list[str]]:
    profile = profiles.get(case.profile_id)
    if profile is None:
        choices = ", ".join(sorted(profiles))
        raise AcceptanceError(
            f"case {case.name} uses unknown profile {case.profile_id}; "
            f"available PDF profiles: {choices}"
        )
    rows, warnings = _import_pdf(case.pdf_path, profile, config, case.pdf_path.parent)
    actual_path = root / "actual" / f"{case.name}.csv"
    _write_snapshot(actual_path, rows)
    status_path = root / "actual" / f"{case.name}.status.json"
    status_path.write_text(
        json.dumps(
            {
                "profile": case.profile_id,
                "row_count": len(rows),
                "warning_count": len(warnings),
                "pdf_sha256": _file_sha256(case.pdf_path),
                "snapshot_sha256": _file_sha256(actual_path),
                "profile_sha256": _document_sha256(profile),
                "config_sha256": _document_sha256(config),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return actual_path, warnings


def _accept_snapshot(
    root: Path,
    case_name: str,
    config: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> Path:
    if not CASE_NAME_PATTERN.fullmatch(case_name):
        raise AcceptanceError("Invalid case name")
    actual_path = root / "actual" / f"{case_name}.csv"
    status_path = root / "actual" / f"{case_name}.status.json"
    if not actual_path.is_file() or not status_path.is_file():
        raise AcceptanceError(
            f"case {case_name} has not been prepared; run prepare first"
        )
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise AcceptanceError(
            f"Could not read prepare status for {case_name}"
        ) from error
    if status.get("warning_count"):
        raise AcceptanceError(
            f"case {case_name} has parser warnings and cannot be accepted"
        )
    if int(status.get("row_count", 0)) < 1:
        raise AcceptanceError(
            f"case {case_name} has no parsed transactions and cannot be accepted"
        )
    [case] = _selected_cases(_load_cases(root), case_name)
    profile = profiles.get(case.profile_id)
    if profile is None:
        raise AcceptanceError(
            f"case {case_name} profile is not available in the selected config"
        )
    prepared_identity = {
        "profile": case.profile_id,
        "pdf_sha256": _file_sha256(case.pdf_path),
        "snapshot_sha256": _file_sha256(actual_path),
        "profile_sha256": _document_sha256(profile),
        "config_sha256": _document_sha256(config),
    }
    if any(status.get(field) != value for field, value in prepared_identity.items()):
        raise AcceptanceError(
            f"case {case_name} changed after preparation; run prepare and inspect it again"
        )
    destination = root / "expected" / f"{case_name}.csv"
    shutil.copyfile(actual_path, destination)
    return destination


def _selected_cases(
    cases: list[AcceptanceCase], selected: str | None
) -> list[AcceptanceCase]:
    if selected is None:
        return cases
    matches = [case for case in cases if case.name == selected]
    if not matches:
        raise AcceptanceError(f"Unknown case: {selected}")
    return matches


def _prepare_command(
    root: Path, config_path: Path, selected: str | None, *, check: bool
) -> int:
    cases = _selected_cases(_load_cases(root), selected)
    if not cases:
        raise AcceptanceError(
            f"No cases configured in {root / 'cases.json'}; add a PDF case first"
        )
    config, profiles = _load_config_and_profiles(config_path)
    failed = False
    for case in cases:
        try:
            actual_path, warnings = _prepare_case(root, case, config, profiles)
        except AcceptanceError as error:
            print(f"FAIL {case.name}: {error}", file=sys.stderr)
            failed = True
            continue
        except Exception as error:
            print(
                f"FAIL {case.name}: parser raised {type(error).__name__}; "
                "transaction values were suppressed",
                file=sys.stderr,
            )
            failed = True
            continue
        if warnings:
            print(
                f"FAIL {case.name}: parser produced {len(warnings)} warning(s)",
                file=sys.stderr,
            )
            for warning in warnings:
                print(f"  {warning}", file=sys.stderr)
            failed = True
            continue
        _, actual_rows = _read_snapshot(actual_path)
        if not actual_rows:
            print(
                f"FAIL {case.name}: parser produced no transactions",
                file=sys.stderr,
            )
            failed = True
            continue
        if not check:
            print(f"READY {case.name}: manually inspect {actual_path}")
            continue
        expected_path = root / "expected" / f"{case.name}.csv"
        if not expected_path.is_file():
            print(
                f"FAIL {case.name}: no accepted snapshot; prepare, inspect, then accept it",
                file=sys.stderr,
            )
            failed = True
            continue
        differences = _compare_snapshots(expected_path, actual_path)
        if differences:
            print(
                f"FAIL {case.name}: {len(differences)} parser difference(s)",
                file=sys.stderr,
            )
            for difference in differences[:50]:
                print(f"  {difference}", file=sys.stderr)
            if len(differences) > 50:
                print(f"  ... {len(differences) - 50} more", file=sys.stderr)
            failed = True
        else:
            print(f"PASS {case.name}")
    return 1 if failed else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify real PDFs locally without committing private data."
    )
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help="private acceptance root"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="create the ignored local workspace")

    profiles = subparsers.add_parser("profiles", help="list configured PDF profiles")
    profiles.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    add = subparsers.add_parser("add", help="register a PDF already under the root")
    add.add_argument("pdf", type=Path)
    add.add_argument("--profile", required=True)
    add.add_argument("--name")
    add.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    for command in ("prepare", "check"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        command_parser.add_argument("--case", dest="case_name")

    accept = subparsers.add_parser(
        "accept", help="accept one manually inspected candidate"
    )
    accept.add_argument("case_name", nargs="?", help=argparse.SUPPRESS)
    accept.add_argument("--case", dest="case_option", help="case name to accept")
    accept.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = _ensure_private_root(args.root)
        if args.command == "init":
            _initialize(root)
            print(f"Private PDF acceptance workspace ready at {root}")
            return 0
        if args.command == "profiles":
            _, profiles = _load_config_and_profiles(args.config.resolve())
            for profile_id in sorted(profiles):
                print(profile_id)
            return 0
        if args.command == "add":
            _, profiles = _load_config_and_profiles(args.config.resolve())
            if args.profile not in profiles:
                choices = ", ".join(sorted(profiles))
                raise AcceptanceError(
                    f"Unknown PDF profile {args.profile}; available profiles: {choices}"
                )
            case_name = _add_case(root, args.pdf, args.profile, args.name)
            print(f"Added {case_name}; run prepare to create its review CSV")
            return 0
        if args.command == "accept":
            if args.case_name and args.case_option:
                raise AcceptanceError(
                    "Pass the case name with --case or as a positional argument, not both"
                )
            case_name = args.case_option or args.case_name
            if not case_name:
                raise AcceptanceError("accept requires --case CASE_NAME")
            config, profiles = _load_config_and_profiles(args.config.resolve())
            destination = _accept_snapshot(root, case_name, config, profiles)
            print(f"Accepted {case_name}: {destination}")
            return 0
        return _prepare_command(
            root,
            args.config.resolve(),
            args.case_name,
            check=args.command == "check",
        )
    except (AcceptanceError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
