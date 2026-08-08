"""Validated account bindings kept beside user-owned profile mappings."""

from __future__ import annotations

import copy
import unicodedata
from fnmatch import fnmatch
from pathlib import Path
from typing import Mapping, Sequence, TypedDict, cast

from honeymoney.parser_contracts import Profile
from honeymoney.schema import allowed_owners

_BOUND_OWNER_FIELD = "_honeymoney_bound_owner"


class BoundAccount(TypedDict):
    source_account_id: str
    account_id: str
    account: str


class AccountBinding(TypedDict):
    id: str
    profile: str
    owner: str
    accounts: list[BoundAccount]


class AccountBindingError(ValueError):
    """A safe account-binding validation error for CLI output."""


def profile_id(profile: Mapping[str, object]) -> str:
    return str(profile.get("id") or profile.get("account_id") or "")


def upsert_binding(
    mappings: Mapping[str, object],
    binding: AccountBinding,
    pattern: str,
) -> dict[str, object]:
    """Return a copied mapping document with one binding and filename rule."""
    document = copy.deepcopy(dict(mappings))
    _clear_pattern_edit_receipts(document, binding["id"])
    next_bindings = [
        item
        for item in _mapping_list(document, "account_bindings")
        if not isinstance(item, Mapping) or item.get("id") != binding["id"]
    ]
    next_bindings.append(dict(binding))
    next_bindings.sort(
        key=lambda item: str(item.get("id", "")) if isinstance(item, Mapping) else ""
    )
    document["account_bindings"] = next_bindings

    next_patterns: list[object] = []
    pattern_found = False
    for item in _mapping_list(document, "filename_patterns"):
        if not isinstance(item, Mapping):
            next_patterns.append(copy.deepcopy(item))
            continue
        next_item = dict(item)
        if next_item.get("pattern") == pattern:
            if next_item.get("profile") != binding["profile"] or next_item.get(
                "binding"
            ) not in {None, binding["id"]}:
                raise AccountBindingError(
                    f"Filename pattern {pattern} already selects another "
                    "profile or binding"
                )
            next_item["binding"] = binding["id"]
            pattern_found = True
        next_patterns.append(next_item)
    if not pattern_found:
        next_patterns.append(
            {
                "pattern": pattern,
                "profile": binding["profile"],
                "binding": binding["id"],
            }
        )
    document["filename_patterns"] = next_patterns
    return document


def replace_binding_pattern(
    mappings: Mapping[str, object],
    binding_id: str,
    old_pattern: str,
    new_pattern: str,
) -> tuple[dict[str, object], bool]:
    """Return mappings with one binding pattern replaced and whether they changed."""
    document = copy.deepcopy(dict(mappings))
    binding = next(
        (
            item
            for item in _mapping_list(document, "account_bindings")
            if isinstance(item, Mapping) and item.get("id") == binding_id
        ),
        None,
    )
    if binding is None:
        raise AccountBindingError(f"Unknown account binding: {binding_id}")
    selected_profile = str(binding.get("profile", ""))
    patterns = _mapping_list(document, "filename_patterns")
    receipts = _mapping_list(document, "replaced_filename_patterns")
    receipt = next(
        (
            item
            for item in receipts
            if isinstance(item, Mapping)
            and item.get("binding") == binding_id
            and item.get("old_pattern") == old_pattern
            and item.get("new_pattern") == new_pattern
        ),
        None,
    )
    old_index = next(
        (
            index
            for index, item in enumerate(patterns)
            if isinstance(item, Mapping)
            and item.get("pattern") == old_pattern
            and item.get("binding") == binding_id
        ),
        None,
    )
    new_index = next(
        (
            index
            for index, item in enumerate(patterns)
            if isinstance(item, Mapping) and item.get("pattern") == new_pattern
        ),
        None,
    )
    if old_index is None:
        if receipt is not None and new_index is not None:
            selected = patterns[new_index]
            if (
                isinstance(selected, Mapping)
                and selected.get("profile") == selected_profile
                and selected.get("binding") == binding_id
            ):
                return document, False
        raise AccountBindingError(
            f"Account binding {binding_id} does not use filename pattern {old_pattern}"
        )
    if old_pattern == new_pattern:
        return document, False
    if new_index is not None:
        selected = patterns[new_index]
        if (
            not isinstance(selected, Mapping)
            or selected.get("profile") != selected_profile
            or selected.get("binding") != binding_id
        ):
            raise AccountBindingError(
                f"Filename pattern {new_pattern} already selects another "
                "profile or binding"
            )
        del patterns[old_index]
    else:
        patterns[old_index] = {
            "pattern": new_pattern,
            "profile": selected_profile,
            "binding": binding_id,
        }
    document["filename_patterns"] = patterns
    _clear_pattern_edit_receipts(document, binding_id)
    replacement_receipts = _mapping_list(document, "replaced_filename_patterns")
    replacement_receipts.append(
        {
            "binding": binding_id,
            "old_pattern": old_pattern,
            "new_pattern": new_pattern,
            "profile": selected_profile,
        }
    )
    document["replaced_filename_patterns"] = replacement_receipts
    return document, True


