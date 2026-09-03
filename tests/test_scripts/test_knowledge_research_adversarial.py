from __future__ import annotations

import copy
import hashlib
import io
import json
import socket
import stat
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from threading import Event
from types import ModuleType
from typing import Any, NoReturn

import pytest
import tomli_w

from scripts.knowledge_research import prepare_candidate as candidate
from scripts.knowledge_research.bridge import KnowledgeResearchBridge, serve
from scripts.knowledge_research.navigation import MAX_FRAME_BYTES, Navigation
from tests.test_scripts import test_knowledge_research_navigation as nav_fixture
from tests.test_scripts.test_knowledge_research_media import media as media


@pytest.fixture(autouse=True)
def _deny_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Independent acceptance tests must never open a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _page(number: int, next_cursor: str | None) -> dict[str, Any]:
    return {
        "contractVersion": "knowledge-vnext/2",
        "file": {
            "fileId": "fixture-file",
            "documentId": "fixture-document",
            "revision": "a" * 64,
        },
        "tableExtraction": {"status": "ready", "tableCount": 2, "policyId": "fixture-v1"},
        "tables": [{"tableId": f"t1_{number:032x}", "fileId": "fixture-file"}],
        "nextCursor": next_cursor,
    }


def _serve_pages(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def post(path: str, arguments: dict[str, Any]) -> dict[str, Any]:
        assert path == "/opensquilla-rag/v2/file-details"
        requests.append(copy.deepcopy(arguments))
        assert len(requests) <= len(pages), "Pagination did not stop within the fixture"
        return copy.deepcopy(pages[len(requests) - 1])

    monkeypatch.setattr(media, "_knowledge_post_json", post)
    return requests


@pytest.mark.parametrize(
    ("field", "value"),
    [("tableCount", 3), ("status", "running"), ("policyId", "fixture-v2")],
)
def test_media_rejects_extraction_change_between_pages(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    pages = [_page(1, "second"), _page(2, None)]
    pages[1]["tableExtraction"][field] = value
    _serve_pages(media, monkeypatch, pages)

    with pytest.raises(ValueError):
        media._complete_file_details({"fileId": "fixture-file"})


def test_media_rejects_contract_change_between_pages(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    pages = [_page(1, "second"), _page(2, None)]
    pages[1]["contractVersion"] = "knowledge-vnext/changed"
    _serve_pages(media, monkeypatch, pages)

    with pytest.raises(ValueError):
        media._complete_file_details({"fileId": "fixture-file"})


@pytest.mark.parametrize("field", ["revision", "documentId"])
def test_media_rejects_source_identity_change_between_pages(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    pages = [_page(1, "second"), _page(2, None)]
    pages[1]["file"][field] = "changed"
    _serve_pages(media, monkeypatch, pages)

    with pytest.raises(ValueError, match="metadata changed"):
        media._complete_file_details({"fileId": "fixture-file"})


@pytest.mark.parametrize("failure", ["duplicate", "cursor_loop", "count_mismatch"])
def test_media_rejects_invalid_inventory_sequences(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    pages = [_page(1, "second"), _page(2, None)]
    if failure == "duplicate":
        pages[1]["tables"] = copy.deepcopy(pages[0]["tables"])
    elif failure == "cursor_loop":
        pages[1]["nextCursor"] = "second"
    else:
        for page in pages:
            page["tableExtraction"]["tableCount"] = 3
    _serve_pages(media, monkeypatch, pages)

    with pytest.raises(ValueError):
        media._complete_file_details({"fileId": "fixture-file"})


def _wire_bytes(media: ModuleType, response: dict[str, Any]) -> int:
    return len((media._encode_message(response) + "\n").encode("utf-8"))


@pytest.mark.parametrize("budget_delta", [-1, 0, 1])
def test_media_whole_wire_boundary_includes_utf8_escaping_and_newline(
    media: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    budget_delta: int,
) -> None:
    text = ("\u4e2d\u65e5\ud55c" + '\\"\n') * 150
    response = {
        "jsonrpc": "2.0",
        "id": "fixture-" + '\\"' * 20,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }
    expected_wire_bytes = _wire_bytes(media, response)
    budget = expected_wire_bytes + budget_delta
    monkeypatch.setattr(media, "MAX_STDIO_MESSAGE_BYTES", budget)

    media._write_message(response)
    wire = capsys.readouterr().out
    actual = json.loads(wire)

    assert wire.endswith("\n") and wire.count("\n") == 1
    assert len(wire.encode("utf-8")) <= budget
    assert actual["id"] == response["id"]
    if budget_delta < 0:
        assert actual["result"]["isError"] is True
    else:
        assert actual == response
        assert len(wire.encode("utf-8")) == expected_wire_bytes


def test_media_drops_only_duplicate_representation_to_fit(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"text": "\u4e2d" * 1200, "textTruncated": False}
    response = media._tool_success_response({"id": "duplicate"}, payload)
    original = copy.deepcopy(response)
    content_only = copy.deepcopy(response)
    content_only["result"].pop("structuredContent")
    budget = _wire_bytes(media, content_only)
    assert _wire_bytes(media, response) > budget
    monkeypatch.setattr(media, "MAX_STDIO_MESSAGE_BYTES", budget)

    media._write_message(response)
    wire = capsys.readouterr().out

    assert len(wire.encode("utf-8")) == budget
    assert json.loads(json.loads(wire)["result"]["content"][0]["text"]) == payload
    assert response == original


@pytest.mark.parametrize("budget_delta", [-1, 0, 1])
def test_media_inventory_limit_includes_metadata_and_utf8(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, budget_delta: int
) -> None:
    payload = {"file": {"title": "\u4e2d" * 200}, "tables": [], "inventoryComplete": True}
    size = len(media._encode_message(payload).encode("utf-8"))
    monkeypatch.setattr(media, "MAX_FILE_DETAILS_PAYLOAD_BYTES", size + budget_delta)
    if budget_delta < 0:
        with pytest.raises(ValueError, match="private collection limit"):
            media._fit_file_details_payload(payload)
    else:
        assert media._fit_file_details_payload(payload) == payload


def test_media_materialization_preserves_long_text_hash_and_source_truncation(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = ("\u4e2d\u65e5\ud55c" + '\\"\n') * 6000 + "end-of-stored-source"
    payload = {
        "tableId": "t1_" + "a" * 32,
        "text": {"content": text, "sha256": hashlib.sha256(text.encode()).hexdigest()},
        "textTruncated": True,
        "screenshot": {"url": "/opensquilla-rag/v2/table-image/fixture"},
    }
    monkeypatch.setattr(
        media,
        "_fetch_screenshot",
        lambda *_: {"localPath": str(media.MEDIA_DIR / "fixture.png")},
    )
    response = media._tool_success_response({"id": 1}, copy.deepcopy(payload))
    actual = media._materialize_response(
        response, {"params": {"arguments": {"tableId": payload["tableId"]}}}
    )
    text_payload = json.loads(actual["result"]["content"][0]["text"])
    for representation in (text_payload, actual["result"]["structuredContent"]):
        assert representation["text"] == payload["text"]
        assert representation["textTruncated"] is True


class _MemoryHTTPResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int, headers: Message, url: str) -> None:
        super().__init__(body)
        self.code = status
        self.msg = "Found" if status == 302 else "OK"
        self.headers = headers
        self.url = url

    def info(self) -> Message:
        return self.headers

    def geturl(self) -> str:
        return self.url


@pytest.mark.parametrize("operation", ["details", "screenshot"])
def test_media_never_follows_off_loopback_redirect_with_service_credential(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    requests: list[tuple[str, str | None]] = []

    def http_open(
        _handler: urllib.request.HTTPHandler, request: urllib.request.Request
    ) -> _MemoryHTTPResponse:
        requests.append((request.full_url, request.get_header("Authorization")))
        headers = Message()
        if urllib.parse.urlsplit(request.full_url).hostname == "127.0.0.1":
            headers["Location"] = "http://off-loopback.invalid/fixture"
            return _MemoryHTTPResponse(b"", 302, headers, request.full_url)
        headers["Content-Type"] = "application/octet-stream"
        body = b"{}" if operation == "details" else b"\x89PNG\r\n\x1a\nfixture-only"
        return _MemoryHTTPResponse(body, 200, headers, request.full_url)

    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", http_open)
    monkeypatch.setattr(urllib.request, "_opener", None)
    try:
        if operation == "details":
            media._knowledge_post_json(
                "/opensquilla-rag/v2/file-details", {"fileId": "fixture-file"}
            )
        else:
            media._fetch_screenshot(
                {"url": "/opensquilla-rag/v2/table-image/fixture"}, "t1_" + "a" * 32
            )
    except (ValueError, RuntimeError, urllib.error.URLError):
        pass

    assert requests, "The synthetic transport was not exercised"
    assert all(urllib.parse.urlsplit(url).hostname == "127.0.0.1" for url, _ in requests), (
        "The private hop followed an off-loopback redirect: " + repr(requests)
    )


@pytest.fixture
def candidate_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, dict[str, Any]]:
    monkeypatch.setattr(candidate, "CANDIDATE_PARENT", tmp_path)
    live = tmp_path / "synthetic-live"
    source = tmp_path / "synthetic-source"
    target = tmp_path / "candidate"
    for name in ("config", "private", "state", "workspace"):
        (live / name).mkdir(parents=True)
    (live / "state/history.json").write_text('{"historical":true}')
    (live / "workspace/report.html").write_text("HISTORIC-REPORT-MUST-NOT-BE-COPIED")
    (live / "private/gateway.env").write_text(
        'OPENSQUILLA_KNOWLEDGE_API_KEY="fixture-not-a-real-key"\n'
        'OPENSQUILLA_AUTH_TOKEN="old-fixture-token"\n'
    )
    code = source / "scripts/knowledge_research"
    canonical = {
        "bridge.py": "# Synthetic fixture only; never executed.\n",
        "media_bridge.py": "# Synthetic fixture only; never executed.\n",
        "skill/SKILL.md": "# Canonical fixture skill\n",
        "workspace/AGENTS.md": "# Canonical fixture bootstrap\n",
        "workspace/TOOLS.md": "# Canonical fixture tools\n",
    }
    for name, content in canonical.items():
        path = code / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    config: dict[str, Any] = {
        "host": "0.0.0.0",
        "port": 19636,
        "workspace_dir": str(live / "workspace"),
        "state_dir": str(live / "state"),
        "tools": {"allow": ["mcp_search", "mcp_searchByIds", "skill_view", "publish_artifact"]},
        "skills": {
            "workspace_dir": str(live / "workspace/skills"),
            "managed_dir": str(live / "managed-skills"),
            "extra_dirs": [str(live / "extra-skills")],
            "allow_bundled": False,
        },
        "llm": {"provider": "fixture-provider", "model": "fixture-model"},
        "squilla_router": {"enabled": True, "tiers": {"c0": {"model": "fixture-model"}}},
        "mcp": {
            "servers": [
                {
                    "name": "knowledge-vnext",
                    "transport": "stdio",
                    "command": "/not-executed-python",
                    "args": ["/not-executed-old-sidecar"],
                    "env": {
                        "OPENSQUILLA_KNOWLEDGE_MCP_BASE_URL": "http://127.0.0.1:1",
                        "OPENSQUILLA_KNOWLEDGE_MCP_UPSTREAM_COMMAND": "/not-executed-upstream",
                        "OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR": str(live / "workspace/media"),
                    },
                }
            ]
        },
    }
    (live / "config/gateway.toml").write_text(tomli_w.dumps(config))
    return live, source, target, config


def test_candidate_isolates_rules_paths_and_state_without_changing_routes(
    candidate_setup: tuple[Path, Path, Path, dict[str, Any]],
) -> None:
    live, source, target, config = candidate_setup
    before = {
        path.relative_to(live): path.read_bytes() for path in live.rglob("*") if path.is_file()
    }
    result = candidate.prepare(live=live, source=source, target=target, port=19637)
    prepared = tomllib.loads((target / "config/gateway.toml").read_text())

    assert result == {
        "target": str(target),
        "config": str(target / "config/gateway.toml"),
        "port": 19637,
    }
    assert prepared["host"] == "127.0.0.1"
    assert prepared["port"] == 19637
    for field in ("llm", "squilla_router"):
        assert prepared[field] == config[field]
    assert prepared["workspace_dir"] == str(target / "workspace")
    assert prepared["state_dir"] == str(target / "state")
    assert prepared["skills"] == {
        "workspace_dir": str(target / "workspace/skills"),
        "managed_dir": str(target / "home/skills"),
        "extra_dirs": [],
        "allow_bundled": False,
    }
    assert prepared["tools"]["allow"] == config["tools"]["allow"] + [
        "mcp_researchNavigate",
        "mcp_researchReadEvidence",
    ]
    server = prepared["mcp"]["servers"][0]
    original_server = config["mcp"]["servers"][0]
    assert server["command"] == original_server["command"]
    assert server["env"] == {
        **original_server["env"],
        "OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR": str(target / "workspace/knowledge-artifacts"),
    }
    code = source / "scripts/knowledge_research"
    assert server["args"] == [
        "-I",
        "-B",
        str(code / "bridge.py"),
        "--workspace",
        str(target / "workspace"),
        "--private-root",
        str(target / "private/research"),
        "--media-root",
        str(target / "workspace/knowledge-artifacts"),
        "--",
        server["command"],
        "-I",
        "-B",
        str(code / "media_bridge.py"),
    ]
    for name in ("AGENTS.md", "TOOLS.md"):
        assert (target / "workspace" / name).read_bytes() == (
            code / "workspace" / name
        ).read_bytes()
    assert (target / "workspace/skills/knowledge-local-research/SKILL.md").read_bytes() == (
        code / "skill/SKILL.md"
    ).read_bytes()
    assert list((target / "state").iterdir()) == []
    assert not (target / "workspace/report.html").exists()
    assert before == {
        path.relative_to(live): path.read_bytes() for path in live.rglob("*") if path.is_file()
    }


def test_candidate_private_environment_and_directory_permissions(
    candidate_setup: tuple[Path, Path, Path, dict[str, Any]],
) -> None:
    live, source, target, _ = candidate_setup
    candidate.prepare(live=live, source=source, target=target, port=19637)
    env_path = target / "private/gateway.env"
    environment = {
        name: json.loads(value)
        for name, value in (line.split("=", 1) for line in env_path.read_text().splitlines())
    }
    assert environment["OPENSQUILLA_AUTH_MODE"] == "token"
    assert environment["OPENSQUILLA_AUTH_TOKEN"] != "old-fixture-token"
    assert len(environment["OPENSQUILLA_AUTH_TOKEN"]) == 64
    assert environment["OPENSQUILLA_KNOWLEDGE_API_KEY"] == "fixture-not-a-real-key"
    for key in (
        "HOME",
        "OPENSQUILLA_HOME",
        "OPENSQUILLA_STATE_DIR",
        "XDG_STATE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    ):
        assert Path(environment[key]).is_relative_to(target)
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    for path in (target, target / "private", target / "state", target / "config"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "failure",
    [
        "multiple_servers",
        "wrong_server",
        "missing_credential",
        "malformed_env",
        "missing_agents",
        "missing_tools",
        "missing_skill",
        "same_port",
        "outside_parent",
    ],
)
def test_candidate_preflight_failure_does_not_create_target(
    candidate_setup: tuple[Path, Path, Path, dict[str, Any]], failure: str
) -> None:
    live, source, target, config = candidate_setup
    port = 19637
    if failure == "multiple_servers":
        config["mcp"]["servers"].append(copy.deepcopy(config["mcp"]["servers"][0]))
    elif failure == "wrong_server":
        config["mcp"]["servers"][0]["name"] = "unrelated"
    elif failure == "missing_credential":
        (live / "private/gateway.env").write_text('UNRELATED="fixture"\n')
    elif failure == "malformed_env":
        (live / "private/gateway.env").write_text('OPENSQUILLA_KNOWLEDGE_API_KEY="unterminated\n')
    elif failure.startswith("missing_"):
        name = {
            "missing_agents": "workspace/AGENTS.md",
            "missing_tools": "workspace/TOOLS.md",
            "missing_skill": "skill/SKILL.md",
        }[failure]
        (source / "scripts/knowledge_research" / name).unlink()
    elif failure == "same_port":
        port = config["port"]
    else:
        target = target.parent.parent / "outside-candidate-parent"
    (live / "config/gateway.toml").write_text(tomli_w.dumps(config))

    with pytest.raises(ValueError):
        candidate.prepare(live=live, source=source, target=target, port=port)
    assert not target.exists()


def test_candidate_rejects_existing_target_without_touching_its_files(
    candidate_setup: tuple[Path, Path, Path, dict[str, Any]],
) -> None:
    live, source, target, _ = candidate_setup
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("existing-candidate")
    with pytest.raises(ValueError, match="new isolated temporary directory"):
        candidate.prepare(live=live, source=source, target=target, port=19637)
    assert sentinel.read_text() == "existing-candidate"
    assert list(target.iterdir()) == [sentinel]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("tableExtraction", "tableCount", 3),
        ("tableExtraction", "status", "running"),
        ("tableExtraction", "policyId", "fixture-v2"),
        (None, "contractVersion", "knowledge-vnext/changed"),
        ("file", "revision", "b" * 64),
        ("file", "documentId", "changed-document"),
    ],
)
def test_nav_fallback_inventory_rejects_page_contract_drift(
    tmp_path: Path, section: str | None, field: str, value: object
) -> None:
    pages = [_page(1, "second"), _page(2, None)]
    changed = pages[1][section] if section else pages[1]
    changed[field] = value
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path, [nav_fixture.result(page) for page in pages]
    )
    reply, _ = nav_fixture.invoke(
        bridge, "getFileDetails", {"researchId": rid, "fileId": "fixture-file"}
    )
    assert "error" in reply, "Mixed page contracts must never become a complete inventory"
    assert len(upstream.calls) == 2
    state = store.snapshot(rid)
    assert state["ledger"]["inventories"] == {}
    assert state["extensions"]["navigation"]["snapshots"] == {}


def test_nav_partial_search_text_cannot_claim_complete_projection(tmp_path: Path) -> None:
    content = '\u4e2d\u65e5\u97d3"\\\n' * 15000
    bridge, store, _, rid = nav_fixture.setup(
        tmp_path, [nav_fixture.result(nav_fixture.search_payload("q", ["source"], content))]
    )
    reply = nav_fixture.discovery(bridge, rid, "q")
    hit = reply["results"][0]
    assert hit["contentTruncatedForTransport"] is True
    assert 0 < len(hit["content"]) < len(content)
    assert reply["projectionComplete"] is False
    assert Navigation(store.snapshot(rid)).progress()["readEvidenceCount"] == 0


def test_nav_committed_request_replays_original_after_later_discovery(tmp_path: Path) -> None:
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path,
        [
            nav_fixture.result(nav_fixture.search_payload("first", ["old-source"])),
            nav_fixture.result(nav_fixture.search_payload("later", ["new-source"])),
        ],
    )
    arguments = {"researchId": rid, "query": "first", "requestKey": "stable-first"}
    first, _ = nav_fixture.invoke(bridge, "search", arguments)
    later = nav_fixture.discovery(bridge, rid, "later")
    assert later["snapshotRef"] != first["snapshotRef"]
    before = store.snapshot(rid)
    reopened = KnowledgeResearchBridge(upstream, store)
    replay, _ = nav_fixture.invoke(reopened, "search", arguments, request_id="\u4e2d" * 80)
    assert replay == first
    assert store.snapshot(rid) == before
    assert len(upstream.calls) == 2


@pytest.mark.parametrize("change", [{"query": "other"}, {"limit": 1}])
def test_nav_request_key_conflict_never_queries_or_mutates(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path, [nav_fixture.result(nav_fixture.search_payload("q", ["source"]))]
    )
    arguments = {"researchId": rid, "query": "q", "requestKey": "stable"}
    nav_fixture.invoke(bridge, "search", arguments)
    before = store.snapshot(rid)
    reply, _ = nav_fixture.invoke(bridge, "search", {**arguments, **change})
    assert reply["details"]["code"] == "REQUEST_KEY_CONFLICT"
    assert store.snapshot(rid) == before
    assert len(upstream.calls) == 1


def test_nav_concurrent_same_request_key_has_one_upstream_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path, [nav_fixture.result(nav_fixture.search_payload("q", ["source"]))]
    )
    entered, release = Event(), Event()
    original = upstream.request

    def blocked(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        entered.set()
        assert release.wait(5), "Fixture upstream was not released"
        return original(method, params)

    monkeypatch.setattr(upstream, "request", blocked)
    arguments = {"researchId": rid, "query": "q", "requestKey": "one-request"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(nav_fixture.invoke, bridge, "search", arguments)
        try:
            assert entered.wait(5)
            uncertain, _ = nav_fixture.invoke(bridge, "search", arguments)
            assert uncertain["details"]["code"] == "IN_DOUBT"
        finally:
            release.set()
        first, _ = future.result(timeout=5)
    before = store.snapshot(rid)
    replay, _ = nav_fixture.invoke(bridge, "search", arguments)
    assert replay == first and "error" not in replay
    assert store.snapshot(rid) == before
    assert len(upstream.calls) == 1


@pytest.mark.parametrize("count", [21, 40, 54])
def test_nav_overlapping_reversed_scopes_group_exact_union_without_query(
    tmp_path: Path, count: int
) -> None:
    files = [f"source-{index:03d}" for index in range(count)]
    batches = [files[start : start + 20] for start in range(0, count, 20)]
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path,
        [
            nav_fixture.result(nav_fixture.search_payload(str(i), batch))
            for i, batch in enumerate(batches)
        ],
    )
    scopes = [nav_fixture.discovery(bridge, rid, str(i))["scopeRef"] for i in range(len(batches))]
    arguments = {"researchId": rid, "query": "focused", "scopeRefs": [*reversed(scopes), scopes[0]]}
    grouped, _ = nav_fixture.invoke(bridge, "searchByIds", arguments)
    assert grouped["queried"] is False and grouped["totalFiles"] == count
    assert [group["fileCount"] for group in grouped["groups"]] == [len(batch) for batch in batches]
    nav = store.snapshot(rid)["extensions"]["navigation"]
    expanded = [
        file_id
        for group in grouped["groups"]
        for file_id in nav["scopes"][group["scopeRef"]]["orderedIds"]
    ]
    assert expanded == files
    before = store.snapshot(rid)
    replay, _ = nav_fixture.invoke(bridge, "searchByIds", {**arguments, "scopeRefs": scopes})
    assert replay == grouped
    assert store.snapshot(rid) == before
    assert len(upstream.calls) == len(batches)


@pytest.mark.parametrize("case", ["cross_research", "wrong_type", "wrong_evidence", "altered"])
def test_nav_evidence_cursor_rejects_wrong_context_without_mutation(
    tmp_path: Path, case: str
) -> None:
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path,
        [
            nav_fixture.result(
                nav_fixture.search_payload("q", ["source-a", "source-b"], "\u4e2d" * 40000)
            )
        ],
    )
    hit = nav_fixture.discovery(bridge, rid, "q")["results"][0]
    arguments = {"researchId": rid, "evidenceRef": hit["evidenceRef"]}
    first, _ = nav_fixture.invoke(bridge, "researchReadEvidence", arguments)
    assert first["nextCursor"]
    arguments["cursor"] = first["nextCursor"]
    method = "researchReadEvidence"
    target_rid = rid
    if case == "cross_research":
        other, _ = nav_fixture.invoke(bridge, "researchBegin", {"title": "Other"})
        target_rid = arguments["researchId"] = other["researchId"]
    elif case == "wrong_type":
        method = "getFileDetails"
        arguments["fileRef"] = hit["fileRef"]
    elif case == "wrong_evidence":
        refs = store.snapshot(rid)["extensions"]["navigation"]["evidence"]
        arguments["evidenceRef"] = next(
            row["ref"] for row in refs.values() if row["ref"] != hit["evidenceRef"]
        )
    else:
        cursor = arguments["cursor"]
        arguments["cursor"] = cursor[:-1] + ("0" if cursor[-1] != "0" else "1")
    before = store.snapshot(target_rid)
    rejected, _ = nav_fixture.invoke(bridge, method, arguments)
    assert rejected["details"]["code"] in {"INVALID_CURSOR", "REFERENCE_MISMATCH"}
    assert store.snapshot(target_rid) == before
    assert len(upstream.calls) == 1


def test_nav_file_revision_change_does_not_replace_cursor_source(tmp_path: Path) -> None:
    content = "\u4e2d" * 40000
    first_source = nav_fixture.search_payload("first", ["source"], content)
    changed_source = nav_fixture.search_payload("changed", ["source"], "new source text")
    changed_source["results"][0].update(evidenceId="new-evidence", revision="b" * 64)
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path, [nav_fixture.result(first_source), nav_fixture.result(changed_source)]
    )
    hit = nav_fixture.discovery(bridge, rid, "first")["results"][0]
    args = {"researchId": rid, "evidenceRef": hit["evidenceRef"]}
    page, _ = nav_fixture.invoke(bridge, "researchReadEvidence", args)
    parts = [page["content"]]
    rejected, _ = nav_fixture.invoke(bridge, "search", {"researchId": rid, "query": "changed"})
    assert "error" in rejected
    while page["nextCursor"]:
        page, _ = nav_fixture.invoke(
            bridge, "researchReadEvidence", {**args, "cursor": page["nextCursor"]}
        )
        assert page["sourceRevision"] == nav_fixture.REVISION
        parts.append(page["content"])
    assert "".join(parts) == content
    assert "new-evidence" not in store.snapshot(rid)["ledger"]["evidence"]
    assert len(upstream.calls) == 2


