from __future__ import annotations

import base64
import copy
import html
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research.references import build_bibliography
from scripts.knowledge_research.report import render_html_report
from scripts.knowledge_research.table_views import (
    TableViewError,
    _compact_summary,
    project_table_page,
    summarize_table,
    table_quality_view,
)
from tests.test_scripts.test_knowledge_research_navigation import invoke, setup
from tests.test_scripts.test_knowledge_research_tables import (
    HTML,
    PNG,
    _assessment,
    _measure,
    _references,
    _state,
    _table,
)


def _claim_state() -> dict[str, Any]:
    state = _state()
    state["ledger"]["evidence"] = {
        "e1": {"fileId": "file-private", "locator": {"pageStart": 3, "pageEnd": 5}}
    }
    state["report"]["items"].insert(
        0,
        {"kind": "claim", "section": "Analysis", "text": "Returns", "evidenceIds": ["e1"]},
    )
    return state


@pytest.mark.parametrize("field", ["title", "subtitle", "body"])
def test_report_detects_chinese_only_from_report_content(field: str) -> None:
    state = _claim_state()
    if field == "body":
        state["report"]["items"][0]["text"] = "\u5e02\u573a\u56de\u62a5"
    else:
        state[field] = "\u5e02\u573a\u56de\u62a5"
    before = copy.deepcopy(state)
    rendered = render_html_report(state)
    assert '<html lang="zh-CN">' in rendered
    for label in (
        "\u53c2\u8003\u6587\u732e",
        "\u8d44\u6599\u8bf4\u660e",
        "\u8868\u683c\u6587\u5b57",
        "\u539f\u59cb PDF \u622a\u56fe",
        "\u7b2c 3-5 \u9875",
    ):
        assert label in rendered
    assert "References" not in rendered
    assert "Parsed table" not in rendered
    assert "Original PDF crop" not in rendered
    assert state == before


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_explicit_language_wins_and_bibliography_never_selects_language(language: str) -> None:
    state = _claim_state()
    state["language"] = language
    state["title"] = "\u82f1\u6587\u62a5\u544a" if language == "en" else "English report title"
    state["ledger"]["files"]["file-private"]["title"] = "\u4e2d\u6587\u53c2\u8003\u6587\u732e"
    rendered = render_html_report(state)
    assert f'<html lang="{language}">' in rendered
    assert ("<h2>References</h2>" in rendered) is (language == "en")
    state.pop("language")
    state["title"] = "English report title"
    assert '<html lang="en">' in render_html_report(state)


@pytest.mark.parametrize("language", ["fr", "zh", [], {"language": "en"}])
def test_invalid_explicit_language_is_not_silently_reinterpreted(language: Any) -> None:
    state = _state()
    state["language"] = language
    with pytest.raises(ValueError, match="unsupported_report_language"):
        render_html_report(state)


def test_chinese_text_version_citation_and_source_note_are_localized_at_end() -> None:
    state = _claim_state()
    state["language"] = "zh-CN"
    state["ledger"]["files"]["file-private"]["filename"] = "returns.md"
    rendered = render_html_report(state)
    assert "[1\uff0c\u6587\u5b57\u7248]" in rendered
    assert "text version" not in rendered
    assert (
        rendered.index("</main>")
        < rendered.index("\u53c2\u8003\u6587\u732e")
        < rendered.index("\u8d44\u6599\u8bf4\u660e")
    )
    assert rendered.count('class="table-quality-note"') == 1
    assert "\u5b8c\u6574\u539f\u56fe\u6838\u5bf9" in rendered
    assert "not_performed" not in rendered


