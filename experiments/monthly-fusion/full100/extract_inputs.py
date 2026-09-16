#!/usr/bin/env python3
"""Read campaign records and emit a compressed, credential-free snapshot."""

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")


def read(path):
    return json.loads(path.read_bytes())


def lines(path):
    if path.exists():
        for line in path.read_bytes().splitlines():
            if line.strip():
                yield json.loads(line)


def count(text):
    text = text or ""
    return sum(
        len(ENC.encode(text[i : i + 4096], disallowed_special=()))
        for i in range(0, len(text), 4096)
    )


def selected_judge_model(selection, explicit_path=None):
    if explicit_path:
        return read(Path(explicit_path))
    catalog = selection.get("catalog_evidence") or {}
    selected = catalog.get("selected_model")
    if not isinstance(selected, dict):
        raise ValueError(
            "Judge pricing/model record is required via --judge-model-record "
            "or judge-selection catalog evidence"
        )
    return selected


def resolve_receipt(run, value):
    path = Path(value)
    return path if path.is_absolute() else run / path


def extract(
    run,
    *,
    generation_catalog=None,
    judge_model_record=None,
    include_judge=True,
    emit=True,
    output=None,
):
    """Default emits the final snapshot; generation-only use omits live judge files."""
    run = Path(run).resolve()
    tasks = []
    for path in sorted((run / "groups").glob("*/tasks/*/generation.json")):
        gen = read(path)
        billing_path = path.parent / "agent/billing-enrichment.json"
        billing = (
            read(billing_path)
            if billing_path.exists()
            else {
                "billing_requests": [],
                "generation_cost_exact": False,
                "generation_cost_known_usd": gen.get("metrics", {}).get(
                    "generation_cost_known_usd"
                ),
            }
        )
        visible_by_call = {}
        calls = {}
        for event in lines(path.parent / "agent/provider-calls.jsonl"):
            cid = event["call_id"]
            call = calls.setdefault(cid, {"call_id": cid})
            kind = event["event"]
            if kind == "llm.request":
                payload = event.get("payload") or {}
                meta = event.get("metadata") or {}
                proof = meta.get("request_proof") or {}
                call.update(
                    model=event["model"],
                    call_index=event["call_index"],
                    started_at=event["created_at"],
                    payload_sha256=event.get("payload_sha256"),
                    input_token_estimate=proof.get("estimated_tokens"),
                    input_estimator=proof.get("token_estimate_source"),
                    tools_count=len(payload.get("tools") or []),
                    message_count=len(payload.get("messages") or []),
                    max_tokens=payload.get("max_tokens"),
                    cache_shape=meta.get("cache_shape"),
                )
            elif kind == "llm.response":
                visible = event.get("assistant_text") or ""
                tool_calls = event.get("tool_calls") or []
                if tool_calls:
                    visible += json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":"))
                visible_by_call[cid] = visible
                usage = event.get("usage") or {}
                call.update(usage=usage, finished_at=event["created_at"])
                if (usage.get("reasoning_tokens") or 0) > (usage.get("output_tokens") or 0):
                    call["visible_text_token_estimate"] = count(visible)
            elif kind == "llm.error":
                call.update(
                    error={key: event.get(key) for key in ("code", "status_code")},
                    finished_at=event["created_at"],
                )
        for item in billing["billing_requests"]:
            call = calls[item["call_id"]]
            receipts = []
            for receipt_path in item["receipt_paths"]:
                receipt = read(resolve_receipt(run, receipt_path))
                data = receipt.get("data") or {}
                receipts.append(
                    {
                        "generation_id": receipt.get("generation_id"),
                        "status": receipt.get("status"),
                        "data": {
                            key: data.get(key)
                            for key in (
                                "total_cost",
                                "native_tokens_prompt",
                                "native_tokens_completion",
                                "native_tokens_reasoning",
                                "native_tokens_cached",
                                "tokens_prompt",
                                "tokens_completion",
                                "model",
                                "provider_name",
                                "cancelled",
                            )
                        },
                    }
                )
            call["receipts"] = receipts
            paid = [
                r["data"] for r in receipts if isinstance(r["data"].get("total_cost"), (int, float))
            ]
            usage = call.get("usage") or {}

            def observed_token(native, field):
                if paid and all(isinstance(d.get(native), (int, float)) for d in paid):
                    return sum(d[native] for d in paid)
                return usage.get(field) or 0

            if observed_token("native_tokens_reasoning", "reasoning_tokens") > observed_token(
                "native_tokens_completion", "output_tokens"
            ):
                call["visible_text_token_estimate"] = count(
                    visible_by_call.get(call["call_id"], "")
                )
        attempts = {}
        for event in lines(path.parent / "grader/judge_attempts.jsonl") if include_judge else ():
            if not all(key in event for key in ("repeat_index", "criterion_index", "attempt")):
                continue
            key = (event["repeat_index"], event["criterion_index"], event["attempt"])
            a = attempts.setdefault(
                key, dict(zip(("repeat_index", "criterion_index", "attempt"), key))
            )
            kind = event["event"]
            if kind == "started":
                a["started_at"] = event["recorded_at"]
            elif kind == "http.request_diagnostic":
                a["request_bytes"] = event.get("request_bytes")
                a["payload_sha256"] = event.get("request_payload_sha256")
            elif kind == "receipt":
                a.update(
                    cost_usd=event.get("cost_usd"),
                    usage=event.get("raw_usage") or event.get("token_usage") or {},
                    model=event.get("model"),
                    request_id=event.get("request_id"),
                )
                if event.get("cost_usd") is None:
                    a["output_text_token_estimate"] = count(event.get("output_text"))
            elif kind in ("http.error_diagnostic", "error"):
                if event.get("http_status") is not None:
                    a["http_status"] = event["http_status"]
                if kind == "error":
                    a["error_type"] = event.get("error_type")
        tasks.append(
            {
                "group": gen["group"],
                "task_id": gen["task_id"],
                "generation_status": gen["generation_status"],
                "generation_elapsed_ms": gen["generation_elapsed_ms"],
                "generation_metrics": gen["metrics"],
                "original_exact_cost": billing["generation_cost_exact"],
                "original_known_cost_usd": billing["generation_cost_known_usd"],
                "calls": sorted(calls.values(), key=lambda x: x["call_index"]),
                "judge_attempts": [v for v in attempts.values() if "started_at" in v],
            }
        )
    selection = read(run / "judge-selection.json")
    catalog_path = (
        Path(generation_catalog) if generation_catalog else run / "model-catalog-snapshot.json"
    )
    snapshot = {
        "schema": "draco-full100-existing-records/v2",
        "run_dir": str(run),
        "generation_catalog": read(catalog_path),
        "judge_model": selected_judge_model(selection, judge_model_record),
        "tasks": tasks,
        "network_calls": 0,
        "new_model_calls": 0,
        "text_tokenizer": "cl100k_base, 4096-character chunks; text estimates only",
        "source_comparison_sha256": hashlib.sha256(
            (run / "comparison.json").read_bytes()
        ).hexdigest()
        if include_judge
        else None,
    }
    if not include_judge:
        snapshot["judge_data_included"] = False
        snapshot["scope"] = (
            "generation-only; judge attempts and live quality comparison are not read"
        )
    if emit:
        encoded = gzip.compress(
            json.dumps(
                snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode(),
            mtime=0,
        )
        if output:
            Path(output).write_bytes(encoded)
        else:
            sys.stdout.buffer.write(encoded)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--generation-catalog", type=Path)
    parser.add_argument("--judge-model-record", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--generation-only", action="store_true")
    args = parser.parse_args()
    extract(
        args.run_dir,
        generation_catalog=args.generation_catalog,
        judge_model_record=args.judge_model_record,
        include_judge=not args.generation_only,
        output=args.output,
    )


if __name__ == "__main__":
    main()
