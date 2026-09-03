from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research import state as state_module
from scripts.knowledge_research.state import KnowledgeResearchStore, ResearchStateError

EVIDENCE = "ev4_11111111111111111111111111111111"
FILE = "fixture-file-001"


def _result() -> dict[str, Any]:
    return {
        "structuredContent": {
            "contractVersion": "knowledge-vnext/2",
            "chunkPolicyId": "hierarchical_token_v4",
            "indexVersion": "knowledge-index-v5",
            "effectiveProfile": "hybrid_rrf_bge_m3_fts5",
            "retrievalProfile": "hybrid_rrf_bge_m3_fts5",
            "selectionStrategy": "hierarchical_interleave",
            "scopeEnforced": True,
            "warnings": [],
            "lexicalCandidateCount": 1,
            "vectorCandidateCount": 1,
            "count": 1,
            "results": [
                {
                    "evidenceId": EVIDENCE,
                    "fileId": FILE,
                    "documentId": "fixture-document-001",
                    "chunkId": "fixture-chunk-001",
                    "revision": "a" * 64,
                    "content": "Conditional earnings forecast.",
                    "title": "Fixture Research",
                    "locator": {"pageStart": 1, "pageEnd": 1},
                }
            ],
        },
        "isError": False,
    }


def _store(path: Path) -> KnowledgeResearchStore:
    return KnowledgeResearchStore(workspace=path, pdf_renderer=lambda *_: b"%PDF-fixture")


def _seed(store: KnowledgeResearchStore) -> str:
    research_id = store.begin(title="Fixture Research")["researchId"]
    store.record_knowledge_call(
        research_id=research_id,
        tool_name="search",
        arguments={"query": "earnings"},
        result=_result(),
    )
    return str(research_id)


def _claim(
    key: str | None = "outlook-01", text: str = "The forecast is conditional."
) -> dict[str, Any]:
    value: dict[str, Any] = {"section": "Outlook", "text": text, "evidenceIds": [EVIDENCE]}
    if key is not None:
        value["claimKey"] = key
    return value


def _artifact_bytes(store: KnowledgeResearchStore, receipt: dict[str, Any]) -> dict[str, bytes]:
    return {
        item["path"]: (store.workspace / item["path"]).read_bytes()
        for item in receipt["publicArtifactManifest"]["files"]
    }


def _assert_bytes_unchanged(store: KnowledgeResearchStore, expected: dict[str, bytes]) -> None:
    for path, payload in expected.items():
        assert (store.workspace / path).read_bytes() == payload


