from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.types import ToolCall
from opensquilla.provider.openai import _build_openai_tool
from opensquilla.tools import registry as tool_registry
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.schema_validation import validate_tool_arguments
from opensquilla.tools.types import CallerKind, ToolContext


def _publication_registry(monkeypatch: pytest.MonkeyPatch) -> ToolRegistry:
    # The development interpreter may have an editable install of another
    # worktree. Execute this checkout's real decorator into an isolated registry
    # instead of copying a schema or inheriting a stale process-global entry.
    source = Path(__file__).resolve().parents[2] / "src/opensquilla/tools/builtin/artifacts.py"
    spec = importlib.util.spec_from_file_location("local_artifact_schema_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    registry = ToolRegistry()
    previous = tool_registry.get_default_registry()
    with monkeypatch.context() as isolated:
        isolated.setattr(tool_registry, "_default_registry", registry)
        spec.loader.exec_module(module)
    assert tool_registry.get_default_registry() is previous
    return registry


@pytest.mark.asyncio
async def test_exported_publish_schema_accepts_null_and_publishes_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    report = b'<html><body><img src="figure.svg"></body></html>'
    (workspace / "report.html").write_bytes(report)
    (workspace / "figure.svg").write_text("<svg/>", encoding="utf-8")
    media = tmp_path / "media"
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(workspace),
        artifact_media_root=str(media),
        artifact_session_id="null-bundle-session",
        session_key="agent:main:webchat:null-bundle-session",
    )
    registry = _publication_registry(monkeypatch)
    definition = registry.to_tool_definitions(ctx)[0]
    schema = _build_openai_tool(definition)["function"]["parameters"]
    arguments = {
        "path": "report.html",
        "name": "report.html",
        "mime": "text/html",
        "bundle": "none",
        "bundle_root": None,
    }
    # Exercise the model-facing schema with every property populated, as in the
    # failing native call, then dispatch through the actual registered tool.
    assert set(arguments) == set(schema["properties"])
    assert (
        validate_tool_arguments(
            arguments,
            properties=schema["properties"],
            required=schema["required"],
        )
        == []
    )
    result = await build_tool_handler(registry, ctx)(
        ToolCall(
            tool_use_id="publish-null-root",
            tool_name=definition.name,
            arguments=arguments,
        )
    )
    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["status"] == "published"
    assert "bundle" not in payload
    assert len(ctx.published_artifacts) == 1
    _, published_path = ArtifactStore(media).resolve_for_download(
        payload["artifact"]["id"], session_id="null-bundle-session"
    )
    assert published_path.read_bytes() == report
    assert len(list(media.rglob("meta.json"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bundle_root", ["", "site"])
async def test_none_bundle_rejects_non_null_root_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle_root: str
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "site").mkdir(parents=True)
    (workspace / "site" / "report.html").write_text("<p>report</p>", encoding="utf-8")
    media = tmp_path / "media"
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(media),
        artifact_session_id="invalid-bundle-session",
        session_key="agent:main:webchat:invalid-bundle-session",
    )
    result = await build_tool_handler(_publication_registry(monkeypatch), ctx)(
        ToolCall(
            tool_use_id="publish-invalid-root",
            tool_name="publish_artifact",
            arguments={
                "path": "site/report.html",
                "bundle": "none",
                "bundle_root": bundle_root,
            },
        )
    )
    assert result.is_error is True
    assert json.loads(result.content)["status"] == "error"
    assert result.artifacts == []
    assert not ctx.published_artifacts
    assert not list(media.rglob("meta.json"))