def remove_binding_pattern(
    mappings: Mapping[str, object],
    binding_id: str,
    pattern: str,
    *,
    confirm_final: bool,
) -> tuple[dict[str, object], bool, bool, str]:
    """Remove one pattern and its binding when no other pattern uses it."""
    document = copy.deepcopy(dict(mappings))
    receipts = _mapping_list(document, "removed_filename_patterns")
    receipt = next(
        (
            item
            for item in receipts
            if isinstance(item, Mapping)
            and item.get("binding") == binding_id
            and item.get("pattern") == pattern
        ),
        None,
    )
    bindings = _mapping_list(document, "account_bindings")
    binding_index = next(
        (
            index
            for index, item in enumerate(bindings)
            if isinstance(item, Mapping) and item.get("id") == binding_id
        ),
        None,
    )
    if binding_index is None:
        if receipt is not None:
            return document, False, True, str(receipt.get("profile", ""))
        raise AccountBindingError(f"Unknown account binding: {binding_id}")
    binding = bindings[binding_index]
    if not isinstance(binding, Mapping):
        raise AccountBindingError(f"Unknown account binding: {binding_id}")
    selected_profile = str(binding.get("profile", ""))
    patterns = _mapping_list(document, "filename_patterns")
    pattern_index = next(
        (
            index
            for index, item in enumerate(patterns)
            if isinstance(item, Mapping)
            and item.get("pattern") == pattern
            and item.get("binding") == binding_id
        ),
        None,
    )
    if pattern_index is None:
        if receipt is not None:
            return document, False, False, selected_profile
        raise AccountBindingError(
            f"Account binding {binding_id} does not use filename pattern {pattern}"
        )
    remaining_pattern_count = sum(
        1
        for index, item in enumerate(patterns)
        if index != pattern_index
        and isinstance(item, Mapping)
        and item.get("binding") == binding_id
    )
    removing_binding = remaining_pattern_count == 0
    if removing_binding and not confirm_final:
        raise AccountBindingError(
            f"Removing the final pattern from account binding {binding_id} also "
            "removes the binding; pass --yes to confirm"
        )
    del patterns[pattern_index]
    document["filename_patterns"] = patterns
    if removing_binding:
        del bindings[binding_index]
        document["account_bindings"] = bindings
    _clear_pattern_edit_receipts(document, binding_id)
    removal_receipts = _mapping_list(document, "removed_filename_patterns")
    removal_receipts.append(
        {
            "binding": binding_id,
            "pattern": pattern,
            "profile": selected_profile,
        }
    )
    document["removed_filename_patterns"] = removal_receipts
    return document, True, removing_binding, selected_profile


