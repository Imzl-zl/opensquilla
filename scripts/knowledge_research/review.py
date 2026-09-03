"""Bounded source comparisons and observable review preparation, never fact checking."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import PurePosixPath
from typing import Any

if __package__:
    from .claims import canonical_json, claim_hash, sha256_json
    from .locator import source_locator as review_locator
    from .numeric_checks import numeric_scale_checks
    from .references import _clean_title, source_format
    from .table_views import _source_text, table_quality_view
else:  # pragma: no cover - standalone bridge
    from claims import (  # type: ignore[import-not-found,no-redef]
        canonical_json,
        claim_hash,
        sha256_json,
    )
    from locator import source_locator as review_locator  # type: ignore[import-not-found,no-redef]
    from numeric_checks import numeric_scale_checks  # type: ignore[import-not-found,no-redef]
    from references import _clean_title, source_format  # type: ignore[import-not-found,no-redef]
    from table_views import (  # type: ignore[import-not-found,no-redef]
        _source_text,
        table_quality_view,
    )

FRAGMENT_CHARS = 2_000
MAX_METADATA_BYTES = 12_000
Reference = Callable[[str, str, Mapping[str, Any]], str]
LocatorProjection = Callable[[Mapping[str, Any], str], dict[str, Any]]


def review_source_metadata(state: Mapping[str, Any], file_id: str) -> dict[str, Any]:
    file = state["ledger"]["files"][file_id]
    remembered = state.get("extensions", {}).get("navigation", {}).get("fileMetadata", {})
    known = {
        **remembered.get(file_id, {}),
        **{key: value for key, value in file.items() if value not in (None, "")},
    }
    raw_title = str(file.get("title") or "")
    filename = PurePosixPath(str(known.get("filename") or "")).name
    title = _clean_title(raw_title)
    if not title and len(raw_title) > 10_000:
        title = raw_title
    result: dict[str, Any] = {"title": title or filename, "sourceFormat": source_format(known)}
    if filename:
        result["filename"] = filename
    for key in ("institution", "publicationDate"):
        value = known.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value
    return result


def table_item_hash(item: Mapping[str, Any]) -> str:
    return sha256_json({name: item[name] for name in ("section", "caption", "tableId")})


def report_review_hash(state: Mapping[str, Any]) -> str:
    ledger = state["ledger"]
    items = state["report"]["items"]
    evidence_ids = sorted(
        {key for item in items if item["kind"] == "claim" for key in item["evidenceIds"]}
    )
    table_ids = sorted({item["tableId"] for item in items if item["kind"] == "table"})
    file_ids = sorted(
        {ledger["evidence"][key]["fileId"] for key in evidence_ids}
        | {ledger["tables"][key]["fileId"] for key in table_ids}
    )
    return sha256_json(
        {
            "protocol": "source-comparison/3",
            "report": items,
            "files": [
                {
                    "identity": {
                        name: ledger["files"][file_id].get(name)
                        for name in ("fileId", "documentId", "revision", "sourcePath")
                    },
                    "metadata": review_source_metadata(state, file_id),
                }
                for file_id in file_ids
            ],
            "evidence": [
                {
                    name: ledger["evidence"][key].get(name)
                    for name in (
                        "evidenceId",
                        "fileId",
                        "documentId",
                        "revision",
                        "title",
                        "content",
                        "contentSha256",
                        "contentKind",
                        "locator",
                    )
                }
                for key in evidence_ids
            ],
            "tables": [
                {
                    "identity": {
                        name: ledger["tables"][table_id].get(name)
                        for name in (
                            "tableId",
                            "fileId",
                            "documentId",
                            "revision",
                            "page",
                            "locator",
                        )
                    },
                    "text": _source_text(ledger["tables"][table_id]),
                    "screenshotSha256": ledger["tables"][table_id]
                    .get("screenshot", {})
                    .get("sha256"),
                    "quality": table_quality_view(
                        ledger["tables"][table_id],
                        assessment=ledger.get("tableAssessments", {}).get(table_id),
                    ),
                }
                for table_id in table_ids
            ],
        }
    )


def _fragments(metadata: dict[str, Any], content: str) -> list[dict[str, Any]]:
    encoded = canonical_json(metadata)
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        # Metadata participates in the same bounded, mandatory review pagination as prose.
        identity = {
            key: metadata[key]
            for key in ("item", "forClaimItem", "evidenceRef", "tableRef", "fileRef")
            if key in metadata
        }
        digest = sha256_json(metadata)
        return _fragments(
            {
                **identity,
                "kind": "metadata",
                "forKind": metadata["kind"],
                "metadataSha256": digest,
                "format": "application/json",
            },
            encoded,
        ) + _fragments(
            {
                **identity,
                "kind": metadata["kind"],
                "metadataSha256": digest,
                "metadataComplete": False,
            },
            content,
        )
    return [
        {
            **metadata,
            "content": content[start : start + FRAGMENT_CHARS],
            "contentRange": {
                "start": start,
                "end": min(start + FRAGMENT_CHARS, len(content)),
                "total": len(content),
            },
        }
        for start in range(0, max(1, len(content)), FRAGMENT_CHARS)
    ]


def review_entries(
    state: Mapping[str, Any],
    reference: Reference,
    *,
    locator_projection: LocatorProjection = review_locator,
) -> dict[str, Any]:
    ledger = state["ledger"]
    entries: list[dict[str, Any]] = []

    def append_evidence(key: str, claim_item: str) -> None:
        record = ledger["evidence"][key]
        file = ledger["files"][record["fileId"]]
        metadata = review_source_metadata(state, record["fileId"])
        if not metadata["title"]:
            metadata["title"] = _clean_title(str(record.get("title") or ""))
        entries.extend(
            _fragments(
                {
                    "kind": "evidence",
                    "forClaimItem": claim_item,
                    "evidenceRef": reference("evidence", key, record),
                    "fileRef": reference("file", record["fileId"], file),
                    **metadata,
                    "locator": locator_projection(record.get("locator", {}), metadata["title"]),
                },
                record["content"],
            )
        )

    for item in state["report"]["items"]:
        if item["kind"] == "claim":
            refs = []
            for key in item["evidenceIds"]:
                ref = reference("evidence", key, ledger["evidence"][key])
                refs.append(ref)
            entries.extend(
                _fragments(
                    {
                        "kind": "claim",
                        "claimKey": item.get("claimKey"),
                        "item": item["itemId"],
                        "claimHash": claim_hash(item),
                        "section": item["section"],
                        "evidenceRefs": refs,
                    },
                    item["text"],
                )
            )
            for key in item["evidenceIds"]:
                append_evidence(key, item["itemId"])
        elif item["kind"] == "table":
            table = ledger["tables"][item["tableId"]]
            file = ledger["files"][table["fileId"]]
            metadata = review_source_metadata(state, table["fileId"])
            table_text, table_format, completeness = _source_text(table)
            entries.extend(
                _fragments(
                    {
                        "kind": "table",
                        "item": item["itemId"],
                        "tableHash": table_item_hash(item),
                        "tableRef": reference("table", item["tableId"], table),
                        "fileRef": reference("file", table["fileId"], file),
                        **metadata,
                        "caption": item["caption"],
                        "page": table.get("page"),
                        "locator": locator_projection(table.get("locator", {}), metadata["title"]),
                        "format": table_format,
                        "inputCompleteness": completeness,
                        "quality": table_quality_view(
                            table,
                            assessment=ledger.get("tableAssessments", {}).get(item["tableId"]),
                        ),
                        "visualCheck": "not_performed_by_this_view",
                    },
                    table_text,
                )
            )
    return {
        "reportHash": report_review_hash(state),
        "entries": entries,
        "scope": "submitted_claims_and_cited_excerpts_not_full_documents",
        "semanticVerification": "not_performed_by_service",
        "instruction": (
            "Exact cited excerpts immediately follow their paragraph, including shared "
            "sources, linked by forClaimItem. Check each paragraph as its "
            "sources arrive and record mismatches before requesting the next page. "
            "Compare each assertion with its cited excerpt: attribution, number, unit, date, "
            "forecast horizon and direction. Follow all pages; revise wrong text or references "
            "with the current claimHash. Search scoped files for missing context. "
            "For metadata entries, join JSON content ranges with the same metadataSha256 "
            "and parse the result for that item's full source information. "
            "Bibliography metadata completion may require one fresh comparison before finalize. "
            "A complete projection does not certify accurate interpretation."
        ),
    }


def review_preparation(state: Mapping[str, Any]) -> dict[str, Any]:
    digest = report_review_hash(state)
    nav = state.get("extensions", {}).get("navigation", {})
    complete = False
    for ref, snapshot in nav.get("snapshots", {}).items():
        payload = snapshot.get("payload", {})
        if payload.get("view") != "review" or payload.get("reportHash") != digest:
            continue
        total = len(payload.get("entries", []))
        ranges = sorted(
            (row["range"]["start"], row["range"]["end"])
            for row in nav.get("projections", {}).values()
            if row.get("snapshotRef") == ref
        )
        covered = 0
        for start, end in ranges:
            if start > covered:
                break
            covered = max(covered, end)
        if total and covered >= total:
            complete = True
            break
    scoped_calls = sum(
        call.get("toolName") == "searchByIds" and call.get("verificationStatus") == "verified"
        for call in state["ledger"]["calls"]
    )
    return {
        "reportHash": digest,
        "comparisonPrepared": complete,
        "scopedSearchCallCount": scoped_calls,
        "semanticVerification": "not_performed_by_service",
    }


def review_requirements(state: Mapping[str, Any]) -> dict[str, Any] | None:
    if state.get("mode") != "deep" or not state["report"]["items"]:
        return None
    preparation = review_preparation(state)
    pending: list[dict[str, Any]] = numeric_scale_checks(state)
    if not preparation["scopedSearchCallCount"]:
        pending.append(
            {
                "code": "SCOPED_SEARCH_REQUIRED",
                "tool": "searchByIds",
                "message": (
                    "Deep research has not searched within any candidate file. "
                    "Use a relevant scopeRefs or fileRefs selection. A completed "
                    "empty-result search counts; a grouping response does not."
                ),
            }
        )
    if not preparation["comparisonPrepared"]:
        pending.append(
            {
                "code": "SOURCE_COMPARISON_REQUIRED",
                "tool": "researchNavigate",
                "arguments": {"researchId": state["researchId"], "view": "review"},
                "message": (
                    "Obtain all comparison pages for the current report, check attribution, "
                    "numbers, units, horizons and direction against each cited source. "
                    "Revise incorrect text or references before finalizing. "
                    "If bibliography metadata was completed after review, obtain a fresh "
                    "comparison of the now-known sources, then finalize again. "
                    "Material delivery is not semantic verification."
                ),
            }
        )
    return {"status": "needs_review", "checks": pending, "review": preparation} if pending else None