def test_nav_78_cjk_table_previews_fit_actual_envelope_and_preserve_inventory(
    tmp_path: Path,
) -> None:
    details = _page(1, None)
    details["tables"] = [
        {
            "tableId": f"t1_{index:032x}",
            "fileId": "fixture-file",
            "page": index + 1,
            "text": {
                "format": "html",
                "content": "<table><tr><th>"
                + '\u4e2d"\\' * 320
                + "</th></tr><tr><td>17</td></tr></table>",
                "truncated": False,
            },
        }
        for index in range(78)
    ]
    details["tableExtraction"]["tableCount"] = 78
    details.update(inventoryComplete=True, inventoryPageCount=4, inventoryTableCount=78)
    bridge, store, upstream, rid = nav_fixture.setup(tmp_path, [nav_fixture.result(details)])
    arguments = {"researchId": rid, "fileId": "fixture-file"}
    refs: list[str] = []
    cursor = None
    while True:
        reply, _ = nav_fixture.invoke(
            bridge,
            "getFileDetails",
            {**arguments, **({"cursor": cursor} if cursor else {})},
            request_id="\u4e2d" * 80,
        )
        assert "error" not in reply, reply
        assert reply["extractedInventoryComplete"] is True
        assert reply["projectionComplete"] is False
        assert reply["page"]["start"] == len(refs)
        refs.extend(table["tableRef"] for table in reply["tables"])
        cursor = reply["nextCursor"]
        if cursor is None:
            break
    assert len(refs) == len(set(refs)) == 78
    assert len(store.snapshot(rid)["ledger"]["inventories"]["fixture-file"]["tableIds"]) == 78
    assert len(upstream.calls) == 1


