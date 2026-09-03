from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.knowledge_research.bridge import KnowledgeResearchBridge, _write_frame
from scripts.knowledge_research.claims import claim_hash
from scripts.knowledge_research.navigation import MAX_FRAME_BYTES
from scripts.knowledge_research.state import KnowledgeResearchStore

REVISION = "a" * 64
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def result(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "result": {
            "structuredContent": dict(payload),
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False,
        }
    }


def search_payload(
    query: str,
    file_ids: list[str],
    content: str = "Complete source passage.",
    *,
    scoped: bool = False,
) -> dict[str, Any]:
    return {
        "contractVersion": "knowledge-vnext/2",
        "chunkPolicyId": "hierarchical_token_v4",
        "indexVersion": "knowledge-index-v5",
        "query": query,
        "requestedProfile": None,
        "effectiveProfile": "hybrid_rrf_bge_m3_fts5",
        "retrievalProfile": "hybrid_rrf_bge_m3_fts5",
        "selectionSource": "service_default",
        "fallbackReason": None,
        "warnings": [],
        "scopeEnforced": True,
        "budgetExceeded": None,
        "selectionStrategy": "pure_score" if scoped else "hierarchical_interleave",
        "lexicalCandidateCount": len(file_ids),
        "vectorCandidateCount": len(file_ids),
        "results": [
            {
                "evidenceId": "ev4_" + hashlib.sha256(file_id.encode()).hexdigest()[:32],
                "fileId": file_id,
                "documentId": "document-" + file_id,
                "chunkId": "chunk-" + file_id,
                "revision": REVISION,
                "content": content,
                "title": "Research source",
                "locator": {"pageStart": 1, "pageEnd": 1},
            }
            for file_id in file_ids
        ],
        "count": len(file_ids),
    }


class Upstream:
    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if method == "tools/list":
            return {
                "result": {
                    "tools": [
                        {
                            "name": name,
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": required,
                                "properties": properties,
                            },
                        }
                        for name, required, properties in [
                            ("search", ["query"], {"query": {"type": "string"}}),
                            (
                                "searchByIds",
                                ["query", "fileIds"],
                                {"query": {"type": "string"}, "fileIds": {"type": "array"}},
                            ),
                            ("getFileDetails", ["fileId"], {"fileId": {"type": "string"}}),
                            (
                                "getTable",
                                ["fileId", "tableId"],
                                {"fileId": {"type": "string"}, "tableId": {"type": "string"}},
                            ),
                        ]
                    ]
                }
            }
        assert method == "tools/call" and params is not None
        self.calls.append(copy.deepcopy(dict(params)))
        assert self.responses, f"Unexpected upstream call: {params}"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return cast(dict[str, Any], response)

    def notify(self, *_: Any) -> None:
        pass

    def close(self) -> None:
        pass


def invoke(
    bridge: KnowledgeResearchBridge,
    name: str,
    arguments: Mapping[str, Any],
    *,
    request_id: Any = "request",
) -> tuple[dict[str, Any], dict[str, Any]]:
    response = bridge.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        }
    )
    assert response is not None
    stream = io.BytesIO()
    _write_frame(stream, response, False)
    assert len(stream.getvalue()) <= MAX_FRAME_BYTES
    if "error" in response:
        return {"error": response["error"]}, response
    tool_result = response["result"]
    return json.loads(tool_result["content"][0]["text"]), response


def setup(
    tmp_path: Path, responses: list[Any] | None = None
) -> tuple[KnowledgeResearchBridge, KnowledgeResearchStore, Upstream, str]:
    store = KnowledgeResearchStore(
        workspace=tmp_path, media_root=tmp_path / "media", pdf_renderer=lambda *_: b"%PDF-test"
    )
    upstream = Upstream(responses)
    bridge = KnowledgeResearchBridge(upstream, store)
    begin, _ = invoke(bridge, "researchBegin", {"title": "Research"})
    return bridge, store, upstream, begin["researchId"]


def discovery(
    bridge: KnowledgeResearchBridge, research_id: str, query: str, **arguments: Any
) -> dict[str, Any]:
    payload, envelope = invoke(
        bridge, "search", {"researchId": research_id, "query": query, "limit": 20, **arguments}
    )
    assert envelope["result"]["isError"] is False, payload
    return payload


def test_schema_keeps_original_tools_and_adds_only_two(tmp_path: Path) -> None:
    bridge, _, _, _ = setup(tmp_path)
    response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert set(tools) == {
        "search",
        "searchByIds",
        "getFileDetails",
        "getTable",
        "researchBegin",
        "researchAddClaim",
        "researchAddClaims",
        "researchAddTable",
        "researchFinalize",
        "researchNavigate",
        "researchReadEvidence",
    }
    for name in ("search", "searchByIds", "getFileDetails", "getTable"):
        assert "researchId" in tools[name]["inputSchema"]["required"]
    assert "cursor" in tools["getTable"]["inputSchema"]["properties"]
    assert "report" in tools["researchNavigate"]["inputSchema"]["properties"]["view"]["enum"]
    assert (
        "evidenceRefs"
        in tools["researchAddClaims"]["inputSchema"]["properties"]["claims"]["items"]["properties"]
    )