def validate_profile_mappings(
    mappings: object, config: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(mappings, dict):
        raise ValueError("Profile mappings document must be a JSON object")
    document = cast(dict[str, object], mappings)
    patterns = document.get("filename_patterns", [])
    if not isinstance(patterns, list):
        raise ValueError(
            "Profile mappings field filename_patterns must be a JSON array"
        )

    raw_bindings = document.get("account_bindings", [])
    if not isinstance(raw_bindings, list):
        raise ValueError("Profile mappings field account_bindings must be a JSON array")
    bindings: dict[str, AccountBinding] = {}
    target_owners: dict[str, str] = {}
    for index, raw_binding in enumerate(raw_bindings):
        field = f"account_bindings[{index}]"
        if not isinstance(raw_binding, dict):
            raise ValueError(f"Profile mappings field {field} must be a JSON object")
        for name in ("id", "profile", "owner"):
            if (
                not isinstance(raw_binding.get(name), str)
                or not raw_binding[name].strip()
            ):
                raise ValueError(
                    f"Profile mappings field {field}.{name} must be a non-empty string"
                )
        binding_id = raw_binding["id"].strip()
        if binding_id in bindings:
            raise ValueError(f"Duplicate account binding id: {binding_id}")
        owner = raw_binding["owner"].strip()
        if owner not in allowed_owners(config):
            raise ValueError(
                f"Unsupported owner in account binding {binding_id}: {owner}"
            )
        raw_accounts = raw_binding.get("accounts")
        if not isinstance(raw_accounts, list) or not raw_accounts:
            raise ValueError(
                f"Profile mappings field {field}.accounts must be a non-empty JSON array"
            )
        accounts: list[BoundAccount] = []
        source_ids: set[str] = set()
        for account_index, raw_account in enumerate(raw_accounts):
            account_field = f"{field}.accounts[{account_index}]"
            if not isinstance(raw_account, dict):
                raise ValueError(
                    f"Profile mappings field {account_field} must be a JSON object"
                )
            values: dict[str, str] = {}
            for name in ("source_account_id", "account_id", "account"):
                value = raw_account.get(name)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(
                        f"Profile mappings field {account_field}.{name} "
                        "must be a non-empty string"
                    )
                values[name] = value.strip()
            source_account_id = values["source_account_id"]
            target_account_id = values["account_id"]
            if source_account_id in source_ids:
                raise ValueError(
                    f"Account binding {binding_id} maps source account "
                    f"{source_account_id} more than once"
                )
            source_ids.add(source_account_id)
            target_identity = normalized_account_id(target_account_id)
            prior_binding = target_owners.get(target_identity)
            if prior_binding is not None:
                raise ValueError(
                    "Account identity collision between bindings "
                    f"{prior_binding} and {binding_id}"
                )
            target_owners[target_identity] = binding_id
            accounts.append(
                {
                    "source_account_id": source_account_id,
                    "account_id": target_account_id,
                    "account": values["account"],
                }
            )
        binding: AccountBinding = {
            "id": binding_id,
            "profile": raw_binding["profile"].strip(),
            "owner": owner,
            "accounts": accounts,
        }
        bindings[binding_id] = binding

    raw_removed_patterns = document.get("removed_filename_patterns", [])
    if not isinstance(raw_removed_patterns, list):
        raise ValueError(
            "Profile mappings field removed_filename_patterns must be a JSON array"
        )
    removed_patterns: set[tuple[str, str]] = set()
    for index, raw_removed_pattern in enumerate(raw_removed_patterns):
        field = f"removed_filename_patterns[{index}]"
        if not isinstance(raw_removed_pattern, dict):
            raise ValueError(f"Profile mappings field {field} must be a JSON object")
        removed_values: list[str] = []
        for name in ("binding", "pattern", "profile"):
            value = raw_removed_pattern.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Profile mappings field {field}.{name} must be a non-empty string"
                )
            removed_values.append(value.strip())
        receipt_key = (removed_values[0], removed_values[1])
        if receipt_key in removed_patterns:
            raise ValueError(
                "Duplicate removed filename pattern receipt: "
                f"{removed_values[0]} {removed_values[1]}"
            )
        removed_patterns.add(receipt_key)

    raw_replaced_patterns = document.get("replaced_filename_patterns", [])
    if not isinstance(raw_replaced_patterns, list):
        raise ValueError(
            "Profile mappings field replaced_filename_patterns must be a JSON array"
        )
    replaced_patterns: set[tuple[str, str, str]] = set()
    for index, raw_replaced_pattern in enumerate(raw_replaced_patterns):
        field = f"replaced_filename_patterns[{index}]"
        if not isinstance(raw_replaced_pattern, dict):
            raise ValueError(f"Profile mappings field {field} must be a JSON object")
        replaced_values: list[str] = []
        for name in ("binding", "old_pattern", "new_pattern", "profile"):
            value = raw_replaced_pattern.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"Profile mappings field {field}.{name} must be a non-empty string"
                )
            replaced_values.append(value.strip())
        replaced_receipt_key = (
            replaced_values[0],
            replaced_values[1],
            replaced_values[2],
        )
        if replaced_receipt_key in replaced_patterns:
            raise ValueError(
                "Duplicate replaced filename pattern receipt: "
                f"{replaced_values[0]} {replaced_values[1]} {replaced_values[2]}"
            )
        replaced_patterns.add(replaced_receipt_key)

    seen_patterns: set[str] = set()
    for index, mapping in enumerate(patterns):
        if not isinstance(mapping, dict):
            raise ValueError(
                f"Profile mappings field filename_patterns[{index}] must be a JSON object"
            )
        for field in ("pattern", "profile"):
            if not isinstance(mapping.get(field), str) or not mapping[field].strip():
                raise ValueError(
                    f"Profile mappings field filename_patterns[{index}].{field} "
                    "must be a non-empty string"
                )
        pattern = mapping["pattern"].strip()
        if pattern in seen_patterns:
            raise ValueError(f"Duplicate filename mapping pattern: {pattern}")
        seen_patterns.add(pattern)
        binding_id = mapping.get("binding")
        if binding_id is None:
            continue
        if not isinstance(binding_id, str) or not binding_id.strip():
            raise ValueError(
                f"Profile mappings field filename_patterns[{index}].binding "
                "must be a non-empty string"
            )
        selected_binding = bindings.get(binding_id.strip())
        if selected_binding is None:
            raise ValueError(
                f"Unknown account binding in filename mapping: {binding_id}"
            )
        if selected_binding["profile"] != mapping["profile"].strip():
            raise ValueError(
                f"Filename mapping {pattern} selects profile {mapping['profile']} "
                f"but binding {binding_id} requires profile {selected_binding['profile']}"
            )
    return document


