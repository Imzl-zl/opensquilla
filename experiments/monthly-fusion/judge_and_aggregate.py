#!/usr/bin/env python3
# ruff: noqa: E501
"""DRACO monthly evaluation: resumable native criterion judging and offline reporting.

Only the `judge` command makes model calls. `prepare` and `aggregate` are offline.
Generation is owned by the separate driver; generation.json is authoritative.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import email.utils
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import random
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

TRANSPORT_REVISION = "httpx-audited-v1"
JUDGE_REQUEST_REVISION = "openai-compatible-reasoning-config-v1"
SCHEDULER_REVISION = "shared-rps-gate-v1"
TLS = threading.local()
WRITE_LOCK = threading.Lock()


class SharedRequestGate:
    """One no-burst start-rate gate and shared cooldown for all workers/retries."""

    def __init__(self, requests_per_second, *, clock=time.monotonic, sleep=time.sleep):
        if not math.isfinite(requests_per_second) or requests_per_second <= 0:
            raise ValueError("requests_per_second must be finite and positive")
        self.requests_per_second = float(requests_per_second)
        self.interval = 1.0 / self.requests_per_second
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_start = self.cooldown_until = 0.0

    def acquire(self):
        while True:
            with self.lock:
                now = self.clock()
                allowed = max(self.next_start, self.cooldown_until)
                if now >= allowed:
                    self.next_start = now + self.interval
                    return now
                delay = allowed - now
            self.sleep(min(delay, 1.0))

    def defer(self, seconds):
        with self.lock:
            now = self.clock()
            # Same-wave 429s replace/max absolute deadlines, never add 60s each.
            self.cooldown_until = max(self.cooldown_until, now + max(0.0, seconds))
            return self.cooldown_until - now


def parse_retry_after(value, now_s=None):
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(str(value))
            return max(0.0, parsed.timestamp() - (time.time() if now_s is None else now_s))
        except (TypeError, ValueError, OverflowError):
            return None


def begin_dispatch(context, diagnostic=None):
    # Waiting workers have no started event and do not consume a paid-attempt slot.
    context["gate"].acquire()
    append_jsonl(
        context["attempt_path"],
        {
            **context["base"],
            "event": "started",
            "recorded_at": time.time(),
            "requests_per_second": context["gate"].requests_per_second,
        },
    )
    if diagnostic is not None:
        append_jsonl(
            context["attempt_path"], {**context["base"], **diagnostic, "recorded_at": time.time()}
        )


def read_json(path):
    return json.loads(Path(path).read_text())


def read_jsonl(path):
    if not Path(path).exists():
        return []
    rows = []
    for i, line in enumerate(Path(path).read_text().split("\n"), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            raise ValueError(
                f"Malformed checkpoint JSON at {path}:{i}; recover the interrupted final line before resuming"
            ) from None
    return rows


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    with tmp.open("w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def append_jsonl(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with WRITE_LOCK:
        with path.open("a") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load_native(aef_root):
    main = Path(aef_root) / "main"
    sys.path.insert(0, str(main))
    path = main / "benchmarks/draco-bench/draco_bench.py"
    spec = importlib.util.spec_from_file_location("monthly_draco_native", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tasks = module.load_tasks(path.parent / "tasks")
    return module, {t.task_id: t for t in tasks}, path


def resolve_artifact(gen_path, value, default):
    p = Path(value or default)
    return p if p.is_absolute() else gen_path.parent / p


def generations(run_dir, groups):
    seen = set()
    rows = []
    for group in groups:
        for path in sorted((Path(run_dir) / "groups" / group / "tasks").glob("*/generation.json")):
            row = read_json(path)
            if row.get("group") != group:
                raise ValueError(f"Generation group mismatch: {path}")
            key = (group, row["task_id"])
            if key in seen:
                raise ValueError(f"Duplicate authoritative generation: {key}")
            seen.add(key)
            rows.append((path, row))
    return rows


def generation_classification(gen_path, gen):
    terminal = gen.get("generation_status") in (
        "completed",
        "failed",
        "generation_failed",
        "experiment_invalid",
    )
    identity_error = (
        gen.get("identity_status") == "error"
        or gen.get("identity_valid") is False
        or gen.get("failure_kind") in ("identity_error", "experiment_identity")
        or gen.get("generation_status") == "experiment_invalid"
    )
    identity_valid = not identity_error and (
        gen.get("identity_status") == "valid"
        or gen.get("identity_valid") is True
        or ("identity_status" not in gen and gen.get("generation_status") == "completed")
    )
    response_path = resolve_artifact(gen_path, gen.get("response_path"), "agent/response.md")
    has_report = response_path.exists() and bool(response_path.read_text().strip())
    eligibility = gen.get("judge_eligible", gen.get("eligible_for_judge"))
    explicit_generation_failure = (
        gen.get("generation_status") in ("generation_failed", "failed") and eligibility is False
    )
    valid_generation_failure = (
        terminal and identity_valid and (not has_report or explicit_generation_failure)
    )
    eligible = (
        terminal
        and identity_valid
        and has_report
        and not valid_generation_failure
        and eligibility is not False
    )
    return {
        "terminal": terminal,
        "identity_error": identity_error,
        "identity_valid": identity_valid,
        "has_report": has_report,
        "judge_eligible": eligible,
        "valid_generation_failure": valid_generation_failure,
        "attempted": gen.get("generation_status") == "running" or terminal,
    }


def native_generation_failure(module, task, gen_path, gen):
    result_path = gen_path.parent / "grader/judge-result.json"
    if result_path.exists():
        raw = result_path.read_bytes()
        previous = json.loads(raw)
        if not (previous.get("metadata") or {}).get("generation_failure_classification"):
            archived = result_path.parent / "superseded-judge-results" / f"{sha(raw)}.json"
            if not archived.exists():
                archived.parent.mkdir(parents=True, exist_ok=True)
                with archived.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(archived, 0o600)
                append_jsonl(
                    result_path.parent / "judge-result-supersessions.jsonl",
                    {
                        "recorded_at": time.time(),
                        "source_sha256": sha(raw),
                        "archive_path": str(archived),
                        "prior_outcome": previous.get("outcome"),
                        "reason": "authoritative_identity_valid_generation_failure",
                        "notused_for_quality": True,
                        "attempts_and_receipts_retained_in_place": True,
                    },
                )
    result = module._error_judge(
        task,
        "Identity-valid subject execution failed to produce a scoreable final report",
        error_code="native_agent_failed",
        metadata={
            "generation_status": gen.get("generation_status"),
            "failure_kind": gen.get("failure_kind") or "generation_failure",
            "generation_elapsed_ms": gen.get("generation_elapsed_ms"),
            "criterion_judgments_fabricated": False,
            "generation_failure_classification": True,
            "prior_judge_artifacts_notused_for_quality": True,
        },
    )
    atomic_json(result_path, result)
    return result


def judge_config(args):
    return {
        "filename": "monthly-independent-judge.json",
        "provider": args.judge_provider,
        "provider_type": "openai",
        "model_id": args.judge_model,
        "endpoint": args.judge_endpoint,
        "api_key_env": args.judge_key_env,
        "authentication_required": True,
    }


def reasoning_config(args):
    if args.reasoning_mode == "omit":
        return None
    return {"enabled": args.reasoning_mode == "enabled"}


def protocol_binding(module, native_path, args):
    return {
        "judge_provider": args.judge_provider,
        "judge_model": args.judge_model,
        "judge_endpoint": args.judge_endpoint,
        "judge_repeats": module.OFFICIAL_JUDGE_REPEATS,
        "max_tokens": args.max_tokens,
        "temperature": 0,
        "reasoning": reasoning_config(args),
        "dataset_revision": module.DATASET_REVISION,
        "rubric_implementation_revision": module.RUBRIC_IMPLEMENTATION_REVISION,
        "rubric_system_prompt_sha256": module.RUBRIC_SYSTEM_PROMPT_SHA256,
        "aef_module_sha256": sha(native_path.read_bytes()),
    }


def judge_model_record(path, expected_model):
    if path is None:
        raise ValueError("freeze requires --judge-model-record with reviewed pricing")
    path = Path(path)
    raw = path.read_bytes()
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError(f"Judge model record must be a JSON object: {path}")
    if record.get("id") != expected_model:
        raise ValueError("Judge model record id differs from --judge-model")
    pricing = record.get("pricing")
    if not isinstance(pricing, dict):
        raise ValueError("Judge model record requires a pricing object")
    parsed = {}
    for name in ("prompt", "completion"):
        try:
            value = float(pricing[name])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Judge model pricing.{name} must be numeric") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Judge model pricing.{name} must be finite and nonnegative")
        parsed[name] = value
    if not any(parsed.values()):
        raise ValueError("Judge model prompt/completion prices cannot both be zero")
    return {
        "selected_model": record,
        "source": str(path.resolve()),
        "sha256": sha(raw),
    }


def validate_selection(module, native_path, args, *, required=False):
    path = Path(args.run_dir) / "judge-selection.json"
    if not path.exists():
        if required:
            raise ValueError(
                "judge-selection.json is required before paid judge calls; run the freeze command"
            )
        return None
    selection = read_json(path)
    if selection.get("groups") != list(args.campaign_groups):
        raise ValueError("Frozen independent judge groups differ from the campaign groups")
    if selection.get("active_protocol") != protocol_binding(module, native_path, args):
        raise ValueError(
            "Requested judge differs from judge-selection.json; preserve and explicitly version the selection before any paid calls"
        )
    return selection["selection_revision"]


def freeze_selection(module, native_path, args):
    if not args.selection_revision:
        raise ValueError("freeze requires --selection-revision")
    catalog_evidence = judge_model_record(args.judge_model_record, args.judge_model)
    path = Path(args.run_dir) / "judge-selection.json"
    if path.exists():
        selection = read_json(path)
        if (
            selection.get("selection_revision") != args.selection_revision
            or selection.get("groups") != list(args.campaign_groups)
            or selection.get("active_protocol") != protocol_binding(module, native_path, args)
            or selection.get("catalog_evidence") != catalog_evidence
        ):
            raise FileExistsError(f"judge selection already exists and differs: {path}")
        print(json.dumps(selection, ensure_ascii=False, indent=2))
        return 0
    payload = {
        "schema": "opensquilla-monthly-judge-selection/v1",
        "selection_revision": args.selection_revision,
        "frozen_at": time.time(),
        "groups": list(args.campaign_groups),
        "active_protocol": protocol_binding(module, native_path, args),
        "catalog_evidence": catalog_evidence,
    }
    atomic_json(path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def bind_task(module, native_path, gen_path, gen, args):
    response_path = resolve_artifact(gen_path, gen.get("response_path"), "agent/response.md")
    response = response_path.read_text()
    if not response.strip():
        raise ValueError(f"Completed generation has empty response: {gen_path}")
    grader = gen_path.parent / "grader"
    binding = {
        "schema": "draco-monthly-judge-binding/v3",
        "group": gen["group"],
        "task_id": gen["task_id"],
        "response_sha256": sha(response.encode()),
        "judge_selection_revision": validate_selection(module, native_path, args, required=True),
        **protocol_binding(module, native_path, args),
    }
    path = grader / "judge-state.json"
    if path.exists() and read_json(path) != binding:
        raise ValueError(
            f"Judge response/protocol binding changed: {path}; preserve the prior grader artifacts separately"
        )
    if not path.exists():
        atomic_json(path, binding)
    return grader, response, binding


def checkpoint_index(grader):
    completed = {}
    for row in read_jsonl(grader / "criterion_results.jsonl"):
        key = (row["repeat_index"], row["criterion_index"])
        if key in completed and completed[key] != row:
            raise ValueError(f"Conflicting criterion checkpoint: {grader}/{key}")
        completed[key] = row
    attempts = read_jsonl(grader / "judge_attempts.jsonl")
    counts = defaultdict(int)
    receipts = {}
    for event in attempts:
        key = (event["repeat_index"], event["criterion_index"])
        counts[key] = max(counts[key], int(event["attempt"]))
        if event["event"] == "receipt":
            receipts[key] = event
    return completed, counts, receipts


def native_row(
    module, criterion, repeat_index, criterion_index, receipt, configured_model, configured_provider
):
    verdict = module._parse_criterion_verdict(receipt["output_text"])
    effective_model = receipt.get("model") or configured_model
    return {
        "repeat_index": repeat_index,
        "criterion_index": criterion_index,
        "section_id": str(criterion["section_id"]),
        "section_title": str(criterion["section_title"]),
        "criterion_id": str(criterion["criterion_id"]),
        "criterion_type": "negative" if float(criterion["weight"]) < 0 else "positive",
        "weight": float(criterion["weight"]),
        "requirement": str(criterion["requirement"]),
        **verdict,
        "judge_provider": configured_provider,
        "judge_model": effective_model,
        "judge_model_configured": configured_model,
        "judge_model_effective": effective_model,
        "token_usage": receipt.get("token_usage", {}),
    }


def install_receipt_capture(module, args):
    original = module.invoke_provider_http
    import httpx
    from aef.runtime.provider_clients import ProviderClientError
    from aef.runtime.provider_http import HttpProviderResponse

    class AuditedHttpStatusError(ProviderClientError):
        def __init__(self, status, message, retry_after_s=None):
            super().__init__("provider_http_error", message)
            self.http_status = status
            self.retry_after_s = retry_after_s

    class AuditedHttpxTransport:
        """Native scoring prompt plus explicitly frozen judge reasoning mode."""

        def post_json(self, url, payload, *, headers=None, timeout_s=30):
            client = getattr(TLS, "http_client", None)
            if client is None:
                client = httpx.Client(trust_env=True, follow_redirects=False)
                TLS.http_client = client
            # AEF prompts, structured response format, temperature and token cap stay native.
            wire_payload = dict(payload)
            if reasoning_config(args) is not None:
                wire_payload["reasoning"] = reasoning_config(args)
            body = json.dumps(
                wire_payload, ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode("utf-8")
            request_headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **dict(headers or {}),
            }
            authorization = str(request_headers.get("Authorization") or "")
            markers = [authorization, authorization.removeprefix("Bearer ").strip()]
            context = TLS.context
            begin_dispatch(
                context,
                {
                    "event": "http.request_diagnostic",
                    "transport_revision": TRANSPORT_REVISION,
                    "judge_request_revision": JUDGE_REQUEST_REVISION,
                    "reasoning": reasoning_config(args),
                    "method": "POST",
                    "endpoint": url,
                    "request_payload_sha256": sha(body),
                    "request_bytes": len(body),
                    "user_agent": client.headers.get("User-Agent"),
                    "proxy_environment_names": [
                        name
                        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
                        if os.environ.get(name)
                    ],
                },
            )
            response = client.post(url, content=body, headers=request_headers, timeout=timeout_s)
            if response.status_code >= 400:
                retry_after_s = parse_retry_after(response.headers.get("retry-after"))
                error_body = response.text
                for marker in markers:
                    if marker:
                        error_body = error_body.replace(marker, "[REDACTED]")
                safe_headers = {
                    k: v
                    for k, v in response.headers.items()
                    if k.lower()
                    in {"content-type", "server", "cf-ray", "x-request-id", "retry-after"}
                }
                append_jsonl(
                    context["attempt_path"],
                    {
                        **context["base"],
                        "event": "http.error_diagnostic",
                        "recorded_at": time.time(),
                        "transport_revision": TRANSPORT_REVISION,
                        "http_status": response.status_code,
                        "response_headers": safe_headers,
                        "retry_after_s": retry_after_s,
                        "response_error_body": error_body[:16000],
                    },
                )
                raise AuditedHttpStatusError(
                    response.status_code,
                    f"provider HTTP request failed with status {response.status_code}; response diagnostics persisted",
                    retry_after_s=retry_after_s,
                )
            return HttpProviderResponse(
                status_code=response.status_code, headers=dict(response.headers), body=response.text
            )

    transport = AuditedHttpxTransport()

    def captured(*args, **kwargs):
        kwargs["transport"] = transport
        exchange = original(*args, **kwargs)
        context = TLS.context
        raw = dict(exchange.response.raw)
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        event = {
            **context["base"],
            "event": "receipt",
            "recorded_at": time.time(),
            "request_id": raw.get("id"),
            "model": exchange.response.model,
            "provider": raw.get("provider"),
            "token_usage": dict(exchange.response.token_usage),
            "raw_usage": usage,
            "output_text": exchange.response.output_text,
            "cost_usd": usage.get("cost"),
            "cost_exact": isinstance(usage.get("cost"), (float, int)) and bool(raw.get("id")),
        }
        append_jsonl(context["attempt_path"], event)
        return exchange

    module.invoke_provider_http = captured


def judge_job(module, task, response, grader, criterion, repeat, index, used, args, key):
    for attempt in range(used + 1, args.max_attempts + 1):
        base = {
            "task_id": task.task_id,
            "repeat_index": repeat,
            "criterion_index": index,
            "attempt": attempt,
            "transport_revision": TRANSPORT_REVISION,
            "judge_request_revision": JUDGE_REQUEST_REVISION,
            "scheduler_revision": SCHEDULER_REVISION,
        }
        TLS.context = {
            "base": base,
            "attempt_path": grader / "judge_attempts.jsonl",
            "gate": args.request_gate,
        }
        try:
            result = module._judge_one_criterion(
                task=task,
                response_text=response,
                criterion=criterion,
                repeat_index=repeat,
                criterion_index=index,
                judge_config=judge_config(args),
                judge_api_key=key,
                timeout_s=args.timeout_s,
                max_tokens=args.max_tokens,
            )
            append_jsonl(grader / "criterion_results.jsonl", result)
            return True
        except Exception as exc:
            # AEF redacts provider errors. Strip the secret marker again defensively.
            message = str(exc).replace(key, "[REDACTED]") if key else str(exc)
            append_jsonl(
                TLS.context["attempt_path"],
                {
                    **base,
                    "event": "error",
                    "recorded_at": time.time(),
                    "error_type": type(exc).__name__,
                    "error": message[:1000],
                    "http_status": getattr(exc, "http_status", None),
                    "retry_after_s": getattr(exc, "retry_after_s", None),
                },
            )
            if getattr(exc, "http_status", None) == 429:
                retry_after_s = getattr(exc, "retry_after_s", None)
                cooldown_s = 60.0 if retry_after_s is None else retry_after_s
                remaining_s = args.request_gate.defer(cooldown_s)
                append_jsonl(
                    TLS.context["attempt_path"],
                    {
                        **base,
                        "event": "rate_limit_cooldown",
                        "recorded_at": time.time(),
                        "retry_after_s": retry_after_s,
                        "cooldown_seconds": cooldown_s,
                        "remaining_global_cooldown_seconds": remaining_s,
                        "source": "fallback_60_seconds"
                        if retry_after_s is None
                        else "retry_after_header",
                    },
                )
                # The next attempt and all other workers must pass the same gate.
                if attempt < args.max_attempts:
                    continue
                return False
            if getattr(exc, "http_status", None) in (400, 401, 402, 403, 404, 405, 422):
                return False
            if attempt < args.max_attempts:
                time.sleep(min(2 ** (attempt - 1), 8) + random.random() * 0.5)
    return False


def score_ready(module, task, gen_path, gen, grader, binding, args):
    # Generation may be authoritatively reclassified while older jobs are in flight.
    gen = read_json(gen_path)
    classification = generation_classification(gen_path, gen)
    if classification["valid_generation_failure"]:
        native_generation_failure(module, task, gen_path, gen)
        return False
    if not classification["judge_eligible"]:
        return False
    response_path = resolve_artifact(gen_path, gen.get("response_path"), "agent/response.md")
    if sha(response_path.read_text().encode()) != binding["response_sha256"]:
        raise ValueError(
            f"Generation response changed before scoring: {gen_path}; refuse stale criterion results"
        )
    completed, _, _ = checkpoint_index(grader)
    required = task.grading["criterion_count"] * module.OFFICIAL_JUDGE_REPEATS
    if len(completed) != required:
        return False
    ordered = [completed[k] for k in sorted(completed)]
    result = module.judge_task(
        task,
        gen_path.parent,
        criterion_runs=ordered,
        metadata={
            "judge_provider": args.judge_provider,
            "judge_model": binding["judge_model"],
            "judge_repeats": module.OFFICIAL_JUDGE_REPEATS,
            "response_sha256": binding["response_sha256"],
            "generation_elapsed_ms": gen.get("generation_elapsed_ms"),
        },
    )
    atomic_json(grader / "judge-result.json", result)
    return result.get("outcome") == "scored"


def do_judge(args, module, tasks, native_path):
    validate_selection(module, native_path, args, required=True)
    if not args.key_file:
        raise ValueError("judge requires --key-file; prepare/aggregate never read credentials")
    key_path = Path(args.key_file)
    if key_path.stat().st_mode & 0o077:
        raise ValueError("Judge key file must not be accessible to group/other users")
    key = key_path.read_text().strip()
    if not key:
        raise ValueError("Judge key file is empty")
    args.request_gate = SharedRequestGate(args.requests_per_second)
    install_receipt_capture(module, args)
    jobs = []
    prepared = []
    for gen_path, gen in generations(args.run_dir, args.groups):
        if args.task_ids and gen["task_id"] not in args.task_ids:
            continue
        task = tasks[gen["task_id"]]
        classification = generation_classification(gen_path, gen)
        if classification["valid_generation_failure"]:
            native_generation_failure(module, task, gen_path, gen)
            continue
        if not classification["judge_eligible"]:
            continue
        grader, response, binding = bind_task(module, native_path, gen_path, gen, args)
        criteria = module._flatten_rubric(task)
        completed, used, receipts = checkpoint_index(grader)
        for repeat in range(module.OFFICIAL_JUDGE_REPEATS):
            for index, criterion in enumerate(criteria):
                pair = (repeat, index)
                if pair not in completed and pair in receipts:
                    try:
                        recovered = native_row(
                            module,
                            criterion,
                            repeat,
                            index,
                            receipts[pair],
                            args.judge_model,
                            args.judge_provider,
                        )
                    except ValueError:
                        pass
                    else:
                        append_jsonl(grader / "criterion_results.jsonl", recovered)
                        completed[pair] = recovered
                if pair not in completed and used[pair] < args.max_attempts:
                    jobs.append((task, response, grader, criterion, repeat, index, used[pair]))
        prepared.append((task, gen_path, gen, grader, binding))
    if args.max_jobs:
        jobs = jobs[: args.max_jobs]
    print(
        json.dumps(
            {
                "scheduled_criterion_jobs": len(jobs),
                "concurrency": args.concurrency,
                "requests_per_second": args.requests_per_second,
                "scheduler_revision": SCHEDULER_REVISION,
                "judge_model": args.judge_model,
                "official_repeats": module.OFFICIAL_JUDGE_REPEATS,
            }
        ),
        flush=True,
    )
    finished = successful = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(judge_job, module, *job, args, key) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            successful += int(future.result())
            finished += 1
            if finished % 50 == 0 or finished == len(jobs):
                atomic_json(
                    Path(args.run_dir) / "judge-progress.json",
                    {
                        "finished_this_invocation": finished,
                        "successful_this_invocation": successful,
                        "scheduled_this_invocation": len(jobs),
                        "updated_at": time.time(),
                        "requests_per_second": args.requests_per_second,
                        "scheduler_revision": SCHEDULER_REVISION,
                    },
                )
    for prepared_task in prepared:
        score_ready(module, *prepared_task, args)
    payload = aggregate(args, module, tasks)
    unresolved_errors = sum(
        s["JudgeErr"] + s["identity_error"] + s["identity_unresolved"] for s in payload["groups"]
    )
    failed_this_invocation = finished - successful
    has_error = bool(unresolved_errors or failed_this_invocation)
    all_covered = all(s["experiment_coverage_complete"] for s in payload["groups"])
    status = (
        "error_incomplete"
        if has_error
        else "complete"
        if all_covered
        else "smoke_passed_partial"
        if args.max_jobs and finished
        else "incomplete_waiting_generation_or_judging"
    )
    exit_code = 2 if has_error else 0
    invocation_result = {
        "status": status,
        "exit_code": exit_code,
        "finished_jobs": finished,
        "successful_jobs": successful,
        "failed_this_invocation": failed_this_invocation,
        "unresolved_errors": unresolved_errors,
        "experiment_coverage_complete": all_covered,
        "transport_revision": TRANSPORT_REVISION,
        "updated_at": time.time(),
    }
    invocation_result.update(
        requests_per_second=args.requests_per_second,
        scheduler_revision=SCHEDULER_REVISION,
        max_attempts=args.max_attempts,
        concurrency=args.concurrency,
    )
    atomic_json(Path(args.run_dir) / "judge-invocation-result.json", invocation_result)
    print(json.dumps(invocation_result), flush=True)
    return exit_code


def number(value):
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        else None
    )


def avg(values):
    return sum(values) / len(values) if values and all(v is not None for v in values) else None


def nearest(values, percentile):
    if not values or any(v is None for v in values):
        return None
    return sorted(values)[max(0, math.ceil(len(values) * percentile) - 1)]


def first_number(mapping, names):
    for name in names:
        value = number(mapping.get(name))
        if value is not None:
            return value
    return None


def generation_metrics(gen_path, gen):
    # The generation driver supplies physical-request totals, including failed/retried calls.
    # Never infer unknown usage as zero, and never add parent usage to its nested call units.
    metrics = dict(gen.get("metrics") or {})
    billing_path = gen_path.parent / "agent/billing-enrichment.json"
    billing = read_json(billing_path) if billing_path.exists() else {}
    if billing:
        if billing.get("group") not in (None, gen["group"]) or billing.get("task_id") not in (
            None,
            gen["task_id"],
        ):
            raise ValueError(f"Billing attribution mismatch: {billing_path}")
        if billing.get("response_sha256") is not None and billing["response_sha256"] != gen.get(
            "response_sha256"
        ):
            raise ValueError(f"Billing generation binding mismatch: {billing_path}")
        for name in (
            "generation_attempt",
            "config_sha256",
            "subject_source_sha256",
            "prompt_sha256",
        ):
            if name in billing and name in gen and billing[name] != gen[name]:
                raise ValueError(f"Billing {name} binding mismatch: {billing_path}")
        for name in (
            "generation_cost_usd",
            "generation_cost_known_usd",
            "generation_cost_exact",
            "generation_cost_complete",
        ):
            if name in billing:
                metrics[name] = billing[name]
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "reasoning_tokens": ("reasoning_tokens",),
        "cached_tokens": ("cached_tokens", "cache_read_tokens"),
        "visible_tokens": ("visible_tokens",),
        "total_tokens": ("total_tokens",),
        "tool_calls": ("tool_calls", "total_tool_call_count"),
        "llm_requests": ("llm_requests", "llm_request_count"),
        "generation_cost_usd": ("generation_cost_usd", "billed_cost"),
    }
    out = {key: first_number(metrics, names) for key, names in aliases.items()}
    token_fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
    stream_tokens = {name: out[name] for name in token_fields}
    if metrics.get("physical_usage_complete") is False:
        for name in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "visible_tokens",
            "total_tokens",
        ):
            out[name] = None
    token_sources = {
        name: "physical_generation_trace" if out[name] is not None else None
        for name in token_fields
    }
    token_diagnostics = []
    native_fields = {
        "input_tokens": "native_tokens_prompt",
        "output_tokens": "native_tokens_completion",
        "reasoning_tokens": "native_tokens_reasoning",
        "cached_tokens": "native_tokens_cached",
    }
    native_totals = billing.get("billing_native_token_totals") or {}
    native_complete = billing.get("billing_native_token_complete_by_field") or {}
    trace_path = resolve_artifact(
        gen_path, gen.get("provider_calls_path"), "agent/provider-calls.jsonl"
    )
    trace_binding_valid = bool(
        billing.get("trace_sha256")
        and trace_path.exists()
        and billing["trace_sha256"] == sha(trace_path.read_bytes())
    )
    native_scope_valid = (
        billing.get("billing_source") == "openrouter_generation_api"
        and billing.get("billing_scope")
        == "all_generation_physical_requests_including_failed_and_retried"
        and trace_binding_valid
    )
    if native_totals and not native_scope_valid:
        token_diagnostics.append(
            {
                "kind": "billing_native_scope_or_trace_binding_invalid",
                "trace_binding_valid": trace_binding_valid,
            }
        )
    for name, native_name in native_fields.items():
        value = number(native_totals.get(native_name))
        if native_scope_valid and native_complete.get(native_name) is True:
            if value is None or value < 0:
                token_diagnostics.append(
                    {
                        "kind": "invalid_complete_native_token_value",
                        "field": name,
                        "value": native_totals.get(native_name),
                    }
                )
                continue
            if out[name] is not None and out[name] != value:
                token_diagnostics.append(
                    {
                        "kind": "stream_billing_token_mismatch",
                        "field": name,
                        "physical_trace_value": out[name],
                        "billing_native_value": value,
                    }
                )
            out[name] = (
                value  # Replace the same physical-request total; never add stream and billing.
            )
            token_sources[name] = "openrouter_generation_api_native_tokens"
    out["total_tokens"] = (
        out["input_tokens"] + out["output_tokens"]
        if out["input_tokens"] is not None and out["output_tokens"] is not None
        else None
    )
    out["visible_tokens"] = None
    if out["output_tokens"] is not None and out["reasoning_tokens"] is not None:
        if out["output_tokens"] >= out["reasoning_tokens"]:
            out["visible_tokens"] = out["output_tokens"] - out["reasoning_tokens"]
        else:
            token_diagnostics.append(
                {
                    "kind": "reasoning_exceeds_output",
                    "output_tokens": out["output_tokens"],
                    "reasoning_tokens": out["reasoning_tokens"],
                }
            )
    out["generation_token_source_by_field"] = token_sources
    out["generation_token_complete_by_field"] = {
        name: out[name] is not None for name in (*token_fields, "visible_tokens", "total_tokens")
    }
    out["generation_token_diagnostics"] = token_diagnostics
    out["physical_trace_token_totals_before_billing"] = stream_tokens
    out["billing_native_token_totals"] = native_totals
    out["billing_native_token_complete_by_field"] = native_complete
    out["generation_cost_exact"] = metrics.get("generation_cost_exact") is True
    out["generation_cost_complete"] = metrics.get("generation_cost_complete") is True
    out["generation_cost_known_usd"] = number(metrics.get("generation_cost_known_usd"))
    out["physical_usage_complete"] = metrics.get("physical_usage_complete")
    out["physical_unreceipted_calls"] = number(metrics.get("physical_unreceipted_calls"))
    out["billing_enrichment_path"] = str(billing_path) if billing else None
    out["billing_source"] = billing.get("billing_source")
    out["billing_unresolved_call_ids"] = billing.get("billing_unresolved_call_ids")
    out["generation_elapsed_ms"] = first_number(gen, ("generation_elapsed_ms", "elapsed_ms"))
    if out["generation_elapsed_ms"] is None:
        out["generation_elapsed_ms"] = first_number(metrics, ("generation_elapsed_ms",))
    return out


def aggregate(args, module, tasks):
    all_gen = generations(args.run_dir, args.groups)
    per_task = []
    native_results = defaultdict(list)
    for path, gen in all_gen:
        classification = generation_classification(path, gen)
        grader = path.parent / "grader"
        result_path = grader / "judge-result.json"
        result = (
            native_generation_failure(module, tasks[gen["task_id"]], path, gen)
            if classification["valid_generation_failure"]
            else read_json(result_path)
            if classification["judge_eligible"] and result_path.exists()
            else {}
        )
        if result:
            native_results[gen["group"]].append(result)
        metadata = result.get("metadata") or {}
        checkpoints, counts, receipts = checkpoint_index(grader)
        expected_jobs = (
            tasks[gen["task_id"]].grading["criterion_count"] * module.OFFICIAL_JUDGE_REPEATS
        )
        events = read_jsonl(grader / "judge_attempts.jsonl")
        starts = {
            (e["repeat_index"], e["criterion_index"], e["attempt"])
            for e in events
            if e["event"] == "started"
        }
        paid_receipts = {
            (e["repeat_index"], e["criterion_index"], e["attempt"]): e
            for e in events
            if e["event"] == "receipt"
        }
        judge_known_cost = sum(number(e.get("cost_usd")) or 0.0 for e in paid_receipts.values())
        judge_cost_complete = len(paid_receipts) == len(starts) and all(
            number(e.get("cost_usd")) is not None for e in paid_receipts.values()
        )
        missing = expected_jobs - len(checkpoints)
        exhausted = sum(
            1 for pair, n in counts.items() if n >= args.max_attempts and pair not in checkpoints
        )
        scoring_excluded = (
            classification["valid_generation_failure"] or classification["identity_error"]
        )
        quality = number(result.get("score")) if result.get("outcome") == "scored" else None
        per_task.append(
            {
                "group": gen["group"],
                "task_id": gen["task_id"],
                "generation_status": gen.get("generation_status"),
                **classification,
                **generation_metrics(path, gen),
                "quality": quality,
                "criterion_pass_rate": number(metadata.get("pass_rate")),
                "judge_complete": bool(metadata.get("official_repeat_count_complete"))
                and result.get("outcome") == "scored",
                "judge_identity_consistent": metadata.get("judge_identity_consistent") is True,
                "judge_provider": metadata.get("judge_provider"),
                "judge_model": metadata.get("judge_model"),
                "judge_missing": 0 if scoring_excluded else missing,
                "judge_errors": 0 if scoring_excluded else exhausted,
                "judge_artifacts_notused_for_quality": scoring_excluded,
                "judge_disregarded_criterion_rows": len(checkpoints) if scoring_excluded else 0,
                "judge_historical_exhausted_units": exhausted,
                "judge_failed_attempts": sum(e["event"] == "error" for e in events),
                "judge_cost_usd": judge_known_cost if judge_cost_complete else None,
                "judge_known_cost_usd": judge_known_cost,
                "judge_request_count": len(starts),
                "judge_priced_receipt_count": sum(
                    number(e.get("cost_usd")) is not None for e in paid_receipts.values()
                ),
                "generation_path": str(path),
                "judge_result_path": str(result_path),
            }
        )
    summaries = []
    expected_n = len(tasks)
    all_identities = {
        (r["judge_provider"], r["judge_model"]) for r in per_task if r["quality"] is not None
    }
    cross_group_judge_consistent = len(all_identities) <= 1
    for group in args.groups:
        rows = [r for r in per_task if r["group"] == group]
        n = len(rows)
        exact = sum(r["generation_cost_exact"] and r["generation_cost_complete"] for r in rows)
        native_aggregate = module.aggregate_results(native_results[group])
        complete = (
            n == expected_n
            and all(r["judge_complete"] for r in rows)
            and native_aggregate.get("official_protocol_complete") is True
            and cross_group_judge_consistent
        )
        experiment_complete = (
            n == expected_n
            and all(r["terminal"] and r["identity_valid"] for r in rows)
            and all(
                r["valid_generation_failure"]
                or (r["judge_complete"] and r["judge_identity_consistent"])
                for r in rows
            )
            and cross_group_judge_consistent
        )

        def vals(name):
            return [row[name] for row in rows]

        scored = [r["quality"] for r in rows if r["quality"] is not None]
        costs = vals("generation_cost_usd")
        cost_complete = (
            n == expected_n
            and all(r["generation_cost_complete"] for r in rows)
            and all(c is not None for c in costs)
            and all(r["identity_valid"] for r in rows)
        )
        tools, requests = vals("tool_calls"), vals("llm_requests")
        token_coverage = {
            name: sum(
                (r.get("generation_token_complete_by_field") or {}).get(name) is True for r in rows
            )
            for name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "visible_tokens",
                "total_tokens",
            )
        }
        known_gen_costs = [
            r["generation_cost_known_usd"]
            if r["generation_cost_known_usd"] is not None
            else r["generation_cost_usd"]
            for r in rows
            if r["generation_cost_known_usd"] is not None or r["generation_cost_usd"] is not None
        ]
        priced_judge_receipts = sum(vals("judge_priced_receipt_count"))
        judge_request_count = sum(vals("judge_request_count"))
        summary = {
            "Group": group,
            "Rows": n,
            "Done": sum(r["judge_complete"] for r in rows),
            "AvgQ": 100 * native_aggregate["primary_score"]
            if native_aggregate.get("primary_score") is not None
            else None,
            "AvgPass": native_aggregate.get("pass_rate_percent"),
            "JudgeErr": sum(vals("judge_errors")),
            "JudgeErr Tasks": sum(r["judge_errors"] > 0 for r in rows),
            "Had Judge Error Tasks": sum(r["judge_failed_attempts"] > 0 for r in rows),
            "Avg Gen$": avg(costs) if cost_complete else None,
            "Total Gen$": sum(costs) if cost_complete else None,
            "Gen exact": f"{exact}/{expected_n}",
            "Avg Input": avg(vals("input_tokens")),
            "Avg Output": avg(vals("output_tokens")),
            "Avg Reason": avg(vals("reasoning_tokens")),
            "Avg Cache": avg(vals("cached_tokens")),
            "Avg Visible": avg(vals("visible_tokens")),
            "Avg Tokens": avg(vals("total_tokens")),
            "Avg Tools": avg(tools),
            "Tool%": 100 * sum(t > 0 for t in tools) / n
            if n and all(t is not None for t in tools)
            else None,
            "Avg Steps": avg(
                [
                    t + q if t is not None and q is not None else None
                    for t, q in zip(tools, requests)
                ]
            ),
            "Avg LLMReq": avg(requests),
            "p50 ms": nearest(vals("generation_elapsed_ms"), 0.50),
            "p95 ms": nearest(vals("generation_elapsed_ms"), 0.95),
            "AvgQ Scored": 100 * avg(scored) if scored else None,
            "Judge Missing": sum(vals("judge_missing")),
            "Judge Failed Attempts": sum(vals("judge_failed_attempts")),
            "Judge Requests": sum(vals("judge_request_count")),
            "Total Judge$": sum(vals("judge_cost_usd"))
            if n and all(v is not None for v in vals("judge_cost_usd"))
            else None,
            "Known Judge$": sum(vals("judge_known_cost_usd")),
            "Token Coverage Tasks": token_coverage,
            "Token Coverage Denominator": expected_n,
            "Known Gen$ Lower Bound": sum(known_gen_costs) if known_gen_costs else None,
            "Known Gen$ Tasks": len(known_gen_costs),
            "Judge Priced Receipts": priced_judge_receipts,
            "Known Judge$ Lower Bound": (
                sum(vals("judge_known_cost_usd"))
                if priced_judge_receipts
                else 0.0
                if judge_request_count == 0
                else None
            ),
            "Campaign Known Gen$ (incl identity errors)": sum(
                r["generation_cost_known_usd"]
                if r["generation_cost_known_usd"] is not None
                else r["generation_cost_usd"]
                if r["generation_cost_usd"] is not None
                else 0.0
                for r in rows
            ),
            "Attempted": sum(r["attempted"] for r in rows),
            "GenFail": sum(r["valid_generation_failure"] for r in rows),
            "identity_error": sum(r["identity_error"] for r in rows),
            "identity_unresolved": sum(
                r["terminal"] and not r["identity_valid"] and not r["identity_error"] for r in rows
            ),
            "Generation Failed": sum(r["valid_generation_failure"] for r in rows),
            "AvgQ (0-for-GenFail; non-native)": 100 * sum(scored) / expected_n
            if experiment_complete
            else None,
            "experiment_coverage_complete": experiment_complete,
            "quality_denominator": native_aggregate.get("scored_tasks", 0),
            "official_protocol_complete": complete,
            "native_aggregate": native_aggregate,
            "cross_group_judge_consistent": cross_group_judge_consistent,
        }
        summaries.append(summary)
    preflight_starts = preflight_errors = 0
    preflight_known_cost = 0.0
    preflight_complete = True
    for path in sorted((Path(args.run_dir) / "preflight").glob("**/judge_attempts.jsonl")):
        events = read_jsonl(path)
        starts = {
            (e["repeat_index"], e["criterion_index"], e["attempt"])
            for e in events
            if e["event"] == "started"
        }
        receipts = {
            (e["repeat_index"], e["criterion_index"], e["attempt"]): e
            for e in events
            if e["event"] == "receipt"
        }
        preflight_starts += len(starts)
        preflight_errors += sum(e["event"] == "error" for e in events)
        preflight_known_cost += sum(number(e.get("cost_usd")) or 0.0 for e in receipts.values())
        preflight_complete &= len(starts) == len(receipts) and all(
            number(e.get("cost_usd")) is not None for e in receipts.values()
        )
    selection_path = Path(args.run_dir) / "judge-selection.json"
    payload = {
        "schema": "draco-monthly-comparison/v1",
        "updated_at": time.time(),
        "expected_tasks_per_group": expected_n,
        "groups": summaries,
        "tasks": per_task,
        "judge_selection": read_json(selection_path) if selection_path.exists() else None,
        "archived_judge_preflight": {
            "requests": preflight_starts,
            "failed_attempts": preflight_errors,
            "known_cost_usd": preflight_known_cost,
            "cost_complete": preflight_complete,
            "total_cost_usd": preflight_known_cost if preflight_complete else None,
            "scope": "archived preflight only; separate from active scoring judge costs",
        },
    }
    atomic_json(Path(args.run_dir) / "comparison.json", payload)
    columns = [
        "Group",
        "Rows",
        "Done",
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
    ]

    def fmt(v):
        if v is None:
            return "N/A"
        return f"{v:.6f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)

    lines = [
        "# DRACO 多模型融合评测",
        "",
        "质量均分严格使用AEF原生scored集合，分母见下表；无报告的真实generation失败单列，不伪造criterion判决或原生0分。",
        "",
        f"| Group | Attempted/{expected_n} | Scored/{expected_n} | GenFail | JudgeErr | JudgeErr Tasks | Had Judge Error Tasks | identity_error | identity_unresolved | Native completion | Experiment coverage complete |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    lines += [
        f"| {s['Group']} | {s['Attempted']}/{expected_n} | {s['Done']}/{expected_n} | {s['GenFail']} | {s['JudgeErr']} | {s['JudgeErr Tasks']} | {s['Had Judge Error Tasks']} | {s['identity_error']} | {s['identity_unresolved']} | {s['native_aggregate']['completion_status']} | {s['experiment_coverage_complete']} |"
        for s in summaries
    ]
    lines += ["", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(fmt(s[c]) for c in columns) + " |" for s in summaries]
    lines += [
        "",
        f"Token字段完整覆盖任务数（各字段独立判断；分母为计划的{expected_n}题）：",
        "",
        "| Group | Input | Output | Reason | Cache | Visible | Tokens |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    token_names = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "visible_tokens",
        "total_tokens",
    )
    lines += [
        "| "
        + s["Group"]
        + " | "
        + " | ".join(f"{s['Token Coverage Tasks'][name]}/{expected_n}" for name in token_names)
        + " |"
        for s in summaries
    ]
    lines += [
        "",
        "当前账本已知费用下限（包括失败、重试和身份错误任务的实际已知费用；不替代主表完整费用）：",
        "",
        "| Group | Known Gen$ lower bound | Tasks with Gen$ subtotal | Gen exact | Known Judge$ lower bound | Priced judge receipts / started requests |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {s['Group']} | {fmt(s['Known Gen$ Lower Bound'])} | {s['Known Gen$ Tasks']}/{expected_n} | {s['Gen exact']} | {fmt(s['Known Judge$ Lower Bound'])} | {s['Judge Priced Receipts']}/{s['Judge Requests']} |"
        for s in summaries
    ]
    if preflight_starts:
        lines += [
            "",
            f"归档的裁判接入预检：{preflight_starts}次请求、{preflight_errors}次错误，已知费用${preflight_known_cost:.6f}；完整费用为{fmt(preflight_known_cost if preflight_complete else None)}。这些接入记录独立于当前统一裁判的评分与费用；未知费用不记为0。",
        ]
    repeats = module.OFFICIAL_JUDGE_REPEATS
    lines += [
        "",
        f"- AvgQ：每题原生加权分（含负权重惩罚、限于0–100），先平均{repeats}次criterion judge，再对原生scored题集求均值，实际分母见coverage表。AvgPass为criterion符合要求比例，非任务成功率。无报告的有效generation失败由原生native_agent_failed记录，无官方score，不进入这两个原生质量均分；不能因此将结果称为完整官方成绩。",
        f"- 补充字段AvgQ(0-for-GenFail)仅在{expected_n}题全部有效终态且所有可评分报告judge完成时计算：无报告generation失败给予任务级0分，分母{expected_n}。它是非原生运行指标，不生成虚假的criterion判决，也不提供0惩罚AvgPass。",
        f"- Avg Gen$/Total Gen$：本轮全部generation物理请求，包括起草、汇总、工具续接、失败和重试；不含judge。Gen exact为所有generation请求账单完整且精确的任务数/{expected_n}。缺失费用不是0。",
        "- Token为generation物理请求总计后按任务平均；Reason/Cache是子集，Avg Tokens=Input+Output，不再重复加Reason/Cache。Visible=Output−Reason，仅在字段可核对时计算。",
        "- Avg Tools为实际执行工具数，Tool%为至少执行一次工具的任务比例；Avg Steps=物理generation LLMReq+实际Tools，非agent轮数。",
        "- p50/p95：本轮统一为纯generation墙钟耗时，采用nearest-rank；与旧报表包含judge的completed_at−started_at不同。",
        "- JudgeErr为checkpoint中达到最大attempt仍未成功的criterion/repeat单元；Judge Missing、失败attempt数和Judge费用另存comparison.json，修复成功不会伪装成从未发生过重试。",
        "- JudgeErr Tasks按任务去重统计含耗尽未成功criterion/repeat的任务；Had Judge Error Tasks统计当前裁判账本中曾有error attempt的任务，包含后来恢复或被GenFail排除质量的历史请求；归档预检另列。",
        f"- Token覆盖表分母为计划{expected_n}题，主均值仍沿用对当前Rows集合求均值的原有口径；其中任何一题该字段未知就显示N/A，部分字段完整不代表其他字段完整。费用下限只汇总有数值的小计；没有任何数值小计时显示N/A。明确的0值保留为0，但部分账单的0下限不表示最终费用为0；judge没有started请求时当前费用为0，有请求但无计价receipt时为未知。Tasks with Gen$ subtotal也包含部分小计，不等于完整账单任务数。",
        f"- 成功criterion/repeat即时持久化；{repeats}次judge是同一生成答案的重复评分，不是{repeats}次generation，也不按最多得分挑答案。",
        "",
    ]
    (Path(args.run_dir) / "comparison.md").write_text("\n".join(lines))
    atomic_json(
        Path(args.run_dir) / "judge-status.json", {"updated_at": time.time(), "groups": summaries}
    )
    print(
        json.dumps(
            {"comparison": str(Path(args.run_dir) / "comparison.md"), "groups": summaries},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return payload


def discover_groups(run_dir):
    groups_root = Path(run_dir) / "groups"
    discovered = (
        tuple(
            sorted(
                path.name
                for path in groups_root.iterdir()
                if path.is_dir() and (path / "tasks").is_dir()
            )
        )
        if groups_root.is_dir()
        else ()
    )
    if not discovered:
        raise ValueError(f"No campaign groups found under {groups_root}")
    lock_path = Path(run_dir) / "generation-campaign-lock.json"
    if lock_path.exists():
        frozen = read_json(lock_path).get("groups")
        if frozen is not None:
            if not isinstance(frozen, list) or set(frozen) != set(discovered):
                raise ValueError(
                    "generation campaign lock groups differ from materialized group directories"
                )
            return tuple(frozen)
    return discovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "freeze", "judge", "aggregate"))
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--aef-root", required=True, type=Path)
    parser.add_argument(
        "--groups",
        default="",
        help="Comma-separated subset; defaults to every frozen campaign group",
    )
    parser.add_argument("--task-ids", default="")
    parser.add_argument("--key-file")
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--judge-provider", default="openrouter")
    parser.add_argument("--judge-key-env", default="MONTHLY_DRACO_JUDGE_KEY")
    parser.add_argument("--judge-endpoint", default="https://openrouter.ai/api/v1/chat/completions")
    parser.add_argument(
        "--reasoning-mode", choices=("disabled", "enabled", "omit"), default="disabled"
    )
    parser.add_argument(
        "--selection-revision",
        help="Immutable human-readable revision required by the freeze command",
    )
    parser.add_argument(
        "--judge-model-record",
        type=Path,
        help="Reviewed JSON model/pricing record required by the freeze command",
    )
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument(
        "--requests-per-second",
        type=float,
        default=1.0,
        help="Shared no-burst HTTP start rate across every judge worker and retry",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=10,
        help="Total attempts per criterion/repeat unit, including the first request",
    )
    parser.add_argument("--timeout-s", type=int, default=120)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument(
        "--max-jobs",
        type=int,
        default=0,
        help="Bound scheduled criterion jobs for an explicit paid smoke",
    )
    args = parser.parse_args()
    args.run_dir = args.run_dir.resolve()
    args.aef_root = args.aef_root.resolve()
    args.campaign_groups = discover_groups(args.run_dir)
    requested_groups = tuple(s for s in args.groups.split(",") if s)
    args.groups = requested_groups or args.campaign_groups
    args.task_ids = set(s for s in args.task_ids.split(",") if s)
    if (
        not args.groups
        or len(args.groups) != len(set(args.groups))
        or any(g not in args.campaign_groups for g in args.groups)
    ):
        raise ValueError("Groups must be a unique, nonempty subset of the frozen campaign groups")
    if not 1 <= args.concurrency <= 256 or not 1 <= args.max_attempts <= 10:
        raise ValueError("Invalid concurrency or total attempt budget")
    if not math.isfinite(args.requests_per_second) or args.requests_per_second <= 0:
        raise ValueError("requests-per-second must be finite and positive")
    module, tasks, native_path = load_native(args.aef_root)
    validate_selection(module, native_path, args, required=args.command in ("judge", "aggregate"))
    if args.command == "prepare":
        selected_rows = [
            row
            for row in generations(args.run_dir, args.groups)
            if not args.task_ids or row[1]["task_id"] in args.task_ids
        ]
        selected_jobs = (
            sum(tasks[row[1]["task_id"]].grading["criterion_count"] for row in selected_rows)
            * module.OFFICIAL_JUDGE_REPEATS
        )
        print(
            json.dumps(
                {
                    "tasks": len(tasks),
                    "criteria": sum(t.grading["criterion_count"] for t in tasks.values()),
                    "official_judge_repeats": module.OFFICIAL_JUDGE_REPEATS,
                    "judge_model": args.judge_model,
                    "criterion_jobs_for_materialized_rows": selected_jobs,
                    "generation_rows": len(selected_rows),
                    "paid_calls": 0,
                }
            )
        )
        return
    with (args.run_dir / ".judge-writer.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == "freeze":
            return freeze_selection(module, native_path, args)
        if args.command == "judge":
            return do_judge(args, module, tasks, native_path)
        else:
            aggregate(args, module, tasks)


if __name__ == "__main__":
    sys.exit(main() or 0)
