from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from scripts.knowledge_research.bridge import KnowledgeResearchBridge
from scripts.knowledge_research.navigation import Navigation
from scripts.knowledge_research.review import report_review_hash, review_preparation
from scripts.knowledge_research.state import KnowledgeResearchStore
from tests.test_scripts.test_knowledge_research_navigation import (
    REVISION,
    Upstream,
    invoke,
    result,
    search_payload,
)


def details(file_id: str = "file-a") -> dict[str, Any]:
    return {
        "contractVersion": "knowledge-vnext/2",
        "file": {
            "fileId": file_id,
            "documentId": "document-" + file_id,
            "revision": REVISION,
            "title": "Source outlook",
            "filename": "\u7814\u7a76-outlook.pdf",
            "sourcePath": "research/outlook.pdf",
            "mediaType": "application/pdf",
            "institution": "Source Institute",
            "publicationDate": "2026-09-01",
        },
        "tables": [],
        "nextCursor": None,
        "tableExtraction": {"status": "ready", "tableCount": 0},
    }


def seed(
    tmp_path: Path,
    *,
    mode: str = "deep",
    files: tuple[str, ...] = ("file-a",),
    cited: tuple[str, ...] = ("file-a",),
) -> tuple[KnowledgeResearchBridge, KnowledgeResearchStore, Upstream, str]:
    store = KnowledgeResearchStore(workspace=tmp_path, pdf_renderer=lambda *_: b"%PDF-test")
    source = search_payload("q", list(files), scoped=True)
    upstream = Upstream([result(source)])
    bridge = KnowledgeResearchBridge(upstream, store)
    # Model an already stored draft: these tests exercise review-time metadata
    # recovery, including legacy drafts created before first-write preparation.
    begin, _ = invoke(bridge, "researchBegin", {"title": "Research", "mode": "standard"})
    rid = begin["researchId"]
    found, response = invoke(
        bridge, "searchByIds", {"researchId": rid, "query": "q", "fileIds": list(files)}
    )
    assert not response["result"]["isError"], found
    by_file = {row["fileId"]: row["evidenceId"] for row in source["results"]}
    store.add_claims(
        research_id=rid,
        claims=[
            {
                "claimKey": f"fact-{index}",
                "section": "Findings",
                "text": "A source-bound finding.",
                "evidenceIds": [by_file[file_id]],
            }
            for index, file_id in enumerate(cited)
        ],
    )
    if mode != "standard":
        store.atomic_update(rid, lambda state: state.update(mode=mode))
    return bridge, store, upstream, rid


