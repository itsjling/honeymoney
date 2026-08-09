"""Strict, value-free workspace-index authority."""

from __future__ import annotations

import json
import re
import secrets
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Never, TypedDict

from honeymoney.identity import empty_manifest, manifest_document, parse_manifest
from honeymoney.identity_contracts import IdentityManifest
from honeymoney.import_records import (
    ATTEMPT_SCHEMA_VERSION,
    IMPORT_RECORD_SCHEMA_VERSION,
    TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
)
from honeymoney.overlap import (
    OVERLAP_MANIFEST_SCHEMA_VERSION,
    empty_overlap_manifest,
    overlap_manifest_document,
    parse_overlap_manifest,
)
from honeymoney.overlap_contracts import OverlapManifest
from honeymoney.persistence import private_atomic_write_text

WORKSPACE_INDEX_SCHEMA_VERSION = 2
WORKSPACE_INDEX_NAME = "workspace-index.json"
WORKSPACE_STATE_DIRECTORY = ".honeymoney"
WORKSPACE_INDEX_RELATIVE_PATH = Path(WORKSPACE_STATE_DIRECTORY) / WORKSPACE_INDEX_NAME
HONEYMONEY_VERSION = "0.2.0"
DERIVATION_CONTRACT = sha256(b"honeymoney-workspace-derivation-v4").hexdigest()
MODEL_DERIVATION_CONTRACT = sha256(
    b"honeymoney-workspace-derivation-v4-model-allowed"
).hexdigest()
_GENERATION_ID = re.compile(r"gen_[0-9a-f]{64}")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_PERIOD = re.compile(r"(?:[0-9]{4}-(?:0[1-9]|1[0-2])|undated)")
_PROOF_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")


