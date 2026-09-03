from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opensquilla.mcp.stdio import MCPStdioClient
from opensquilla.mcp.types import MCPServerConfig
from scripts.knowledge_research.state import KnowledgeResearchStore
from tests.test_scripts.test_knowledge_research_sidecar import (
    FILE_ID,
    PNG,
    _details_payload,
    _search_payload,
    _table_payload,
)

CHILD = """import json
import sys

data = json.load(open(sys.argv[1]))
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": []}
    else:
        params = message["params"]
        payload = data[params["name"]]
        result = {"content": [{"type": "text", "text": json.dumps(payload)}],
                  "structuredContent": payload, "isError": False}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


async def _call(client: MCPStdioClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    return json.loads(result.content)  # type: ignore[no-any-return]


@pytest.mark.parametrize("real_pdf", [False, True])
async def test_stock_stdio_restart_and_full_workflow(tmp_path: Path, real_pdf: bool) -> None:
    runtime = os.environ.get("KNOWLEDGE_TEST_RUNTIME_PYTHON")
    if real_pdf and not runtime:
        pytest.skip("Set KNOWLEDGE_TEST_RUNTIME_PYTHON to an existing WeasyPrint runtime")
    interpreter = str(runtime) if real_pdf else sys.executable
    workspace = tmp_path / "workspace"
    media = tmp_path / "media"
    workspace.mkdir()
    media.mkdir()
    screenshot = media / "original.png"
    screenshot.write_bytes(PNG)
    source = _search_payload()
    source["query"] = "q"
    content = '\u4e2d\u6587 "quoted" \\ 58.8%\n' * 8_000
    source["results"][0]["content"] = content
    details = _details_payload()
    table = _table_payload(screenshot)
    details["tables"][0]["text"] = table["text"]
    fixture = tmp_path / "responses.json"
    fixture.write_text(
        json.dumps(
            {
                "search": {**source, "selectionStrategy": "hierarchical_interleave"},
                "searchByIds": source,
                "getFileDetails": details,
                "getTable": table,
            }
        ),
        encoding="utf-8",
    )
    child = tmp_path / "upstream.py"
    child.write_text(CHILD)
    bridge = Path(__file__).resolve().parents[2] / "scripts/knowledge_research/bridge.py"
    config = MCPServerConfig(
        name="runtime-fixture",
        transport="stdio",
        command=interpreter,
        args=[
            "-I",
            "-B",
            str(bridge),
            "--workspace",
            str(workspace),
            "--media-root",
            str(media),
            "--",
            interpreter,
            "-I",
            "-B",
            str(child),
            str(fixture),
        ],
    )
    client = MCPStdioClient(config)
    await client.connect()
    try:
        missing = await client.call_tool("search", {"query": "q"})
        assert missing.is_error
        begin = await _call(
            client, "researchBegin", {"title": "\u4e2d\u6587\u7814\u7a76\u9a8c\u8bc1"}
        )
        rid = begin["researchId"]
        common = {"researchId": rid}
        discovery = await _call(client, "search", {**common, "query": "q", "requestKey": "q1"})
        hit = discovery["results"][0]
        assert hit["contentTruncatedForTransport"]
        assert content.startswith(hit["content"])
    finally:
        await client.close()

    client = MCPStdioClient(config)
    await client.connect()
    try:
        replay = await _call(client, "search", {**common, "query": "q", "requestKey": "q1"})
        assert replay == discovery
        chunks = []
        cursor = None
        while True:
            page = await _call(
                client,
                "researchReadEvidence",
                {
                    **common,
                    "evidenceRef": hit["evidenceRef"],
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            chunks.append(page["content"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
        assert "".join(chunks) == content
        scoped = await _call(
            client,
            "searchByIds",
            {
                **common,
                "query": "q",
                "scopeRefs": [discovery["scopeRef"]],
            },
        )
        assert scoped["results"][0]["fileRef"] == hit["fileRef"]
        inventory = await _call(client, "getFileDetails", {**common, "fileRef": hit["fileRef"]})
        table_ref = inventory["tables"][0]["tableRef"]
        fetched = await _call(
            client, "getTable", {**common, "fileRef": hit["fileRef"], "tableRef": table_ref}
        )
        assert fetched["quality"]["visualCheck"] == "not_performed"
        batch = {
            **common,
            "batchKey": "p1",
            "claims": [
                {
                    "claimKey": "summary",
                    "section": "\u6458\u8981",
                    "text": (
                        "\u4e2d\u6587\u8bc1\u636e\u4e0e\u539f\u59cb"
                        "\u8868\u683c\u622a\u56fe\u9a8c\u8bc1\u3002"
                    ),
                    "evidenceRefs": [hit["evidenceRef"]],
                }
            ],
        }
        receipt = await _call(client, "researchAddClaims", batch)
        assert await _call(client, "researchAddClaims", batch) == receipt
        table_arguments = {
            **common,
            "tableRef": table_ref,
            "section": "\u6458\u8981",
            "caption": "\u539f\u59cb\u8868\u683c",
        }
        added = await _call(client, "researchAddTable", table_arguments)
        assert await _call(client, "researchAddTable", table_arguments) == added
        recovery = await _call(client, "researchNavigate", {**common, "view": "report"})
        assert len(recovery["entries"]) == 2
        if not real_pdf:
            return
        finalized = await _call(client, "researchFinalize", common)
        assert await _call(client, "researchFinalize", common) == finalized
        artifacts = {item["name"]: item for item in finalized["publicArtifactManifest"]["files"]}
        assert set(artifacts) == {"report.html", "report.pdf", "provenance.json"}
        for item in artifacts.values():
            payload = (workspace / item["path"]).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]
        html = (workspace / artifacts["report.html"]["path"]).read_text()
        assert "data:image/png;base64," in html and FILE_ID not in html
        assert hit["evidenceRef"] not in html and table_ref not in html
        pdf = workspace / artifacts["report.pdf"]["path"]
        extracted = subprocess.check_output(["pdftotext", str(pdf), "-"], text=True)
        assert "\u4e2d\u6587\u7814\u7a76\u9a8c\u8bc1" in extracted
        assert "\u539f\u59cb\u8868\u683c" in extracted
        assert "KOSPI" not in extracted  # Parsed table is HTML-only; PDF uses the crop.
        assert hit["evidenceRef"] not in extracted and FILE_ID not in extracted
        fonts = subprocess.check_output(["pdffonts", str(pdf)], text=True)
        assert "Noto" in fonts and "yes" in fonts
        images = subprocess.check_output(["pdfimages", "-list", str(pdf)], text=True)
        assert "image" in images.splitlines()[-1]
        snapshot = KnowledgeResearchStore(workspace=workspace, media_root=media).snapshot(rid)
        assert len(snapshot["report"]["items"]) == 2
    finally:
        await client.close()