def test_stock_gateway_properties_and_required_keep_model_selectors_unambiguous(
    tmp_path: Path,
) -> None:
    bridge, _, _, _ = setup(tmp_path)
    response = bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    # Stock discovery retains only these two schema fields.
    schemas = {
        tool["name"]: {key: tool["inputSchema"][key] for key in ("properties", "required")}
        for tool in response["result"]["tools"]
    }
    for name, refs, legacy in [
        ("getFileDetails", ["fileRef"], ["fileId"]),
        ("getTable", ["fileRef", "tableRef"], ["fileId", "tableId"]),
        ("researchReadEvidence", ["evidenceRef"], ["evidenceId"]),
        ("researchAddTable", ["tableRef"], ["tableId"]),
        ("researchAddClaim", ["evidenceRefs"], ["evidenceIds"]),
    ]:
        schema = schemas[name]
        assert set(refs) <= set(schema["required"])
        assert not set(legacy) & schema["properties"].keys()
        for ref in refs:
            value = schema["properties"][ref]
            assert "Never invent" in value.get("items", value)["description"]
    claim = schemas["researchAddClaims"]["properties"]["claims"]["items"]
    assert "evidenceRefs" in claim["required"] and "evidenceIds" not in claim["properties"]
    assert claim["properties"]["evidenceRefs"]["maxItems"] == 200
    scope = schemas["searchByIds"]["properties"]
    assert "fileIds" not in scope
    for name in ("fileRefs", "scopeRefs"):
        assert scope[name]["minItems"] == 1 and scope[name]["maxItems"] == 20
        assert "exactly one" in scope[name]["description"]
        assert (
            "fileRefs" in scope[name]["description"] and "scopeRefs" in scope[name]["description"]
        )
    assert "expectedTableIds" not in schemas["researchFinalize"]["properties"]
    assert (
        schemas["researchAddTable"]["properties"]["expectedTableHash"]["pattern"]
        == "^[0-9a-f]{64}$"
    )
    assert "expectedTableHash" not in schemas["researchAddTable"]["required"]
    assert "review" in schemas["researchNavigate"]["properties"]["view"]["enum"]
    assert schemas["researchBegin"]["properties"]["mode"]["default"] == "standard"
    assert schemas["researchBegin"]["properties"]["language"]["enum"] == ["zh-CN", "en"]


def test_begin_forwards_only_explicit_mode_and_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, _, _ = setup(tmp_path)
    received = []
    original = store.begin

    def begin(**kwargs: Any) -> dict[str, Any]:
        received.append(copy.deepcopy(kwargs))
        return original(title=kwargs["title"], subtitle=kwargs["subtitle"])

    monkeypatch.setattr(store, "begin", begin)
    for options in ({}, {"mode": "deep", "language": "zh-CN"}, {"mode": "standard"}):
        payload, response = invoke(bridge, "researchBegin", {"title": "Research", **options})
        assert not response["result"]["isError"], payload
        assert received[-1] == {"title": "Research", "subtitle": None, **options}


def test_finalize_pending_review_precedes_metadata_and_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, upstream, rid = setup(tmp_path)
    pending = {
        "status": "needs_review",
        "researchId": rid,
        "checks": [{"code": "SOURCE_COMPARISON_REQUIRED"}],
    }

    def pending_review(research_id: str) -> dict[str, Any]:
        assert research_id == rid
        return pending

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Review must be prepared before metadata fetching or rendering")

    monkeypatch.setattr(store, "pending_review", pending_review)
    monkeypatch.setattr(store, "missing_reference_metadata", unexpected)
    monkeypatch.setattr(store, "finalize", unexpected)
    before = store.snapshot(rid)
    payload, response = invoke(bridge, "researchFinalize", {"researchId": rid})
    assert not response["result"]["isError"] and payload == pending
    assert upstream.calls == [] and store.snapshot(rid) == before
    assert "publicArtifactManifest" not in payload and not store.output_root.exists()


def test_readable_directories_keep_known_metadata_and_executable_page_scope(tmp_path: Path) -> None:
    source = search_payload("q", ["pdf-source", "unknown-source"])
    first = source["results"][0]
    first.update(
        title="<b>Forecast</b>", filename="forecast.pdf", mediaType="application/pdf", pageCount=40
    )
    first["locator"] = {
        "title": "Forecast",
        "pageStart": 9,
        "pageEnd": 12,
        "sectionPath": [f"section-{i} " + "heading " * 30 for i in range(12)],
        "anchor": "sha256:private",
    }
    bridge, store, upstream, rid = setup(tmp_path, [result(source)])
    search = discovery(bridge, rid, "q")
    hit = search["results"][0]
    assert hit["title"] == "Forecast" and hit["fileType"] == "PDF" and hit["pageCount"] == 40
    assert hit["locator"]["sectionPath"] == first["locator"]["sectionPath"]
    assert "title" not in hit["locator"] and "anchor" not in hit["locator"]
    files, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "files", "limit": 1})
    entry = files["entries"][0]
    assert entry["title"] == hit["title"] and entry["fileType"] == "PDF"
    assert entry["observedLocators"] == [hit["locator"]]
    assert files["searchScope"] == {"fileRefs": [hit["fileRef"]]}
    assert "institution" not in entry and "publicationDate" not in entry
    last, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "cursor": files["nextCursor"]})
    assert "fileType" not in last["entries"][0] and "pageCount" not in last["entries"][0]
    evidence, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "evidence"})
    assert evidence["entries"][0]["locator"] == hit["locator"]
    serialized = json.dumps([search, files, evidence])
    assert REVISION not in serialized and "contentSha256" not in serialized
    private = store.snapshot(rid)["ledger"]["evidence"][first["evidenceId"]]
    assert private["revision"] == REVISION and private["locator"] == first["locator"]
    upstream.responses.append(result(search_payload("narrow", [], scoped=True)))
    narrow, envelope = invoke(
        bridge, "searchByIds", {"researchId": rid, "query": "narrow", **files["searchScope"]}
    )
    assert not envelope["result"]["isError"] and narrow["results"] == []
    assert upstream.calls[-1]["arguments"]["fileIds"] == ["pdf-source"]


