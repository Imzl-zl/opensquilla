"""Pure validation and mutation preparation for report paragraph batches."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NoReturn

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BIBLIOGRAPHY_SECTION = re.compile(
    r"^(?:#{1,6}\s*)?(?:[0-9IVXLCDM\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341]+"
    r"\s*[.\u3001\uff0e:\uff1a)]?\s*)?"
    r"(?:references|bibliography|\u53c2\u8003\u6587\u732e|\u5f15\u7528\u6587\u732e|\u53c2\u8003\u8d44\u6599)"
    r"\s*[:\uff1a]?\s*$",
    re.IGNORECASE,
)


class ResearchStateError(ValueError):
    """A rejected research operation, optionally with machine-readable locations."""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details) if details is not None else {}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _reject(
    code: str, message: str, path: str, claim_key: str | None = None, **extra: Any
) -> NoReturn:
    issue = {"code": code, "path": path, **extra}
    if claim_key is not None:
        issue["claimKey"] = claim_key
    raise ResearchStateError(message, details={"code": code, "committed": False, "issues": [issue]})


def _text(value: Any, path: str, maximum: int, key: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        _reject("INVALID_ARGUMENT", "expected a non-empty string", path, key)
    clean = value.strip()
    if len(clean) > maximum:
        _reject("INVALID_ARGUMENT", f"text exceeds {maximum} characters", path, key)
    return str(clean)


def _key(value: Any, path: str) -> str:
    clean = _text(value, path, 128)
    if clean != value or any(ord(character) < 32 for character in clean):
        _reject("INVALID_KEY", "keys must not contain padding or control characters", path)
    return clean


def claim_hash(item: Mapping[str, Any]) -> str:
    return sha256_json({name: item[name] for name in ("section", "text", "evidenceIds")})


def invalidate_finalized(state: dict[str, Any]) -> None:
    """Retain legacy manifests too; invalidation never removes artifact files."""

    previous = state.get("finalized")
    if previous is not None:
        history = state.setdefault("artifactHistory", [])
        if previous not in history:
            history.append(json.loads(canonical_json(previous)))
    state["finalized"] = None


def apply_claim_batch(
    state: dict[str, Any],
    claims: Sequence[Mapping[str, Any]],
    *,
    batch_key: str | None,
    reject_text: Callable[[str], None],
    before_commit: Callable[[Sequence[Mapping[str, Any]]], None] | None = None,
) -> dict[str, Any]:
    """Prepare all changes before mutating state; the caller owns persistence."""

    if isinstance(claims, str | bytes) or not isinstance(claims, Sequence):
        _reject("INVALID_ARGUMENT", "claims must be an array", "/claims")
    if not claims or len(claims) > 40:
        _reject("INVALID_ARGUMENT", "claims must contain between 1 and 40 items", "/claims")
    if batch_key is not None:
        batch_key = _key(batch_key, "/batchKey")

    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()
    for offset, raw in enumerate(claims):
        path = f"/claims/{offset}"
        if not isinstance(raw, Mapping):
            _reject("INVALID_ARGUMENT", "each claim must be an object", path)
        key = _key(raw["claimKey"], f"{path}/claimKey") if "claimKey" in raw else None
        if key is not None:
            if key in keys:
                _reject("DUPLICATE_CLAIM_KEY", "claimKey repeats in this batch", path, key)
            keys.add(key)
        item: dict[str, Any] = {}
        for name, maximum in (("section", 200), ("text", 20_000)):
            item[name] = _text(raw.get(name), f"{path}/{name}", maximum, key)
            try:
                reject_text(item[name])
            except ResearchStateError as exc:
                _reject("INTERNAL_IDENTIFIER", str(exc), f"{path}/{name}", key)
        values = raw.get("evidenceIds", [])
        if isinstance(values, str | bytes) or not isinstance(values, Sequence) or not values:
            _reject(
                "INVALID_ARGUMENT",
                "evidenceIds must be a non-empty array",
                f"{path}/evidenceIds",
                key,
            )
        ids: list[str] = []
        for index, value in enumerate(values):
            location = f"{path}/evidenceIds/{index}"
            if not isinstance(value, str) or not value:
                _reject("INVALID_ARGUMENT", "evidence ID must be a non-empty string", location, key)
            record = state["ledger"]["evidence"].get(value)
            if not isinstance(record, Mapping) or record.get("verificationStatus") != "verified":
                _reject(
                    "UNVERIFIED_EVIDENCE_ID",
                    f"evidence was not verified by an actual search result: {value}",
                    location,
                    key,
                    evidenceId=value,
                )
            if value not in ids:
                ids.append(value)
        item["evidenceIds"] = ids
        if key is not None:
            item["claimKey"] = key
        if "expectedClaimHash" in raw:
            expected = raw["expectedClaimHash"]
            if key is None or not isinstance(expected, str) or _SHA256.fullmatch(expected) is None:
                _reject(
                    "INVALID_ARGUMENT",
                    "expectedClaimHash requires claimKey and a SHA256 digest",
                    f"{path}/expectedClaimHash",
                    key,
                )
            item["expectedClaimHash"] = expected
        normalized.append(item)

    request_hash = sha256_json(
        {"protocol": "claims/1", "researchId": state["researchId"], "claims": normalized}
    )
    batches = state.get("claimBatches", {})
    if batch_key is not None and batch_key in batches:
        previous = batches[batch_key]
        if previous["requestHash"] != request_hash:
            _reject(
                "BATCH_KEY_CONFLICT", "batchKey already committed a different request", "/batchKey"
            )
        return dict(json.loads(canonical_json(previous["receipt"])))

    items = list(state["report"]["items"])
    existing = {
        item["claimKey"]: index
        for index, item in enumerate(items)
        if item.get("kind") == "claim" and "claimKey" in item
    }
    prepared: list[dict[str, Any]] = []
    inserted = updated = unchanged = 0
    for offset, normalized_item in enumerate(normalized):
        item = dict(normalized_item)
        expected = item.pop("expectedClaimHash", None)
        key = item.get("claimKey")
        existing_index = existing.get(key) if key is not None else None
        digest = claim_hash(item)
        if (
            state.get("mode") == "deep"
            and _BIBLIOGRAPHY_SECTION.fullmatch(item["section"])
            and (existing_index is None or claim_hash(items[existing_index]) != digest)
        ):
            _reject(
                "RESERVED_REPORT_SECTION",
                "References are generated from substantive claims and tables. Omit manual "
                "bibliography paragraphs; do not relabel a source list as analysis. "
                "Resubmit the uncommitted batch with only substantive paragraphs.",
                f"/claims/{offset}/section",
                key,
            )
        if existing_index is None:
            if expected is not None:
                _reject(
                    "CLAIM_NOT_FOUND",
                    "claimKey has no committed paragraph",
                    f"/claims/{offset}",
                    key,
                )
            item.update(kind="claim", itemId=f"claim-{len(items) + 1:04d}")
            if key is not None:
                item["claimHash"] = digest
            items.append(item)
            inserted += 1
        else:
            previous = items[existing_index]
            current_hash = claim_hash(previous)
            if digest == current_hash:
                item = previous
                unchanged += 1
            else:
                if expected != current_hash:
                    _reject(
                        "CLAIM_KEY_CONFLICT" if expected is None else "CLAIM_HASH_CONFLICT",
                        "changing a paragraph requires its current expectedClaimHash",
                        f"/claims/{offset}/expectedClaimHash",
                        key,
                        currentClaimHash=current_hash,
                    )
                item.update(kind="claim", itemId=previous["itemId"], claimHash=digest)
                items[existing_index] = item
                updated += 1
        prepared.append(item)

    if (inserted or updated) and before_commit is not None:
        before_commit(prepared)

    revision = int(state["report"].get("revision", 0))
    if inserted or updated:
        revision += 1
        state["report"]["items"] = items
        state["report"]["revision"] = revision
        invalidate_finalized(state)
    receipt: dict[str, Any] = {
        "status": "accepted",
        "claimCount": len(prepared),
        "citationCount": sum(len(item["evidenceIds"]) for item in prepared),
        "items": [item["itemId"] for item in prepared],
        "claimHashes": {
            item["claimKey"]: claim_hash(item) for item in prepared if "claimKey" in item
        },
        "insertedCount": inserted,
        "updatedCount": updated,
        "unchangedCount": unchanged,
        "committedRevision": revision,
    }
    if batch_key is not None:
        receipt.update(batchKey=batch_key, requestHash=request_hash)
        state.setdefault("claimBatches", {})[batch_key] = {
            "requestHash": request_hash,
            "receipt": json.loads(canonical_json(receipt)),
        }
    return receipt
