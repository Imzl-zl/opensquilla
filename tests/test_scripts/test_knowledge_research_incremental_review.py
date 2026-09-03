from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from scripts.knowledge_research.claims import claim_hash
from scripts.knowledge_research.navigation import Navigation
from scripts.knowledge_research.review import review_preparation
from tests.test_scripts.test_knowledge_research_navigation import (
    invoke,
    result,
    search_payload,
    setup,
)


def seeded(tmp_path: Path, count: int = 3) -> tuple[Any, Any, str]:
    source = search_payload("q", [f"file-{i}" for i in range(count)])
    bridge, store, _, rid = setup(tmp_path, [result(source)])
    invoke(bridge, "search", {"researchId": rid, "query": "q", "limit": 20})
    store.add_claims(
        research_id=rid,
        claims=[
            {
                "claimKey": f"fact-{i}",
                "section": "Findings",
                "text": "Source-bound fact.",
                "evidenceIds": [row["evidenceId"]],
            }
            for i, row in enumerate(source["results"])
        ],
        batch_key="draft",
    )
    return bridge, store, rid


def read_pending(bridge: Any, rid: str) -> dict[str, Any]:
    first, response = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review"})
    assert not response["result"]["isError"], first
    page = first
    while page["nextCursor"]:
        page, response = invoke(
            bridge, "researchNavigate", {"researchId": rid, "cursor": page["nextCursor"]}
        )
        assert not response["result"]["isError"], page
    return first


def test_one_claim_revision_does_not_repeat_unchanged_comparisons(tmp_path: Path) -> None:
    bridge, store, rid = seeded(tmp_path, 20)
    first = read_pending(bridge, rid)
    assert first["pendingItemCount"] == 20
    assert review_preparation(store.snapshot(rid))["preparedItemCount"] == 20
    old = store.snapshot(rid)["report"]["items"][0]
    store.add_claims(
        research_id=rid,
        batch_key="revision",
        claims=[
            {
                "claimKey": old["claimKey"],
                "section": old["section"],
                "text": "A corrected source-bound fact.",
                "evidenceIds": old["evidenceIds"],
                "expectedClaimHash": claim_hash(old),
            }
        ],
    )
    pending = review_preparation(store.snapshot(rid))
    assert pending["preparedItemCount"] == 19
    assert pending["pendingItemCount"] == 1
    second = read_pending(bridge, rid)
    assert second["pendingItemCount"] == 1
    assert second["reusedItemCount"] == 19
    assert len(second["entries"]) == 2
    assert review_preparation(store.snapshot(rid))["comparisonPrepared"]
    empty = read_pending(bridge, rid)
    assert empty["entries"] == [] and empty["nextCursor"] is None
    assert empty["reusedItemCount"] == 20
    assert "reviewGroups" not in empty and "_reviewGroups" not in empty


@pytest.mark.parametrize("change", ["content", "locator", "revision", "institution", "binding"])
def test_dependency_changes_only_invalidate_affected_items(tmp_path: Path, change: str) -> None:
    bridge, store, rid = seeded(tmp_path)
    read_pending(bridge, rid)

    def mutate(state: dict[str, Any]) -> None:
        item = state["report"]["items"][0]
        evidence = state["ledger"]["evidence"][item["evidenceIds"][0]]
        if change == "binding":
            item["evidenceIds"] = state["report"]["items"][1]["evidenceIds"]
        elif change == "institution":
            state["ledger"]["files"][evidence["fileId"]]["institution"] = "Correct institution"
        elif change == "locator":
            evidence["locator"]["pageStart"] = 2
        else:
            evidence[change] = "changed"

    store.atomic_update(rid, mutate)
    before = review_preparation(store.snapshot(rid))
    assert before["pendingItemCount"] == 1
    if change == "revision":
        blocked, response = invoke(
            bridge, "researchNavigate", {"researchId": rid, "view": "review"}
        )
        assert response["result"]["isError"]
        assert blocked["details"]["code"] == "REVISION_CONFLICT"
        return
    second = read_pending(bridge, rid)
    assert second["pendingItemCount"] == 1 and second["reusedItemCount"] == 2


