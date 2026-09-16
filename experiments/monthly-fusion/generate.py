#!/usr/bin/env python3
"""Explicit AEF DRACO fusion-generation adapter; execution requires --execute.

Reuses AEF's authenticated public-task loader, public-only materializer and
research instructions. Fusion is an explicit separate harness, not an override
of AEF's single-model harness. Judging is intentionally handled separately.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HARNESS = "aef-draco-opensquilla-fusion-generation/v2"
SCHEDULER_ADAPTER_VERSION = "disjoint-generation-batches/v1"
WRITE_LOCK = threading.Lock()
STOP = threading.Event()
CHILDREN: dict[int, subprocess.Popen] = {}
CHILD_LOCK = threading.Lock()


def utc() -> str:
    return datetime.now(UTC).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def draco_base_config() -> dict[str, Any]:
    """Native AEF research settings, shared identically by all fusion groups.

    Mirrors AutoEval-Factory main/aef/benchmarks/opensquilla.py and DRACO's
    _configure_opensquilla tool settings. The fusion profile owns llm/ensemble.
    All paths here are replaced with task-private paths before agent launch.
    """
    return {
        "workspace_strict": True,
        "agent_max_iterations": 100,
        "search_provider": "brave",
        "search_api_key_env": "BRAVE_API_KEY",
        "search_max_results": 10,
        "search_use_env_proxy": True,
        "search_fallback_policy": "off",
        "skills": {"allow_bundled": False},
        "tools": {
            "profile": "full",
            "deny": [
                "group:messaging",
                "group:sessions",
                "group:memory",
                "group:trusted_host",
                "apply_patch",
                "background_process",
                "execute_code",
                "process",
            ],
            "file_edit_requires_fresh_read": True,
        },
        "compaction": {"enabled": False},
        "naming": {"enabled": False},
        "sandbox": {"sandbox": False, "security_grading": False},
        "permissions": {"default_mode": "full"},
        "memory": {
            "flush_enabled": False,
            "auto_capture_enabled": False,
            "capture_mode": "off",
            "session_source_enabled": False,
            "repair_enabled": False,
        },
    }


def profile_file(root: Path, group: str) -> Path:
    candidates = [root / group / "config.toml", root / f"{group}.toml"]
    found = [p for p in candidates if p.is_file()]
    if len(found) != 1:
        raise ValueError(f"Exactly one profile required for {group}: {candidates}")
    return found[0]


def safe_id(task_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id).strip(".")
    if clean == task_id and clean and len(clean) <= 120:
        return clean
    return f"{clean[:100] or 'task'}-{digest(task_id.encode())[:12]}"


def subject_digest(root: Path) -> str:
    h = hashlib.sha256()
    files = [root / "pyproject.toml", *sorted((root / "src" / "opensquilla").rglob("*.py"))]
    for p in files:
        h.update(str(p.relative_to(root)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], bool]:
    if not path.is_file():
        return [], False
    rows = []
    complete = True
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                complete = False
                continue
            if isinstance(row, dict):
                rows.append(row)
            else:
                complete = False
    return rows, complete


def metrics_from_artifacts(agent: Path) -> dict[str, Any]:
    """Keep physical-call accounting distinct from logical agent iterations.

    Incomplete provider receipts stay incomplete; paid cancelled requests are
    never silently treated as free. The judge may enrich these raw metrics.
    """
    usage = read_json(agent / "usage.json") or {}
    rows, valid = jsonl_rows(agent / "provider-calls.jsonl")
    requests = {r.get("call_id"): r for r in rows if r.get("event") == "llm.request"}
    responses = {r.get("call_id"): r for r in rows if r.get("event") == "llm.response"}
    completed = [responses[k] for k in requests if k in responses]
    physical_complete = bool(requests) and valid and set(requests) == set(responses)
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
    totals: dict[str, Any] = {}
    for field in fields:
        values = [r.get("usage", {}).get(field) for r in completed]
        totals[field] = (
            sum(values)
            if values
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)
            else None
        )
    known_costs = [
        r.get("usage", {}).get("billed_cost")
        for r in completed
        if r.get("usage", {}).get("cost_source") == "provider_billed"
        and isinstance(r.get("usage", {}).get("billed_cost"), (int, float))
    ]
    cost_complete = physical_complete and len(known_costs) == len(requests)
    cost_exact = cost_complete and all(
        isinstance(r.get("response_ids"), list) and any(r["response_ids"]) for r in completed
    )
    transcripts, transcript_complete = jsonl_rows(agent / "transcript.jsonl")
    tool_events = [
        r
        for r in transcripts
        if isinstance(r.get("message"), dict) and r["message"].get("role") == "toolResult"
    ]
    totals.update(
        {
            "total_tokens": totals["input_tokens"] + totals["output_tokens"]
            if totals["input_tokens"] is not None and totals["output_tokens"] is not None
            else None,
            "visible_tokens": totals["output_tokens"] - totals["reasoning_tokens"]
            if totals["output_tokens"] is not None
            and totals["reasoning_tokens"] is not None
            and totals["output_tokens"] >= totals["reasoning_tokens"]
            else None,
            "tool_calls": len(tool_events) if transcript_complete else None,
            "llm_requests": len(requests) if valid else None,
            "logical_agent_iterations": usage.get("request_count"),
            "generation_cost_usd": sum(known_costs) if cost_complete else None,
            "generation_cost_known_usd": sum(known_costs) if known_costs else None,
            "generation_cost_exact": cost_exact,
            "generation_cost_complete": cost_complete,
            "physical_usage_complete": physical_complete,
            "physical_completed_calls": len(completed),
            "physical_unreceipted_calls": len(set(requests) - set(responses)),
            "usage_reported_cost_usd": usage.get("cost_usd"),
            "usage_reported_billed_cost_usd": usage.get("billed_cost"),
            "token_measurement": "sum_of_recorded_physical_response_usage",
            "token_measurement_anomalies": ["reasoning_tokens_exceed_output_tokens"]
            if totals["output_tokens"] is not None
            and totals["reasoning_tokens"] is not None
            and totals["reasoning_tokens"] > totals["output_tokens"]
            else [],
        }
    )
    # Partial physical receipts provide lower bounds only. Never expose those
    # bounds in the primary token metrics used to compute campaign averages.
    for field in (*fields, "total_tokens", "visible_tokens"):
        totals["known_" + field] = totals[field]
        if not physical_complete:
            totals[field] = None
    return totals


def audit_fusion_identity(agent: Path, config_path: Path, usage: dict[str, Any]) -> dict[str, Any]:
    """Separate model/dispatch identity violations from valid runtime failures."""
    config = tomllib.loads(config_path.read_text())
    candidates = config["llm_ensemble"]["candidates"]
    proposer_models = [r["model"] for r in candidates if r["role"] == "proposer"]
    aggregator_model = next(r["model"] for r in candidates if r["role"] == "aggregator")
    allowed = set(proposer_models) | {aggregator_model, config["llm"]["model"]}
    rows, readable = jsonl_rows(agent / "provider-calls.jsonl")
    requests = [r for r in rows if r.get("event") == "llm.request"]
    requested = [str(r.get("model") or "") for r in requests]
    actual = sorted(
        {
            str(r.get("actual_model"))
            for r in rows
            if r.get("event") == "llm.response" and r.get("actual_model")
        }
    )
    violations = []
    if any(m and m not in allowed for m in requested):
        violations.append("requested_model_outside_frozen_roster")
    # A provider may return a canonical snapshot or omit its vendor prefix.
    # Unknown response aliases require read-only binding verification; they are
    # not automatically evidence that the requested experiment model was wrong.
    unknown_actual_aliases = sorted(m for m in actual if m not in allowed)
    trace = usage.get("ensemble_trace")
    plan_verified = False
    if isinstance(trace, dict) and trace:
        plan = trace.get("selection_plan") or {}
        observed_proposers = [
            r.get("model") for r in plan.get("proposers", []) if isinstance(r, dict)
        ]
        observed_aggregator = (plan.get("aggregator") or {}).get("model")
        plan_verified = (
            trace.get("mode") == "b5_fusion"
            and plan.get("strategy") == "custom_b5"
            and observed_proposers == proposer_models
            and observed_aggregator == aggregator_model
        )
        if not plan_verified:
            violations.append("recorded_fusion_dispatch_plan_mismatch")
    # A deadline may interrupt all proposers before the terminal trace exists.
    # The initial physical dispatch roster still proves a fusion attempt.
    dispatch_verified = len(requested) >= len(proposer_models) and sorted(
        requested[: len(proposer_models)]
    ) == sorted(proposer_models)
    proven = readable and bool(requests) and (plan_verified or dispatch_verified)
    return {
        "status": "invalid"
        if violations
        else "unproven"
        if unknown_actual_aliases
        else "passed"
        if proven
        else "unproven",
        "violations": violations,
        "selection_plan_verified": plan_verified,
        "physical_proposer_dispatch_verified": dispatch_verified,
        "requested_models": sorted(set(requested)),
        "actual_response_models": actual,
        "allowed_models": sorted(allowed),
        "trace_readable": readable,
        "actual_model_aliases_unverified": unknown_actual_aliases,
    }


def scrub_text(text: str) -> str:
    for key in ("OPENROUTER_API_KEY", "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEY"):
        value = os.environ.get(key, "")
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


def scrub_log(path: Path) -> None:
    if path.is_file():
        original = path.read_text(encoding="utf-8", errors="replace")
        cleaned = scrub_text(original)
        if cleaned != original:
            path.write_text(cleaned, encoding="utf-8")
        path.chmod(0o600)


def emit_event(root: Path, payload: dict[str, Any]) -> None:
    event = {
        k: payload.get(k)
        for k in ("group", "task_id", "generation_status", "generation_elapsed_ms", "error_code")
    }
    event["recorded_at"] = utc()
    with WRITE_LOCK:
        with (root / "generation-manifest.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps(event, ensure_ascii=False), flush=True)


def task_command(args, task, task_root: Path) -> list[str]:
    agent = task_root / "agent"
    return [
        str(args.opensquilla_cli),
        "agent",
        "--message",
        task.prompt,
        "--agent",
        "main",
        "--session-id",
        f"draco-{digest(str(task_root).encode())[:24]}",
        "--workspace",
        str(agent / "workspace"),
        "--workspace-strict",
        "--workspace-lockdown",
        "--timeout",
        str(args.timeout),
        "--max-iterations",
        str(args.max_iterations),
        "--iteration-timeout-seconds",
        "1200",
        "--tool-timeout-seconds",
        "300",
        "--request-timeout-seconds",
        "900",
        "--max-provider-retries",
        "0",
        "--length-capped-continuations",
        "3",
        "--thinking",
        "high",
        "--transcript-path",
        str(agent / "transcript.jsonl"),
        "--usage-path",
        str(agent / "usage.json"),
        "--session-db-path",
        str(agent / "state" / "sessions.db"),
        "--no-memory-capture",
        "--stateless",
        "--stateless-keep-project-rules",
        "--unattended",
        "--permissions",
        "full",
        "--json",
    ]


def prepare_task(
    args, draco, profiles_module, task, group, template
) -> tuple[Path, dict[str, Any]]:
    import tomli_w

    task_root = args.run_root / "groups" / group / "tasks" / safe_id(task.task_id)
    agent = task_root / "agent"
    existing = read_json(task_root / "generation.json")
    for p in (agent, agent / "workspace", agent / "state", task_root / "config"):
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.chmod(0o700)
    # This is AEF's public-only materializer: expert rubrics are never written.
    if not existing or existing.get("generation_status") == "prepared":
        draco.materialize_task(task, task_root)
        draco._prepare_agent_workspace(agent / "workspace")
    config = copy.deepcopy(template)
    for key, value in draco_base_config().items():
        if key not in config or config[key] != value:
            raise ValueError(f"Frozen {group} profile lacks exact DRACO base setting: {key}")
    config["workspace_dir"] = str(agent / "workspace")
    config["state_dir"] = str(agent / "state")
    profiles_module.assert_profile_config(config, group, args.profile_spec)
    config_bytes = tomli_w.dumps(config).encode()
    config_path = task_root / "config" / "config.toml"
    if config_path.exists() and config_path.read_bytes() != config_bytes:
        raise ValueError(f"Task configuration drift: {config_path}")
    if not config_path.exists():
        config_path.write_bytes(config_bytes)
        config_path.chmod(0o600)
    base = {
        "schema_version": HARNESS,
        "harness": HARNESS,
        "group": group,
        "task_id": task.task_id,
        "task_directory": str(task_root),
        "generation_status": "prepared",
        "generation_elapsed_ms": None,
        "response_path": str(agent / "response.md"),
        "usage_path": str(agent / "usage.json"),
        "transcript_path": str(agent / "transcript.jsonl"),
        "provider_calls_path": str(agent / "provider-calls.jsonl"),
        "stdout_path": str(agent / "agent.stdout.json"),
        "stderr_path": str(agent / "agent.stderr.log"),
        "config_path": str(config_path),
        "config_sha256": digest(config_bytes),
        "prompt_sha256": digest(task.prompt.encode()),
        "dataset_sha256": draco.DATASET_SHA256,
        "research_instructions_sha256": draco.RESEARCH_AGENT_INSTRUCTIONS_SHA256,
        "subject_source_sha256": args.subject_source_sha256,
        "generation_attempt": 1,
        "metrics": {},
    }
    if existing:
        for key in (
            "schema_version",
            "group",
            "task_id",
            "config_sha256",
            "prompt_sha256",
            "subject_source_sha256",
        ):
            if existing.get(key) != base[key]:
                raise ValueError(
                    f"Existing generation identity drift for {group}/{task.task_id}: {key}"
                )
        return task_root, existing
    private_json(task_root / "generation.json", base)
    return task_root, base


def generate(args, draco, task, group, task_root, record):
    """Claim one prepared task before any subprocess or paid request exists."""
    with (task_root / ".generation-task.lock").open("a") as claim:
        try:
            fcntl.flock(claim, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"Task already claimed by another controller: {group}/{task.task_id}"
            ) from exc
        latest = read_json(task_root / "generation.json")
        if latest is None:
            raise RuntimeError(f"Prepared task record disappeared: {group}/{task.task_id}")
        if latest.get("generation_status") == "running":
            raise RuntimeError(f"Refusing to take over running task: {group}/{task.task_id}")
        if args.disjoint_batch and latest.get("generation_status") != "prepared":
            raise RuntimeError(f"Disjoint batch task is no longer prepared: {group}/{task.task_id}")
        return _generate_claimed(args, draco, task, group, task_root, latest)


def _generate_claimed(args, draco, task, group, task_root, record):
    if record["generation_status"] != "prepared":
        # Single generation per task: failed/running/orphaned work never causes
        # another paid request on resume. Saved artifacts remain reviewable.
        return {
            "group": group,
            "task_id": task.task_id,
            "status": "skipped_existing",
            "existing_status": record["generation_status"],
        }
    if STOP.is_set():
        return {"group": group, "task_id": task.task_id, "status": "not_started"}
    agent = task_root / "agent"
    env = dict(os.environ)
    # Point imports at exactly the user-selected clone, not another installed copy.
    env.update(
        {
            "PYTHONPATH": str(args.opensquilla_root / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "OPENSQUILLA_GATEWAY_CONFIG_PATH": record["config_path"],
            "OPENSQUILLA_STATE_DIR": str(agent / "state"),
            "OPENSQUILLA_PROVIDER_ROUTING_STRICT": "1",
            "OPENSQUILLA_AGENT_PERMISSIONS": "full",
            "OPENSQUILLA_TRUST_ENV": "1",
            "OPENSQUILLA_LLM_TRACE_RECORDER": "1",
            "OPENSQUILLA_LLM_TRACE_PATH": str(agent / "provider-calls.jsonl"),
            "OPENSQUILLA_LLM_TRACE_INCLUDE_CHUNKS": "0",
            "BRAVE_SEARCH_API_KEY": env["BRAVE_API_KEY"],
        }
    )
    # Avoid ambient agent home/config loading; credentials are process env only.
    env["XDG_CONFIG_HOME"] = str(agent / "state" / "xdg-config")
    env["XDG_DATA_HOME"] = str(agent / "state" / "xdg-data")
    record = {
        **record,
        "generation_status": "running",
        "started_at": utc(),
        "driver_pid": os.getpid(),
    }
    private_json(task_root / "generation.json", record)
    emit_event(args.run_root, record)
    start = time.monotonic()
    process = None
    returncode = None
    timed_out = False
    error = None
    try:
        with (
            (agent / "agent.stdout.json").open("w", encoding="utf-8") as stdout,
            (agent / "agent.stderr.log").open("w", encoding="utf-8") as stderr,
        ):
            process = subprocess.Popen(
                task_command(args, task, task_root),
                cwd=agent / "workspace",
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            with CHILD_LOCK:
                CHILDREN[process.pid] = process
            record["agent_pid"] = process.pid
            private_json(task_root / "generation.json", record)
            try:
                returncode = process.wait(timeout=args.timeout + 60)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    returncode = process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    returncode = process.wait()
    except Exception as exc:
        error = scrub_text(f"{type(exc).__name__}: {exc}")
    finally:
        if process:
            with CHILD_LOCK:
                CHILDREN.pop(process.pid, None)
    elapsed = round((time.monotonic() - start) * 1000)
    for name in (
        "agent.stdout.json",
        "agent.stderr.log",
        "usage.json",
        "transcript.jsonl",
        "provider-calls.jsonl",
    ):
        scrub_log(agent / name)
    payload = None
    try:
        payload = draco.parse_json_object(
            (agent / "agent.stdout.json").read_text(), source="fusion agent stdout"
        )
    except Exception as exc:
        error = error or scrub_text(f"{type(exc).__name__}: {exc}")
    response = ""
    response_metadata = {}
    agent_errors = []
    if payload:
        if not (agent / "usage.json").is_file() and isinstance(payload.get("usage"), dict):
            private_json(agent / "usage.json", payload["usage"])
        if isinstance(payload.get("errors"), list):
            agent_errors = payload["errors"]
        try:
            response, response_metadata = draco._resolve_opensquilla_response(
                payload, workspace=agent / "workspace"
            )
        except Exception as exc:
            error = error or scrub_text(f"{type(exc).__name__}: {exc}")
    metrics = metrics_from_artifacts(agent)
    sidecar = read_json(agent / "usage.json") or {}
    identity = audit_fusion_identity(agent, Path(record["config_path"]), sidecar)
    if response.strip():
        (agent / "response.md").write_text(response, encoding="utf-8")
        (agent / "response.md").chmod(0o600)
    agent_success = (
        returncode == 0
        and not timed_out
        and not error
        and not agent_errors
        and bool(response.strip())
    )
    # Recoverable proposer/tool errors do not invalidate an explicitly delivered
    # report. Judge every report with proven correct experiment identity.
    completed = identity["status"] == "passed" and bool(response.strip())
    status = (
        "completed"
        if completed
        else "experiment_invalid"
        if identity["status"] == "invalid"
        else "generation_failed"
    )
    record.update(
        {
            "generation_status": status,
            "finished_at": utc(),
            "generation_elapsed_ms": elapsed,
            "exit_code": returncode,
            "timed_out": timed_out,
            "agent_success": agent_success,
            "agent_errors": agent_errors,
            "identity_status": {"passed": "valid", "invalid": "error", "unproven": "unproven"}[
                identity["status"]
            ],
            "fusion_identity": identity,
            "report_available": bool(response.strip()),
            "eligible_for_judge": completed,
            "judge_eligible": completed,
            "generation_valid_terminal": identity["status"] == "passed",
            "failure_kind": None
            if completed
            else "experiment_identity"
            if identity["status"] == "invalid"
            else "runtime_no_report"
            if identity["status"] == "passed"
            else "runtime_identity_unproven",
            "error_code": None if completed else status,
            "error": error
            or (None if completed else "No scorable report with proven experiment identity"),
            "metrics": metrics,
            "response_sha256": digest(response.encode()) if response else None,
            **response_metadata,
        }
    )
    private_json(task_root / "generation.json", record)
    emit_event(args.run_root, record)
    return {"group": group, "task_id": task.task_id, "status": record["generation_status"]}


def stop_handler(signum, _frame):
    STOP.set()
    with CHILD_LOCK:
        for process in list(CHILDREN.values()):
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-root", type=Path, required=True)
    p.add_argument("--aef-root", type=Path, required=True)
    p.add_argument("--opensquilla-root", type=Path, required=True)
    p.add_argument("--opensquilla-cli", type=Path)
    p.add_argument("--profiles-root", type=Path)
    p.add_argument(
        "--groups",
        nargs="+",
        default=[],
        help="Profile groups to run; defaults to every group in profiles-manifest.json",
    )
    p.add_argument(
        "--expected-tasks", type=int, default=100, help="Required size of the frozen DRACO task set"
    )
    p.add_argument("--task-id", action="append", default=[])
    p.add_argument(
        "--exclude-task-id",
        action="append",
        default=[],
        help="Exclude this official task ID from every selected group; repeatable",
    )
    p.add_argument(
        "--disjoint-batch",
        default=None,
        help="Named prepared-only batch; verifies disjointness with all active controllers",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Prepare/execute only first N selected tasks per group; 0=all100",
    )
    p.add_argument("--concurrency", type=int, default=24)
    p.add_argument("--timeout", type=int, default=7200)
    p.add_argument("--max-iterations", type=int, default=100)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-only",
        action="store_true",
        help="Default: freeze/materialize without model calls",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Authorize this process to perform pending generation",
    )
    return p


def select_tasks(args, tasks):
    ids = {t.task_id for t in tasks}
    unknown = (set(args.task_id) | set(args.exclude_task_id)) - ids
    if unknown:
        raise ValueError("Unknown task IDs: " + ", ".join(sorted(unknown)))
    selected = [
        t
        for t in tasks
        if (not args.task_id or t.task_id in args.task_id) and t.task_id not in args.exclude_task_id
    ]
    return selected[: args.limit] if args.limit else selected


def process_start_ticks(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def flock_owners(path: Path) -> list[int]:
    """Read owner PIDs without acquiring or changing an existing flock."""
    if not path.exists():
        return []
    stat = path.stat()
    owners = []
    for line in Path("/proc/locks").read_text().splitlines():
        parts = line.split()
        if "FLOCK" not in parts or "->" in parts:
            continue
        index = parts.index("FLOCK")
        try:
            major, minor, inode = parts[index + 4].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == (
                os.major(stat.st_dev),
                os.minor(stat.st_dev),
                stat.st_ino,
            ):
                owners.append(int(parts[index + 3]))
        except (ValueError, IndexError):
            continue
    return sorted(set(owners))


def active_legacy_scope(args, all_tasks, pid: int) -> dict[str, Any]:
    """Reconstruct the old global-lock controller's selected scope read-only.

    The already-running smoke predates batch registries and task claims. Its
    live process argv is therefore required evidence before scheduling around it.
    No process environment, memory, signal, or network connection is touched.
    """
    try:
        argv = [v.decode() for v in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if v]
        script = str(args.run_root / "driver" / "generate.py")
        index = argv.index(script)
        legacy, unknown = parser().parse_known_args(argv[index + 1 :])
    except (OSError, UnicodeError, ValueError, SystemExit) as exc:
        raise RuntimeError(f"Cannot prove active legacy controller scope for pid {pid}") from exc
    if unknown or Path(legacy.run_root).resolve() != args.run_root:
        raise RuntimeError(f"Unrecognized active legacy controller scope for pid {pid}")
    selected = select_tasks(legacy, all_tasks)
    return {
        "pid": pid,
        "process_start_ticks": process_start_ticks(pid),
        "scope": [[group, task.task_id] for task in selected for group in legacy.groups],
        "argv_sha256": digest(canonical(argv)),
        "evidence": "/proc/locks + /proc/PID/cmdline",
    }


@contextmanager
def scheduler_scope(args, all_tasks, selected_tasks):
    """Atomically register a bounded scope, refusing overlapping active work."""
    batch_root = args.run_root / "generation-batches"
    batch_root.mkdir(mode=0o700, exist_ok=True)
    batch_name = args.disjoint_batch or f"default-{os.getpid()}-{process_start_ticks(os.getpid())}"
    registry_path = batch_root / f"{batch_name}.json"
    proposed = {(group, task.task_id) for task in selected_tasks for group in args.groups}
    if not proposed:
        raise ValueError("Selected batch is empty")
    with (args.run_root / ".generation-scheduler.lock").open("a") as coordinator:
        fcntl.flock(coordinator, fcntl.LOCK_EX)
        for path in sorted(batch_root.glob("*.json")):
            other = read_json(path) or {}
            pid = int(other.get("pid") or 0)
            alive = (
                pid > 0
                and other.get("status") == "active"
                and process_start_ticks(pid) == other.get("process_start_ticks")
            )
            if not alive:
                continue
            overlap = proposed & {tuple(item) for item in other.get("scope", [])}
            if overlap:
                raise RuntimeError(
                    f"Batch overlaps active batch {other.get('batch')}: {sorted(overlap)[:3]}"
                )
        legacy_evidence = []
        for pid in flock_owners(args.run_root / ".generation-driver.lock"):
            if pid == os.getpid():
                continue
            legacy = active_legacy_scope(args, all_tasks, pid)
            overlap = proposed & {tuple(item) for item in legacy["scope"]}
            if overlap:
                raise RuntimeError(
                    f"Batch overlaps active legacy controller pid {pid}: {sorted(overlap)[:3]}"
                )
            legacy_evidence.append(legacy)
        if args.disjoint_batch:
            # Only pre-materialized pending work may be claimed by this mode.
            # In particular it cannot rescue or replace a running/orphaned task.
            for group, task_id in sorted(proposed):
                path = (
                    args.run_root
                    / "groups"
                    / group
                    / "tasks"
                    / safe_id(task_id)
                    / "generation.json"
                )
                status = (read_json(path) or {}).get("generation_status")
                if status != "prepared":
                    raise RuntimeError(
                        f"Disjoint batch requires prepared task; {group}/{task_id} is {status}"
                    )
        registration = {
            "schema_version": SCHEDULER_ADAPTER_VERSION,
            "batch": batch_name,
            "status": "active",
            "pid": os.getpid(),
            "process_start_ticks": process_start_ticks(os.getpid()),
            "registered_at": utc(),
            "scope": [list(item) for item in sorted(proposed)],
            "scope_sha256": digest(canonical(sorted(proposed))),
            "driver_sha256": digest(Path(__file__).read_bytes()),
            "subject_source_sha256": args.subject_source_sha256,
            "excluded_task_ids": sorted(args.exclude_task_id),
            "execute": args.execute,
            "concurrency": args.concurrency,
            "verified_legacy_controllers": legacy_evidence,
        }
        private_json(registry_path, registration)
    try:
        yield registration
    finally:
        with (args.run_root / ".generation-scheduler.lock").open("a") as coordinator:
            fcntl.flock(coordinator, fcntl.LOCK_EX)
            registration.update(status="finished", finished_at=utc(), stopped=STOP.is_set())
            private_json(registry_path, registration)


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    if (
        args.concurrency < 1
        or args.limit < 0
        or args.timeout < 1
        or args.max_iterations < 1
        or args.expected_tasks < 1
    ):
        raise ValueError("Invalid capacity/limit/timeout")
    if args.disjoint_batch is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.disjoint_batch):
            raise ValueError("Invalid disjoint batch name")
        if not args.exclude_task_id:
            raise ValueError("Disjoint batch requires explicit --exclude-task-id")
    for key in ("run_root", "aef_root", "opensquilla_root"):
        setattr(args, key, getattr(args, key).resolve())
    args.profiles_root = (args.profiles_root or args.run_root / "profiles").resolve()
    manifest_path = args.profiles_root / "profiles-manifest.json"
    manifest = read_json(manifest_path)
    if manifest is None or not isinstance(manifest.get("lineup_spec"), dict):
        raise ValueError(f"Missing or invalid frozen profiles manifest: {manifest_path}")
    args.profile_spec = manifest["lineup_spec"]
    available_groups = tuple(manifest.get("profiles", {}))
    if not available_groups:
        raise ValueError("profiles manifest contains no groups")
    args.groups = list(args.groups or available_groups)
    args.campaign_groups = available_groups
    unknown_groups = set(args.groups) - set(available_groups)
    if unknown_groups:
        raise ValueError("Unknown profile groups: " + ", ".join(sorted(unknown_groups)))
    if len(args.groups) != len(set(args.groups)):
        raise ValueError("Groups must be unique")
    args.opensquilla_cli = (
        args.opensquilla_cli or args.opensquilla_root / ".venv/bin/opensquilla"
    ).absolute()
    if args.execute:
        missing = [
            key for key in ("OPENROUTER_API_KEY", "BRAVE_API_KEY") if not os.environ.get(key)
        ]
        if missing:
            raise ValueError("Missing required secret environment names: " + ", ".join(missing))
        if not args.opensquilla_cli.is_file() or not os.access(args.opensquilla_cli, os.X_OK):
            raise ValueError(f"Missing executable subject CLI: {args.opensquilla_cli}")
    args.run_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.disjoint_batch:
        batch_root = args.run_root / "generation-batches"
        batch_root.mkdir(mode=0o700, exist_ok=True)
        lock_path = batch_root / f"{args.disjoint_batch}.lock"
    else:
        lock_path = args.run_root / ".generation-driver.lock"
    lock = lock_path.open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sys.path.insert(0, str(args.aef_root / "main"))
    draco_path = args.aef_root / "main/benchmarks/draco-bench/draco_bench.py"
    draco = load_module("monthly_aef_native_draco", draco_path)
    profiles = load_module("monthly_fusion_profiles", Path(__file__).with_name("profiles.py"))
    tasks = draco.load_tasks(args.aef_root / "main/benchmarks/draco-bench/tasks")
    if len(tasks) != args.expected_tasks:
        raise ValueError(f"Expected {args.expected_tasks} frozen tasks, got {len(tasks)}")
    templates, profile_hashes = {}, {}
    for group in args.groups:
        path = profile_file(args.profiles_root, group)
        templates[group] = tomllib.loads(path.read_text())
        profiles.assert_profile_config(templates[group], group, args.profile_spec)
        profile_hashes[group] = digest(path.read_bytes())
        expected_hash = (manifest.get("profiles", {}).get(group) or {}).get("config_sha256")
        if profile_hashes[group] != expected_hash:
            raise ValueError(f"Profile hash differs from profiles manifest: {group}")
    args.subject_source_sha256 = subject_digest(args.opensquilla_root)
    campaign = {
        "schema_version": HARNESS,
        "dataset_sha256": draco.DATASET_SHA256,
        "aef_draco_source_sha256": digest(draco_path.read_bytes()),
        "research_instructions_sha256": draco.RESEARCH_AGENT_INSTRUCTIONS_SHA256,
        "opensquilla_root": str(args.opensquilla_root),
        "subject_source_sha256": args.subject_source_sha256,
        "opensquilla_cli": str(args.opensquilla_cli),
        "task_ids": [t.task_id for t in tasks],
        "timeout_s": args.timeout,
        "max_iterations": args.max_iterations,
        "groups": list(args.campaign_groups),
        "profiles_manifest_sha256": digest(manifest_path.read_bytes()),
        "research_base_config": draco_base_config(),
    }
    freeze_path = args.run_root / "generation-campaign-lock.json"
    previous = read_json(freeze_path)
    if previous and previous != campaign:
        raise ValueError(
            "Generation campaign lock changed; use a distinct run root for a different experiment"
        )
    if previous is None:
        private_json(freeze_path, campaign)
    for group in args.groups:
        group_freeze = args.run_root / "groups" / group / "profile-lock.json"
        value = {"group": group, "profile_sha256": profile_hashes[group]}
        previous_group = read_json(group_freeze)
        if previous_group is not None and previous_group != value:
            raise ValueError(f"Frozen profile drift for {group}")
        if previous_group is None:
            private_json(group_freeze, value)
    all_tasks = tasks
    tasks = select_tasks(args, all_tasks)
    with scheduler_scope(args, all_tasks, tasks):
        return execute_selected(args, draco, profiles, tasks, templates)


def execute_selected(args, draco, profiles, tasks, templates):
    jobs = []
    for task in tasks:
        for group in args.groups:
            task_root, record = prepare_task(args, draco, profiles, task, group, templates[group])
            jobs.append((task, group, task_root, record))
    if not args.execute:
        summary = {
            "schema_version": HARNESS,
            "mode": "prepare_only",
            "task_count_per_group": len(tasks),
            "groups": args.groups,
            "generation_count": len(jobs),
            "paid_calls_started": 0,
        }
        summary_path = args.run_root / (
            f"generation-prepare-report-{args.disjoint_batch}.json"
            if args.disjoint_batch
            else "generation-prepare-report.json"
        )
        private_json(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, stop_handler)
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [
            executor.submit(generate, args, draco, task, group, root, record)
            for task, group, root, record in jobs
        ]
        for future in as_completed(futures):
            results.append(future.result())
    summary = {
        "schema_version": HARNESS,
        "mode": "execute",
        "finished_at": utc(),
        "stopped": STOP.is_set(),
        "selected_generations": len(jobs),
        "results": results,
    }
    summary_path = args.run_root / (
        f"generation-run-summary-{args.disjoint_batch}.json"
        if args.disjoint_batch
        else "generation-run-summary.json"
    )
    private_json(summary_path, summary)
    print(
        json.dumps(
            {
                "finished_at": summary["finished_at"],
                "counts": {
                    status: sum(r["status"] == status for r in results)
                    for status in sorted({r["status"] for r in results})
                },
            },
            ensure_ascii=False,
        )
    )
    return (
        1 if any(r["status"] in {"generation_failed", "experiment_invalid"} for r in results) else 0
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": scrub_text(f"{type(exc).__name__}: {exc}")}), file=sys.stderr)
        raise SystemExit(1)