@pytest.mark.parametrize("bad_title", ["\ufeff", "\u200b\u2060", "<p>&#xfeff;</p>"])
def test_invisible_details_title_preserves_readable_search_title(
    tmp_path: Path, bad_title: str
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    before = store.snapshot(rid)
    original = before["ledger"]["files"]["file-a"]["title"]
    evidence_before = copy.deepcopy(before["ledger"]["evidence"])
    payload = details()
    payload["file"]["title"] = bad_title
    upstream.responses.append(result(payload))
    _, response = invoke(bridge, "getFileDetails", {"researchId": rid, "fileId": "file-a"})
    assert not response["result"]["isError"]
    after = store.snapshot(rid)
    assert after["ledger"]["files"]["file-a"]["title"] == original
    assert after["ledger"]["files"]["file-a"]["filename"] == payload["file"]["filename"]
    assert after["ledger"]["evidence"] == evidence_before


def navigate(bridge: KnowledgeResearchBridge, rid: str, **arguments: Any) -> dict[str, Any]:
    payload, response = invoke(bridge, "researchNavigate", {"researchId": rid, **arguments})
    assert not response["result"]["isError"], payload
    return payload


def finish_review(bridge: KnowledgeResearchBridge, rid: str, page: dict[str, Any]) -> None:
    while page["nextCursor"]:
        page = navigate(bridge, rid, cursor=page["nextCursor"])


class PausedMetadataUpstream(Upstream):
    def __init__(self, file_id: str, responses: list[Any]) -> None:
        super().__init__(responses)
        self.file_id = file_id
        self.started = Event()
        self.resume = Event()

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if params and params.get("arguments", {}).get("fileId") == self.file_id:
            self.started.set()
            assert self.resume.wait(10), "Concurrent metadata fixture was not released"
        return super().request(method, params)


@pytest.mark.parametrize("failure", [OSError("offline"), {"result": {"isError": True}}])
def test_concurrent_metadata_merges_current_attempts_without_retrying_sibling(
    tmp_path: Path, failure: Any
) -> None:
    bridge, store, upstream, rid = seed(
        tmp_path, files=("file-a", "file-b"), cited=("file-a", "file-b")
    )
    slow_upstream = PausedMetadataUpstream("file-a", [failure, failure])
    slow_bridge = KnowledgeResearchBridge(slow_upstream, KnowledgeResearchStore(workspace=tmp_path))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(navigate, slow_bridge, rid, view="review", requestKey="slow")
        try:
            assert slow_upstream.started.wait(10)
            upstream.responses.extend([OSError("offline"), OSError("offline")])
            navigate(bridge, rid, view="review", requestKey="fast")
            current = store.snapshot(rid)["extensions"]["metadataPreparation"]
            sibling = current["attempts"]["file-b"]
        finally:
            slow_upstream.resume.set()
        pending.result(timeout=10)
    saved = store.snapshot(rid)["extensions"]["metadataPreparation"]
    assert saved["reportHash"] == current["reportHash"]
    assert saved["attempts"]["file-b"] == sibling
    assert len(slow_upstream.calls) == 1
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0]["status"] == "finalized"
    assert len(upstream.calls) == 3


@pytest.mark.parametrize("prepare_current", [False, True])
@pytest.mark.parametrize("failure", [OSError("offline"), {"result": {"isError": True}}])
def test_concurrent_old_version_failure_cannot_replace_or_populate_current_attempts(
    tmp_path: Path, prepare_current: bool, failure: Any
) -> None:
    bridge, store, upstream, rid = seed(
        tmp_path, files=("file-a", "file-b"), cited=("file-a", "file-b")
    )
    slow_upstream = PausedMetadataUpstream("file-b", [OSError("offline"), failure])
    slow_bridge = KnowledgeResearchBridge(slow_upstream, KnowledgeResearchStore(workspace=tmp_path))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(navigate, slow_bridge, rid, view="review", requestKey="slow")
        try:
            assert slow_upstream.started.wait(10)
            old = store.snapshot(rid)
            assert set(old["extensions"]["metadataPreparation"]["attempts"]) == {"file-a"}
            store.add_claim(
                research_id=rid,
                section="More",
                text="Another source-bound finding.",
                evidence_ids=old["report"]["items"][0]["evidenceIds"],
                claim_key="new-fact",
            )
            if prepare_current:
                upstream.responses.extend([OSError("offline"), OSError("offline")])
                navigate(bridge, rid, view="review", requestKey="fast")
            current = store.snapshot(rid)["extensions"]["metadataPreparation"]
        finally:
            slow_upstream.resume.set()
        pending.result(timeout=10)
    state = store.snapshot(rid)
    assert state["extensions"]["metadataPreparation"] == current
    if prepare_current:
        assert current["reportHash"] == report_review_hash(state)
        assert invoke(bridge, "researchFinalize", {"researchId": rid})[0]["status"] == "finalized"
    else:
        assert current["reportHash"] != report_review_hash(state)
        upstream.responses.extend([result(details("file-a")), result(details("file-b"))])
        fresh = navigate(bridge, rid, view="review")
        assert "warnings" not in fresh
        assert not store.missing_reference_metadata(rid)
    assert len(upstream.calls) == 3