def test_batch_replay_conflict_and_cross_batch_key_deduplication(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    original = store.add_claims(research_id=rid, claims=[_claim()], batch_key="batch-01")
    before = store.snapshot(rid)
    replay = _store(tmp_path).add_claims(research_id=rid, claims=[_claim()], batch_key="batch-01")
    assert replay == original
    assert store.snapshot(rid) == before
    assert "batch-01" in before["claimBatches"]
    assert before["report"]["items"][0]["claimKey"] == "outlook-01"

    with pytest.raises(ResearchStateError) as error:
        store.add_claims(
            research_id=rid, claims=[_claim(text="Changed forecast.")], batch_key="batch-01"
        )
    assert error.value.details["code"] == "BATCH_KEY_CONFLICT"
    assert store.snapshot(rid) == before
    dedup = store.add_claims(research_id=rid, claims=[_claim()], batch_key="batch-02")
    assert dedup["insertedCount"] == 0
    assert dedup["unchangedCount"] == 1
    assert len(store.snapshot(rid)["report"]["items"]) == 1


def test_local_repair_preserves_order_and_old_replay_never_reverts_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    claims = [_claim(), _claim("risk-01", "Risk remains uncertain.")]
    first = store.add_claims(research_id=rid, claims=claims, batch_key="initial")
    repaired = _claim(text="The forecast depends on the cited assumptions.")
    with pytest.raises(ResearchStateError) as error:
        store.add_claims(research_id=rid, claims=[repaired], batch_key="repair")
    assert error.value.details["code"] == "CLAIM_KEY_CONFLICT"
    repaired["expectedClaimHash"] = first["claimHashes"]["outlook-01"]
    result = store.add_claims(research_id=rid, claims=[repaired], batch_key="repair")
    assert result["updatedCount"] == 1
    after = store.snapshot(rid)
    assert [item["itemId"] for item in after["report"]["items"]] == first["items"]
    assert after["report"]["items"][1]["text"] == claims[1]["text"]
    assert store.add_claims(research_id=rid, claims=claims, batch_key="initial") == first
    assert store.snapshot(rid) == after
    stale = {**repaired, "text": "Another forecast."}
    with pytest.raises(ResearchStateError) as error:
        store.add_claims(research_id=rid, claims=[stale], batch_key="stale")
    assert error.value.details["code"] == "CLAIM_HASH_CONFLICT"
    assert store.snapshot(rid) == after


def test_failure_has_exact_location_and_repaired_batch_can_reuse_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    before = store.snapshot(rid)
    claims = [_claim(f"p-{index}") for index in range(4)]
    claims[3]["evidenceIds"] = [EVIDENCE, EVIDENCE, "mistyped-evidence"]
    with pytest.raises(ResearchStateError) as error:
        store.add_claims(research_id=rid, claims=claims, batch_key="retryable")
    issue = error.value.details["issues"][0]
    assert issue["path"] == "/claims/3/evidenceIds/2"
    assert issue["claimKey"] == "p-3"
    assert issue["code"] == "UNVERIFIED_EVIDENCE_ID"
    assert error.value.details["committed"] is False
    assert store.snapshot(rid) == before
    claims[3]["evidenceIds"] = [EVIDENCE]
    assert (
        store.add_claims(research_id=rid, claims=claims, batch_key="retryable")["claimCount"] == 4
    )


@pytest.mark.parametrize("claims", [[], [_claim()] * 41, [_claim(), _claim()]])
def test_invalid_batches_never_commit(tmp_path: Path, claims: list[dict[str, Any]]) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    before = store.snapshot(rid)
    with pytest.raises(ResearchStateError):
        store.add_claims(research_id=rid, claims=claims, batch_key="bad")
    assert store.snapshot(rid) == before


def test_legacy_append_and_optional_single_claim_keys(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    legacy = _claim(None)
    assert store.add_claims(research_id=rid, claims=[legacy])["claimCount"] == 1
    assert store.add_claims(research_id=rid, claims=[legacy])["claimCount"] == 1
    kwargs: dict[str, Any] = dict(
        research_id=rid, section="Outlook", text="A cited forecast.", evidence_ids=[EVIDENCE]
    )
    first = store.add_claim(**kwargs, claim_key="single", batch_key="single-batch")
    assert store.add_claim(**kwargs, claim_key="single", batch_key="single-batch") == first
    assert first["item"] == first["items"][0]
    assert len(store.snapshot(rid)["report"]["items"]) == 3


@pytest.mark.parametrize("kind", ["call", "error"])
def test_on_commit_is_in_the_same_transaction(tmp_path: Path, kind: str) -> None:
    store = _store(tmp_path)
    rid = store.begin(title="Callback fixture")["researchId"]
    before = store.snapshot(rid)

    def callback(state: dict[str, Any], call: dict[str, Any]) -> None:
        state["navigation"] = {"sequence": call["sequence"]}
        call["navigationRegistered"] = True

    def failing(state: dict[str, Any], call: dict[str, Any]) -> None:
        callback(state, call)
        raise ResearchStateError("callback rejected")

    def invoke(on_commit: Any) -> dict[str, Any]:
        kwargs = dict(
            research_id=rid,
            tool_name="search",
            arguments={"query": "forecast"},
            on_commit=on_commit,
        )
        if kind == "call":
            return store.record_knowledge_call(**kwargs, result=_result())
        return store.record_knowledge_error(**kwargs, error={"code": -1})

    with pytest.raises(ResearchStateError, match="callback rejected"):
        invoke(failing)
    assert store.snapshot(rid) == before
    result = invoke(callback)
    snapshot = store.snapshot(rid)
    assert snapshot["navigation"]["sequence"] == result["sequence"] == 1
    assert snapshot["ledger"]["calls"] == [result]
    assert result["navigationRegistered"] is True
    assert snapshot["stateRevision"] == before["stateRevision"] + 1


def test_snapshot_detached_and_atomic_update_rolls_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    before = store.snapshot(rid)
    detached = store.snapshot(rid)
    detached["ledger"]["evidence"].clear()
    assert store.snapshot(rid) == before

    def fail(state: dict[str, Any]) -> None:
        state["ledger"]["calls"].clear()
        raise RuntimeError("rollback")

    with pytest.raises(RuntimeError):
        store.atomic_update(rid, fail)
    assert store.snapshot(rid) == before
    assert store.atomic_update(rid, lambda _: "no-op") == "no-op"
    assert store.snapshot(rid) == before


def test_state_save_failure_and_lost_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    before = store.snapshot(rid)
    save = store._save

    def fail_before(_: Any) -> None:
        raise OSError("before replace")

    monkeypatch.setattr(store, "_save", fail_before)
    with pytest.raises(OSError):
        store.add_claims(research_id=rid, claims=[_claim()], batch_key="lost-reply")
    assert store.snapshot(rid) == before

    def fail_after(state: Any) -> None:
        save(state)
        raise OSError("reply lost after replace")

    monkeypatch.setattr(store, "_save", fail_after)
    with pytest.raises(OSError):
        store.add_claims(research_id=rid, claims=[_claim()], batch_key="lost-reply")
    committed = store.snapshot(rid)
    resumed = _store(tmp_path)
    result = resumed.add_claims(research_id=rid, claims=[_claim()], batch_key="lost-reply")
    assert result == committed["claimBatches"]["lost-reply"]["receipt"]
    assert resumed.snapshot(rid) == committed
    assert len(committed["report"]["items"]) == 1


def test_unicode_and_reserved_aliases_without_short_string_false_positives(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    safe = _claim(text="\u4f30\u503c D1/E1 \u4ec5\u4e3a\u60c5\u666f\u5047\u8bbe\u3002")
    result = store.add_claims(research_id=rid, claims=[safe], batch_key="unicode")
    assert result["claimHashes"]["outlook-01"]
    for field in ("text", "section"):
        bad = {**_claim("leak"), field: "\u53c2\u89c1kref_a91c70e83d42_e12\u8868"}
        with pytest.raises(ResearchStateError) as error:
            store.add_claims(research_id=rid, claims=[bad])
        assert error.value.details["issues"][0]["path"] == f"/claims/0/{field}"
    other = _seed(store)
    store.atomic_update(other, lambda state: state["ledger"]["evidence"].clear())
    with pytest.raises(ResearchStateError):
        store.add_claims(research_id=other, claims=[safe])


def test_finalize_optional_checklist_replay_and_immutable_generations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim()], batch_key="first")
    before = store.snapshot(rid)
    with pytest.raises(ResearchStateError) as error:
        store.finalize(research_id=rid, expected_claim_keys=["not-submitted"])
    assert error.value.details["code"] == "EXPECTED_ITEMS_MISMATCH"
    assert store.snapshot(rid) == before
    with pytest.raises(ResearchStateError):
        store.finalize(research_id=rid, expected_table_ids=["not-selected"])
    first = store.finalize(
        research_id=rid, expected_claim_keys=["outlook-01"], expected_table_ids=[]
    )
    payloads = _artifact_bytes(store, first)
    assert store.finalize(research_id=rid) == first
    finalized = store.snapshot(rid)
    with pytest.raises(ResearchStateError):
        store.add_claims(research_id=rid, claims=[_claim(text="Conflicting change.")])
    assert store.snapshot(rid) == finalized
    _assert_bytes_unchanged(store, payloads)
    store.add_claims(research_id=rid, claims=[_claim("second", "Additional sourced paragraph.")])
    _assert_bytes_unchanged(store, payloads)
    second = store.finalize(research_id=rid)
    assert second["coverage"]["claimCount"] == 2
    assert set(_artifact_bytes(store, second)).isdisjoint(payloads)
    _assert_bytes_unchanged(store, payloads)
    assert len(store.snapshot(rid)["artifactHistory"]) == 2


@pytest.mark.parametrize("historical", [False, True])
def test_staging_write_failure_never_publishes_partial_or_removes_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: bool
) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim()])
    payloads = _artifact_bytes(store, store.finalize(research_id=rid)) if historical else {}
    if historical:
        store.add_claims(research_id=rid, claims=[_claim("second")])
    before = store.snapshot(rid)
    write = store._atomic_write

    def failing(path: Path, payload: bytes) -> None:
        if path.name == "report.pdf":
            raise OSError("injected second-file failure")
        write(path, payload)

    monkeypatch.setattr(store, "_atomic_write", failing)
    with pytest.raises(OSError, match="second-file"):
        store.finalize(research_id=rid)
    assert store.snapshot(rid) == before
    _assert_bytes_unchanged(store, payloads)
    assert len(list(store.output_root.iterdir())) == int(historical)


