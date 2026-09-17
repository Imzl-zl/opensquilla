"""Offline transport ordering and open-objective evidence for real-provider cases."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.gateway import websocket
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.observability.turn_call_log import TurnCallLogger
from scripts import live_plan_goal_runtime as live


@pytest.fixture
def transport_stack(tmp_path, monkeypatch):
    registry = websocket.ConnectionRegistry()
    registry.register(SimpleNamespace(conn_id="synthetic-owner"))
    monkeypatch.setattr(websocket, "get_registry", lambda: registry)
    calls, listener_order = [], []

    def goal_listener(connection):
        assert registry.get(connection.conn_id) is connection
        listener_order.append("goal-authority-observed")

    registry.set_unregister_listener(goal_listener)

    async def fake_http(_transport, _request):
        calls.append("physical-dispatch")
        return httpx.Response(200, content=b"{}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", httpx.HTTPTransport.handle_request)
    monkeypatch.setattr(TaskRuntime, "_mark_running", TaskRuntime._mark_running)
    monkeypatch.setattr(TurnCallLogger, "write", TurnCallLogger.write)
    monkeypatch.setattr(websocket.ConnectionRegistry, "unregister",
                        websocket.ConnectionRegistry.unregister)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-offline-key")
    guard = live.DispatchGuard(
        tmp_path / "guard.sqlite", provider="deepseek", model="deepseek-chat",
    )
    guard.claim("root_turns", 4, turn_id="first")
    gate = live.install_dispatch_guard(guard)
    assert registry._unregister_listener is goal_listener
    return guard, gate, registry, calls, listener_order


async def dispatch(guard, turn_id="first"):
    scope = UsageAccountingScope(
        sink=object(), context=UsageExecutionContext(
            execution_id=turn_id, agent_run_id=turn_id, turn_id=turn_id,
        ),
    )
    with bind_usage_accounting_scope(scope):
        async with httpx.AsyncClient() as client:
            return await client.post(
                guard.endpoint + "/chat/completions",
                content=json.dumps({"model": "deepseek-chat", "max_tokens": 16}).encode(),
            )


async def test_unarmed_disconnect_gate_preserves_normal_dispatch(transport_stack):
    guard, gate, registry, calls, observed = transport_stack
    await dispatch(guard)
    assert calls == ["physical-dispatch"] and not observed
    assert len(registry.all()) == 1 and not gate.waiting.is_set()
    assert guard.snapshot()["counts"]["physical_calls"] == 1


async def test_disconnect_gate_waits_for_real_unregister_and_preserves_goal_listener(
    transport_stack,
):
    guard, gate, registry, calls, observed = transport_stack
    gate.arm("first")
    sending = asyncio.create_task(dispatch(guard))
    await asyncio.wait_for(gate.waiting.wait(), 2)
    assert not sending.done() and not calls
    assert guard.snapshot()["counts"].get("physical_calls", 0) == 0
    registry.unregister("synthetic-owner")
    assert observed == ["goal-authority-observed"] and registry.all() == []
    await sending
    assert calls == ["physical-dispatch"]
    assert gate.evidence() == {
        "roots_at_disconnect": 1, "connections_at_disconnect": 0,
        "connections_at_dispatch": 0, "calls_at_disconnect": 0,
        "automatic_dispatch_without_connections": None,
    }
    guard.claim("root_turns", 4, turn_id="automatic")
    await dispatch(guard, "automatic")
    assert gate.evidence()["automatic_dispatch_without_connections"] == 1
    assert guard.snapshot()["counts"]["physical_calls"] == 2


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
async def test_disconnect_gate_fails_closed_on_cancellation_or_deadline(
    transport_stack, interruption,
):
    guard, gate, registry, calls, observed = transport_stack
    gate.arm("first")
    if interruption == "deadline":
        with guard.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE name='started'",
                       (str(time.time() - live.LIMITS["case_seconds"] - 1),))
        with pytest.raises(TimeoutError):
            await dispatch(guard)
    else:
        sending = asyncio.create_task(dispatch(guard))
        await asyncio.wait_for(gate.waiting.wait(), 2)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    assert not calls and not observed and len(registry.all()) == 1
    assert guard.snapshot()["counts"].get("physical_calls", 0) == 0
    assert gate.evidence()["connections_at_dispatch"] is None


async def test_disconnect_gate_rechecks_registry_if_connection_returns(transport_stack):
    guard, gate, registry, calls, _observed = transport_stack
    gate.arm("first")
    sending = asyncio.create_task(dispatch(guard))
    await asyncio.wait_for(gate.waiting.wait(), 2)
    registry.unregister("synthetic-owner")
    registry.register(SimpleNamespace(conn_id="new-owner"))
    with pytest.raises(live.DispatchLimitError, match="connection_returned"):
        await sending
    assert not calls and guard.snapshot()["counts"].get("physical_calls", 0) == 0


@pytest.mark.parametrize("status", ["complete", "blocked", "usage_limited"])
async def test_budget_case_rejects_terminal_goal_without_waiting_out_deadline(
    tmp_path, monkeypatch, status,
):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    goal = {"status": status, "tokenBudget": 1, "budgetTokensUsed": 10, "pauseReason": None}
    seen = []

    async def send(_key, message):
        seen.append(message)
        return "synthetic-task"

    async def done(_key, _task):
        return {"goal": goal}

    async def until(_key, predicate):
        assert predicate({"goal": goal}), "A settled terminal Goal must not enter a 600s wait"
        return {"goal": goal}

    monkeypatch.setattr(case, "send", send)
    monkeypatch.setattr(case, "done", done)
    monkeypatch.setattr(case, "until", until)
    with pytest.raises(live.CaseFailureError, match="budget_stops_goal"):
        await live.budget_case(case)
    assert "release.txt" in seen[0] and "Do not create" in seen[0]
    assert case.assertions["budget_external_release_not_fabricated"]


@pytest.mark.parametrize("fabricated", [False, True])
async def test_budget_case_rejects_missing_goal_or_fabricated_external_release(
    tmp_path, monkeypatch, fabricated,
):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    goal = {"status": "paused", "tokenBudget": 1, "budgetTokensUsed": 10,
            "pauseReason": "token_budget"}

    async def send(_key, _message):
        if fabricated:
            case.workspace.mkdir(parents=True, exist_ok=True)
            (case.workspace / "release.txt").write_text("synthetic forgery")
        return "synthetic-task"

    async def done(_key, _task):
        return {"goal": goal if fabricated else None}

    async def until(_key, predicate):
        assert fabricated and predicate({"goal": goal})
        return {"goal": goal}

    monkeypatch.setattr(case, "send", send)
    monkeypatch.setattr(case, "done", done)
    monkeypatch.setattr(case, "until", until)
    expected = "budget_external_release_not_fabricated" if fabricated else "budget_goal_created"
    with pytest.raises(live.CaseFailureError, match=expected):
        await live.budget_case(case)