@pytest.mark.parametrize(
    "warnings, expected",
    [
        (["The year column is missing."], "\u8868\u683c\u7f3a\u5c11\u5e74\u4efd\u5217\u3002"),
        (["Missing year column <2026>."], "\u7f3a\u5c11\u5e74\u4efd\u5217\uff1a&lt;2026&gt;\u3002"),
        (
            ["Annual totals mismatch the crop."],
            "\u5e74\u5ea6\u5408\u8ba1\u4e0e\u539f\u59cb\u622a\u56fe\u4e0d\u4e00\u81f4\u3002",
        ),
        (["\u7f3a\u5c11 2026 \u5e74\u5217\u3002"], "\u7f3a\u5c11 2026 \u5e74\u5217\u3002"),
        (
            ["Novel mismatch <2027>."],
            "\u6838\u9a8c\u63d0\u793a\uff08\u539f\u6587\uff09\uff1aNovel mismatch &lt;2027&gt;.",
        ),
        ([], "\u5df2\u77e5\u8868\u683c\u63d0\u53d6\u6709\u5185\u5bb9\u9057\u6f0f\u3002"),
    ],
)
def test_chinese_warnings_stay_beside_image_and_machine_assessment_is_unchanged(
    warnings: list[str], expected: str
) -> None:
    state = _state()
    state["language"] = "zh-CN"
    table = state["ledger"]["tables"]["table-private-0"]
    assessment = _assessment(
        table, visualCheck="mismatch", sourceCompleteness="known_incomplete", warnings=warnings
    )
    state["ledger"]["tableAssessments"] = {"table-private-0": assessment}
    before = copy.deepcopy(state)
    rendered = render_html_report(state)
    assert rendered.index("<img ") < rendered.index(expected) < rendered.index("</figure>")
    assert table_quality_view(table, assessment=assessment)["visualCheck"] == "mismatch"
    assert state == before


def test_known_incomplete_without_visual_check_still_discloses_no_review() -> None:
    state = _state()
    state["language"] = "zh-CN"
    table = state["ledger"]["tables"]["table-private-0"]
    state["ledger"]["tableAssessments"] = {
        "table-private-0": _assessment(
            table, visualCheck="not_performed", sourceCompleteness="known_incomplete"
        )
    }
    rendered = render_html_report(state)
    assert "\u5df2\u77e5\u8868\u683c\u63d0\u53d6\u6709\u5185\u5bb9\u9057\u6f0f\u3002" in rendered
    assert "\u5b8c\u6574\u539f\u56fe\u6838\u5bf9" in rendered


def test_chinese_safety_warning_does_not_change_original_image_or_print_rules() -> None:
    state = _state()
    state["language"] = "zh-CN"
    state["ledger"]["tables"]["table-private-0"] = _table(
        '<table><tr><td colspan="9999">bad</td></tr></table>', key="table-private-0"
    )
    rendered = render_html_report(state)
    assert "\u8868\u683c\u6587\u5b57\u65e0\u6cd5\u5b89\u5168\u663e\u793a" in rendered
    assert "9999" not in rendered
    image = re.search(r'src="data:image/png;base64,([^"]+)"', rendered)
    assert image is not None
    assert base64.b64decode(image[1]) == PNG
    assert ".parsed-table { display: none; }" in rendered
    assert not re.search(r'(?:src|href)="(?:https?:|file:|/)', rendered)


def test_compact_rows_save_cell_metadata_and_do_not_duplicate_header() -> None:
    source = _table(HTML.replace("<th", "<td").replace("</th>", "</td>"))
    summary = summarize_table(source)
    parsed = summary["tables"][0]
    rows = parsed["headerRows"] + parsed["representativeRows"]
    assert len({row["rowIndex"] for row in rows}) == len(rows)
    legacy_rows = [
        {
            "rowIndex": row["rowIndex"],
            "omittedCellCount": 0,
            "cells": [
                {
                    "text": cell,
                    "tag": "td",
                    "alignment": None,
                    "rowspan": 1,
                    "colspan": 1,
                    "textTruncated": False,
                }
                for cell in row["cells"]
            ],
        }
        for row in rows
    ]

    def encoded(value: Any) -> int:
        return len(json.dumps(value, separators=(",", ":")).encode())

    assert encoded(rows) < encoded(legacy_rows) * 0.4
    assert all(isinstance(cell, str) for row in rows for cell in row["cells"])


