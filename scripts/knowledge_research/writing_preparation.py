"""Observable first-draft prerequisites, not research or semantic certification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

if __package__:
    from .claims import ResearchStateError, sha256_json
    from .references import source_format
else:  # pragma: no cover - standalone bridge
    from claims import ResearchStateError, sha256_json  # type: ignore[import-not-found,no-redef]
    from references import source_format  # type: ignore[import-not-found,no-redef]


def scoped_search_count(state: Mapping[str, Any]) -> int:
    return sum(
        call.get("toolName") == "searchByIds" and call.get("verificationStatus") == "verified"
        for call in state["ledger"]["calls"]
    )


def scoped_search_check() -> dict[str, Any]:
    return {
        "code": "SCOPED_SEARCH_REQUIRED",
        "tool": "searchByIds",
        "message": (
            "Deep research has not searched within any candidate file. Use selection "
            "with returned scopes or files for context, assumptions or "
            "counterevidence. A verified empty-result search counts; grouping does not."
        ),
    }


def _read_gap(navigation: Mapping[str, Any], record: Mapping[str, Any]) -> tuple[int, str | None]:
    """Return the first unprojected offset using only explicit evidence-read snapshots."""
    intervals: list[tuple[int, int]] = []
    resume_snapshot = None
    for projection in navigation.get("projections", {}).values():
        if projection.get("projectionPrepared") is not True:
            continue
        snapshot_ref = projection.get("snapshotRef")
        snapshot = navigation.get("snapshots", {}).get(snapshot_ref, {})
        payload = snapshot.get("payload", {})
        if (
            snapshot.get("kind") != "evidence"
            or snapshot.get("orderedIds") != [record["evidenceId"]]
            or snapshot.get("sourceRevisions") != [record["revision"]]
            or payload.get("contentSha256") != record["contentSha256"]
        ):
            continue
        page = projection.get("range", {})
        left, right = page.get("start"), page.get("end")
        if type(left) is int and type(right) is int and 0 <= left < right <= len(record["content"]):
            intervals.append((left, right))
            resume_snapshot = str(snapshot_ref)
    covered = 0
    for left, right in sorted(intervals):
        if left > covered:
            break
        covered = max(covered, right)
    return covered, resume_snapshot


def require_first_write_preparation(
    state: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]
) -> None:
    if state.get("mode") != "deep" or any(
        item.get("kind") == "claim" for item in state["report"]["items"]
    ):
        return

    navigation = state.get("extensions", {}).get("navigation", {})
    ledger = state["ledger"]
    evidence_ids = list(dict.fromkeys(key for item in claims for key in item["evidenceIds"]))
    file_ids = list(dict.fromkeys(ledger["evidence"][key]["fileId"] for key in evidence_ids))
    checks = []
    if not scoped_search_count(state):
        check = scoped_search_check()
        file_refs = [
            navigation["files"][key]["ref"]
            for key in file_ids
            if key in navigation.get("files", {})
        ]
        if file_refs:
            check["suggestedSelection"] = {"selection": {"kind": "files", "refs": file_refs[:20]}}
        checks.append(check)

    for evidence_id in evidence_ids:
        record = ledger["evidence"][evidence_id]
        covered, snapshot_ref = _read_gap(navigation, record)
        if covered == len(record["content"]):
            continue
        arguments: dict[str, Any] = {"researchId": state["researchId"]}
        evidence_ref = navigation.get("evidence", {}).get(evidence_id, {}).get("ref")
        if evidence_ref:
            arguments["evidenceRef"] = evidence_ref
        if snapshot_ref:
            # Same signed offset format as Navigation.cursor; resume at the first gap.
            digest = sha256_json(
                [snapshot_ref, navigation["snapshots"][snapshot_ref]["hash"], covered]
            )
            arguments["cursor"] = f"{snapshot_ref}:{covered}:{digest[:24]}"
        checks.append(
            {
                "code": "EVIDENCE_READ_REQUIRED",
                "tool": "researchReadEvidence",
                "arguments": arguments,
                "message": (
                    "Read this first-batch cited excerpt and its continuations before writing. "
                    "Discovery and review projections do not replace explicit evidence reading. "
                    "Complete projection does not certify comprehension."
                ),
            }
        )

    explicit_files = {
        call["arguments"].get("fileId")
        for call in ledger["calls"]
        if call.get("toolName") == "getFileDetails"
        and call.get("verificationStatus") == "verified"
        and call.get("purpose") != "bibliography_metadata"
    }
    for file_id in file_ids:
        file = {
            **ledger["files"][file_id],
            **navigation.get("fileMetadata", {}).get(file_id, {}),
        }
        if source_format(file) != "PDF" and str(file.get("fileType", "")).lower() != "pdf":
            continue
        inventory = ledger["inventories"].get(file_id, {})
        if file_id in explicit_files and inventory.get("complete") is True:
            continue
        arguments = {"researchId": state["researchId"]}
        file_ref = navigation.get("files", {}).get(file_id, {}).get("ref")
        if file_ref:
            arguments["fileRef"] = file_ref
        checks.append(
            {
                "code": "PDF_TABLE_INSPECTION_REQUIRED",
                "tool": "getFileDetails",
                "arguments": arguments,
                "message": (
                    "Explicitly inspect this first-batch cited PDF's complete table inventory. "
                    "Follow returned pages and fetch relevant usable tables. Automatic "
                    "bibliography metadata does not count; no table quantity is required."
                ),
            }
        )
    if checks:
        raise ResearchStateError(
            "Complete the missing deep-research preparation, then retry this uncommitted batch.",
            details={"code": "RESEARCH_PREPARATION_REQUIRED", "committed": False, "checks": checks},
        )
