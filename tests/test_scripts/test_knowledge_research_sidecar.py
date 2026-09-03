from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from opensquilla.mcp.stdio import MCPStdioClient
from opensquilla.mcp.types import MCPServerConfig
from scripts.knowledge_research.bridge import KnowledgeResearchBridge
from scripts.knowledge_research.references import build_bibliography
from scripts.knowledge_research.report import render_html_report
from scripts.knowledge_research.state import KnowledgeResearchStore

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
EVIDENCE_ID = "ev4_11111111111111111111111111111111"
TABLE_ID = "t1_22222222222222222222222222222222"
FILE_ID = "file-001"
DOCUMENT_ID = "document-001"
REVISION = "a" * 64


class FakeUpstream:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, Mapping[str, Any] | None]] = []
        self.notifications: list[str] = []
        self.closed = False

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": "inner",
                "result": {
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search Knowledge",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["query"],
                                "properties": {"query": {"type": "string"}},
                            },
                        },
                        {
                            "name": "getFileDetails",
                            "description": "Complete table inventory",
                            "inputSchema": {
                                "type": "object",
                                "required": ["fileId"],
                                "properties": {"fileId": {"type": "string"}},
                            },
                        },
                    ]
                },
            }
        if not self.responses:
            raise AssertionError(f"unexpected upstream request: {method} {params}")
        return self.responses.pop(0)

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        del params
        self.notifications.append(method)

    def close(self) -> None:
        self.closed = True


def _mcp_result(payload: dict[str, Any], *, structured: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "isError": False,
    }
    if structured:
        result["structuredContent"] = payload
    return result


def _response(payload: dict[str, Any], *, structured: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "inner",
        "result": _mcp_result(payload, structured=structured),
    }


def _search_payload(*, file_id: str = FILE_ID) -> dict[str, Any]:
    return {
        "contractVersion": "knowledge-vnext/2",
        "chunkPolicyId": "hierarchical_token_v4",
        "indexVersion": "knowledge-index-v5",
        "query": "KOSPI drivers",
        "requestedProfile": None,
        "effectiveProfile": "hybrid_rrf_bge_m3_fts5",
        "retrievalProfile": "hybrid_rrf_bge_m3_fts5",
        "selectionSource": "service_default",
        "fallbackReason": None,
        "warnings": [],
        "scopeEnforced": True,
        "budgetExceeded": None,
        "selectionStrategy": "pure_score",
        "lexicalCandidateCount": 12,
        "vectorCandidateCount": 15,
        "results": [
            {
                "evidenceId": EVIDENCE_ID,
                "fileId": file_id,
                "documentId": DOCUMENT_ID,
                "chunkId": "doc-v4:child:001",
                "revision": REVISION,
                "title": "Korea Equity Strategy",
                "content": "KOSPI advanced as semiconductor earnings improved.",
                "locator": {
                    "title": "Market review",
                    "sectionPath": ["Korea"],
                    "pageStart": 3,
                    "pageEnd": 3,
                },
            }
        ],
        "count": 1,
    }


def _details_payload() -> dict[str, Any]:
    return {
        "contractVersion": "knowledge-vnext/2",
        "file": {
            "fileId": FILE_ID,
            "documentId": DOCUMENT_ID,
            "title": "Korea Equity Strategy",
            "filename": "korea-equity.pdf",
            "sourcePath": "research/korea-equity.pdf",
            "mediaType": "application/pdf",
            "revision": REVISION,
        },
        "tableExtraction": {"status": "ready", "tableCount": 1},
        "tables": [
            {
                "tableId": TABLE_ID,
                "page": 7,
                "ordinal": 0,
                "screenshotAvailable": True,
                "textFormat": "html",
            }
        ],
        "nextCursor": None,
        "inventoryComplete": True,
        "inventoryPageCount": 2,
        "inventoryTableCount": 1,
    }


