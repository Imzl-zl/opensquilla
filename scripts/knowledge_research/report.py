"""Deterministic HTML rendering for verified Knowledge research state."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from typing import Any

if __package__:
    from .references import build_bibliography, source_format
else:  # pragma: no cover
    from references import (  # type: ignore[import-not-found,no-redef]
        build_bibliography,
        source_format,
    )


class _TableSanitizer(HTMLParser):
    _allowed = frozenset(
        {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "colgroup", "col"}
    )
    _void = frozenset({"col"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self._allowed:
            return
        kept: list[str] = []
        for name, value in attrs:
            if name not in {"colspan", "rowspan", "scope"} or value is None:
                continue
            if name in {"colspan", "rowspan"} and not value.isdigit():
                continue
            if name == "scope" and value not in {"row", "col", "rowgroup", "colgroup"}:
                continue
            kept.append(f' {name}="{html.escape(value, quote=True)}"')
        self.parts.append(f"<{tag}{''.join(kept)}>")
        if tag not in self._void:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._allowed and tag not in self._void:
            self.parts.append(f"</{tag}>")
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(html.escape(data))


def _render_table_text(text_payload: Mapping[str, Any]) -> str:
    content = str(text_payload.get("content") or "")
    fmt = str(text_payload.get("format") or "").lower()
    if fmt in {"html", "text/html"} or "<table" in content.lower():
        parser = _TableSanitizer()
        parser.feed(content)
        rendered = "".join(parser.parts)
        if "<table" in rendered:
            return rendered
    markdown = _markdown_table(content)
    if markdown is not None:
        return markdown
    return f"<pre>{html.escape(content)}</pre>"


def _markdown_table(content: str) -> str | None:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if len(lines) < 2 or "|" not in lines[0]:
        return None
    rows = [_pipe_cells(line) for line in lines]
    if len(rows[0]) < 2 or any(len(row) != len(rows[0]) for row in rows):
        return None
    separator = all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in rows[1])
    body_start = 2 if separator else 1
    head = "".join(f"<th>{html.escape(cell)}</th>" for cell in rows[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows[body_start:]
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _pipe_cells(line: str) -> list[str]:
    clean = line.strip().strip("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", clean)]


def _page(record: Mapping[str, Any]) -> tuple[int | None, int | None]:
    locator = record.get("locator")
    locator = locator if isinstance(locator, Mapping) else {}
    start = locator.get("pageStart")
    end = locator.get("pageEnd")
    page_range = locator.get("page")
    if isinstance(page_range, Mapping):
        start = start or page_range.get("start")
        end = end or page_range.get("end")
    if not isinstance(start, int):
        start = record.get("page") if isinstance(record.get("page"), int) else None
    if not isinstance(end, int):
        end = start
    return start, end


def _page_label(start: int | None, end: int | None) -> str:
    if start is None:
        return ""
    if end is None or end == start:
        return f", p. {start}"
    return f", pp. {start}-{end}"


def render_html_report(state: Mapping[str, Any]) -> str:
    ledger = state["ledger"]
    evidence = ledger["evidence"]
    files = ledger["files"]
    tables = ledger["tables"]
    bibliography = build_bibliography(state)
    reference_numbers = bibliography["fileReferenceNumbers"]
    references = bibliography["references"]

    def evidence_citations(evidence_ids: list[str]) -> str:
        labels: list[str] = []
        seen: set[str] = set()
        for evidence_id in evidence_ids:
            record = evidence[evidence_id]
            file_id = str(record["fileId"])
            number = reference_numbers[file_id]
            start, end = _page(record)
            suffix = (
                _page_label(start, end)
                if source_format(files[file_id]) == "PDF"
                else ", text version"
            )
            label = f"[{number}{suffix}]"
            if label in seen:
                continue
            seen.add(label)
            labels.append(f'<span class="citation">{label}</span>')
        return " ".join(labels)

    sections: dict[str, list[str]] = {}
    section_order: list[str] = []
    for item in state["report"]["items"]:
        section = str(item["section"])
        if section not in sections:
            sections[section] = []
            section_order.append(section)
        if item["kind"] == "claim":
            text = html.escape(str(item["text"])).replace("\n", "<br>")
            citations = evidence_citations(list(item["evidenceIds"]))
            sections[section].append(f"<p>{text} {citations}</p>")
            continue
        table = tables[item["tableId"]]
        file_id = str(table["fileId"])
        number = reference_numbers[file_id]
        start, end = _page(table)
        citation = f'<span class="citation">[{number}{_page_label(start, end)}]</span>'
        parsed = _render_table_text(table["text"])
        screenshot = table["screenshot"]
        mime = html.escape(str(screenshot["mediaType"]), quote=True)
        data = html.escape(str(table["screenshotDataBase64"]), quote=True)
        caption = html.escape(str(item["caption"]))
        sections[section].append(
            '<figure class="table-evidence">'
            f"<figcaption>{caption} {citation}</figcaption>"
            '<div class="parsed-table"><h3>Parsed table</h3>'
            f"{parsed}</div>"
            '<div class="original-table"><h3>Original PDF crop</h3>'
            f'<img src="data:{mime};base64,{data}" alt="Original PDF table crop"></div>'
            "</figure>"
        )

    section_html = "".join(
        f"<section><h2>{html.escape(section)}</h2>{''.join(sections[section])}</section>"
        for section in section_order
    )
    reference_html = "".join(
        "<li>"
        + html.escape(str(reference["title"]))
        + ' <span class="filename">['
        + " + ".join(dict.fromkeys(member["format"] for member in reference["members"]))
        + "]</span>"
        + "</li>"
        for reference in references
    )
    subtitle = state.get("subtitle")
    subtitle_html = f'<div class="subtitle">{html.escape(str(subtitle))}</div>' if subtitle else ""
    title = html.escape(str(state["title"]))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
@page {{ size: A4; margin: 18mm 16mm 20mm; @bottom-right {{ content: counter(page); }} }}
:root {{ color-scheme: light; font-family: Arial, "Noto Sans CJK SC", sans-serif; color: #20252b; }}
body {{ max-width: 980px; margin: 0 auto; padding: 36px 28px 64px;
  line-height: 1.68; background: #fff; }}
header {{ border-bottom: 3px solid #183153; padding-bottom: 18px; margin-bottom: 30px; }}
h1 {{ margin: 0; font-size: 32px; line-height: 1.25; letter-spacing: 0; }}
.subtitle {{ margin-top: 8px; color: #59636e; font-size: 15px; }}
h2 {{ margin: 34px 0 14px; font-size: 22px; letter-spacing: 0; color: #183153; }}
h3 {{ margin: 0 0 10px; font-size: 14px; letter-spacing: 0; color: #4b5563; }}
p {{ margin: 0 0 14px; }}
.citation {{ white-space: nowrap; color: #075985; font-size: .88em; font-weight: 600; }}
.table-evidence {{ margin: 24px 0 30px; break-inside: avoid; }}
figcaption {{ font-weight: 700; margin-bottom: 12px; }}
.parsed-table, .original-table {{ margin-top: 14px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
th, td {{ border: 1px solid #aab2bb; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #eef2f5; }}
img {{ display: block; max-width: 100%; height: auto; border: 1px solid #b8c0c8; }}
pre {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f5f7f9; padding: 12px; }}
.references {{ margin-top: 42px; border-top: 2px solid #183153; padding-top: 18px; }}
.references ol {{ padding-left: 24px; }}
.references li {{ margin: 0 0 9px; overflow-wrap: anywhere; }}
.filename {{ color: #66717c; }}
@media print {{
  body {{ max-width: none; padding: 0; }}
  .parsed-table {{ display: none; }}
  .original-table h3 {{ display: none; }}
}}
</style>
</head>
<body>
<header><h1>{title}</h1>{subtitle_html}</header>
<main>{section_html}</main>
<section class="references"><h2>References</h2><ol>{reference_html}</ol></section>
</body>
</html>
"""