def test_progress_counts_committed_scoped_calls_not_scope_membership_or_reading(
    tmp_path: Path,
) -> None:
    bridge, store, upstream, rid = setup(tmp_path, [result(search_payload("q", ["a", "b"]))])
    search = discovery(bridge, rid, "q")

    def progress() -> dict[str, Any]:
        reply, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "files"})
        return cast(dict[str, Any], reply["progress"])

    before = progress()
    assert before["discoveredFileCount"] == 2 and before["searchCallCount"] == 1
    assert before["searchByIdsCallCount"] == before["searchByIdsSelectedFileCount"] == 0
    assert before["completeEvidenceProjectionCount"] == 2
    assert not {"readFileCount", "readEvidenceCount", "scopedFileCount"} & before.keys()
    upstream.responses.extend(
        [
            result(search_payload("absent", [], scoped=True)),
            {"error": {"message": "Unavailable"}},
        ]
    )
    arguments = {
        "researchId": rid,
        "query": "absent",
        "scopeRefs": [search["scopeRef"]],
        "requestKey": "zero",
    }
    zero, _ = invoke(bridge, "searchByIds", arguments)
    assert zero["results"] == []
    replay, _ = invoke(bridge, "searchByIds", arguments)
    assert replay == zero
    counted = progress()
    assert counted["searchByIdsCallCount"] == counted["searchByIdsSuccessfulCallCount"] == 1
    assert counted["searchByIdsSelectedFileCount"] == 2
    assert counted["completeEvidenceProjectionCount"] == before["completeEvidenceProjectionCount"]
    invoke(bridge, "searchByIds", {**arguments, "requestKey": "failed"})
    failed = progress()
    assert failed["searchByIdsCallCount"] == 2 and failed["searchByIdsSuccessfulCallCount"] == 1
    assert failed["searchByIdsSelectedFileCount"] == 2 and len(upstream.calls) == 3
    restarted = KnowledgeResearchBridge(upstream, KnowledgeResearchStore(workspace=tmp_path))
    current, _ = invoke(restarted, "researchNavigate", {"researchId": rid, "view": "files"})
    assert current["progress"] == failed
    assert len(store.snapshot(rid)["ledger"]["calls"]) == 3


def test_same_content_hints_do_not_merge_or_deduplicate_similar_titles(tmp_path: Path) -> None:
    source = search_payload("q", ["a", "b", "c", "d"])
    source["results"][2]["content"] = "Different passage in a similarly titled report."
    source["results"][3]["content"] = "Another excerpt from the same source identity."
    source["results"][3]["documentId"] = source["results"][0]["documentId"]
    bridge, store, upstream, rid = setup(tmp_path, [result(source)])
    search = discovery(bridge, rid, "q")
    a, b, c, d = search["results"]
    assert b["sameContentAs"] == a["evidenceRef"]
    assert "not independent corroboration" in b["sourceRelationMeaning"]
    assert "sameContentAs" not in c and "relatedSourceFileRef" not in c
    assert d["relatedSourceFileRef"] == a["fileRef"]
    assert len({row["evidenceRef"] for row in search["results"]}) == 4
    assert len({row["fileRef"] for row in search["results"]}) == 4
    assert [row["content"] for row in search["results"]] == [
        row["content"] for row in source["results"]
    ]
    upstream.responses.append(result(search_payload("next", ["e"])))
    discovery(bridge, rid, "next")
    replay, _ = invoke(
        bridge, "researchNavigate", {"researchId": rid, "snapshotRef": search["snapshotRef"]}
    )
    assert replay == search
    assert len(store.snapshot(rid)["ledger"]["evidence"]) == 5


def read_locator_continuation(
    bridge: KnowledgeResearchBridge, rid: str, continuation: Mapping[str, Any]
) -> tuple[Any, list[dict[str, Any]]]:
    parts: list[str] = []
    pages: list[dict[str, Any]] = []
    cursor = continuation["cursor"]
    while cursor:
        page, response = invoke(
            bridge,
            "researchNavigate",
            {"researchId": rid, "cursor": cursor, "limit": 2},
            request_id='\u4e2d"\\\n' * 8,
        )
        assert not response["result"]["isError"], page
        assert page["format"] == "json" and page["offsetUnit"] == "unicode_code_point"
        for fragment in page["entries"]:
            assert fragment["contentRange"]["start"] == sum(map(len, parts))
            parts.append(fragment["content"])
        pages.append(page)
        cursor = page["nextCursor"]
    return json.loads("".join(parts)), pages


