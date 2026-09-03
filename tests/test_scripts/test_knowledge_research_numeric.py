from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research.numeric_checks import numeric_scale_checks
from tests.test_scripts.test_knowledge_research_navigation import (
    invoke,
    result,
    search_payload,
    setup,
)


@pytest.mark.parametrize(
    "text, flagged",
    [
        ("1160\u4ebf\u97e9\u5143\u7ea7\u522b\u3001\u5373116\u4e07\u4ebf\u97e9\u5143", True),
        ("1\u4e07\u4ebf\u97e9\u5143\u537310,000\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u97e9\u5143\u53739.999\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u97e9\u5143\u537310\u4ebf\u97e9\u5143", True),
        ("-1\u4ebf\u97e9\u5143\u5373-100\u4ebf\u97e9\u5143", True),
        ("0\u4ebf\u97e9\u5143\u5373100\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u7f8e\u5143\u76f8\u5f53\u4e8e100\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u97e9\u5143\u4e0d\u7b49\u4e8e100\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u97e9\u5143\u3002\u5373100\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u97e9\u5143\n\u5373100\u4ebf\u97e9\u5143", False),
        ("1\u81f310\u4ebf\u97e9\u5143\u5373100\u4ebf\u97e9\u5143", False),
        ("1-10\u4ebf\u97e9\u5143\u5373100\u4ebf\u97e9\u5143", False),
        ("1 -10\u4ebf\u97e9\u5143\u5373100\u4ebf\u97e9\u5143", False),
        ("1e3\u4ebf\u97e9\u5143\u53731000\u4ebf\u97e9\u5143", False),
        ("1\u4ebf\u97e9\u5143\u76f8\u5f53\u4e8e100\u4ebf\u97e9\u5143\u76841%", False),
        ("1\u4ebf\u97e9\u5143\u5373100\u4ebf\u97e9\u5143\u76841%", False),
        ("1\u4ebf\u97e9\u5143\u537310\u4ebf\u97e9\u5143\u81f3100\u4ebf\u97e9\u5143", False),
        (
            "\u6b64\u524d\u8bef\u5199\u4e86\u5e74\u4efd\u30021160\u4ebf\u97e9\u5143\u7ea7\u522b\u3001\u5373116\u4e07\u4ebf\u97e9\u5143",
            True,
        ),
        ("1\u4ebf\u97e9\u5143\u589e\u957f\u5230100\u4ebf\u97e9\u5143", False),
        (
            "\u9519\u8bef\u6362\u7b97\u793a\u4f8b\uff1a1\u4ebf\u97e9\u5143\u5373100\u4ebf\u97e9\u5143",
            False,
        ),
    ],
)
def test_only_gross_direct_same_currency_equivalences(text: str, flagged: bool) -> None:
    state: dict[str, Any] = {
        "report": {
            "items": [
                {
                    "kind": "claim",
                    "claimKey": "k",
                    "section": "Finding",
                    "text": text,
                    "evidenceIds": ["e"],
                }
            ]
        }
    }
    before = copy.deepcopy(state)
    checks = numeric_scale_checks(state)
    assert bool(checks) is flagged
    assert state == before
    if flagged:
        assert checks[0]["code"] == "NUMBER_SCALE_MISMATCH"
        assert all(isinstance(value, str) for value in checks[0]["normalizedAmounts"])


def test_numeric_gate_requires_correction_and_new_review(tmp_path: Path) -> None:
    from tests.test_scripts.test_knowledge_research_review import _source_details

    source = search_payload("q", ["file-a"], "Nomura forecasts KRW 116 trillion for 2026.")
    scoped = search_payload("scope", ["file-a"], source["results"][0]["content"], scoped=True)
    bridge, store, _, _ = setup(
        tmp_path, [result(source), result(scoped), result(_source_details(source))]
    )
    begin, _ = invoke(bridge, "researchBegin", {"title": "Report", "mode": "deep"})
    common = {"researchId": begin["researchId"]}
    found, _ = invoke(bridge, "search", {**common, "query": "q"})
    invoke(bridge, "searchByIds", {**common, "query": "scope", "scopeRefs": [found["scopeRef"]]})
    claim = {
        "claimKey": "forecast",
        "section": "Forecast",
        "text": "1160\u4ebf\u97e9\u5143\u7ea7\u522b\u3001\u5373116\u4e07\u4ebf\u97e9\u5143",
        "evidenceRefs": [found["results"][0]["evidenceRef"]],
    }
    invoke(bridge, "researchAddClaims", {**common, "claims": [claim], "batchKey": "draft"})
    invoke(bridge, "researchNavigate", {**common, "view": "review"})
    pending = store.finalize(research_id=begin["researchId"])
    assert [check["code"] for check in pending["checks"]] == ["NUMBER_SCALE_MISMATCH"]
    assert "publicArtifactManifest" not in pending
    assert not store.output_root.exists()
    current = pending["checks"][0]["expectedClaimHash"]
    invoke(
        bridge,
        "researchAddClaims",
        {
            **common,
            "batchKey": "corrected",
            "claims": [
                {
                    **claim,
                    "text": "116\u4e07\u4ebf\u97e9\u5143\u53731,160,000\u4ebf\u97e9\u5143",
                    "expectedClaimHash": current,
                }
            ],
        },
    )
    pending = store.finalize(research_id=begin["researchId"])
    assert [check["code"] for check in pending["checks"]] == ["SOURCE_COMPARISON_REQUIRED"]
    invoke(bridge, "researchNavigate", {**common, "view": "review"})
    assert store.finalize(research_id=begin["researchId"])["status"] == "finalized"
