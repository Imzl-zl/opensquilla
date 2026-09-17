from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway.websocket import ConnectionRegistry
from scripts import live_plan_goal_runtime as live


class ReachedDisconnectedObservationError(Exception):
    pass


async def test_background_case_waits_for_delayed_server_unregister_after_client_close(
    tmp_path, monkeypatch,
):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-flash", {})
    case.process = SimpleNamespace(poll=lambda: None)
    case.guard.claim("root_turns", 4, turn_id="first")
    registry = ConnectionRegistry()
    registry.register(SimpleNamespace(conn_id="synthetic-client"))
    server_gate = live.DisconnectDispatchGate(case.guard)
    closed = asyncio.Event()
    reads = 0

    async def rpc(method, **params):
        return {"taskId": "first"} if method == "goals.set" else {}

    async def subscribe(_key):
        return None

    async def pending(_key, _first):
        return {"request_id": "synthetic-question"}

    async def answer(_key, _request):
        return {}

    async def disconnect():
        case.client = None
        closed.set()
        # Deliberately return before the independent server's unregister.

    def durable_state(_state, _key):
        nonlocal reads
        reads += 1
        if reads == 1:
            return {"tasks": [{"id": "first"}], "goal": [None, "active", "first", 0, 1]}
        assert server_gate.evidence()["connections_at_disconnect"] == 0
        raise ReachedDisconnectedObservationError

    for name, value in (("rpc", rpc), ("subscribe", subscribe), ("pending", pending),
                        ("answer", answer), ("disconnect", disconnect)):
        monkeypatch.setattr(case, name, value)
    monkeypatch.setattr(live, "durable_case_state", durable_state)
    checking = asyncio.create_task(live.background_case(case))
    try:
        await asyncio.wait_for(closed.wait(), 2)
        # The prior harness has already failed here with boundary fields None.
        assert not checking.done()
        assert reads == 1 and registry.get("synthetic-client") is not None
        assert server_gate.evidence()["roots_at_disconnect"] is None
        registry.unregister("synthetic-client")
        server_gate.after_unregister(registry)
        with pytest.raises(ReachedDisconnectedObservationError):
            await checking
        assert case.assertions["background_disconnected_before_auto"]
        assert case.evidence["background_disconnect_boundary"]["roots_at_disconnect"] == 1
    finally:
        if not checking.done():
            checking.cancel()
        await asyncio.gather(checking, return_exceptions=True)


@pytest.mark.parametrize("reason", ["deadline", "cancel", "gateway_exited"])
async def test_disconnect_boundary_wait_fails_closed_without_server_evidence(
    tmp_path, monkeypatch, reason,
):
    guard = live.DispatchGuard(
        tmp_path / "guard.sqlite", provider="deepseek", model="deepseek-flash",
    )
    gate = live.DisconnectDispatchGate(guard)
    gate.arm("first")
    observed = asyncio.Event()
    original = gate.evidence

    def evidence():
        observed.set()
        return original()

    monkeypatch.setattr(gate, "evidence", evidence)
    loop = asyncio.get_running_loop()
    process = SimpleNamespace(poll=lambda: 1 if reason == "gateway_exited" else None)
    deadline = loop.time() - 1 if reason == "deadline" else loop.time() + 2
    waiting = asyncio.create_task(gate.wait_for_boundary(deadline=deadline, process=process))
    if reason == "cancel":
        await observed.wait()
        waiting.cancel()
    expected = {
        "deadline": TimeoutError, "cancel": asyncio.CancelledError,
        "gateway_exited": live.CaseFailureError,
    }[reason]
    with pytest.raises(expected):
        await waiting
    assert original()["roots_at_disconnect"] is None
    assert guard.snapshot()["counts"].get("physical_calls", 0) == 0


async def test_disconnect_boundary_wait_does_not_turn_wrong_root_count_into_success(tmp_path):
    guard = live.DispatchGuard(
        tmp_path / "guard.sqlite", provider="deepseek", model="deepseek-flash",
    )
    gate = live.DisconnectDispatchGate(guard)
    gate.arm("first")
    guard.claim("root_turns", 4, turn_id="first")
    guard.claim("root_turns", 4, turn_id="premature-auto")
    gate.after_unregister(ConnectionRegistry())
    boundary = await gate.wait_for_boundary(
        deadline=asyncio.get_running_loop().time() + 2,
        process=SimpleNamespace(poll=lambda: None),
    )
    assert boundary["roots_at_disconnect"] == 2
    assert not (boundary["roots_at_disconnect"] == 1
                and boundary["connections_at_disconnect"] == 0)
