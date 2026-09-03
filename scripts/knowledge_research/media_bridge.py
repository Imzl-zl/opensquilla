#!/usr/bin/env python3
"""Private Knowledge hop preserving table text and materializing source images."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

MAX_IMAGE_BYTES = 20 * 1024 * 1024
# The outer research bridge applies the smaller model-facing frame budget.
MAX_STDIO_MESSAGE_BYTES = 16 * 1024 * 1024
FILE_DETAILS_PAGE_LIMIT = 20
MAX_FILE_DETAILS_PAGES = 100
MAX_FILE_DETAILS_PAYLOAD_BYTES = 8 * 1024 * 1024
SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
VALID_TABLE_ID = re.compile(r"^(?:t1_[0-9a-f]{32}|tbl2_[0-9a-f]{40})$")


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


UPSTREAM_COMMAND = _required_env("OPENSQUILLA_KNOWLEDGE_MCP_UPSTREAM_COMMAND")
BASE_URL = _required_env("OPENSQUILLA_KNOWLEDGE_MCP_BASE_URL").rstrip("/")
API_KEY = _required_env("OPENSQUILLA_KNOWLEDGE_API_KEY")
MEDIA_DIR = Path(_required_env("OPENSQUILLA_KNOWLEDGE_MCP_MEDIA_DIR")).resolve()


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url, code, "Private hop redirect rejected", headers, fp
        )


def _open_private(request: urllib.request.Request, *, timeout: int) -> Any:
    target = urllib.parse.urlsplit(request.full_url)
    base = urllib.parse.urlsplit(_loopback_base_url())
    if (target.scheme, target.hostname, target.port) != (base.scheme, base.hostname, base.port):
        raise ValueError("Private request must target the configured Knowledge origin")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
    return opener.open(request, timeout=timeout)


def _tool_error_response(message: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "jsonrpc": message.get("jsonrpc", "2.0"),
        "id": message.get("id"),
        "result": {
            "content": [{"type": "text", "text": error}],
            "isError": True,
        },
    }


def _tool_success_response(message: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": message.get("jsonrpc", "2.0"),
        "id": message.get("id"),
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                }
            ],
            "structuredContent": payload,
        },
    }


def _loopback_base_url() -> str:
    parsed_base = urllib.parse.urlsplit(BASE_URL)
    if parsed_base.scheme not in {"http", "https"} or parsed_base.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("Knowledge MCP bridge only permits a loopback base URL")
    return BASE_URL


def _knowledge_post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{_loopback_base_url()}{path}",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with _open_private(request, timeout=30) as response:
            body = response.read(MAX_STDIO_MESSAGE_BYTES + 1)
            if len(body) > MAX_STDIO_MESSAGE_BYTES:
                raise ValueError("Knowledge JSON response exceeds the private hop limit")
            result = json.loads(body)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Knowledge file-details request failed with HTTP {exc.code}") from exc
    if not isinstance(result, dict):
        raise ValueError("Knowledge file-details response must be an object")
    return result


def _fit_file_details_payload(payload: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_FILE_DETAILS_PAYLOAD_BYTES:
        raise ValueError("PDF table inventory exceeds the private collection limit")
    return payload


def _complete_file_details(arguments: dict[str, Any]) -> dict[str, Any]:
    file_id = arguments.get("fileId")
    if not isinstance(file_id, str) or not file_id.strip():
        raise ValueError("getFileDetails requires a non-empty fileId")

    merged: dict[str, Any] | None = None
    all_tables: list[dict[str, Any]] = []
    seen_table_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None
    page_count = 0
    inventory_bytes = 0

    while page_count < MAX_FILE_DETAILS_PAGES:
        request_payload: dict[str, Any] = {
            "fileId": file_id,
            "limit": FILE_DETAILS_PAGE_LIMIT,
        }
        if cursor is not None:
            request_payload["cursor"] = cursor
        page = _knowledge_post_json("/opensquilla-rag/v2/file-details", request_payload)
        page_count += 1

        page_file = page.get("file")
        if not isinstance(page_file, dict) or page_file.get("fileId") != file_id:
            raise ValueError("Knowledge file-details response changed file identity")
        if merged is None:
            merged = {
                key: value for key, value in page.items() if key not in {"tables", "nextCursor"}
            }
        elif page_file != merged.get("file"):
            raise ValueError("Knowledge file metadata changed during pagination")
        elif any(
            page.get(key) != merged.get(key) for key in ("contractVersion", "tableExtraction")
        ):
            raise ValueError("Knowledge extraction metadata changed during pagination")

        page_tables = page.get("tables")
        if not isinstance(page_tables, list):
            raise ValueError("Knowledge file-details response has invalid tables")
        for table in page_tables:
            if not isinstance(table, dict):
                raise ValueError("Knowledge file-details returned an invalid table")
            table_id = table.get("tableId")
            if not isinstance(table_id, str) or not VALID_TABLE_ID.fullmatch(table_id):
                raise ValueError("Knowledge file-details returned an invalid tableId")
            if table_id in seen_table_ids:
                raise ValueError("Knowledge file-details returned a duplicate tableId")
            inventory_bytes += len(_encode_message(table).encode("utf-8"))
            if inventory_bytes > MAX_FILE_DETAILS_PAYLOAD_BYTES:
                raise ValueError("PDF table inventory exceeds the private collection limit")
            seen_table_ids.add(table_id)
            all_tables.append(table)

        next_cursor = page.get("nextCursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ValueError("Knowledge file-details returned an invalid nextCursor")
        if next_cursor in seen_cursors:
            raise ValueError("Knowledge file-details pagination cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ValueError("Knowledge file-details pagination exceeded its safety limit")

    assert merged is not None
    extraction = merged.get("tableExtraction")
    expected_count = extraction.get("tableCount") if isinstance(extraction, dict) else None
    if isinstance(expected_count, int) and expected_count != len(all_tables):
        raise ValueError("Knowledge file-details table count does not match the complete inventory")
    merged["tables"] = all_tables
    merged["nextCursor"] = None
    merged["inventoryComplete"] = True
    merged["inventoryPageCount"] = page_count
    merged["inventoryTableCount"] = len(all_tables)
    merged["tableTextProjection"] = "source-preserved"
    return _fit_file_details_payload(merged)


def _prepare_request(
    message: dict[str, Any],
) -> tuple[dict[str, Any], bool, dict[str, Any] | None]:
    if message.get("method") != "tools/call":
        return message, False, None
    params = message.get("params")
    if not isinstance(params, dict):
        return message, False, None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return message, False, None

    tool_name = params.get("name")
    if tool_name == "getFileDetails":
        try:
            payload = _complete_file_details(arguments)
        except Exception as exc:  # noqa: BLE001 - return a fail-closed tool result.
            return (
                message,
                False,
                _tool_error_response(
                    message,
                    f"Unable to enumerate the complete PDF table inventory: "
                    f"{type(exc).__name__}: {exc}",
                ),
            )
        return message, False, _tool_success_response(message, payload)

    if tool_name != "getTable":
        return message, False, None

    table_id = arguments.get("tableId")
    if not isinstance(table_id, str) or not VALID_TABLE_ID.fullmatch(table_id):
        return (
            message,
            False,
            _tool_error_response(
                message,
                "Invalid tableId. Use an exact tableId returned by a successful "
                "getFileDetails call; table IDs must never be guessed.",
            ),
        )

    include_screenshot = arguments.get("includeScreenshot", True) is not False
    if not include_screenshot:
        return message, False, None

    prepared = dict(message)
    prepared_params = dict(params)
    prepared_arguments = dict(arguments)
    prepared_arguments["includeScreenshot"] = False
    prepared_params["arguments"] = prepared_arguments
    prepared["params"] = prepared_params
    return prepared, True, None


def _fetch_screenshot(screenshot: dict[str, Any], table_id: str) -> dict[str, Any]:
    relative_url = screenshot.get("url")
    if not isinstance(relative_url, str) or not relative_url.startswith(
        "/opensquilla-rag/v2/table-image/"
    ):
        raise ValueError("Knowledge returned an invalid table screenshot URL")

    _loopback_base_url()

    request = urllib.request.Request(
        f"{BASE_URL}{relative_url}",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    with _open_private(request, timeout=20) as response:
        declared_length = response.headers.get("Content-Length")
        if declared_length and int(declared_length) > MAX_IMAGE_BYTES:
            raise ValueError("Knowledge table screenshot exceeds the media bridge limit")
        payload = response.read(MAX_IMAGE_BYTES + 1)
        response_media_type = response.headers.get_content_type()

    if len(payload) > MAX_IMAGE_BYTES:
        raise ValueError("Knowledge table screenshot exceeds the media bridge limit")
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        extension = ".png"
        detected_media_type = "image/png"
    elif payload.startswith(b"\xff\xd8\xff"):
        extension = ".jpg"
        detected_media_type = "image/jpeg"
    elif payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        extension = ".webp"
        detected_media_type = "image/webp"
    else:
        raise ValueError("Knowledge table screenshot is not a supported image")
    if response_media_type not in {detected_media_type, "application/octet-stream"}:
        raise ValueError("Knowledge table screenshot media type does not match its bytes")

    digest = hashlib.sha256(payload).hexdigest()
    expected_digest = screenshot.get("sha256")
    if isinstance(expected_digest, str) and expected_digest and expected_digest != digest:
        raise ValueError("Knowledge table screenshot SHA256 mismatch")

    safe_table_id = SAFE_ID.sub("_", table_id).strip("._") or "table"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target = MEDIA_DIR / f"{safe_table_id}-{digest[:16]}{extension}"
    if not target.exists():
        temporary = MEDIA_DIR / f".{target.name}.{os.getpid()}.tmp"
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)

    return {
        "localPath": str(target),
        "mediaType": detected_media_type,
        "sha256": digest,
        "sizeBytes": len(payload),
    }


def _materialize_response(response: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    content = result.get("content")
    if not isinstance(content, list):
        return response

    arguments = request.get("params", {}).get("arguments", {})
    table_id = str(arguments.get("tableId", "table"))
    materialized: dict[str, Any] | None = None
    materialization_error: str | None = None
    transformed_content: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            transformed_content.append(block)
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            transformed_content.append(block)
            continue
        screenshot = payload.get("screenshot") if isinstance(payload, dict) else None
        if isinstance(screenshot, dict) and materialized is None:
            try:
                materialized = _fetch_screenshot(screenshot, table_id)
                screenshot.update(materialized)
                payload["screenshotLocalPath"] = materialized["localPath"]
            except Exception as exc:  # noqa: BLE001 - return text even if media fails.
                materialization_error = f"{type(exc).__name__}: {exc}"
                payload["screenshotMaterializationError"] = materialization_error
        transformed_content.append(
            {**block, "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}
        )

    result["content"] = transformed_content
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if materialized is not None:
            structured["screenshotLocalPath"] = materialized["localPath"]
        if materialization_error is not None:
            structured["screenshotMaterializationError"] = materialization_error
    return response


def _full_corpus_tool_schema(response: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Expose search as an unscoped operation on this full-corpus gateway."""
    if request.get("method") != "tools/list":
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    tools = result.get("tools")
    if not isinstance(tools, list):
        return response

    normalized_tools: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized_tools.append(tool)
            continue
        normalized_tool = dict(tool)
        tool_name = tool.get("name")
        hidden_properties: set[str] = set()
        if tool_name == "search":
            normalized_tool["description"] = (
                "Search the whole local knowledge base. This tool is always unscoped."
            )
            hidden_properties = {"collectionIds"}
        elif tool_name == "getFileDetails":
            normalized_tool["description"] = (
                "Return one file's complete PDF table inventory. Pagination is "
                "automatic; call once with fileId, then use getTable for any table "
                "whose full text and screenshot are needed."
            )
            hidden_properties = {"cursor", "limit"}
        else:
            normalized_tools.append(tool)
            continue
        schema = tool.get("inputSchema")
        if isinstance(schema, dict):
            normalized_schema = dict(schema)
            properties = schema.get("properties")
            if isinstance(properties, dict):
                normalized_properties = dict(properties)
                for property_name in hidden_properties:
                    normalized_properties.pop(property_name, None)
                normalized_schema["properties"] = normalized_properties
            required = schema.get("required")
            if isinstance(required, list):
                normalized_schema["required"] = [
                    item for item in required if item not in hidden_properties
                ]
            normalized_tool["inputSchema"] = normalized_schema
        normalized_tools.append(normalized_tool)

    result["tools"] = normalized_tools
    return response