def test_sparse_metadata_is_bounded_and_tracks_omitted_cells_when_compacted() -> None:
    source = _table(
        "<table><thead><tr>"
        + '<th colspan="2">'
        + "x" * 300
        + "</th>"
        + "<th>x</th>" * 7
        + '<th rowspan="2">'
        + "y" * 300
        + "</th>"
        + "<th>x</th>" * 31
        + "</tr></thead></table>"
    )
    summary = summarize_table(source)
    row = summary["tables"][0]["headerRows"][0]
    assert row["spans"] == [[0, 1, 2], [8, 2, 1]]
    assert row["truncatedCellIndexes"] == [0, 8]
    assert row["omittedCellCount"] == 8
    reduced = _compact_summary(summary)
    compact_row = reduced["tables"][0]["headerRows"][0]
    assert compact_row["spans"] == [[0, 1, 2]]
    assert compact_row["truncatedCellIndexes"] == [0]
    assert compact_row["omittedCellCount"] == 32
    assert len(compact_row["cells"][0]) == 64
    assert row["spans"] == [[0, 1, 2], [8, 2, 1]]
    assert _compact_summary(summary, metadata_only=True)["tables"] == []


def test_span_in_unsampled_row_still_warns_against_using_a_flat_grid() -> None:
    source = _table(
        "<table><tr><th>A</th><th>B</th></tr>"
        "<tr><td>a</td><td>1</td></tr>"
        '<tr><td rowspan="2">b</td><td>2</td></tr>'
        "<tr><td>3</td></tr>"
        "<tr><td>c</td><td>4</td></tr></table>"
    )
    parsed = summarize_table(source)["tables"][0]
    assert [row["rowIndex"] for row in parsed["representativeRows"]] == [1, 3, 4]
    assert "Omitted rows may carry spans" in parsed["spanLayout"]
    assert parsed["omittedBodyRowCount"] == 1


@pytest.mark.parametrize(
    "values",
    [
        ["2026E", "2027E", "6M", "3M", "#DIV/0!"],
        ["Current", "Low", "Mean", "9.6", "10.0", ""],
        ["KRW trillion", "%", "116", "274", "328", "0"],
        ["MSCI Korea", "KOSPI", "10,000", "8,500", "6,000"],
    ],
)
def test_audit_sample_values_survive_summary_and_canonical_paging(values: list[str]) -> None:
    content = (
        '<table><tr><th colspan="'
        + str(len(values))
        + '">Assumptions</th></tr><tr>'
        + "".join(f"<td>{value}</td>" for value in values)
        + "</tr></table>"
    )
    source = _table(content)
    before = copy.deepcopy(source)
    parsed = summarize_table(source)["tables"][0]
    assert parsed["representativeRows"][0]["cells"] == values
    assert parsed["headerRows"][0]["spans"] == [[0, 1, len(values)]]
    fragments = []
    start = 0
    while start < len(content):
        page = project_table_page(source, start=start, target_chars=17, frame_bytes=_measure)
        fragments.append(page["text"]["content"])
        start = page["page"]["end"]
    assert "".join(fragments) == content
    assert source == before


def test_similar_translated_titles_and_exact_evidence_do_not_merge_distinct_sources() -> None:
    state = _references(["Korea outlook", "\u97e9\u56fd\u5e02\u573a\u5c55\u671b"])
    second = state["ledger"]["files"]["file-1"]
    second["sourcePath"] = "other/2026-07-05/research.md"
    for evidence in state["ledger"]["evidence"].values():
        evidence["content"] = "The same cited passage, not proof of identical documents."
    before = copy.deepcopy(state)
    assert build_bibliography(state)["sourceCount"] == 2
    assert state == before


@pytest.mark.parametrize(
    "title", ["Market outlook", "HSBC | Korea outlook", "\u9ad8\u76db\u97e9\u56fd\u5468\u62a5"]
)
def test_source_folder_is_not_publisher_identity(title: str) -> None:
    state = _references([title])
    before = copy.deepcopy(state)
    reference = build_bibliography(state)["references"][0]
    assert "Goldman Sachs" not in reference["title"]
    assert title in reference["title"]
    assert reference["titleSelection"]["basis"] == "clean_metadata_title"
    assert state == before


def test_real_publisher_in_title_survives_english_and_chinese_rendering() -> None:
    state = _claim_state()
    title = "2026-07-05 Goldman Sachs | Korea outlook: EPS +5% & P/E < 10"
    state["ledger"]["files"]["file-private"]["title"] = title
    before = build_bibliography(state)
    for language in ("en", "zh-CN"):
        rendered = render_html_report({**state, "language": language})
        assert html.escape(title) in rendered
        assert build_bibliography(state) == before


