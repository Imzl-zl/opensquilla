#!/usr/bin/env python3
"""Bounded, real-provider Gateway acceptance for Plan, Goal and waiting turns.

Run explicitly with a selected provider's environment key. Raw conversation and
Gateway state exist only in a private temporary tree, removed on exit. A test-only
HTTP dispatch guard forwards the original production transport and counts actual
requests, including retries and children; it never supplies a model response.
Offline unit tests exercise this harness, not the truth of its live assertions.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import platform
import re
import runpy
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from opensquilla.gateway_client import GatewayRPCClient, GatewayRPCError  # noqa: E402
from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    child_environment,
    classify_failure,
    minimal_child_environment,
    provider_response_model_matches,
    registry_endpoint,
    require_temporary_report_path,
    retain_failed_temporary_tree,
    sanitize_report,
    scan_and_remove_temporary_tree,
    write_safe_report,
)
from scripts.smoke_v4_phase3_router import (  # noqa: E402
    _free_port,
    _read_turn_call_records,
    _wait_for_gateway_health,
)

LIMITS = {
    "physical_calls": 24,
    "root_turns": 4,
    "children": 2,
    "output_tokens": 4096,
    "request_seconds": 90,
    "case_seconds": 600,
    "transient_retries": 1,
}
MODELS = {
    "tokenrhythm": "deepseek-v4-pro-0813",
    "deepseek": "deepseek-flash",
    "openrouter": "deepseek/deepseek-v4-flash",
}
SCENARIOS = {
    "plan": ("plan", "cancel", "plan-stop", "plan-recovery"),
    "plan-stop": ("plan-stop",),
    "plan-stop-child": ("plan-stop-child",),
    "plan-recovery": ("plan-recovery",),
    "goal": ("goal", "budget", "childbudget", "background", "restart"),
    "wait": ("wait",),
    "childbudget": ("childbudget",),
    "background": ("background",),
    "plan-only": ("plan",),
    "goal-control": ("goal",),
    "budget": ("budget",),
    "restart": ("restart",),
    "cli": ("cli",),
}
ALLOWED_TOOLS = [
    "read_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "list_dir",
    "grep_search",
    "glob_search",
    "exec_command",
    "submit_plan",
    "update_plan",
    "request_user_input",
    "get_goal",
    "create_goal",
    "update_goal",
    "update_goal_progress",
    "sessions_spawn",
    "sessions_yield",
    "sessions_history",
]
TERMINAL = {"succeeded", "failed", "cancelled", "timeout", "abandoned"}


class CaseFailureError(RuntimeError):
    """A bounded assertion failure, never a provider transcript."""


class DispatchLimitError(RuntimeError):
    """Raised before dispatch when the isolated run's reviewed bound is reached."""