def test_post_promotion_save_failure_preserves_complete_orphan_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim()])
    before = store.snapshot(rid)

    def failing(_: Any) -> None:
        raise OSError("state save after promotion")

    monkeypatch.setattr(store, "_save", failing)
    with pytest.raises(OSError):
        store.finalize(research_id=rid)
    assert store.snapshot(rid) == before
    orphan = store.output_root / rid
    assert {path.name for path in orphan.iterdir()} == {
        "report.html",
        "report.pdf",
        "provenance.json",
    }
    originals = {
        str(path.relative_to(store.workspace)): path.read_bytes() for path in orphan.iterdir()
    }
    resumed = _store(tmp_path)
    result = resumed.finalize(research_id=rid)
    assert set(_artifact_bytes(resumed, result)).isdisjoint(originals)
    _assert_bytes_unchanged(resumed, originals)


def test_changed_finalized_artifact_is_not_silently_rewritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim()])
    receipt = store.finalize(research_id=rid)
    target = store.workspace / receipt["publicArtifactManifest"]["files"][0]["path"]
    target.write_bytes(b"changed fixture")
    with pytest.raises(ResearchStateError, match="missing or changed"):
        store.finalize(research_id=rid)
    assert target.read_bytes() == b"changed fixture"


def test_actual_navigation_namespaces_cannot_leak_into_human_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    namespace = base64.urlsafe_b64encode(bytes.fromhex(rid[3:])).decode().rstrip("=")
    suffixes = ["D1", "E1", "T1", "C1", "S" + "a" * 24, "N" + "b" * 24]
    for suffix in suffixes:
        prose = "\u53c2\u89c1" + namespace + ":" + suffix + "\u3002"
        with pytest.raises(ResearchStateError):
            store.add_claims(research_id=rid, claims=[_claim(text=prose)])
        with pytest.raises(ResearchStateError):
            store.begin(title=prose)
    assert store.add_claims(research_id=rid, claims=[_claim(text="D1 and E1 are row labels.")])


