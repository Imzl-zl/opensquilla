#!/usr/bin/env python3
"""Render a reviewable Markdown report from a verified full-set JSON report."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


def fmt(value, digits=3):
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def pct(value):
    if value is None:
        return "N/A"
    return "0.00%" if abs(value) < 0.0000005 else f"{value:+.2f}%"


def table(headers, rows):
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| "
            + " | ".join("---" if index == 0 else "---:" for index in range(len(headers)))
            + " |",
            *("| " + " | ".join(row) + " |" for row in rows),
        ]
    )


def render(report, title, profiles=None):
    groups = report["groups"]
    if not groups:
        raise ValueError("report contains no groups")
    baseline_name = groups[0]["comparison_vs_baseline_full100"]["baseline_group"]
    display_groups = list(groups)
    reference = report.get("reference_baseline")
    if reference and all(group["group"] != baseline_name for group in groups):
        historical = copy.deepcopy(reference["group_data"])
        historical["comparison_vs_baseline_full100"] = {
            "baseline_group": baseline_name,
            "quality_difference_pp": 0.0,
            "quality_change_percent": 0.0,
            "generation_cost_change_percent": 0.0,
            "p50_change_percent": 0.0,
            "p95_change_percent": 0.0,
        }
        display_groups.insert(0, historical)
    lines = [
        f"# {title}",
        "",
        (
            f"所有主指标使用每组完整 {groups[0]['denominator']} 题。"
            "未取得完整原生评分的任务以运营统计 0 分计入 AvgQ 和 AvgPass。"
        ),
        (
            "费用优先采用已记录金额；缺失金额使用包含 KV cache 的 token 估算。"
            "相对变化统一使用未舍入值计算。"
        ),
        "",
    ]
    if report.get("reference_baseline"):
        lines += [
            (
                f"基线 `{baseline_name}` 来自历史报告，当前候选与基线属于跨轮描述性比较；"
                "没有重新执行历史基线。"
            ),
            "",
        ]
    if profiles:
        spec = profiles.get("lineup_spec") or {}
        rows = []
        for group, lineup in (spec.get("groups") or {}).items():
            rows.append(
                [
                    group,
                    "<br>".join(lineup["proposers"]),
                    lineup["aggregator"],
                    spec["fallback_model"],
                ]
            )
        if rows:
            lines += ["## 配置", "", table(["组别", "起草模型", "汇总模型", "回退模型"], rows), ""]
    quality_rows = []
    token_rows = []
    runtime_rows = []
    coverage_rows = []
    cost_rows = []
    for group in display_groups:
        metrics = group["metrics"]
        delta = group["comparison_vs_baseline_full100"]
        quality_rows.append(
            [
                group["group"],
                fmt(metrics["AvgQ"]),
                pct(delta["quality_change_percent"]),
                fmt(metrics["AvgPass"]),
                fmt(metrics["JudgeErr"]),
                fmt(metrics["Avg Gen$"], 6),
                pct(delta["generation_cost_change_percent"]),
                fmt(metrics["Total Gen$"], 6),
                metrics["Gen exact"],
            ]
        )
        token_rows.append(
            [
                group["group"],
                fmt(metrics["Avg Input"], 2),
                fmt(metrics["Avg Output"], 2),
                fmt(metrics["Avg Reason"], 2),
                fmt(metrics["Avg Cache"], 2),
                fmt(metrics["Avg Visible"], 2),
                fmt(metrics["Avg Tokens"], 2),
            ]
        )
        runtime_rows.append(
            [
                group["group"],
                fmt(metrics["Avg Tools"], 2),
                fmt(metrics["Tool%"], 2),
                fmt(metrics["Avg Steps"], 2),
                fmt(metrics["Avg LLMReq"], 2),
                fmt(metrics["p50 ms"], 0),
                pct(delta["p50_change_percent"]),
                fmt(metrics["p95 ms"], 0),
                pct(delta["p95_change_percent"]),
            ]
        )
        coverage_rows.append(
            [
                group["group"],
                f"{group['scored_tasks']}/{group['denominator']}",
                str(len(group["zero_score_tasks"])),
                str(group["generation_failures"]),
            ]
        )
        cost_rows.append(
            [
                group["group"],
                fmt(group["generation_recorded_cost_usd"], 6),
                fmt(group["generation_estimated_cost_usd"], 6),
                fmt(group["total_judge_cost_usd"], 6),
            ]
        )
    lines += [
        "## 质量与生成费用",
        "",
        table(
            [
                "组别",
                "AvgQ",
                f"AvgQ较{baseline_name}",
                "AvgPass",
                "JudgeErr",
                "Avg Gen$",
                f"Avg Gen$较{baseline_name}",
                "Total Gen$",
                "Gen exact",
            ],
            quality_rows,
        ),
        "",
        "## Token 用量",
        "",
        table(
            [
                "组别",
                "Avg Input",
                "Avg Output",
                "Avg Reason",
                "Avg Cache",
                "Avg Visible",
                "Avg Tokens",
            ],
            token_rows,
        ),
        "",
        "## 工具、请求与生成耗时",
        "",
        table(
            [
                "组别",
                "Avg Tools",
                "Tool%",
                "Avg Steps",
                "Avg LLMReq",
                "p50 ms",
                f"p50较{baseline_name}",
                "p95 ms",
                f"p95较{baseline_name}",
            ],
            runtime_rows,
        ),
        "",
        "## 覆盖与费用来源",
        "",
        table(["组别", "完整原生评分", "0分占位任务", "生成失败"], coverage_rows),
        "",
        table(["组别", "已记录生成费", "补估生成费", "Judge费"], cost_rows),
        "",
        "## 口径",
        "",
        "- AvgQ相对变化为 `(当前AvgQ－基线AvgQ)÷基线AvgQ`，与质量百分点差值不同。",
        "- Avg Gen$、p50和p95为负变化时，分别表示生成费用或生成耗时下降。",
        "- Reason和Cache分别包含在Output和Input中；Avg Tokens等于Input加Output。",
        "- JudgeErr是重试耗尽后仍缺失的 criterion/repeat 单元数，不是题目数。",
        "- 本报告不包含显著性或非劣检验，不能仅凭点估计宣布模型升级胜出。",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", default="DRACO 多模型融合完整任务结果")
    parser.add_argument("--profiles-manifest", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    profiles = json.loads(args.profiles_manifest.read_text()) if args.profiles_manifest else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(report, args.title, profiles), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