def test_ancestor_storage_date_is_not_a_report_date() -> None:
    state = _references(["HSBC | Korea outlook"])
    source = state["ledger"]["files"]["file-0"]
    source["filename"] = "research.pdf"
    source["sourcePath"] = "goldman/2026-09-03/research.pdf"
    assert build_bibliography(state)["references"][0]["title"] == "HSBC | Korea outlook"


@pytest.mark.parametrize("title", [None, "", "[page 1]"])
def test_unconfirmed_long_parent_never_replaces_real_filename(title: str | None) -> None:
    state = _references(["[page 1]"])
    source = state["ledger"]["files"]["file-0"]
    source.update(
        title=title,
        filename="research.pdf",
        sourcePath="archive/2026-09-03+customer-import-batch-not-a-document-title/research.pdf",
    )
    before = copy.deepcopy(state)
    reference = build_bibliography(state)["references"][0]
    assert reference["title"] == "research"
    assert reference["titleSelection"]["basis"] == "source_filename_fallback"
    assert state == before


@pytest.mark.parametrize("verified", [False, True])
def test_only_confirmed_source_package_supplies_full_fallback_title(verified: bool) -> None:
    state = _references(["[page 1]"])
    stem = "2026-07-05+Korea Weekly Kickstart Market performance"
    parent = stem + " and earnings"
    filename = stem + "~abcdef123456.pdf"
    state["ledger"]["files"]["file-0"].update(
        filename=filename,
        sourcePath=f"archive/{parent}/{filename}",
        verificationStatus="verified" if verified else "unverified",
    )
    before = copy.deepcopy(state)
    reference = build_bibliography(state)["references"][0]
    assert reference["title"] == (parent if verified else stem).replace("+", " ")
    assert state == before


@pytest.mark.parametrize("extension", ["pdf", "md"])
def test_short_imported_basename_does_not_confirm_package_lineage(extension: str) -> None:
    state = _references(["[page 1]"])
    parent = "2026-07-05+Korea Weekly Kickstart Market performance and earnings"
    filename = f"2~aaaabbbb.{extension}"
    state["ledger"]["files"]["file-0"].update(
        filename=filename,
        sourcePath=f"archive/{parent}/{filename}",
    )
    before = copy.deepcopy(state)
    reference = build_bibliography(state)["references"][0]
    assert reference["title"] == "2"
    assert reference["titleSelection"]["basis"] == "source_filename_fallback"
    assert reference["groupingBasis"] == "distinct_source_file"
    assert state == before


def test_title_date_is_not_prefixed_with_a_conflicting_filename_date() -> None:
    title = "HSBC | Korea outlook - 2026-07-09"
    state = _references([title])
    label = build_bibliography(state)["references"][0]["title"]
    assert label == title
    assert "2026-07-05" not in label


@pytest.mark.parametrize("closed_markup", [False, True])
def test_exact_320_preview_never_upgrades_even_when_markup_is_closed(closed_markup: bool) -> None:
    preview = (
        "<table><tr><th>Year</th><th>Value</th></tr><tr><td>2026</td><td>8.5</td></tr></table>"
    )
    preview = (
        preview.ljust(320) if closed_markup else preview.replace("8.5", "8.5" + "x" * 500)[:320]
    )
    assert len(preview) == 320
    source = {
        "tableId": "preview-only",
        "textFormat": "html",
        "textPreview": preview,
        "textTruncated": True,
        "textPreviewTruncatedForTransport": False,
        "tableTextProjection": "complete",
    }
    before = copy.deepcopy(source)
    view = summarize_table(source)
    for projected in (view, _compact_summary(view), _compact_summary(view, metadata_only=True)):
        assert projected["inputCompleteness"] == "truncated"
        assert projected["parseComplete"] is False
        assert projected["summaryTruncated"] is True
    if closed_markup:
        assert view["issues"] == []
    else:
        assert "unclosed_markup" in view["issues"]
    with pytest.raises(TableViewError, match="SOURCE_CONTENT_UNAVAILABLE"):
        project_table_page(source, start=0, frame_bytes=_measure)
    assert source == before


