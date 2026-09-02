"""Persistent, server-authoritative state for local Knowledge research."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

if __package__:
    from .report import render_html_report
else:  # pragma: no cover - exercised by deployment entrypoint smoke tests
    from report import render_html_report  # type: ignore[import-not-found,no-redef]

STATE_SCHEMA_VERSION = "opensquilla-knowledge-research-state/1"
LEDGER_SCHEMA_VERSION = "opensquilla-knowledge-evidence-ledger/1"
PROVENANCE_SCHEMA_VERSION = "opensquilla-knowledge-provenance/1"
PUBLIC_MANIFEST_VERSION = "opensquilla-public-artifact-manifest/1"

_RESEARCH_ID = re.compile(r"^kr_[0-9a-f]{32}$")
_INTERNAL_ID_HINT = re.compile(
    r"(?:\bev(?:idence)?\d*_[0-9a-f]{8,}\b|\b(?:tbl\d*|t\d+)_[0-9a-z]{8,}\b|"
    r"\bkr_[0-9a-f]{32}\b|\b(?:researchId|fileId|evidenceId|tableId)\b)",
    re.IGNORECASE,
)
_KNOWLEDGE_TOOLS = frozenset({"search", "searchByIds", "getFileDetails", "getTable"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024


class ResearchStateError(ValueError):
    """Raised when a research action cannot be verified safely."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _string(value: Any, *, name: str, maximum: int = 20_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResearchStateError(f"{name} must be a non-empty string")
    clean = value.strip()
    if len(clean) > maximum:
        raise ResearchStateError(f"{name} exceeds {maximum} characters")
    return clean


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_json[JsonValue](value: JsonValue) -> JsonValue:
    return cast(JsonValue, json.loads(canonical_json(value)))


def _decode_image(content: Sequence[Any]) -> tuple[bytes, str] | None:
    for block in content:
        if not isinstance(block, Mapping) or block.get("type") != "image":
            continue
        data = block.get("data")
        mime = block.get("mimeType")
        if not isinstance(data, str) or not isinstance(mime, str):
            continue
        try:
            return base64.b64decode(data, validate=True), mime
        except (ValueError, TypeError):
            return None
    return None


