"""One deterministic bibliography shared by rendering, counts and provenance."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Any

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?=$|[ +_])")
_TITLE_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_HASH_SUFFIX = re.compile(r"~[0-9a-f]{8,}$", re.I)
_IMPORT_SUFFIX = re.compile(r"(?:[ +](?:omni-\d+|[0-9a-f]{8}-[0-9a-f-]{8,}))$", re.I)
_PLACEHOLDER = re.compile(r"^\[?page\s+\d+\]?$|^Untitled local document$", re.I)
_CONVERTER = re.compile(r"^Generated locally by pdf-md-splitter(?:\b.*)?$", re.I)
_SERIES = re.compile(r"\b(?:weekly|monthly|quarterly|kickstart|strategy|outlook)\b", re.I)


class _TitleText(HTMLParser):
    _markup = frozenset({"p", "div", "span", "b", "strong", "i", "em", "h1", "h2", "br"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.suppressed: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.suppressed.append(tag)
        elif not self.suppressed:
            if tag not in self._markup:
                self.parts.append(self.get_starttag_text() or "")
            elif tag in {"p", "div", "h1", "h2", "br"}:
                self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.suppressed:
            if tag == self.suppressed[-1]:
                self.suppressed.pop()
        elif tag not in self._markup:
            self.parts.append(f"</{tag}>")
        elif tag in {"p", "div", "h1", "h2"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.suppressed:
            self.parts.append(data)


def _clean_title(value: str) -> str:
    if len(value) > 10_000:
        return ""
    # A truncated converter comment is not a usable title either.
    opening = value.rfind("<!--")
    if opening > value.rfind("-->"):
        value = value[:opening]
    parser = _TitleText()
    parser.feed(value)
    parser.close()
    lines = [line.strip() for line in "".join(parser.parts).splitlines()]
    cleaned = " ".join(" ".join(line.split()) for line in lines if not _CONVERTER.fullmatch(line))
    # BOM/zero-width transport artifacts are not visible bibliographic titles.
    cleaned = cleaned.translate(dict.fromkeys(map(ord, "\ufeff\u200b\u2060"))).strip()
    return "" if _PLACEHOLDER.fullmatch(cleaned) else cleaned


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
    stem = _clean_title(_clean(PurePosixPath(filename).stem))
    package = _package(record)
    if package and package[1] == "source_package_variants":
        stem = _clean(path.parent.name)
    title = _clean_title(str(record.get("title") or ""))
    # Storage folders are neither publisher identity nor publication-date evidence.
    date = stem[:10] if _DATE.match(stem) else ""
    if not title:
        title = stem or "Local document"
    elif (
        package
        and package[1] == "source_package_variants"
        and len(title) >= 20
        and len(stem) > len(title)
        and stem.casefold().startswith(title.casefold())
    ):
        # Expand only an exact truncated prefix from its verified document package.
        title = stem
    elif title.isupper() and len(title) < 70 and _SERIES.search(title):
        # A series title is not an issue title. Append a verified section only
        # when the source filename did not already provide the issue headline.
        if title.casefold() in stem.casefold() and len(stem) > len(title):
            title = stem
        sections = [
            _clean_title(str(locator.get("sectionPath", [""])[0]))
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
            title += f" (cited section: {heading})"
    if date and not _TITLE_DATE.search(title):
        title = f"{date} {title}"
    return title


def _label_rank(record: Mapping[str, Any]) -> tuple[int, str, str]:
    clean = _clean_title(str(record.get("title") or ""))
    series = clean.isupper() and len(clean) < 70 and _SERIES.search(clean)
    score = 0 if clean and not series else 1 if clean else 2
    label = _label(record)
    return score, label.casefold(), label


def _issue_dates(record: Mapping[str, Any]) -> set[str]:
    path = PurePosixPath(str(record.get("sourcePath") or ""))
    values = [
        path.stem,
        path.parent.name,
        str(record.get("filename") or ""),
    ]
    dates = {value[:10] for value in values if _DATE.match(value)}
    # Title dates can veto an existing lineage match, never establish identity.
    dates.update(_TITLE_DATE.findall(_clean_title(str(record.get("title") or ""))))
    return dates


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
        dates = set().union(*(_issue_dates(files[key]) for key in members))
        if len(members) == 2 and set(formats) == {"PDF", "Markdown"} and len(dates) <= 1:
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
    for reference in references:
        chosen = min(reference["members"], key=lambda member: _label_rank(files[member["fileId"]]))
        reference["title"] = _label(files[chosen["fileId"]])
        reference["titleSelection"] = {
            "fileId": chosen["fileId"],
            "basis": "clean_metadata_title"
            if _clean_title(str(files[chosen["fileId"]].get("title") or ""))
            else "source_filename_fallback",
        }
    return {
        "schemaVersion": "opensquilla-knowledge-bibliography/1",
        "references": references,
        "fileReferenceNumbers": numbers,
        "sourceFileCount": len(cited),
        "sourceCount": len(references),
    }