@pytest.mark.parametrize("heading", ["\u4e2d" * 25_000, '\u4e2d\U0001f680"\\\n\t\x00' * 6000])
def test_large_locator_has_lossless_bounded_continuation_and_restart(
    tmp_path: Path, heading: str
) -> None:
    from scripts.knowledge_research.locator import source_locator

    source = search_payload("q", ["long-location"])
    locator = {"page": {"start": 7, "end": 8}, "sectionPath": [heading, "Normal section"]}
    source["results"][0]["locator"] = locator
    bridge, store, upstream, rid = setup(tmp_path, [result(source)])
    search = discovery(bridge, rid, "q", requestKey="original-search")
    hit = search["results"][0]
    assert hit["content"] == source["results"][0]["content"]
    assert hit["contentTruncatedForTransport"] is False and hit["locatorComplete"] is False
    assert not search["projectionComplete"]
    assert (hit["locator"]["pageStart"], hit["locator"]["pageEnd"]) == (7, 8)
    restarted = KnowledgeResearchBridge(upstream, KnowledgeResearchStore(workspace=tmp_path))
    rebuilt, pages = read_locator_continuation(restarted, rid, hit["locatorContinuation"])
    assert rebuilt == source_locator(locator) and len(pages) > 1
    first_replay, _ = invoke(
        restarted,
        "researchNavigate",
        {"researchId": rid, "cursor": hit["locatorContinuation"]["cursor"], "limit": 2},
    )
    assert first_replay == pages[0]
    cached = discovery(restarted, rid, "q", requestKey="original-search")
    assert cached == search
    private = store.snapshot(rid)
    metadata_snapshot = private["extensions"]["navigation"]["snapshots"][pages[0]["snapshotRef"]]
    assert metadata_snapshot["payload"]["sourceSnapshotRef"] == search["snapshotRef"]
    assert private["ledger"]["evidence"][source["results"][0]["evidenceId"]]["locator"] == locator
    assert len(upstream.calls) == 1
    assert bridge._navigation(private).progress()["completeEvidenceProjectionCount"] == 1
    foreign, _ = invoke(restarted, "researchBegin", {"title": "Other research"})
    rejected, _ = invoke(
        restarted,
        "researchNavigate",
        {
            "researchId": foreign["researchId"],
            "cursor": hit["locatorContinuation"]["cursor"],
        },
    )
    assert rejected["details"]["code"] == "INVALID_CURSOR"


def test_old_snapshot_long_locator_and_directory_metadata_remain_recoverable(
    tmp_path: Path,
) -> None:
    from scripts.knowledge_research.locator import source_locator

    source = search_payload("q", ["location-source"])
    locator = {"sectionPath": ["\u4e2d" * 25_000], "pageStart": 1, "pageEnd": 2}
    source["results"][0]["locator"] = locator
    bridge, store, _, rid = setup(tmp_path)
    store.record_knowledge_call(
        research_id=rid,
        tool_name="search",
        arguments={"query": "q"},
        result=result(source)["result"],
    )

    def save_old(state: dict[str, Any]) -> str:
        nav = bridge._navigation(state)
        evidence = source["results"][0]
        nav.file_ref(evidence["fileId"])
        nav.reference(
            "evidence", evidence["evidenceId"], state["ledger"]["evidence"][evidence["evidenceId"]]
        )
        return nav.snapshot("search", source, [evidence["evidenceId"]], [REVISION])

    old_ref = store.atomic_update(rid, save_old)
    old, response = invoke(bridge, "researchNavigate", {"researchId": rid, "snapshotRef": old_ref})
    assert not response["result"]["isError"], old
    rebuilt, _ = read_locator_continuation(bridge, rid, old["results"][0]["locatorContinuation"])
    assert rebuilt == source_locator(locator)
    for view, field in (("files", "observedLocators"), ("evidence", "locator")):
        directory, response = invoke(bridge, "researchNavigate", {"researchId": rid, "view": view})
        assert not response["result"]["isError"], directory
        row = directory["entries"][0]
        recovered, _ = read_locator_continuation(bridge, rid, row[field + "Continuation"])
        assert recovered == (
            [source_locator(locator)] if view == "files" else source_locator(locator)
        )
    evidence, response = invoke(
        bridge,
        "researchReadEvidence",
        {
            "researchId": rid,
            "evidenceRef": old["results"][0]["evidenceRef"],
        },
    )
    assert (
        not response["result"]["isError"] and evidence["content"] == source["results"][0]["content"]
    )
    recovered, _ = read_locator_continuation(bridge, rid, evidence["locatorContinuation"])
    assert recovered == source_locator(locator)


@pytest.mark.parametrize("research_id", [None, "", "bad", "kr_" + "0" * 32])
def test_missing_or_invalid_research_rejected_before_upstream(
    tmp_path: Path, research_id: Any
) -> None:
    bridge, _, upstream, _ = setup(tmp_path)
    arguments = {"query": "q"}
    if research_id is not None:
        arguments["researchId"] = research_id
    payload, _ = invoke(bridge, "search", arguments)
    assert "error" in payload
    assert upstream.calls == []


