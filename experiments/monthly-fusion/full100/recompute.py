#!/usr/bin/env python3
"""Operational full-set report: recorded costs first, cache-aware estimates second."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

METRICS = (
    "AvgQ",
    "AvgPass",
    "JudgeErr",
    "Avg Gen$",
    "Total Gen$",
    "Gen exact",
    "Avg Input",
    "Avg Output",
    "Avg Reason",
    "Avg Cache",
    "Avg Visible",
    "Avg Tokens",
    "Avg Tools",
    "Tool%",
    "Avg Steps",
    "Avg LLMReq",
    "p50 ms",
    "p95 ms",
)
FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_write_tokens",
    "visible_tokens",
    "total_tokens",
)


def numeric(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def first(*values):
    return next((x for x in values if numeric(x)), None)


def price_at(pricing, started_at, input_tokens):
    """Catalogue overrides use UTC HHMM and/or minimum context thresholds."""
    p = dict(pricing)
    dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone(UTC)
    hhmm = dt.hour * 100 + dt.minute
    for override in pricing.get("overrides", []):
        if "utc_days" in override and dt.strftime("%A").lower() not in override["utc_days"]:
            continue
        if "utc_start" in override:
            start, end = override["utc_start"], override["utc_end"]
            fits = start <= hhmm < end if end > start else (hhmm >= start or hhmm < end)
            if not fits:
                continue
        if input_tokens < override.get("min_prompt_tokens", 0):
            continue
        p.update(override)
    inp, out = float(p["prompt"]), float(p["completion"])
    return {
        "input": inp,
        "output": out,
        "cache_read": float(p.get("input_cache_read", inp)),
        "cache_write": float(p.get("input_cache_write", inp)),
        "request": float(p.get("request", 0)),
    }


def token_cost(tokens, rates):
    i, o, c, w = (
        tokens[k] for k in ("input_tokens", "output_tokens", "cached_tokens", "cache_write_tokens")
    )
    assert 0 <= c + w <= i
    # Cache writes replace normal input billing for these tokens, not add it twice.
    return (
        (i - c - w) * rates["input"]
        + c * rates["cache_read"]
        + w * rates["cache_write"]
        + o * rates["output"]
        + rates.get("request", 0)
    )


def recorded_generation(call):
    receipts = [r["data"] for r in call.get("receipts", []) if numeric(r["data"].get("total_cost"))]
    u = call.get("usage") or {}
    cost = (
        sum(d["total_cost"] for d in receipts)
        if receipts
        else first(u.get("billed_cost"), u.get("cost_usd"))
    )
    sources = {}
    tokens = {}
    for field, native in [
        ("input_tokens", "native_tokens_prompt"),
        ("output_tokens", "native_tokens_completion"),
        ("reasoning_tokens", "native_tokens_reasoning"),
        ("cached_tokens", "native_tokens_cached"),
    ]:
        if receipts and all(numeric(d.get(native)) for d in receipts):
            tokens[field] = sum(d[native] for d in receipts)
            sources[field] = "generation_receipt"
        elif numeric(u.get(field)):
            tokens[field] = u[field]
            sources[field] = "response_usage"
    if numeric(u.get("cache_write_tokens")):
        tokens["cache_write_tokens"] = u["cache_write_tokens"]
        sources["cache_write_tokens"] = "response_usage"
    return (
        cost,
        ("generation_receipt" if receipts else "response_usage") if cost is not None else None,
        tokens,
        sources,
    )


def account_generation(snapshot):
    """Account for stable generation requests independently of pending judge work."""
    prices = {m["id"]: m["pricing"] for m in snapshot["generation_catalog"]["models"]}
    judge_model = snapshot["judge_model"]["id"]
    prices[judge_model] = snapshot["judge_model"]["pricing"]
    calls = []
    for task in snapshot["tasks"]:
        for call in task["calls"]:
            cost, source, tokens, token_sources = recorded_generation(call)
            calls.append(
                {
                    **call,
                    "group": task["group"],
                    "task_id": task["task_id"],
                    "tokens": tokens,
                    "token_sources": token_sources,
                    "cost_usd": cost,
                    "cost_source": source,
                    "estimated_fields": [],
                    "phase": "with_tools" if call["tools_count"] else "without_tools",
                }
            )
    assert len({(c["group"], c["task_id"], c["call_id"]) for c in calls}) == len(calls)
    # Normalize the observed donor pool before any imputation, independently of row order.
    for c in calls:
        t, sources = c["tokens"], c["token_sources"]
        if (
            all(k in t for k in ("reasoning_tokens", "output_tokens"))
            and t["reasoning_tokens"] > t["output_tokens"]
        ):
            c["reported_output_tokens_before_normalization"] = t["output_tokens"]
            c["reported_reasoning_tokens_before_normalization"] = t["reasoning_tokens"]
            assert numeric(c.get("visible_text_token_estimate"))
            t["visible_tokens"] = c["visible_text_token_estimate"]
            t["output_tokens"] = t["reasoning_tokens"] + t["visible_tokens"]
            sources["visible_tokens"] = "cl100k_base_visible_text_estimate"
            sources["output_tokens"] = "reasoning_plus_estimated_visible"
            c["estimated_fields"].extend(["visible_tokens", "output_tokens"])
    observed = [c for c in calls if all(k in c["tokens"] for k in FIELDS[:4])]
    by_model_phase = defaultdict(list)
    by_task_model_phase = defaultdict(list)
    by_payload = defaultdict(list)
    for c in observed:
        by_model_phase[(c["model"], c["phase"])].append(c)
        by_task_model_phase[(c["group"], c["task_id"], c["model"], c["phase"])].append(c)
        by_payload[(c["model"], c["payload_sha256"])].append(c)
    for c in calls:
        t, sources = c["tokens"], c["token_sources"]
        rejected = c["cost_usd"] is None and c.get("error", {}).get("status_code") in (
            400,
            403,
            429,
        )
        if rejected:
            # Explicit estimation convention, not a recovered invoice.
            for field in FIELDS:
                t[field] = 0
                sources[field] = "rejected_request_assumed_zero"
            c["estimated_fields"] = list(FIELDS)
            c["cost_usd"] = 0.0
            c["cost_source"] = "rejected_request_assumed_zero"
            continue
        missing = [field for field in FIELDS[:4] if field not in t]
        if missing:
            exact_payload = by_payload.get((c["model"], c["payload_sha256"]), [])
            task_pool = by_task_model_phase.get(
                (c["group"], c["task_id"], c["model"], c["phase"]), []
            )
            pool = exact_payload or task_pool or by_model_phase[(c["model"], c["phase"])]
            assert pool, (c["model"], c["call_id"])
            pool_kind = (
                "same_payload"
                if exact_payload
                else "same_task_model_phase"
                if task_pool
                else "same_model_phase"
            )
            c["imputation"] = {"pool": pool_kind, "observed_calls": len(pool)}
            if "input_tokens" in t:
                i = t["input_tokens"]
            elif exact_payload:
                i = round(median(p["tokens"]["input_tokens"] for p in pool))
            else:
                ratios = [
                    p["tokens"]["input_tokens"] / p["input_token_estimate"]
                    for p in pool
                    if p.get("input_token_estimate")
                ]
                i = round(c["input_token_estimate"] * median(ratios))
                c["imputation"]["input_native_to_estimator_ratio"] = median(ratios)
            # Subset estimates must respect already observed parent totals.
            o = t.get("output_tokens", round(median(p["tokens"]["output_tokens"] for p in pool)))
            r = min(o, round(median(p["tokens"]["reasoning_tokens"] for p in pool)))
            ratio = median(
                p["tokens"]["cached_tokens"] / p["tokens"]["input_tokens"]
                for p in pool
                if p["tokens"]["input_tokens"]
            )
            cached = min(max(0, i - t.get("cache_write_tokens", 0)), round(i * ratio))
            c["imputation"]["cache_hit_ratio"] = ratio
            estimates = {
                "input_tokens": i,
                "output_tokens": o,
                "reasoning_tokens": r,
                "cached_tokens": cached,
            }
            for field in missing:
                t[field] = estimates[field]
                sources[field] = "usage_estimate_" + pool_kind
                c["estimated_fields"].append(field)
        if "cache_write_tokens" not in t:
            t["cache_write_tokens"] = 0
            sources["cache_write_tokens"] = "no_cache_write_record_assumed_zero"
            c["estimated_fields"].append("cache_write_tokens")
        if t["reasoning_tokens"] > t["output_tokens"]:
            # Preserve reported fields in snapshot. Reconstruct a coherent output estimate.
            c["reported_output_tokens_before_normalization"] = t["output_tokens"]
            c["reported_reasoning_tokens_before_normalization"] = t["reasoning_tokens"]
            assert numeric(c.get("visible_text_token_estimate"))
            t["visible_tokens"] = c["visible_text_token_estimate"]
            t["output_tokens"] = t["reasoning_tokens"] + t["visible_tokens"]
            sources["visible_tokens"] = "cl100k_base_visible_text_estimate"
            sources["output_tokens"] = "reasoning_plus_estimated_visible"
            c["estimated_fields"].extend(["visible_tokens", "output_tokens"])
        else:
            t["visible_tokens"] = t["output_tokens"] - t["reasoning_tokens"]
            sources.setdefault("visible_tokens", "output_minus_reasoning")
        t["total_tokens"] = t["input_tokens"] + t["output_tokens"]
        assert t["cached_tokens"] + t["cache_write_tokens"] <= t["input_tokens"]
        if c["cost_usd"] is None:
            rates = price_at(prices[c["model"]], c["started_at"], t["input_tokens"])
            c["rates_usd_per_token"] = rates
            c["cost_usd"] = token_cost(t, rates)
            c["cost_source"] = "cache_aware_token_estimate"
    return calls, prices


def compute(
    snapshot,
    comparison,
    groups,
    expected_tasks,
    baseline_group,
    baseline_report=None,
    snapshot_sha256=None,
):
    group_names = tuple(groups)
    if not group_names or len(group_names) != len(set(group_names)):
        raise ValueError("groups must be unique and nonempty")
    if expected_tasks < 1:
        raise ValueError("expected_tasks must be positive")
    reference = {(t["group"], t["task_id"]): t for t in comparison["tasks"]}
    assert len(reference) == len(snapshot["tasks"]) == expected_tasks * len(group_names)
    assert set(reference) == {(t["group"], t["task_id"]) for t in snapshot["tasks"]}
    assert all(
        len({t["task_id"] for t in snapshot["tasks"] if t["group"] == g}) == expected_tasks
        for g in group_names
    )
    assert len({t["task_id"] for t in snapshot["tasks"]}) == expected_tasks
    calls, prices = account_generation(snapshot)
    judge_model = snapshot["judge_model"]["id"]
    assert len(calls) == sum(t["llm_requests"] for t in reference.values())
    judge_calls = []
    for task in snapshot["tasks"]:
        for a in task["judge_attempts"]:
            u = a.get("usage") or {}
            cost = first(a.get("cost_usd"), u.get("cost"))
            judge_calls.append(
                {
                    **a,
                    "group": task["group"],
                    "task_id": task["task_id"],
                    "cost_usd": cost,
                    "cost_source": "response_usage" if cost is not None else None,
                }
            )
    judge_by_wire = defaultdict(list)
    judge_ratios = []
    judge_output = []
    for c in judge_calls:
        u = c.get("usage") or {}
        if numeric(u.get("prompt_tokens")):
            judge_by_wire[(c["group"], c["task_id"], c.get("payload_sha256"))].append(c)
            if c.get("request_bytes"):
                judge_ratios.append(u["prompt_tokens"] / c["request_bytes"])
            if numeric(u.get("completion_tokens")):
                judge_output.append(u["completion_tokens"])
    for c in judge_calls:
        if c["cost_usd"] is not None:
            continue
        if c.get("http_status") in (400, 401, 403, 404, 422, 429):
            c["cost_usd"], c["cost_source"] = 0.0, "rejected_request_assumed_zero"
            continue
        u = c.get("usage") or {}
        pool = judge_by_wire.get((c["group"], c["task_id"], c.get("payload_sha256")), [])
        inp = first(u.get("prompt_tokens"))
        if inp is None:
            inp = (
                round(median(p["usage"]["prompt_tokens"] for p in pool))
                if pool
                else round(c["request_bytes"] * median(judge_ratios))
            )
        out = first(u.get("completion_tokens"), c.get("output_text_token_estimate"))
        if out is None:
            out = round(median(judge_output))
        details = u.get("prompt_tokens_details") or {}
        cached = first(details.get("cached_tokens"))
        if cached is None:
            cached = (
                round(
                    median(
                        (p["usage"].get("prompt_tokens_details") or {}).get("cached_tokens", 0)
                        for p in pool
                    )
                )
                if pool
                else 0
            )
        tokens = {
            "input_tokens": inp,
            "output_tokens": out,
            "cached_tokens": min(inp, cached),
            "cache_write_tokens": first(details.get("cache_write_tokens"), 0),
        }
        rates = price_at(
            prices[judge_model], datetime.fromtimestamp(c["started_at"], UTC).isoformat(), inp
        )
        c.update(
            estimated_tokens=tokens,
            cost_usd=token_cost(tokens, rates),
            cost_source="cache_aware_token_estimate",
        )
    generation_by_task = defaultdict(list)
    judges_by_task = defaultdict(list)
    for c in calls:
        generation_by_task[(c["group"], c["task_id"])].append(c)
    for c in judge_calls:
        judges_by_task[(c["group"], c["task_id"])].append(c)
    tasks = []
    for raw in snapshot["tasks"]:
        key = raw["group"], raw["task_id"]
        ref = reference[key]
        cs, js = generation_by_task[key], judges_by_task[key]
        assert len(cs) == ref["llm_requests"] and len(js) == ref["judge_request_count"]
        scored = numeric(ref.get("quality"))
        q, p = (ref["quality"], ref["criterion_pass_rate"]) if scored else (0, 0)
        tasks.append(
            {
                "group": key[0],
                "task_id": key[1],
                "quality": q,
                "criterion_pass_rate": p,
                "quality_source": "native_scored" if scored else "full100_zero_for_unscored",
                "generation_status": raw["generation_status"],
                "judge_errors": ref["judge_errors"],
                "generation_cost_usd": math.fsum(c["cost_usd"] for c in cs),
                "generation_recorded_cost_usd": math.fsum(
                    c["cost_usd"]
                    for c in cs
                    if c["cost_source"] in ("generation_receipt", "response_usage")
                ),
                "generation_estimated_cost_usd": math.fsum(
                    c["cost_usd"] for c in cs if c["cost_source"] == "cache_aware_token_estimate"
                ),
                "generation_cost_sources": dict(Counter(c["cost_source"] for c in cs)),
                "original_exact_cost": raw["original_exact_cost"],
                "token_totals": {field: sum(c["tokens"][field] for c in cs) for field in FIELDS},
                "judge_cost_usd": math.fsum(c["cost_usd"] for c in js),
                "judge_recorded_cost_usd": math.fsum(
                    c["cost_usd"] for c in js if c["cost_source"] == "response_usage"
                ),
                "judge_estimated_cost_usd": math.fsum(
                    c["cost_usd"] for c in js if c["cost_source"] == "cache_aware_token_estimate"
                ),
                "judge_cost_sources": dict(Counter(c["cost_source"] for c in js)),
                "tool_calls": ref["tool_calls"],
                "llm_requests": len(cs),
                "judge_requests": len(js),
                "generation_elapsed_ms": ref["generation_elapsed_ms"],
            }
        )
    group_results = []
    for group in group_names:
        rows = [t for t in tasks if t["group"] == group]
        cs = [c for c in calls if c["group"] == group]
        js = [c for c in judge_calls if c["group"] == group]
        assert len(rows) == expected_tasks

        def avg(key):
            return math.fsum(task[key] for task in rows) / expected_tasks

        costs = math.fsum(t["generation_cost_usd"] for t in rows)
        times = sorted(t["generation_elapsed_ms"] for t in rows)
        m = {
            "AvgQ": avg("quality") * 100,
            "AvgPass": avg("criterion_pass_rate") * 100,
            "JudgeErr": sum(t["judge_errors"] for t in rows),
            "Avg Gen$": costs / expected_tasks,
            "Total Gen$": costs,
            "Gen exact": f"{sum(t['original_exact_cost'] for t in rows)}/{expected_tasks}",
        }
        for label, field in [
            ("Input", "input_tokens"),
            ("Output", "output_tokens"),
            ("Reason", "reasoning_tokens"),
            ("Cache", "cached_tokens"),
            ("Visible", "visible_tokens"),
            ("Tokens", "total_tokens"),
        ]:
            m["Avg " + label] = sum(t["token_totals"][field] for t in rows) / expected_tasks
        m.update(
            {
                "Avg Tools": avg("tool_calls"),
                "Tool%": 100 * sum(t["tool_calls"] > 0 for t in rows) / expected_tasks,
                "Avg Steps": avg("tool_calls") + avg("llm_requests"),
                "Avg LLMReq": avg("llm_requests"),
                "p50 ms": times[max(0, math.ceil(expected_tasks * 0.50) - 1)],
                "p95 ms": times[max(0, math.ceil(expected_tasks * 0.95) - 1)],
            }
        )
        assert set(m) == set(METRICS) and all(v is not None for v in m.values())
        group_results.append(
            {
                "group": group,
                "denominator": expected_tasks,
                "metrics": {k: m[k] for k in METRICS},
                "scored_tasks": sum(t["quality_source"] == "native_scored" for t in rows),
                "zero_score_tasks": [
                    t["task_id"] for t in rows if t["quality_source"] != "native_scored"
                ],
                "generation_failures": sum(
                    t["generation_status"] == "generation_failed" for t in rows
                ),
                "generation_recorded_cost_usd": math.fsum(
                    t["generation_recorded_cost_usd"] for t in rows
                ),
                "generation_estimated_cost_usd": math.fsum(
                    t["generation_estimated_cost_usd"] for t in rows
                ),
                "generation_cost_sources": dict(Counter(c["cost_source"] for c in cs)),
                "generation_usage_estimated_calls": sum(bool(c.get("imputation")) for c in cs),
                "generation_output_normalized_calls": sum(
                    "reported_output_tokens_before_normalization" in c for c in cs
                ),
                "average_cache_write_tokens": sum(
                    t["token_totals"]["cache_write_tokens"] for t in rows
                )
                / expected_tasks,
                "total_judge_cost_usd": math.fsum(t["judge_cost_usd"] for t in rows),
                "average_judge_cost_usd": avg("judge_cost_usd"),
                "judge_recorded_cost_usd": math.fsum(t["judge_recorded_cost_usd"] for t in rows),
                "judge_estimated_cost_usd": math.fsum(t["judge_estimated_cost_usd"] for t in rows),
                "judge_cost_sources": dict(Counter(c["cost_source"] for c in js)),
            }
        )
    reference_info = None
    if baseline_report is not None:
        reference_path = Path(baseline_report)
        reference_bytes = reference_path.read_bytes()
        historical = json.loads(reference_bytes)
        baseline = next(g for g in historical["groups"] if g["group"] == baseline_group)
        reference_info = {
            "source": str(reference_path),
            "sha256": hashlib.sha256(reference_bytes).hexdigest(),
            "historical_run": True,
            "group_data": baseline,
        }
    else:
        baseline = next(g for g in group_results if g["group"] == baseline_group)

    def change(current, reference_value):
        return (current / reference_value - 1) * 100 if reference_value else None

    for group_result in group_results:
        group_result["comparison_vs_baseline_full100"] = {
            "baseline_group": baseline_group,
            "quality_difference_pp": group_result["metrics"]["AvgQ"] - baseline["metrics"]["AvgQ"],
            "quality_change_percent": change(
                group_result["metrics"]["AvgQ"], baseline["metrics"]["AvgQ"]
            ),
            "generation_cost_change_percent": change(
                group_result["metrics"]["Total Gen$"], baseline["metrics"]["Total Gen$"]
            ),
            "p50_change_percent": change(
                group_result["metrics"]["p50 ms"], baseline["metrics"]["p50 ms"]
            ),
            "p95_change_percent": change(
                group_result["metrics"]["p95 ms"], baseline["metrics"]["p95 ms"]
            ),
        }
    report = {
        "schema": "draco-full-set-pragmatic-accounting/v1",
        "groups": group_results,
        "tasks": tasks,
        "generation_calls": calls,
        "judge_calls": judge_calls,
        "prices": prices,
        "cost_policy": (
            "recorded cost first; cache-aware estimated tokens when unavailable; "
            "rejected requests assumed zero"
        ),
        "all_generation_calls": len(calls),
        "all_judge_requests": len(judge_calls),
        "generation_catalog_fetched_at": snapshot["generation_catalog"]["fetched_at"],
        "snapshot_sha256": snapshot_sha256,
        "new_model_calls_during_accounting": 0,
        "official_full100_score_claimed": False,
        "statistical_superiority_claimed": False,
    }
    report["quality_policy"] = (
        f"native scored value; otherwise operational zero; denominator always {expected_tasks}"
    )
    if reference_info is not None:
        report["reference_baseline"] = reference_info
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--comparison", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--groups", required=True, help="Comma-separated campaign groups")
    parser.add_argument("--expected-tasks", type=int, default=100)
    parser.add_argument("--baseline-group", required=True)
    parser.add_argument(
        "--baseline-report",
        type=Path,
        help="Optional historical full-set report containing the baseline group",
    )
    args = parser.parse_args()
    groups = tuple(group for group in args.groups.split(",") if group)
    snapshot_bytes = args.snapshot.read_bytes()
    snapshot = json.loads(gzip.decompress(snapshot_bytes))
    comparison = json.loads(args.comparison.read_bytes())
    report = compute(
        snapshot,
        comparison,
        groups,
        args.expected_tasks,
        args.baseline_group,
        args.baseline_report,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
    )
    calls = report.pop("generation_calls")
    judge_calls = report.pop("judge_calls")
    keep = (
        "group",
        "task_id",
        "call_id",
        "call_index",
        "model",
        "phase",
        "started_at",
        "error",
        "cost_usd",
        "cost_source",
        "tokens",
        "token_sources",
        "estimated_fields",
        "imputation",
        "input_token_estimate",
        "rates_usd_per_token",
        "reported_output_tokens_before_normalization",
        "reported_reasoning_tokens_before_normalization",
        "visible_text_token_estimate",
    )
    call_detail = [{k: c[k] for k in keep if k in c} for c in calls]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "generation-accounting.json").write_text(
        json.dumps(call_detail, ensure_ascii=False, indent=2) + "\n"
    )
    keep_judge = (
        "group",
        "task_id",
        "repeat_index",
        "criterion_index",
        "attempt",
        "request_id",
        "started_at",
        "http_status",
        "error_type",
        "cost_usd",
        "cost_source",
        "estimated_tokens",
    )
    judge_detail = [{k: c[k] for k in keep_judge if k in c} for c in judge_calls]
    (args.output_dir / "judge-accounting.json.gz").write_bytes(
        gzip.compress(
            json.dumps(judge_detail, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0
        )
    )
    report["detail_files"] = {
        "generation": "generation-accounting.json",
        "judge": "judge-accounting.json.gz",
    }
    (args.output_dir / "full100-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print(
        json.dumps(
            {
                "groups": report["groups"],
                "generation_calls": report["all_generation_calls"],
                "judge_requests": report["all_judge_requests"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
