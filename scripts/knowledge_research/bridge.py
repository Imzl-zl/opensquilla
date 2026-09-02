"""MCP proxy adding authoritative research state around Knowledge tools."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol

if __package__:
    from .state import (
        KnowledgeResearchStore,
        ResearchStateError,
        canonical_json,
        recover_structured_content,
    )
else:  # pragma: no cover - exercised by deployment entrypoint smoke tests
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from state import (  # type: ignore[import-not-found,no-redef]
        KnowledgeResearchStore,
        ResearchStateError,
        canonical_json,
        recover_structured_content,
    )

_LOCAL_TOOLS = frozenset(
    {
        "researchBegin",
        "researchAddClaim",
        "researchAddClaims",
        "researchAddTable",
        "researchFinalize",
    }
)
_LEDGER_TOOLS = frozenset({"search", "searchByIds", "getFileDetails", "getTable"})
_MAX_DETAIL_PAGES = 250
_MODEL_DISCOVERY_CONTENT_CHARS = 800
_MODEL_FOCUSED_CONTENT_CHARS = 1_200
_MODEL_DETAIL_PREVIEW_CHARS = 320
_MODEL_TABLE_CONTENT_CHARS = 20_000


class Upstream(Protocol):
    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]: ...

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None: ...

    def close(self) -> None: ...


class SubprocessUpstream:
    """Sequential newline-delimited JSON-RPC client for a Knowledge MCP child."""

    def __init__(self, argv: Sequence[str]) -> None:
        if not argv:
            raise ValueError("an upstream MCP command is required")
        self.process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
        )
        self.sequence = 0

    def request(self, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        request_id = f"proxy-{self.sequence}"
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)
        while True:
            response = self._read()
            if response.get("id") == request_id:
                return response

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._write(payload)

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)

    def _write(self, payload: Mapping[str, Any]) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise RuntimeError("upstream Knowledge MCP process is unavailable")
        self.process.stdin.write((canonical_json(payload) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("upstream Knowledge MCP stdout is unavailable")
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("upstream Knowledge MCP process closed")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload


class KnowledgeResearchBridge:
    def __init__(self, upstream: Upstream, store: KnowledgeResearchStore) -> None:
        self.upstream = upstream
        self.store = store

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, Mapping) else None
        if request_id is None:
            if isinstance(method, str):
                self.upstream.notify(method, params)
            return None
        try:
            if method == "tools/list":
                response = self.upstream.request("tools/list", params)
                result = response.get("result")
                if not isinstance(result, Mapping):
                    return _with_id(response, request_id)
                augmented = copy.deepcopy(dict(result))
                augmented["tools"] = self._augment_tools(augmented.get("tools"))
                return _success(request_id, augmented)
            if method == "tools/call":
                return self._call_tool(request_id, params)
            if not isinstance(method, str):
                return _rpc_error(request_id, -32600, "Invalid Request")
            return _with_id(self.upstream.request(method, params), request_id)
        except ResearchStateError as exc:
            if method == "tools/call":
                return _success(request_id, _tool_result({"error": str(exc)}, is_error=True))
            return _rpc_error(request_id, -32602, str(exc))
        except (OSError, RuntimeError, ValueError) as exc:
            return _rpc_error(request_id, -32603, f"Knowledge research bridge failed: {exc}")

    def close(self) -> None:
        self.upstream.close()

    def _call_tool(self, request_id: Any, params: Mapping[str, Any] | None) -> dict[str, Any]:
        if params is None:
            return _rpc_error(request_id, -32602, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return _rpc_error(request_id, -32602, "tool name and arguments are required")
        if name in _LOCAL_TOOLS:
            return _success(request_id, self._call_local(name, arguments))

        research_id = arguments.get("researchId")
        forwarded = {key: value for key, value in arguments.items() if key != "researchId"}
        if research_id is None or name not in _LEDGER_TOOLS:
            return _with_id(
                self.upstream.request("tools/call", {"name": name, "arguments": forwarded}),
                request_id,
            )
        if not isinstance(research_id, str) or not self.store.exists(research_id):
            raise ResearchStateError("researchId was not found")
        if name == "getFileDetails" and (
            forwarded.get("cursor") is None or forwarded.get("cursor") == ""
        ):
            result = self._auto_paginate_details(research_id, forwarded)
            return _success(request_id, result)
        if name == "getTable":
            forwarded["includeScreenshot"] = True
        response = self._upstream_tool(name, forwarded)
        upstream_result = response.get("result")
        if isinstance(upstream_result, Mapping):
            call = self.store.record_knowledge_call(
                research_id=research_id,
                tool_name=name,
                arguments=forwarded,
                result=upstream_result,
            )
            if (
                not bool(upstream_result.get("isError"))
                and call["verificationStatus"] != "verified"
            ):
                return _success(
                    request_id,
                    _tool_result(
                        {
                            "error": "Knowledge result was not accepted by the evidence ledger",
                            "verificationStatus": call["verificationStatus"],
                        },
                        is_error=True,
                    ),
                )
            if not bool(upstream_result.get("isError")):
                structured, _ = recover_structured_content(upstream_result)
                if structured is not None:
                    return _success(
                        request_id,
                        _model_result(name, structured),
                    )
        else:
            error = response.get("error")
            if isinstance(error, Mapping):
                self.store.record_knowledge_error(
                    research_id=research_id,
                    tool_name=name,
                    arguments=forwarded,
                    error=error,
                )
        return _with_id(response, request_id)

    def _call_local(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "researchBegin":
            payload = self.store.begin(
                title=_text_argument(arguments, "title"),
                subtitle=_optional_text_argument(arguments, "subtitle"),
            )
        elif name == "researchAddClaim":
            payload = self.store.add_claim(
                research_id=_text_argument(arguments, "researchId"),
                section=_text_argument(arguments, "section"),
                text=_text_argument(arguments, "text"),
                evidence_ids=arguments.get("evidenceIds", []),
            )
        elif name == "researchAddClaims":
            payload = self.store.add_claims(
                research_id=_text_argument(arguments, "researchId"),
                claims=arguments.get("claims", []),
            )
        elif name == "researchAddTable":
            payload = self.store.add_table(
                research_id=_text_argument(arguments, "researchId"),
                section=_text_argument(arguments, "section"),
                table_id=_text_argument(arguments, "tableId"),
                caption=_text_argument(arguments, "caption"),
            )
        else:
            payload = self.store.finalize(research_id=_text_argument(arguments, "researchId"))
        return _tool_result(payload)

    def _auto_paginate_details(
        self, research_id: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        cursor: str | None = None
        seen: set[str] = set()
        tables: list[Any] = []
        first_result: dict[str, Any] | None = None
        first_structured: dict[str, Any] | None = None
        page_count = 0
        while page_count < _MAX_DETAIL_PAGES:
            page_arguments = dict(arguments)
            page_arguments["limit"] = 20
            if cursor is None:
                page_arguments.pop("cursor", None)
            else:
                page_arguments["cursor"] = cursor
            response = self._upstream_tool("getFileDetails", page_arguments)
            if "error" in response:
                error = response["error"]
                error_record = error if isinstance(error, Mapping) else {"message": str(error)}
                self.store.record_knowledge_error(
                    research_id=research_id,
                    tool_name="getFileDetails",
                    arguments=page_arguments,
                    error=error_record,
                )
                return _tool_result(response["error"], is_error=True)
            raw_result = response.get("result")
            if not isinstance(raw_result, Mapping):
                return _tool_result(
                    {"error": "Knowledge getFileDetails returned an invalid result"},
                    is_error=True,
                )
            call = self.store.record_knowledge_call(
                research_id=research_id,
                tool_name="getFileDetails",
                arguments=page_arguments,
                result=raw_result,
            )
            if bool(raw_result.get("isError")):
                return dict(raw_result)
            if call["verificationStatus"] != "verified":
                return _tool_result(
                    {"error": "Knowledge table inventory could not be verified"},
                    is_error=True,
                )
            structured, _ = recover_structured_content(raw_result)
            if structured is None:
                return _tool_result(
                    {"error": "Knowledge table inventory has no structured content"},
                    is_error=True,
                )
            page_count += 1
            if page_count == 1 and structured.get("inventoryComplete") is True:
                return _model_result("getFileDetails", structured)
            tables.extend(structured.get("tables", []))
            if first_result is None:
                first_result = copy.deepcopy(dict(raw_result))
                first_structured = copy.deepcopy(dict(structured))
            next_cursor = structured.get("nextCursor")
            if next_cursor is None or next_cursor == "":
                assert first_result is not None and first_structured is not None
                first_structured["tables"] = tables
                first_structured["nextCursor"] = None
                first_structured["inventoryComplete"] = True
                first_structured["inventoryPageCount"] = page_count
                first_structured["inventoryTableCount"] = len(tables)
                return _model_result("getFileDetails", first_structured)
            if not isinstance(next_cursor, str) or next_cursor in seen:
                return _tool_result(
                    {"error": "Knowledge table inventory cursor is invalid or repeated"},
                    is_error=True,
                )
            seen.add(next_cursor)
            cursor = next_cursor
        return _tool_result(
            {"error": f"Knowledge table inventory exceeded {_MAX_DETAIL_PAGES} pages"},
            is_error=True,
        )

    def _upstream_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.upstream.request("tools/call", {"name": name, "arguments": dict(arguments)})

    @staticmethod
    def _augment_tools(raw_tools: Any) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if isinstance(raw_tools, list):
            for raw in raw_tools:
                if not isinstance(raw, Mapping):
                    continue
                tool = copy.deepcopy(dict(raw))
                if tool.get("name") in _LEDGER_TOOLS:
                    schema = tool.get("inputSchema")
                    if not isinstance(schema, dict):
                        schema = {"type": "object"}
                        tool["inputSchema"] = schema
                    properties = schema.get("properties")
                    if not isinstance(properties, dict):
                        properties = {}
                        schema["properties"] = properties
                    properties["researchId"] = {
                        "type": "string",
                        "pattern": "^kr_[0-9a-f]{32}$",
                        "description": (
                            "Opaque ID from researchBegin. Include it so the server can "
                            "verify evidence used by the final report."
                        ),
                    }
                tools.append(tool)
        tools.extend(_research_tools())
        return tools


def _research_tools() -> list[dict[str, Any]]:
    research_id = {
        "type": "string",
        "pattern": "^kr_[0-9a-f]{32}$",
        "description": "Copy exactly from researchBegin.",
    }
    return [
        {
            "name": "researchBegin",
            "description": "Begin an authoritative local-Knowledge report assembly.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title"],
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "subtitle": {"type": "string", "minLength": 1},
                },
            },
        },
        {
            "name": "researchAddClaim",
            "description": (
                "Add one report paragraph backed by exact evidence IDs observed by this bridge."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["researchId", "section", "text", "evidenceIds"],
                "properties": {
                    "researchId": research_id,
                    "section": {"type": "string", "minLength": 1},
                    "text": {"type": "string", "minLength": 1},
                    "evidenceIds": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        {
            "name": "researchAddClaims",
            "description": (
                "Atomically add all report paragraphs in reading order with verified evidence. "
                "Prefer this batch tool over repeated researchAddClaim calls."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["researchId", "claims"],
                "properties": {
                    "researchId": research_id,
                    "claims": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 40,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["section", "text", "evidenceIds"],
                            "properties": {
                                "section": {"type": "string", "minLength": 1},
                                "text": {"type": "string", "minLength": 1},
                                "evidenceIds": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {"type": "string", "minLength": 1},
                                },
                            },
                        },
                    },
                },
            },
        },
        {
            "name": "researchAddTable",
            "description": (
                "Add a table whose inventory, canonical text, and original crop were verified."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["researchId", "section", "tableId", "caption"],
                "properties": {
                    "researchId": research_id,
                    "section": {"type": "string", "minLength": 1},
                    "tableId": {"type": "string", "minLength": 1},
                    "caption": {"type": "string", "minLength": 1},
                },
            },
        },
        {
            "name": "researchFinalize",
            "description": (
                "Verify and render report.html, report.pdf, and provenance.json, then return "
                "the only allowed public publish manifest."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["researchId"],
                "properties": {"researchId": research_id},
            },
        },
    ]


def _text_argument(
    arguments: Mapping[str, Any],
    name: str,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ResearchStateError(f"{name} must be a non-empty string")
    return value


def _optional_text_argument(arguments: Mapping[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ResearchStateError(f"{name} must be a non-empty string")
    return value


def _tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    structured = json.loads(canonical_json(payload))
    return {
        "content": [{"type": "text", "text": canonical_json(structured)}],
        "structuredContent": structured,
        "isError": is_error,
    }


def _model_result(name: str, structured: Mapping[str, Any]) -> dict[str, Any]:
    projected = copy.deepcopy(dict(structured))
    if name in {"search", "searchByIds"}:
        results = projected.get("results")
        if isinstance(results, list):
            content_limit = (
                _MODEL_FOCUSED_CONTENT_CHARS
                if name == "searchByIds"
                else _MODEL_DISCOVERY_CONTENT_CHARS
            )
            projected["results"] = [
                _model_search_item(item, content_limit=content_limit)
                if isinstance(item, Mapping)
                else item
                for item in results
            ]
    elif name == "getFileDetails":
        projected = _model_file_details(structured)
    elif name == "getTable":
        projected = _model_table(structured)
        text = projected.get("text")
        if isinstance(text, dict):
            content = text.get("content")
            if isinstance(content, str) and len(content) > _MODEL_TABLE_CONTENT_CHARS:
                text["content"] = content[:_MODEL_TABLE_CONTENT_CHARS]
                text["contentTruncatedForTransport"] = True
                text["fullContentStoredInLedger"] = True
        projected.pop("screenshotDataBase64", None)
        screenshot = projected.get("screenshot")
        if isinstance(screenshot, dict):
            screenshot.pop("dataBase64", None)
    return {
        "content": [{"type": "text", "text": canonical_json(projected)}],
        "isError": False,
    }


def _model_search_item(item: Mapping[str, Any], *, content_limit: int) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(item[key])
        for key in ("evidenceId", "fileId", "title", "contentKind", "content")
        if key in item
    }
    locator = item.get("locator")
    if isinstance(locator, Mapping):
        projected["locator"] = {
            key: copy.deepcopy(locator[key])
            for key in ("title", "sectionPath", "pageStart", "pageEnd")
            if key in locator
        }
    content = projected.get("content")
    if isinstance(content, str) and len(content) > content_limit:
        projected["content"] = content[:content_limit]
        projected["contentTruncatedForTransport"] = True
    return projected


def _model_file_details(structured: Mapping[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {
        key: copy.deepcopy(structured[key])
        for key in (
            "contractVersion",
            "inventoryComplete",
            "inventoryPageCount",
            "inventoryTableCount",
            "nextCursor",
        )
        if key in structured
    }
    source_file = structured.get("file")
    if isinstance(source_file, Mapping):
        projected["file"] = {
            key: copy.deepcopy(source_file[key])
            for key in ("fileId", "title", "filename", "mediaType")
            if key in source_file
        }
    extraction = structured.get("tableExtraction")
    if isinstance(extraction, Mapping):
        projected["tableExtraction"] = {
            key: copy.deepcopy(extraction[key])
            for key in ("status", "tableCount", "policyId")
            if key in extraction
        }
    tables = structured.get("tables")
    if isinstance(tables, list):
        projected["tables"] = [
            _model_table_inventory_item(table) for table in tables if isinstance(table, Mapping)
        ]
    projected["tableTextProjection"] = "compact-preview"
    return projected


def _model_table_inventory_item(table: Mapping[str, Any]) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(table[key])
        for key in (
            "tableId",
            "page",
            "ordinal",
            "continuationOf",
            "screenshotAvailable",
            "textAvailable",
            "textFormat",
        )
        if key in table
    }
    preview = table.get("textPreview")
    if not isinstance(preview, str):
        text = table.get("text")
        if isinstance(text, Mapping) and isinstance(text.get("content"), str):
            preview = text["content"]
    if isinstance(preview, str) and preview:
        projected["textPreview"] = preview[:_MODEL_DETAIL_PREVIEW_CHARS]
        if len(preview) > _MODEL_DETAIL_PREVIEW_CHARS:
            projected["textPreviewTruncatedForTransport"] = True
    return projected


def _model_table(structured: Mapping[str, Any]) -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(structured[key])
        for key in ("schemaVersion", "tableId", "fileId", "page", "locator", "text")
        if key in structured
    }
    screenshot = structured.get("screenshot")
    if isinstance(screenshot, Mapping):
        projected["screenshot"] = {
            key: copy.deepcopy(screenshot[key])
            for key in ("mediaType", "widthPixels", "heightPixels")
            if key in screenshot
        }
        projected["screenshotAvailable"] = True
    return projected


def _success(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _with_id(response: Mapping[str, Any], request_id: Any) -> dict[str, Any]:
    payload = dict(response)
    payload["id"] = request_id
    payload.setdefault("jsonrpc", "2.0")
    return payload


def _read_frame(stream: BinaryIO) -> tuple[dict[str, Any], bool] | None:
    first = stream.readline()
    if not first:
        return None
    if first.lstrip().startswith(b"{"):
        payload = json.loads(first)
        if not isinstance(payload, dict):
            raise ValueError("MCP message must be an object")
        return payload, False
    content_length: int | None = None
    line = first
    while line not in {b"\r\n", b"\n", b""}:
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            content_length = int(value.strip())
        line = stream.readline()
    if content_length is None or content_length < 0:
        raise ValueError("missing Content-Length")
    body = stream.read(content_length)
    if len(body) != content_length:
        raise ValueError("truncated MCP message")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("MCP message must be an object")
    return payload, True


def _write_frame(stream: BinaryIO, payload: Mapping[str, Any], content_length: bool) -> None:
    encoded = canonical_json(payload).encode("utf-8")
    if content_length:
        stream.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("ascii"))
    stream.write(encoded + (b"" if content_length else b"\n"))
    stream.flush()


def serve(
    bridge: KnowledgeResearchBridge,
    *,
    input_stream: BinaryIO | None = None,
    output_stream: BinaryIO | None = None,
) -> None:
    source = input_stream or sys.stdin.buffer
    target = output_stream or sys.stdout.buffer
    while True:
        use_content_length = False
        try:
            frame = _read_frame(source)
            if frame is None:
                return
            message, use_content_length = frame
            response = bridge.handle(message)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            response = _rpc_error(None, -32700, f"Parse error: {exc}")
        if response is not None:
            _write_frame(target, response, use_content_length)


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=os.environ.get("OPENSQUILLA_KNOWLEDGE_RESEARCH_WORKSPACE"),
    )
    parser.add_argument(
        "--private-root",
        default=os.environ.get("OPENSQUILLA_KNOWLEDGE_RESEARCH_PRIVATE_ROOT"),
    )
    parser.add_argument(
        "--media-root",
        default=os.environ.get("OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR"),
    )
    parser.add_argument("upstream", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace is required")
    if not args.media_root:
        parser.error("--media-root or OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR is required")
    if args.upstream and args.upstream[0] == "--":
        args.upstream = args.upstream[1:]
    if not args.upstream:
        parser.error("upstream MCP command is required after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    upstream = SubprocessUpstream(args.upstream)
    bridge = KnowledgeResearchBridge(
        upstream,
        KnowledgeResearchStore(
            workspace=args.workspace,
            private_root=args.private_root,
            media_root=args.media_root,
        ),
    )
    try:
        serve(bridge)
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