def _table_payload(screenshot_path: Path) -> dict[str, Any]:
    table_html = (
        "<table><tr><th>Index</th><th>Return</th></tr><tr><td>KOSPI</td><td>8%</td></tr></table>"
    )
    image_sha = hashlib.sha256(PNG).hexdigest()
    return {
        "schemaVersion": "knowledge-table-artifact/2",
        "tableId": TABLE_ID,
        "fileId": FILE_ID,
        "documentId": DOCUMENT_ID,
        "revision": REVISION,
        "page": 7,
        "locator": {"page": 7},
        "text": {
            "format": "html",
            "content": table_html,
            "sha256": hashlib.sha256(table_html.encode()).hexdigest(),
        },
        "screenshot": {
            "mediaType": "image/png",
            "sha256": image_sha,
            "sizeBytes": len(PNG),
            "localPath": str(screenshot_path),
        },
        "screenshotLocalPath": str(screenshot_path),
    }


def _call(
    bridge: KnowledgeResearchBridge,
    name: str,
    arguments: dict[str, Any],
    *,
    request_id: int = 1,
) -> dict[str, Any]:
    response = bridge.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    assert response["id"] == request_id
    result = response["result"]
    assert isinstance(result, dict)
    return result


def _result_payload(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    assert isinstance(content, list)
    payload = json.loads(content[0]["text"])
    assert isinstance(payload, dict)
    return payload


def _begin(bridge: KnowledgeResearchBridge) -> str:
    result = _call(bridge, "researchBegin", {"title": "KOSPI Research"})
    research_id = _result_payload(result)["researchId"]
    assert isinstance(research_id, str)
    return research_id


def test_full_v9_shape_builds_verified_private_media_and_three_public_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    screenshot = media_root / "table.png"
    screenshot.write_bytes(PNG)
    captured_html: list[str] = []

    def render_pdf(html: str, base_url: Path) -> bytes:
        assert base_url == workspace.resolve()
        captured_html.append(html)
        return b"%PDF-1.7\nverified-test-pdf\n"

    upstream = FakeUpstream(
        [
            _response(_search_payload()),
            _response(_details_payload()),
            _response(_table_payload(screenshot)),
        ]
    )
    store = KnowledgeResearchStore(
        workspace=workspace,
        media_root=media_root,
        pdf_renderer=render_pdf,
    )
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)

    search_result = _call(
        bridge,
        "searchByIds",
        {
            "researchId": research_id,
            "query": "KOSPI drivers",
            "fileIds": [FILE_ID],
        },
    )
    assert search_result["isError"] is False
    projected_search = _result_payload(search_result)
    assert "chunkId" not in projected_search["results"][0]

    details_result = _call(
        bridge,
        "getFileDetails",
        {"researchId": research_id, "fileId": FILE_ID},
    )
    assert details_result["isError"] is False
    assert "structuredContent" not in details_result
    detail_requests = [
        request
        for request in upstream.requests
        if request[0] == "tools/call"
        and request[1] is not None
        and request[1].get("name") == "getFileDetails"
    ]
    assert len(detail_requests) == 1

    table_result = _call(
        bridge,
        "getTable",
        {
            "researchId": research_id,
            "fileId": FILE_ID,
            "tableId": TABLE_ID,
            "includeScreenshot": False,
        },
    )
    assert table_result["isError"] is False
    assert "structuredContent" not in table_result
    table_request = upstream.requests[-1][1]
    assert table_request is not None
    assert table_request["arguments"]["includeScreenshot"] is True
    assert "base64" not in json.dumps(table_result).lower()

    rejected = _call(
        bridge,
        "researchAddClaim",
        {
            "researchId": research_id,
            "section": "Performance",
            "text": "This paragraph has no valid source.",
            "evidenceIds": ["ev4_99999999999999999999999999999999"],
        },
    )
    assert rejected["isError"] is True

    assert (
        _call(
            bridge,
            "researchAddClaim",
            {
                "researchId": research_id,
                "section": "Performance",
                "text": "KOSPI gained as semiconductor earnings improved.",
                "evidenceIds": [EVIDENCE_ID],
            },
        )["isError"]
        is False
    )
    assert (
        _call(
            bridge,
            "researchAddTable",
            {
                "researchId": research_id,
                "section": "Evidence Table",
                "tableId": TABLE_ID,
                "caption": "Reported KOSPI return",
            },
        )["isError"]
        is False
    )

    finalized = _result_payload(_call(bridge, "researchFinalize", {"researchId": research_id}))
    manifest = finalized["publicArtifactManifest"]
    assert [item["name"] for item in manifest["files"]] == [
        "report.html",
        "report.pdf",
        "provenance.json",
    ]
    assert {item["bundle"] for item in manifest["files"]} == {"none"}

    output_dir = workspace / "knowledge-reports" / research_id
    assert {path.name for path in output_dir.iterdir()} == {
        "report.html",
        "report.pdf",
        "provenance.json",
    }
    html = (output_dir / "report.html").read_text(encoding="utf-8")
    assert "<table>" in html
    assert "data:image/png;base64," in html
    assert "[1, p. 3]" in html
    assert "Korea Equity Strategy" in html
    assert ".parsed-table { display: none; }" in html
    for internal_id in (FILE_ID, EVIDENCE_ID, TABLE_ID, research_id):
        assert internal_id not in html

    provenance = json.loads((output_dir / "provenance.json").read_text())
    assert EVIDENCE_ID in provenance["ledger"]["evidence"]
    assert provenance["ledger"]["evidence"][EVIDENCE_ID]["chunkId"] == ("doc-v4:child:001")
    assert TABLE_ID in provenance["ledger"]["tables"]
    assert "screenshotPrivatePath" not in provenance["ledger"]["tables"][TABLE_ID]
    assert "localPath" not in provenance["ledger"]["tables"][TABLE_ID]["screenshot"]
    assert captured_html == [html]

    snapshot = store.snapshot(research_id)
    call = snapshot["ledger"]["calls"][0]
    assert call["structuredSource"] == "content[0].text"
    assert call["retrieval"] == {
        "requestedProfile": None,
        "contractVersion": "knowledge-vnext/2",
        "chunkPolicyId": "hierarchical_token_v4",
        "indexVersion": "knowledge-index-v5",
        "effectiveProfile": "hybrid_rrf_bge_m3_fts5",
        "retrievalProfile": "hybrid_rrf_bge_m3_fts5",
        "selectionSource": "service_default",
        "fallbackReason": None,
        "warnings": [],
        "scopeEnforced": True,
        "selectionStrategy": "pure_score",
        "budgetExceeded": None,
        "lexicalCandidateCount": 12,
        "vectorCandidateCount": 15,
    }
    assert finalized["coverage"] == {
        "claimCount": 1,
        "tableCount": 1,
        "sourceCount": 1,
        "sourceFileCount": 1,
        "evidenceCount": 1,
    }
    assert snapshot["ledger"]["files"][FILE_ID]["observedLocators"]
    assert snapshot["ledger"]["inventories"][FILE_ID]["pageCount"] == 2
    private_state = workspace / ".codex" / "knowledge-research" / research_id
    assert len(list((private_state / "media").iterdir())) == 1
    assert base64.b64encode(PNG).decode() not in (private_state / "state.json").read_text()


