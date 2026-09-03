"""Lossless locator normalization and bounded transport fragments, without state I/O."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

LOCATOR_INLINE_BYTES = 4096
METADATA_FRAGMENT_CHARS = 2000


def source_locator(locator: Mapping[str, Any], title: str = "") -> dict[str, Any]:
    projected = {
        key: copy.deepcopy(locator[key])
        for key in ("title", "sectionPath", "pageStart", "pageEnd", "page", "pages")
        if key in locator
    }
    page = projected.get("page")
    if isinstance(page, Mapping):
        normalized = {
            target: page[key]
            for key, target in (("start", "pageStart"), ("end", "pageEnd"))
            if type(page.get(key)) is int
        }
        for key, value in normalized.items():
            projected.setdefault(key, value)
        if (
            normalized
            and set(page) <= {"start", "end"}
            and len(normalized) == len(page)
            and all(projected[key] == value for key, value in normalized.items())
        ):
            projected.pop("page")
    if projected.get("title") == title:
        projected.pop("title")
    return projected


def locator_transport(value: Any) -> tuple[Any, str | None]:
    """Return a small preview plus complete JSON when a continuation is required."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(json.dumps(encoded, ensure_ascii=False).encode("utf-8")) <= LOCATOR_INLINE_BYTES:
        return value, None
    if isinstance(value, list):
        preview: Any = [_preview(row) for row in value[:3] if isinstance(row, Mapping)]
    else:
        preview = _preview(value)
    return preview, encoded


def _preview(locator: Mapping[str, Any]) -> dict[str, Any]:
    preview = {
        key: locator[key]
        for key in ("pageStart", "pageEnd", "page")
        if type(locator.get(key)) is int
    }
    if isinstance(locator.get("title"), str):
        preview["title"] = locator["title"][:128]
    if isinstance(locator.get("sectionPath"), list):
        preview["sectionPath"] = [
            part[:128] for part in locator["sectionPath"][:3] if isinstance(part, str)
        ]
    return preview


def locator_fragments(encoded: str) -> list[dict[str, Any]]:
    return [
        {
            "kind": "locator",
            "format": "json",
            "content": encoded[start : start + METADATA_FRAGMENT_CHARS],
            "contentRange": {
                "start": start,
                "end": min(start + METADATA_FRAGMENT_CHARS, len(encoded)),
                "total": len(encoded),
            },
        }
        for start in range(0, len(encoded), METADATA_FRAGMENT_CHARS)
    ]