def test_dynamic_navigation_registry_rejects_refs_and_cursors_without_pattern_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    namespace = base64.urlsafe_b64encode(bytes.fromhex(rid[3:])).decode().rstrip("=")
    foreign_namespace = base64.urlsafe_b64encode(bytes.fromhex("12" * 16)).decode().rstrip("=")
    snapshot_ref = namespace + ":N" + "b" * 24
    cursor = snapshot_ref + ":120:" + "c" * 24
    refs = [namespace + ":" + suffix for suffix in ("D1", "E1", "T1", "S" + "a" * 24)]
    registered = foreign_namespace + ":reserved-ref"

    def register(state: dict[str, Any]) -> None:
        state["extensions"] = {
            "navigation": {
                "namespace": namespace,
                "files": {FILE: {"ref": refs[0]}},
                "evidence": {EVIDENCE: {"ref": refs[1]}},
                "tables": {"fixture-table": {"ref": refs[2]}},
                "scopes": {refs[3]: {"scopeRef": refs[3]}},
                "snapshots": {snapshot_ref: {"snapshotRef": snapshot_ref, "resumeCursor": cursor}},
                "projections": {"fixture": {"nextCursor": registered}},
            }
        }

    store.atomic_update(rid, register)
    monkeypatch.setattr(state_module, "_INTERNAL_ID_HINT", re.compile(r"(?!)"))
    for reference in [*refs, snapshot_ref, cursor, registered, namespace + ":future-kind"]:
        before = store.snapshot(rid)
        prose = "\u53c2\u89c1" + reference + "\u3002"
        for field in ("text", "section"):
            with pytest.raises(ResearchStateError, match="internal identifier"):
                store.add_claims(research_id=rid, claims=[{**_claim(), field: prose}])
        with pytest.raises(ResearchStateError, match="internal identifier"):
            store.add_table(research_id=rid, section="Tables", table_id="fixture", caption=prose)
        assert store.snapshot(rid) == before
    assert store.add_claims(research_id=rid, claims=[_claim(text="D1/E1/T1 are ordinary labels.")])