@pytest.mark.parametrize("total", [54, 64])
def test_files_group_without_query_and_replay_does_not_grow(tmp_path: Path, total: int) -> None:
    batches = [
        [f"source-{i:03d}" for i in range(start, min(start + 20, total))]
        for start in range(0, total, 20)
    ]
    bridge, store, upstream, rid = setup(
        tmp_path, [result(search_payload(str(i), batch)) for i, batch in enumerate(batches)]
    )
    scopes = [
        discovery(bridge, rid, str(i), requestKey=f"discover-{i}")["scopeRef"]
        for i in range(len(batches))
    ]
    arguments = {"researchId": rid, "query": "focused", "scopeRefs": scopes, "requestKey": "group"}
    payload, _ = invoke(bridge, "searchByIds", arguments)
    assert payload["queried"] is False and payload["subset"] is True
    assert [group["fileCount"] for group in payload["groups"]] == list(map(len, batches))
    assert len(upstream.calls) == len(batches)
    before = store.snapshot(rid)
    replay, _ = invoke(bridge, "searchByIds", arguments)
    assert replay == payload
    assert store.snapshot(rid) == before
    grouped_page, _ = invoke(
        bridge,
        "researchNavigate",
        {"researchId": rid, "snapshotRef": payload["snapshotRef"], "limit": 1},
    )
    next_group, _ = invoke(
        bridge,
        "researchNavigate",
        {"researchId": rid, "cursor": grouped_page["nextCursor"], "limit": 1},
    )
    nav = store.snapshot(rid)["extensions"]["navigation"]
    receipts = [
        row for row in nav["projections"].values() if row["snapshotRef"] == payload["snapshotRef"]
    ]
    initial_receipt = next(row for row in receipts if row["range"] == payload["page"])
    group_receipt = next(row for row in receipts if row["range"] == next_group["page"])
    assert initial_receipt["orderedIds"] == [file_id for batch in batches for file_id in batch]
    assert group_receipt["orderedIds"] == batches[1]
    assert group_receipt["sourceRevisions"] == [REVISION] * len(batches[1])
    upstream.responses.append(result(search_payload("focused", batches[-1], scoped=True)))
    focused, envelope = invoke(
        bridge,
        "searchByIds",
        {
            "researchId": rid,
            "query": "focused",
            "limit": 20,
            "scopeRefs": [payload["groups"][-1]["scopeRef"]],
        },
    )
    assert not envelope["result"]["isError"]
    assert upstream.calls[-1]["arguments"]["fileIds"] == batches[-1]
    assert len(focused["results"]) == len(batches[-1])


def test_scope_is_actual_returned_files_not_requested_files(tmp_path: Path) -> None:
    bridge, store, _, rid = setup(
        tmp_path, [result(search_payload("q", ["returned"], scoped=True))]
    )
    payload, _ = invoke(
        bridge,
        "searchByIds",
        {"researchId": rid, "query": "q", "fileIds": ["requested", "returned"]},
    )
    scope = store.snapshot(rid)["extensions"]["navigation"]["scopes"][payload["scopeRef"]]
    assert scope["orderedIds"] == ["returned"]
    assert scope["sourceRevisions"] == [REVISION]


def test_references_are_namespaced_and_selectors_are_exclusive(tmp_path: Path) -> None:
    bridge, store, upstream, rid = setup(tmp_path, [result(search_payload("q", ["file-one"]))])
    payload = discovery(bridge, rid, "q")
    ref = payload["results"][0]["fileRef"]
    other, _ = invoke(bridge, "researchBegin", {"title": "Other research"})
    for arguments in [
        {"researchId": rid, "fileRefs": [ref], "fileIds": ["file-one"]},
        {"researchId": other["researchId"], "fileRefs": [ref]},
        {"researchId": rid, "fileRefs": ["D1"]},
        {"researchId": rid, "scopeRefs": ["S1"]},
    ]:
        reply, _ = invoke(bridge, "searchByIds", {"query": "q", **arguments})
        assert "error" in reply
    assert len(upstream.calls) == 1
    assert store.snapshot(other["researchId"])["ledger"]["evidence"] == {}
    hit = payload["results"][0]
    before = store.snapshot(rid)
    for name, arguments in [
        ("getFileDetails", {"fileRef": ref, "fileId": "file-one"}),
        ("researchReadEvidence", {"evidenceRef": hit["evidenceRef"], "evidenceId": "invented"}),
        (
            "researchAddClaim",
            {
                "section": "Findings",
                "text": "Finding.",
                "evidenceRefs": [hit["evidenceRef"]],
                "evidenceIds": ["invented"],
            },
        ),
        ("researchFinalize", {"expectedTableRefs": [], "expectedTableIds": []}),
    ]:
        rejected, _ = invoke(bridge, name, {"researchId": rid, **arguments})
        assert rejected["details"]["code"] == "INVALID_SELECTOR"
        assert store.snapshot(rid) == before
    assert len(upstream.calls) == 1