class DispatchGuard:
    """Durable atomic limits survive Gateway restart and concurrent HTTP calls."""

    def __init__(self, path: Path, *, provider: str, model: str) -> None:
        self.path = path
        self.provider = provider
        self.model = model
        self.endpoint = registry_endpoint(provider)
        with self.connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER);"
                "CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, kind TEXT);"
                "CREATE TABLE IF NOT EXISTS requests "
                "(id INTEGER PRIMARY KEY, status INTEGER, failure TEXT);"
                "CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT);"
            )
            db.execute("INSERT OR IGNORE INTO settings VALUES ('started', ?)", (str(time.time()),))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # sqlite3's context manager commits/rolls back, but does not close its
        # handle. Explicit closure is required before deleting evidence on Windows.
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def claim(self, name: str, limit: int, *, turn_id: str | None = None) -> int:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            started = float(
                db.execute("SELECT value FROM settings WHERE name='started'").fetchone()[0]
            )
            if time.time() - started >= LIMITS["case_seconds"]:
                raise DispatchLimitError("case_deadline")
            if turn_id and db.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)).fetchone():
                return 0
            row = db.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
            count = int(row[0]) if row else 0
            if count >= limit:
                raise DispatchLimitError(f"{name}_limit")
            if name == "physical_calls":
                failed = db.execute(
                    "SELECT value FROM settings WHERE name='pending_failure'"
                ).fetchone()
                if failed and failed[0]:
                    if failed[0] not in {"transport", "rate-limit"}:
                        raise DispatchLimitError("nonretryable_provider_failure")
                    retries = db.execute(
                        "SELECT value FROM counters WHERE name='transient_retries'"
                    ).fetchone()
                    if retries and retries[0] >= LIMITS["transient_retries"]:
                        raise DispatchLimitError("transient_retry_limit")
                    db.execute("INSERT OR REPLACE INTO counters VALUES ('transient_retries', 1)")
                    db.execute("DELETE FROM settings WHERE name='pending_failure'")
            db.execute("INSERT OR REPLACE INTO counters VALUES (?, ?)", (name, count + 1))
            if turn_id:
                db.execute("INSERT INTO turns VALUES (?, ?)", (turn_id, name))
            if name == "physical_calls":
                db.execute("INSERT INTO requests (id) VALUES (?)", (count + 1,))
            return count + 1

    def request(self, method: str, url: str, body: bytes) -> int:
        actual, expected = urlsplit(url), urlsplit(self.endpoint)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise DispatchLimitError("unselected_provider_endpoint")
        # Registry/catalog GETs are not model calls and cannot consume tokens.
        if method == "GET":
            return 0
        if method != "POST" or not actual.path.endswith(("/chat/completions", "/responses")):
            raise DispatchLimitError("unreviewed_provider_operation")
        payload = json.loads(body)
        if payload.get("model") != self.model:
            raise DispatchLimitError("unselected_model")
        output = [
            payload[k]
            for k in ("max_tokens", "max_completion_tokens", "max_output_tokens")
            if k in payload
        ]
        if not output or any(
            type(n) is not int or not 0 < n <= LIMITS["output_tokens"] for n in output
        ):
            raise DispatchLimitError("output_limit")
        if payload.get("n", 1) != 1 or payload.get("best_of", 1) != 1:
            raise DispatchLimitError("multiple_completions")
        return self.claim("physical_calls", LIMITS["physical_calls"])

    def release_unstarted_turn(self, name: str, turn_id: str) -> None:
        """Return only a newly reserved turn that explicitly never activated."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute("DELETE FROM turns WHERE id=? AND kind=?", (turn_id, name))
            if deleted.rowcount:
                db.execute("UPDATE counters SET value=value-1 WHERE name=? AND value>0", (name,))

    def finish(self, index: int, status: int | None, failure: str = "") -> None:
        if index:
            with self.connect() as db:
                db.execute(
                    "UPDATE requests SET status=?, failure=? WHERE id=?", (status, failure, index)
                )
                if failure:
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES ('pending_failure', ?)",
                        (failure,),
                    )

    def logical_failure(self, code: str) -> None:
        """Catch provider error events carried inside HTTP 200 streams as well."""
        failure = classify_failure(code.replace("_", " "))
        with self.connect() as db:
            # The HTTP boundary may know a more precise quota/auth diagnosis.
            db.execute("INSERT OR IGNORE INTO settings VALUES ('pending_failure', ?)", (failure,))

    def snapshot(self) -> dict[str, Any]:
        with self.connect() as db:
            return {
                "counts": dict(db.execute("SELECT name,value FROM counters")),
                "http_statuses": [
                    row[0] for row in db.execute("SELECT status FROM requests ORDER BY id")
                ],
                "failures": [
                    row[0]
                    for row in db.execute(
                        "SELECT failure FROM requests WHERE failure != '' ORDER BY id"
                    )
                ],
            }



class DisconnectDispatchGate:
    """Hold an armed request until the real Gateway registry loses its last client."""

    def __init__(self, guard: DispatchGuard) -> None:
        self.guard = guard
        self.disconnected = asyncio.Event()
        self.waiting = asyncio.Event()

    def arm(self, turn_id: str) -> None:
        with self.guard.connect() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('disconnect_gate_turn', ?)",
                       (turn_id,))

    def _armed_turn(self) -> str | None:
        with self.guard.connect() as db:
            row = db.execute(
                "SELECT value FROM settings WHERE name='disconnect_gate_turn'"
            ).fetchone()
        return str(row[0]) if row else None

    def after_unregister(self, registry: Any) -> None:
        if not self._armed_turn() or registry.all():
            return
        # The existing Goal observer has already run, and this is the actual
        # registry-removal boundary, not completion of client.close().
        with self.guard.connect() as db:
            roots = db.execute(
                "SELECT value FROM counters WHERE name='root_turns'"
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('disconnect_boundary_roots', ?)",
                (str(roots[0] if roots else 0),),
            )
            calls = db.execute(
                "SELECT value FROM counters WHERE name='physical_calls'"
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('disconnect_boundary_calls', ?)",
                (str(calls[0] if calls else 0),),
            )
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('disconnect_boundary_connections', '0')"
            )
        self.disconnected.set()

    async def before_dispatch(self, turn_id: str | None, registry: Any) -> None:
        armed = self._armed_turn()
        if not turn_id or not armed:
            return
        if turn_id != armed:
            if self.disconnected.is_set():
                if registry.all():
                    raise DispatchLimitError("disconnect_gate_connection_returned")
                with self.guard.connect() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES "
                               "('automatic_dispatch_without_connections', '1')")
            return
        with self.guard.connect() as db:
            started = float(db.execute(
                "SELECT value FROM settings WHERE name='started'"
            ).fetchone()[0])
        remaining = max(0.0, started + LIMITS['case_seconds'] - time.time())
        self.waiting.set()
        async with asyncio.timeout(remaining):
            await self.disconnected.wait()
        if registry.all():
            raise DispatchLimitError("disconnect_gate_connection_returned")
        with self.guard.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('disconnect_dispatch_connections', '0')"
            )

    async def wait_for_boundary(self, *, deadline: float, process: Any) -> dict[str, int | None]:
        """Wait for server-side registry removal, which can follow client.close()."""
        async with asyncio.timeout_at(deadline):
            while True:
                boundary = self.evidence()
                if all(boundary[key] is not None for key in (
                    "roots_at_disconnect", "connections_at_disconnect", "calls_at_disconnect",
                )):
                    return boundary
                if process is None or process.poll() is not None:
                    raise CaseFailureError("gateway_exited_before_disconnect_boundary")
                # Like the existing disconnected Goal observer, read only the
                # isolated durable evidence until its condition or case deadline.
                await asyncio.sleep(0.15)

    def evidence(self) -> dict[str, int | None]:
        with self.guard.connect() as db:
            settings = dict(db.execute(
                "SELECT name,value FROM settings WHERE name IN "
                "('disconnect_boundary_roots', 'disconnect_boundary_connections', "
                "'disconnect_boundary_calls', 'disconnect_dispatch_connections', "
                "'automatic_dispatch_without_connections')"
            ))
        return {key: int(value) if value is not None else None for key, value in {
            "roots_at_disconnect": settings.get("disconnect_boundary_roots"),
            "connections_at_disconnect": settings.get("disconnect_boundary_connections"),
            "connections_at_dispatch": settings.get("disconnect_dispatch_connections"),
            "calls_at_disconnect": settings.get("disconnect_boundary_calls"),
            "automatic_dispatch_without_connections": settings.get(
                "automatic_dispatch_without_connections"
            ),
        }.items()}


def install_dispatch_guard(guard: DispatchGuard) -> DisconnectDispatchGate:
    """Safety instrumentation only; preserve real provider and real task execution."""
    import httpx

    from opensquilla.engine.usage_accounting import current_usage_accounting_scope
    from opensquilla.gateway.task_runtime import TaskRuntime
    from opensquilla.gateway.websocket import ConnectionRegistry, get_registry
    from opensquilla.observability.turn_call_log import TurnCallLogger

    original_http = httpx.AsyncHTTPTransport.handle_async_request
    original_sync = httpx.HTTPTransport.handle_request
    original_running = TaskRuntime._mark_running
    original_log = TurnCallLogger.write
    original_unregister = ConnectionRegistry.unregister
    disconnect_gate = DisconnectDispatchGate(guard)

    def observed_unregister(registry: Any, conn_id: str) -> None:
        present = registry.get(conn_id) is not None
        original_unregister(registry, conn_id)
        if present and registry is get_registry():
            disconnect_gate.after_unregister(registry)

    class DeadlineStream(httpx.AsyncByteStream):
        def __init__(self, stream: Any, deadline: float, index: int) -> None:
            self.stream, self.deadline, self.index = stream, deadline, index

        async def __aiter__(self) -> Any:
            try:
                async with asyncio.timeout_at(self.deadline):
                    async for chunk in self.stream:
                        yield chunk
            except (httpx.TransportError, TimeoutError):
                guard.finish(self.index, None, "transport")
                raise

        async def aclose(self) -> None:
            await self.stream.aclose()

    async def bounded_http(transport: Any, request: Any) -> Any:
        if not os.environ.get(get_provider_spec(guard.provider).env_key):
            raise DispatchLimitError("missing_provider_credential")
        body = await request.aread()
        if request.method == "POST":
            scope = current_usage_accounting_scope()
            await disconnect_gate.before_dispatch(
                scope.context.turn_id if scope is not None else None, get_registry(),
            )
        index = guard.request(request.method, str(request.url), body)
        deadline = asyncio.get_running_loop().time() + LIMITS["request_seconds"]
        try:
            async with asyncio.timeout_at(deadline):
                response = await original_http(transport, request)
        except (httpx.TransportError, TimeoutError):
            guard.finish(index, None, "transport")
            raise
        response.stream = DeadlineStream(response.stream, deadline, index)
        failure = ""
        if response.status_code >= 400:
            # Only a class is retained. Agent owns the actual retry and ledger
            # attempt; this guard rejects any retry outside the shared allowance.
            error_text = (await response.aread()).decode(errors="replace").lower()
            if response.status_code == 402 or (
                "quota" in error_text
                and any(word in error_text for word in ("exceeded", "exhausted", "insufficient"))
            ):
                failure = "balance"
            else:
                failure = classify_failure(error_text + f" HTTP {response.status_code}")
        guard.finish(index, response.status_code, failure)
        return response

    def bounded_sync(transport: Any, request: Any) -> Any:
        if not os.environ.get(get_provider_spec(guard.provider).env_key):
            raise DispatchLimitError("missing_provider_credential")
        # Supported provider inference is asynchronous. Fail closed if a future
        # adapter moves inference to a synchronous path without a total deadline.
        if request.method != "GET":
            raise DispatchLimitError("unreviewed_sync_inference")
        index = guard.request(request.method, str(request.url), request.read())
        response = original_sync(transport, request)
        guard.finish(index, response.status_code)
        return response

    async def bounded_running(runtime: Any, task: Any) -> bool:
        kind = "children" if task.run_kind == "subagent" else "root_turns"
        reserved = guard.claim(kind, LIMITS[kind], turn_id=task.task_id)
        running = await original_running(runtime, task)
        if running is False and reserved:
            guard.release_unstarted_turn(kind, task.task_id)
        return running

    def bounded_log(logger: Any, kind: str, payload: dict[str, Any]) -> Any:
        if kind == "llm_error":
            guard.logical_failure(str((payload.get("error") or {}).get("code") or "unknown"))
        return original_log(logger, kind, payload)

    httpx.AsyncHTTPTransport.handle_async_request = bounded_http
    httpx.HTTPTransport.handle_request = bounded_sync
    TaskRuntime._mark_running = bounded_running
    TurnCallLogger.write = bounded_log
    ConnectionRegistry.unregister = observed_unregister
    return disconnect_gate


def selected_model(provider: str, env: Mapping[str, str]) -> str:
    model = env.get(f"{provider.upper()}_MODEL", MODELS[provider]).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:/+-]{1,160}", model):
        raise ValueError("invalid model identifier")
    return model


def execution_source_fingerprint() -> dict[str, Any]:
    """Identify the actual checkout content without publishing local paths."""
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, timeout=10, env=minimal_child_environment(),
        )

    paths = sorted(set(git(
        "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--",
        "src", "migrations", "contracts", "scripts/live_plan_goal_runtime.py",
        "scripts/live_harness_security.py", "tests/integration/cli/tui_real_terminal",
        "scripts/smoke_v4_phase3_router.py",
    ).split(b"\0")) - {b""})
    digest = hashlib.sha256()
    for name in paths:
        path = ROOT / os.fsdecode(name)
        digest.update(name + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"deleted")
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "execution_source_sha256": digest.hexdigest(), "files": len(paths)}


def render_config(provider: str, model: str, workspace: Path, *, thinking: str = "off") -> str:
    if thinking not in {"off", "low", "medium", "high"}:
        raise ValueError("unsupported live thinking level")
    spec = get_provider_spec(provider)
    return "\n".join(
        [
            'host = "127.0.0.1"',
            "debug = false",
            f"workspace_dir = {json.dumps(str(workspace))}",
            "workspace_strict = true",
            "llm_request_timeout_seconds = 90",
            "agent_request_timeout_seconds = 90",
            "agent_runtime_timeout_seconds = 480",
            "agent_max_iterations = 16",
            "agent_max_provider_retries = 1",
            "[auth]",
            'mode = "none"',
            "[control_ui]",
            "enabled = false",
            "[rate_limit]",
            "enabled = false",
            "[privacy]",
            "disable_network_observability = true",
            "[naming]",
            "enabled = false",
            "[memory]",
            'source = "state"',
            "[tools]",
            'profile = "coding"',
            f"allow = {json.dumps(ALLOWED_TOOLS)}",
            "[task_runtime]",
            "max_concurrency = 1",
            "turn_hard_deadline_s = 540",
            "[goal]",
            "max_turns = 4",
            "runtime_budget_seconds = 540",
            "[sandbox]",
            'run_mode = "safe"',
            'network_default = "none"',
            "[llm]",
            f"provider = {json.dumps(provider)}",
            f"model = {json.dumps(model)}",
            f"api_key_env = {json.dumps(spec.env_key)}",
            f"base_url = {json.dumps(registry_endpoint(provider))}",
            "max_tokens = 4096",
            f"thinking = {json.dumps(thinking)}",
            "[squilla_router]",
            "enabled = false",
            "[llm_ensemble]",
            "enabled = false",
            "",
        ]
    )


def tool_calls(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        call
        for row in records
        if row.get("kind") == "llm_response"
        for call in (row.get("payload") or {}).get("tool_calls", [])
        if isinstance(call, dict)
    ]


def provider_evidence(
    records: Sequence[dict[str, Any]], provider: str, model: str
) -> dict[str, Any]:
    requests = [r for r in records if r.get("kind") == "llm_request"]
    responses = [r for r in records if r.get("kind") == "llm_response"]
    identities = {(str(r.get("provider") or ""), str(r.get("model") or "")) for r in requests}
    reported = {
        (
            str((r.get("payload") or {}).get("usage", {}).get("provider") or ""),
            str((r.get("payload") or {}).get("usage", {}).get("model") or ""),
        )
        for r in responses
    }
    return {
        "logged_requests": len(requests),
        "logged_responses": len(responses),
        "request_identity_matches": bool(identities) and identities == {(provider, model)},
        "response_identity_matches": bool(reported)
        and all(
            p == provider and provider_response_model_matches(provider, model, m)
            for p, m in reported
        ),
        # Never project arbitrary provider text as a trusted model identifier.
        "response_models": sorted(
            {
                m
                for p, m in reported
                if p == provider and provider_response_model_matches(provider, model, m)
            }
        ),
        "tools_observed": sorted(
            {c["name"] for c in tool_calls(records) if c.get("name") in ALLOWED_TOOLS}
        ),
    }


def usage_ledger_path(state: Path) -> Path | None:
    """Resolve the real CLI database layout and reject ambiguous evidence."""
    candidates = []
    for path in state.rglob("*.db"):
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='usage_events'").fetchone():
                candidates.append(path)
    if len(candidates) > 1:
        raise CaseFailureError("ambiguous_usage_ledger")
    return candidates[0] if candidates else None


def _diagnostic_code(value: Any) -> str | None:
    """Accept bounded machine identifiers, never error messages or paths."""
    if value is None:
        return None
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value)
        else "redacted"
    )


def runtime_diagnostics(
    state: Path,
    records: Sequence[dict[str, Any]],
    workspace: Path,
) -> dict[str, Any]:
    """Preserve execution evidence before removing private case state."""
    tasks = []
    path = usage_ledger_path(state)
    if path is not None:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_tasks'").fetchone()
            if exists:
                for status, mode, kind, reason, error_class, raw in db.execute(
                    "SELECT status,queue_mode,run_kind,terminal_reason,error_class,details "
                    "FROM agent_tasks ORDER BY created_at,rowid"
                ):
                    details = json.loads(raw) if raw else {}
                    outcome = details.get("turn_outcome") or {}
                    tasks.append(
                        {
                            "status": _diagnostic_code(status),
                            "queue_mode": _diagnostic_code(mode),
                            "is_child": kind == "subagent",
                            "terminal_reason": _diagnostic_code(reason),
                            "error_class": _diagnostic_code(error_class),
                            "outcome_code": _diagnostic_code(outcome.get("code")),
                            "failure_kind": _diagnostic_code(outcome.get("failure_kind")),
                        }
                    )
    requests = []
    results = []
    goal_control_calls = []
    turn_indices: dict[str, int] = {}
    path_hits = []
    for record in records:
        payload = record.get("payload") or {}
        if record.get("kind") == "llm_request":
            names = {
                tool.get("name") or (tool.get("function") or {}).get("name")
                for tool in payload.get("tools") or []
                if isinstance(tool, dict)
            }
            requests.append(
                {
                    "tools": sorted(names.intersection(ALLOWED_TOOLS)),
                    "other_tool_count": len(names.difference(ALLOWED_TOOLS)),
                    "workspace_context_present": json.dumps(str(workspace))[1:-1]
                    in json.dumps(
                        {
                            "messages": payload.get("messages"),
                            "system": (payload.get("config") or {}).get("system"),
                        }
                    ),
                }
            )
        if record.get("kind") == "tool_request" and payload.get("name") in {
            "create_goal", "update_goal", "get_goal",
        }:
            args = payload.get("arguments") or {}
            if isinstance(args, dict):
                turn = str(record.get("turn_id") or "")
                index = turn_indices.setdefault(turn, len(turn_indices) + 1)
                allowed = {"objective", "token_budget", "status", "reason"}
                status = args.get("status")
                goal_control_calls.append({
                    "turn_index": index, "tool": payload["name"],
                    "argument_keys": sorted(set(args).intersection(allowed)),
                    "other_argument_count": len(set(args).difference(allowed)),
                    "status": status if isinstance(status, str) and status in {
                        "active", "paused", "complete", "blocked"}
                    else "invalid",
                    "objective_present": "objective" in args,
                    "token_budget_present": "token_budget" in args,
                    "reason_present": "reason" in args,
                })
        if record.get("kind") != "tool_response" or payload.get("name") not in ALLOWED_TOOLS:
            continue
        raw = payload.get("result") or ""
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        parsed = parsed if isinstance(parsed, dict) else {}
        execution = parsed.get("execution_status") or {}
        execution = execution if isinstance(execution, dict) else {}
        error = parsed.get("error") or {}
        error = error if isinstance(error, dict) else {}
        exit_match = re.search(r"^exit_code=(-?\d+)\n", raw) if isinstance(raw, str) else None
        results.append(
            {
                "tool": payload["name"],
                "is_error": bool(payload.get("is_error")),
                "status": _diagnostic_code(execution.get("status") or parsed.get("status")),
                "reason": _diagnostic_code(execution.get("reason") or parsed.get("reason")),
                "error_class": _diagnostic_code(parsed.get("error_class") or error.get("class")),
                "code": _diagnostic_code(parsed.get("code") or error.get("code")),
                "exit_code": int(exit_match[1]) if exit_match else None,
            }
        )
    for call in tool_calls(records):
        arguments = call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        if not isinstance(arguments, dict):
            continue
        raw_path = arguments.get("path")
        if call.get("name") in {"read_file", "write_file", "list_dir"} and isinstance(
            raw_path, str
        ):
            candidate = Path(raw_path)
            candidate = candidate if candidate.is_absolute() else workspace / candidate
            path_hits.append(candidate.resolve().is_relative_to(workspace.resolve()))
    return {
        "tasks": tasks,
        "requests": requests,
        "tool_results": results,
        "goal_control_calls": goal_control_calls,
        "file_tool_workspace_hits": path_hits,
    }


def ledger_projection(state: Path) -> dict[str, Any]:
    """Read accounting only; never return session identities or transcript text."""
    path = usage_ledger_path(state)
    if path is None:
        return {"rows": []}
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT status,COUNT(*),SUM(input_tokens),SUM(output_tokens),"
            "SUM(reasoning_tokens),SUM(total_tokens) FROM usage_events GROUP BY status"
        ).fetchall()
    return {
        "rows": [
            {
                "status": r[0],
                "calls": r[1],
                "input": r[2],
                "output": r[3],
                "reasoning": r[4],
                "total": r[5],
            }
            for r in rows
        ]
    }


def child_usage_evidence(state: Path, goal_id: str) -> dict[str, Any]:
    """Compare the Goal total to physical root/child rows; project no identities."""
    path = usage_ledger_path(state)
    if path is None:
        raise CaseFailureError("missing_usage_ledger")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute(
            "SELECT turn_id,root_turn_id,parent_turn_id,status,input_tokens,output_tokens,"
            "cache_read_tokens,total_tokens,goal_id FROM usage_events WHERE goal_id=?",
            (goal_id,),
        ).fetchall()
        parent_calls = child_calls = parent_budget = child_budget = total = 0
        links_valid = bool(rows)
        finalized = True
        for (
            turn,
            root,
            parent,
            status,
            input_tokens,
            output_tokens,
            cache_read,
            tokens,
            goal,
        ) in rows:
            budget = max(0, input_tokens - cache_read) + output_tokens
            finalized &= status == "finalized"
            links_valid &= bool(root and goal == goal_id)
            total += tokens
            if turn == root:
                parent_calls += 1
                parent_budget += budget
            else:
                child_calls += 1
                child_budget += budget
                links_valid &= bool(parent)
        stored = db.execute(
            "SELECT budget_tokens_used,total_tokens FROM session_goals WHERE goal_id=?", (goal_id,)
        ).fetchone()
    return {
        "root_calls": parent_calls,
        "child_calls": child_calls,
        "root_budget_tokens": parent_budget,
        "child_budget_tokens": child_budget,
        "physical_budget_tokens": parent_budget + child_budget,
        "goal_budget_tokens": int(stored[0]) if stored else None,
        "physical_total_tokens": total,
        "goal_total_tokens": int(stored[1]) if stored else None,
        "lineage_present": links_valid,
        "all_calls_finalized": finalized,
    }


def isolated_user_environment(root: Path) -> dict[str, str]:
    """Give ordinary home lookups a private cross-platform location."""
    home = root / "user-home"
    roaming, local = home / "AppData" / "Roaming", home / "AppData" / "Local"
    for path in (home, roaming, local):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        # ntpath.expanduser deliberately ignores HOME; omitting USERPROFILE
        # makes Path.home() fail in a Windows child with our minimal environment.
        "USERPROFILE": str(home),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def gateway_startup_diagnostics(root: Path, exit_code: int | None) -> dict[str, Any]:
    """Retain bounded failure categories, never raw startup log messages."""
    exceptions: set[str] = set()
    for name in ("stdout", "stderr"):
        path = root / f"gateway.{name}.log"
        if path.is_file():
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 65536))
                tail = stream.read().decode("utf-8", errors="replace")
            exceptions.update(re.findall(
                r"(?m)^([A-Za-z][A-Za-z0-9_]{0,70}(?:Error|Exception)):", tail,
            ))
    return {
        "phase": "early_exit" if exit_code is not None else "health_timeout",
        "exit_code": exit_code,
        "exception_types": sorted(exceptions),
    }


class LiveCase:
    def __init__(
        self, root: Path, provider: str, model: str, secrets: Mapping[str, str],
        *, thinking: str = "off",
    ) -> None:
        self.root, self.provider, self.model, self.secrets = root, provider, model, secrets
        self.deadline = time.monotonic() + LIMITS["case_seconds"]
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "AGENTS.md").write_text(
            "# Repository\n\n"
            "This is a configured, isolated Python test repository.\n"
            "Work on the files relevant to the user's requested change. "
            "Identity, user profiles, and long-term memory are outside this task's scope.\n"
            "Run Python checks with python3.\n",
            encoding="utf-8",
        )
        for name in ("state", "turn-calls", "user-state"):
            (root / name).mkdir()
        self.port = _free_port()
        self.process: subprocess.Popen[Any] | None = None
        self.client: GatewayRPCClient | None = None
        self.terminal: Any | None = None
        self.subscriptions: set[str] = set()
        self.logs: list[Any] = []
        self.assertions: dict[str, bool] = {}
        self.evidence: dict[str, Any] = {}
        self.guard = DispatchGuard(root / "dispatch.sqlite", provider=provider, model=model)
        (root / "config.toml").write_text(
            render_config(provider, model, self.workspace, thinking=thinking)
        )

    def check(self, name: str, condition: Any) -> None:
        self.assertions[name] = bool(condition)
        if not condition:
            raise CaseFailureError(name)

    async def start(self) -> None:
        env = child_environment(self.provider, self.secrets)
        env.update(isolated_user_environment(self.root))
        env.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(self.root / "config.toml"),
                "OPENSQUILLA_STATE_DIR": str(self.root / "state"),
                "OPENSQUILLA_USER_STATE_DIR": str(self.root / "user-state"),
                "OPENSQUILLA_HOME": str(self.root / "profiles"),
                "OPENSQUILLA_TEST_PROFILE_LOCK_ROOT": "1",
                "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
                "OPENSQUILLA_TURN_CALL_LOG": "1",
                "OPENSQUILLA_TURN_CALL_LOG_DIR": str(self.root / "turn-calls"),
            }
        )
        self.logs = [(self.root / f"gateway.{name}.log").open("a") for name in ("stdout", "stderr")]
        self.process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--_gateway",
                "--provider",
                self.provider,
                "--model",
                self.model,
                "--guard",
                str(self.guard.path),
                "--port",
                str(self.port),
            ],
            cwd=self.root,
            env=env,
            stdout=self.logs[0],
            stderr=self.logs[1],
            shell=False,
        )
        _health, error = await asyncio.to_thread(_wait_for_gateway_health, self.process, self.port)
        if error:
            self.evidence["gateway_startup"] = gateway_startup_diagnostics(
                self.root, self.process.poll(),
            )
        self.check("gateway_healthy", not error)
        self.check("usage_ledger_resolved", usage_ledger_path(self.root / "state") is not None)
        await self.reconnect()

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        if client:
            await client.close()

    async def reconnect(self) -> None:
        await self.disconnect()
        self.client = GatewayRPCClient(scopes=["operator.admin"])
        await self.client.connect(f"ws://127.0.0.1:{self.port}/ws")
        for key in self.subscriptions:
            await self.client.call("sessions.messages.subscribe", {"key": key})

    async def rpc(self, method: str, **params: Any) -> dict[str, Any]:
        assert self.client is not None
        return await self.client.call(method, params)

    async def snapshot(self, key: str) -> dict[str, Any]:
        return await self.rpc("sessions.messages.hydrate", key=key)

    async def until(
        self,
        key: str,
        predicate: Callable[[dict[str, Any]], Any],
    ) -> dict[str, Any]:
        async with asyncio.timeout_at(self.deadline):
            while True:
                snapshot = await self.snapshot(key)
                if predicate(snapshot):
                    return snapshot
                if self.process is None or self.process.poll() is not None:
                    raise CaseFailureError("gateway_exited")
                await asyncio.sleep(0.15)

    async def subscribe(self, key: str) -> None:
        await self.rpc("sessions.messages.subscribe", key=key)
        self.subscriptions.add(key)

    async def send(self, key: str, prompt: str) -> str:
        await self.subscribe(key)
        result = await self.rpc(
            "chat.send",
            sessionKey=key,
            message=prompt,
            clientRequestId=str(uuid.uuid4()),
            intent="continue",
            queueMode="followup",
        )
        return str(result.get("turn_id") or result.get("task_id") or result.get("taskId") or "")

    async def done(self, key: str, task: str) -> dict[str, Any]:
        snapshot = await self.until(
            key,
            lambda s: any(
                t.get("task_id") == task and t.get("status") in TERMINAL for t in s.get("tasks", [])
            ),
        )
        row = next(t for t in snapshot["tasks"] if t.get("task_id") == task)
        self.check("turn_completed", row.get("status") == "succeeded")
        return snapshot

    async def pending(self, key: str, task: str | None = None) -> dict[str, Any]:
        def resolved(snapshot: dict[str, Any]) -> bool:
            if snapshot.get("pendingUserInputs"):
                return True
            tasks = snapshot.get("tasks", [])
            if task is not None:
                return any(row.get("task_id") == task and row.get("status") in TERMINAL
                           for row in tasks)
            return bool(tasks) and all(row.get("status") in TERMINAL for row in tasks)

        snapshot = await self.until(key, resolved)
        self.check("questionnaire_requested", bool(snapshot.get("pendingUserInputs")))
        return snapshot["pendingUserInputs"][0]

    async def answer(self, key: str, request: dict[str, Any]) -> dict[str, Any]:
        fields = (request.get("clarify_schema") or {}).get("fields", [])
        answers = {f["name"]: "Blue" for f in fields}
        if not answers:
            answers = {q["id"]: "Blue" for q in request.get("questions", [])}
        return await self.rpc(
            "chat.clarify_submit", sessionKey=key, request_id=request["request_id"], fields=answers
        )

    def records(self) -> list[dict[str, Any]]:
        return _read_turn_call_records(self.root / "turn-calls")

    async def stop(self, *, crash: bool = False) -> dict[str, Any]:
        import httpx

        from opensquilla.gateway.boot import gateway_shutdown_deadline

        shutdown: dict[str, Any] = {"forced": False, "process_exited": True}
        try:
            remaining = max(0.0, self.deadline - time.monotonic())
            async with asyncio.timeout(min(2.0, remaining)):
                await self.disconnect()
        except Exception as exc:
            shutdown["connection_close_error"] = type(exc).__name__
        try:
            process = self.process
            if process is not None:
                remaining = max(0.0, self.deadline - time.monotonic())
                graceful_requested = False
                if not crash and remaining and process.poll() is None:
                    # The owner-only endpoint shares the CLI's drain path and
                    # works on Windows, where terminate() skips finalization.
                    try:
                        async with asyncio.timeout_at(self.deadline):
                            async with httpx.AsyncClient(trust_env=False) as client:
                                response = await client.post(
                                    f"http://127.0.0.1:{self.port}/api/system/shutdown",
                                    timeout=min(5.0, remaining),
                                )
                        graceful_requested = response.status_code == 202
                        if not graceful_requested:
                            shutdown["graceful_request_error"] = "not_accepted"
                    except Exception as exc:
                        shutdown["graceful_request_error"] = type(exc).__name__

                def stop_process() -> bool:
                    forced = False
                    if process.poll() is None:
                        remaining = max(0.0, self.deadline - time.monotonic())
                        if crash or remaining == 0:
                            process.kill()
                            forced = True
                        else:
                            if not graceful_requested:
                                process.terminate()
                            try:
                                # Use the production shutdown budget; the old
                                # 10s kill raced its normal 30s task drain.
                                process.wait(timeout=min(gateway_shutdown_deadline(), remaining))
                            except subprocess.TimeoutExpired:
                                process.kill()
                                forced = True
                        # Reaping a killed process grants no extra model runtime.
                        process.wait(timeout=5)
                    return forced

                shutdown["forced"] = await asyncio.to_thread(stop_process)
                shutdown["process_exited"] = process.poll() is not None
                self.process = None
        except Exception as exc:
            shutdown["process_stop_error"] = type(exc).__name__
            shutdown["process_exited"] = self.process is None or self.process.poll() is not None
        finally:
            if self.process is not None and self.process.poll() is None:
                # An unexpected drain/reap error must not leave inference alive
                # while we clean up the terminal or produce a failure report.
                shutdown["forced"] = True
                try:
                    self.process.kill()
                    await asyncio.to_thread(self.process.wait, timeout=5)
                except Exception as exc:
                    shutdown["process_kill_error"] = type(exc).__name__
                shutdown["process_exited"] = self.process.poll() is not None
            if self.terminal is not None:
                # Gateway is stopped before terminal cleanup can outlive the
                # inference deadline. The driver bounds every cleanup command.
                try:
                    await asyncio.to_thread(self.terminal.terminate)
                except Exception as exc:
                    shutdown["terminal_close_error"] = type(exc).__name__
                self.terminal = None
            for stream in self.logs:
                stream.close()
            self.logs = []
        return shutdown


QUESTION = (
    "Ask me to choose Blue or Green with request_user_input, question id color. "
    "Wait for my actual response; do not guess. After the answer, reply briefly."
)


def _python_fixture_arguments(
    arguments: Any, workspace: Path, filename: str,
) -> list[str] | None:
    """Parse one Python fixture command with optional exact-workspace cd."""
    if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
        return None
    command = arguments["command"]
    # The evidence reader does not evaluate expansions or multi-line programs.
    if any(char in command for char in ("$", "`", "\n", "\r")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        parts = list(lexer)
        cwd = Path(arguments.get("workdir") or workspace)
        cwd = cwd if cwd.is_absolute() else workspace / cwd
        if len(parts) >= 3 and parts[0] == "cd":
            if parts[2] != "&&":
                return None
            target = Path(parts[1])
            target = target if target.is_absolute() else cwd / target
            if target.resolve() != workspace.resolve():
                return None
            cwd = target
            parts = parts[3:]
        if not parts or not re.fullmatch(
            r"(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?", Path(parts[0]).name, flags=re.I
        ):
            return None
        rest = parts[1:]
        while rest and rest[0] in {"-3", "-u", "-B", "-E", "-s"}:
            rest = rest[1:]
        if not rest:
            return None
        script = Path(rest[0])
        script = script if script.is_absolute() else cwd / script
        if script.resolve() != (workspace / filename).resolve():
            return None
        return rest[1:]
    except (TypeError, ValueError, OSError):
        return None


def _direct_verification_command(arguments: Any, workspace: Path) -> bool:
    return _python_fixture_arguments(arguments, workspace, "verify.py") == []

def verification_evidence(
    records: Sequence[dict[str, Any]],
    turn_id: str,
    workspace: Path,
) -> dict[str, Any]:
    """Prove failure-before-edit and success-after-edit on one actual turn."""
    rows = [r for r in records if r.get("turn_id") == turn_id]
    sequences = [r.get("seq") for r in rows]
    valid = bool(rows) and all(isinstance(n, int) and n > 0 for n in sequences)
    valid = valid and len(set(sequences)) == len(sequences)
    if not valid:
        return {
            "valid_turn_sequence": False,
            "failure_before_first_write": False,
            "success_after_last_write": False,
            "direct_verification_calls": 0,
        }
    rows.sort(key=lambda r: r["seq"])
    requests = [r for r in rows if r.get("kind") == "tool_request"]
    responses: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("kind") == "tool_response":
            responses.setdefault(row["payload"].get("tool_use_id"), []).append(row)
    writes = [
        r
        for r in requests
        if r["payload"].get("name")
        in {
            "write_file",
            "edit_file",
            "apply_patch",
        }
    ]
    write_done = []
    for request in writes:
        matches = responses.get(request["payload"].get("tool_use_id"), [])
        if len(matches) == 1 and matches[0]["seq"] > request["seq"]:
            write_done.append(matches[0]["seq"])
    first_write = min((r["seq"] for r in writes), default=None)
    last_write_done = max(write_done, default=None)
    samples = []
    opaque_exec_before_write = False
    non_direct_verifier = 0
    suppressed_exit = False
    zero_exit_assertion = False
    for request in requests:
        payload = request["payload"]
        if payload.get("name") != "exec_command":
            continue
        arguments = payload.get("arguments") or {}
        direct = _direct_verification_command(arguments, workspace)
        command = arguments.get("command", "") if isinstance(arguments, dict) else ""
        command = command if isinstance(command, str) else ""
        non_direct_verifier += int(not direct and "verify.py" in command)
        suppressed_exit |= "verify.py" in command and bool(re.search(r"\|\||\|\s|;", command))
        opaque_exec_before_write |= not direct and (
            first_write is None or request["seq"] < first_write
        )
        matches = responses.get(payload.get("tool_use_id"), [])
        if len(matches) != 1 or matches[0]["seq"] <= request["seq"]:
            continue
        response = matches[0]
        result = response["payload"].get("result", "")
        if not isinstance(result, str):
            continue
        match = re.match(r"exit_code=(-?\d+)\r?\n", result)
        exit_code = int(match[1]) if match else None
        assertion = "Traceback (most recent call last)" in result and bool(
            re.search(r"(?m)^AssertionError(?::|\s*$)", result)
        )
        zero_exit_assertion |= exit_code == 0 and assertion
        if direct:
            samples.append(
                {
                    "request_seq": request["seq"],
                    "response_seq": response["seq"],
                    "exit_code": exit_code,
                    "assertion_failure": assertion,
                    "pass_marker": bool(re.search(r"(?m)^PASS\r?$", result)),
                    "is_error": bool(response["payload"].get("is_error")),
                }
            )
    failed_before = (
        first_write is not None
        and not opaque_exec_before_write
        and any(
            s["exit_code"] is not None
            and s["exit_code"] != 0
            and s["assertion_failure"]
            and s["response_seq"] < first_write
            for s in samples
        )
    )
    success_after = (
        len(write_done) == len(writes)
        and last_write_done is not None
        and any(
            s["request_seq"] > last_write_done
            and s["exit_code"] == 0
            and not s["is_error"]
            and s["pass_marker"]
            for s in samples
        )
    )
    return {
        "valid_turn_sequence": True,
        "direct_verification_calls": len(samples),
        "verification_exits": [s["exit_code"] for s in samples],
        "assertion_failure_observed": any(s["assertion_failure"] for s in samples),
        "zero_exit_assertion_observed": zero_exit_assertion,
        "non_direct_verifier_requests": non_direct_verifier,
        "suppressed_exit_syntax_seen": suppressed_exit,
        "opaque_exec_before_first_write": opaque_exec_before_write,
        "write_requests": len(writes),
        "failure_before_first_write": failed_before,
        "success_after_last_write": success_after,
    }


async def verify_final_artifact(workspace: Path, verifier: bytes) -> dict[str, Any]:
    """Run the unchanged synthetic verifier independently, without provider credentials."""
    unchanged = (workspace / "verify.py").read_bytes() == verifier
    if not unchanged:
        return {"fixture_unchanged": False, "ran": False, "passed": False}
    env = {
        "PATH": os.defpath,
        **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-s",
        "verify.py",
        cwd=workspace,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    stdout = stderr = b""
    try:
        async with asyncio.timeout(30):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        timed_out = True
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    unchanged_after = (workspace / "verify.py").is_file() and (
        workspace / "verify.py"
    ).read_bytes() == verifier
    passed = unchanged_after and process.returncode == 0 and b"PASS" in stdout.splitlines()
    return {
        "fixture_unchanged": unchanged_after,
        "ran": True,
        "passed": passed,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "assertion_failure": b"AssertionError" in stderr,
    }


async def plan_case(case: LiveCase) -> None:
    key = "agent:main:webchat:live-plan"
    source = case.workspace / "clamp.py"
    source.write_text("def clamp(value, low, high):\n    return min(value, high)\n")
    (case.workspace / "verify.py").write_text(
        "from clamp import clamp\nassert clamp(-2, 0, 10) == 0\n"
        "assert clamp(4, 0, 10) == 4\nassert clamp(20, 0, 10) == 10\n"
    )
    original = source.read_bytes()
    await case.rpc("plans.setMode", sessionKey=key, mode="plan", expectedRevision=0)
    task = await case.send(
        key,
        "Investigate clamp.py and verify.py in the workspace with tools. "
        "Prepare a small repair plan and submit it using submit_plan. "
        "Do not implement until I approve. Do not ask questions.",
    )
    snapshot = await case.done(key, task)
    revision = snapshot.get("currentPlan") or {}
    revision_id = revision.get("revisionId")
    case.check("investigation_submitted", revision_id and source.read_bytes() == original)
    case.check(
        "investigation_used_tools",
        any(c["name"] == "read_file" for c in tool_calls(case.records())),
    )
    hidden = await case.rpc(
        "plans.setPresentation",
        sessionKey=key,
        revisionId=revision_id,
        dismissed=True,
        expectedEpoch=snapshot["epoch"],
        expectedPresentationRevision=0,
        clientRequestId=str(uuid.uuid4()),
    )
    await case.reconnect()
    refreshed = await case.snapshot(key)
    case.check(
        "dismissal_survives_reconnect",
        any(
            p["revisionId"] == revision_id and p["dismissed"]
            for p in refreshed["planPresentations"]
        ),
    )
    await case.rpc(
        "plans.setPresentation",
        sessionKey=key,
        revisionId=revision_id,
        dismissed=False,
        expectedEpoch=snapshot["epoch"],
        expectedPresentationRevision=hidden["planPresentations"][0]["stateRevision"],
        clientRequestId=str(uuid.uuid4()),
    )
    # A newly supplied requirement must be integrated into dynamic progress.
    with (case.workspace / "verify.py").open("a") as stream:
        stream.write(
            "try:\n    clamp(1, 3, 2)\nexcept ValueError:\n    pass\nelse:\n"
            "    raise AssertionError('inverted bounds must raise ValueError')\nprint('PASS')\n"
        )
    verifier = (case.workspace / "verify.py").read_bytes()
    params = {
        "sessionKey": key,
        "planRevisionId": revision_id,
        "clientRequestId": str(uuid.uuid4()),
        "intent": "continue",
        "message": "Implement the approved repair. First execute python3 verify.py directly "
        "(or the platform equivalent Python 3 executable) and observe its nonzero exit. "
        "Do not mask the exit status with shell operators, pipes, redirection "
        "or exception handling. "
        "Use file tools for inspection and write_file, edit_file or apply_patch for edits. "
        "Wait for the failing verification result before the first edit. "
        "The test now also requires ValueError for inverted bounds. Use update_plan to "
        "add a new repair item named 'Handle inverted bounds', adapt progress as work changes, "
        "repair clamp.py, "
        "then execute python3 verify.py directly again without modifying verify.py. "
        "Mark verified progress completed. "
        "Finish only on success.",
    }
    accepted = await case.rpc("plans.implement", **params)
    replay = await case.rpc("plans.implement", **params)
    case.check(
        "implement_idempotent",
        replay.get("replayed") and accepted.get("turn_id") == replay.get("turn_id"),
    )
    completed = await case.done(key, str(accepted["turn_id"]))
    records = case.records()
    proof = verification_evidence(records, str(accepted["turn_id"]), case.workspace)
    external = await verify_final_artifact(case.workspace, verifier)
    case.evidence["verification"] = proof
    case.evidence["final_artifact"] = external
    calls = tool_calls([r for r in records if r.get("turn_id") == accepted["turn_id"]])
    updates = [c for c in calls if c["name"] == "update_plan"]
    progress = next(t for t in completed["tasks"] if t["task_id"] == accepted["turn_id"]).get(
        "progress", {}
    )
    case.check(
        "dynamic_progress_persisted",
        progress.get("revision", 0) >= 2
        and any("inverted bounds" in s["step"].lower() for s in progress.get("steps", []))
        and all(s["status"] == "completed" for s in progress.get("steps", [])),
    )
    case.check("verification_fixture_unchanged", external["fixture_unchanged"])
    case.check("final_artifact_independently_verified", external["passed"])
    case.check("repaired_output_verified", proof["success_after_last_write"])
    case.check("failure_observed_before_repair", proof["failure_before_first_write"])
    case.check(
        "dynamic_progress_updated",
        len(updates) >= 2 and updates[0].get("arguments") != updates[-1].get("arguments"),
    )
    case.check("agent_ran_verification", proof["direct_verification_calls"] >= 2)


# Each action is deliberately non-idempotent: the acceptance oracle must detect
# accidental replay, rather than have the synthetic workload hide it.
EFFECT_FIXTURE = """from pathlib import Path
import os
import sys