def test_nav_oversize_projection_retains_snapshot_not_false_prepared_count_and_continues(
    tmp_path: Path,
) -> None:
    too_large = nav_fixture.search_payload("\u4e2d" * 50000, ["oversize-source"])
    bridge, store, upstream, rid = nav_fixture.setup(
        tmp_path,
        [
            nav_fixture.result(too_large),
            nav_fixture.result(nav_fixture.search_payload("next", ["next-source"])),
        ],
    )
    requests = [
        {
            "jsonrpc": "2.0",
            "id": index,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"researchId": rid, "query": query}},
        }
        for index, query in enumerate(["oversize", "next"])
    ]
    source = io.BytesIO(b"".join((json.dumps(request) + "\n").encode() for request in requests))
    target = io.BytesIO()
    serve(bridge, input_stream=source, output_stream=target)
    lines = target.getvalue().splitlines(keepends=True)
    assert len(lines) == 2 and all(len(line) <= MAX_FRAME_BYTES for line in lines)
    replies = [json.loads(json.loads(line)["result"]["content"][0]["text"]) for line in lines]
    assert replies[0]["details"]["code"] == "PROJECTION_UNAVAILABLE"
    assert "error" not in replies[1]
    state = store.snapshot(rid)
    nav = Navigation(state)
    assert len(state["ledger"]["evidence"]) == len(nav.data["snapshots"]) == 2
    assert len(nav.data["projections"]) == nav.progress()["readEvidenceCount"] == 1
    assert nav.progress()["modelDelivery"] == "unknown"
    assert len(upstream.calls) == 2