def test_concurrent_success_carries_current_attempts_across_its_metadata_update(
    tmp_path: Path,
) -> None:
    bridge, store, upstream, rid = seed(
        tmp_path, files=("file-a", "file-b"), cited=("file-a", "file-b")
    )
    slow_upstream = PausedMetadataUpstream(
        "file-b", [OSError("offline"), result(details("file-b"))]
    )
    slow_bridge = KnowledgeResearchBridge(slow_upstream, KnowledgeResearchStore(workspace=tmp_path))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(navigate, slow_bridge, rid, view="review", requestKey="slow")
        try:
            assert slow_upstream.started.wait(10)
            old = store.snapshot(rid)
            store.add_claim(
                research_id=rid,
                section="More",
                text="Another source-bound finding.",
                evidence_ids=old["report"]["items"][0]["evidenceIds"],
                claim_key="new-fact",
            )
            upstream.responses.extend([OSError("offline"), OSError("offline")])
            navigate(bridge, rid, view="review", requestKey="fast")
            current = store.snapshot(rid)["extensions"]["metadataPreparation"]
        finally:
            slow_upstream.resume.set()
        pending.result(timeout=10)
    state = store.snapshot(rid)
    saved = state["extensions"]["metadataPreparation"]
    assert saved["reportHash"] == report_review_hash(state) != current["reportHash"]
    assert saved["attempts"]["file-a"] == current["attempts"]["file-a"]
    assert saved["attempts"]["file-b"]["verificationStatus"] == "verified"
    assert state["ledger"]["files"]["file-b"]["metadataSource"] == "getFileDetails"
    assert store.missing_reference_metadata(rid) == ["file-a"]
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0]["status"] == "finalized"
    assert len(upstream.calls) == 3 and len(slow_upstream.calls) == 2


def test_fresh_deep_prepares_cited_metadata_before_snapshot_outside_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, upstream, rid = seed(
        tmp_path, files=("file-a", "file-b"), cited=("file-a", "file-a")
    )
    metadata = details()
    metadata.update(nextCursor="private-inventory-page", tableExtraction={"tableCount": 5})
    upstream.responses.append(result(metadata))
    original_update, original_request = store.atomic_update, upstream.request
    in_update = False

    def guarded_update(research_id: str, mutator: Callable[[dict[str, Any]], Any]) -> Any:
        def guarded(state: dict[str, Any]) -> Any:
            nonlocal in_update
            assert not in_update
            in_update = True
            try:
                return mutator(state)
            finally:
                in_update = False

        return original_update(research_id, guarded)

    def request(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        assert not in_update, "Metadata network I/O must not hold atomic_update"
        snapshots = store.snapshot(rid)["extensions"]["navigation"]["snapshots"]
        assert not any(row["payload"].get("view") == "review" for row in snapshots.values())
        return original_request(method, params)

    monkeypatch.setattr(store, "atomic_update", guarded_update)
    monkeypatch.setattr(upstream, "request", request)
    page = navigate(bridge, rid, view="review", limit=1, requestKey="first-review")
    state = store.snapshot(rid)
    assert page["reportHash"] == report_review_hash(state)
    assert upstream.calls[-1] == {
        "name": "getFileDetails",
        "arguments": {"fileId": "file-a", "limit": 1},
    }
    assert len(upstream.calls) == 2
    assert state["ledger"]["files"]["file-b"]["metadataSource"] == "search"
    assert state["ledger"]["inventories"] == {}
    assert state["extensions"]["navigation"]["tables"] == {}
    snapshot = state["extensions"]["navigation"]["snapshots"][page["snapshotRef"]]
    evidence = next(row for row in snapshot["payload"]["entries"] if row["kind"] == "evidence")
    assert evidence["filename"] == metadata["file"]["filename"]
    assert evidence["title"] == "Source outlook"
    assert evidence["sourceFormat"] == "PDF"
    assert evidence["institution"] == "Source Institute"
    assert evidence["publicationDate"] == "2026-09-01"
    assert state["ledger"]["calls"][-1]["purpose"] == "bibliography_metadata"
    assert not review_preparation(state)["comparisonPrepared"]
    finish_review(bridge, rid, page)
    finished, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert finished["status"] == "finalized"
    assert "warnings" not in finished
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0] == finished
    assert len(upstream.calls) == 2


