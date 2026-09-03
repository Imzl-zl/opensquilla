"""MCP proxy adding authoritative research state around Knowledge tools."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, Protocol

if __package__:
    from .navigation import (
        MAX_FRAME_BYTES,
        PAGE_BUDGET,
        Navigation,
        NavigationError,
        choose,
        content_page,
        item_page,
        positive_limit,
        source_locator,
        strings,
        text,
    )
    from .review import report_review_hash
    from .state import (
        KnowledgeResearchStore,
        ResearchStateError,
        canonical_json,
        recover_structured_content,
    )
else:  # pragma: no cover - exercised by deployment entrypoint smoke tests
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from navigation import (  # type: ignore[import-not-found,no-redef]
        MAX_FRAME_BYTES,
        PAGE_BUDGET,
        Navigation,
        NavigationError,
        choose,
        content_page,
        item_page,
        positive_limit,
        source_locator,
        strings,
        text,
    )
    from review import report_review_hash  # type: ignore[import-not-found,no-redef]
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
        "researchNavigate",
        "researchReadEvidence",
    }
)
_LEDGER_TOOLS = frozenset({"search", "searchByIds", "getFileDetails", "getTable"})
_MAX_DETAIL_PAGES = 250


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
            line = self.process.stdout.readline(16 * 1024 * 1024 + 1)
            if len(line) > 16 * 1024 * 1024:
                raise RuntimeError("Knowledge private-hop frame exceeded 16 MiB")
            if not line:
                raise RuntimeError("upstream Knowledge MCP process closed")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload


class KnowledgeResearchBridge:
    def __init__(
        self,
        upstream: Upstream,
        store: KnowledgeResearchStore,
        *,
        caller_binding: Callable[[], str | None] | None = None,
    ) -> None:
        self.upstream = upstream
        self.store = store
        self.caller_binding = caller_binding or (lambda: None)

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        if request_id is not None and (
            type(request_id) not in {str, int} or len(canonical_json(request_id).encode()) > 256
        ):
            return _rpc_error(None, -32600, "Invalid request id")
        response = self._handle(message)
        if response is not None and _frame_bytes(response) > MAX_FRAME_BYTES:
            return _success(
                request_id,
                _tool_result(
                    {
                        "error": "Response exceeds frame budget; saved snapshots remain navigable",
                        "details": {"code": "FRAME_TOO_LARGE"},
                    },
                    is_error=True,
                ),
            )
        return response

    def _handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, Mapping) else None
        if request_id is None:
            if method == "notifications/initialized":
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
                payload: dict[str, Any] = {"error": str(exc)}
                details = getattr(exc, "details", None)
                if isinstance(details, Mapping):
                    payload["details"] = dict(details)
                return _success(request_id, _tool_result(payload, is_error=True))
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
        if name not in _LOCAL_TOOLS | _LEDGER_TOOLS:
            raise NavigationError("UNKNOWN_TOOL", "Unknown research tool")
        if name == "researchBegin":
            result = self._call_local(name, arguments)
            payload, _ = recover_structured_content(result)
            assert payload is not None
            self.store.atomic_update(
                str(payload["researchId"]), lambda state: self._navigation(state).progress()
            )
            return _success(request_id, result)
        research_id = text(arguments.get("researchId"), "/researchId", 35)
        # The transport has no per-model session auth. Only a trusted host may supply this binding.
        self._navigation(self.store.snapshot(research_id))
        if name in _LOCAL_TOOLS - {"researchNavigate", "researchReadEvidence"}:
            return _success(
                request_id,
                self._call_local(name, self._canonical_arguments(research_id, name, arguments)),
            )

        def project(nav: Navigation, snapshot_ref: str, start: int = 0) -> dict[str, Any]:
            reply = self._project(
                nav, snapshot_ref, start, positive_limit(arguments.get("limit")), request_id
            )
            nav.commit_request(arguments.get("requestKey"), reply)
            return reply

        def prepare(state: dict[str, Any]) -> dict[str, Any]:
            nav = self._navigation(state)
            key = arguments.get("requestKey")
            fingerprint = {
                "tool": name,
                "arguments": {k: v for k, v in arguments.items() if k != "requestKey"},
            }
            replay = nav.begin_request(key, fingerprint)
            if replay is not None:
                return {"reply": replay}
            positive_limit(arguments.get("limit"))
            forwarded: dict[str, Any] = {}
            snapshot_ref: str | None = None
            start = 0
            if "cursor" in arguments:
                snapshot_ref, start = nav.from_cursor(arguments["cursor"])
                self._check_cursor_target(nav, name, arguments, snapshot_ref)
            elif name == "researchNavigate":
                if "snapshotRef" in arguments:
                    snapshot_ref = text(arguments["snapshotRef"], "/snapshotRef")
                    if snapshot_ref not in nav.data["snapshots"]:
                        raise NavigationError("UNKNOWN_REFERENCE", "Unknown snapshot")
                    self._check_cursor_target(nav, name, arguments, snapshot_ref)
                elif arguments.get("view") == "review" and state.get("mode") == "deep":
                    return {"prepareMetadata": True}
                else:
                    snapshot_ref = nav.directory(str(arguments.get("view", "progress")))
            elif name == "researchReadEvidence":
                snapshot_ref = nav.read_snapshot(arguments)
            elif name in {"search", "searchByIds"}:
                forwarded = {
                    "query": text(arguments.get("query"), "/query"),
                    "limit": positive_limit(arguments.get("limit"), 10),
                }
                if name == "searchByIds":
                    selected = nav.selected_files(arguments)
                    if len(selected) > 20:
                        snapshot_ref = nav.grouping(selected)
                    else:
                        forwarded["fileIds"] = selected
                elif "collectionIds" in arguments:
                    forwarded["collectionIds"] = strings(
                        arguments["collectionIds"], "/collectionIds"
                    )
            else:
                forwarded["fileId"] = nav.canonical(arguments, "file")
                if name == "getTable":
                    forwarded.update(
                        tableId=nav.canonical(arguments, "table"), includeScreenshot=True
                    )
                    table_row = nav.data["tables"].get(forwarded["tableId"])
                    if table_row and table_row["fileId"] != forwarded["fileId"]:
                        raise NavigationError(
                            "REFERENCE_MISMATCH", "Table belongs to a different file"
                        )
            if snapshot_ref is not None:
                return {"reply": project(nav, snapshot_ref, start)}
            return {"forwarded": forwarded}

        prepared = self.store.atomic_update(research_id, prepare)
        if "reply" in prepared:
            return _success(request_id, prepared["reply"])
        if prepared.get("prepareMetadata"):
            warnings = self._prepare_reference_metadata(research_id)

            def review(state: dict[str, Any]) -> dict[str, Any]:
                nav = self._navigation(state)
                snapshot_ref = nav.directory("review")
                nav.data["snapshots"][snapshot_ref].setdefault("metadataWarnings", warnings)
                return project(nav, snapshot_ref)

            return _success(request_id, self.store.atomic_update(research_id, review))
        forwarded = prepared["forwarded"]
        # Both the upstream call and inventory enumeration run outside the state lock.
        response = (
            self._fetch_details(forwarded)
            if name == "getFileDetails"
            else self._upstream_tool(name, forwarded)
        )
        upstream_result = response.get("result")
        committed: dict[str, Any] = {}

        def on_commit(state: dict[str, Any], call: dict[str, Any]) -> None:
            nav = self._navigation(state)
            if call["verificationStatus"] != "verified":
                reply = _tool_result(
                    {
                        "error": "Knowledge result was not accepted by the evidence ledger",
                        "verificationStatus": call["verificationStatus"],
                    },
                    is_error=True,
                )
            else:
                assert isinstance(upstream_result, Mapping)
                structured, _ = recover_structured_content(upstream_result)
                assert structured is not None
                snapshot_ref = nav.observe(name, structured)
                call["navigationSnapshotRef"] = snapshot_ref
                if name in {"search", "searchByIds"}:
                    call["orderedResultIds"] = [row["evidenceId"] for row in structured["results"]]
                try:
                    reply = self._project(
                        nav, snapshot_ref, 0, positive_limit(arguments.get("limit")), request_id
                    )
                except ValueError:
                    # Retain accepted source data even if its projection cannot fit this frame.
                    reply = _tool_result(
                        {
                            "error": "Projection unavailable; complete source snapshot retained",
                            "details": {"code": "PROJECTION_UNAVAILABLE"},
                            "snapshotRef": snapshot_ref,
                            "resumeCursor": nav.cursor(snapshot_ref, 0),
                            "modelDelivery": "unknown",
                        },
                        is_error=True,
                    )
            nav.commit_request(arguments.get("requestKey"), reply)
            committed["reply"] = reply

        if isinstance(upstream_result, Mapping):
            self.store.record_knowledge_call(
                research_id=research_id,
                tool_name=name,
                arguments=forwarded,
                result=upstream_result,
                on_commit=on_commit,
            )
        else:
            error = response.get("error")
            self.store.record_knowledge_error(
                research_id=research_id,
                tool_name=name,
                arguments=forwarded,
                error=error
                if isinstance(error, Mapping)
                else {"message": "Invalid upstream response"},
                on_commit=on_commit,
            )
        return _success(request_id, committed["reply"])

    def _navigation(self, state: dict[str, Any]) -> Navigation:
        return Navigation(state, self.caller_binding())

    def _canonical_arguments(
        self, research_id: str, name: str, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        nav = self._navigation(self.store.snapshot(research_id))
        normalized = copy.deepcopy(dict(arguments))

        def claim(raw: Mapping[str, Any], prefix: str) -> dict[str, Any]:
            value = dict(raw)
            selector = choose(value, ("evidenceRefs", "evidenceIds"))
            ids = strings(value[selector], prefix + "/" + selector, 200)
            if selector == "evidenceRefs":
                translated = []
                for index, ref in enumerate(ids):
                    try:
                        translated.append(nav.resolve("evidence", ref))
                    except NavigationError as exc:
                        exc.details["pointer"] = f"{prefix}/evidenceRefs/{index}"
                        raise
                value.pop("evidenceRefs")
                value["evidenceIds"] = translated
            for field in ("text", "section"):
                prose = value.get(field)
                if isinstance(prose, str) and nav.namespace + ":" in prose:
                    raise NavigationError(
                        "INTERNAL_REFERENCE_IN_TEXT",
                        "Report text contains an internal reference",
                        pointer=f"{prefix}/{field}",
                    )
            return value

        if name == "researchAddClaim":
            normalized = claim(normalized, "")
        elif name == "researchAddClaims":
            claims = arguments.get("claims")
            if not isinstance(claims, list) or not all(isinstance(row, Mapping) for row in claims):
                raise NavigationError(
                    "INVALID_CLAIMS", "claims must be an array of objects", pointer="/claims"
                )
            normalized["claims"] = [
                claim(row, f"/claims/{index}") for index, row in enumerate(claims)
            ]
        elif name == "researchAddTable":
            normalized["tableId"] = nav.canonical(arguments, "table")
            normalized.pop("tableRef", None)
            if any(
                isinstance(arguments.get(key), str) and nav.namespace + ":" in arguments[key]
                for key in ("section", "caption")
            ):
                raise NavigationError(
                    "INTERNAL_REFERENCE_IN_TEXT", "Report text contains an internal reference"
                )
        elif name == "researchFinalize" and "expectedTableRefs" in arguments:
            if "expectedTableIds" in arguments:
                raise NavigationError(
                    "INVALID_SELECTOR", "Supply expectedTableRefs or expectedTableIds"
                )
            values = arguments["expectedTableRefs"]
            if not isinstance(values, list) or len(values) > 1000:
                raise NavigationError("INVALID_SELECTOR", "Invalid expectedTableRefs")
            normalized["expectedTableIds"] = [nav.resolve("table", value) for value in values]
            normalized.pop("expectedTableRefs")
        return normalized

    def _project(
        self, nav: Navigation, snapshot_ref: str, start: int, limit: int, request_id: Any
    ) -> dict[str, Any]:
        snapshot = nav.data["snapshots"][snapshot_ref]
        kind, source = snapshot["kind"], snapshot["payload"]

        def decorate(payload: Mapping[str, Any]) -> dict[str, Any]:
            return nav.wrap_page(snapshot_ref, payload)

        def frame_bytes(payload: Mapping[str, Any]) -> int:
            return _frame_bytes(_success(request_id, _projected_result(decorate(payload))))

        if kind == "search":
            original = source["results"]
            items = [
                self._search_item(
                    nav, row, source.get("_evidenceEntries", {}).get(row["evidenceId"])
                )
                for row in original
            ]
            metadata = {
                key: copy.deepcopy(source[key])
                for key in (
                    "contractVersion",
                    "chunkPolicyId",
                    "indexVersion",
                    "query",
                    "scopeRef",
                    "retrievalProfile",
                    "requestedProfile",
                    "effectiveProfile",
                    "selectionSource",
                    "fallbackReason",
                    "warnings",
                    "scopeEnforced",
                    "selectionStrategy",
                    "lexicalCandidateCount",
                    "vectorCandidateCount",
                    "budgetExceeded",
                )
                if key in source
            }

            def build(selected: list[Any], page: dict[str, Any]) -> dict[str, Any]:
                return {
                    **metadata,
                    "results": selected,
                    "count": len(selected),
                    "page": page,
                    "projectionComplete": all(
                        not item["contentTruncatedForTransport"] for item in selected
                    ),
                }

            # Large chunks have an explicit ledger read path; small chunks remain whole.
            if start < len(items):
                first = items[start]
                first_page = {
                    "start": start,
                    "end": start + 1,
                    "total": len(items),
                    "hasMore": start + 1 < len(items),
                }
                if frame_bytes(build([first], first_page)) > PAGE_BUDGET:

                    def prefix(content: str, page: dict[str, Any]) -> dict[str, Any]:
                        projected = {
                            **first,
                            "content": content,
                            "contentRange": page,
                            "contentTruncatedForTransport": page["end"] < page["total"],
                        }
                        return build([projected], first_page)

                    payload = content_page(first["content"], 0, prefix, frame_bytes)
                else:
                    payload = item_page(items, start, limit, build, frame_bytes)
            else:
                payload = item_page(items, start, limit, build, frame_bytes)
        elif kind == "evidence":
            item = self._search_item(
                nav, source, source.get("_evidenceEntries", {}).get(source["evidenceId"])
            )

            def evidence_page(content: str, page: dict[str, Any]) -> dict[str, Any]:
                return {
                    **{
                        key: value
                        for key, value in item.items()
                        if key not in {"contentRange", "contentTruncatedForTransport"}
                    },
                    "content": content,
                    "page": page,
                    "offsetUnit": "unicode_code_point",
                }

            payload = content_page(source["content"], start, evidence_page, frame_bytes)
        elif kind in {"inventory", "table"}:
            if __package__:
                from .table_views import (
                    project_inventory_page,
                    project_table_page,
                    table_quality_view,
                )
            else:  # pragma: no cover
                from table_views import (  # type: ignore[import-not-found,no-redef]
                    project_inventory_page,
                    project_table_page,
                    table_quality_view,
                )
            if kind == "inventory":
                payload = project_inventory_page(
                    source,
                    start=start,
                    max_items=limit,
                    frame_bytes=frame_bytes,
                    max_frame_bytes=PAGE_BUDGET,
                )
            else:
                quality = table_quality_view(source)

                def table_frame(candidate: Mapping[str, Any]) -> int:
                    return frame_bytes({**candidate, "quality": quality})

                payload = project_table_page(
                    source,
                    start=start,
                    target_chars=10_000,
                    frame_bytes=table_frame,
                    max_frame_bytes=PAGE_BUDGET,
                )
                payload["quality"] = quality
        else:
            key = "groups" if kind == "groups" else "entries"
            metadata = {k: v for k, v in source.items() if k != key}
            if snapshot.get("metadataWarnings"):
                metadata["warnings"] = [
                    *metadata.get("warnings", []),
                    *snapshot["metadataWarnings"],
                ]
            if kind == "groups":
                metadata.update(status="grouped", queried=False, subset=True)
            payload = item_page(
                source[key],
                start,
                limit,
                lambda items, page: {**metadata, key: items, "page": page},
                frame_bytes,
            )
        projected = decorate(payload)
        result = _projected_result(projected)
        if _frame_bytes(_success(request_id, result)) > MAX_FRAME_BYTES:
            raise NavigationError(
                "FRAME_TOO_LARGE", "Projection exceeds the complete RPC frame budget"
            )
        nav.record_projection(snapshot_ref, projected)
        return result

    @staticmethod
    def _search_item(
        nav: Navigation, source: Mapping[str, Any], entry: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        evidence_id = source["evidenceId"]
        record = nav.state["ledger"]["evidence"][evidence_id]
        content = record["content"]
        # Old snapshots remain readable without adding metadata learned after their creation.
        if entry is None:
            entry = {
                "fileRef": nav.data["files"][record["fileId"]]["ref"],
                "evidenceRef": nav.data["evidence"][evidence_id]["ref"],
                "title": record.get("title", ""),
                "locator": source_locator(record.get("locator", {})),
                "contentKind": record.get("contentKind") or "text",
            }
        return {
            **entry,
            "content": content,
            "contentRange": {"start": 0, "end": len(content), "total": len(content)},
            "contentTruncatedForTransport": False,
        }

    def _check_cursor_target(
        self, nav: Navigation, name: str, arguments: Mapping[str, Any], ref: str
    ) -> None:
        snapshot = nav.data["snapshots"][ref]
        expected = {
            "getTable": "table",
            "getFileDetails": "inventory",
            "researchReadEvidence": "evidence",
        }
        if name != "researchNavigate" and snapshot["kind"] != expected.get(name):
            raise NavigationError("INVALID_CURSOR", "Cursor belongs to a different operation")
        payload = snapshot["payload"]
        if name == "researchNavigate":
            if "snapshotRef" in arguments and arguments["snapshotRef"] != ref:
                raise NavigationError("REFERENCE_MISMATCH", "Cursor belongs to another snapshot")
            if "view" in arguments and (
                snapshot["kind"] != "directory" or payload.get("view") != arguments["view"]
            ):
                raise NavigationError("REFERENCE_MISMATCH", "Snapshot belongs to another view")
        if name in {"getTable", "getFileDetails"}:
            source_file = payload.get("file", payload)
            if nav.canonical(arguments, "file") != source_file["fileId"]:
                raise NavigationError("REFERENCE_MISMATCH", "Cursor belongs to a different file")
        if name == "getTable" and nav.canonical(arguments, "table") != payload["tableId"]:
            raise NavigationError("REFERENCE_MISMATCH", "Cursor belongs to a different table")
        if (
            name == "researchReadEvidence"
            and nav.canonical(arguments, "evidence") != payload["evidenceId"]
        ):
            raise NavigationError("REFERENCE_MISMATCH", "Cursor belongs to different evidence")

    def _fetch_details(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] | None = None
        tables: list[Any] = []
        seen: set[str] = set()
        cursor: str | None = None
        for index in range(_MAX_DETAIL_PAGES):
            forwarded = {**arguments, "limit": 20}
            if cursor is not None:
                forwarded["cursor"] = cursor
            response = self._upstream_tool("getFileDetails", forwarded)
            raw = response.get("result")
            if not isinstance(raw, Mapping) or raw.get("isError"):
                return response
            payload, _ = recover_structured_content(raw)
            if payload is None:
                return response
            if index == 0 and payload.get("inventoryComplete") is True:
                return response
            if merged is None:
                merged = copy.deepcopy(dict(payload))
            elif merged.get("file") != payload.get("file"):
                raise NavigationError(
                    "REVISION_CONFLICT", "File identity changed during inventory pagination"
                )
            elif any(
                merged.get(key) != payload.get(key)
                for key in ("contractVersion", "tableExtraction")
            ):
                raise NavigationError(
                    "INVENTORY_CONTRACT_CHANGED",
                    "Source contract or extraction metadata changed during inventory pagination",
                )
            if not isinstance(payload.get("tables"), list):
                raise NavigationError("INVALID_INVENTORY", "Invalid table inventory")
            tables.extend(payload["tables"])
            if len(canonical_json(tables).encode()) > 8 * 1024 * 1024:
                raise NavigationError(
                    "INVENTORY_TOO_LARGE", "Inventory exceeds the private 8 MiB limit"
                )
            cursor = payload.get("nextCursor")
            if cursor is None:
                merged.update(
                    tables=tables,
                    nextCursor=None,
                    inventoryComplete=True,
                    inventoryPageCount=index + 1,
                    inventoryTableCount=len(tables),
                )
                return {"result": _tool_result(merged)}
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise NavigationError(
                    "INVALID_CURSOR", "Upstream inventory cursor repeated or is invalid"
                )
            seen.add(cursor)
        raise NavigationError("INVENTORY_TOO_LARGE", "Upstream inventory exceeded its page limit")

    def _prepare_reference_metadata(self, research_id: str) -> list[str]:
        for file_id in self.store.missing_reference_metadata(research_id):
            state = self.store.snapshot(research_id)
            request_hash = report_review_hash(state)
            previous = state.get("extensions", {}).get("metadataPreparation", {})
            if state["ledger"]["files"][file_id].get("metadataSource") == "getFileDetails" or (
                previous.get("reportHash") == request_hash and file_id in previous["attempts"]
            ):
                continue
            metadata_arguments = {"fileId": file_id, "limit": 1}
            try:
                response = self._upstream_tool("getFileDetails", metadata_arguments)
            except (OSError, RuntimeError):
                response = {}
            result = response.get("result")

            def on_commit(
                state: dict[str, Any], call: dict[str, Any], before_hash: str | None = None
            ) -> None:
                call["purpose"] = "bibliography_metadata"
                before_hash = before_hash or report_review_hash(state)
                if call["verificationStatus"] != "verified" and before_hash != request_hash:
                    return
                current = state.get("extensions", {}).get("metadataPreparation", {})
                attempts = (
                    copy.deepcopy(current["attempts"])
                    if current.get("reportHash") == before_hash
                    else {}
                )
                attempt = {
                    "callSequence": call["sequence"],
                    "verificationStatus": call["verificationStatus"],
                }
                if call["verificationStatus"] == "verified":
                    assert isinstance(result, Mapping)
                    structured, _ = recover_structured_content(result)
                    assert structured is not None
                    self._navigation(state).remember_file_metadata(file_id, structured["file"])
                else:
                    attempt["warning"] = (
                        "Metadata preparation attempted, not completed; "
                        "explicit getFileDetails can retry."
                    )
                attempts[file_id] = attempt
                state.setdefault("extensions", {})["metadataPreparation"] = {
                    "reportHash": report_review_hash(state),
                    "attempts": copy.deepcopy(attempts),
                }

            if isinstance(result, Mapping):

                def record(state: dict[str, Any]) -> dict[str, Any]:
                    # Match cached attempts before ingestion changes the hash, under the same
                    # lock; only this accepted metadata update may carry them to the new hash.
                    before_hash = report_review_hash(state)
                    return self.store._record_knowledge_call(
                        state,
                        research_id=research_id,
                        tool_name="getFileDetails",
                        arguments=metadata_arguments,
                        result=result,
                        metadata_only=True,
                        on_commit=lambda state, call: on_commit(state, call, before_hash),
                    )

                self.store.atomic_update(research_id, record)
            else:
                self.store.record_knowledge_error(
                    research_id=research_id,
                    tool_name="getFileDetails",
                    arguments=metadata_arguments,
                    error={"message": "Bibliography metadata unavailable"},
                    on_commit=on_commit,
                )
        unavailable = len(self.store.missing_reference_metadata(research_id))
        return (
            [
                f"Metadata preparation incomplete for {unavailable} cited files; "
                "unavailable metadata was not inferred. Automatic retry is deferred until "
                "report/source data changes; explicit getFileDetails can retry."
            ]
            if unavailable
            else []
        )

    def _call_local(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "researchBegin":
            payload = self.store.begin(
                title=_text_argument(arguments, "title"),
                subtitle=_optional_text_argument(arguments, "subtitle"),
                **{key: arguments[key] for key in ("mode", "language") if key in arguments},
            )
        elif name == "researchAddClaim":
            payload = self.store.add_claim(
                research_id=_text_argument(arguments, "researchId"),
                section=_text_argument(arguments, "section"),
                text=_text_argument(arguments, "text"),
                evidence_ids=arguments.get("evidenceIds", []),
                batch_key=arguments.get("batchKey"),
                claim_key=arguments.get("claimKey"),
                expected_claim_hash=arguments.get("expectedClaimHash"),
            )
        elif name == "researchAddClaims":
            payload = self.store.add_claims(
                research_id=_text_argument(arguments, "researchId"),
                claims=arguments.get("claims", []),
                batch_key=arguments.get("batchKey"),
            )
        elif name == "researchAddTable":
            payload = self.store.add_table(
                research_id=_text_argument(arguments, "researchId"),
                section=_text_argument(arguments, "section"),
                table_id=_text_argument(arguments, "tableId"),
                caption=_text_argument(arguments, "caption"),
                expected_table_hash=arguments.get("expectedTableHash"),
            )
        elif name == "researchFinalize":
            research_id = _text_argument(arguments, "researchId")
            pending = self.store.pending_review(research_id)
            if pending is not None:
                return _tool_result(pending)
            warnings = self._prepare_reference_metadata(research_id)
            payload = self.store.finalize(
                research_id=research_id,
                expected_claim_keys=arguments.get("expectedClaimKeys"),
                expected_table_ids=arguments.get("expectedTableIds"),
            )
            if warnings:
                payload["warnings"] = [*payload.get("warnings", []), *warnings]
        else:
            raise NavigationError("UNKNOWN_TOOL", "Unknown local research tool")
        return _tool_result(payload)

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
                    properties["requestKey"] = {"type": "string", "minLength": 1, "maxLength": 128}
                    required = [
                        key
                        for key in schema.get("required", [])
                        if key not in {"fileId", "fileIds", "tableId"}
                    ]
                    schema["required"] = list(dict.fromkeys([*required, "researchId"]))
                    if tool["name"] == "searchByIds":
                        properties.pop("fileIds", None)
                        properties["fileRefs"] = {
                            **_ref_array("D"),
                            "description": (
                                "Select 1-20 fileRefs returned in this research. Supply exactly "
                                "one of fileRefs or scopeRefs, never both. Do not invent IDs."
                            ),
                        }
                        properties["scopeRefs"] = {
                            **_ref_array("S"),
                            "description": (
                                "Select returned scopeRefs instead of fileRefs. Supply exactly "
                                "one of scopeRefs or fileRefs, never both. Do not invent IDs."
                            ),
                        }
                        schema["oneOf"] = [{"required": [key]} for key in ("fileRefs", "scopeRefs")]
                        tool["description"] = (
                            "Search selected files. Over 20 files returns group scopeRefs without "
                            "querying; explicitly search each group."
                        )
                    if tool["name"] in {"getFileDetails", "getTable"}:
                        properties.pop("fileId", None)
                        properties["fileRef"] = _ref_schema("D")
                        properties["cursor"] = {"type": "string", "minLength": 1, "maxLength": 512}
                        schema["required"].append("fileRef")
                        if tool["name"] == "getTable":
                            properties.pop("tableId", None)
                            properties["tableRef"] = _ref_schema("T")
                            schema["required"].append("tableRef")
                        else:
                            properties["limit"] = {"type": "integer", "minimum": 1, "maximum": 20}
                            tool["description"] = (
                                "Page a fixed extracted table inventory using nextCursor, "
                                "then getTable for selected tables."
                            )
                tools.append(tool)
        tools.extend(_research_tools())
        return tools


def _ref_schema(kind: str) -> dict[str, Any]:
    suffix = "[0-9a-f]{24}" if kind == "S" else "[1-9][0-9]*"
    return {
        "type": "string",
        "pattern": "^[A-Za-z0-9_-]{22}:" + kind + suffix + "$",
        "description": (
            "Copy an exact reference returned by a tool in this research. "
            "Never invent an ID or label."
        ),
    }


def _ref_array(kind: str) -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "maxItems": 20, "items": _ref_schema(kind)}


def _research_tools() -> list[dict[str, Any]]:
    research_id = {
        "type": "string",
        "pattern": "^kr_[0-9a-f]{32}$",
        "description": "Copy exactly from researchBegin.",
    }
    tools: list[dict[str, Any]] = [
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
                    "mode": {"type": "string", "enum": ["standard", "deep"], "default": "standard"},
                    "language": {"type": "string", "enum": ["zh-CN", "en"]},
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
    for tool in tools:
        schema = tool["inputSchema"]
        properties = schema["properties"]
        name = tool["name"]
        if name in {"researchAddClaim", "researchAddClaims"}:
            properties["batchKey"] = {"type": "string", "minLength": 1, "maxLength": 128}
            claim_schema = schema if name == "researchAddClaim" else properties["claims"]["items"]
            claim_properties = claim_schema["properties"]
            claim_properties["evidenceRefs"] = {**_ref_array("E"), "maxItems": 200}
            claim_properties.pop("evidenceIds")
            claim_properties["claimKey"] = {"type": "string", "minLength": 1, "maxLength": 128}
            claim_properties["expectedClaimHash"] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
            claim_schema["required"] = [
                key for key in claim_schema["required"] if key != "evidenceIds"
            ] + ["evidenceRefs"]
            tool["description"] = (
                "Atomically add paragraphs with exact returned evidenceRefs, not invented IDs; "
                "do not submit a References/bibliography section: it is generated automatically. "
                "batchKey makes committed retries idempotent. To revise a claimKey, supply its "
                "current expectedClaimHash from researchNavigate view=report."
            )
        elif name == "researchAddTable":
            properties["tableRef"] = _ref_schema("T")
            properties["expectedTableHash"] = {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
                "description": (
                    "To revise an already-added table's caption or section, copy its current "
                    "tableHash from researchNavigate view=report or review. Omit for a new "
                    "table or an identical retry. A stale hash is rejected."
                ),
            }
            properties.pop("tableId")
            schema["required"].remove("tableId")
            schema["required"].append("tableRef")
            tool["description"] += (
                " Same table, section and caption replays the existing item; changed metadata "
                "requires its current expectedTableHash."
            )
        elif name == "researchFinalize":
            properties["expectedClaimKeys"] = {
                "type": "array",
                "maxItems": 1000,
                "items": {"type": "string"},
            }
            properties["expectedTableRefs"] = {**_ref_array("T"), "minItems": 0, "maxItems": 1000}
    common = {
        "researchId": research_id,
        "requestKey": {"type": "string", "minLength": 1, "maxLength": 128},
        "cursor": {"type": "string", "minLength": 1, "maxLength": 512},
    }
    tools.extend(
        [
            {
                "name": "researchNavigate",
                "description": (
                    "Page fixed result snapshots, reference directories and progress. "
                    "view=report returns current paragraph keys/hashes/items and added table "
                    "refs, without paragraph text. Fresh view=review returns only pending "
                    "new/changed items; unchanged complete comparisons are reused. In deep "
                    "mode it first prepares cited file metadata. Cursor resumes the original "
                    "snapshot without fetching metadata."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["researchId"],
                    "properties": {
                        **common,
                        "view": {
                            "type": "string",
                            "enum": ["files", "scopes", "evidence", "progress", "report", "review"],
                        },
                        "snapshotRef": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                },
            },
            {
                "name": "researchReadEvidence",
                "description": (
                    "Read exact saved evidence text using nextCursor for long text. "
                    "No new upstream query."
                ),
                "inputSchema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["researchId", "evidenceRef"],
                    "properties": {
                        **common,
                        "evidenceRef": _ref_schema("E"),
                    },
                },
            },
        ]
    )
    return tools


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


def _projected_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": canonical_json(payload)}], "isError": False}


def _frame_bytes(payload: Mapping[str, Any]) -> int:
    return len((canonical_json(payload) + "\n").encode("utf-8"))


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
    if len(encoded) + 1 > MAX_FRAME_BYTES:
        raise ValueError("MCP frame exceeds 60 KiB")
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
    parser.add_argument(
        "--coverage-db",
        default=os.environ.get("OPENSQUILLA_KNOWLEDGE_COVERAGE_DB"),
        help="Read-only Knowledge database for bibliography text coverage",
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
    coverage_resolver = None
    if args.coverage_db:
        if __package__:
            from .reading_coverage import SQLiteReadingCoverage
        else:
            from reading_coverage import (  # type: ignore[import-not-found,no-redef]
                SQLiteReadingCoverage,
            )
        coverage_resolver = SQLiteReadingCoverage(args.coverage_db)
    upstream = SubprocessUpstream(args.upstream)
    bridge = KnowledgeResearchBridge(
        upstream,
        KnowledgeResearchStore(
            workspace=args.workspace,
            private_root=args.private_root,
            media_root=args.media_root,
            reading_coverage_resolver=coverage_resolver,
        ),
    )
    try:
        serve(bridge)
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