def _encode_message(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _compact_duplicate_result(message: dict[str, Any]) -> dict[str, Any]:
    encoded = _encode_message(message)
    if len(encoded.encode("utf-8")) + 1 <= MAX_STDIO_MESSAGE_BYTES:
        return message

    result = message.get("result")
    if not isinstance(result, dict) or "content" not in result:
        return message

    # Knowledge exposes the same payload in MCP content and structuredContent.
    # OpenSquilla consumes content, so omit only the duplicate representation
    # when the newline-framed stdio message would exceed its reader limit.
    compacted_result = dict(result)
    compacted_result.pop("structuredContent", None)
    compacted = dict(message)
    compacted["result"] = compacted_result
    return compacted


def _write_message(message: dict[str, Any]) -> None:
    encoded = _encode_message(_compact_duplicate_result(message))
    if len(encoded.encode("utf-8")) + 1 > MAX_STDIO_MESSAGE_BYTES:
        encoded = _encode_message(
            _tool_error_response(
                {"id": message.get("id")},
                "Knowledge response exceeds the private hop limit; no content was truncated.",
            )
        )
        if len(encoded.encode("utf-8")) + 1 > MAX_STDIO_MESSAGE_BYTES:
            raise ValueError("Knowledge response envelope exceeds the private hop limit")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def main() -> int:
    child = subprocess.Popen(
        [UPSTREAM_COMMAND],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
    )
    assert child.stdin is not None
    assert child.stdout is not None
    try:
        for raw_line in sys.stdin.buffer:
            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            prepared, materialize, local_response = _prepare_request(message)
            if local_response is not None:
                if "id" in message:
                    _write_message(local_response)
                continue
            child.stdin.write((json.dumps(prepared, separators=(",", ":")) + "\n").encode("utf-8"))
            child.stdin.flush()
            if "id" not in message:
                continue

            expected_id = message["id"]
            while True:
                upstream_line = child.stdout.readline(MAX_STDIO_MESSAGE_BYTES + 1)
                if len(upstream_line) > MAX_STDIO_MESSAGE_BYTES:
                    raise ValueError("Knowledge upstream frame exceeds the private hop limit")
                if not upstream_line:
                    raise RuntimeError("Knowledge MCP upstream closed its stdout")
                upstream_message = json.loads(upstream_line)
                if not isinstance(upstream_message, dict):
                    continue
                if upstream_message.get("id") != expected_id:
                    _write_message(upstream_message)
                    continue
                upstream_message = _full_corpus_tool_schema(upstream_message, message)
                if materialize:
                    upstream_message = _materialize_response(upstream_message, message)
                _write_message(upstream_message)
                break
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
    return child.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
