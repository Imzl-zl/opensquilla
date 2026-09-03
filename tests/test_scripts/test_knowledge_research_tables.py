from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

import pytest

from scripts.knowledge_research import table_views
from scripts.knowledge_research.references import build_bibliography
from scripts.knowledge_research.report import (
    _render_table_text,
    _TableSanitizer,
    render_html_report,
)
from scripts.knowledge_research.table_views import (
    MAX_FRAME_BYTES,
    MAX_PARSE_CHARS,
    TableViewError,
    markdown_table_html,
    project_inventory_page,
    project_table_page,
    summarize_table,
    table_quality_view,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
HTML = (
    "<table><caption>Annual returns (%)</caption><thead>"
    '<tr><th rowspan="2">Index</th><th colspan="2">Return (%)</th></tr>'
    "<tr><th>2025</th><th>2026</th></tr></thead><tbody>"
    "<tr><td>Market A</td><td></td><td>58.8%</td></tr>"
    "<tr><td>Market B</td><td>0</td><td>31.2%</td></tr></tbody></table>"
)


def _table(content: str = HTML, *, key: str = "table-private") -> dict[str, Any]:
    return {
        "tableId": key,
        "fileId": "file-private",
        "revision": "a" * 64,
        "page": 3,
        "text": {
            "content": content,
            "format": "html",
            "truncated": False,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
        "screenshot": {"sha256": hashlib.sha256(PNG).hexdigest(), "mediaType": "image/png"},
        "screenshotDataBase64": base64.b64encode(PNG).decode(),
        "verificationStatus": "verified",
    }


def _wire(payload: Mapping[str, Any], *, duplicate: bool = False) -> bytes:
    projected = copy.deepcopy(dict(payload))
    page = projected["page"]
    cursor = json.dumps({"snapshot": "immutable", "next": page["end"]}).encode()
    projected["nextCursor"] = base64.urlsafe_b64encode(cursor).decode() if page["hasMore"] else None
    encoded = json.dumps(projected, ensure_ascii=False, separators=(",", ":"))
    result: dict[str, Any] = {"content": [{"type": "text", "text": encoded}], "isError": False}
    if duplicate:
        result["structuredContent"] = projected
    return (
        json.dumps(
            {"jsonrpc": "2.0", "id": 'request-"\\', "result": result},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _measure(payload: Mapping[str, Any]) -> int:
    return len(_wire(payload))


def _assessment(table: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "authority": "server",
        "verificationStatus": "verified",
        "scope": "full_table",
        "revision": table["revision"],
        "textSha256": table["text"]["sha256"],
        "screenshotSha256": table["screenshot"]["sha256"],
        "visualCheck": "matched",
        "sourceCompleteness": "checked",
        **changes,
    }


def _state(count: int = 1) -> dict[str, Any]:
    tables = {
        f"table-private-{index}": _table(key=f"table-private-{index}") for index in range(count)
    }
    return {
        "title": "Synthetic research",
        "ledger": {
            "files": {
                "file-private": {"title": "Returns & valuation + risks", "filename": "returns.pdf"}
            },
            "evidence": {},
            "tables": tables,
        },
        "report": {
            "items": [
                {"kind": "table", "section": "Tables", "tableId": key, "caption": "Annual returns"}
                for key in tables
            ]
        },
    }


def _references(titles: list[str], dates: list[str] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "ledger": {"files": {}, "evidence": {}, "tables": {}},
        "report": {"items": []},
    }
    dates = dates or ["2026-07-05"] * len(titles)
    for index, title in enumerate(titles):
        stem = f"{dates[index]}+Korea Weekly Kickstart Market performance and earnings"
        extension = "pdf" if index == 0 else "md"
        key = f"file-{index}"
        state["ledger"]["files"][key] = {
            "title": title,
            "filename": f"{stem}.{extension}",
            "sourcePath": f"goldman/{dates[index]}/{stem}/{stem}.{extension}",
            "verificationStatus": "verified",
            "revision": str(index) * 64,
        }
        state["ledger"]["evidence"][key] = {"fileId": key}
        state["report"]["items"].append({"kind": "claim", "evidenceIds": [key]})
    return state


def test_structured_headers_units_spans_and_empty_cells_are_preserved() -> None:
    source = _table()
    before = copy.deepcopy(source)
    view = summarize_table(source)
    assert source == before
    parsed = view["tables"][0]
    assert view["page"] == 3
    assert view["parseComplete"] is True
    assert parsed["titleRaw"] == "Annual returns (%)"
    assert parsed["headerBasis"] == "thead"
    assert parsed["headerRows"][0]["cells"][0]["rowspan"] == 2
    assert parsed["headerRows"][0]["cells"][1]["colspan"] == 2
    assert parsed["headerRows"][1]["cells"][1]["text"] == "2026"
    assert "Return (%)" in parsed["unitTextsRaw"]
    assert parsed["representativeRows"][0]["cells"][1]["text"] == ""
    assert parsed["representativeRows"][1]["cells"][1]["text"] == "0"
    assert "screenshotDataBase64" not in view


def test_td_header_is_explicitly_heuristic_and_kept_as_a_sample() -> None:
    table = _table(
        "<table><tr><td>Index</td><td>2026</td></tr><tr><td>A</td><td>8%</td></tr></table>"
    )
    parsed = summarize_table(table)["tables"][0]
    assert parsed["headerBasis"] == "heuristic_first_td_row"
    assert parsed["representativeRows"][0]["rowIndex"] == 0


def test_entities_line_breaks_comments_and_scripts() -> None:
    source = _table(
        "<table><caption>A &amp; B</caption><!-- ignore -->"
        "<tr><th>Name</th><th>USD</th></tr><tr><td>A<br>B</td>"
        "<td><script>not data</script><b>10</b></td></tr></table>"
    )
    parsed = summarize_table(source)["tables"][0]
    assert parsed["titleRaw"] == "A & B"
    assert parsed["representativeRows"][0]["cells"][0]["text"] == "A\nB"
    assert "not data" not in json.dumps(parsed)


@pytest.mark.parametrize("content", ["y" * 2_000, "<p>Plain HTML, without a table.</p>"])
@pytest.mark.parametrize("complete", [False, True])
def test_html_without_table_is_not_structurally_complete(content: str, complete: bool) -> None:
    source = _table(content)
    if not complete:
        source["text"].pop("sha256")
        source["text"].pop("truncated")
    view = summarize_table(source)
    assert view["inputCompleteness"] == ("complete" if complete else "unknown")
    assert view["tables"] == []
    assert "no_structured_table" in view["issues"]
    assert view["parseComplete"] is False
    assert view["summaryTruncated"] is True


@pytest.mark.parametrize("span", ["999999999999999999999999", "129", "0", "-1", "1.5", "\uff11"])
def test_malicious_spans_stop_without_expanding_or_faking_complete(span: str) -> None:
    source = _table(f'<table><tr><td colspan="{span}">x</td></tr></table>')
    view = summarize_table(source)
    assert "invalid_or_excessive_span" in view["issues"]
    assert view["parseComplete"] is False
    assert view["summaryTruncated"] is True


@pytest.mark.parametrize(
    "content, issue",
    [
        ("<div>" * 100 + HTML, "parse_depth_limit"),
        ("<table><tr><td>" + HTML + "</td></tr></table>", "nested_table_unsupported"),
        ("x" * (MAX_PARSE_CHARS + 1), "parse_character_limit"),
        ("<table><tr><td>A", "unclosed_markup"),
    ],
)
def test_parse_limits_and_malformed_markup_are_conservative(content: str, issue: str) -> None:
    view = summarize_table(_table(content))
    assert issue in view["issues"]
    assert view["parseComplete"] is False


@pytest.mark.parametrize(
    "constant, content, issue",
    [
        ("MAX_PARSE_EVENTS", "<!--x-->" * 100, "parse_event_limit"),
        ("MAX_PARSE_ROWS", "<table>" + "<tr><td>x</td></tr>" * 100 + "</table>", "parse_row_limit"),
        (
            "MAX_PARSE_CELLS",
            "<table><tr>" + "<td>x</td>" * 100 + "</tr></table>",
            "parse_cell_limit",
        ),
    ],
)
def test_resource_counters(
    monkeypatch: pytest.MonkeyPatch, constant: str, content: str, issue: str
) -> None:
    monkeypatch.setattr(table_views, constant, 16)
    view = summarize_table(_table(content))
    assert issue in view["issues"]
    assert view["parseComplete"] is False


def test_markdown_and_multiple_tables() -> None:
    source = _table("| Name | Return (%) |\n|---|---|\n| A | 8% |")
    source["text"]["format"] = "markdown"
    assert summarize_table(source)["tables"][0]["headerBasis"] == "thead"
    assert len(summarize_table(_table(HTML + HTML))["tables"]) == 2


@pytest.mark.parametrize("preview_chars", [800, 300, 80, 0])
def test_inner_adapter_projection_never_becomes_full_text(preview_chars: int) -> None:
    content = HTML * 3
    source = {"tableId": "t", "textFormat": "html", "textTruncated": True}
    if preview_chars:
        source["textPreview"] = content[:preview_chars]
    else:
        source["textAvailable"] = True
    view = summarize_table(
        source, source_projection="preview" if preview_chars else "metadata-only"
    )
    assert view["inputCompleteness"] in {"truncated", "unavailable"}
    with pytest.raises(TableViewError, match="SOURCE_CONTENT_UNAVAILABLE"):
        project_table_page(source, start=0, frame_bytes=_measure)


def test_upstream_truncation_at_exact_320_and_unmarked_preview() -> None:
    source = _table("x" * 320)
    source["text"]["truncated"] = True
    assert summarize_table(source)["inputCompleteness"] == "truncated"
    assert summarize_table({"textPreview": HTML})["inputCompleteness"] == "unknown"


@pytest.mark.parametrize("unit", ["a", "\u4e2d", "\U0001f4c8", '"\\\n'])
@pytest.mark.parametrize("budget", [8_192, MAX_FRAME_BYTES])
def test_inventory_78_entries_resumable_and_whole_wire_bounded(unit: str, budget: int) -> None:
    tables = [
        _table(
            f"<table><tr><th>{unit * 320}</th><th>%</th></tr>"
            f"<tr><td>{unit * 320}</td><td>8%</td></tr></table>",
            key=str(index),
        )
        for index in range(78)
    ]
    source = {"file": {"fileId": "f"}, "inventoryComplete": True, "tables": tables}
    before = copy.deepcopy(source)
    start = 0
    keys: list[str] = []
    pages = []
    while start < 78:
        page = project_inventory_page(
            source, start=start, max_items=78, frame_bytes=_measure, max_frame_bytes=budget
        )
        assert len(_wire(page)) <= budget
        assert page["page"]["end"] > start
        assert page["extractedInventoryComplete"] is True
        assert "inventoryComplete" not in page
        assert "modelInventoryComplete" not in page
        assert page["projectionComplete"] is False
        keys.extend(item["tableId"] for item in page["tables"])
        pages.append(page)
        start = page["page"]["end"]
    assert keys == [str(index) for index in range(78)]
    assert pages[-1]["page"]["hasMore"] is False
    assert source == before
    assert pages[0] == project_inventory_page(
        source, start=0, max_items=78, frame_bytes=_measure, max_frame_bytes=budget
    )


def test_empty_single_and_terminal_inventory_pages() -> None:
    source = {"inventoryComplete": True, "tables": [_table(), _table(key="second")]}
    all_items = project_inventory_page(source, start=0, max_items=2, frame_bytes=_measure)
    assert all_items["projectionComplete"] is True
    last = project_inventory_page(source, start=1, max_items=2, frame_bytes=_measure)
    assert last["projectionComplete"] is False
    terminal = project_inventory_page(source, start=2, max_items=2, frame_bytes=_measure)
    assert terminal["page"] == {"start": 2, "end": 2, "total": 2, "hasMore": False}
    assert terminal["projectionComplete"] is False
    empty = project_inventory_page(
        {"inventoryComplete": True, "tables": []}, start=0, max_items=1, frame_bytes=_measure
    )
    assert empty["projectionComplete"] is True
    incomplete = project_inventory_page(
        {"inventoryComplete": False, "tables": [_table()]},
        start=0,
        max_items=1,
        frame_bytes=_measure,
    )
    assert incomplete["projectionComplete"] is False


@pytest.mark.parametrize("duplicate", [False, True])
def test_unicode_original_text_reassembles_under_actual_frame_budget(duplicate: bool) -> None:
    content = "<table><tr><td>" + '\u4e2d\U0001f4c8"\\\n' * 8_000 + "</td></tr></table>"
    source = _table(content)
    start = 0
    fragments = []
    while start < len(content):
        view = project_table_page(
            source,
            start=start,
            target_chars=30_000,
            frame_bytes=lambda value: len(_wire(value, duplicate=duplicate)),
        )
        assert len(_wire(view, duplicate=duplicate)) <= MAX_FRAME_BYTES
        assert view["page"]["end"] > start
        fragments.append(view["text"]["content"])
        start = view["page"]["end"]
    assert "".join(fragments) == content
    assert view["projectionComplete"] is False
    assert view["text"]["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert view["text"]["hashScope"] == "canonical_full_text"


def test_table_character_target_and_byte_boundary() -> None:
    source = _table("x" * 20_001)
    page = project_table_page(source, start=0, frame_bytes=_measure)
    assert page["page"]["end"] == 10_000
    small = _table("x")
    actual = _measure(project_table_page(small, start=0, frame_bytes=_measure))
    assert (
        project_table_page(small, start=0, frame_bytes=_measure, max_frame_bytes=actual)[
            "projectionComplete"
        ]
        is True
    )
    with pytest.raises(TableViewError, match="FRAME_BUDGET_EXCEEDED"):
        project_table_page(small, start=0, frame_bytes=_measure, max_frame_bytes=actual - 1)


@pytest.mark.parametrize(
    "start,target,budget",
    [(-1, 1, 1000), (100, 1, 1000), (True, 1, 1000), (0, 0, 1000), (0, 1, 61_441), (0, 1, 0)],
)
def test_invalid_page_arguments(start: int, target: int, budget: int) -> None:
    with pytest.raises(TableViewError, match="INVALID_PAGE"):
        project_table_page(
            _table("small"),
            start=start,
            target_chars=target,
            frame_bytes=_measure,
            max_frame_bytes=budget,
        )


def test_impossible_inventory_budget_does_not_drop_item() -> None:
    with pytest.raises(TableViewError, match="FRAME_BUDGET_EXCEEDED"):
        project_inventory_page(
            {"inventoryComplete": True, "tables": [_table()]},
            start=0,
            max_items=1,
            frame_bytes=_measure,
            max_frame_bytes=20,
        )


def test_integrity_and_model_self_report_do_not_verify_ocr() -> None:
    table = _table()
    table.update(visualCheck="matched", sourceCompleteness="checked", assessment=_assessment(table))
    assert table_quality_view(table) == {
        "visualCheck": "not_performed",
        "sourceCompleteness": "unknown",
        "warnings": [],
    }
    assert (
        table_quality_view(table, assessment=_assessment(table, authority="model"))[
            "sourceCompleteness"
        ]
        == "unknown"
    )


def test_server_assessment_scope_binding_and_known_omission() -> None:
    table = _table(
        HTML.replace("<th>2026</th>", "")
        .replace("<td>58.8%</td>", "")
        .replace("<td>31.2%</td>", "")
    )
    assert summarize_table(table)["parseComplete"] is True
    assert table_quality_view(table)["sourceCompleteness"] == "unknown"
    assessment = _assessment(
        table,
        sourceCompleteness="known_incomplete",
        visualCheck="mismatch",
        warnings=["The year column is missing."],
    )
    assert (
        table_quality_view(table, assessment=assessment)["sourceCompleteness"] == "known_incomplete"
    )
    assert table_quality_view(table, assessment=assessment) == table_quality_view(
        copy.deepcopy(table), assessment=assessment
    )
    assert (
        table_quality_view(table, assessment=_assessment(table, textSha256="stale"))[
            "sourceCompleteness"
        ]
        == "unknown"
    )
    assert (
        table_quality_view(table, assessment=_assessment(table, scope="sample"))[
            "sourceCompleteness"
        ]
        == "unknown"
    )
    assert (
        table_quality_view(table, assessment=_assessment(table))["sourceCompleteness"] == "checked"
    )


@pytest.mark.parametrize(
    "title",
    [
        "<!-- Generated locally by pdf-md-splitter -->",
        "<!--\nGenerated locally by pdf-md-splitter\n-->",
        "<!-- Generated locally by pdf-md-splitter",
        "Generated locally by pdf-md-splitter.",
        "[page 1]",
    ],
)
def test_converter_and_placeholder_titles_fall_back_without_pollution(title: str) -> None:
    state = _references(["[page 1]", title])
    bibliography = build_bibliography(state)
    assert bibliography["sourceCount"] == 1
    label = bibliography["references"][0]["title"]
    assert "pdf-md-splitter" not in label
    assert "<!--" not in label
    assert "Market performance and earnings" in label


@pytest.mark.parametrize(
    "title",
    [
        "S&P 500 +5% / EPS_-2%: USD/KRW < 1,400 & > 1,200",
        "2026-07-05 P/E +10% / EPS_-5%",
        "\u65e5\u672c\u682a\uff1aEPS +5%\u3001P/E < 20\u500d",
    ],
)
def test_financial_title_characters_are_not_filename_cleanup(title: str) -> None:
    state = _references(["<!-- hidden -->" + title])
    label = build_bibliography(state)["references"][0]["title"]
    assert title in label


def test_title_selection_is_stable_but_identity_is_independent() -> None:
    state = _references(["[page 1]", "<!-- generated -->Specific earnings + valuation"])
    before = copy.deepcopy(state)
    first = build_bibliography(state)
    state["report"]["items"].reverse()
    second = build_bibliography(state)
    assert first["references"][0]["title"] == second["references"][0]["title"]
    assert first["references"][0]["titleSelection"] == second["references"][0]["titleSelection"]
    assert first["sourceCount"] == 1
    assert before["ledger"] == state["ledger"]
    different = _references(["Same title", "Same title"], ["2026-06-26", "2026-07-10"])
    assert build_bibliography(different)["sourceCount"] == 2
    ambiguous = _references(["Same title"] * 3)
    assert build_bibliography(ambiguous)["sourceCount"] == 3
    state["ledger"]["files"]["file-1"]["title"] = "2026-07-06 Different issue"
    assert build_bibliography(state)["sourceCount"] == 2


def test_report_has_one_unknown_note_and_original_print_rules_not_real_pdf() -> None:
    state = _state(3)
    before = copy.deepcopy(state)
    html = render_html_report(state)
    assert state == before
    assert html.count('<p class="table-quality-note">') == 1
    images = re.findall(r'src="data:image/png;base64,([^"]+)"', html)
    assert len(images) == 3
    assert all(base64.b64decode(image) == PNG for image in images)
    assert ".parsed-table { display: none; }" in html
    assert not re.search(r"\.original-table\s*\{[^}]*display:\s*none", html)
    assert "file-private" not in html
    assert "table-private" not in html
    assert not re.search(r'(?:src|href)="(?:https?:|file:|/)', html)


def test_report_known_omission_warning_follows_image_and_is_print_visible() -> None:
    state = _state(2)
    table = state["ledger"]["tables"]["table-private-0"]
    state["ledger"]["tableAssessments"] = {
        "table-private-0": _assessment(
            table,
            sourceCompleteness="known_incomplete",
            visualCheck="mismatch",
            warnings=["Missing year column <2026>."],
        )
    }
    html = render_html_report(state)
    assert html.count('<p class="table-quality-note">') == 1
    assert html.count("Missing year column &lt;2026&gt;.") == 1
    assert (
        html.index('alt="Original PDF table crop"')
        < html.index("Missing year column")
        < html.index("</figure>")
    )
    assert not re.search(r"\.table-warning\s*\{[^}]*display:\s*none", html)


def test_report_rejects_dangerous_table_html_without_changing_original_image() -> None:
    state = _state()
    table = _table(
        '<table><tr><td colspan="99999999999999999999">value</td></tr></table>',
        key="table-private-0",
    )
    state["ledger"]["tables"]["table-private-0"] = table
    html = render_html_report(state)
    assert "99999999999999999999" not in html
    assert "cannot be safely displayed" in html
    assert base64.b64encode(PNG).decode() in html


def test_cumulative_span_area_is_bounded_before_rendering() -> None:
    content = "<table><tr>" + '<td rowspan="128" colspan="128">x</td>' * 5 + "</tr></table>"
    assert "parse_span_area_limit" in summarize_table(_table(content))["issues"]
    state = _state()
    state["ledger"]["tables"]["table-private-0"] = _table(content, key="table-private-0")
    assert "cannot be safely displayed" in render_html_report(state)


@pytest.mark.parametrize(
    "attrs",
    [
        'rowspan="128" rowspan="1" colspan="128" colspan="1"',
        'rowspan="1" rowspan="128" colspan="1" colspan="128"',
        'ROWSPAN="128" rowspan="1" colspan="128"',
        'rowspan="128" colspan="128" COLSPAN',
    ],
)
def test_duplicate_spans_fail_closed_in_summary_and_renderer(attrs: str) -> None:
    content = "<table><tr>" + f"<td {attrs}>x</td>" * 5 + "</tr></table>"
    source = _table(content)
    view = summarize_table(source)
    assert "duplicate_span_attribute" in view["issues"]
    assert view["parseComplete"] is False
    assert view["summaryTruncated"] is True
    rendered = _render_table_text(source["text"])
    assert "cannot be safely displayed" in rendered
    assert "<td" not in rendered
    with pytest.raises(ValueError, match="duplicate_span_attribute"):
        _TableSanitizer().feed(content)
    state = _state()
    state["ledger"]["tables"]["table-private-0"] = _table(content, key="table-private-0")
    assert base64.b64encode(PNG).decode() in render_html_report(state)


@pytest.mark.parametrize("before", [True, False])
@pytest.mark.parametrize("inline_markup", [False, True])
def test_outside_table_units_are_preserved_without_asserting_attribution(
    before: bool, inline_markup: bool
) -> None:
    units = "Units: <strong>USD</strong> million" if inline_markup else "Units: USD million"
    note = f'<p onclick="bad()">{units}</p>'
    markup = "<table><tr><th>A</th><th>B</th></tr><tr><td>Sales</td><td>10</td></tr></table>"
    source = _table(note + markup if before else markup + note)
    view = summarize_table(source)
    assert view["summaryTruncated"] is True
    assert "outside_table_text" in view["issues"]
    assert view["outsideTableContext"] == {
        "scope": "unassigned_to_table",
        "textsRaw": ["Units: USD million"],
        "unitTextsRaw": ["Units: USD million"],
    }
    assert view["tables"][0]["unitTextsRaw"] == []
    rendered = _render_table_text(source["text"])
    assert units in rendered
    assert "<table>" in rendered
    assert "onclick" not in rendered
    assert (rendered.index("Units:") < rendered.index("<table>")) is before


def test_outside_table_context_is_bounded_and_not_copied_to_other_tables() -> None:
    note = "Units: USD million " + "x" * 1_000
    source = _table("<p>" + note + "</p>" + HTML + "<p>Units: KRW billion</p>" + HTML)
    view = summarize_table(source)
    assert view["summaryTruncated"] is True
    assert len(view["outsideTableContext"]["textsRaw"][0]) == 256
    assert view["outsideTableContext"]["unitTextsRaw"][1] == "Units: KRW billion"
    assert all("Units: KRW billion" not in item["unitTextsRaw"] for item in view["tables"])


@pytest.mark.parametrize("visual", ["mismatch", "failed"])
def test_valid_visual_failure_warnings_survive_unknown_completeness(visual: str) -> None:
    state = _state()
    table = state["ledger"]["tables"]["table-private-0"]
    warning = "Annual totals mismatch the crop."
    assessment = _assessment(
        table, visualCheck=visual, sourceCompleteness="unknown", warnings=[warning]
    )
    before = copy.deepcopy(assessment)
    quality = table_quality_view(table, assessment=assessment)
    assert quality["warnings"] == [warning]
    assert quality["visualCheck"] == visual
    assert quality["sourceCompleteness"] == "unknown"
    assert assessment == before
    state["ledger"]["tableAssessments"] = {"table-private-0": assessment}
    rendered = render_html_report(state)
    assert rendered.count(warning) == 1
    assert rendered.index('alt="Original PDF table crop"') < rendered.index(warning)
    assert rendered.index(warning) < rendered.index("</figure>")
    assert rendered.count('<p class="table-quality-note">') == 1
    for changes in ({"authority": "model"}, {"textSha256": "stale"}):
        assert table_quality_view(table, assessment={**assessment, **changes})["warnings"] == []


@pytest.mark.parametrize("visual", ["mismatch", "failed"])
def test_visual_failure_warnings_remain_bounded_and_have_a_fallback(visual: str) -> None:
    table = _table()
    assessment = _assessment(
        table, visualCheck=visual, sourceCompleteness="unknown", warnings=["x" * 900] * 20
    )
    assert table_quality_view(table, assessment=assessment)["warnings"] == ["x" * 500] * 8
    assert table_quality_view(table, assessment={**assessment, "warnings": []})["warnings"]


def test_conflicting_title_suffix_dates_veto_reference_merge() -> None:
    titles = ["Korea Weekly Kickstart - 2026-07-05", "Korea Weekly Kickstart - 2026-07-12"]
    state = _references(titles)
    bibliography = build_bibliography(state)
    assert bibliography["sourceCount"] == 2
    assert bibliography["sourceFileCount"] == 2
    for title, reference in zip(titles, bibliography["references"], strict=True):
        assert title in reference["title"]
        assert reference["groupingBasis"] == "distinct_source_file"
    state["report"]["items"].reverse()
    assert build_bibliography(state)["sourceCount"] == 2
    same_issue = _references(["S&P +5% / EPS_-2% - 2026-07-05"] * 2)
    assert build_bibliography(same_issue)["sourceCount"] == 1
    assert "S&P +5% / EPS_-2%" in build_bibliography(same_issue)["references"][0]["title"]


def test_unit_notes_stay_with_their_table() -> None:
    content = (
        "<table><tr><th>A</th><th>B</th></tr><tr><td>Units: USD million</td><td>1</td></tr></table>"
        "<table><tr><th>C</th><th>D</th></tr><tr><td>Units: KRW billion</td><td>2</td></tr></table>"
    )
    tables = summarize_table(_table(content))["tables"]
    assert tables[0]["unitTextsRaw"] == ["Units: USD million"]
    assert tables[1]["unitTextsRaw"] == ["Units: KRW billion"]


def test_huge_single_summary_still_has_a_bounded_explicit_projection() -> None:
    content = (
        "<table><thead><tr>" + ("<th>" + "\u4e2d" * 500 + "</th>") * 32 + "</tr></thead></table>"
    )
    source = {"inventoryComplete": True, "tables": [_table(content)]}
    view = project_inventory_page(
        source, start=0, max_items=1, frame_bytes=_measure, max_frame_bytes=4_096
    )
    assert _measure(view) <= 4_096
    assert view["page"]["end"] == 1
    assert view["tables"][0]["summaryTruncated"] is True
    assert view["tables"][0]["summaryProjection"] in {"compact-structure", "metadata-only"}


def test_wrong_hash_and_invalid_unicode_are_not_canonical_full_content() -> None:
    source = _table()
    source["text"]["sha256"] = "f" * 64
    assert summarize_table(source)["inputCompleteness"] == "unknown"
    with pytest.raises(TableViewError, match="SOURCE_CONTENT_UNAVAILABLE"):
        project_table_page(source, start=0, frame_bytes=_measure)
    source["text"]["content"] = "\ud800"
    assert summarize_table(source)["inputCompleteness"] == "unavailable"


def test_markdown_row_resource_limit_and_width_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _table("| A | B |\n|---|---|\n" + "| x | y |\n" * 20)
    source["text"]["format"] = "markdown"
    monkeypatch.setattr(table_views, "MAX_PARSE_ROWS", 10)
    view = summarize_table(source)
    assert "parse_row_or_cell_limit" in view["issues"]
    assert view["parseComplete"] is False
    assert view["tables"] == []


@pytest.mark.parametrize(
    "body, expected",
    [
        (r"| A \| B | `C\|D` |", ["A | B", "C|D"]),
        ('| **USD** &amp; *KRW* | <span onclick="boom()">P/E</span> |', ["USD & KRW", "P/E"]),
        ("| `<table>` | EPS |", ["<table>", "EPS"]),
        (r"| ``a`b`` | \*literal\* |", ["a`b", "*literal*"]),
    ],
)
def test_markdown_tokens_preserve_pipes_code_and_inline_formatting(
    body: str,
    expected: list[str],
) -> None:
    source = _table("| Label | Value |\n|---|---|\n" + body)
    source["text"]["format"] = "markdown"
    view = summarize_table(source)
    assert view["issues"] == []
    assert view["parseComplete"] is True
    cells = view["tables"][0]["representativeRows"][0]["cells"]
    assert [cell["text"] for cell in cells] == expected
    rendered = _render_table_text(source["text"])
    assert "<table>" in rendered
    assert "onclick" not in rendered
    if "C|D" in expected:
        assert "<code>C|D</code>" in rendered
    if "USD & KRW" in expected:
        assert "<strong>USD</strong> &amp; <em>KRW</em>" in rendered
        assert "<span>P/E</span>" in rendered
    if "<table>" in expected:
        assert "<code>&lt;table&gt;</code>" in rendered
        assert rendered.count("<table>") == 1


def test_markdown_alignment_uses_token_attributes_with_safe_css_classes() -> None:
    source = _table("Left | Center | Right\n:---|:---:|---:\nA | B | 9\n")
    source["text"]["format"] = "text/markdown"
    view = summarize_table(source)
    cells = view["tables"][0]["headerRows"][0]["cells"]
    assert [cell["alignment"] for cell in cells] == ["left", "center", "right"]
    rendered = _render_table_text(source["text"])
    assert '<th class="align-left">Left</th>' in rendered
    assert '<td class="align-center">B</td>' in rendered
    assert '<td class="align-right">9</td>' in rendered
    assert "style=" not in rendered


@pytest.mark.parametrize("body", ["| A |", "| A | B | EXTRA-VALUE |", "| `A|B` | C |"])
def test_markdown_normalized_width_is_diagnostic_and_report_preserves_source(body: str) -> None:
    source = _table("| Label | Value |\n|---|---|\n" + body)
    source["text"]["format"] = "markdown"
    view = summarize_table(source)
    assert "markdown_row_width_normalized" in view["issues"]
    assert view["parseComplete"] is False
    rendered = _render_table_text(source["text"])
    assert rendered.startswith("<pre>")
    assert body in rendered


def test_markdown_cells_events_and_allocation_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    content = "| A | B |\n|---|---|\n| x | y |\n"
    monkeypatch.setattr(table_views, "MAX_PARSE_CELLS", 3)
    rendered, issues = markdown_table_html(content)
    assert rendered is None
    assert "parse_row_or_cell_limit" in issues
    monkeypatch.setattr(table_views, "MAX_PARSE_CELLS", 8_192)
    oversized_grid = ("|" * 256 + "\n") * 256
    rendered, issues = markdown_table_html(oversized_grid)
    assert rendered is None
    assert "markdown_allocation_limit" in issues
    monkeypatch.setattr(table_views, "MAX_PARSE_EVENTS", 100)
    rendered, issues = markdown_table_html(content.replace("x", "*x* " * 100))
    assert rendered is None
    assert "parse_event_limit" in issues


def test_markdown_source_length_limit_precedes_parsing() -> None:
    rendered, issues = markdown_table_html("x" * (MAX_PARSE_CHARS + 1))
    assert rendered is None
    assert issues == ["parse_character_limit"]


def test_markdown_raw_html_is_sanitized_and_nested_tables_fail_closed() -> None:
    source = _table(
        '| A | B |\n|---|---|\n| <span style="background:url(https://invalid.example)">'
        "x</span> | <script>bad()</script>2 |"
    )
    source["text"]["format"] = "markdown"
    rendered = _render_table_text(source["text"])
    assert "<span>x</span>" in rendered
    assert "invalid.example" not in rendered
    assert "<script>" not in rendered
    assert "bad()" not in rendered
    nested = _table("| A | B |\n|---|---|\n| <table><tr><td>x</td></tr></table> | y |")
    nested["text"]["format"] = "markdown"
    assert "nested_table_unsupported" in summarize_table(nested)["issues"]
    assert _render_table_text(nested["text"]).startswith("<pre>")


def test_multiple_markdown_tables_and_non_table_text_are_not_silently_dropped() -> None:
    content = "| A | B |\n|---|---|\n| x | y |\n"
    source = _table(content + "\n" + content)
    source["text"]["format"] = "markdown"
    assert len(summarize_table(source)["tables"]) == 2
    assert _render_table_text(source["text"]).count("<table>") == 2
    source = _table(content + "\nImportant source footnote.")
    source["text"]["format"] = "markdown"
    assert "non_table_markdown" in summarize_table(source)["issues"]
    rendered = _render_table_text(source["text"])
    assert rendered.startswith("<pre>")
    assert "Important source footnote." in rendered