@pytest.mark.parametrize(
    "arguments,code",
    [
        ({"limit": 0}, "INVALID_LIMIT"),
        ({"limit": 21}, "INVALID_LIMIT"),
        ({"limit": True}, "INVALID_LIMIT"),
        ({"limit": "1"}, "INVALID_LIMIT"),
        ({"cursor": "altered"}, "INVALID_CURSOR"),
        ({"cursor": None}, "INVALID_ARGUMENT"),
        ({"snapshotRef": "unknown"}, "UNKNOWN_REFERENCE"),
        ({"snapshotRef": None}, "INVALID_ARGUMENT"),
        ({"view": "unknown"}, "INVALID_VIEW"),
        ({"view": None}, "INVALID_VIEW"),
        ({"requestKey": ""}, "INVALID_ARGUMENT"),
        ({"requestKey": "x" * 129}, "INVALID_ARGUMENT"),
    ],
)
def test_invalid_navigation_has_no_metadata_or_request_side_effects(
    tmp_path: Path, arguments: dict[str, Any], code: str
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    before = store.snapshot(rid)
    rejected, response = invoke(
        bridge,
        "researchNavigate",
        {"researchId": rid, "view": "review", "requestKey": "invalid", **arguments},
    )
    assert response["result"]["isError"]
    assert rejected["details"]["code"] == code
    assert store.snapshot(rid) == before
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("view", ["files", "scopes", "evidence", "progress", "report"])
def test_other_views_never_prepare_metadata(tmp_path: Path, view: str) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    before = store.snapshot(rid)["ledger"]
    navigate(bridge, rid, view=view)
    assert store.snapshot(rid)["ledger"] == before
    assert "metadataPreparation" not in store.snapshot(rid)["extensions"]
    assert len(upstream.calls) == 1


def test_snapshot_and_cursor_resume_without_metadata_and_reject_wrong_targets(
    tmp_path: Path,
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    ref = store.atomic_update(rid, lambda state: Navigation(state).directory("review"))
    before = store.snapshot(rid)["ledger"]
    page = navigate(bridge, rid, snapshotRef=ref, view="review", limit=1)
    navigate(bridge, rid, cursor=page["nextCursor"], view="review", limit=1)
    for target in ({"view": "report"}, {"snapshotRef": "other"}):
        rejected, response = invoke(
            bridge, "researchNavigate", {"researchId": rid, "cursor": page["nextCursor"], **target}
        )
        assert response["result"]["isError"]
        assert rejected["details"]["code"] == "REFERENCE_MISMATCH"
    assert store.snapshot(rid)["ledger"] == before
    assert "metadataPreparation" not in store.snapshot(rid)["extensions"]
    assert len(upstream.calls) == 1


@pytest.mark.parametrize(
    "failure,status",
    [
        (OSError("offline"), "upstream_rpc_error"),
        (RuntimeError("child closed"), "upstream_rpc_error"),
        ({"error": {"message": "offline"}}, "upstream_rpc_error"),
        ({"result": None}, "upstream_rpc_error"),
        ({"result": {"isError": True}}, "upstream_error"),
        ({"result": {"content": []}}, "unverified_missing_structured_content"),
        (result({**details(), "contractVersion": "wrong"}), "unverified_contract_version"),
        (
            result({**details(), "file": {**details()["file"], "revision": "b" * 64}}),
            "unverified_revision_mismatch",
        ),
        (result(details("another-file")), "unverified_identity_mismatch"),
        (result({**details(), "tables": "invalid"}), "unverified_invalid_payload"),
    ],
)
def test_failure_is_an_attempt_not_completion_and_not_immediately_retried(
    tmp_path: Path, failure: Any, status: str
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    upstream.responses.append(failure)
    files = store.snapshot(rid)["ledger"]["files"]
    page = navigate(bridge, rid, view="review", limit=1)
    state = store.snapshot(rid)
    assert state["ledger"]["files"] == files
    assert state["ledger"]["inventories"] == {}
    preparation = state["extensions"]["metadataPreparation"]
    assert preparation["reportHash"] == report_review_hash(state)
    attempt = preparation["attempts"]["file-a"]
    assert attempt["verificationStatus"] == status
    assert "not completed" in attempt["warning"]
    assert attempt["callSequence"] == state["ledger"]["calls"][-1]["sequence"]
    assert "incomplete" in page["warnings"][0]
    assert "getFileDetails" in page["warnings"][0]
    assert not review_preparation(state)["comparisonPrepared"]
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0]["status"] == "needs_review"
    finish_review(bridge, rid, page)
    store = KnowledgeResearchStore(workspace=tmp_path, pdf_renderer=lambda *_: b"%PDF-test")
    bridge = KnowledgeResearchBridge(upstream, store)
    again = navigate(bridge, rid, view="review")
    assert again["entries"] == []
    assert again["pendingItemCount"] == 0 and again["reusedItemCount"] == 1
    assert again["warnings"] == page["warnings"]
    finished, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert finished["status"] == "finalized"
    assert finished["warnings"] == page["warnings"]
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0] == finished
    assert len(upstream.calls) == 2
    assert store.snapshot(rid)["ledger"]["files"] == files


def test_mixed_results_share_final_data_version_and_retry_after_report_edit(tmp_path: Path) -> None:
    bridge, store, upstream, rid = seed(
        tmp_path, files=("file-a", "file-b"), cited=("file-a", "file-b")
    )
    upstream.responses.extend([OSError("offline"), result(details("file-b"))])
    page = navigate(bridge, rid, view="review")
    state = store.snapshot(rid)
    assert state["extensions"]["metadataPreparation"]["reportHash"] == report_review_hash(state)
    assert set(state["extensions"]["metadataPreparation"]["attempts"]) == {"file-a", "file-b"}
    assert review_preparation(state)["comparisonPrepared"]
    navigate(bridge, rid, view="progress")
    navigate(bridge, rid, view="review")
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0]["status"] == "finalized"
    assert len(upstream.calls) == 3
    item = state["report"]["items"][0]
    store.add_claim(
        research_id=rid,
        section="More",
        text="Another source-bound finding.",
        evidence_ids=item["evidenceIds"],
        claim_key="new-fact",
    )
    upstream.responses.append(result(details()))
    fresh = navigate(bridge, rid, view="review")
    assert fresh["reportHash"] != page["reportHash"]
    assert "warnings" not in fresh
    assert len(upstream.calls) == 4
    assert upstream.calls[-1]["arguments"]["fileId"] == "file-a"
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0]["status"] == "finalized"
    assert len(upstream.calls) == 4