root = Path(__file__).resolve().parent
receipt = root / "effect-receipt.txt"
action = sys.argv[1]
if action == "record":
    with receipt.open("a", encoding="utf-8", newline="\\n") as stream:
        stream.write("EFFECT\\n")
        stream.flush()
        os.fsync(stream.fileno())
    (root / "effect-recorded.txt").write_text("RECORDED\\n", encoding="utf-8")
    print("RECORDED")
elif action == "finish":
    assert receipt.read_text(encoding="utf-8").splitlines() == ["EFFECT"]
    with (root / "effect-finished.txt").open("a", encoding="utf-8", newline="\\n") as stream:
        stream.write("FINISHED\\n")
    print("FINISHED")
else:
    raise ValueError("unknown synthetic action")
"""


def _effect_command(arguments: Any, workspace: Path) -> str | None:
    """Only one Python fixture action can prove that the side effect ran."""
    rest = _python_fixture_arguments(arguments, workspace, "effect_fixture.py")
    if rest is None or len(rest) != 1 or rest[0] not in {"record", "finish"}:
        return None
    return rest[0]

def effect_evidence(
    records: Sequence[dict[str, Any]],
    workspace: Path,
    fixture: bytes,
    record_turn: str,
    finish_turn: str | None = None,
) -> dict[str, Any]:
    """Count real requests as well as receipts; never make the fixture deduplicate."""
    requests = [r for r in records if r.get("kind") == "tool_request"]
    actions: dict[str, list[dict[str, Any]]] = {"record": [], "finish": []}
    opaque_by_phase = {"original": 0, "recovery": 0, "other": 0}
    for row in requests:
        payload = row.get("payload") or {}
        if payload.get("name") != "exec_command":
            continue
        action = _effect_command(payload.get("arguments"), workspace)
        if action is None:
            phase = ("original" if row.get("turn_id") == record_turn else
                     "recovery" if finish_turn and row.get("turn_id") == finish_turn else "other")
            opaque_by_phase[phase] += 1
        else:
            actions[action].append(row)

    def succeeded(action: str, expected_turn: str | None) -> bool:
        if expected_turn is None or len(actions[action]) != 1:
            return False
        request = actions[action][0]
        if request.get("turn_id") != expected_turn or not isinstance(request.get("seq"), int):
            return False
        matches = [
            r
            for r in records
            if r.get("kind") == "tool_response"
            and r.get("turn_id") == expected_turn
            and (r.get("payload") or {}).get("tool_use_id") == request["payload"].get("tool_use_id")
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("seq"), int):
            return False
        response = matches[0]
        payload = response.get("payload") or {}
        result = payload.get("result")
        marker = "RECORDED" if action == "record" else "FINISHED"
        return bool(
            response["seq"] > request["seq"]
            and not payload.get("is_error")
            and isinstance(result, str)
            and re.match(r"exit_code=0\r?\n", result)
            and marker in result.splitlines()
        )

    def lines(name: str) -> list[str]:
        path = workspace / name
        return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []

    fixture_path = workspace / "effect_fixture.py"
    return {
        "fixture_unchanged": fixture_path.is_file() and fixture_path.read_bytes() == fixture,
        "receipt_exactly_once": lines("effect-receipt.txt") == ["EFFECT"],
        "record_marker_present": lines("effect-recorded.txt") == ["RECORDED"],
        "finish_marker_absent": not (workspace / "effect-finished.txt").exists(),
        "finish_exactly_once": lines("effect-finished.txt") == ["FINISHED"],
        "record_requests": len(actions["record"]),
        "finish_requests": len(actions["finish"]),
        "opaque_exec_requests": opaque_by_phase["original"] + opaque_by_phase["recovery"],
        "opaque_exec_outside_implementation": opaque_by_phase["other"],
        "opaque_exec_by_phase": opaque_by_phase,
        "record_succeeded_on_original_turn": succeeded("record", record_turn),
        "finish_succeeded_on_recovery_turn": succeeded("finish", finish_turn),
    }


async def _prepare_effect_plan(case: LiveCase, key: str) -> tuple[str, bytes]:
    fixture_path = case.workspace / "effect_fixture.py"
    fixture_path.write_text(EFFECT_FIXTURE, encoding="utf-8")
    fixture = fixture_path.read_bytes()
    await case.rpc("plans.setMode", sessionKey=key, mode="plan", expectedRevision=0)
    task = await case.send(
        key,
        "Read effect_fixture.py with read_file and submit_plan for this small synthetic task: "
        "after implementation is approved, run python3 effect_fixture.py record exactly once, "
        "then ask the user to choose Blue or Green using request_user_input and wait. "
        "Only after the user answers, run python3 effect_fixture.py finish. "
        "The record and finish actions append non-idempotent receipts. "
        "This turn is investigation only: do not run the fixture, edit files, or ask questions. "
        "Use submit_plan, then finish your planning response.",
    )
    snapshot = await case.done(key, task)
    revision_id = (snapshot.get("currentPlan") or {}).get("revisionId")
    case.check("effect_plan_submitted", bool(revision_id))
    case.check(
        "effect_investigation_preserved_fixture",
        fixture_path.read_bytes() == fixture
        and not (case.workspace / "effect-receipt.txt").exists()
        and not (case.workspace / "effect-finished.txt").exists(),
    )
    case.check(
        "effect_investigation_read_fixture",
        any(
            c["name"] == "read_file"
            for c in tool_calls([r for r in case.records() if r.get("turn_id") == task])
        ),
    )
    return str(revision_id), fixture


async def _hold_effect_implementation(
    case: LiveCase, key: str, revision: str, fixture: bytes
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    params = {
        "sessionKey": key,
        "planRevisionId": revision,
        "clientRequestId": str(uuid.uuid4()),
        "intent": "continue",
        "message": "Implement the approved synthetic task now. First execute exactly "
        "python3 effect_fixture.py record (or the platform equivalent Python 3 executable), "
        "optionally preceded only by cd to the exact workspace followed by &&. Do not "
        "add another command, redirection, or suppress exit status. After that tool succeeds, call "
        "request_user_input with question id color, choices Blue and Green. Wait for the "
        "actual answer before executing python3 effect_fixture.py finish. "
        "Do not edit the fixture or any receipt. Do not execute record more than once. "
        "The only exec_command calls allowed for this task are those direct record/finish "
        "commands, optionally with that exact-workspace cd prefix. If you inspect receipts, "
        "use read_file or list_dir; do not use shell "
        "commands for inspection or environment checks.",
    }
    accepted = await case.rpc("plans.implement", **params)
    request = await case.pending(key, str(accepted["turn_id"]))
    snapshot = await case.snapshot(key)
    run = snapshot.get("activePlanRun") or {}
    case.check(
        "implementation_running_at_effect_barrier",
        run.get("status") == "running"
        and run.get("runId") == accepted["planRun"]["runId"]
        and run.get("activeTaskId") == accepted["turn_id"]
        and any(
            t.get("task_id") == accepted["turn_id"] and t.get("status") == "running"
            for t in snapshot.get("tasks", [])
        ),
    )
    proof = effect_evidence(case.records(), case.workspace, fixture, str(accepted["turn_id"]))
    case.evidence["effect_before_interrupt"] = proof
    case.check(
        "side_effect_recorded_once_before_interrupt",
        proof["fixture_unchanged"]
        and proof["receipt_exactly_once"]
        and proof["record_marker_present"]
        and proof["record_succeeded_on_original_turn"],
    )
    case.check("effect_finish_not_started_before_interrupt",
               proof["finish_marker_absent"] and proof["finish_requests"] == 0)
    case.check("effect_no_opaque_exec_before_interrupt", proof["opaque_exec_requests"] == 0)
    return params, accepted, request


async def _reject_stale_effect_answer(case: LiveCase, key: str, request: dict[str, Any]) -> None:
    rejected = False
    try:
        await case.answer(key, request)
    except GatewayRPCError as exc:
        rejected = exc.code == "USER_INPUT_EXPIRED"
    case.check("stale_implementation_answer_rejected", rejected)


async def plan_stop_case(case: LiveCase) -> None:
    """Stop an actual running implementation at a real user-input barrier."""
    key = "agent:main:webchat:live-plan-stop"
    revision, fixture = await _prepare_effect_plan(case, key)
    _params, accepted, request = await _hold_effect_implementation(case, key, revision, fixture)
    run = (await case.snapshot(key))["activePlanRun"]
    stopped = await case.rpc(
        "plans.cancelRun",
        sessionKey=key,
        runId=run["runId"],
        expectedStateRevision=run["stateRevision"],
    )
    case.check(
        "implementation_stop_acknowledged",
        stopped["planRun"]["runId"] == run["runId"]
        and stopped["planRun"]["status"] == "cancelled"
        and stopped["planRun"].get("activeTaskId") is None,
    )
    await case.reconnect()
    snapshot = await case.until(
        key,
        lambda s: any(
            t.get("task_id") == accepted["turn_id"] and t.get("status") == "cancelled"
            for t in s.get("tasks", [])
        ),
    )
    case.check(
        "implementation_stop_survives_reconnect",
        not snapshot.get("activePlanRun") and not snapshot.get("pendingUserInputs"),
    )
    repeated = await case.rpc(
        "plans.cancelRun",
        sessionKey=key,
        runId=run["runId"],
        expectedStateRevision=stopped["planRun"]["stateRevision"],
    )
    case.check("implementation_stop_repeat_is_idempotent", repeated == stopped)
    await _reject_stale_effect_answer(case, key, request)
    followup = await case.send(
        key,
        "Read effect-receipt.txt and report its one EFFECT line. The previous implementation "
        "was stopped. Do not run commands, edit files, ask questions or resume that task.",
    )
    await case.done(key, followup)
    case.check("same_session_accepts_turn_after_plan_stop", followup != accepted["turn_id"])
    proof = effect_evidence(case.records(), case.workspace, fixture, str(accepted["turn_id"]))
    case.evidence["effect_after_stop"] = proof
    case.check(
        "plan_stop_never_replays_or_continues_effect",
        proof["fixture_unchanged"]
        and proof["receipt_exactly_once"]
        and proof["record_succeeded_on_original_turn"]
        and proof["finish_marker_absent"]
        and proof["finish_requests"] == proof["opaque_exec_requests"] == 0,
    )


# The child waits on a file barrier, bounded even if the Gateway is killed.
# It intentionally writes FINISHED after release so an orphaned process is visible.
CHILD_STOP_FIXTURE = """from pathlib import Path
import os
import sys
import time