def test_late_hash_conflict_rolls_back_other_prepared_paragraphs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim()])
    before = store.snapshot(rid)
    with pytest.raises(ResearchStateError):
        store.add_claims(
            research_id=rid,
            batch_key="mixed",
            claims=[_claim("new"), {**_claim(text="Changed."), "expectedClaimHash": "0" * 64}],
        )
    assert store.snapshot(rid) == before


def test_legacy_manifest_survives_successful_append(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    legacy = {"manifestVersion": "opensquilla-public-artifact-manifest/1", "files": []}
    store.atomic_update(rid, lambda state: state.update(finalized=legacy))
    store.add_claims(research_id=rid, claims=[_claim()])
    snapshot = store.snapshot(rid)
    assert snapshot["artifactHistory"] == [legacy]
    assert snapshot["finalized"] is None


def _seed_legacy_finalized(
    store: KnowledgeResearchStore, *, archived: bool = False
) -> tuple[str, dict[str, Any], dict[str, bytes]]:
    rid = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim(None)])
    receipt = store.finalize(research_id=rid)
    # HEAD's finalized record has only these two fields, without a replay receipt.
    legacy = {
        "manifestVersion": state_module.PUBLIC_MANIFEST_VERSION,
        "files": receipt["publicArtifactManifest"]["files"],
    }

    def legacy_format(state: dict[str, Any]) -> None:
        state["finalized"] = legacy
        state["report"].pop("revision", None)
        state.pop("artifactHistory", None)
        if archived:
            state["artifactHistory"] = [legacy]

    store.atomic_update(rid, legacy_format)
    return rid, legacy, _artifact_bytes(store, receipt)