def test_span_coordinates_are_source_cell_indexes_not_expanded_columns() -> None:
    content = (
        '<table><thead><tr><th colspan="2">Year</th><th rowspan="2">Units</th></tr>'
        "<tr><th>2026E</th><th>2027E</th></tr></thead><tbody>"
        '<tr><td rowspan="2">Year</td><td>0</td><td>%</td></tr>'
        "<tr><td></td><td>KRW</td></tr>"
        "<tr><td>Year</td><td>0</td><td>%</td></tr></tbody></table>"
    )
    view = summarize_table(_table(content))
    parsed = view["tables"][0]
    assert parsed["headerRows"] == [
        {"rowIndex": 0, "cells": ["Year", "Units"], "spans": [[0, 1, 2], [1, 2, 1]]},
        {"rowIndex": 1, "cells": ["2026E", "2027E"]},
    ]
    assert parsed["representativeRows"] == [
        {"rowIndex": 2, "cells": ["Year", "0", "%"], "spans": [[0, 2, 1]]},
        {"rowIndex": 3, "cells": ["", "KRW"]},
        {"rowIndex": 4, "cells": ["Year", "0", "%"]},
    ]
    assert parsed["omittedHeaderRowCount"] == parsed["omittedBodyRowCount"] == 0
    assert view["summaryTruncated"] is False


def test_first_numeric_td_row_is_preserved_despite_heuristic_header_label() -> None:
    view = summarize_table(_table("<table><tr><td>116</td><td>0</td><td></td></tr></table>"))
    parsed = view["tables"][0]
    assert parsed["headerBasis"] == "heuristic_first_td_row"
    assert parsed["headerRows"] == [{"rowIndex": 0, "cells": ["116", "0", ""]}]
    assert parsed["representativeRows"] == []
    assert parsed["omittedBodyRowCount"] == 0
    assert view["summaryTruncated"] is False


@pytest.mark.parametrize("language", ["en", "zh-CN"])
def test_unknown_quality_note_is_only_at_end_and_never_a_lead_warning(language: str) -> None:
    state = {**_state(2), "language": language}
    rendered = render_html_report(state)
    body = rendered.split("<body>", 1)[1]
    main = body.split("<main>", 1)[1].split("</main>", 1)[0]
    assert 'class="table-warning"' not in main
    assert 'class="table-quality-note"' not in main
    assert body.index('class="source-notes"') > body.index('class="references"')
    assert body.count('class="table-quality-note"') == 1
    assert body.count("<img ") == 2


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("visual", ["not_performed", "mismatch", "failed"])
def test_concrete_footnote_and_value_warning_survives_next_to_original_crop(
    language: str, visual: str
) -> None:
    state = {**_state(), "language": language}
    table = state["ledger"]["tables"]["table-private-0"]
    warning = "Missing footnote: <2027E> is a forecast; the 328 value is absent from parsed text."
    state["ledger"]["tableAssessments"] = {
        "table-private-0": _assessment(
            table, visualCheck=visual, sourceCompleteness="known_incomplete", warnings=[warning]
        )
    }
    before = copy.deepcopy(state)
    rendered = render_html_report(state)
    expected = html.escape(warning)
    assert rendered.index("<img ") < rendered.index(expected) < rendered.index("</figure>")
    assert rendered.count(expected) == 1
    assert not re.search(r"\.table-warning\s*\{[^}]*display:\s*none", rendered)
    assert state == before


@pytest.mark.parametrize("mode", ["standard", "deep"])
@pytest.mark.parametrize("language", [None, "en", "zh-CN"])
def test_integrated_begin_language_and_mode_reach_renderer_without_quality_claims(
    tmp_path: Path, mode: str, language: str | None
) -> None:
    bridge, store, upstream, _ = setup(tmp_path)
    arguments = {"title": "\u4e2d\u6587\u62a5\u544a", "mode": mode}
    if language is not None:
        arguments["language"] = language
    begin, envelope = invoke(bridge, "researchBegin", arguments)
    assert envelope["result"]["isError"] is False
    state = store.snapshot(begin["researchId"])
    assert state["mode"] == mode
    assert state.get("language") == language
    rendered = render_html_report(store._state_for_render(state))
    assert f'<html lang="{language or "zh-CN"}">' in rendered
    assert "server_authoritative" not in rendered
    assert "verificationStatus" not in rendered
    assert "visualCheck" not in rendered
    assert not upstream.calls
    legacy = {key: value for key, value in state.items() if key not in {"mode", "language"}}
    if language is None:
        assert render_html_report(legacy) == rendered