def test_explicit_details_after_failed_preparation_updates_source_and_preserves_old_snapshot(
    tmp_path: Path,
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    upstream.responses.append(OSError("offline"))
    old = navigate(bridge, rid, view="review", limit=1)
    finish_review(bridge, rid, old)
    before = store.snapshot(rid)
    file_ref = before["extensions"]["navigation"]["files"]["file-a"]["ref"]
    upstream.responses.append(result(details()))
    payload, response = invoke(
        bridge, "getFileDetails", {"researchId": rid, "fileRef": file_ref, "requestKey": "retry"}
    )
    assert not response["result"]["isError"], payload
    state = store.snapshot(rid)
    assert state["ledger"]["files"]["file-a"]["metadataSource"] == "getFileDetails"
    assert state["ledger"]["inventories"]["file-a"]["complete"]
    assert not review_preparation(state)["comparisonPrepared"]
    frozen = navigate(bridge, rid, snapshotRef=old["snapshotRef"], view="review", limit=1)
    assert frozen["reportHash"] == old["reportHash"]
    assert frozen["warnings"] == old["warnings"]
    fresh = navigate(bridge, rid, view="review")
    assert fresh["reportHash"] != old["reportHash"]
    assert "warnings" not in fresh
    finished, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert finished["status"] == "finalized" and "warnings" not in finished
    assert len(upstream.calls) == 3


def test_replayed_request_and_conflict_do_not_prepare_metadata_after_data_changes(
    tmp_path: Path,
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    upstream.responses.append(OSError("offline"))
    arguments = {"view": "review", "requestKey": "once"}
    original = navigate(bridge, rid, **arguments)
    state = store.snapshot(rid)
    store.add_claim(
        research_id=rid,
        section="More",
        text="Another finding.",
        evidence_ids=state["report"]["items"][0]["evidenceIds"],
        claim_key="new-fact",
    )
    before = store.snapshot(rid)
    assert navigate(bridge, rid, **arguments) == original
    rejected, response = invoke(
        bridge, "researchNavigate", {"researchId": rid, **arguments, "limit": 1}
    )
    assert response["result"]["isError"]
    assert rejected["details"]["code"] == "REQUEST_KEY_CONFLICT"
    assert store.snapshot(rid) == before
    assert len(upstream.calls) == 2


def test_crash_after_preparation_keeps_attempt_and_request_in_doubt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    upstream.responses.append(OSError("offline"))

    def fail_projection(*_: Any) -> dict[str, Any]:
        raise RuntimeError("projection failed")

    arguments = {"researchId": rid, "view": "review", "requestKey": "crashed"}
    with monkeypatch.context() as patch:
        patch.setattr(bridge, "_project", fail_projection)
        assert "error" in invoke(bridge, "researchNavigate", arguments)[1]
    state = store.snapshot(rid)
    assert state["extensions"]["metadataPreparation"]["attempts"]["file-a"]["warning"]
    rejected, _ = invoke(bridge, "researchNavigate", arguments)
    assert rejected["details"]["code"] == "IN_DOUBT"
    assert store.snapshot(rid) == state
    recovered = navigate(bridge, rid, view="review", requestKey="recovered")
    assert recovered["warnings"]
    assert len(upstream.calls) == 2


@pytest.mark.parametrize("review_first", [False, True])
def test_standard_keeps_metadata_at_finalize(tmp_path: Path, review_first: bool) -> None:
    bridge, store, upstream, rid = seed(tmp_path, mode="standard")
    if review_first:
        navigate(bridge, rid, view="review")
    assert len(upstream.calls) == 1
    assert "metadataPreparation" not in store.snapshot(rid)["extensions"]
    upstream.responses.append(result(details()))
    finished, _ = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert finished["status"] == "finalized"
    assert len(upstream.calls) == 2
    assert invoke(bridge, "researchFinalize", {"researchId": rid})[0] == finished
    assert len(upstream.calls) == 2


def test_empty_deep_review_has_no_metadata_work(tmp_path: Path) -> None:
    store = KnowledgeResearchStore(workspace=tmp_path)
    upstream = Upstream()
    bridge = KnowledgeResearchBridge(upstream, store)
    begin, _ = invoke(bridge, "researchBegin", {"title": "Empty research", "mode": "deep"})
    page = navigate(bridge, begin["researchId"], view="review")
    assert page["entries"] == [] and "warnings" not in page
    assert not upstream.calls


def test_known_metadata_needs_no_automatic_fetch(tmp_path: Path) -> None:
    bridge, store, upstream, rid = seed(tmp_path)
    upstream.responses.append(result(details()))
    invoke(bridge, "getFileDetails", {"researchId": rid, "fileId": "file-a"})
    ledger = copy.deepcopy(store.snapshot(rid)["ledger"])
    page = navigate(bridge, rid, view="review")
    assert "warnings" not in page
    assert store.snapshot(rid)["ledger"] == ledger
    assert len(upstream.calls) == 2