@pytest.mark.parametrize("method", ["search", "researchAddClaims", "researchFinalize"])
def test_nav_model_binding_cannot_authorize_read_or_write(tmp_path: Path, method: str) -> None:
    _, store, upstream, _ = nav_fixture.setup(tmp_path)
    owner = KnowledgeResearchBridge(upstream, store, caller_binding=lambda: "trusted-owner")
    begin, _ = nav_fixture.invoke(owner, "researchBegin", {"title": "Bound research"})
    rid = begin["researchId"]
    before = store.snapshot(rid)
    stranger = KnowledgeResearchBridge(upstream, store)
    rejected, _ = nav_fixture.invoke(
        stranger,
        method,
        {"researchId": rid, "callerBinding": "trusted-owner", "query": "q", "claims": []},
    )
    assert rejected["details"]["code"] == "RESEARCH_NOT_FOUND"
    assert store.snapshot(rid) == before and upstream.calls == []


def test_nav_on_commit_failure_rolls_back_snapshot_scope_and_ledger_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge, store, _, rid = nav_fixture.setup(
        tmp_path, [nav_fixture.result(nav_fixture.search_payload("q", ["source"]))]
    )
    original = Navigation.observe

    def fail_after_observe(self: Navigation, tool: str, payload: Mapping[str, Any]) -> str:
        original(self, tool, payload)
        raise RuntimeError("fixture callback failure")

    monkeypatch.setattr(Navigation, "observe", fail_after_observe)
    arguments = {"researchId": rid, "query": "q", "requestKey": "failed-commit"}
    failed, _ = nav_fixture.invoke(bridge, "search", arguments)
    assert "error" in failed
    state = store.snapshot(rid)
    nav = state["extensions"]["navigation"]
    for bucket in ("snapshots", "files", "evidence", "scopes", "projections", "coverage"):
        assert nav[bucket] == {}
    assert state["ledger"]["evidence"] == {} and state["ledger"]["calls"] == []
    assert [row["status"] for row in nav["requests"].values()] == ["in_doubt"]
    replay, _ = nav_fixture.invoke(bridge, "search", arguments)
    assert replay["details"]["code"] == "IN_DOUBT"
