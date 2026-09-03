from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    for key, value in {
        "OPENSQUILLA_KNOWLEDGE_MCP_UPSTREAM_COMMAND": "/not-executed",
        "OPENSQUILLA_KNOWLEDGE_MCP_BASE_URL": "http://127.0.0.1:1",
        "OPENSQUILLA_KNOWLEDGE_API_KEY": "test-only",
        "OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR": str(tmp_path / "media"),
    }.items():
        monkeypatch.setenv(key, value)
    path = Path(__file__).parents[2] / "scripts/knowledge_research/media_bridge.py"
    spec = importlib.util.spec_from_file_location("research_media_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_inventory_preserves_large_source_text(media: ModuleType) -> None:
    table = {
        "tableId": "t1_" + "a" * 32,
        "text": {"format": "html", "content": '<table><tr><td>"\\\n' * 5000},
        "textTruncated": False,
    }
    payload = {"tables": [table], "inventoryComplete": True}
    assert len(json.dumps(payload).encode()) > 48 * 1024
    assert media._fit_file_details_payload(payload) is payload
    assert payload["tables"][0]["text"] == table["text"]


def test_private_inventory_rejects_instead_of_silently_projecting(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media, "MAX_FILE_DETAILS_PAYLOAD_BYTES", 200)
    payload = {"tables": [{"text": {"content": "x" * 500}}]}
    with pytest.raises(ValueError, match="private collection limit"):
        media._fit_file_details_payload(payload)
    assert len(payload["tables"][0]["text"]["content"]) == 500


def test_private_inventory_keeps_all_pages_and_source_flags(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    tables = [
        {
            "tableId": f"t1_{n:032x}",
            "text": {"format": "html", "content": f"<table>{n}</table>"},
            "textTruncated": n == 0,
        }
        for n in range(78)
    ]
    calls = []

    def post(path: str, args: dict) -> dict:
        calls.append((path, args))
        start = int(args.get("cursor", "0"))
        end = min(start + 20, len(tables))
        return {
            "file": {"fileId": "test-pdf", "revision": "a" * 64},
            "tableExtraction": {"tableCount": len(tables)},
            "tables": tables[start:end],
            "nextCursor": str(end) if end < len(tables) else None,
        }

    monkeypatch.setattr(media, "_knowledge_post_json", post)
    result = media._complete_file_details({"fileId": "test-pdf"})
    assert result["tables"] == tables
    assert result["tables"][0]["textTruncated"] is True
    assert result["inventoryTableCount"] == 78
    assert result["inventoryPageCount"] == 4
    assert result["tableTextProjection"] == "source-preserved"
    assert len(calls) == 4


def test_private_serialization_bounds_the_whole_frame(
    media: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(media, "MAX_STDIO_MESSAGE_BYTES", 1500)
    result = media._tool_success_response({"id": "test"}, {"text": (chr(0x6C49) + '"\\\n') * 700})
    media._write_message(result)
    output = capsys.readouterr().out
    assert len(output.encode("utf-8")) <= 1500
    error = json.loads(output)
    assert error["id"] == "test"
    assert error["result"]["isError"] is True
    assert "no content was truncated" in error["result"]["content"][0]["text"]
    media._write_message(media._tool_success_response({"id": "next"}, {"ok": True}))
    assert json.loads(capsys.readouterr().out)["id"] == "next"