def recover_structured_content(
    result: Mapping[str, Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Recover the duplicated MCP payload without trusting arbitrary prose."""

    structured = _mapping(result.get("structuredContent"))
    if structured is not None:
        return structured, "structuredContent"
    for block in _list(result.get("content")):
        if not isinstance(block, Mapping) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            return None, None
        try:
            recovered = json.loads(text)
        except json.JSONDecodeError:
            return None, None
        return (_mapping(recovered), "content[0].text")
    return None, None


class KnowledgeResearchStore:
    """Atomic JSON state store keyed by an opaque research ID."""

    def __init__(
        self,
        *,
        workspace: str | os.PathLike[str],
        private_root: str | os.PathLike[str] | None = None,
        media_root: str | os.PathLike[str] | None = None,
        pdf_renderer: Callable[[str, Path], bytes] | None = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.private_root = (
            Path(private_root).expanduser().resolve()
            if private_root is not None
            else self.workspace / ".codex" / "knowledge-research"
        )
        self.media_root = (
            Path(media_root).expanduser().resolve() if media_root is not None else None
        )
        self.output_root = self.workspace / "knowledge-reports"
        self.pdf_renderer = pdf_renderer or _render_pdf

    def begin(self, *, title: str, subtitle: str | None = None) -> dict[str, Any]:
        clean_title = _string(title, name="title", maximum=300)
        clean_subtitle = (
            _string(subtitle, name="subtitle", maximum=500) if subtitle is not None else None
        )
        self._reject_internal_ids(clean_title, clean_subtitle or "")
        while True:
            research_id = f"kr_{secrets.token_hex(16)}"
            if not self._state_path(research_id).exists():
                break
        state: dict[str, Any] = {
            "schemaVersion": STATE_SCHEMA_VERSION,
            "researchId": research_id,
            "title": clean_title,
            "subtitle": clean_subtitle,
            "ledger": {
                "schemaVersion": LEDGER_SCHEMA_VERSION,
                "calls": [],
                "evidence": {},
                "files": {},
                "inventories": {},
                "tables": {},
            },
            "report": {"items": []},
            "finalized": None,
        }
        self._save(state)
        return {
            "researchId": research_id,
            "status": "ready",
            "verificationStatus": "server_authoritative",
        }

    def exists(self, research_id: str) -> bool:
        return self._state_path(research_id).is_file()

    def snapshot(self, research_id: str) -> dict[str, Any]:
        return _safe_json(self._load(research_id))

    def record_knowledge_call(
        self,
        *,
        research_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        if tool_name not in _KNOWLEDGE_TOOLS:
            raise ResearchStateError(f"unsupported Knowledge tool: {tool_name}")
        state = self._load(research_id)
        ledger = state["ledger"]
        structured, structured_source = recover_structured_content(result)
        if bool(result.get("isError")):
            status = "upstream_error"
            accepted = 0
        elif structured is None:
            status = "unverified_missing_structured_content"
            accepted = 0
        else:
            status, accepted = self._ingest(
                research_id=research_id,
                ledger=ledger,
                tool_name=tool_name,
                arguments=arguments,
                structured=structured,
                content=_list(result.get("content")),
            )
        call = {
            "sequence": len(ledger["calls"]) + 1,
            "toolName": tool_name,
            "arguments": _safe_json(dict(arguments)),
            "resultSha256": sha256_json(result),
            "verificationStatus": status,
            "acceptedRecordCount": accepted,
            "structuredSource": structured_source,
        }
        if tool_name in {"search", "searchByIds"} and structured is not None:
            call["retrieval"] = self._retrieval_telemetry(structured)
        ledger["calls"].append(call)
        self._save(state)
        return _safe_json(call)

    def record_knowledge_error(
        self,
        *,
        research_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        error: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record an actual upstream JSON-RPC failure without accepting evidence."""

        if tool_name not in _KNOWLEDGE_TOOLS:
            raise ResearchStateError(f"unsupported Knowledge tool: {tool_name}")
        state = self._load(research_id)
        ledger = state["ledger"]
        call = {
            "sequence": len(ledger["calls"]) + 1,
            "toolName": tool_name,
            "arguments": _safe_json(dict(arguments)),
            "resultSha256": sha256_json({"error": error}),
            "verificationStatus": "upstream_rpc_error",
            "acceptedRecordCount": 0,
        }
        ledger["calls"].append(call)
        self._save(state)
        return _safe_json(call)

    def add_claim(
        self,
        *,
        research_id: str,
        section: str,
        text: str,
        evidence_ids: Sequence[str],
    ) -> dict[str, Any]:
        state = self._load(research_id)
        clean_section = _string(section, name="section", maximum=200)
        clean_text = _string(text, name="text")
        self._reject_internal_ids(clean_section, clean_text, state=state)
        ids = self._verified_evidence_ids(state, evidence_ids)
        item = {
            "kind": "claim",
            "itemId": f"claim-{len(state['report']['items']) + 1:04d}",
            "section": clean_section,
            "text": clean_text,
            "evidenceIds": ids,
        }
        state["report"]["items"].append(item)
        state["finalized"] = None
        self._save(state)
        self._remove_public_outputs(research_id)
        return {
            "status": "accepted",
            "item": item["itemId"],
            "citationCount": len(ids),
        }

    def add_table(
        self,
        *,
        research_id: str,
        section: str,
        table_id: str,
        caption: str,
    ) -> dict[str, Any]:
        state = self._load(research_id)
        clean_section = _string(section, name="section", maximum=200)
        clean_caption = _string(caption, name="caption", maximum=1_000)
        self._reject_internal_ids(clean_section, clean_caption, state=state)
        table = state["ledger"]["tables"].get(table_id)
        if not isinstance(table, Mapping) or table.get("verificationStatus") != "verified":
            raise ResearchStateError("table was not verified by an actual getTable response")
        file_id = str(table.get("fileId") or "")
        inventory = state["ledger"]["inventories"].get(file_id)
        if not isinstance(inventory, Mapping) or not inventory.get("complete"):
            raise ResearchStateError("table inventory is incomplete; run getFileDetails first")
        if table_id not in inventory.get("tableIds", []):
            raise ResearchStateError("table is not present in the verified file inventory")
        if file_id not in state["ledger"]["files"]:
            raise ResearchStateError("table source metadata is unavailable")
        if any(
            item.get("kind") == "table" and item.get("tableId") == table_id
            for item in state["report"]["items"]
        ):
            raise ResearchStateError("table is already present in the report")
        item = {
            "kind": "table",
            "itemId": f"table-{len(state['report']['items']) + 1:04d}",
            "section": clean_section,
            "caption": clean_caption,
            "tableId": table_id,
        }
        state["report"]["items"].append(item)
        state["finalized"] = None
        self._save(state)
        self._remove_public_outputs(research_id)
        return {"status": "accepted", "item": item["itemId"], "tableCount": 1}

    def finalize(self, *, research_id: str) -> dict[str, Any]:
        state = self._load(research_id)
        items = state["report"]["items"]
        if not items or not any(item.get("kind") == "claim" for item in items):
            raise ResearchStateError("report requires at least one cited claim")
        html = render_html_report(self._state_for_render(state))
        self._reject_report_leaks(html, state)
        pdf = self.pdf_renderer(html, self.workspace)
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF"):
            raise ResearchStateError("PDF renderer did not return a PDF")

        html_bytes = html.encode("utf-8")
        provenance = self._provenance(state, html_bytes=html_bytes, pdf_bytes=pdf)
        provenance_bytes = (
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        output_dir = (self.output_root / research_id).resolve()
        output_dir.relative_to(self.workspace)
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = {
            "report.html": (html_bytes, "text/html"),
            "report.pdf": (pdf, "application/pdf"),
            "provenance.json": (provenance_bytes, "application/json"),
        }
        files: list[dict[str, Any]] = []
        for name, (payload, mime) in outputs.items():
            target = output_dir / name
            self._atomic_write(target, payload)
            files.append(
                {
                    "path": target.relative_to(self.workspace).as_posix(),
                    "name": name,
                    "mime": mime,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "bundle": "none",
                }
            )
        state["finalized"] = {
            "manifestVersion": PUBLIC_MANIFEST_VERSION,
            "files": files,
        }
        self._save(state)
        return {
            "status": "finalized",
            "publicArtifactManifest": {
                "schemaVersion": PUBLIC_MANIFEST_VERSION,
                "files": files,
            },
            "note": (
                "Publish exactly these three files with bundle=none. Do not publish "
                "the report directory or any private research state."
            ),
        }

    def _ingest(
        self,
        research_id: str,
        ledger: dict[str, Any],
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
        content: list[Any],
    ) -> tuple[str, int]:
        if tool_name in {"search", "searchByIds"}:
            return self._ingest_search(
                ledger,
                tool_name=tool_name,
                arguments=arguments,
                structured=structured,
            )
        if tool_name == "getFileDetails":
            return self._ingest_file_details(ledger, arguments, structured)
        return self._ingest_table(
            research_id,
            ledger,
            arguments,
            structured,
            content,
        )

    @staticmethod
    def _ingest_search(
        ledger: dict[str, Any],
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
    ) -> tuple[str, int]:
        results = structured.get("results")
        if not isinstance(results, list):
            return "unverified_invalid_payload", 0
        requested_file_ids: set[str] | None = None
        if tool_name == "searchByIds":
            raw_file_ids = arguments.get("fileIds")
            if not isinstance(raw_file_ids, list) or not raw_file_ids:
                return "unverified_invalid_scope", 0
            if not all(isinstance(value, str) and value for value in raw_file_ids):
                return "unverified_invalid_scope", 0
            requested_file_ids = set(raw_file_ids)
            if structured.get("scopeEnforced") is not True:
                return "unverified_scope_not_enforced", 0
        elif "collectionIds" in arguments and structured.get("scopeEnforced") is not True:
            return "unverified_scope_not_enforced", 0

        pending: dict[str, dict[str, Any]] = {}
        pending_files: dict[str, dict[str, Any]] = {}
        for raw in results:
            item = _mapping(raw)
            if item is None:
                return "unverified_invalid_payload", 0
            evidence_id = item.get("evidenceId")
            file_id = item.get("fileId")
            document_id = item.get("documentId")
            revision = item.get("revision")
            content = item.get("content")
            if not all(
                isinstance(value, str) and value
                for value in (evidence_id, file_id, revision, content)
            ):
                return "unverified_invalid_payload", 0
            if document_id is not None and not isinstance(document_id, str):
                return "unverified_invalid_payload", 0
            assert isinstance(evidence_id, str)
            assert isinstance(file_id, str)
            assert isinstance(revision, str)
            assert isinstance(content, str)
            if requested_file_ids is not None and file_id not in requested_file_ids:
                return "unverified_scope_violation", 0
            locator = _mapping(item.get("locator"))
            if locator is None:
                return "unverified_invalid_payload", 0
            title = item.get("title")
            title = title if isinstance(title, str) and title.strip() else "Untitled local document"
            record = {
                "evidenceId": evidence_id,
                "fileId": file_id,
                "documentId": document_id,
                "revision": revision,
                "title": title,
                "locator": _safe_json(locator),
                "content": content,
                "contentSha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "verificationStatus": "verified",
            }
            existing = ledger["evidence"].get(evidence_id)
            if existing is not None and sha256_json(existing) != sha256_json(record):
                return "unverified_collision", 0
            duplicate = pending.get(evidence_id)
            if duplicate is not None and sha256_json(duplicate) != sha256_json(record):
                return "unverified_collision", 0
            pending[evidence_id] = record

            current_file = pending_files.get(file_id) or ledger["files"].get(file_id)
            merged_file: dict[str, Any]
            if current_file is None:
                merged_file = {
                    "fileId": file_id,
                    "documentId": document_id,
                    "revision": revision,
                    "title": title,
                    "filename": None,
                    "sourcePath": None,
                    "mediaType": None,
                    "observedLocators": [],
                    "metadataSource": "search",
                    "verificationStatus": "verified",
                }
            else:
                if current_file.get("documentId") not in {None, document_id}:
                    return "unverified_document_mismatch", 0
                if current_file.get("revision") not in {None, revision}:
                    return "unverified_revision_mismatch", 0
                merged_file = dict(_safe_json(current_file))
                if merged_file.get("title") == "Untitled local document":
                    merged_file["title"] = title
            observed_locator = _safe_json(locator)
            observed_locators = merged_file.get("observedLocators")
            if not isinstance(observed_locators, list):
                return "unverified_invalid_file_record", 0
            if observed_locator not in observed_locators:
                observed_locators.append(observed_locator)
            pending_files[file_id] = merged_file
        ledger["evidence"].update(pending)
        ledger["files"].update(pending_files)
        return "verified", len(pending)

    @staticmethod
    def _ingest_file_details(
        ledger: dict[str, Any],
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
    ) -> tuple[str, int]:
        file_payload = _mapping(structured.get("file"))
        tables = structured.get("tables")
        requested_file = arguments.get("fileId")
        if file_payload is None or not isinstance(tables, list):
            return "unverified_invalid_payload", 0
        file_id = file_payload.get("fileId")
        if not isinstance(file_id, str) or not file_id or file_id != requested_file:
            return "unverified_identity_mismatch", 0
        document_id = file_payload.get("documentId")
        revision = file_payload.get("revision")
        if not isinstance(document_id, str) or not document_id:
            return "unverified_invalid_payload", 0
        if not isinstance(revision, str) or not revision:
            return "unverified_invalid_payload", 0
        existing_file = ledger["files"].get(file_id)
        if isinstance(existing_file, Mapping):
            if existing_file.get("documentId") not in {None, document_id}:
                return "unverified_document_mismatch", 0
            if existing_file.get("revision") not in {None, revision}:
                return "unverified_revision_mismatch", 0
            observed_locators = _safe_json(existing_file.get("observedLocators") or [])
        else:
            observed_locators = []
        file_record = {
            "fileId": file_id,
            "documentId": document_id,
            "title": file_payload.get("title")
            or file_payload.get("filename")
            or "Untitled local document",
            "filename": file_payload.get("filename"),
            "sourcePath": file_payload.get("sourcePath"),
            "mediaType": file_payload.get("mediaType"),
            "revision": revision,
            "observedLocators": observed_locators,
            "metadataSource": "getFileDetails",
            "verificationStatus": "verified",
        }

        cursor = arguments.get("cursor")
        first_page = cursor is None or cursor == ""
        complete_inventory = structured.get("inventoryComplete") is True
        if "inventoryComplete" in structured and not isinstance(
            structured.get("inventoryComplete"), bool
        ):
            return "unverified_invalid_inventory", 0
        if complete_inventory and not first_page:
            return "unverified_cursor_sequence", 0
        current = ledger["inventories"].get(file_id)
        inventory: dict[str, Any]
        if complete_inventory:
            inventory_page_count = structured.get("inventoryPageCount")
            inventory_table_count = structured.get("inventoryTableCount")
            if (
                not isinstance(inventory_page_count, int)
                or isinstance(inventory_page_count, bool)
                or inventory_page_count < 1
                or not isinstance(inventory_table_count, int)
                or isinstance(inventory_table_count, bool)
                or inventory_table_count < 0
                or inventory_table_count != len(tables)
            ):
                return "unverified_inventory_count_mismatch", 0
            inventory = {
                "fileId": file_id,
                "tableIds": [],
                "complete": False,
                "pageCount": inventory_page_count,
                "nextCursor": None,
                "reportedTableCount": inventory_table_count,
                "verificationStatus": "verified",
            }
        elif first_page:
            inventory = {
                "fileId": file_id,
                "tableIds": [],
                "complete": False,
                "pageCount": 0,
                "nextCursor": None,
                "verificationStatus": "verified",
            }
        else:
            if not isinstance(cursor, str) or not cursor:
                return "unverified_cursor_sequence", 0
            if not isinstance(current, Mapping) or current.get("complete"):
                return "unverified_cursor_sequence", 0
            if current.get("nextCursor") != cursor:
                return "unverified_cursor_sequence", 0
            inventory = dict(_safe_json(current))

        pending_table_ids: list[str] = []
        for raw in tables:
            table = _mapping(raw)
            if table is None:
                return "unverified_invalid_payload", 0
            table_id = table.get("tableId")
            if not isinstance(table_id, str) or not table_id:
                return "unverified_invalid_payload", 0
            projected_file_id = table.get("fileId")
            if projected_file_id is None:
                if not complete_inventory:
                    return "unverified_identity_mismatch", 0
            elif projected_file_id != file_id:
                return "unverified_identity_mismatch", 0
            if table_id in pending_table_ids or table_id in inventory["tableIds"]:
                return "unverified_duplicate_table", 0
            pending_table_ids.append(table_id)

        next_cursor = structured.get("nextCursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
            return "unverified_invalid_cursor", 0
        for table_id in pending_table_ids:
            if table_id not in inventory["tableIds"]:
                inventory["tableIds"].append(table_id)
        if not complete_inventory:
            inventory["pageCount"] += 1
        inventory["nextCursor"] = next_cursor
        inventory["complete"] = next_cursor is None
        if complete_inventory and next_cursor is not None:
            return "unverified_invalid_inventory", 0

        extraction = _mapping(structured.get("tableExtraction"))
        extraction_count = extraction.get("tableCount") if extraction is not None else None
        if extraction_count is not None and (
            not isinstance(extraction_count, int)
            or isinstance(extraction_count, bool)
            or extraction_count < 0
        ):
            return "unverified_inventory_count_mismatch", 0
        if inventory["complete"] and isinstance(extraction_count, int):
            if extraction_count != len(inventory["tableIds"]):
                return "unverified_inventory_count_mismatch", 0
            inventory["reportedTableCount"] = extraction_count
        ledger["files"][file_id] = file_record
        ledger["inventories"][file_id] = inventory
        return "verified", len(pending_table_ids)

    def _ingest_table(
        self,
        research_id: str,
        ledger: dict[str, Any],
        arguments: Mapping[str, Any],
        structured: Mapping[str, Any],
        content: list[Any],
    ) -> tuple[str, int]:
        table_id = structured.get("tableId")
        file_id = structured.get("fileId")
        if table_id != arguments.get("tableId") or file_id != arguments.get("fileId"):
            return "unverified_identity_mismatch", 0
        text = _mapping(structured.get("text"))
        screenshot = _mapping(structured.get("screenshot"))
        if (
            not isinstance(table_id, str)
            or not isinstance(file_id, str)
            or text is None
            or screenshot is None
        ):
            return "unverified_invalid_payload", 0
        text_content = text.get("content")
        text_sha = text.get("sha256")
        screenshot_sha = screenshot.get("sha256")
        if not isinstance(text_content, str) or not isinstance(text_sha, str):
            return "unverified_invalid_payload", 0
        if hashlib.sha256(text_content.encode("utf-8")).hexdigest() != text_sha:
            return "unverified_hash_mismatch", 0
        if not isinstance(screenshot_sha, str) or _SHA256.fullmatch(screenshot_sha) is None:
            return "unverified_invalid_payload", 0
        image_bytes, image_mime, image_status = self._verified_screenshot(
            structured,
            screenshot,
            content,
        )
        if image_bytes is None or image_mime is None:
            return image_status, 0
        if hashlib.sha256(image_bytes).hexdigest() != screenshot_sha:
            return "unverified_hash_mismatch", 0
        source_file = ledger["files"].get(file_id)
        if isinstance(source_file, Mapping):
            source_revision = source_file.get("revision")
            table_revision = structured.get("revision")
            if source_revision and table_revision and source_revision != table_revision:
                return "unverified_revision_mismatch", 0
        private_path = self._private_screenshot_path(
            research_id,
            digest=screenshot_sha,
            media_type=image_mime,
        )
        safe_screenshot = {
            key: value
            for key, value in screenshot.items()
            if key not in {"dataBase64", "localPath"}
        }
        record = {
            "tableId": table_id,
            "fileId": file_id,
            "documentId": structured.get("documentId"),
            "revision": structured.get("revision"),
            "page": structured.get("page"),
            "locator": _safe_json(structured.get("locator") or {}),
            "text": _safe_json(text),
            "screenshot": _safe_json(safe_screenshot),
            "screenshotPrivatePath": private_path.relative_to(self.private_root).as_posix(),
            "verificationStatus": "verified",
        }
        existing = ledger["tables"].get(table_id)
        if existing is not None and sha256_json(existing) != sha256_json(record):
            return "unverified_collision", 0
        self._write_private_screenshot(private_path, image_bytes)
        ledger["tables"][table_id] = record
        return "verified", 1

    @staticmethod
    def _retrieval_telemetry(structured: Mapping[str, Any]) -> dict[str, Any]:
        return _safe_json(
            {
                "requestedProfile": structured.get("requestedProfile"),
                "effectiveProfile": structured.get("effectiveProfile")
                or structured.get("retrievalProfile"),
                "retrievalProfile": structured.get("retrievalProfile"),
                "selectionSource": structured.get("selectionSource"),
                "fallbackReason": structured.get("fallbackReason"),
                "warnings": structured.get("warnings")
                if isinstance(structured.get("warnings"), list)
                else [],
                "scopeEnforced": structured.get("scopeEnforced"),
                "lexicalCandidateCount": structured.get("lexicalCandidateCount"),
                "vectorCandidateCount": structured.get("vectorCandidateCount"),
            }
        )

    def _verified_screenshot(
        self,
        structured: Mapping[str, Any],
        screenshot: Mapping[str, Any],
        content: Sequence[Any],
    ) -> tuple[bytes | None, str | None, str]:
        if structured.get("screenshotMaterializationError") is not None:
            return None, None, "unverified_screenshot_materialization"

        declared_media_type = screenshot.get("mediaType")
        if not isinstance(declared_media_type, str):
            return None, None, "unverified_invalid_payload"
        image = _decode_image(content)
        local_values = [
            value
            for value in (
                structured.get("screenshotLocalPath"),
                screenshot.get("localPath"),
            )
            if value is not None
        ]
        local_payload: bytes | None = None
        if local_values:
            if self.media_root is None:
                return None, None, "unverified_screenshot_root_unconfigured"
            if not all(isinstance(value, str) and value for value in local_values):
                return None, None, "unverified_screenshot_path"
            try:
                resolved_paths = {
                    Path(value).expanduser().resolve(strict=True) for value in local_values
                }
                if len(resolved_paths) != 1:
                    return None, None, "unverified_screenshot_path_mismatch"
                local_payload = self._read_bounded_file(
                    resolved_paths.pop(),
                    allowed_root=self.media_root,
                )
            except (OSError, ResearchStateError, RuntimeError, ValueError):
                return None, None, "unverified_screenshot_path"

        if local_payload is None and image is None:
            return None, None, "unverified_missing_screenshot"
        image_payload = image[0] if image is not None else None
        if local_payload is not None and image_payload is not None:
            if hashlib.sha256(local_payload).digest() != hashlib.sha256(image_payload).digest():
                return None, None, "unverified_screenshot_source_mismatch"
        payload = local_payload if local_payload is not None else image_payload
        assert payload is not None
        detected_media_type = self._detect_image_media_type(payload)
        if detected_media_type is None:
            return None, None, "unverified_screenshot_magic"
        if detected_media_type != declared_media_type:
            return None, None, "unverified_screenshot_media_type"
        if image is not None and image[1] != detected_media_type:
            return None, None, "unverified_screenshot_media_type"
        declared_size = screenshot.get("sizeBytes")
        if declared_size is not None and (
            not isinstance(declared_size, int)
            or isinstance(declared_size, bool)
            or declared_size != len(payload)
        ):
            return None, None, "unverified_screenshot_size"
        return payload, detected_media_type, "verified"

    @staticmethod
    def _detect_image_media_type(payload: bytes) -> str | None:
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if payload.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
            return "image/webp"
        return None

    @staticmethod
    def _read_bounded_file(path: Path, *, allowed_root: Path) -> bytes:
        root = allowed_root.resolve(strict=True)
        target = path.resolve(strict=True)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ResearchStateError("screenshot path is outside its allowed root") from exc
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ResearchStateError("screenshot path is not a regular file")
            if metadata.st_size <= 0 or metadata.st_size > _MAX_SCREENSHOT_BYTES:
                raise ResearchStateError("screenshot size is outside the allowed range")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                payload = handle.read(_MAX_SCREENSHOT_BYTES + 1)
            if len(payload) != metadata.st_size or len(payload) > _MAX_SCREENSHOT_BYTES:
                raise ResearchStateError("screenshot changed while it was being read")
            return payload
        finally:
            os.close(descriptor)

    def _private_screenshot_path(
        self,
        research_id: str,
        *,
        digest: str,
        media_type: str,
    ) -> Path:
        self._state_path(research_id)
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
        }[media_type]
        target = (self.private_root / research_id / "media" / f"{digest}{extension}").resolve()
        target.relative_to((self.private_root / research_id).resolve())
        return target

    @staticmethod
    def _write_private_screenshot(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        if path.exists():
            if path.read_bytes() != payload:
                raise ResearchStateError("private screenshot digest collision")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)

    def _state_for_render(self, state: Mapping[str, Any]) -> dict[str, Any]:
        rendered = dict(_safe_json(state))
        research_id = str(state["researchId"])
        research_root = (self.private_root / research_id).resolve()
        selected_table_ids = {
            item["tableId"] for item in rendered["report"]["items"] if item.get("kind") == "table"
        }
        for table_id in selected_table_ids:
            record = rendered["ledger"]["tables"].get(table_id)
            if not isinstance(record, dict):
                raise ResearchStateError("selected table is missing from the ledger")
            relative_path = record.get("screenshotPrivatePath")
            screenshot = record.get("screenshot")
            if not isinstance(relative_path, str) or not isinstance(screenshot, dict):
                raise ResearchStateError("selected table screenshot metadata is invalid")
            target = (self.private_root / relative_path).resolve()
            payload = self._read_bounded_file(target, allowed_root=research_root)
            expected_digest = screenshot.get("sha256")
            expected_media_type = screenshot.get("mediaType")
            if hashlib.sha256(payload).hexdigest() != expected_digest:
                raise ResearchStateError("private screenshot SHA256 mismatch")
            if self._detect_image_media_type(payload) != expected_media_type:
                raise ResearchStateError("private screenshot media type mismatch")
            record["screenshotDataBase64"] = base64.b64encode(payload).decode("ascii")
        return rendered

    @staticmethod
    def _verified_evidence_ids(state: Mapping[str, Any], values: Sequence[str]) -> list[str]:
        if isinstance(values, str) or not isinstance(values, Sequence) or not values:
            raise ResearchStateError("evidenceIds must be a non-empty array")
        evidence = state["ledger"]["evidence"]
        ids: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value:
                raise ResearchStateError("evidenceIds must contain non-empty strings")
            record = evidence.get(value)
            if not isinstance(record, Mapping) or record.get("verificationStatus") != "verified":
                raise ResearchStateError(
                    f"evidence was not verified by an actual search result: {value}"
                )
            if value not in ids:
                ids.append(value)
        return ids

    @staticmethod
    def _reject_internal_ids(*values: str, state: Mapping[str, Any] | None = None) -> None:
        known_ids: set[str] = set()
        if state is not None:
            ledger = state["ledger"]
            known_ids.update(ledger["evidence"])
            known_ids.update(ledger["files"])
            known_ids.update(ledger["tables"])
        for value in values:
            if _INTERNAL_ID_HINT.search(value) or any(
                identifier in value for identifier in known_ids
            ):
                raise ResearchStateError("human-facing report text contains an internal identifier")

    @classmethod
    def _reject_report_leaks(cls, html: str, state: Mapping[str, Any]) -> None:
        cls._reject_internal_ids(html, state=state)

    def _provenance(
        self,
        state: Mapping[str, Any],
        *,
        html_bytes: bytes,
        pdf_bytes: bytes,
    ) -> dict[str, Any]:
        ledger = _safe_json(state["ledger"])
        for record in ledger["evidence"].values():
            record.pop("content", None)
        for record in ledger["tables"].values():
            record.pop("screenshotDataBase64", None)
            record.pop("screenshotPrivatePath", None)
            text = record.get("text")
            if isinstance(text, dict):
                text.pop("content", None)
        return {
            "schemaVersion": PROVENANCE_SCHEMA_VERSION,
            "researchId": state["researchId"],
            "title": state["title"],
            "ledger": ledger,
            "report": _safe_json(state["report"]),
            "artifacts": {
                "report.html": hashlib.sha256(html_bytes).hexdigest(),
                "report.pdf": hashlib.sha256(pdf_bytes).hexdigest(),
            },
        }

    def _state_path(self, research_id: str) -> Path:
        if not isinstance(research_id, str) or _RESEARCH_ID.fullmatch(research_id) is None:
            raise ResearchStateError("researchId is invalid")
        return self.private_root / research_id / "state.json"

    def _load(self, research_id: str) -> dict[str, Any]:
        path = self._state_path(research_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ResearchStateError("researchId was not found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ResearchStateError("research state is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schemaVersion") != STATE_SCHEMA_VERSION:
            raise ResearchStateError("research state schema is invalid")
        return payload

    def _save(self, state: Mapping[str, Any]) -> None:
        path = self._state_path(str(state["researchId"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)

    def _remove_public_outputs(self, research_id: str) -> None:
        output_dir = (self.output_root / research_id).resolve()
        try:
            output_dir.relative_to(self.workspace)
        except ValueError as exc:
            raise ResearchStateError("research output path escaped the workspace") from exc
        for name in ("report.html", "report.pdf", "provenance.json"):
            try:
                (output_dir / name).unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)


def _render_pdf(html: str, base_url: Path) -> bytes:
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ResearchStateError(
            "knowledge report PDF rendering requires opensquilla[document-extras]"
        ) from exc
    payload = HTML(string=html, base_url=str(base_url)).write_pdf()
    if not isinstance(payload, bytes):
        raise ResearchStateError("PDF renderer returned an invalid payload")
    return payload
