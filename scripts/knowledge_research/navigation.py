"""Research-local references and immutable navigation snapshots (no independent I/O)."""

from __future__ import annotations

import base64
import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any, cast

if __package__:
    from .claims import claim_hash
    from .state import ResearchStateError, sha256_json
else:  # pragma: no cover - direct bridge entrypoint
    from claims import claim_hash  # type: ignore[import-not-found,no-redef]
    from state import ResearchStateError, sha256_json  # type: ignore[import-not-found,no-redef]

MAX_FRAME_BYTES = 60 * 1024
PAGE_BUDGET = MAX_FRAME_BYTES - 512
MAX_ITEMS = 20
SCHEMA_VERSION = "opensquilla-knowledge-navigation/1"


class NavigationError(ResearchStateError):
    def __init__(self, code: str, message: str, *, pointer: str = "", key: str = "") -> None:
        super().__init__(message)
        self.details = {"code": code, "pointer": pointer, "key": key}


def positive_limit(value: Any, default: int = MAX_ITEMS) -> int:
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= MAX_ITEMS:
        raise NavigationError("INVALID_LIMIT", "limit must be between 1 and 20", pointer="/limit")
    return value


def text(value: Any, pointer: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise NavigationError(
            "INVALID_ARGUMENT", "Expected a bounded non-empty string", pointer=pointer
        )
    return value


def choose(arguments: Mapping[str, Any], names: tuple[str, ...]) -> str:
    present = [name for name in names if name in arguments]
    if len(present) != 1:
        raise NavigationError("INVALID_SELECTOR", "Supply exactly one of " + ", ".join(names))
    return present[0]


def strings(value: Any, pointer: str, maximum: int = MAX_ITEMS) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise NavigationError(
            "INVALID_SELECTOR", f"Expected 1-{maximum} identifiers", pointer=pointer
        )
    return [text(item, f"{pointer}/{index}") for index, item in enumerate(value)]


class Navigation:
    """Mutations must execute inside the store's atomic_update/on_commit callbacks."""

    def __init__(self, state: dict[str, Any], caller_binding: str | None = None) -> None:
        self.state = state
        extensions = state.setdefault("extensions", {})
        namespace = (
            base64.urlsafe_b64encode(bytes.fromhex(state["researchId"][3:])).decode().rstrip("=")
        )
        self.data = extensions.setdefault(
            "navigation",
            {
                "schemaVersion": SCHEMA_VERSION,
                "namespace": namespace,
                "callerBinding": caller_binding,
                "files": {},
                "evidence": {},
                "tables": {},
                "scopes": {},
                "snapshots": {},
                "requests": {},
                "coverage": {},
                "projections": {},
            },
        )
        if (
            self.data.get("schemaVersion") != SCHEMA_VERSION
            or self.data.get("namespace") != namespace
        ):
            raise NavigationError("INVALID_STATE", "Unsupported navigation state")
        # This is an optional trusted-host binding, never a model-supplied credential.
        if self.data.get("callerBinding") != caller_binding:
            raise NavigationError("RESEARCH_NOT_FOUND", "Research is unavailable to this caller")
        self.namespace = namespace

    def reference(self, kind: str, canonical_id: str, record: Mapping[str, Any]) -> str:
        bucket_name, letter = {
            "file": ("files", "D"),
            "evidence": ("evidence", "E"),
            "table": ("tables", "T"),
        }[kind]
        bucket = self.data[bucket_name]
        identity = {
            "id": canonical_id,
            "fileId": record.get("fileId", canonical_id if kind == "file" else None),
            "documentId": record.get("documentId"),
            "revision": record.get("revision"),
        }
        if kind == "evidence":
            identity["contentSha256"] = record["contentSha256"]
        if canonical_id in bucket:
            existing = bucket[canonical_id]
            if any(existing.get(key) != value for key, value in identity.items()):
                raise NavigationError("REVISION_CONFLICT", "Reference source identity changed")
            return str(existing["ref"])
        ref = f"{self.namespace}:{letter}{len(bucket) + 1}"
        bucket[canonical_id] = {**identity, "ref": ref, "ordinal": len(bucket), "observed": False}
        return ref

    def resolve(self, kind: str, ref: Any) -> str:
        supplied = text(ref, f"/{kind}Ref")
        bucket = self.data[{"file": "files", "evidence": "evidence", "table": "tables"}[kind]]
        if supplied.startswith(self.namespace + ":"):
            for canonical_id, row in bucket.items():
                if row["ref"] == supplied:
                    self._validate_source(row)
                    return str(canonical_id)
        raise NavigationError(
            "UNKNOWN_REFERENCE", "Unknown or cross-research reference", key=supplied
        )

    def _validate_source(self, row: Mapping[str, Any]) -> None:
        source = self.state["ledger"]["files"].get(row.get("fileId"))
        if isinstance(source, Mapping) and source.get("revision") != row.get("revision"):
            raise NavigationError("REVISION_CONFLICT", "Reference source revision changed")

    def canonical(self, arguments: Mapping[str, Any], kind: str) -> str:
        ref_name, id_name = f"{kind}Ref", f"{kind}Id"
        name = choose(arguments, (ref_name, id_name))
        return (
            self.resolve(kind, arguments[name])
            if name == ref_name
            else text(arguments[name], f"/{name}")
        )

    def file_ref(self, file_id: str) -> str:
        record = self.state["ledger"]["files"].get(file_id)
        if not isinstance(record, Mapping):
            raise NavigationError("UNKNOWN_FILE", "File has not been returned by Knowledge")
        return self.reference("file", file_id, record)

    def scope(self, file_ids: list[str]) -> str:
        ordered = sorted(set(file_ids), key=lambda value: self.data["files"][value]["ordinal"])
        identities = [copy.deepcopy(self.data["files"][value]) for value in ordered]
        digest = sha256_json(identities)
        ref = f"{self.namespace}:S{digest[:24]}"
        self.data["scopes"].setdefault(
            ref,
            {
                "scopeRef": ref,
                "orderedIds": ordered,
                "sourceRevisions": [row["revision"] for row in identities],
                "hash": digest,
            },
        )
        return ref

    def selected_files(self, arguments: Mapping[str, Any]) -> list[str]:
        name = choose(arguments, ("fileIds", "fileRefs", "scopeRefs"))
        values = strings(arguments[name], f"/{name}")
        if name == "fileIds":
            return list(dict.fromkeys(values))
        if name == "fileRefs":
            selected = [self.resolve("file", value) for value in values]
        else:
            selected = []
            for ref in values:
                scope = self.data["scopes"].get(ref)
                if scope is None:
                    raise NavigationError(
                        "UNKNOWN_REFERENCE", "Unknown or cross-research scope", key=ref
                    )
                for file_id in scope["orderedIds"]:
                    self._validate_source(self.data["files"][file_id])
                    selected.append(file_id)
        if not selected:
            raise NavigationError("EMPTY_SCOPE", "An empty scope cannot be searched")
        return sorted(set(selected), key=lambda value: self.data["files"][value]["ordinal"])

    def snapshot(
        self, kind: str, payload: Mapping[str, Any], ordered_ids: list[str], revisions: list[Any]
    ) -> str:
        body = {
            "kind": kind,
            "payload": copy.deepcopy(dict(payload)),
            "orderedIds": ordered_ids,
            "sourceRevisions": revisions,
        }
        digest = sha256_json(body)
        ref = f"{self.namespace}:N{digest[:24]}"
        self.data["snapshots"].setdefault(ref, {**body, "snapshotRef": ref, "hash": digest})
        return ref

    def grouping(self, selected: list[str]) -> str:
        groups = []
        for offset in range(0, len(selected), MAX_ITEMS):
            members = selected[offset : offset + MAX_ITEMS]
            groups.append({"scopeRef": self.scope(members), "fileCount": len(members)})
        return self.snapshot(
            "groups",
            {"groups": groups, "totalFiles": len(selected)},
            selected,
            [self.data["files"][value]["revision"] for value in selected],
        )

    def observe(self, tool: str, payload: Mapping[str, Any]) -> str:
        if tool in {"search", "searchByIds"}:
            results = payload["results"]
            file_ids = []
            for item in results:
                file_id = item["fileId"]
                self.file_ref(file_id)
                self.data["files"][file_id]["observed"] = True
                self.reference(
                    "evidence",
                    item["evidenceId"],
                    self.state["ledger"]["evidence"][item["evidenceId"]],
                )
                file_ids.append(file_id)
            saved = {**copy.deepcopy(dict(payload)), "scopeRef": self.scope(file_ids)}
            return self.snapshot(
                "search",
                saved,
                [row["evidenceId"] for row in results],
                [row["revision"] for row in results],
            )
        if tool == "getFileDetails":
            file = payload["file"]
            self.file_ref(file["fileId"])
            for table in payload["tables"]:
                self.reference("table", table["tableId"], {**file, "fileId": file["fileId"]})
            return self.snapshot(
                "inventory",
                payload,
                [row["tableId"] for row in payload["tables"]],
                [file["revision"]] * len(payload["tables"]),
            )
        table_id = str(payload["tableId"])
        record = self.state["ledger"]["tables"][table_id]
        self.reference("table", table_id, record)
        if record["fileId"] in self.state["ledger"]["files"]:
            self.file_ref(record["fileId"])
        return self.snapshot("table", payload, [table_id], [record.get("revision")])

    def read_snapshot(self, arguments: Mapping[str, Any]) -> str:
        canonical_id = self.canonical(arguments, "evidence")
        record = self.state["ledger"]["evidence"].get(canonical_id)
        if not isinstance(record, Mapping) or record.get("verificationStatus") != "verified":
            raise NavigationError("UNKNOWN_EVIDENCE", "Evidence is not in this research ledger")
        self.file_ref(record["fileId"])
        self.reference("evidence", canonical_id, record)
        return self.snapshot("evidence", record, [canonical_id], [record["revision"]])

    def directory(self, view: str) -> str:
        entries: list[dict[str, Any]] = []
        ids: list[str] = []
        revisions: list[Any] = []
        metadata: dict[str, Any] = {}
        if view in {"files", "evidence"}:
            for canonical_id, row in self.data[view].items():
                entries.append(
                    {
                        "fileRef" if view == "files" else "evidenceRef": row["ref"],
                        "sourceRevision": row["revision"],
                    }
                )
                ids.append(canonical_id)
                revisions.append(row["revision"])
        elif view == "scopes":
            for ref, row in self.data["scopes"].items():
                entries.append(
                    {"scopeRef": ref, "fileCount": len(row["orderedIds"]), "hash": row["hash"]}
                )
                ids.append(ref)
                revisions.append(row["sourceRevisions"])
        elif view == "report":
            report = self.state["report"]
            metadata["reportRevision"] = report.get("revision", 0)
            for item in report["items"]:
                entry = {"kind": item["kind"], "item": item["itemId"], "section": item["section"]}
                revision: Any = metadata["reportRevision"]
                if item["kind"] == "claim":
                    entry.update(claimKey=item.get("claimKey"), claimHash=claim_hash(item))
                elif item["kind"] == "table":
                    table_id = item["tableId"]
                    table = self.state["ledger"]["tables"][table_id]
                    entry.update(
                        tableRef=self.reference("table", table_id, table),
                        caption=item["caption"],
                        sourceRevision=table["revision"],
                    )
                    revision = table["revision"]
                else:
                    raise NavigationError("INVALID_STATE", "Unknown report item kind")
                entries.append(entry)
                ids.append(item["itemId"])
                revisions.append(revision)
        elif view == "progress":
            for ref, row in self.data["snapshots"].items():
                if row["kind"] == "directory":
                    continue
                entries.append(
                    {
                        "snapshotRef": ref,
                        "kind": row["kind"],
                        "resumeCursor": self.cursor(ref, 0),
                        "hash": row["hash"],
                    }
                )
                ids.append(ref)
                revisions.append(row["sourceRevisions"])
            for row in self.data["requests"].values():
                if row["status"] == "in_doubt" and row.get("tool") in {
                    "search",
                    "searchByIds",
                    "getFileDetails",
                    "getTable",
                }:
                    entries.append({"requestKey": row["key"], "status": "in_doubt"})
                    ids.append(row["key"])
                    revisions.append(None)
        else:
            raise NavigationError("INVALID_VIEW", "Unknown navigation view", pointer="/view")
        return self.snapshot(
            "directory",
            {"view": view, "entries": entries, "progress": self.progress(), **metadata},
            ids,
            revisions,
        )

    def progress(self) -> dict[str, Any]:
        scoped: set[str] = set()
        for scope in self.data["scopes"].values():
            scoped.update(scope["orderedIds"])
        full_evidence = []
        full_files: set[str] = set()
        for evidence_id, ranges in self.data["coverage"].items():
            record = self.state["ledger"]["evidence"].get(evidence_id)
            if record and ranges == [[0, len(record["content"])]]:
                full_evidence.append(evidence_id)
                full_files.add(record["fileId"])
        return {
            "observedFileCount": sum(row["observed"] for row in self.data["files"].values()),
            "scopedFileCount": len(scoped),
            "readFileCount": len(full_files),
            "readEvidenceCount": len(full_evidence),
            "readMeaning": "complete_evidence_projection_prepared",
            "modelDelivery": "unknown",
            "sessionIsolation": "trusted_host_binding"
            if self.data["callerBinding"]
            else "not_available",
        }

    def cursor(self, snapshot_ref: str, start: int) -> str:
        snapshot = self.data["snapshots"][snapshot_ref]
        digest = sha256_json([snapshot_ref, snapshot["hash"], start])[:24]
        return f"{snapshot_ref}:{start}:{digest}"

    def from_cursor(self, value: Any) -> tuple[str, int]:
        supplied = text(value, "/cursor", 512)
        parts = supplied.rsplit(":", 2)
        try:
            snapshot_ref, raw_start, _ = parts
            start = int(raw_start)
            if (
                start < 0
                or snapshot_ref not in self.data["snapshots"]
                or self.cursor(snapshot_ref, start) != supplied
            ):
                raise ValueError
        except (ValueError, KeyError):
            raise NavigationError(
                "INVALID_CURSOR", "Unknown, altered or cross-research cursor"
            ) from None
        return snapshot_ref, start

    def begin_request(self, key: Any, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
        if key is None:
            return None
        request_key = text(key, "/requestKey", 128)
        fingerprint = sha256_json(arguments)
        slot = hashlib.sha256(request_key.encode()).hexdigest()
        previous = self.data["requests"].get(slot)
        if previous is not None:
            if previous["argumentsHash"] != fingerprint:
                raise NavigationError(
                    "REQUEST_KEY_CONFLICT",
                    "requestKey was used with different arguments",
                    key=request_key,
                )
            if previous["status"] != "committed":
                raise NavigationError(
                    "IN_DOUBT",
                    "Previous result is uncertain; explicitly use a new requestKey",
                    key=request_key,
                )
            return cast(dict[str, Any], copy.deepcopy(previous["reply"]))
        self.data["requests"][slot] = {
            "key": request_key,
            "argumentsHash": fingerprint,
            "status": "in_doubt",
            "tool": arguments.get("tool"),
        }
        return None

    def commit_request(self, key: Any, reply: Mapping[str, Any]) -> None:
        if key is not None:
            slot = hashlib.sha256(key.encode()).hexdigest()
            self.data["requests"][slot].update(status="committed", reply=copy.deepcopy(dict(reply)))

    def public(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.public(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {
                "documentId",
                "chunkId",
                "parentChunkId",
                "previousChunkId",
                "nextChunkId",
                "sourcePath",
                "dataBase64",
                "screenshotDataBase64",
                "localPath",
                "screenshotLocalPath",
                "screenshotPrivatePath",
                "url",
            }:
                continue
            if key in {"fileId", "evidenceId", "tableId"}:
                bucket_name, ref_name = {
                    "fileId": ("files", "fileRef"),
                    "evidenceId": ("evidence", "evidenceRef"),
                    "tableId": ("tables", "tableRef"),
                }[key]
                row = self.data[bucket_name].get(item)
                if row:
                    result[ref_name] = row["ref"]
            elif key == "continuationOf" and isinstance(item, str):
                row = self.data["tables"].get(item)
                result[key] = row["ref"] if row else None
            else:
                result[key] = self.public(item)
        return result

    def wrap_page(self, snapshot_ref: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = self.data["snapshots"][snapshot_ref]
        projected: dict[str, Any] = self.public(payload)
        page = projected["page"]
        projected.update(
            snapshotRef=snapshot_ref,
            snapshotHash=snapshot["hash"],
            modelDelivery="unknown",
            projectionPrepared=True,
            projectionComplete=projected.get("projectionComplete", True)
            and page["start"] == 0
            and page["end"] == page["total"],
            nextCursor=self.cursor(snapshot_ref, page["end"]) if page["hasMore"] else None,
        )
        return projected

    def record_projection(self, snapshot_ref: str, payload: Mapping[str, Any]) -> None:
        page = payload["page"]
        digest = sha256_json(payload)
        snapshot = self.data["snapshots"][snapshot_ref]
        start, end = page["start"], page["end"]
        if snapshot["kind"] == "groups":
            # The public range counts groups; the private receipt identifies their members.
            selected_groups = snapshot["payload"]["groups"][start:end]
            ordered_ids = [
                file_id
                for group in selected_groups
                for file_id in self.data["scopes"][group["scopeRef"]]["orderedIds"]
            ]
            revisions_by_id = dict(
                zip(snapshot["orderedIds"], snapshot["sourceRevisions"], strict=True)
            )
            revisions = [revisions_by_id[file_id] for file_id in ordered_ids]
        elif snapshot["kind"] in {"evidence", "table"}:
            ordered_ids, revisions = snapshot["orderedIds"], snapshot["sourceRevisions"]
        else:
            ordered_ids = snapshot["orderedIds"][start:end]
            revisions = snapshot["sourceRevisions"][start:end]
        intervals: list[tuple[str, int, int]] = []
        if snapshot["kind"] == "evidence":
            intervals.append((snapshot["orderedIds"][0], page["start"], page["end"]))
        elif snapshot["kind"] == "search":
            for item in payload["results"]:
                evidence_id = self.resolve("evidence", item["evidenceRef"])
                intervals.append(
                    (evidence_id, item["contentRange"]["start"], item["contentRange"]["end"])
                )
        self.data["projections"].setdefault(
            digest,
            {
                "snapshotRef": snapshot_ref,
                "hash": digest,
                "range": copy.deepcopy(page),
                "orderedIds": copy.deepcopy(ordered_ids),
                "sourceRevisions": copy.deepcopy(revisions),
                "contentRanges": [
                    {"evidenceId": evidence_id, "start": left, "end": right}
                    for evidence_id, left, right in intervals
                ],
                "projectionPrepared": True,
                "modelDelivery": "unknown",
            },
        )
        for evidence_id, start, end in intervals:
            if start == end:
                continue
            ranges = sorted([*self.data["coverage"].get(evidence_id, []), [start, end]])
            merged: list[list[int]] = []
            for left, right in ranges:
                if merged and left <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], right)
                else:
                    merged.append([left, right])
            self.data["coverage"][evidence_id] = merged


def item_page(
    items: list[Any],
    start: int,
    limit: int,
    build: Callable[[list[Any], dict[str, Any]], dict[str, Any]],
    frame_bytes: Callable[[Mapping[str, Any]], int],
) -> dict[str, Any]:
    if not 0 <= start <= len(items):
        raise NavigationError("INVALID_CURSOR", "Page offset is out of range")
    selected: list[Any] = []
    for item in items[start : start + limit]:
        candidate = selected + [item]
        page = {
            "start": start,
            "end": start + len(candidate),
            "total": len(items),
            "hasMore": start + len(candidate) < len(items),
        }
        if frame_bytes(build(candidate, page)) > PAGE_BUDGET:
            break
        selected = candidate
    if not selected and start < len(items):
        raise NavigationError("PROJECTION_TOO_LARGE", "A directory item exceeds the frame budget")
    page = {
        "start": start,
        "end": start + len(selected),
        "total": len(items),
        "hasMore": start + len(selected) < len(items),
    }
    return build(selected, page)


def content_page(
    content: str,
    start: int,
    build: Callable[[str, dict[str, Any]], dict[str, Any]],
    frame_bytes: Callable[[Mapping[str, Any]], int],
) -> dict[str, Any]:
    if not 0 <= start <= len(content):
        raise NavigationError("INVALID_CURSOR", "Content offset is out of range")
    low, high = start, len(content)
    while low < high:
        end = (low + high + 1) // 2
        candidate = build(
            content[start:end],
            {"start": start, "end": end, "total": len(content), "hasMore": end < len(content)},
        )
        if frame_bytes(candidate) <= PAGE_BUDGET:
            low = end
        else:
            high = end - 1
    if low == start and start < len(content):
        raise NavigationError("PROJECTION_TOO_LARGE", "Content metadata exceeds the frame budget")
    return build(
        content[start:low],
        {"start": start, "end": low, "total": len(content), "hasMore": low < len(content)},
    )