def static_profile_account_ids(profile: Mapping[str, object]) -> set[str] | None:
    pdf = profile.get("pdf")
    if isinstance(pdf, Mapping) and pdf.get("word_rows") == "sectioned":
        sectioned = pdf.get("sectioned_word_rows")
        accounts = sectioned.get("accounts") if isinstance(sectioned, Mapping) else None
        if isinstance(accounts, Mapping):
            return {
                str(account.get("account_id", "")).strip()
                for account in accounts.values()
                if isinstance(account, Mapping)
            }
    parser = profile.get("csv") or profile.get("pdf")
    columns = parser.get("columns") if isinstance(parser, Mapping) else None
    if isinstance(columns, Mapping) and columns.get("account_id"):
        return None
    return {str(profile.get("account_id", "")).strip()}


def validate_bindings_for_profiles(
    mappings: Mapping[str, object], profiles: Sequence[Profile]
) -> None:
    profiles_by_id = {profile_id(profile): profile for profile in profiles}
    raw_bindings = mappings.get("account_bindings", [])
    if not isinstance(raw_bindings, list):
        return
    for raw_binding in raw_bindings:
        if not isinstance(raw_binding, dict):
            continue
        binding_id = str(raw_binding.get("id", ""))
        selected_profile_id = str(raw_binding.get("profile", ""))
        profile = profiles_by_id.get(selected_profile_id)
        if profile is None:
            raise ValueError(
                f"Account binding {binding_id} uses unknown profile {selected_profile_id}"
            )
        expected = static_profile_account_ids(profile)
        if expected is None:
            continue
        accounts = raw_binding.get("accounts", [])
        actual = {
            str(account.get("source_account_id", ""))
            for account in accounts
            if isinstance(account, Mapping)
        }
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unknown " + ", ".join(extra))
            raise ValueError(
                f"Account binding {binding_id} does not cover profile "
                f"{selected_profile_id}: {'; '.join(details)}"
            )


def matching_filename_mapping(
    source_path: Path, mappings: Mapping[str, object]
) -> Mapping[str, object] | None:
    raw_patterns = mappings.get("filename_patterns", [])
    patterns = raw_patterns if isinstance(raw_patterns, list) else []
    matches = [
        mapping
        for mapping in patterns
        if isinstance(mapping, Mapping)
        and fnmatch(source_path.name, str(mapping.get("pattern", "")))
    ]
    if not matches:
        return None
    selections = {
        (str(mapping.get("profile", "")), str(mapping.get("binding", "")))
        for mapping in matches
    }
    if len(selections) > 1:
        raise AccountBindingError(
            f"Conflicting filename mappings for {source_path.name}"
        )
    return matches[0]


def binding_for_source(
    source_path: Path,
    profile: Mapping[str, object],
    mappings: Mapping[str, object],
) -> AccountBinding | None:
    mapping = matching_filename_mapping(source_path, mappings)
    if mapping is None or not mapping.get("binding"):
        return None
    if str(mapping.get("profile", "")) != profile_id(profile):
        return None
    binding_id = str(mapping["binding"])
    for raw_binding in _mapping_list(mappings, "account_bindings"):
        if isinstance(raw_binding, dict) and raw_binding.get("id") == binding_id:
            return cast(AccountBinding, raw_binding)
    raise AccountBindingError(
        f"Unknown account binding in filename mapping: {binding_id}"
    )