class WorkspaceIndexError(ValueError):
    """A stable workspace-index validation failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkspaceContracts(TypedDict):
    honeymoney_version: str
    import_record_schema_version: int
    attempt_schema_version: int
    transaction_schema_version: int
    derivation_contract: str


class RegisteredView(TypedDict):
    period: str
    content_proof: str


class InputProof(TypedDict):
    name: str
    proof: str


class WorkspaceIndex(TypedDict):
    schema_version: int
    generation_id: str
    contracts: WorkspaceContracts
    identity_manifest: IdentityManifest
    overlap_manifest: OverlapManifest
    registered_views: list[RegisteredView]
    input_proofs: list[InputProof]


def workspace_index_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / WORKSPACE_INDEX_RELATIVE_PATH


def empty_workspace_index(
    generation_id: str | None = None,
    contracts: WorkspaceContracts | None = None,
    *,
    overlap_namespace: str | None = None,
) -> WorkspaceIndex:
    """Return a valid empty authority with fresh private workspace keys."""
    actual_contracts: WorkspaceContracts = contracts or {
        "honeymoney_version": HONEYMONEY_VERSION,
        "import_record_schema_version": IMPORT_RECORD_SCHEMA_VERSION,
        "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
        "transaction_schema_version": TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
        "derivation_contract": "0" * 64,
    }
    value: WorkspaceIndex = {
        "schema_version": WORKSPACE_INDEX_SCHEMA_VERSION,
        "generation_id": generation_id or f"gen_{secrets.token_hex(32)}",
        "contracts": actual_contracts,
        "identity_manifest": empty_manifest(),
        "overlap_manifest": empty_overlap_manifest(
            overlap_namespace or f"ovns_{secrets.token_hex(32)}"
        ),
        "registered_views": [],
        "input_proofs": [],
    }
    return validate_workspace_index(value)


def workspace_index_document(index: Mapping[str, object]) -> str:
    checked = validate_workspace_index(index)
    return (
        json.dumps(
            checked,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def parse_workspace_index(document: str) -> WorkspaceIndex:
    try:
        value = json.loads(document)
    except (TypeError, json.JSONDecodeError) as error:
        raise WorkspaceIndexError("workspace_index_invalid") from error
    if not isinstance(value, dict):
        raise WorkspaceIndexError("workspace_index_invalid")
    checked = validate_workspace_index(value)
    if workspace_index_document(checked) != document:
        raise WorkspaceIndexError("workspace_index_noncanonical")
    return checked


def load_workspace_index(path: Path) -> WorkspaceIndex:
    try:
        return parse_workspace_index(Path(path).read_text(encoding="utf-8"))
    except WorkspaceIndexError:
        raise
    except (OSError, UnicodeError) as error:
        raise WorkspaceIndexError("workspace_index_unreadable") from error


def load_compatible_workspace_index(path: Path) -> WorkspaceIndex:
    """Load an index only when this release supports every saved contract."""
    if _has_newer_index_schema(path):
        raise WorkspaceIndexError("newer_honeymoney_required")
    index = load_workspace_index(path)
    if workspace_index_requires_newer_honeymoney(index):
        raise WorkspaceIndexError("newer_honeymoney_required")
    return index


def workspace_index_requires_newer_honeymoney(
    index: Mapping[str, object],
) -> bool:
    """Return whether saved contracts exceed the current release."""
    contracts = index.get("contracts")
    if not isinstance(contracts, Mapping):
        return False
    supported = {
        "import_record_schema_version": IMPORT_RECORD_SCHEMA_VERSION,
        "attempt_schema_version": ATTEMPT_SCHEMA_VERSION,
        "transaction_schema_version": TRANSACTION_SNAPSHOT_SCHEMA_VERSION,
    }
    if any(
        isinstance(contracts.get(name), int)
        and not isinstance(contracts.get(name), bool)
        and contracts[name] > version
        for name, version in supported.items()
    ):
        return True
    version = contracts.get("honeymoney_version")
    if isinstance(version, str) and _version_tuple(version) > _version_tuple(
        HONEYMONEY_VERSION
    ):
        return True
    derivation = contracts.get("derivation_contract")
    return (
        derivation != "0" * 64
        and bool(_index_has_authority(index))
        and derivation not in {DERIVATION_CONTRACT, MODEL_DERIVATION_CONTRACT}
    )


def derivation_contract_for_model(*, model_allowed: bool) -> str:
    """Return the saved contract for one complete workspace derivation."""
    return MODEL_DERIVATION_CONTRACT if model_allowed else DERIVATION_CONTRACT


def derivation_contract_is_rederivable(index: Mapping[str, object]) -> bool:
    """Return whether doctor can recreate generated views without a model."""
    contracts = index.get("contracts")
    return isinstance(contracts, Mapping) and contracts.get("derivation_contract") in {
        "0" * 64,
        DERIVATION_CONTRACT,
    }


def write_workspace_index(path: Path, index: Mapping[str, object]) -> None:
    private_atomic_write_text(Path(path), workspace_index_document(index))


def validate_workspace_index(index: Mapping[str, object]) -> WorkspaceIndex:
    expected = {
        "schema_version",
        "generation_id",
        "contracts",
        "identity_manifest",
        "overlap_manifest",
        "registered_views",
        "input_proofs",
    }
    if set(index) != expected:
        _invalid()
    if index.get("schema_version") != WORKSPACE_INDEX_SCHEMA_VERSION:
        raise WorkspaceIndexError("workspace_index_schema_unsupported")
    generation = index.get("generation_id")
    if not isinstance(generation, str) or _GENERATION_ID.fullmatch(generation) is None:
        _invalid()
    contracts = _contracts(index.get("contracts"))
    identity = _identity_manifest(index.get("identity_manifest"))
    overlap = _overlap_manifest(index.get("overlap_manifest"))
    _validate_active_overlap_support(identity, overlap)
    views = _registered_views(index.get("registered_views"))
    proofs = _input_proofs(index.get("input_proofs"))
    return {
        "schema_version": WORKSPACE_INDEX_SCHEMA_VERSION,
        "generation_id": generation,
        "contracts": contracts,
        "identity_manifest": identity,
        "overlap_manifest": overlap,
        "registered_views": views,
        "input_proofs": proofs,
    }


def _contracts(value: object) -> WorkspaceContracts:
    expected = {
        "honeymoney_version",
        "import_record_schema_version",
        "attempt_schema_version",
        "transaction_schema_version",
        "derivation_contract",
    }
    if not isinstance(value, dict) or set(value) != expected:
        _invalid()
    version = value.get("honeymoney_version")
    derivation = value.get("derivation_contract")
    if not isinstance(version, str) or not version:
        _invalid()
    if not isinstance(derivation, str) or _HEX_64.fullmatch(derivation) is None:
        _invalid()
    result: WorkspaceContracts = {
        "honeymoney_version": version,
        "import_record_schema_version": _positive_version(
            value.get("import_record_schema_version")
        ),
        "attempt_schema_version": _positive_version(
            value.get("attempt_schema_version")
        ),
        "transaction_schema_version": _positive_version(
            value.get("transaction_schema_version")
        ),
        "derivation_contract": derivation,
    }
    return result


def _identity_manifest(value: object) -> IdentityManifest:
    if not isinstance(value, dict):
        _invalid()
    try:
        return parse_manifest(manifest_document(value))
    except TypeError, ValueError:
        _invalid()


def _overlap_manifest(value: object) -> OverlapManifest:
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != OVERLAP_MANIFEST_SCHEMA_VERSION
    ):
        _invalid()
    try:
        parsed = parse_overlap_manifest(overlap_manifest_document(value))
    except TypeError, ValueError:
        _invalid()
    if parsed.get("schema_version") != OVERLAP_MANIFEST_SCHEMA_VERSION:
        _invalid()
    return parsed


def _validate_active_overlap_support(
    identity: IdentityManifest,
    overlap: OverlapManifest,
) -> None:
    """Require exact group support to cover each active identity record once."""
    active_links = {
        (source["source_id"], record["source_record_id"])
        for source in identity["sources"]
        for record in source["records"]
        if record["state"] == "active"
    }
    supported_links = {
        (pool["source_id"], source_record_id)
        for group in overlap["groups"]
        for pool in group["support_pools"]
        for source_record_id in pool["source_record_ids"]
    }
    if supported_links != active_links:
        _invalid()


def _registered_views(value: object) -> list[RegisteredView]:
    if not isinstance(value, list):
        _invalid()
    result: list[RegisteredView] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"period", "content_proof"}:
            _invalid()
        period = item.get("period")
        proof = item.get("content_proof")
        if (
            not isinstance(period, str)
            or _PERIOD.fullmatch(period) is None
            or period in seen
            or not isinstance(proof, str)
            or _HEX_64.fullmatch(proof) is None
        ):
            _invalid()
        seen.add(period)
        result.append({"period": period, "content_proof": proof})
    if [item["period"] for item in result] != sorted(seen):
        _invalid()
    return result


def _input_proofs(value: object) -> list[InputProof]:
    if not isinstance(value, list):
        _invalid()
    result: list[InputProof] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "proof"}:
            _invalid()
        name = item.get("name")
        proof = item.get("proof")
        if (
            not isinstance(name, str)
            or _PROOF_NAME.fullmatch(name) is None
            or name in seen
            or not isinstance(proof, str)
            or _HEX_64.fullmatch(proof) is None
        ):
            _invalid()
        seen.add(name)
        result.append({"name": name, "proof": proof})
    if [item["name"] for item in result] != sorted(seen):
        _invalid()
    return result


def _positive_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        _invalid()
    return value


def _has_newer_index_schema(path: Path) -> bool:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return False
    return (
        isinstance(value, dict)
        and isinstance(value.get("schema_version"), int)
        and not isinstance(value.get("schema_version"), bool)
        and value["schema_version"] > WORKSPACE_INDEX_SCHEMA_VERSION
    )


def _index_has_authority(index: Mapping[str, object]) -> bool:
    identity = index.get("identity_manifest")
    sources = identity.get("sources") if isinstance(identity, Mapping) else None
    return bool(sources or index.get("registered_views"))


def _version_tuple(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(
        not part.isascii() or not part.isdecimal() for part in parts
    ):
        return (0, 0, 0)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def _invalid() -> Never:
    raise WorkspaceIndexError("workspace_index_invalid")
