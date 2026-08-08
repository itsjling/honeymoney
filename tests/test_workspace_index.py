from __future__ import annotations

import copy
import stat
import tempfile
import unittest
from pathlib import Path

from honeymoney.identity import (
    AllocationLocator,
    AllocationOrigin,
    empty_manifest,
    extractor_contract_id,
    ownership_record,
    record_fingerprint,
    source_id,
    source_namespace_id,
    source_ownership,
    source_revision,
)
from honeymoney.overlap import canonicalize_overlaps, empty_overlap_manifest
from honeymoney.workspace_index import (
    WORKSPACE_INDEX_SCHEMA_VERSION,
    WorkspaceContracts,
    WorkspaceIndex,
    WorkspaceIndexError,
    empty_workspace_index,
    load_workspace_index,
    parse_workspace_index,
    workspace_index_document,
    write_workspace_index,
)


def _contracts() -> WorkspaceContracts:
    return {
        "honeymoney_version": "0.2.0",
        "import_record_schema_version": 1,
        "attempt_schema_version": 1,
        "transaction_schema_version": 1,
        "derivation_contract": "d" * 64,
    }


def _populated_index() -> tuple[WorkspaceIndex, dict[str, object]]:
    namespace = source_namespace_id("workspace", "synthetic.csv")
    source = source_id(namespace)
    revision = source_revision(b"synthetic statement\n")
    contract = extractor_contract_id(
        1,
        {
            "id": "synthetic",
            "account_id": "checking",
            "csv": {"columns": {"date": "Date"}},
        },
    )
    facts = {
        "account_id": "checking",
        "date": "2026-08-08",
        "transaction_date": "2026-08-08",
        "posting_date": "",
        "original_amount": "-12",
        "original_currency": "HKD",
        "posted_amount": "-12",
        "posted_currency": "HKD",
        "merchant": "Synthetic Shop",
        "original_description": "Synthetic Shop",
    }
    fingerprint = record_fingerprint(facts)
    origin = AllocationOrigin(revision, contract, AllocationLocator(1, (2,)), 1)
    record = ownership_record(
        source_id_value=source,
        fingerprint=fingerprint,
        origin=origin,
    )
    identity_manifest = {
        "schema_version": 1,
        "sources": [
            source_ownership(
                source_id_value=source,
                namespace_id=namespace,
                revision=revision,
                contract_id=contract,
                records=[record],
            )
        ],
    }
    occurrence = {
        "transaction_id": record["transaction_id"],
        "source_id": source,
        "source_namespace_id": namespace,
        "source_revision": revision,
        "source_record_id": record["source_record_id"],
        **facts,
    }
    index = empty_workspace_index(
        "gen_" + "a" * 64,
        _contracts(),
        overlap_namespace="ovns_" + "c" * 64,
    )
    index["identity_manifest"] = identity_manifest
    index["overlap_manifest"] = canonicalize_overlaps(
        [occurrence],
        [],
        empty_overlap_manifest("ovns_" + "c" * 64),
    ).manifest
    return index, {
        "contract": contract,
        "facts": facts,
        "fingerprint": fingerprint,
        "namespace": namespace,
        "origin": origin,
        "record": record,
        "revision": revision,
        "source": source,
    }