def test_full_evidence_unicode_pagination_reassembles_and_counts_prepared_only(
    tmp_path: Path,
) -> None:
    content = ('\u4e2d\U0001f680"\\\n\t\x00' * 14000) + "tail"
    bridge, store, upstream, rid = setup(
        tmp_path, [result(search_payload("q", ["source-001"], content))]
    )
    search = discovery(bridge, rid, "q", requestKey="discovery")
    hit = search["results"][0]
    assert hit["contentTruncatedForTransport"]
    assert not search["projectionComplete"]
    assert "fileId" not in hit and "evidenceId" not in hit
    received = []
    cursor = None
    page_index = 0
    while True:
        arguments = {
            "researchId": rid,
            "evidenceRef": hit["evidenceRef"],
            "requestKey": f"read-{page_index}",
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        page, _ = invoke(bridge, "researchReadEvidence", arguments, request_id='\u4e2d"\\\n' * 8)
        assert page["modelDelivery"] == "unknown"
        assert page["projectionPrepared"] is True
        assert "projectionReturned" not in page
        assert "contentSha256" not in page and "sourceRevision" not in page
        received.append(page["content"])
        before = store.snapshot(rid)
        replay, _ = invoke(bridge, "researchReadEvidence", arguments)
        assert replay == page and store.snapshot(rid) == before
        cursor = page["nextCursor"]
        page_index += 1
        if cursor is None:
            assert page["projectionComplete"] is False
            break
    assert "".join(received) == content
    assert len(upstream.calls) == 1
    state = store.snapshot(rid)
    assert (
        next(iter(state["ledger"]["evidence"].values()))["contentSha256"]
        == hashlib.sha256(content.encode()).hexdigest()
    )
    nav = state["extensions"]["navigation"]
    assert list(nav["coverage"].values()) == [[[0, len(content)]]]
    assert all(row["modelDelivery"] == "unknown" for row in nav["projections"].values())
    search_receipt = next(
        row for row in nav["projections"].values() if row["snapshotRef"] == search["snapshotRef"]
    )
    assert search_receipt["contentRanges"] == [
        {
            "evidenceId": next(iter(state["ledger"]["evidence"])),
            "start": 0,
            "end": len(hit["content"]),
        }
    ]
    assert "projectionReturned" not in json.dumps(nav)


def test_all_search_results_have_ordered_private_snapshot_and_resume(tmp_path: Path) -> None:
    ids = [f"source-{i:03d}" for i in range(20)]
    original = search_payload("q", ids, "z" * 18000)
    bridge, store, upstream, rid = setup(tmp_path, [result(original)])
    first = discovery(bridge, rid, "q")
    assert first["page"]["hasMore"]
    snapshot = store.snapshot(rid)["extensions"]["navigation"]["snapshots"][first["snapshotRef"]]
    assert snapshot["orderedIds"] == [row["evidenceId"] for row in original["results"]]
    assert snapshot["payload"]["results"] == original["results"]
    assert snapshot["sourceRevisions"] == [REVISION] * 20
    restarted = KnowledgeResearchBridge(
        upstream, KnowledgeResearchStore(workspace=tmp_path, media_root=tmp_path / "media")
    )
    seen = list(first["results"])
    cursor = first["nextCursor"]
    while cursor:
        page, _ = invoke(restarted, "researchNavigate", {"researchId": rid, "cursor": cursor})
        seen.extend(page["results"])
        cursor = page["nextCursor"]
    assert len(seen) == 20 and len({row["fileRef"] for row in seen}) == 20
    assert len(upstream.calls) == 1


def test_timeout_and_commit_crash_are_in_doubt_not_fake_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, upstream, rid = setup(tmp_path, [RuntimeError("interrupted")])
    arguments = {"researchId": rid, "query": "q", "requestKey": "uncertain"}
    invoke(bridge, "search", arguments)
    retry, _ = invoke(bridge, "search", arguments)
    assert retry["details"]["code"] == "IN_DOUBT"
    assert len(upstream.calls) == 1
    upstream.responses.append(result(search_payload("q", ["source"])))
    original_save = store._save
    saves = 0

    def crash(state: Mapping[str, Any]) -> None:
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("simulated commit failure")
        original_save(state)

    monkeypatch.setattr(store, "_save", crash)
    invoke(bridge, "search", {**arguments, "requestKey": "commit-crash"})
    monkeypatch.setattr(store, "_save", original_save)
    retry, _ = invoke(bridge, "search", {**arguments, "requestKey": "commit-crash"})
    assert retry["details"]["code"] == "IN_DOUBT"
    assert store.snapshot(rid)["ledger"]["evidence"] == {}


def test_ref_claim_translation_batch_replay_and_error_details(tmp_path: Path) -> None:
    original = search_payload("q", ["source"])
    bridge, store, _, rid = setup(tmp_path, [result(original)])
    hit = discovery(bridge, rid, "q")["results"][0]
    arguments = {
        "researchId": rid,
        "batchKey": "batch-a",
        "claims": [
            {
                "claimKey": "claim-a",
                "section": "Review",
                "text": "Supported finding.",
                "evidenceRefs": [hit["evidenceRef"]],
            }
        ],
    }
    payload, envelope = invoke(bridge, "researchAddClaims", arguments)
    assert not envelope["result"]["isError"], payload
    invoke(bridge, "researchAddClaims", arguments)
    claims = store.snapshot(rid)["report"]["items"]
    assert len(claims) == 1
    assert claims[0]["evidenceIds"] == [original["results"][0]["evidenceId"]]
    changed: dict[str, Any] = copy.deepcopy(arguments)
    changed["claims"][0]["text"] = "Changed finding."
    error, _ = invoke(bridge, "researchAddClaims", changed)
    assert error["details"]["code"]
    assert len(store.snapshot(rid)["report"]["items"]) == 1


def test_optional_host_binding_is_not_model_supplied_auth(tmp_path: Path) -> None:
    store = KnowledgeResearchStore(workspace=tmp_path)
    upstream = Upstream()
    owner = KnowledgeResearchBridge(upstream, store, caller_binding=lambda: "trusted-owner")
    begin, _ = invoke(owner, "researchBegin", {"title": "Research"})
    stranger = KnowledgeResearchBridge(upstream, store, caller_binding=lambda: "another-owner")
    payload, _ = invoke(
        stranger,
        "researchNavigate",
        {"researchId": begin["researchId"], "callerBinding": "trusted-owner"},
    )
    assert payload["details"]["code"] == "RESEARCH_NOT_FOUND"
    assert upstream.calls == []


def test_report_recovery_snapshot_and_expected_hash_repair(tmp_path: Path) -> None:
    original = search_payload("q", ["source"])
    bridge, store, upstream, rid = setup(tmp_path, [result(original)])
    empty, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "report"})
    assert empty["entries"] == [] and empty["projectionComplete"]
    hit = discovery(bridge, rid, "q")["results"][0]
    prose = "Report body is deliberately absent from the recovery directory. " * 200
    claims = [
        {
            "claimKey": f"finding-{index:02d}",
            "section": "Findings",
            "text": prose,
            "evidenceRefs": [hit["evidenceRef"]],
        }
        for index in range(23)
    ]
    added, response = invoke(
        bridge, "researchAddClaims", {"researchId": rid, "claims": claims, "batchKey": "draft"}
    )
    assert not response["result"]["isError"], added
    legacy, _ = invoke(
        bridge,
        "researchAddClaim",
        {
            "researchId": rid,
            "section": "Legacy",
            "text": "Earlier paragraph without a key.",
            "evidenceIds": [original["results"][0]["evidenceId"]],
        },
    )
    arguments = {"researchId": rid, "view": "report", "requestKey": "recover-draft"}
    first, response = invoke(bridge, "researchNavigate", arguments)
    assert not response["result"]["isError"], first
    assert first["page"] == {"start": 0, "end": 20, "total": 24, "hasMore": True}
    assert not first["projectionComplete"]
    private = store.snapshot(rid)
    snapshot = private["extensions"]["navigation"]["snapshots"][first["snapshotRef"]]
    assert snapshot["orderedIds"] == [row["itemId"] for row in private["report"]["items"]]
    assert snapshot["sourceRevisions"] == [private["report"]["revision"]] * 24
    assert "text" not in json.dumps(snapshot) and prose not in json.dumps(first)
    assert "evidenceIds" not in json.dumps(snapshot)
    assert first["entries"][0] == {
        "kind": "claim",
        "item": added["items"][0],
        "section": "Findings",
        "claimKey": "finding-00",
        "claimHash": added["claimHashes"]["finding-00"],
    }
    resumed_bridge = KnowledgeResearchBridge(
        upstream, KnowledgeResearchStore(workspace=tmp_path, media_root=tmp_path / "media")
    )
    repaired = {
        **claims[20],
        "text": "Corrected finding.",
        "expectedClaimHash": added["claimHashes"]["finding-20"],
    }
    repair_args = {"researchId": rid, "claims": [repaired], "batchKey": "repair"}
    repair, response = invoke(resumed_bridge, "researchAddClaims", repair_args)
    assert not response["result"]["isError"], repair
    assert repair["updatedCount"] == 1 and repair["items"] == [added["items"][20]]
    repaired_state = store.snapshot(rid)
    replay, _ = invoke(resumed_bridge, "researchAddClaims", repair_args)
    assert replay == repair and store.snapshot(rid) == repaired_state
    stale, response = invoke(
        resumed_bridge,
        "researchAddClaims",
        {"researchId": rid, "claims": [{**repaired, "text": "Stale edit."}]},
    )
    assert response["result"]["isError"]
    assert stale["details"]["code"] == "CLAIM_HASH_CONFLICT"
    assert stale["details"]["issues"][0]["path"] == "/claims/0/expectedClaimHash"
    assert stale["details"]["issues"][0]["currentClaimHash"] == repair["claimHashes"]["finding-20"]
    assert store.snapshot(rid) == repaired_state
    last, _ = invoke(
        resumed_bridge,
        "researchNavigate",
        {"researchId": rid, "view": "report", "cursor": first["nextCursor"]},
    )
    assert last["entries"][0]["claimHash"] == added["claimHashes"]["finding-20"]
    assert last["reportRevision"] == first["reportRevision"]
    assert not last["projectionComplete"] and last["nextCursor"] is None
    assert last["entries"][-1]["claimKey"] is None
    assert last["entries"][-1]["item"] == legacy["item"]
    replay, _ = invoke(resumed_bridge, "researchNavigate", arguments)
    assert replay == first
    current, _ = invoke(resumed_bridge, "researchNavigate", {"researchId": rid, "view": "report"})
    assert current["reportRevision"] > first["reportRevision"]
    assert current["snapshotRef"] != first["snapshotRef"]
    current_last, _ = invoke(
        resumed_bridge, "researchNavigate", {"researchId": rid, "cursor": current["nextCursor"]}
    )
    assert current_last["entries"][0]["claimHash"] == repair["claimHashes"]["finding-20"]
    item = store.snapshot(rid)["report"]["items"][20]
    assert item["evidenceIds"] == [original["results"][0]["evidenceId"]]
    assert current_last["entries"][0]["claimHash"] == claim_hash(item)
    assert len(upstream.calls) == 1