def test_bibliography_pairs_formats_but_preserves_different_issues() -> None:
    files: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for index, (date, extension) in enumerate(
        [
            ("2026-06-26", "md"),
            ("2026-06-26", "pdf"),
            ("2026-07-10", "md"),
        ]
    ):
        stem = f"{date}+Korea Weekly Kickstart Market performance and earnings"
        file_id = f"private-file-{index}"
        files[file_id] = {
            "title": "KOREA WEEKLY KICKSTART",
            "filename": f"{stem}.{extension}",
            "sourcePath": f"goldman/{date}/{stem}/{stem}.{extension}",
            "revision": str(index) * 64,
            "verificationStatus": "verified",
        }
        evidence[f"e-{index}"] = {
            "fileId": file_id,
            "locator": {"pageStart": index + 1, "pageEnd": index + 1},
        }
    state = {
        "title": "Research",
        "ledger": {"files": files, "evidence": evidence, "tables": {}},
        "report": {
            "items": [
                {
                    "kind": "claim",
                    "section": "Review",
                    "text": "Claim",
                    "evidenceIds": list(evidence),
                }
            ]
        },
    }
    bibliography = build_bibliography(state)
    assert bibliography["sourceFileCount"] == 3
    assert bibliography["sourceCount"] == 2
    assert bibliography["fileReferenceNumbers"] == {
        "private-file-0": 1,
        "private-file-1": 1,
        "private-file-2": 2,
    }
    assert "2026-06-26" in bibliography["references"][0]["title"]
    assert "2026-07-10" in bibliography["references"][1]["title"]
    html = render_html_report(state)
    assert "[1, text version]" in html
    assert "[1, p. 2]" in html
    assert "[2, text version]" in html
    assert "[1, p. 1]" not in html
    assert html.count('<span class="citation">') == 3
    assert "private-file-" not in html
    assert build_bibliography(copy.deepcopy(state)) == bibliography

    # Generic date folders, different report stems and ambiguous versions must not merge.
    files["private-file-1"]["sourcePath"] = "goldman/2026-06-26/different-report.pdf"
    assert build_bibliography(state)["sourceCount"] == 3
    files["private-file-1"]["sourcePath"] = files["private-file-0"]["sourcePath"].replace(
        ".md", ".pdf"
    )
    files["private-file-2"] = copy.deepcopy(files["private-file-1"])
    assert build_bibliography(state)["sourceCount"] == 3
    files["private-file-1"].update(title="[page 1]", filename="2~aaaabbbb.pdf")
    files["private-file-1"]["sourcePath"] = str(
        Path(files["private-file-0"]["sourcePath"]).parent / "2~aaaabbbb.pdf"
    )
    reference = build_bibliography(state)["references"][1]
    # A short imported basename does not establish its parent's document lineage.
    assert reference["title"] == "2"
    assert reference["titleSelection"]["basis"] == "source_filename_fallback"
    assert reference["groupingBasis"] == "distinct_source_file"


