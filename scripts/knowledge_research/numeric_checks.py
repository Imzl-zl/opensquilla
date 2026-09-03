"""Flag gross scale contradictions in explicit same-currency equivalences, not facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Any

if __package__:
    from .claims import claim_hash
else:  # pragma: no cover - standalone bridge
    from claims import claim_hash  # type: ignore[import-not-found,no-redef]

_NUMBER = r"[+-]?\d{1,24}(?:,\d{3}){0,8}(?:\.\d{1,12})?"
_SCALE = r"\u4e07\u4ebf|\u5341\u4ebf|\u767e\u4e07|\u4ebf|\u4e07|\u5343|\u767e"
_CURRENCY = r"\u97e9\u5143|\u4eba\u6c11\u5e01|\u7f8e\u5143|\u65e5\u5143|\u6b27\u5143|\u6e2f\u5143"
_EQUIVALENCE = re.compile(
    rf"(?<![A-Za-z\d.,+-])(?P<a>{_NUMBER})[ \t]*(?P<sa>{_SCALE})?[ \t]*(?P<currency>{_CURRENCY})"
    r"[ \t]*(?:\u7ea7\u522b)?[ \t]*[,\u3001\uff0c]?[ \t]*"
    r"(?:\u4ea6\u5373|\u5373|\u7b49\u4e8e)[ \t]*"
    rf"(?P<b>{_NUMBER})[ \t]*(?P<sb>{_SCALE})?[ \t]*(?P=currency)"
)
_POWERS = {
    None: 0,
    "\u767e": 2,
    "\u5343": 3,
    "\u4e07": 4,
    "\u767e\u4e07": 6,
    "\u4ebf": 8,
    "\u5341\u4ebf": 9,
    "\u4e07\u4ebf": 12,
}
_REPORTED_ERROR = re.compile(
    r"\u8bef\u5199|\u9519\u5199|\u9519\u8bef\u6362\u7b97|\u9519\u8bef\u5730|"
    r"\u9519\u8bef\u793a\u4f8b|\u9519\u8bef\u8868\u8ff0|\u5e76\u975e|\u5e76\u4e0d"
)


def numeric_scale_checks(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for item in state["report"]["items"]:
        if item["kind"] != "claim" or not item.get("claimKey"):
            continue
        for match in _EQUIVALENCE.finditer(item["text"]):
            prefix = item["text"][max(0, match.start() - 40) : match.start()]
            sentence_prefix = re.split(r"[.!?;\u3002\uff01\uff1f\uff1b\n]", prefix)[-1]
            tail = item["text"][match.end() :].lstrip(" \t")
            if tail and tail[0] not in ",.;!?)\u3002\uff0c\uff1b\uff01\uff1f\uff09\n":
                continue
            if match["a"].startswith(("+", "-")) and re.search(r"[\d,.][ \t]*$", prefix):
                continue
            if _REPORTED_ERROR.search(sentence_prefix) or prefix.rstrip().endswith(
                ("\u81f3", "\u5230", "-", "~", "\uff5e", "\u2013", "\u2014")
            ):
                continue
            with localcontext() as context:
                context.prec = 100
                a = Decimal(match["a"].replace(",", "")).scaleb(_POWERS[match["sa"]])
                b = Decimal(match["b"].replace(",", "")).scaleb(_POWERS[match["sb"]])
                low, high = sorted((abs(a), abs(b)))
                if not low or high < low * 10:
                    continue
            checks.append(
                {
                    "code": "NUMBER_SCALE_MISMATCH",
                    "tool": "researchAddClaims",
                    "claimKey": item["claimKey"],
                    "expectedClaimHash": claim_hash(item),
                    "textRange": {"start": match.start(), "end": match.end()},
                    "expression": match[0],
                    "normalizedAmounts": [str(a), str(b)],
                    "currency": match["currency"],
                    "message": (
                        "These explicitly equivalent amounts differ by at least 10x after "
                        "unit conversion. Check the bound source, correct the paragraph with "
                        "its current hash, then review again. The service does not decide "
                        "which amount is factually correct. This is not general fact checking."
                    ),
                }
            )
            break
        if len(checks) == 10:
            break
    return checks