@pytest.mark.parametrize("archived", [False, True])
def test_legacy_direct_finalize_preserves_manifest_once_and_replays_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archived: bool
) -> None:
    store = _store(tmp_path)
    rid, legacy, old_files = _seed_legacy_finalized(store, archived=archived)
    report = store.snapshot(rid)["report"]
    receipt = store.finalize(research_id=rid)
    committed = store.snapshot(rid)
    assert committed["artifactHistory"] == [legacy, committed["finalized"]]
    assert committed["report"] == report
    assert set(_artifact_bytes(store, receipt)).isdisjoint(old_files)
    _assert_bytes_unchanged(store, old_files)

    resumed = _store(tmp_path)
    monkeypatch.setattr(resumed, "_save", lambda _: pytest.fail("replay must not save state"))
    monkeypatch.setattr(
        resumed, "pdf_renderer", lambda *_: pytest.fail("replay must not render again")
    )
    for _ in range(2):
        assert resumed.finalize(research_id=rid) == receipt
        assert resumed.snapshot(rid) == committed
    assert len(list(store.output_root.iterdir())) == 2
    _assert_bytes_unchanged(store, old_files)


@pytest.mark.parametrize("saved", [False, True])
def test_legacy_direct_finalize_save_failure_and_retry_keep_history_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: bool
) -> None:
    store = _store(tmp_path)
    rid, legacy, old_files = _seed_legacy_finalized(store)
    before = store.snapshot(rid)
    save = store._save

    def failing(state: dict[str, Any]) -> None:
        if saved:
            save(state)
        raise OSError("injected finalize state save failure")

    monkeypatch.setattr(store, "_save", failing)
    with pytest.raises(OSError, match="finalize state save"):
        store.finalize(research_id=rid)
    after_failure = store.snapshot(rid)
    if saved:
        assert after_failure["artifactHistory"] == [legacy, after_failure["finalized"]]
    else:
        assert after_failure == before
    retained = {
        str(path.relative_to(store.workspace)): path.read_bytes()
        for path in store.output_root.glob("*/*")
        if path.is_file()
    }
    assert len(retained) == 6
    _assert_bytes_unchanged(store, old_files)

    resumed = _store(tmp_path)
    if saved:
        monkeypatch.setattr(resumed, "_save", lambda _: pytest.fail("lost reply must replay"))
        monkeypatch.setattr(
            resumed, "pdf_renderer", lambda *_: pytest.fail("lost reply must not rerender")
        )
    receipt = resumed.finalize(research_id=rid)
    committed = resumed.snapshot(rid)
    assert committed["artifactHistory"] == [legacy, committed["finalized"]]
    assert committed["report"] == before["report"]
    assert resumed.finalize(research_id=rid) == receipt
    assert resumed.snapshot(rid) == committed
    assert len(list(store.output_root.iterdir())) == (2 if saved else 3)
    _assert_bytes_unchanged(resumed, retained)


def _seed_table(store: KnowledgeResearchStore, rid: str, *, truncated: bool = False) -> str:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    table_id = "t1_" + "2" * 32
    payload = {
        "schemaVersion": "knowledge-table-artifact/2",
        "tableId": table_id,
        "fileId": FILE,
        "revision": "a" * 64,
        "text": {
            "format": "html",
            "content": "<table></table>",
            "sha256": hashlib.sha256(b"<table></table>").hexdigest(),
        },
        "screenshot": {"mediaType": "image/png", "sha256": hashlib.sha256(png).hexdigest()},
        "textTruncated": truncated,
        "textPreviewTruncatedForTransport": truncated,
        "tableTextProjection": "preview" if truncated else "full",
    }
    call = store.record_knowledge_call(
        research_id=rid,
        tool_name="getTable",
        arguments={"fileId": FILE, "tableId": table_id},
        result={
            "structuredContent": payload,
            "content": [
                {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}
            ],
        },
    )
    assert call["verificationStatus"] == "verified"
    inventory = store.record_knowledge_call(
        research_id=rid,
        tool_name="getFileDetails",
        arguments={"fileId": FILE},
        result={
            "structuredContent": {
                "contractVersion": "knowledge-vnext/2",
                "file": {
                    "fileId": FILE,
                    "documentId": "fixture-document-001",
                    "revision": "a" * 64,
                    "title": "Fixture Research",
                    "filename": "fixture.pdf",
                    "mediaType": "application/pdf",
                },
                "tables": [{"tableId": table_id, "fileId": FILE}],
                "tableExtraction": {"tableCount": 1},
                "nextCursor": None,
            }
        },
    )
    assert inventory["verificationStatus"] == "verified"
    return table_id