root = Path(__file__).resolve().parent
maximum = float(sys.argv[1]) if len(sys.argv) == 2 else 180.0
assert 0 < maximum <= 180
with (root / "child-started.txt").open("a", encoding="utf-8") as stream:
    stream.write("STARTED\\n")
    stream.flush()
    os.fsync(stream.fileno())
print("STARTED", flush=True)
deadline = time.monotonic() + maximum
while not (root / "child-release.txt").is_file():
    if time.monotonic() >= deadline:
        (root / "child-timed-out.txt").write_text("TIMEOUT\\n", encoding="utf-8")
        raise SystemExit(3)
    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
with (root / "child-finished.txt").open("a", encoding="utf-8") as stream:
    stream.write("FINISHED\\n")
print("FINISHED", flush=True)
"""


def _plan_stop_child_binding(records: Sequence[dict[str, Any]], parent: str) -> dict[str, str]:
    requests = [r for r in records if r.get("turn_id") == parent
                and r.get("kind") == "tool_request"
                and (r.get("payload") or {}).get("name") == "sessions_spawn"]
    if len(requests) != 1:
        raise CaseFailureError("plan_stop_child_exactly_one_spawn")
    request = requests[0]
    replies = [r for r in records if r.get("turn_id") == parent
               and r.get("kind") == "tool_response"
               and (r.get("payload") or {}).get("tool_use_id")
               == request["payload"].get("tool_use_id")]
    if len(replies) != 1 or replies[0]["payload"].get("is_error"):
        raise CaseFailureError("plan_stop_child_spawn_receipt")
    if not (isinstance(request.get("seq"), int) and isinstance(replies[0].get("seq"), int)
            and request["seq"] < replies[0]["seq"]):
        raise CaseFailureError("plan_stop_child_spawn_sequence")
    try:
        child = json.loads(replies[0]["payload"]["result"])
        if not all(isinstance(child.get(k), str) and child[k] for k in ("session_key", "task_id")):
            raise ValueError("invalid child identity")
    except (ValueError, TypeError, KeyError) as exc:
        raise CaseFailureError("plan_stop_child_spawn_identity") from exc
    return {k: child[k] for k in ("session_key", "task_id")}


def _plan_stop_child_exec(arguments: Any, workspace: Path) -> bool:
    if not isinstance(arguments, dict) or not isinstance(arguments.get("command"), str):
        return False
    if any(marker in arguments["command"] for marker in ("$", "`", "\n", "\r")):
        return False
    try:
        lexer = shlex.shlex(arguments["command"], posix=True, punctuation_chars=True)
        lexer.whitespace_split, lexer.commenters = True, ""
        parts = list(lexer)
        cwd = Path(arguments.get("workdir") or workspace)
        cwd = cwd if cwd.is_absolute() else workspace / cwd
        if len(parts) >= 3 and parts[0] == "cd":
            target = Path(parts[1])
            target = target if target.is_absolute() else cwd / target
            if parts[2] != "&&" or target.resolve() != workspace.resolve():
                return False
            cwd, parts = target, parts[3:]
        if not parts or not re.fullmatch(
            r"(?:python(?:3(?:\.\d+)?)?|py)(?:\.exe)?", Path(parts[0]).name, flags=re.I,
        ):
            return False
        rest = parts[1:]
        while rest and rest[0] in {"-3", "-u", "-B", "-E", "-s"}:
            rest = rest[1:]
        if len(rest) not in {1, 2}:
            return False
        if len(rest) == 2:
            maximum = float(rest[1])
            if not math.isfinite(maximum) or not 0 < maximum <= 180:
                return False
        path = Path(rest[0])
        path = path if path.is_absolute() else cwd / path
        return path.resolve() == (workspace / "child_wait.py").resolve()
    except (TypeError, ValueError, OSError):
        return False


def plan_stop_child_evidence(
    case: LiveCase, key: str, child_key: str, parent: str, child: str,
    expected_parent_turns: set[str], fixture: bytes,
) -> dict[str, Any]:
    """Project exact lineage and absence of a completion wake, without identities."""
    path = usage_ledger_path(case.root / "state")
    if path is None:
        raise CaseFailureError("missing_usage_ledger")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT task_id,session_key,status,details FROM agent_tasks").fetchall()
        child_row = next((r for r in rows if r[0] == child and r[1] == child_key), None)
        metadata = ((json.loads(child_row[3] or "{}") if child_row else {}).get("metadata") or {})
        parent_ids = {r[0] for r in rows if r[1] == key}
        child_rows = [r for r in rows if r[1] == child_key]
        usage = dict(db.execute(
            "SELECT turn_id,COUNT(*) FROM usage_events WHERE turn_id IN (?,?) GROUP BY turn_id",
            (parent, child),
        ))
    child_exec = [r for r in case.records() if r.get("turn_id") == child
                  and r.get("kind") == "tool_request"
                  and (r.get("payload") or {}).get("name") == "exec_command"]
    started = case.workspace / "child-started.txt"
    return {
        "exact_parent_turns": parent_ids == expected_parent_turns,
        "exact_child_turn": len(child_rows) == 1 and child_rows[0][0] == child,
        "child_lineage_matches": metadata.get("parent_task_id") == parent
        and metadata.get("parent_session_key") == key,
        "child_cancelled": child_row is not None and child_row[2] == "cancelled",
        "parent_and_child_have_usage": usage.get(parent, 0) > 0 and usage.get(child, 0) > 0,
        "one_real_child_execution": len(child_exec) == 1
        and _plan_stop_child_exec(child_exec[0]["payload"].get("arguments"), case.workspace),
        "started_once": started.is_file() and started.read_text().splitlines() == ["STARTED"],
        "fixture_unchanged": (case.workspace / "child_wait.py").read_bytes() == fixture,
        "child_finish_absent": not (case.workspace / "child-finished.txt").exists(),
        "child_timeout_absent": not (case.workspace / "child-timed-out.txt").exists(),
    }


async def plan_stop_child_case(case: LiveCase) -> None:
    """Stop a running Plan task and its real child blocked on a bounded file barrier."""
    key = "agent:main:webchat:live-plan-stop-child"
    script = case.workspace / "child_wait.py"
    script.write_text(CHILD_STOP_FIXTURE, encoding="utf-8")
    fixture = script.read_bytes()
    await case.rpc("plans.setMode", sessionKey=key, mode="plan", expectedRevision=0)
    planning = await case.send(
        key, "Read child_wait.py and submit a plan for delegating this bounded synthetic task "
        "to one child while the parent asks me Blue or Green with request_user_input. "
        "The child will execute the existing script and wait for its file barrier. "
        "Do not implement, spawn, run commands, edit files, or ask questions during planning.",
    )
    planned = await case.done(key, planning)
    revision = (planned.get("currentPlan") or {}).get("revisionId")
    case.check("stop_child_plan_submitted", bool(revision) and script.read_bytes() == fixture
               and not (case.workspace / "child-started.txt").exists())
    accepted = await case.rpc(
        "plans.implement", sessionKey=key, planRevisionId=revision,
        clientRequestId=str(uuid.uuid4()), intent="continue",
        message=(
            "Implement the approved delegation now. Call sessions_spawn exactly once. "
            f"Tell the child to execute python3 {script} with exec_command, workdir "
            f"{case.workspace}, timeout 180; the script is bounded to 180 seconds. "
            "Tell it not to edit any files, spawn children, or ask questions, and to wait for "
            "that real command. After sessions_spawn returns, immediately call request_user_input "
            "with question id color, Blue and Green choices, and wait for my actual answer. "
            "Do not wait for the child first, do not call sessions_yield, do not execute the "
            "script yourself and do not write the release file or edit any fixture/marker."
        ),
    )
    parent = str(accepted["turn_id"])
    request = await case.pending(key, parent)
    child = _plan_stop_child_binding(case.records(), parent)
    await case.subscribe(child["session_key"])
    child_snapshot = await case.until(
        child["session_key"],
        lambda s: (case.workspace / "child-started.txt").is_file() or any(
            t.get("task_id") == child["task_id"] and t.get("status") in TERMINAL
            for t in s.get("tasks", [])
        ),
    )
    case.check("stop_child_running_at_barrier", any(
        t.get("task_id") == child["task_id"] and t.get("status") == "running"
        for t in child_snapshot.get("tasks", [])
    ))
    parent_snapshot = await case.snapshot(key)
    run = parent_snapshot.get("activePlanRun") or {}
    case.check("stop_child_parent_wait_released_single_slot", run.get("status") == "running"
               and run.get("activeTaskId") == parent and bool(parent_snapshot["pendingUserInputs"]))
    before = plan_stop_child_evidence(
        case, key, child["session_key"], parent, child["task_id"], {planning, parent}, fixture,
    )
    case.evidence["child_before_plan_stop"] = before
    case.check("stop_child_real_execution_before_stop", all(
        value for name, value in before.items() if name != "child_cancelled"
    ))
    stopped = await case.rpc(
        "plans.cancelRun", sessionKey=key, runId=run["runId"],
        expectedStateRevision=run["stateRevision"],
    )
    case.check("stop_child_plan_cancelled", stopped["planRun"]["status"] == "cancelled")
    for session_key, task_id in ((key, parent), (child["session_key"], child["task_id"])):
        ended = await case.until(session_key, lambda s: any(
            t.get("task_id") == task_id and t.get("status") in TERMINAL for t in s.get("tasks", [])
        ))
        case.check("stop_child_parent_cancelled" if task_id == parent else "stop_child_cancelled",
                   any(t.get("task_id") == task_id and t.get("status") == "cancelled"
                       for t in ended["tasks"]))
    # Release only after both task terminals. An orphaned shell child would now
    # write FINISHED; a later ordinary user turn also exercises wake suppression.
    (case.workspace / "child-release.txt").write_text("RELEASE\n", encoding="utf-8")
    await case.reconnect()
    await _reject_stale_effect_answer(case, key, request)
    followup = await case.send(
        key, "Read child-started.txt and report its one STARTED line. The earlier Plan and "
        "child were stopped. Do not run commands, edit files, delegate, ask questions, "
        "create a Goal or resume any earlier task.",
    )
    await case.done(key, followup)
    async with asyncio.timeout_at(case.deadline):
        observation_end = time.monotonic() + 1.0
        while time.monotonic() < observation_end:
            case.check("stop_child_no_orphan_finish", not (
                case.workspace / "child-finished.txt"
            ).exists())
            await asyncio.sleep(min(0.05, max(0.0, observation_end - time.monotonic())))
    after = plan_stop_child_evidence(
        case, key, child["session_key"], parent, child["task_id"],
        {planning, parent, followup}, fixture,
    )
    case.evidence["child_after_plan_stop"] = after
    case.check("stop_child_no_completion_wake_or_orphan", all(after.values()))
    final = await case.snapshot(key)
    case.check("stop_child_parent_idle", not final.get("active_task_group_ids")
               and not final.get("pendingUserInputs") and not final.get("activePlanRun"))
    counts = case.guard.snapshot()["counts"]
    case.check("stop_child_exact_dispatch_turns", counts.get("root_turns") == 3
               and counts.get("children") == 1)


async def plan_recovery_case(case: LiveCase) -> None:
    """Inject Gateway loss after a durable effect; explicitly resume a normal turn."""
    key = "agent:main:webchat:live-plan-recovery"
    revision, fixture = await _prepare_effect_plan(case, key)
    params, accepted, request = await _hold_effect_implementation(case, key, revision, fixture)
    run_id = accepted["planRun"]["runId"]
    counts = dict(case.guard.snapshot()["counts"])
    await case.stop(crash=True)
    await case.start()
    snapshot = await case.snapshot(key)
    run = snapshot.get("activePlanRun") or {}
    case.check(
        "restart_preserves_resumable_plan_in_same_session",
        run.get("runId") == run_id
        and run.get("planRevisionId") == revision
        and run.get("status") == "paused"
        and run.get("pauseReason") == "process_restart"
        and run.get("activeTaskId") is None
        and (snapshot.get("currentPlan") or {}).get("revisionId") == revision,
    )
    case.check(
        "restart_marks_old_implementation_abandoned",
        any(
            t.get("task_id") == accepted["turn_id"] and t.get("status") == "abandoned"
            for t in snapshot.get("tasks", [])
        )
        and not snapshot.get("pendingUserInputs"),
    )
    await _reject_stale_effect_answer(case, key, request)
    replay_old = await case.rpc("plans.implement", **params)
    case.check(
        "old_implementation_nonce_does_not_resume",
        replay_old.get("replayed")
        and replay_old.get("turn_id") == accepted["turn_id"]
        and case.guard.snapshot()["counts"] == counts,
    )
    # This is explicit ordinary Agent admission, not automatic step replay.
    resumed_params = {
        **params,
        "clientRequestId": str(uuid.uuid4()),
        "message": "Resume the interrupted implementation in this same session. Read "
        "effect-receipt.txt and effect-recorded.txt first: the record action already succeeded. "
        "Do not execute record again. I now answer Blue. Execute exactly python3 "
        "effect_fixture.py finish (or the platform equivalent Python 3 executable), without "
        "shell wrappers or suppressing exit status, and verify FINISHED. Do not edit the "
        "fixture or receipts and do not ask another question. Use read_file or list_dir for "
        "all observations; the only exec_command permitted is the direct finish command. "
        "Complete this ordinary turn.",
    }
    resumed = await case.rpc("plans.implement", **resumed_params)
    replay = await case.rpc("plans.implement", **resumed_params)
    case.check(
        "recovery_is_new_turn_on_original_run",
        resumed["turn_id"] != accepted["turn_id"]
        and resumed["planRun"]["runId"] == run_id
        and resumed["planRun"]["planRevisionId"] == revision,
    )
    case.check(
        "recovery_nonce_is_idempotent",
        replay.get("replayed") and replay.get("turn_id") == resumed["turn_id"],
    )
    completed = await case.done(key, str(resumed["turn_id"]))
    final_counts = dict(case.guard.snapshot()["counts"])
    final_replay = await case.rpc("plans.implement", **resumed_params)
    case.check(
        "recovered_plan_releases_execution_owner",
        not completed.get("activePlanRun")
        and final_replay.get("replayed")
        and final_replay.get("turn_id") == resumed["turn_id"]
        and final_replay["planRun"]["runId"] == run_id
        and final_replay["planRun"]["status"] == "completed"
        and final_replay["planRun"].get("activeTaskId") is None
        and case.guard.snapshot()["counts"] == final_counts,
    )
    proof = effect_evidence(
        case.records(), case.workspace, fixture, str(accepted["turn_id"]), str(resumed["turn_id"])
    )
    case.evidence["effect_after_recovery"] = proof
    case.evidence["recovery_fault"] = "gateway_process_kill_after_completed_tool"
    case.check(
        "recovery_completes_without_repeating_side_effect",
        proof["fixture_unchanged"]
        and proof["receipt_exactly_once"]
        and proof["record_marker_present"]
        and proof["record_succeeded_on_original_turn"]
        and proof["finish_succeeded_on_recovery_turn"]
        and proof["finish_exactly_once"]
        and proof["opaque_exec_requests"] == 0,
    )


async def wait_case(case: LiveCase, *, cancel: bool = False) -> None:
    a, b = "agent:main:webchat:live-wait-a", "agent:main:webchat:live-wait-b"
    if cancel:
        await case.rpc("plans.setMode", sessionKey=a, mode="plan", expectedRevision=0)
    first = await case.send(a, QUESTION)
    pending = await case.pending(a, first)
    queued = await case.send(a, "Reply SECOND only. Do not ask any questions.")
    other = await case.send(b, "Reply OTHER only. Do not call tools.")
    await case.done(b, other)
    snapshot = await case.snapshot(a)
    case.check("other_session_runs_while_waiting", bool(snapshot["pendingUserInputs"]))
    case.check(
        "same_session_remains_fenced",
        any(t.get("task_id") == queued and t.get("status") == "queued" for t in snapshot["tasks"]),
    )
    await case.reconnect()
    await case.rpc("sessions.messages.subscribe", key=a)
    restored = await case.pending(a, first)
    case.check("pending_survives_reconnect", restored["request_id"] == pending["request_id"])
    if cancel:
        await case.rpc("chat.abort", sessionKey=a, taskId=first, scope="task")
        await case.until(
            a,
            lambda s: (
                not s["pendingUserInputs"]
                and any(t["task_id"] == first and t["status"] == "cancelled" for t in s["tasks"])
            ),
        )
        case.check("cancel_removed_question", not (await case.snapshot(a))["pendingUserInputs"])
    else:
        await case.answer(a, restored)
        replay = await case.answer(a, restored)
        case.check("question_answer_idempotent", replay.get("replayed") is True)
        await case.done(a, first)
    await case.done(a, queued)


def durable_case_state(state: Path, key: str) -> dict[str, Any]:
    """Internal evidence only: never place task identities/details in the report."""
    path = usage_ledger_path(state)
    if path is None:
        raise CaseFailureError("missing_usage_ledger")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        tasks = [
            {"id": task, "status": status, "goal": (
                json.loads(raw or "{}").get(
                    "goal_effective_context", json.loads(raw or "{}").get("goal_context")
                ) or {}
            )}
            for task, status, raw in db.execute(
                "SELECT task_id,status,details FROM agent_tasks "
                "WHERE session_key=? ORDER BY created_at,rowid", (key,)
            )
        ]
        goal = db.execute(
            "SELECT goal_id,status,active_task_id,continuation_seq,background "
            "FROM session_goals WHERE session_key=?", (key,)
        ).fetchone()
        calls = dict(db.execute(
            "SELECT turn_id,COUNT(*) FROM usage_events WHERE status='finalized' "
            "AND turn_id IN (SELECT task_id FROM agent_tasks WHERE session_key=?) "
            "GROUP BY turn_id", (key,)
        ))
    return {"tasks": tasks, "goal": goal, "calls": calls}


def exact_user_tasks(case: LiveCase, key: str, expected: Sequence[str]) -> None:
    state = durable_case_state(case.root / "state", key)
    with case.guard.connect() as db:
        started = {row[0] for row in db.execute("SELECT id FROM turns")}
    case.check(
        f"natural_exact_{len(expected)}_user_tasks",
        len(expected) == len(set(expected))
        and bool(all(expected))
        and {task["id"] for task in state["tasks"]} == set(expected)
        and started == set(expected)
        and all(task["status"] == "succeeded" for task in state["tasks"])
        and all(not task["goal"].get("automatic", False) for task in state["tasks"]),
    )


def file_write_in_turn(case: LiveCase, task_id: str, filename: str) -> bool:
    for record in case.records():
        if record.get("turn_id") != task_id or record.get("kind") != "tool_request":
            continue
        payload = record.get("payload") or {}
        args = payload.get("arguments") or {}
        if payload.get("name") != "write_file" or not isinstance(args, dict):
            continue
        path = args.get("path")
        if not isinstance(path, str):
            continue
        candidate = Path(path)
        candidate = candidate if candidate.is_absolute() else case.workspace / candidate
        if candidate.resolve() == (case.workspace / filename).resolve():
            return True
    return False


async def background_case(case: LiveCase) -> None:
    key = "agent:main:webchat:live-background"
    await case.rpc("plans.setMode", sessionKey=key, mode="default", expectedRevision=0)
    await case.subscribe(key)
    accepted = await case.rpc(
        "goals.set", sessionKey=key, executionPolicy="background",
        objective=(
            "Perform this explicit two-turn background check. During the initial turn, ask "
            "me Blue or Green using request_user_input, question id color, and wait for "
            "the real answer. After the answer use write_file to write phase1.txt with "
            "PHASE1, read it back, and end this turn with a brief status. Keep this Goal "
            "active; do not pause, complete, or write phase2.txt in the first turn. On the "
            "next automatic Goal turn, read phase1.txt, use write_file to write phase2.txt "
            "with PHASE2, read both files back, and mark this same Goal complete. "
            "Do not ask further questions, create another Goal, or spawn children."
        ),
        clientRequestId=str(uuid.uuid4()), clientMessageId=str(uuid.uuid4()),
    )
    first = accepted["taskId"]
    pending = await case.pending(key, first)
    before = durable_case_state(case.root / "state", key)
    case.check("background_first_turn_waits", len(before["tasks"]) == 1)
    case.check("background_policy_explicit", bool(before["goal"][4]))
    disconnect_gate = DisconnectDispatchGate(case.guard)
    disconnect_gate.arm(first)
    await case.answer(key, pending)
    await case.disconnect()
    try:
        boundary = await disconnect_gate.wait_for_boundary(
            deadline=case.deadline, process=case.process,
        )
    finally:
        case.evidence["background_disconnect_boundary"] = disconnect_gate.evidence()
    calls_at_disconnect = boundary["calls_at_disconnect"]
    case.check(
        "background_disconnected_before_auto",
        boundary["roots_at_disconnect"] == 1 and boundary["connections_at_disconnect"] == 0,
    )
    # The harness has exactly one WS client. Remain completely disconnected and
    # observe only SQLite and the real transport guard until the Goal settles.
    async with asyncio.timeout_at(case.deadline):
        while True:
            state = durable_case_state(case.root / "state", key)
            if state["goal"][1] == "complete" and state["goal"][2] is None:
                break
            if case.process is None or case.process.poll() is not None:
                raise CaseFailureError("gateway_exited")
            if state["goal"][1] not in {"active", "complete"}:
                raise CaseFailureError("background_stopped_before_completion")
            await asyncio.sleep(0.15)
    tasks = state["tasks"]
    automatic = [task for task in tasks if task["goal"].get("automatic") is True]
    case.check("background_exactly_one_auto_turn", len(tasks) == 2 and len(automatic) == 1)
    auto = automatic[0]
    case.check(
        "background_normal_first_turn_then_auto",
        tasks[0]["id"] == first and tasks[0]["status"] == "succeeded"
        and auto["status"] == "succeeded" and auto["goal"].get("continuationSeq") == 1,
    )
    case.check(
        "background_provider_dispatched_without_client",
        case.client is None and state["calls"].get(auto["id"], 0) > 0
        and disconnect_gate.evidence()["connections_at_dispatch"] == 0
        and disconnect_gate.evidence()["automatic_dispatch_without_connections"] == 1
        and calls_at_disconnect is not None
        and case.guard.snapshot()["counts"].get("physical_calls", 0) > calls_at_disconnect,
    )
    case.check(
        "background_files_written_in_distinct_turns",
        file_write_in_turn(case, first, "phase1.txt")
        and not file_write_in_turn(case, first, "phase2.txt")
        and file_write_in_turn(case, auto["id"], "phase2.txt")
        and (case.workspace / "phase1.txt").read_text().strip() == "PHASE1"
        and (case.workspace / "phase2.txt").read_text().strip() == "PHASE2",
    )
    counts = case.guard.snapshot()["counts"]
    await case.reconnect()
    snapshot = await case.snapshot(key)
    case.check("background_reconnect_hydrates_complete", snapshot["goal"]["status"] == "complete")
    await asyncio.sleep(1)
    final = durable_case_state(case.root / "state", key)
    case.check(
        "background_reconnect_does_not_dispatch",
        case.guard.snapshot()["counts"] == counts
        and {task["id"] for task in final["tasks"]} == {first, auto["id"]},
    )
    case.evidence["background"] = {
        "initial_turns": 1, "automatic_turns_without_client": len(automatic),
        "automatic_finalized_calls": state["calls"][auto["id"]],
        "disconnect_boundary": disconnect_gate.evidence(),
    }


async def goal_case(case: LiveCase) -> None:
    key = "agent:main:webchat:live-goal"
    first = await case.send(
        key,
        "Create a Goal to write result.txt containing BLUE in this workspace. "
        "Use create_goal. I explicitly want you to pause it immediately "
        "using update_goal paused before doing any work. Then stop.",
    )
    await case.done(key, first)
    exact_user_tasks(case, key, [first])
    goal = (await case.rpc("goals.status", sessionKey=key))["goal"]
    case.check("natural_goal_exists", isinstance(goal, dict))
    case.check("natural_goal_created_paused", goal["status"] == "paused")
    case.check("no_implicit_token_budget", goal["tokenBudget"] is None)
    second = await case.send(
        key,
        "Change the existing Goal objective with update_goal: write result.txt containing "
        "GREEN, read it back, then complete. Keep the Goal paused. Do not write files yet. "
        "Do not create a second Goal.",
    )
    await case.done(key, second)
    exact_user_tasks(case, key, [first, second])
    edited = (await case.rpc("goals.status", sessionKey=key))["goal"]
    case.check(
        "natural_edit_preserves_goal",
        edited["goalId"] == goal["goalId"]
        and "GREEN" in edited["objective"]
        and edited["status"] == "paused",
    )
    case.check("paused_goal_did_not_execute", not (case.workspace / "result.txt").exists())
    third = await case.send(
        key,
        "Resume the existing Goal now with update_goal status active. Complete the requested "
        "GREEN file, verify it by reading it back, and mark this same Goal complete. "
        "Do not create a new Goal.",
    )
    snapshot = await case.done(key, third)
    exact_user_tasks(case, key, [first, second, third])
    case.check(
        "natural_resume_completes_same_goal",
        snapshot["goal"]["status"] == "complete" and snapshot["goal"]["goalId"] == goal["goalId"],
    )
    case.check(
        "goal_complete_verified", (case.workspace / "result.txt").read_text().strip() == "GREEN"
    )
    case.check("goal_usage_accounted", snapshot["goal"]["budgetTokensUsed"] > 0)


async def seed_historical_budget_goal(case: LiveCase, key: str, objective: str) -> str:
    """Seed current-schema upgrade accounting, not a retired Goal DDL conversion."""
    import aiosqlite

    from opensquilla.session.goals import new_goal
    from opensquilla.session.storage import SessionStorage

    path = usage_ledger_path(case.root / "state")
    case.check("historical_fixture_database_present", path is not None)
    assert path is not None
    async with aiosqlite.connect(path) as db:
        await db.execute("BEGIN IMMEDIATE")
        for table in ("agent_tasks", "usage_events", "session_goals"):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                row = await cursor.fetchone()
            case.check("historical_fixture_database_unused", row == (0,))
        async with db.execute(
            "SELECT session_id, epoch FROM sessions WHERE session_key = ?", (key,),
        ) as cursor:
            session = await cursor.fetchone()
        case.check("historical_fixture_session_present", session is not None)
        assert session is not None
        goal = new_goal(
            goal_id=str(uuid.uuid4()), session_key=key, session_id=session[0],
            session_epoch=session[1], objective=objective,
        ).model_copy(update={
            "status": "paused", "pause_reason": "process_restart",
            "usage_accounting_version": 0, "usage_coverage": "partial_history",
            "usage_accounting_started_at_ms": None,
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
        })
        await SessionStorage._insert_goal_on_conn(db, goal)
        await db.commit()
    return goal.goal_id


def historical_budget_evidence(state: Path, key: str) -> dict[str, Any]:
    path = usage_ledger_path(state)
    if path is None:
        raise CaseFailureError("missing_usage_ledger")
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as db:
        goal = db.execute(
            "SELECT goal_id, usage_accounting_version, usage_coverage, "
            "usage_accounting_started_at_ms, budget_tokens_used, "
            "input_tokens, output_tokens, total_tokens FROM session_goals WHERE session_key = ?",
            (key,),
        ).fetchone()
        if goal is None:
            raise CaseFailureError("historical_budget_goal_missing")
        usage = db.execute(
            "SELECT COUNT(*), MIN(started_at_ms), SUM(input_tokens), SUM(output_tokens), "
            "SUM(total_tokens), SUM(MAX(0, input_tokens - cache_read_tokens) + output_tokens), "
            "SUM(status != 'finalized' OR coverage_status = 'usage_missing') "
            "FROM usage_events WHERE goal_id = ?", (goal[0],),
        ).fetchone()
    assert usage is not None
    return {
        "fixture_kind": "synthetic_upgraded_goal_accounting",
        "retired_goal_conversion_tested": False,
        "accounting_version": goal[1], "coverage": goal[2],
        "boundary_matches_first_request": goal[3] is not None and goal[3] == usage[1],
        "physical_receipts": usage[0],
        "receipts_complete": usage[0] > 0 and usage[6] == 0,
        "historical_totals_preserved": (
            goal[5] == 100 + (usage[2] or 0)
            and goal[6] == 50 + (usage[3] or 0)
            and goal[7] == 150 + (usage[4] or 0)
        ),
        "budget_matches_physical_usage": goal[4] == usage[5] and goal[4] > 0,
    }


async def budget_case(case: LiveCase, *, historical: bool = False) -> None:
    key = "agent:main:webchat:live-historical-budget" if historical else (
        "agent:main:webchat:live-budget"
    )
    release = case.workspace / "release.txt"
    case.check("budget_external_release_initially_absent", not release.exists())
    inspection = (
        "to check release.txt and observe its missing-file result. It is initially absent. "
        "Do not create "
        "or modify release.txt or substitute any other evidence. This Goal can only be "
        "complete after the external operator supplies the real release.txt; until then "
        "keep it unfinished and do not call complete or blocked. Report its current state. "
        "The token budget should pause automatic continuation naturally."
    )
    objective = "Inspect release.txt supplied by an external operator. Use read_file " + inspection
    if historical:
        await case.rpc("plans.setMode", sessionKey=key, mode="default", expectedRevision=0)
        await case.subscribe(key)
        goal_id = await seed_historical_budget_goal(case, key, objective)
        initial = await case.snapshot(key)
        goal = initial.get("goal") or {}
        case.check(
            "historical_fixture_hydrated",
            goal.get("goalId") == goal_id and goal.get("status") == "paused"
            and goal.get("usageCoverage") == "partial_history"
            and goal.get("usageAccountingStartedAtMs") is None
            and goal.get("budgetTokensUsed") == 0
            and (goal.get("usage") or {}).get("totalTokens") == 150,
        )
        edited = await case.rpc(
            "goals.edit", sessionKey=key, expectedGoalId=goal_id,
            expectedStateRevision=goal["stateRevision"], clientRequestId=str(uuid.uuid4()),
            objective=goal["objective"], tokenBudget=1,
        )
        goal = edited["goal"]
        case.check(
            "historical_budget_set_without_resuming",
            goal["status"] == "paused" and goal["tokenBudget"] == 1
            and goal["budgetTokensUsed"] == 0 and goal["usageAccountingStartedAtMs"] is None,
        )
        await case.rpc(
            "goals.resume", sessionKey=key, expectedGoalId=goal_id,
            expectedStateRevision=goal["stateRevision"], clientRequestId=str(uuid.uuid4()),
        )
        # Resume schedules ordinary admission; its RPC receipt has no task id.
        # Wait for the actual task, including a task already terminal at hydrate.
        started = await case.until(key, lambda s: bool(s.get("tasks")))
        tasks = started["tasks"]
        task = str(tasks[0].get("task_id") or "")
        case.check("historical_budget_resumed_task", len(tasks) == 1 and bool(task))
    else:
        task = await case.send(
            key,
            "Create a Goal with token budget exactly 1 to inspect release.txt supplied by an "
            "external operator. Use create_goal with token_budget=1 first, then use read_file "
            + inspection,
        )
    initial = await case.done(key, task)
    case.check("budget_goal_created", isinstance(initial.get("goal"), dict))
    snapshot = await case.until(
        key,
        lambda s: (s.get("goal") or {}).get("status") in {
            "paused", "complete", "blocked", "usage_limited",
        },
    )
    goal = snapshot["goal"]
    case.check("budget_external_release_not_fabricated", not release.exists())
    case.check(
        "budget_stops_goal",
        goal["status"] == "paused" and goal["pauseReason"] == "token_budget"
        and goal["tokenBudget"] == 1 and goal["budgetTokensUsed"] >= 1,
    )
    checked_release = False
    for record in case.records():
        payload = record.get("payload") or {}
        args = payload.get("arguments") or {}
        if (record.get("kind") == "tool_request" and record.get("turn_id") == task
                and payload.get("name") == "read_file" and isinstance(args, dict)):
            raw_path = args.get("path")
            if isinstance(raw_path, str):
                path = Path(raw_path)
                path = path if path.is_absolute() else case.workspace / path
                checked_release |= path.resolve() == release.resolve()
    case.check("budget_external_release_checked", checked_release)
    calls = case.guard.snapshot()["counts"].get("physical_calls", 0)
    await asyncio.sleep(1)
    case.check(
        "budget_no_continuation", case.guard.snapshot()["counts"].get("physical_calls", 0) == calls
    )
    if historical:
        proof = historical_budget_evidence(case.root / "state", key)
        case.evidence["historical_budget"] = proof
        case.check("historical_coverage_not_rewritten", proof["accounting_version"] == 0
                   and proof["coverage"] == "partial_history")
        for field in ("boundary_matches_first_request", "receipts_complete",
                      "historical_totals_preserved", "budget_matches_physical_usage"):
            case.check("historical_" + field, proof[field])
        case.check("historical_budget_single_root",
                   case.guard.snapshot()["counts"].get("root_turns") == 1)


async def childbudget_case(case: LiveCase) -> None:
    key = "agent:main:webchat:live-childbudget"
    (case.workspace / "numbers.txt").write_text("3\n5\n7\n", encoding="utf-8")
    first = await case.send(
        key,
        "Create a Goal to obtain an independent sum of numbers.txt. Use create_goal first. "
        "Then use sessions_spawn exactly once to ask a child to read numbers.txt and return the "
        "sum of its numeric file contents, excluding the read tool's display line numbers. "
        "Ask it to verify the calculation with python3; do not supply the answer. "
        "Tell the child not to spawn others or change files. "
        "Call sessions_yield without session_key and wait for the pushed child completion. "
        "After receiving that real result, write child-result.txt with the sum and read it back. "
        "I explicitly request that you pause this Goal with update_goal paused at that point. "
        "Do not complete or automatically resume the Goal. Do not set any token budget yet.",
    )
    await case.done(key, first)
    async with asyncio.timeout_at(case.deadline):
        while True:
            spawned = [
                r["payload"]
                for r in case.records()
                if r.get("kind") == "tool_response"
                and r["payload"].get("name") == "sessions_spawn"
                and not r["payload"].get("is_error")
            ]
            if spawned:
                child = json.loads(spawned[0]["result"])
                break
            await asyncio.sleep(0.15)
    await case.subscribe(child["session_key"])
    await case.done(child["session_key"], child["task_id"])
    snapshot = await case.until(
        key,
        lambda s: (s.get("goal") or {}).get("status") == "paused" and not s.get("active_task"),
    )
    case.check(
        "child_output_consumed", (case.workspace / "child-result.txt").read_text().strip() == "15"
    )
    goal = snapshot["goal"]
    proof = child_usage_evidence(case.root / "state", goal["goalId"])
    case.evidence["child_usage"] = proof
    case.check("real_root_and_child_calls", proof["root_calls"] > 0 and proof["child_calls"] > 0)
    case.check("child_lineage_recorded", proof["lineage_present"] and proof["all_calls_finalized"])
    case.check(
        "child_usage_counted_once",
        proof["goal_budget_tokens"] == proof["physical_budget_tokens"]
        and proof["goal_total_tokens"] == proof["physical_total_tokens"],
    )
    case.check("child_budget_nonzero", proof["child_budget_tokens"] > 0)
    # Set a budget which root usage alone would satisfy, but root+child has
    # exhausted. Resume must reject without another model call or duplicated work.
    budget = proof["root_budget_tokens"] + 1
    edited = await case.rpc(
        "goals.edit",
        sessionKey=key,
        expectedGoalId=goal["goalId"],
        expectedStateRevision=goal["stateRevision"],
        clientRequestId=str(uuid.uuid4()),
        objective=goal["objective"],
        tokenBudget=budget,
    )
    goal = edited["goal"]
    calls = case.guard.snapshot()["counts"].get("physical_calls", 0)
    rejected = False
    try:
        await case.rpc(
            "goals.resume",
            sessionKey=key,
            expectedGoalId=goal["goalId"],
            expectedStateRevision=goal["stateRevision"],
            clientRequestId=str(uuid.uuid4()),
        )
    except GatewayRPCError as exc:
        rejected = exc.code == "GOAL_BUDGET_EXHAUSTED"
    case.check("child_usage_blocks_resume", rejected)
    await asyncio.sleep(1)
    case.check(
        "budget_rejection_no_dispatch",
        case.guard.snapshot()["counts"].get("physical_calls", 0) == calls,
    )


async def restart_case(case: LiveCase) -> None:
    key = "agent:main:webchat:live-restart"
    await case.rpc("plans.setMode", sessionKey=key, mode="default", expectedRevision=0)
    await case.subscribe(key)
    created = await case.rpc(
        "goals.set",
        sessionKey=key,
        objective=QUESTION,
        executionPolicy="background",
        clientRequestId=str(uuid.uuid4()),
        clientMessageId=str(uuid.uuid4()),
    )
    await case.pending(key, created["taskId"])
    await case.reconnect()
    snapshot = await case.snapshot(key)
    case.check(
        "background_survives_connection_loss",
        snapshot["goal"]["status"] == "active"
        and snapshot["goal"]["executionPolicy"] == "background",
    )
    await case.stop(crash=True)
    calls = case.guard.snapshot()["counts"].get("physical_calls", 0)
    await case.start()
    snapshot = await case.snapshot(key)
    case.check("restart_pauses_background", snapshot["goal"]["status"] == "paused")
    await asyncio.sleep(1)
    case.check(
        "restart_does_not_replay", case.guard.snapshot()["counts"].get("physical_calls", 0) == calls
    )
    goal = snapshot["goal"]
    params = {
        "sessionKey": key,
        "expectedGoalId": goal["goalId"],
        "expectedStateRevision": goal["stateRevision"],
        "clientRequestId": str(uuid.uuid4()),
    }
    accepted = await case.rpc("goals.resume", **params)
    replay = await case.rpc("goals.resume", **params)
    case.check("resume_idempotent", replay == accepted)
    # Resume acknowledges a state transition before ordinary idle admission;
    # its durable response can correctly have taskId=null. Wait for the actual
    # continuation identity, never interpret the old abandoned task as its result.
    resumed = await case.until(
        key,
        lambda s: any(
            t.get("task_id") != created["taskId"] for t in s.get("tasks", [])
        ),
    )
    next_tasks = [t for t in resumed["tasks"] if t.get("task_id") != created["taskId"]]
    case.check("restart_exactly_one_resume_task", len(next_tasks) == 1)
    await case.pending(key, next_tasks[0]["task_id"])
    goal = (await case.rpc("goals.status", sessionKey=key))["goal"]
    await case.rpc(
        "goals.pause",
        sessionKey=key,
        expectedGoalId=goal["goalId"],
        expectedStateRevision=goal["stateRevision"],
        clientRequestId=str(uuid.uuid4()),
    )
    case.check("rpc_pause_persisted", (await case.snapshot(key))["goal"]["status"] == "paused")


async def cli_case(case: LiveCase) -> None:
    """Drive the public Gateway client through its real terminal renderer."""
    import shutil

    sys.path.insert(0, str(ROOT / "tests" / "integration" / "cli"))
    from tui_real_terminal.assertions import (
        assert_no_completion_menu_overlap,
        assert_no_duplicate_fixed_chrome,
        assert_no_stale_completion_menu,
        assert_prompt_ready,
    )
    from tui_real_terminal.driver import TerminalSize, open_real_terminal_session
    from tui_real_terminal.targets import opentui_host_skip_reason

    case.check("cli_tmux_available", shutil.which("tmux") is not None)
    key = "agent:main:webchat:live-cli"
    await case.rpc("plans.setMode", sessionKey=key, mode="default", expectedRevision=0)
    await case.subscribe(key)
    # The client receives no provider credential. Inference stays in the
    # already guarded Gateway; clear tmux's inherited environment as well.
    env = child_environment(case.provider, {})
    env.update({
        "PYTHONPATH": str(ROOT / "src"),
        "OPENSQUILLA_HOME": str(case.root / "cli-profile"),
        "OPENSQUILLA_STATE_DIR": str(case.root / "cli-state"),
        "OPENSQUILLA_USER_STATE_DIR": str(case.root / "cli-user-state"),
        "OPENSQUILLA_GATEWAY_URL": f"ws://127.0.0.1:{case.port}/ws",
        "OPENSQUILLA_GATEWAY_CONFIG_PATH": str(case.root / "config.toml"),
        "OPENSQUILLA_LOG_DIR": str(case.root / "cli-logs"),
        "OPENSQUILLA_TUI_DEV_SOURCE_HOST": "1",
        "OPENSQUILLA_TUI_READY_MARKER": "OPEN_SQUILLA_TUI_READY",
        "OPENSQUILLA_MEMORY_DREAM_DISABLED": "1",
        "OPENSQUILLA_OPENROUTER_LIVE_PRICING": "0",
        "TERM": "xterm-256color",
    })
    case.check("cli_source_host_available", opentui_host_skip_reason(env) is None)
    command = ["env", "-i", *(f"{k}={v}" for k, v in sorted(env.items())),
               sys.executable, "-u", "-m", "opensquilla.cli.main", "chat", "--session", key]
    run_id = f"opensquilla-live-{uuid.uuid4().hex}"
    terminal = open_real_terminal_session(
        command=command, cwd=case.workspace, env={}, run_id=run_id,
        size=TerminalSize(120, 36), artifact_dir=case.root / "terminal", driver="tmux",
        driver_env=env, tmux_socket=run_id, deadline=case.deadline,
    )
    case.terminal = terminal
    geometry = []
    await asyncio.to_thread(terminal.start)
    await asyncio.to_thread(
        terminal.wait_for_text, "OPEN_SQUILLA_TUI_READY",
        timeout_s=min(20, max(0, case.deadline - time.monotonic())), checkpoint="ready",
    )
    await asyncio.to_thread(
        terminal.send_text,
        "请直接用中文写三段简短的合成终端测试说明，包含 emoji 🦑 和 ✅。"
        "不要调用工具，不要创建目标或方案。最后一行写：终端验证完成。",
    )
    accepted = await case.until(key, lambda s: bool(s.get("tasks")))
    case.check("cli_single_task", len(accepted["tasks"]) == 1)
    task = accepted["tasks"][0]["task_id"]
    # Resize while the real provider request/stream is active, then prove
    # the completed framebuffer at both narrow and wide geometries.
    await asyncio.to_thread(terminal.resize, TerminalSize(80, 28))
    await case.done(key, task)
    for width in (80, 120):
        await asyncio.to_thread(terminal.resize, TerminalSize(width, 36))
        await asyncio.to_thread(
            terminal.wait_for_text, "终端验证完成",
            timeout_s=min(15, max(0, case.deadline - time.monotonic())),
            checkpoint=f"settled-{width}",
        )
        frame = await asyncio.to_thread(terminal.capture_text, f"geometry-{width}")
        assert_prompt_ready(frame)
        assert_no_duplicate_fixed_chrome(frame)
        case.check("cli_unicode_framebuffer", all(
            text in frame.text for text in ("🦑", "✅", "终端验证完成")
        ))
        cursor = await asyncio.to_thread(terminal.cursor_position)
        case.check("cli_cursor_in_bounds", cursor is not None
                   and 0 <= cursor[0] < width and 0 <= cursor[1] < 36)
        geometry.append({"columns": width, "rows": 36, "cursor_in_bounds": True,
                         "unicode_visible": True})
    await asyncio.to_thread(terminal.paste, "/")
    overlay = await asyncio.to_thread(
        terminal.wait_for_text, "commands", timeout_s=5, checkpoint="commands",
    )
    assert_no_completion_menu_overlap(overlay)
    await asyncio.to_thread(terminal.send_key, "Escape")
    await asyncio.to_thread(terminal.send_key, "C-u")
    # Waiting for the ordinary composer also gives the renderer a flush;
    # no browser screenshot stands in for these terminal frames.
    await asyncio.sleep(0.2)
    frame = await asyncio.to_thread(terminal.capture_text, "overlay-closed")
    assert_no_stale_completion_menu(frame)
    assert_prompt_ready(frame)
    replies = "\n".join(str(r["payload"].get("text", "")) for r in case.records()
                        if r.get("kind") == "llm_response")
    case.check("cli_unicode_response", "🦑" in replies and "✅" in replies
               and "终端验证完成" in replies)
    case.check("cli_rendering_verified", True)
    case.evidence["terminal"] = {"driver": "tmux", "geometry": geometry,
                                 "unicode": True, "overlay": True}
    case.check(
        "cli_one_provider_call", case.guard.snapshot()["counts"].get("physical_calls") == 1,
    )


async def run_case(
    name: str, provider: str, model: str, secrets: Mapping[str, str],
    *, retain_failed_state: bool = False, thinking: str = "off",
) -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix="opensquilla-live-plan-goal-"))
    os.chmod(root, 0o700)
    case: LiveCase | None = None
    result: dict[str, Any] = {"case": name, "status": "failed"}
    try:
        case = LiveCase(root, provider, model, secrets, thinking=thinking)
        async with asyncio.timeout_at(case.deadline):
            await case.start()
            if name in {"wait", "cancel"}:
                await wait_case(case, cancel=name == "cancel")
            elif name == "historical-budget":
                await budget_case(case, historical=True)
            else:
                await {
                    "plan": plan_case,
                    "plan-stop": plan_stop_case,
                    "plan-stop-child": plan_stop_child_case,
                    "plan-recovery": plan_recovery_case,
                    "goal": goal_case,
                    "budget": budget_case,
                    "childbudget": childbudget_case,
                    "restart": restart_case,
                    "background": background_case,
                    "cli": cli_case,
                }[name](case)
            case.check(
                "real_provider_called", case.guard.snapshot()["counts"].get("physical_calls", 0) > 0
            )
            evidence = provider_evidence(case.records(), provider, model)
            case.check("provider_request_identity", evidence["request_identity_matches"])
            case.check("provider_response_identity", evidence["response_identity_matches"])
            case.check(
                "durable_usage_present",
                any(
                    row["status"] == "finalized" and row["total"] > 0
                    for row in ledger_projection(root / "state")["rows"]
                ),
            )
            result["status"] = "passed"
    except Exception as exc:
        result["failure_class"] = (
            "timeout" if isinstance(exc, TimeoutError) else classify_failure(str(exc))
        )
        if isinstance(exc, TimeoutError):
            result["timeout_scope"] = "case_deadline"
        if isinstance(exc, CaseFailureError):
            result["failed_assertion"] = str(exc)
        # Exception messages can contain model text. Never return them.
        result["exception_type"] = type(exc).__name__
        if isinstance(exc, GatewayRPCError):
            code = str(exc.code or "")
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
                result["rpc_error_code"] = code
    finally:
        shutdown: dict[str, Any] = {"process_exited": case is None, "forced": False}
        try:
            if case:
                try:
                    shutdown = await case.stop()
                except Exception as exc:
                    shutdown["capture_error"] = type(exc).__name__
                    shutdown["process_exited"] = (
                        case.process is None or case.process.poll() is not None
                    )
                result.update({"assertions": case.assertions, "scenario_evidence": case.evidence})
                # Capture after shutdown even when it failed. Each projection is
                # independent so one damaged table cannot erase other evidence.
                for field, capture in {
                    "dispatch": case.guard.snapshot,
                    "ledger": lambda: ledger_projection(root / "state"),
                    "provider_evidence": lambda: provider_evidence(case.records(), provider, model),
                    "runtime_diagnostics": lambda: runtime_diagnostics(
                        root / "state", case.records(), case.workspace
                    ),
                }.items():
                    try:
                        result[field] = capture()
                    except Exception as exc:
                        result[field] = {"capture_error": type(exc).__name__}
                shutdown["usage_complete"] = (
                    not any(key.endswith("error") for key in shutdown)
                    and not shutdown.get("forced") and shutdown.get("process_exited") is True
                    and "capture_error" not in result.get("ledger", {})
                    and all(row["status"] == "finalized"
                            for row in result.get("ledger", {}).get("rows", []))
                )
                result["shutdown"] = shutdown
                if not shutdown["usage_complete"] and result["status"] == "passed":
                    result.update(status="failed", failure_class="implementation",
                                  failed_assertion="shutdown_usage_incomplete")
                failures = result.get("dispatch", {}).get("failures", [])
                if failures and result["status"] != "passed":
                    result["failure_class"] = failures[-1]
        finally:
            if retain_failed_state and result["status"] != "passed" and shutdown["process_exited"]:
                retained = retain_failed_temporary_tree(root, secrets)
                result["retained_failed_state"] = root.name if retained else None
            else:
                scan_and_remove_temporary_tree(root, secrets)
    return result


async def run(
    provider: str,
    scenario: str,
    *,
    environment: Mapping[str, str],
    output: Path | None = None,
    retain_failed_state: bool = False,
    thinking: str = "off",
) -> dict[str, Any]:
    spec = get_provider_spec(provider)
    secret = environment.get(spec.env_key, "")
    model = selected_model(provider, environment)
    result: dict[str, Any] = {
        "schema_version": 1,
        "provider": provider,
        "model": model,
        "thinking": thinking,
        "platform": platform.system(),
        "limits": dict(LIMITS),
        "status": "not_run",
        "cases": [],
    }
    if not secret:
        result["failure_class"] = "missing-credential"
        return result
    result["source_start"] = execution_source_fingerprint()
    secrets = {spec.env_key: secret}
    names = ("historical-budget",) if scenario == "historical-budget" else (
        tuple(dict.fromkeys(n for group in SCENARIOS.values() for n in group))
        if scenario == "all"
        else SCENARIOS[scenario]
    )
    for name in names:
        case_options = {"retain_failed_state": True} if retain_failed_state else {}
        if thinking != "off":
            case_options["thinking"] = thinking
        row = await run_case(name, provider, model, secrets, **case_options)
        result["cases"].append(row)
        result["status"] = "running"
        if output is not None:
            write_safe_report(output, result, secrets)
        if row.get("failure_class") in {"auth", "balance", "not-entitled", "model-unavailable"}:
            break
    result["status"] = (
        "passed"
        if len(result["cases"]) == len(names)
        and all(row["status"] == "passed" for row in result["cases"])
        else "failed"
    )
    result["source_end"] = execution_source_fingerprint()
    result["source_unchanged"] = result["source_start"] == result["source_end"]
    if not result["source_unchanged"]:
        result["status"] = "failed"
        result["failure_class"] = "source-changed-during-validation"
    return sanitize_report(result, secrets)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(MODELS), required=True)
    parser.add_argument(
        "--scenario", choices=(*SCENARIOS, "historical-budget", "all"), default="all",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--thinking", choices=("off", "low", "medium", "high"), default="off")
    parser.add_argument(
        "--retain-failed-state", action="store_true",
        help="Retain credential-scanned private state only for failed cases (default: delete)",
    )
    parser.add_argument("--_gateway", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--guard", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--model", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._gateway:
        if not args.guard or not args.port or not args.model:
            parser.error("internal gateway requires guard, port and model")
        install_dispatch_guard(DispatchGuard(args.guard, provider=args.provider, model=args.model))
        sys.argv = [
            "opensquilla",
            "gateway",
            "run",
            "--port",
            str(args.port),
            "--bind",
            "127.0.0.1",
        ]
        runpy.run_module("opensquilla.cli.main", run_name="__main__")
        return 0
    if args.output is None:
        parser.error(
            "--output is required (outside the repository, under the system temporary directory)"
        )
    require_temporary_report_path(args.output)
    report = asyncio.run(
        run(args.provider, args.scenario, environment=os.environ, output=args.output,
            retain_failed_state=args.retain_failed_state, thinking=args.thinking)
    )
    spec = get_provider_spec(args.provider)
    safe = write_safe_report(args.output, report, {spec.env_key: os.environ.get(spec.env_key, "")})
    print(json.dumps(safe, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