def apply_binding(
    rows: Sequence[dict[str, str]], binding: AccountBinding | None
) -> None:
    if binding is None:
        return
    accounts = {
        account["source_account_id"]: account for account in binding["accounts"]
    }
    emitted_ids = {row.get("account_id", "") for row in rows}
    missing = sorted(emitted_ids - set(accounts))
    if missing:
        raise AccountBindingError(
            f"Account binding {binding['id']} does not cover "
            f"{len(missing)} emitted account id{'s' if len(missing) != 1 else ''}"
        )
    for row in rows:
        account = accounts[row.get("account_id", "")]
        row["account_id"] = account["account_id"]
        row["account"] = account["account"]
        row["owner"] = binding["owner"]
        row[_BOUND_OWNER_FIELD] = binding["owner"]


def canonical_bound_owners(
    source_rows: Sequence[dict[str, str]],
    groups: Sequence[Mapping[str, object]],
    mappings: Mapping[str, object],
) -> dict[str, str]:
    """Project source-selected binding owners onto canonical transaction IDs."""
    occurrence_owners: dict[str, str] = {}
    for row in source_rows:
        owner = row.pop(_BOUND_OWNER_FIELD, None) or _saved_binding_owner(row, mappings)
        transaction_id = row.get("transaction_id", "")
        if owner is not None and transaction_id:
            occurrence_owners[transaction_id] = owner

    updates: dict[str, str] = {}
    for group in groups:
        pools = group.get("source_occurrence_pools", [])
        canonical_ids = group.get("canonical_transaction_ids", [])
        if not isinstance(pools, list) or not isinstance(canonical_ids, list):
            continue
        owners = {
            occurrence_owners[identifier]
            for pool in pools
            if isinstance(pool, list)
            for raw_identifier in pool
            if (identifier := str(raw_identifier)) in occurrence_owners
        }
        if len(owners) > 1:
            raise AccountBindingError(
                "Conflicting account binding owners in one canonical group"
            )
        if owners:
            owner = next(iter(owners))
            updates.update({str(identifier): owner for identifier in canonical_ids})
    return updates


def _saved_binding_owner(
    row: Mapping[str, str], mappings: Mapping[str, object]
) -> str | None:
    source_file = row.get("source_file", "")
    if not source_file:
        return None
    mapping = matching_filename_mapping(Path(source_file), mappings)
    if mapping is None or not mapping.get("binding"):
        return None
    binding_id = str(mapping["binding"])
    account_identity = normalized_account_id(row.get("account_id", ""))
    for raw_binding in _mapping_list(mappings, "account_bindings"):
        if not isinstance(raw_binding, Mapping) or raw_binding.get("id") != binding_id:
            continue
        accounts = raw_binding.get("accounts", [])
        if not isinstance(accounts, list):
            return None
        target_identities = {
            normalized_account_id(raw_account.get("account_id", ""))
            for raw_account in accounts
            if isinstance(raw_account, Mapping)
        }
        owner = raw_binding.get("owner")
        return (
            owner
            if account_identity in target_identities and isinstance(owner, str)
            else None
        )
    return None


def enforce_bound_owners(
    rows: Sequence[dict[str, str]], owners: Mapping[str, str]
) -> None:
    """Restore projected binding owners after rules and corrections."""
    for row in rows:
        owner = owners.get(row.get("transaction_id", ""))
        if owner is not None:
            row["owner"] = owner


def normalized_account_id(value: object) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value)).split()).casefold()


def binding_views(mappings: Mapping[str, object]) -> list[dict[str, object]]:
    patterns_by_binding: dict[str, list[str]] = {}
    for mapping in _mapping_list(mappings, "filename_patterns"):
        if isinstance(mapping, Mapping) and mapping.get("binding"):
            patterns_by_binding.setdefault(str(mapping["binding"]), []).append(
                str(mapping.get("pattern", ""))
            )
    views: list[dict[str, object]] = []
    for binding in _mapping_list(mappings, "account_bindings"):
        if not isinstance(binding, Mapping):
            continue
        view = dict(binding)
        view["patterns"] = sorted(patterns_by_binding.get(str(binding.get("id")), []))
        views.append(view)
    return sorted(views, key=lambda item: str(item.get("id", "")))


def _mapping_list(mappings: Mapping[str, object], field: str) -> list[object]:
    value = mappings.get(field, [])
    return value if isinstance(value, list) else []


def _clear_pattern_edit_receipts(mappings: dict[str, object], binding_id: str) -> None:
    for field in ("removed_filename_patterns", "replaced_filename_patterns"):
        retained = [
            item
            for item in _mapping_list(mappings, field)
            if not isinstance(item, Mapping) or item.get("binding") != binding_id
        ]
        if retained:
            mappings[field] = retained
        else:
            mappings.pop(field, None)
