"""Bounded, pure table views; projection coverage is not reading or OCR quality."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.rules_block.table import escapedSplit

JSON = Mapping[str, Any]
FrameBytes = Callable[[JSON], int]
MAX_FRAME_BYTES = 60 * 1024
MAX_PARSE_CHARS = 262_144
MAX_PARSE_EVENTS = 20_000
MAX_PARSE_DEPTH = 32
MAX_PARSE_ROWS = 1_024
MAX_PARSE_CELLS = 8_192
MAX_SPAN = 128
MAX_SPAN_AREA = 65_536
_MAX_TABLES = 8
_UNIT = re.compile(r"\b(?:units?|currency|bps|USD|KRW|EUR|JPY|CNY)\b|[%$]|单位|單位", re.I)
_UNIT_LABEL = re.compile(r"\b(?:units?|currency)\s*[:(]|单位|單位", re.I)


class TableViewError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ParseLimitError(Exception):
    pass


class _TableParser(HTMLParser):
    """Keep source cells and spans, never allocate a span-expanded grid."""

    _void = frozenset({"br", "hr", "img", "input", "meta", "link", "col", "wbr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict[str, Any]] = []
        self.issues: list[str] = []
        self.stack: list[str] = []
        self.events = self.rows = self.cells = 0
        self.span_area = 0
        self.current: dict[str, Any] | None = None
        self.row: dict[str, Any] | None = None
        self.cell: dict[str, Any] | None = None
        self.caption: list[str] | None = None
        self.outside_parts: list[str] = []
        self.outside_context: list[str] = []

    def issue(self, code: str) -> None:
        if code not in self.issues:
            self.issues.append(code)

    def stop(self, code: str) -> None:
        self.issue(code)
        raise _ParseLimitError

    def event(self) -> None:
        self.events += 1
        if self.events > MAX_PARSE_EVENTS:
            self.stop("parse_event_limit")

    def finish_cell(self) -> None:
        if self.cell is not None and self.row is not None:
            self.cell["text"] = "".join(self.cell.pop("parts")).strip()
            self.row["cells"].append(self.cell)
        self.cell = None

    def finish_row(self) -> None:
        self.finish_cell()
        if self.row is not None and self.current is not None:
            self.current["rows"].append(self.row)
        self.row = None

    def finish_outside_context(self) -> None:
        value = "".join(self.outside_parts).strip()
        if value:
            self.outside_context.append(value)
        self.outside_parts = []

    def span(self, attrs: dict[str, str | None], name: str) -> int:
        raw = attrs.get(name)
        if raw is None:
            return 1
        if not raw.isascii() or not raw.isdigit() or len(raw) > 3:
            self.stop("invalid_or_excessive_span")
        value = int(raw)
        if not 1 <= value <= MAX_SPAN:
            self.stop("invalid_or_excessive_span")
        return value

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.event()
        suppressed = any(item in {"script", "style", "template"} for item in self.stack)
        if tag not in self._void:
            if len(self.stack) >= MAX_PARSE_DEPTH:
                self.stop("parse_depth_limit")
            self.stack.append(tag)
        if suppressed or tag in {"script", "style", "template"}:
            return
        # HTML consumers keep the first duplicate; dict(attrs) would keep the last.
        for span_name in ("rowspan", "colspan"):
            if sum(name == span_name for name, _ in attrs) > 1:
                self.stop("duplicate_span_attribute")
        if tag == "table":
            self.finish_outside_context()
            if self.current is not None:
                self.stop("nested_table_unsupported")
            if len(self.tables) >= _MAX_TABLES:
                self.stop("parse_table_limit")
            self.current = {"rows": [], "titleRaw": "", "context": []}
            self.tables.append(self.current)
        elif tag == "caption" and self.current is not None:
            self.caption = []
        elif tag == "tr" and self.current is not None:
            if self.row is not None:
                self.issue("implicit_row_close")
            self.finish_row()
            self.rows += 1
            if self.rows > MAX_PARSE_ROWS:
                self.stop("parse_row_limit")
            self.row = {
                "rowIndex": len(self.current["rows"]),
                "cells": [],
                "inThead": "thead" in self.stack,
            }
        elif tag in {"td", "th"} and self.current is not None:
            if self.row is None:
                self.issue("cell_outside_row")
                return
            if self.cell is not None:
                self.issue("implicit_cell_close")
                self.finish_cell()
            self.cells += 1
            if self.cells > MAX_PARSE_CELLS:
                self.stop("parse_cell_limit")
            attributes = dict(attrs)
            colspan = self.span(attributes, "colspan")
            rowspan = self.span(attributes, "rowspan")
            self.span_area += colspan * rowspan
            if self.span_area > MAX_SPAN_AREA:
                self.stop("parse_span_area_limit")
            self.cell = {
                "tag": tag,
                "parts": [],
                "colspan": colspan,
                "rowspan": rowspan,
                "alignment": {
                    "text-align:left": "left",
                    "text-align:center": "center",
                    "text-align:right": "right",
                }.get(str(attributes.get("style"))),
            }
        elif tag == "br":
            self.handle_data("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self.event()
        suppressed = any(item in {"script", "style", "template"} for item in self.stack)
        if not suppressed:
            if tag in {"td", "th"}:
                self.finish_cell()
            elif tag == "tr":
                self.finish_row()
            elif tag == "caption" and self.caption is not None:
                if self.current is not None:
                    self.current["titleRaw"] = "".join(self.caption).strip()
                self.caption = None
            elif tag == "table":
                self.finish_row()
                self.current = None
            elif tag in {"p", "div"} and self.current is None:
                self.finish_outside_context()
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            if index != len(self.stack) - 1:
                self.issue("unbalanced_markup")
            del self.stack[index:]
        elif tag not in self._void:
            self.issue("unbalanced_markup")

    def handle_data(self, data: str) -> None:
        self.event()
        if any(item in {"script", "style", "template"} for item in self.stack):
            return
        if self.cell is not None:
            self.cell["parts"].append(data)
        elif self.caption is not None:
            self.caption.append(data)
        elif data.strip() and self.current is not None:
            self.current["context"].append(data.strip())
        elif self.current is None:
            self.outside_parts.append(data)
            if data.strip():
                self.issue("outside_table_text")

    def handle_comment(self, data: str) -> None:
        self.event()


def _source_text(table: JSON, projection: str | None = None) -> tuple[str, str, str]:
    payload = table.get("text")
    text = payload if isinstance(payload, Mapping) else {}
    content = text.get("content")
    preview = not isinstance(content, str)
    if preview:
        content = table.get("textPreview")
    if not isinstance(content, str):
        return "", str(text.get("format") or table.get("textFormat") or ""), "unavailable"
    fmt = str(text.get("format") or table.get("textFormat") or "")
    try:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return "", fmt, "unavailable"
    flags = (
        text.get("truncated"),
        text.get("contentTruncatedForTransport"),
        table.get("textTruncated"),
        table.get("textPreviewTruncatedForTransport"),
    )
    mode = projection or table.get("tableTextProjection")
    if mode is not None and not isinstance(mode, str):
        return content, fmt, "unknown"
    if any(value is True for value in flags) or mode in {"preview", "metadata-only"}:
        status = "truncated"
    elif preview or mode == "compact-preview":
        status = "unknown"
    elif text.get("sha256") is not None and text.get("sha256") != digest:
        status = "unknown"
    elif text.get("truncated") is False or text.get("sha256") == digest:
        status = "complete"
    else:
        status = "unknown"
    return content, fmt, status


def markdown_table_html(content: str) -> tuple[str | None, list[str]]:
    """Parse with GFM tokens, then validate generated HTML before either caller uses it."""
    if len(content) > MAX_PARSE_CHARS:
        return None, ["parse_character_limit"]
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines:
        return None, []
    # An allocation bound only: escaped pipes overestimate it, but do not define cells.
    grid_bound = (max(line.count("|") for line in lines) + 1) * len(lines)
    if 3 * grid_bound + 2 * len(lines) > MAX_PARSE_EVENTS:
        return None, ["markdown_allocation_limit"]
    md = MarkdownIt("commonmark", {"maxNesting": MAX_PARSE_DEPTH}).enable("table")
    tokens = md.parse(content)
    issues: list[str] = []
    table_count = rows = cells = events = row_cells = source_cells = 0
    in_table = False

    def issue(code: str) -> None:
        if code not in issues:
            issues.append(code)

    for token in tokens:
        events += 1
        if token.type == "table_open":
            if in_table or token.level != 0:
                issue("nested_markdown_table_unsupported")
            table_count += 1
            if table_count > _MAX_TABLES:
                return None, [*issues, "parse_table_limit"]
            in_table = True
        elif token.type == "table_close":
            in_table = False
        elif token.type == "tr_open":
            rows += 1
            row_cells = 0
            source_cells = 0
            if token.map:
                columns = escapedSplit(lines[token.map[0]].strip())
                if columns and columns[0] == "":
                    columns.pop(0)
                if columns and columns[-1] == "":
                    columns.pop()
                source_cells = len(columns)
        elif token.type == "tr_close" and row_cells != source_cells:
            issue("markdown_row_width_normalized")
        elif token.type in {"th_open", "td_open"}:
            cells += 1
            row_cells += 1
        elif token.type == "inline":
            if not in_table and token.content.strip():
                issue("non_table_markdown")
            pending = list(token.children or [])
            while pending:
                child = pending.pop()
                events += 1
                if events > MAX_PARSE_EVENTS:
                    return None, [*issues, "parse_event_limit"]
                if child.type == "image":
                    issue("markdown_image_not_projected")
                pending.extend(child.children or [])
        elif token.type in {"fence", "code_block", "html_block"}:
            issue("non_table_markdown")
        if events > MAX_PARSE_EVENTS:
            return None, [*issues, "parse_event_limit"]
        if rows > MAX_PARSE_ROWS or cells > MAX_PARSE_CELLS:
            return None, [*issues, "parse_row_or_cell_limit"]
    if not table_count:
        return None, issues
    rendered = md.renderer.render(tokens, md.options, {})
    if len(rendered) > MAX_PARSE_CHARS:
        return None, [*issues, "parse_character_limit"]
    parser = _TableParser()
    try:
        parser.feed(rendered)
        parser.close()
    except (_ParseLimitError, RecursionError):
        parser.issue("parse_incomplete")
    if parser.stack or parser.current is not None:
        parser.issue("unclosed_markup")
    for code in parser.issues:
        issue(code)
    return rendered, issues


def summarize_table(table: JSON, *, source_projection: str | None = None) -> dict[str, Any]:
    """Summarize received markup; parseComplete never attests to source/OCR completeness."""
    content, fmt, completeness = _source_text(table, source_projection)
    result = {
        key: copy.deepcopy(table[key])
        for key in ("tableId", "page", "ordinal", "continuationOf", "screenshotAvailable")
        if key in table
    }
    locator = table.get("locator")
    if "page" not in result and isinstance(locator, Mapping):
        result["page"] = copy.deepcopy(locator.get("page", locator.get("pageStart")))
    result.update(
        inputCompleteness=completeness, summaryTruncated=False, textFormat=fmt, tables=[], issues=[]
    )
    parser = _TableParser()
    bounded = content[:MAX_PARSE_CHARS]
    if len(content) > MAX_PARSE_CHARS:
        parser.issue("parse_character_limit")
    is_markdown = fmt.lower() in {"md", "markdown", "text/markdown", "text/x-markdown"}
    if fmt.lower() in {"html", "text/html"} or (not is_markdown and "<table" in bounded.lower()):
        try:
            parser.feed(bounded)
            parser.close()
        except (_ParseLimitError, RecursionError):
            parser.issue("parse_incomplete")
        if parser.stack or parser.current is not None:
            parser.issue("unclosed_markup")
        parser.finish_row()
        if not parser.tables:
            parser.issue("no_structured_table")
    else:
        rendered, issues = markdown_table_html(bounded)
        for issue in issues:
            parser.issue(issue)
        if rendered is not None:
            try:
                parser.feed(rendered)
                parser.close()
            except (_ParseLimitError, RecursionError):
                parser.issue("parse_incomplete")
            parser.finish_row()
        else:
            parser.issue("no_structured_table")
    parser.finish_outside_context()
    omitted = 0

    def short(value: str, limit: int = 256) -> str:
        nonlocal omitted
        omitted += max(0, len(value) - limit)
        return value[:limit]

    def row_view(row: JSON) -> dict[str, Any]:
        nonlocal omitted
        cells = row["cells"]
        omitted += max(0, len(cells) - 32)
        return {
            "rowIndex": row["rowIndex"],
            "cells": [
                {**cell, "text": short(cell["text"]), "textTruncated": len(cell["text"]) > 256}
                for cell in cells[:32]
            ],
            "omittedCellCount": max(0, len(cells) - 32),
        }

    if parser.outside_context:
        units = [value for value in parser.outside_context if _UNIT.search(value)]
        omitted += max(0, len(parser.outside_context) - 8) + max(0, len(units) - 8)
        result["outsideTableContext"] = {
            "scope": "unassigned_to_table",
            "textsRaw": [short(value) for value in parser.outside_context[:8]],
            "unitTextsRaw": [short(value) for value in units[:8]],
        }

    for parsed in parser.tables:
        rows = parsed["rows"]
        headers = [row for row in rows if row["inThead"]]
        basis = "thead" if headers else "none"
        if not headers:
            for row in rows:
                if row["cells"] and all(cell["tag"] == "th" for cell in row["cells"]):
                    headers.append(row)
                else:
                    break
            if headers:
                basis = "th"
        if not headers and rows and len(rows[0]["cells"]) >= 2:
            headers = [rows[0]]
            basis = "heuristic_first_td_row"
        explicit_header_ids = (
            {row["rowIndex"] for row in headers} if basis != "heuristic_first_td_row" else set()
        )
        body = [row for row in rows if row["rowIndex"] not in explicit_header_ids]
        indexes = sorted({0, len(body) // 2, len(body) - 1}) if body else []
        units = list(
            dict.fromkeys(
                value
                for value in [
                    parsed["titleRaw"],
                    *parsed["context"],
                    *(cell["text"] for row in headers for cell in row["cells"]),
                    *(
                        cell["text"]
                        for row in body
                        for cell in row["cells"]
                        if _UNIT_LABEL.search(cell["text"])
                    ),
                ]
                if _UNIT.search(value)
            )
        )
        omitted += max(0, len(headers) - 8) + max(0, len(units) - 8)
        result["tables"].append(
            {
                "titleRaw": short(parsed["titleRaw"], 600),
                "headerBasis": basis,
                "headerRows": [row_view(row) for row in headers[:8]],
                "unitTextsRaw": [short(value) for value in units[:8]],
                "representativeRows": [row_view(body[index]) for index in indexes],
                "observedRowCount": len(rows),
                "omittedHeaderRowCount": max(0, len(headers) - 8),
                "omittedBodyRowCount": len(body) - len(indexes),
            }
        )
        omitted += len(body) - len(indexes)
    result["issues"] = parser.issues
    result["parseComplete"] = not parser.issues and completeness == "complete"
    result["summaryTruncated"] = bool(omitted or parser.issues or completeness != "complete")
    return result


def _compact_summary(summary: JSON, *, metadata_only: bool = False) -> dict[str, Any]:
    reduced = copy.deepcopy(dict(summary))
    reduced["summaryTruncated"] = True
    reduced["summaryProjection"] = "metadata-only" if metadata_only else "compact-structure"
    context = reduced.get("outsideTableContext")
    if context:
        context["textsRaw"] = []
        context["unitTextsRaw"] = (
            [] if metadata_only else [value[:64] for value in context["unitTextsRaw"][:4]]
        )
    if metadata_only:
        reduced["omittedTableCount"] = len(reduced["tables"])
        reduced["tables"] = []
        return reduced
    for table in reduced["tables"]:
        table["titleRaw"] = table["titleRaw"][:200]
        table["unitTextsRaw"] = [value[:64] for value in table["unitTextsRaw"][:4]]
        table["omittedBodyRowCount"] += len(table["representativeRows"])
        table["representativeRows"] = []
        table["omittedHeaderRowCount"] += max(0, len(table["headerRows"]) - 2)
        table["headerRows"] = table["headerRows"][:2]
        for row in table["headerRows"]:
            row["omittedCellCount"] += max(0, len(row["cells"]) - 8)
            row["cells"] = row["cells"][:8]
            for cell in row["cells"]:
                cell["textTruncated"] |= len(cell["text"]) > 64
                cell["text"] = cell["text"][:64]
    return reduced


def _validate_page(start: int, total: int, target: int, budget: int) -> None:
    if any(type(value) is not int for value in (start, target, budget)) or not (
        0 <= start <= total and target > 0 and 0 < budget <= MAX_FRAME_BYTES
    ):
        raise TableViewError("INVALID_PAGE")


def _page(start: int, end: int, total: int) -> dict[str, Any]:
    return {"start": start, "end": end, "total": total, "hasMore": end < total}


def _fits(payload: JSON, frame_bytes: FrameBytes, budget: int) -> bool:
    size = frame_bytes(copy.deepcopy(dict(payload)))
    if type(size) is not int or size < 0:
        raise TableViewError("INVALID_FRAME_MEASUREMENT")
    return size <= budget


def project_inventory_page(
    snapshot: JSON,
    *,
    start: int,
    max_items: int,
    frame_bytes: FrameBytes,
    max_frame_bytes: int = 60 * 1024,
) -> dict[str, Any]:
    """Page a stable snapshot; frame_bytes must include A's cursor, RPC envelope and LF."""
    tables = snapshot.get("tables")
    if not isinstance(tables, list) or any(not isinstance(item, Mapping) for item in tables):
        raise TableViewError("SOURCE_CONTENT_UNAVAILABLE")
    total = len(tables)
    _validate_page(start, total, max_items, max_frame_bytes)
    collected = (
        snapshot.get("extractedInventoryComplete", snapshot.get("inventoryComplete")) is True
    )
    base: dict[str, Any] = {
        "extractedInventoryComplete": collected,
        "inventoryScope": "Upstream extracted entries, not all real tables in the PDF.",
    }
    source = snapshot.get("file")
    if isinstance(source, Mapping):
        base["file"] = {
            key: copy.deepcopy(source[key])
            for key in ("fileId", "title", "filename", "mediaType")
            if key in source
        }
    selected: list[dict[str, Any]] = []

    def candidate() -> dict[str, Any]:
        end = start + len(selected)
        return {
            **base,
            "tables": list(selected),
            "page": _page(start, end, total),
            "projectionComplete": collected and start == 0 and end == total,
        }

    if start == total:
        empty = candidate()
        if not _fits(empty, frame_bytes, max_frame_bytes):
            raise TableViewError("FRAME_BUDGET_EXCEEDED")
        return empty
    accepted = None
    for item in tables[start : min(total, start + max_items)]:
        selected.append(
            summarize_table(item, source_projection=snapshot.get("tableTextProjection"))
        )
        payload = candidate()
        if not _fits(payload, frame_bytes, max_frame_bytes):
            if accepted is None:
                for metadata_only in (False, True):
                    selected[-1] = _compact_summary(selected[-1], metadata_only=metadata_only)
                    payload = candidate()
                    if _fits(payload, frame_bytes, max_frame_bytes):
                        return payload
            break
        accepted = payload
    if accepted is None:
        raise TableViewError("FRAME_BUDGET_EXCEEDED")
    return accepted