def test_table_source_truncation_survives_ledger_storage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    table_id = _seed_table(store, rid, truncated=True)
    table = store.snapshot(rid)["ledger"]["tables"][table_id]
    assert table["textTruncated"] is True
    assert table["textPreviewTruncatedForTransport"] is True
    assert table["tableTextProjection"] == "preview"


def test_table_replay_preserves_original_item_and_finalized_artifacts(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    table_id = _seed_table(store, rid)
    store.add_claims(research_id=rid, claims=[_claim()])
    arguments = dict(
        research_id=rid, section="Tables", table_id=table_id, caption="Extracted source table"
    )
    first = store.add_table(**arguments)
    finalized = store.finalize(research_id=rid)
    artifacts = _artifact_bytes(store, finalized)
    before = store.snapshot(rid)
    assert _store(tmp_path).add_table(**arguments) == first
    assert store.snapshot(rid) == before
    _assert_bytes_unchanged(store, artifacts)
    assert store.finalize(research_id=rid) == finalized

    store.add_claims(research_id=rid, claims=[_claim("later")])
    after_append = store.snapshot(rid)
    assert store.add_table(**arguments) == first
    assert store.snapshot(rid) == after_append
    tables = [item for item in after_append["report"]["items"] if item["kind"] == "table"]
    assert len(tables) == 1
    assert tables[0]["itemId"] == first["item"]


@pytest.mark.parametrize("field", ["section", "caption"])
def test_table_content_conflict_preserves_report_and_artifacts(tmp_path: Path, field: str) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    table_id = _seed_table(store, rid)
    store.add_claims(research_id=rid, claims=[_claim()])
    arguments = dict(
        research_id=rid, section="Tables", table_id=table_id, caption="Extracted source table"
    )
    first = store.add_table(**arguments)
    artifacts = _artifact_bytes(store, store.finalize(research_id=rid))
    before = store.snapshot(rid)
    with pytest.raises(ResearchStateError) as error:
        store.add_table(**{**arguments, field: "Changed content"})
    assert error.value.details["code"] == "TABLE_ITEM_CONFLICT"
    assert error.value.details["tableId"] == table_id
    assert error.value.details["issues"][0]["path"] == f"/{field}"
    assert error.value.details["issues"][0]["itemId"] == first["item"]
    assert store.snapshot(rid) == before
    _assert_bytes_unchanged(store, artifacts)


def test_table_unknown_commit_status_replays_without_another_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    table_id = _seed_table(store, rid)
    arguments = dict(
        research_id=rid, section="Tables", table_id=table_id, caption="Extracted source table"
    )
    save = store._save

    def lost_reply(state: Any) -> None:
        save(state)
        raise TimeoutError("injected timeout after table commit")

    monkeypatch.setattr(store, "_save", lost_reply)
    with pytest.raises(TimeoutError):
        store.add_table(**arguments)
    committed = store.snapshot(rid)
    resumed = _store(tmp_path)
    monkeypatch.setattr(
        resumed, "_save", lambda _: pytest.fail("identical table retry must not write")
    )
    receipt = resumed.add_table(**arguments)
    assert receipt == {
        "status": "accepted",
        "item": committed["report"]["items"][0]["itemId"],
        "tableCount": 1,
    }
    assert resumed.snapshot(rid) == committed


def _table_worker(path: str, rid: str, table_id: str, barrier: Any) -> None:
    barrier.wait(10)
    _store(Path(path)).add_table(
        research_id=rid,
        section="Tables",
        table_id=table_id,
        caption="Extracted source table",
    )


def test_cross_process_table_retries_have_one_committed_item(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    table_id = _seed_table(store, rid)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    processes = [
        ctx.Process(target=_table_worker, args=(str(tmp_path), rid, table_id, barrier))
        for _ in range(3)
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(10)
        _join(processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    snapshot = store.snapshot(rid)
    assert len(snapshot["report"]["items"]) == 1
    assert snapshot["report"]["revision"] == 1


def _append_worker(path: str, rid: str, key: str, start: Any, done: Any) -> None:
    start.set()
    _store(Path(path)).add_claims(research_id=rid, claims=[_claim(key)], batch_key=key)
    done.set()


def _snapshot_worker(path: str, rid: str, done: Any) -> None:
    _store(Path(path)).snapshot(rid)
    done.set()


def _finalize_worker(path: str, rid: str, entered: Any, release: Any) -> None:
    def renderer(*_: Any) -> bytes:
        entered.set()
        if not release.wait(15):
            raise RuntimeError("test renderer release timed out")
        return b"%PDF-process-fixture"

    KnowledgeResearchStore(workspace=path, pdf_renderer=renderer).finalize(research_id=rid)


def _join(processes: list[Any]) -> None:
    for process in processes:
        process.join(15)
        assert process.exitcode == 0


def test_cross_process_claims_no_lost_writes_or_duplicate_replay(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    ctx = multiprocessing.get_context("spawn")
    signals = [(ctx.Event(), ctx.Event()) for _ in range(6)]
    processes = [
        ctx.Process(target=_append_worker, args=(str(tmp_path), rid, key, *pair))
        for key, pair in zip(["same", "same", "p-1", "p-2", "p-3", "p-4"], signals, strict=True)
    ]
    try:
        for process in processes:
            process.start()
        _join(processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    snapshot = store.snapshot(rid)
    assert len(snapshot["report"]["items"]) == 5
    assert len(snapshot["claimBatches"]) == 5
    assert len({item["itemId"] for item in snapshot["report"]["items"]}) == 5


def test_finalize_locks_only_its_research_and_snapshot_waits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rid = _seed(store)
    other = _seed(store)
    store.add_claims(research_id=rid, claims=[_claim()])
    ctx = multiprocessing.get_context("spawn")
    entered, release, started, appended, read_done, other_started, other_done = [
        ctx.Event() for _ in range(7)
    ]
    finalizer = ctx.Process(target=_finalize_worker, args=(str(tmp_path), rid, entered, release))
    writer = ctx.Process(
        target=_append_worker, args=(str(tmp_path), rid, "later", started, appended)
    )
    reader = ctx.Process(target=_snapshot_worker, args=(str(tmp_path), rid, read_done))
    other_writer = ctx.Process(
        target=_append_worker, args=(str(tmp_path), other, "other", other_started, other_done)
    )
    processes = [finalizer, writer, reader, other_writer]
    try:
        finalizer.start()
        assert entered.wait(10)
        writer.start()
        reader.start()
        other_writer.start()
        assert started.wait(10)
        assert other_done.wait(10), "one research's rendering must not block another research"
        assert not appended.wait(0.15)
        assert not read_done.wait(0.15)
        release.set()
        _join(processes)
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
    snapshot = store.snapshot(rid)
    assert len(snapshot["report"]["items"]) == 2
    first = snapshot["artifactHistory"][0]["receipt"]
    assert first["coverage"]["claimCount"] == 1
    provenance_file = next(
        item
        for item in first["publicArtifactManifest"]["files"]
        if item["name"] == "provenance.json"
    )
    provenance = json.loads((store.workspace / provenance_file["path"]).read_text())
    assert len(provenance["report"]["items"]) == 1
    for item in first["publicArtifactManifest"]["files"]:
        assert (
            hashlib.sha256((store.workspace / item["path"]).read_bytes()).hexdigest()
            == item["sha256"]
        )
