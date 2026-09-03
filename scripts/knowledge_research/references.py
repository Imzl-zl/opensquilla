"""One deterministic bibliography shared by rendering, counts and provenance."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?=$|[ +_])")
_HASH_SUFFIX = re.compile(r"~[0-9a-f]{8,}$", re.I)
_IMPORT_SUFFIX = re.compile(r"(?:[ +](?:omni-\d+|[0-9a-f]{8}-[0-9a-f-]{8,}))$", re.I)
_PLACEHOLDER = re.compile(r"^\[?page\s+\d+\]?$|^Untitled local document$", re.I)


def cited_file_ids(state: Mapping[str, Any]) -> list[str]:
    ledger = state["ledger"]
    ordered: dict[str, None] = {}
    for item in state["report"]["items"]:
        records = (
            [ledger["evidence"][key] for key in item["evidenceIds"]]
            if item["kind"] == "claim"
            else [ledger["tables"][item["tableId"]]]
        )
        for record in records:
            ordered[str(record["fileId"])] = None
    return list(ordered)


def source_format(record: Mapping[str, Any]) -> str:
    suffix = PurePosixPath(
        str(record.get("filename") or record.get("sourcePath") or "")
    ).suffix.lower()
    media = str(record.get("mediaType") or "").lower()
    if suffix == ".pdf" or media == "application/pdf":
        return "PDF"
    if suffix in {".md", ".markdown"} or media in {"text/markdown", "text/x-markdown"}:
        return "Markdown"
    return "text"


def _clean(value: str) -> str:
    value = _HASH_SUFFIX.sub("", value)
    value = _IMPORT_SUFFIX.sub("", value)
    return " ".join(value.replace("+", " ").replace("_", " ").split()).strip()


def _package(record: Mapping[str, Any]) -> tuple[str, str] | None:
    if record.get("verificationStatus") != "verified":
        return None
    path = PurePosixPath(str(record.get("sourcePath") or ""))
    parent = path.parent.name
    # The importer sometimes truncates basenames with a hash; only the full,
    # document-specific parent is a reliable cross-format lineage in that case.
    stem = _HASH_SUFFIX.sub("", path.stem)
    if _DATE.match(parent) and len(parent) > 40 and len(stem) >= 40 and parent.startswith(stem):
        return str(path.parent), "source_package_variants"
    if (
        path.suffix.lower() in {".pdf", ".md", ".markdown"}
        and _DATE.match(path.stem)
        and len(path.stem) > 40
    ):
        return str(path.with_suffix("")), "matching_source_stem"
    return None


def _label(record: Mapping[str, Any]) -> str:
    path = PurePosixPath(str(record.get("sourcePath") or ""))
    filename = str(record.get("filename") or path.name)
    stem = _clean(PurePosixPath(filename).stem)
    package = _package(record)
    if package and package[1] == "source_package_variants":
        stem = _clean(path.parent.name)
    title = _clean(str(record.get("title") or ""))
    if _PLACEHOLDER.fullmatch(title):
        title = ""
        if _DATE.match(path.parent.name) and len(path.parent.name) > 40:
            stem = _clean(path.parent.name)
    date = next((part[:10] for part in [stem, *reversed(path.parts)] if _DATE.match(part)), "")
    if not title:
        title = stem or "Local document"
    elif title.casefold() in stem.casefold() or _DATE.match(title):
        title = stem if len(stem) >= len(title) else title
    elif title.isupper() and len(title) < 70:
        # A series title is not an issue title. Append a verified section only
        # when the source filename did not already provide the issue headline.
        sections = [
            str(locator.get("sectionPath", [""])[0])
            for locator in record.get("observedLocators", [])
            if locator.get("sectionPath")
        ]
        heading = next(
            (
                part
                for part in sections
                if 40 < len(part) < 250 and not part.startswith(("Exhibit", "%"))
            ),
            "",
        )
        if heading and heading.casefold() not in title.casefold():
            title += f" (cited section: {_clean(heading)})"
    if date and not title.startswith(date):
        title = f"{date} {title}"
    if path.parts and path.parts[0] == "goldman" and "goldman sachs" not in title.lower():
        title = f"Goldman Sachs | {title}"
    return title


def build_bibliography(state: Mapping[str, Any]) -> dict[str, Any]:
    files = state["ledger"]["files"]
    cited = cited_file_ids(state)
    candidates: dict[str, list[str]] = defaultdict(list)
    for file_id in cited:
        package = _package(files[file_id])
        if package is not None:
            candidates[package[0]].append(file_id)
    groups: dict[str, tuple[str, str]] = {}
    for members in candidates.values():
        formats = [source_format(files[key]) for key in members]
        # Ambiguous multiple versions remain separate, even in the same package.
        if len(members) == 2 and set(formats) == {"PDF", "Markdown"}:
            package = _package(files[members[0]])
            assert package is not None
            for key in members:
                groups[key] = (members[0], package[1])
    numbers: dict[str, int] = {}
    references: list[dict[str, Any]] = []
    group_numbers: dict[str, int] = {}
    for file_id in cited:
        group, basis = groups.get(file_id, (file_id, "distinct_source_file"))
        if group not in group_numbers:
            group_numbers[group] = len(references) + 1
            references.append(
                {
                    "number": len(references) + 1,
                    "title": _label(files[file_id]),
                    "members": [],
                    "groupingBasis": basis,
                }
            )
        number = group_numbers[group]
        numbers[file_id] = number
        reference = references[number - 1]
        record = files[file_id]
        reference["members"].append(
            {
                "fileId": file_id,
                "documentId": record.get("documentId"),
                "revision": record.get("revision"),
                "format": source_format(record),
                "sourcePath": record.get("sourcePath"),
            }
        )
        # Prefer a meaningful issue title over a generic series label.
        label = _label(record)
        first = files[reference["members"][0]["fileId"]]
        if _PLACEHOLDER.fullmatch(str(first.get("title") or "")) and not _PLACEHOLDER.fullmatch(
            str(record.get("title") or "")
        ):
            reference["title"] = label
    return {
        "schemaVersion": "opensquilla-knowledge-bibliography/1",
        "references": references,
        "fileReferenceNumbers": numbers,
        "sourceFileCount": len(cited),
        "sourceCount": len(references),
    }