def project_table_page(
    table: JSON,
    *,
    start: int,
    target_chars: int = 10_000,
    frame_bytes: FrameBytes,
    max_frame_bytes: int = 60 * 1024,
) -> dict[str, Any]:
    """Page canonical str code points, not bytes; page.end is the continuation offset."""
    content, fmt, status = _source_text(table)
    if status != "complete":
        raise TableViewError("SOURCE_CONTENT_UNAVAILABLE")
    total = len(content)
    _validate_page(start, total, target_chars, max_frame_bytes)
    base = {
        key: copy.deepcopy(table[key]) for key in ("tableId", "fileId", "revision") if key in table
    }
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

    def candidate(end: int) -> dict[str, Any]:
        return {
            **base,
            "text": {
                "format": fmt,
                "content": content[start:end],
                "sha256": digest,
                "hashScope": "canonical_full_text",
            },
            "contentRepresentation": "canonical_text_fragment",
            "inputCompleteness": status,
            "page": _page(start, end, total),
            "projectionComplete": start == 0 and end == total,
        }

    if start == total:
        empty = candidate(start)
        if not _fits(empty, frame_bytes, max_frame_bytes):
            raise TableViewError("FRAME_BUDGET_EXCEEDED")
        return empty
    upper = min(total, start + target_chars)
    payload = candidate(upper)
    if _fits(payload, frame_bytes, max_frame_bytes):
        return payload
    # The cursor-bearing frame, rather than raw character count, decides the cut.
    lower = start + 1
    accepted = None
    while lower <= upper:
        middle = (lower + upper) // 2
        payload = candidate(middle)
        if _fits(payload, frame_bytes, max_frame_bytes):
            accepted = payload
            lower = middle + 1
        else:
            upper = middle - 1
    if accepted is None:
        raise TableViewError("FRAME_BUDGET_EXCEEDED")
    return accepted