def test_finalize_hydrates_only_cited_metadata_once_without_changing_inventory(
    tmp_path: Path,
) -> None:
    store = KnowledgeResearchStore(workspace=tmp_path, pdf_renderer=lambda *_: b"%PDF-test")
    upstream = FakeUpstream([_response(_search_payload()), _response(_details_payload())])
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)
    assert not _call(
        bridge, "searchByIds", {"researchId": research_id, "query": "KOSPI", "fileIds": [FILE_ID]}
    )["isError"]
    _call(
        bridge,
        "researchAddClaim",
        {
            "researchId": research_id,
            "section": "Review",
            "text": "A supported statement",
            "evidenceIds": [EVIDENCE_ID],
        },
    )
    result = _call(bridge, "researchFinalize", {"researchId": research_id})
    assert result["isError"] is False
    state = store.snapshot(research_id)
    assert state["ledger"]["inventories"] == {}
    assert state["ledger"]["calls"][-1]["purpose"] == "bibliography_metadata"
    assert state["ledger"]["files"][FILE_ID]["filename"] == "korea-equity.pdf"
    assert _call(bridge, "researchFinalize", {"researchId": research_id})["isError"] is False
    assert len(upstream.requests) == 2
    provenance = json.loads(
        (tmp_path / "knowledge-reports" / research_id / "provenance.json").read_text()
    )
    assert provenance["bibliography"]["fileReferenceNumbers"][FILE_ID] == 1

    before = copy.deepcopy(state["ledger"]["files"])
    bad_details = _details_payload()
    bad_details["file"]["revision"] = "b" * 64
    call = store.record_knowledge_call(
        research_id=research_id,
        tool_name="getFileDetails",
        arguments={"fileId": FILE_ID},
        result=_mcp_result(bad_details),
        metadata_only=True,
    )
    assert call["verificationStatus"] == "unverified_revision_mismatch"
    assert store.snapshot(research_id)["ledger"]["files"] == before