def test_navigation_rejects_conflicting_cursor_view_and_snapshot(tmp_path: Path) -> None:
    bridge, store, upstream, rid = setup(tmp_path, [result(search_payload("q", ["source"]))])
    search = discovery(bridge, rid, "q")
    report, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "report"})
    snapshot = store.snapshot(rid)
    nav = bridge._navigation(snapshot)
    cursor = nav.cursor(search["snapshotRef"], 0)
    for selectors in [
        {"view": "report", "cursor": cursor},
        {"snapshotRef": report["snapshotRef"], "cursor": cursor},
        {"view": "report", "snapshotRef": search["snapshotRef"]},
        {"view": "files", "snapshotRef": report["snapshotRef"]},
    ]:
        payload, response = invoke(bridge, "researchNavigate", {"researchId": rid, **selectors})
        assert response["result"]["isError"]
        assert payload["details"]["code"] == "REFERENCE_MISMATCH"
        assert store.snapshot(rid) == snapshot
    assert len(upstream.calls) == 1


def test_inventory_and_table_cursors_use_original_tools(tmp_path: Path) -> None:
    from scripts.knowledge_research.review import table_item_hash

    media = tmp_path / "media"
    media.mkdir()
    screenshot = media / "table.png"
    screenshot.write_bytes(PNG)
    tables = [
        {
            "tableId": "t1_" + f"{i:032x}",
            "page": i + 1,
            "ordinal": i,
            "text": {
                "format": "html",
                "content": "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
            },
        }
        for i in range(24)
    ]
    details = {
        "contractVersion": "knowledge-vnext/2",
        "file": {
            "fileId": "source",
            "documentId": "document-source",
            "revision": REVISION,
            "title": "Source",
            "filename": "source.pdf",
            "mediaType": "application/pdf",
        },
        "tableExtraction": {"tableCount": 24},
        "tables": tables,
        "nextCursor": None,
        "inventoryComplete": True,
        "inventoryPageCount": 2,
        "inventoryTableCount": 24,
        "tableTextProjection": "source-preserved",
    }
    content = "<table>" + "<tr><td>123</td></tr>" * 1200 + "</table>"
    table = {
        "schemaVersion": "knowledge-table-artifact/2",
        "fileId": "source",
        "documentId": "document-source",
        "revision": REVISION,
        "tableId": tables[0]["tableId"],
        "page": 1,
        "locator": {"page": 1},
        "text": {
            "format": "html",
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
        "screenshot": {
            "mediaType": "image/png",
            "sha256": hashlib.sha256(PNG).hexdigest(),
            "sizeBytes": len(PNG),
            "localPath": str(screenshot),
        },
    }
    bridge, store, upstream, rid = setup(
        tmp_path, [result(search_payload("q", ["source"])), result(details), result(table)]
    )
    hit = discovery(bridge, rid, "q")["results"][0]
    arguments = {"researchId": rid, "fileRef": hit["fileRef"]}
    inventory, _ = invoke(bridge, "getFileDetails", arguments)
    assert inventory["page"]["hasMore"] and inventory["extractedInventoryComplete"]
    last, _ = invoke(bridge, "getFileDetails", {**arguments, "cursor": inventory["nextCursor"]})
    assert not last["projectionComplete"] and last["extractedInventoryComplete"]
    table_arguments = {**arguments, "tableRef": inventory["tables"][0]["tableRef"]}
    parts = []
    cursor = None
    while True:
        page, envelope = invoke(
            bridge, "getTable", {**table_arguments, **({"cursor": cursor} if cursor else {})}
        )
        assert not envelope["result"]["isError"], page
        assert page["quality"]["visualCheck"] == "not_performed"
        assert page["quality"]["sourceCompleteness"] == "unknown"
        parts.append(page["text"]["content"])
        cursor = page["nextCursor"]
        if cursor is None:
            assert not page["projectionComplete"]
            break
    assert "".join(parts) == content
    assert len(upstream.calls) == 3
    added, envelope = invoke(
        bridge,
        "researchAddTable",
        {
            "researchId": rid,
            "section": "Tables",
            "caption": "Source data",
            "tableRef": table_arguments["tableRef"],
        },
    )
    assert not envelope["result"]["isError"], added
    assert store.snapshot(rid)["report"]["items"][0]["tableId"] == table["tableId"]
    arguments = {
        "researchId": rid,
        "section": "Tables",
        "caption": "Source data",
        "tableRef": table_arguments["tableRef"],
    }
    before = store.snapshot(rid)
    replay, response = invoke(bridge, "researchAddTable", arguments)
    assert not response["result"]["isError"] and replay == added
    assert store.snapshot(rid) == before
    legacy_arguments = {key: value for key, value in arguments.items() if key != "tableRef"}
    legacy, response = invoke(
        bridge, "researchAddTable", {**legacy_arguments, "tableId": table["tableId"]}
    )
    assert not response["result"]["isError"] and legacy == added
    assert store.snapshot(rid) == before
    for table_id in (table["tableId"], "invented-label", None):
        error, response = invoke(bridge, "researchAddTable", {**arguments, "tableId": table_id})
        assert response["result"]["isError"]
        assert error["details"]["code"] == "INVALID_SELECTOR"
        assert store.snapshot(rid) == before
    for changed in ({"section": "Other"}, {"caption": "Changed caption"}):
        error, response = invoke(bridge, "researchAddTable", {**arguments, **changed})
        assert response["result"]["isError"]
        assert error["details"]["code"] == "TABLE_ITEM_CONFLICT"
        assert error["details"]["issues"][0]["path"] == "/" + next(iter(changed))
        assert store.snapshot(rid) == before
    report, response = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "report"})
    assert not response["result"]["isError"]
    assert report["entries"] == [
        {
            "kind": "table",
            "item": added["item"],
            "section": "Tables",
            "caption": "Source data",
            "tableRef": table_arguments["tableRef"],
            "tableHash": table_item_hash(store.snapshot(rid)["report"]["items"][0]),
        }
    ]
    assert str(table["tableId"]) not in json.dumps(report)
    assert content not in json.dumps(report) and "localPath" not in json.dumps(report)
    original_hash = report["entries"][0]["tableHash"]
    corrected, response = invoke(
        bridge,
        "researchAddTable",
        {**arguments, "caption": "Corrected data", "expectedTableHash": original_hash},
    )
    assert not response["result"]["isError"], corrected
    corrected_state = store.snapshot(rid)
    assert corrected_state["report"]["items"][0]["caption"] == "Corrected data"
    assert len(corrected_state["report"]["items"]) == 1
    stale, response = invoke(
        bridge,
        "researchAddTable",
        {**arguments, "caption": "Another change", "expectedTableHash": original_hash},
    )
    assert response["result"]["isError"], stale
    assert store.snapshot(rid) == corrected_state