def table_quality_view(table: JSON, *, assessment: JSON | None = None) -> dict[str, Any]:
    """Only the caller's trusted server assessment may upgrade quality, never table claims."""
    result: dict[str, Any] = {
        "visualCheck": "not_performed",
        "sourceCompleteness": "unknown",
        "warnings": [],
    }
    text = table.get("text")
    screenshot = table.get("screenshot")
    if (
        not isinstance(assessment, Mapping)
        or not isinstance(text, Mapping)
        or not isinstance(screenshot, Mapping)
    ):
        return result
    if (
        assessment.get("authority") != "server"
        or assessment.get("verificationStatus") != "verified"
    ):
        return result
    bindings = (
        ("revision", table.get("revision")),
        ("textSha256", text.get("sha256")),
        ("screenshotSha256", screenshot.get("sha256")),
    )
    if any(
        not isinstance(value, str) or not value or assessment.get(key) != value
        for key, value in bindings
    ):
        return result
    visual = assessment.get("visualCheck")
    scope = assessment.get("scope")
    if not isinstance(scope, str) or scope not in {"full_table", "sample"}:
        return result
    result["scope"] = scope
    if isinstance(visual, str) and visual in {"not_performed", "matched", "mismatch", "failed"}:
        result["visualCheck"] = visual
    known_incomplete = assessment.get("sourceCompleteness") == "known_incomplete"
    if known_incomplete or result["visualCheck"] in {"mismatch", "failed"}:
        warnings = assessment.get("warnings")
        result["warnings"] = (
            [item[:500] for item in warnings[:8] if isinstance(item, str) and item.strip()]
            if isinstance(warnings, list)
            else []
        )
        if not result["warnings"]:
            result["warnings"] = [
                "The extracted table has a known content omission."
                if known_incomplete
                else "The extracted table does not match the original PDF crop."
                if result["visualCheck"] == "mismatch"
                else "The table visual check failed; source completeness remains unknown."
            ]
    if known_incomplete:
        result["sourceCompleteness"] = "known_incomplete"
    elif (
        assessment.get("scope") == "full_table"
        and visual == "matched"
        and assessment.get("sourceCompleteness") == "checked"
    ):
        result["sourceCompleteness"] = "checked"
    return result