def test_search_by_ids_scope_violation_is_rejected_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    payload = _search_payload()
    outside_result = dict(payload["results"][0])
    outside_result["evidenceId"] = "ev4_33333333333333333333333333333333"
    outside_result["fileId"] = "file-outside"
    payload["results"].append(outside_result)
    payload["count"] = 2
    upstream = FakeUpstream([_response(payload)])
    store = KnowledgeResearchStore(workspace=workspace, media_root=media_root)
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)

    result = _call(
        bridge,
        "searchByIds",
        {
            "researchId": research_id,
            "query": "KOSPI",
            "fileIds": [FILE_ID],
        },
    )

    assert result["isError"] is True
    snapshot = store.snapshot(research_id)
    assert snapshot["ledger"]["evidence"] == {}
    assert snapshot["ledger"]["files"] == {}
    assert snapshot["ledger"]["calls"][0]["verificationStatus"] == ("unverified_scope_violation")


def test_model_projection_bounds_search_and_table_inventory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    search_payload = _search_payload()
    search_payload["selectionStrategy"] = "hierarchical_interleave"
    search_payload["results"][0]["content"] = "x" * 5_000
    search_payload["results"][0]["locator"].update(
        {"anchor": "sha256:hidden", "charStart": 1, "charEnd": 5_001}
    )
    details_payload = _details_payload()
    details_payload["tables"][0].update(
        {
            "documentId": DOCUMENT_ID,
            "revision": REVISION,
            "extractor": {"name": "large-metadata"},
            "text": {"format": "html", "content": "y" * 2_000},
        }
    )
    upstream = FakeUpstream([_response(search_payload), _response(details_payload)])
    store = KnowledgeResearchStore(workspace=workspace, media_root=media_root)
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)

    search_result = _result_payload(
        _call(
            bridge,
            "search",
            {"researchId": research_id, "query": "KOSPI"},
        )
    )
    item = search_result["results"][0]
    assert item["content"] == "x" * 5_000
    assert item["contentTruncatedForTransport"] is False
    assert item["contentRange"] == {"start": 0, "end": 5_000, "total": 5_000}
    assert "revision" not in item
    assert set(item["locator"]) == {"title", "sectionPath", "pageStart", "pageEnd"}

    details_result = _result_payload(
        _call(
            bridge,
            "getFileDetails",
            {"researchId": research_id, "fileId": FILE_ID},
        )
    )
    assert set(details_result["file"]) == {"fileRef", "title", "filename", "mediaType"}
    table = details_result["tables"][0]
    assert "documentId" not in table
    assert "extractor" not in table
    assert "textPreview" not in table
    assert table["tableRef"]
    assert table["parseComplete"] is False
    assert table["tables"] == []
    assert "no_structured_table" in table["issues"]
    assert store.snapshot(research_id)["ledger"]["evidence"][EVIDENCE_ID]["content"] == (
        "x" * 5_000
    )


def test_batch_claims_are_atomic(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    upstream = FakeUpstream([_response(_search_payload())])
    store = KnowledgeResearchStore(workspace=workspace, media_root=media_root)
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)
    _call(
        bridge,
        "searchByIds",
        {"researchId": research_id, "query": "KOSPI", "fileIds": [FILE_ID]},
    )

    accepted = _result_payload(
        _call(
            bridge,
            "researchAddClaims",
            {
                "researchId": research_id,
                "claims": [
                    {
                        "section": "Performance",
                        "text": "KOSPI advanced with semiconductor earnings.",
                        "evidenceIds": [EVIDENCE_ID],
                    },
                    {
                        "section": "Outlook",
                        "text": "The cited outlook remains conditional.",
                        "evidenceIds": [EVIDENCE_ID],
                    },
                ],
            },
        )
    )
    assert accepted["claimCount"] == 2

    rejected = _call(
        bridge,
        "researchAddClaims",
        {
            "researchId": research_id,
            "claims": [
                {
                    "section": "Valid",
                    "text": "This would be valid alone.",
                    "evidenceIds": [EVIDENCE_ID],
                },
                {
                    "section": "Invalid",
                    "text": "This invalid claim must roll back the batch.",
                    "evidenceIds": ["ev4_99999999999999999999999999999999"],
                },
            ],
        },
    )
    assert rejected["isError"] is True
    assert len(store.snapshot(research_id)["report"]["items"]) == 2