class WorkspaceIndexTest(unittest.TestCase):
    def test_empty_index_has_canonical_private_round_trip(self) -> None:
        index = empty_workspace_index(
            "gen_" + "a" * 64,
            _contracts(),
            overlap_namespace="ovns_" + "b" * 64,
        )
        self.assertEqual(index["identity_manifest"], empty_manifest())
        self.assertEqual(
            index["overlap_manifest"],
            empty_overlap_manifest("ovns_" + "b" * 64),
        )
        document = workspace_index_document(index)
        self.assertTrue(document.endswith("\n"))
        self.assertEqual(parse_workspace_index(document), index)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".honeymoney" / "workspace-index.json"
            write_workspace_index(path, index)
            self.assertEqual(load_workspace_index(path), index)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_existing_identity_and_overlap_contracts_fit_without_rekeying(self) -> None:
        index, _ = _populated_index()
        self.assertEqual(parse_workspace_index(workspace_index_document(index)), index)

    def test_overlap_support_must_match_active_identity_records(self) -> None:
        index, context = _populated_index()
        source = str(context["source"])
        revision = str(context["revision"])
        contract = str(context["contract"])
        fingerprint = str(context["fingerprint"])
        origin = context["origin"]
        assert isinstance(origin, AllocationOrigin)

        wrong_source = copy.deepcopy(index)
        other_namespace = source_namespace_id("workspace", "other.csv")
        other_source = source_id(other_namespace)
        other_record = ownership_record(
            source_id_value=other_source,
            fingerprint=fingerprint,
            origin=AllocationOrigin(revision, contract, AllocationLocator(1, (3,)), 1),
        )
        wrong_source["identity_manifest"]["sources"].append(
            source_ownership(
                source_id_value=other_source,
                namespace_id=other_namespace,
                revision=revision,
                contract_id=contract,
                records=[other_record],
            )
        )
        wrong_source["identity_manifest"]["sources"].sort(
            key=lambda item: item["source_id"]
        )
        wrong_source_group = wrong_source["overlap_manifest"]["groups"][0]
        wrong_source_group["support_pools"][0]["source_id"] = other_source
        wrong_source_group["slots"][0]["supporting_source_ids"] = [other_source]

        retired = copy.deepcopy(index)
        retired_record = ownership_record(
            source_id_value=source,
            fingerprint=fingerprint,
            origin=AllocationOrigin(revision, contract, AllocationLocator(1, (3,)), 1),
            state="retired",
        )
        retired["identity_manifest"]["sources"][0]["records"].append(retired_record)
        retired["identity_manifest"]["sources"][0]["records"].sort(
            key=lambda item: item["source_record_id"]
        )
        retired["overlap_manifest"]["groups"][0]["support_pools"][0][
            "source_record_ids"
        ] = [retired_record["source_record_id"]]

        omitted = copy.deepcopy(index)
        extra_active = ownership_record(
            source_id_value=source,
            fingerprint="fp_" + "d" * 64,
            origin=AllocationOrigin(revision, contract, AllocationLocator(1, (4,)), 1),
        )
        omitted["identity_manifest"]["sources"][0]["records"].append(extra_active)
        omitted["identity_manifest"]["sources"][0]["records"].sort(
            key=lambda item: item["source_record_id"]
        )

        extra = copy.deepcopy(index)
        extra["overlap_manifest"]["groups"][0]["support_pools"][0][
            "source_record_ids"
        ] = ["rec_" + "f" * 64]

        for invalid in (wrong_source, retired, omitted, extra):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"),
            ):
                workspace_index_document(invalid)

    def test_unknown_fields_bad_ids_and_financial_fields_fail_closed(self) -> None:
        index = empty_workspace_index(
            "gen_" + "a" * 64,
            _contracts(),
            overlap_namespace="ovns_" + "b" * 64,
        )
        invalid = dict(index)
        invalid["transaction_date"] = "2026-08-08"
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(invalid)
        invalid = dict(index, generation_id="bad")
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(invalid)

    def test_noncanonical_and_newer_schema_have_stable_errors(self) -> None:
        index = empty_workspace_index(
            "gen_" + "a" * 64,
            _contracts(),
            overlap_namespace="ovns_" + "b" * 64,
        )
        with self.assertRaisesRegex(
            WorkspaceIndexError, "workspace_index_noncanonical"
        ):
            parse_workspace_index(workspace_index_document(index).replace(":", ": ", 1))
        index["schema_version"] = WORKSPACE_INDEX_SCHEMA_VERSION + 1
        with self.assertRaisesRegex(
            WorkspaceIndexError, "workspace_index_schema_unsupported"
        ):
            workspace_index_document(index)

    def test_duplicate_registered_view_and_unsafe_input_name_are_invalid(self) -> None:
        proof = "b" * 64
        index = empty_workspace_index(
            "gen_" + "a" * 64,
            _contracts(),
            overlap_namespace="ovns_" + "c" * 64,
        )
        index["registered_views"] = [
            {"period": "undated", "content_proof": proof},
            {"period": "undated", "content_proof": proof},
        ]
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(index)
        index["registered_views"] = []
        index["input_proofs"] = [{"name": "../rules", "proof": proof}]
        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(index)

    def test_clean_start_rejects_a_supportless_overlap_schema(self) -> None:
        index = empty_workspace_index(
            "gen_" + "a" * 64,
            _contracts(),
            overlap_namespace="ovns_" + "c" * 64,
        )
        index["overlap_manifest"] = {  # type: ignore[typeddict-item]
            "schema_version": 2,
            "namespace_key": "ovns_" + "c" * 64,
            "groups": [],
        }

        with self.assertRaisesRegex(WorkspaceIndexError, "workspace_index_invalid"):
            workspace_index_document(index)


if __name__ == "__main__":
    unittest.main()