def test_partial_group_and_stale_cursor_cannot_clear_changed_item(tmp_path: Path) -> None:
    bridge, store, rid = seeded(tmp_path)
    first, _ = invoke(bridge, "researchNavigate", {"researchId": rid, "view": "review", "limit": 1})
    assert review_preparation(store.snapshot(rid))["preparedItemCount"] == 0
    nav = Navigation(store.snapshot(rid))
    second_group, _ = invoke(
        bridge,
        "researchNavigate",
        {"researchId": rid, "cursor": nav.cursor(first["snapshotRef"], 2), "limit": 2},
    )
    assert review_preparation(store.snapshot(rid))["preparedItemCount"] == 1
    old_state = store.snapshot(rid)
    store.atomic_update(rid, lambda state: state["report"]["items"][0].update(text="Changed fact."))
    invoke(
        bridge,
        "researchNavigate",
        {"researchId": rid, "cursor": first["nextCursor"], "limit": 1},
    )
    assert review_preparation(store.snapshot(rid))["preparedItemCount"] == 1
    fresh = read_pending(bridge, rid)
    assert fresh["pendingItemCount"] == 2 and fresh["reusedItemCount"] == 1
    assert review_preparation(store.snapshot(rid))["comparisonPrepared"]
    assert second_group["snapshotRef"] == first["snapshotRef"]
    assert old_state["report"]["items"][0]["text"] == "Source-bound fact."


def test_legacy_snapshot_does_not_claim_incremental_verification(tmp_path: Path) -> None:
    bridge, store, rid = seeded(tmp_path)
    read_pending(bridge, rid)
    state = copy.deepcopy(store.snapshot(rid))
    for snapshot in state["extensions"]["navigation"]["snapshots"].values():
        snapshot.pop("reviewProtocol", None)
        snapshot.pop("reviewGroups", None)
    assert review_preparation(state)["pendingItemCount"] == 3


def test_new_claim_with_shared_source_still_requires_own_comparison(tmp_path: Path) -> None:
    bridge, store, rid = seeded(tmp_path)
    read_pending(bridge, rid)
    old = store.snapshot(rid)["report"]["items"][0]
    store.add_claims(
        research_id=rid,
        batch_key="new",
        claims=[
            {
                "claimKey": "new",
                "section": "Another interpretation",
                "text": "A different claim about the same source.",
                "evidenceIds": old["evidenceIds"],
            }
        ],
    )
    assert review_preparation(store.snapshot(rid))["pendingItemCount"] == 1
    fresh = read_pending(bridge, rid)
    assert [x["kind"] for x in fresh["entries"]] == ["claim", "evidence"]


def test_caption_revision_preserves_other_prepared_claims(tmp_path: Path) -> None:
    from scripts.knowledge_research.review import table_item_hash
    from tests.test_scripts.test_knowledge_research_claims import _seed_table

    bridge, store, rid = seeded(tmp_path)
    table_id = _seed_table(store, rid)
    store.add_table(research_id=rid, table_id=table_id, section="Tables", caption="Original")
    read_pending(bridge, rid)
    table = store.snapshot(rid)["report"]["items"][-1]
    store.add_table(
        research_id=rid,
        table_id=table_id,
        section="Tables",
        caption="Corrected caption",
        expected_table_hash=table_item_hash(table),
    )
    fresh = read_pending(bridge, rid)
    assert fresh["pendingItemCount"] == 1 and fresh["reusedItemCount"] == 3
    assert fresh["entries"][0]["caption"] == "Corrected caption"


def test_cached_artifacts_return_current_review_without_rewriting_history(tmp_path: Path) -> None:
    bridge, store, rid = seeded(tmp_path)
    read_pending(bridge, rid)
    first = store.finalize(research_id=rid)
    paths = [tmp_path / row["path"] for row in first["publicArtifactManifest"]["files"]]
    original = [path.read_bytes() for path in paths]

    def older_receipt(state: dict[str, Any]) -> None:
        state["finalized"]["receipt"]["review"] = {
            "reportHash": "older-protocol-hash",
            "comparisonPrepared": True,
        }

    store.atomic_update(rid, older_receipt)
    replay = store.finalize(research_id=rid)
    assert replay["review"] == review_preparation(store.snapshot(rid))
    assert replay["review"]["preparedItemCount"] == 3
    assert replay["publicArtifactManifest"] == first["publicArtifactManifest"]
    assert [path.read_bytes() for path in paths] == original
    assert (
        store.snapshot(rid)["finalized"]["receipt"]["review"]["reportHash"] == "older-protocol-hash"
    )