def test_retrieval_fallback_is_rejected_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    payload = _search_payload()
    payload["effectiveProfile"] = "sqlite_fts5_default"
    payload["retrievalProfile"] = "sqlite_fts5_default"
    payload["selectionSource"] = "fallback"
    payload["fallbackReason"] = "hybrid_vector_failed"
    payload["warnings"] = ["hybrid_vector_failed_fallback_to_fts5"]
    upstream = FakeUpstream([_response(payload)])
    store = KnowledgeResearchStore(workspace=workspace, media_root=media_root)
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)

    result = _call(
        bridge,
        "searchByIds",
        {"researchId": research_id, "query": "KOSPI", "fileIds": [FILE_ID]},
    )

    assert result["isError"] is True
    snapshot = store.snapshot(research_id)
    assert snapshot["ledger"]["evidence"] == {}
    assert snapshot["ledger"]["calls"][0]["verificationStatus"] == ("unverified_retrieval_contract")


def test_complete_inventory_count_mismatch_is_not_accepted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    payload = _details_payload()
    payload["inventoryTableCount"] = 2
    upstream = FakeUpstream([_response(payload)])
    store = KnowledgeResearchStore(workspace=workspace, media_root=media_root)
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)

    result = _call(
        bridge,
        "getFileDetails",
        {"researchId": research_id, "fileId": FILE_ID},
    )

    assert result["isError"] is True
    snapshot = store.snapshot(research_id)
    assert snapshot["ledger"]["inventories"] == {}
    assert snapshot["ledger"]["calls"][0]["verificationStatus"] == (
        "unverified_inventory_count_mismatch"
    )


def test_local_screenshot_must_stay_inside_explicit_media_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    upstream = FakeUpstream([_response(_table_payload(outside))])
    store = KnowledgeResearchStore(workspace=workspace, media_root=media_root)
    bridge = KnowledgeResearchBridge(upstream, store)
    research_id = _begin(bridge)

    result = _call(
        bridge,
        "getTable",
        {"researchId": research_id, "fileId": FILE_ID, "tableId": TABLE_ID},
    )

    assert result["isError"] is True
    snapshot = store.snapshot(research_id)
    assert snapshot["ledger"]["tables"] == {}
    assert snapshot["ledger"]["calls"][0]["verificationStatus"] == ("unverified_screenshot_path")


def test_tools_list_and_direct_script_entrypoint_are_full_v9_compatible(
    tmp_path: Path,
) -> None:
    store = KnowledgeResearchStore(
        workspace=tmp_path,
        media_root=tmp_path,
    )
    bridge = KnowledgeResearchBridge(FakeUpstream([]), store)
    response = bridge.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert response is not None
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}
    assert tools["search"]["inputSchema"]["properties"]["researchId"]["type"] == ("string")
    assert {
        "researchBegin",
        "researchAddClaim",
        "researchAddClaims",
        "researchAddTable",
        "researchFinalize",
    }.issubset(tools)

    completed = subprocess.run(
        [sys.executable, "scripts/knowledge_research/bridge.py", "--help"],
        cwd=Path(__file__).parents[2],
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0
    assert "--media-root" in completed.stdout


@pytest.mark.asyncio
async def test_stock_052_stdio_client_can_run_the_sidecar(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    media_root = tmp_path / "media"
    workspace.mkdir()
    media_root.mkdir()
    fake_child = tmp_path / "fake_knowledge_mcp.py"
    fake_child.write_text(
        """import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake-knowledge", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": []}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""",
        encoding="utf-8",
    )
    config = MCPServerConfig(
        name="knowledge-research-test",
        transport="stdio",
        command=sys.executable,
        args=[
            "scripts/knowledge_research/bridge.py",
            "--workspace",
            str(workspace),
            "--media-root",
            str(media_root),
            "--",
            sys.executable,
            str(fake_child),
        ],
    )
    client = MCPStdioClient(config)
    await client.connect()
    try:
        tools = {tool.name for tool in await client.list_tools()}
        assert {
            "researchBegin",
            "researchAddClaim",
            "researchAddTable",
            "researchFinalize",
        }.issubset(tools)
        result = await client.call_tool("researchBegin", {"title": "Local Research"})
        assert result.is_error is False
        payload = json.loads(result.content)
        assert payload["verificationStatus"] == "server_authoritative"
    finally:
        await client.close()
