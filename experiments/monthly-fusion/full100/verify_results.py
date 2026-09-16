#!/usr/bin/env python3
"""Validate every published full-set metric without network or model calls."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path

METRICS = {
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
}

TOKEN_METRICS = {
    "Avg Input": "input_tokens",
    "Avg Output": "output_tokens",
    "Avg Reason": "reasoning_tokens",
    "Avg Cache": "cached_tokens",
    "Avg Visible": "visible_tokens",
    "Avg Tokens": "total_tokens",
}

DELTA_FIELDS = {
    "quality_difference_pp",
    "quality_change_percent",
    "generation_cost_change_percent",
    "p50_change_percent",
    "p95_change_percent",
}


def close(left, right, tolerance=1e-9):
    return (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and math.isfinite(left)
        and math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)
    )


def require_close(name, field, observed, expected):
    if not close(observed, expected):
        raise ValueError(f"{name}: {field} mismatch: {observed!r} != {expected!r}")


def nearest(values, percentile):
    return sorted(values)[max(0, math.ceil(len(values) * percentile) - 1)]


def percent_change(current, baseline):
    return (current / baseline - 1) * 100 if baseline else None


def expected_metrics(rows, denominator):
    total_cost = math.fsum(task["generation_cost_usd"] for task in rows)
    tools = math.fsum(task["tool_calls"] for task in rows)
    requests = math.fsum(task["llm_requests"] for task in rows)
    times = [task["generation_elapsed_ms"] for task in rows]
    expected = {
        "AvgQ": 100 * math.fsum(task["quality"] for task in rows) / denominator,
        "AvgPass": 100 * math.fsum(task["criterion_pass_rate"] for task in rows) / denominator,
        "JudgeErr": sum(task["judge_errors"] for task in rows),
        "Avg Gen$": total_cost / denominator,
        "Total Gen$": total_cost,
        "Gen exact": f"{sum(task['original_exact_cost'] is True for task in rows)}/{denominator}",
        "Avg Tools": tools / denominator,
        "Tool%": 100 * sum(task["tool_calls"] > 0 for task in rows) / denominator,
        "Avg Steps": (tools + requests) / denominator,
        "Avg LLMReq": requests / denominator,
        "p50 ms": nearest(times, 0.50),
        "p95 ms": nearest(times, 0.95),
    }
    for metric, field in TOKEN_METRICS.items():
        expected[metric] = math.fsum(task["token_totals"][field] for task in rows) / denominator
    return expected


def verify_group(group, rows):
    name = group["group"]
    denominator = group["denominator"]
    if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator < 1:
        raise ValueError(f"{name}: invalid denominator")
    if len(rows) != denominator:
        raise ValueError(f"{name}: expected {denominator} task rows, got {len(rows)}")
    metrics = group.get("metrics") or {}
    if set(metrics) != METRICS or any(value is None for value in metrics.values()):
        raise ValueError(f"{name}: metric set is incomplete")
    expected = expected_metrics(rows, denominator)
    for field in METRICS:
        if field == "Gen exact":
            if metrics[field] != expected[field]:
                raise ValueError(f"{name}: {field} mismatch")
        else:
            require_close(name, field, metrics[field], expected[field])

    scored = [task for task in rows if task["quality_source"] == "native_scored"]
    zeros = [task["task_id"] for task in rows if task["quality_source"] != "native_scored"]
    if group.get("scored_tasks") != len(scored) or group.get("zero_score_tasks") != zeros:
        raise ValueError(f"{name}: score coverage mismatch")
    if any(
        task["quality"] != 0 or task["criterion_pass_rate"] != 0
        for task in rows
        if task["quality_source"] != "native_scored"
    ):
        raise ValueError(f"{name}: unscored task does not carry operational zero")

    generation_failures = sum(task["generation_status"] == "generation_failed" for task in rows)
    if group.get("generation_failures") != generation_failures:
        raise ValueError(f"{name}: generation failure count mismatch")

    recorded = math.fsum(task["generation_recorded_cost_usd"] for task in rows)
    estimated = math.fsum(task["generation_estimated_cost_usd"] for task in rows)
    require_close(
        name,
        "generation_recorded_cost_usd",
        group.get("generation_recorded_cost_usd"),
        recorded,
    )
    require_close(
        name,
        "generation_estimated_cost_usd",
        group.get("generation_estimated_cost_usd"),
        estimated,
    )
    require_close(
        name, "generation cost decomposition", recorded + estimated, expected["Total Gen$"]
    )

    judge_total = math.fsum(task["judge_cost_usd"] for task in rows)
    judge_recorded = math.fsum(task["judge_recorded_cost_usd"] for task in rows)
    judge_estimated = math.fsum(task["judge_estimated_cost_usd"] for task in rows)
    require_close(name, "total_judge_cost_usd", group.get("total_judge_cost_usd"), judge_total)
    require_close(
        name,
        "average_judge_cost_usd",
        group.get("average_judge_cost_usd"),
        judge_total / denominator,
    )
    require_close(
        name,
        "judge_recorded_cost_usd",
        group.get("judge_recorded_cost_usd"),
        judge_recorded,
    )
    require_close(
        name,
        "judge_estimated_cost_usd",
        group.get("judge_estimated_cost_usd"),
        judge_estimated,
    )
    require_close(name, "judge cost decomposition", judge_recorded + judge_estimated, judge_total)

    cache_writes = (
        math.fsum(task["token_totals"]["cache_write_tokens"] for task in rows) / denominator
    )
    require_close(
        name,
        "average_cache_write_tokens",
        group.get("average_cache_write_tokens"),
        cache_writes,
    )


def reference_baseline(report):
    reference = report.get("reference_baseline")
    if not reference:
        return None
    source = Path(reference["source"])
    raw = source.read_bytes() if source.is_file() else b""
    if not raw or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("historical baseline source hash mismatch")
    source_report = json.loads(raw)
    group_data = reference.get("group_data")
    if group_data not in source_report.get("groups", []):
        raise ValueError("historical baseline group_data differs from hashed source")
    return group_data


def verify_deltas(report, groups):
    external = reference_baseline(report)
    for group in groups:
        delta = group.get("comparison_vs_baseline_full100") or {}
        baseline_name = delta.get("baseline_group")
        if not baseline_name:
            raise ValueError(f"{group['group']}: missing baseline_group")
        if external is not None:
            if external.get("group") != baseline_name:
                raise ValueError(f"{group['group']}: historical baseline group mismatch")
            baseline = external
        else:
            matches = [candidate for candidate in groups if candidate["group"] == baseline_name]
            if len(matches) != 1:
                raise ValueError(f"{group['group']}: baseline group not found")
            baseline = matches[0]
        current_metrics = group["metrics"]
        baseline_metrics = baseline["metrics"]
        expected = {
            "quality_difference_pp": current_metrics["AvgQ"] - baseline_metrics["AvgQ"],
            "quality_change_percent": percent_change(
                current_metrics["AvgQ"], baseline_metrics["AvgQ"]
            ),
            "generation_cost_change_percent": percent_change(
                current_metrics["Total Gen$"], baseline_metrics["Total Gen$"]
            ),
            "p50_change_percent": percent_change(
                current_metrics["p50 ms"], baseline_metrics["p50 ms"]
            ),
            "p95_change_percent": percent_change(
                current_metrics["p95 ms"], baseline_metrics["p95 ms"]
            ),
        }
        for field in DELTA_FIELDS:
            observed = delta.get(field)
            wanted = expected[field]
            if wanted is None:
                if observed is not None:
                    raise ValueError(f"{group['group']}: {field} must be null")
            else:
                require_close(group["group"], field, observed, wanted)


def detail_count(report, detail_root, kind):
    inline = report.get("generation_calls" if kind == "generation" else "judge_calls")
    if inline is not None:
        return len(inline)
    if detail_root is None:
        return None
    relative = (report.get("detail_files") or {}).get(kind)
    if not isinstance(relative, str):
        raise ValueError(f"missing {kind} detail file")
    root = Path(detail_root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError(f"{kind} detail path escapes report directory")
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError(f"{kind} detail must be a JSON array")
    return len(rows)


def verify(report, detail_root=None):
    groups = report.get("groups") or []
    tasks = report.get("tasks") or []
    if not groups:
        raise ValueError("report contains no groups")
    names = [group["group"] for group in groups]
    if len(names) != len(set(names)):
        raise ValueError("duplicate report group")
    task_keys = [(task["group"], task["task_id"]) for task in tasks]
    if len(task_keys) != len(set(task_keys)):
        raise ValueError("duplicate task row")
    if any(task["group"] not in names for task in tasks):
        raise ValueError("task row belongs to an unknown group")
    for group in groups:
        verify_group(group, [task for task in tasks if task["group"] == group["group"]])
    verify_deltas(report, groups)
    generation_count = detail_count(report, detail_root, "generation")
    judge_count = detail_count(report, detail_root, "judge")
    if generation_count is not None and report.get("all_generation_calls") != generation_count:
        raise ValueError("all_generation_calls mismatch")
    if judge_count is not None and report.get("all_judge_requests") != judge_count:
        raise ValueError("all_judge_requests mismatch")
    return {
        "verified": True,
        "groups": names,
        "tasks": len(tasks),
        "metrics_per_group": len(METRICS),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            verify(json.loads(args.report.read_text()), detail_root=args.report.parent),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
