"""Offline setup and oracle checks for supplemental real-provider budget validation."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_goals import _handle_goals_edit, _handle_goals_resume
from opensquilla.gateway.rpc_sessions import (
    _handle_plans_set_mode,
    _handle_sessions_messages_hydrate,
)
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import UsageEventCompletion, UsageEventStart
from scripts import live_plan_goal_runtime as live
from tests.test_gateway.test_goal_rpc import _open_goal_rpc_stack

KEY = "agent:main:webchat:live-historical-budget"


@pytest.mark.parametrize("setup", ["missing_database", "missing_session", "task", "goal", "usage"])
async def test_historical_fixture_rejects_missing_or_used_state(tmp_path, setup):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    if setup != "missing_database":
        async with await SessionStorage.open(str(tmp_path / "state" / "sessions.db")) as storage:
            if setup != "missing_session":
                await storage.upsert_session(SessionNode(session_key=KEY, session_id="synthetic"))
            if setup == "task":
                await storage.create_agent_task(AgentTaskRecord(
                    task_id="synthetic-task", session_key=KEY, status=AgentTaskStatus.RUNNING,
                ))
            if setup == "usage":
                await storage.start_usage_event(UsageEventStart(
                    event_id="synthetic-call", execution_id="synthetic-execution", call_index=0,
                    session_id="synthetic", session_epoch=0, started_at_ms=100,
                ))
            if setup == "goal":
                await live.seed_historical_budget_goal(case, KEY, "Synthetic objective")
    reason = {
        "missing_database": "historical_fixture_database_present",
        "missing_session": "historical_fixture_session_present",
    }.get(setup, "historical_fixture_database_unused")
    with pytest.raises(live.CaseFailureError, match=reason):
        await live.seed_historical_budget_goal(case, KEY, "Synthetic objective")
    assert case.guard.snapshot()["counts"] == {}


@pytest.mark.parametrize("receipt", ["finalized", "unknown"])
async def test_historical_budget_uses_real_rpc_admission_and_rejects_missing_receipt(
    tmp_path, monkeypatch, receipt,
):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    case.process = SimpleNamespace(poll=lambda: None)
    records = []

    async def handler(run):
        # Synthetic offline provider accounting, never a live-model success claim.
        case.guard.claim("root_turns", live.LIMITS["root_turns"], turn_id=run.task_id)
        case.guard.claim("physical_calls", live.LIMITS["physical_calls"])
        context = run.goal_context
        assert context is not None
        now = int(time.time() * 1000)
        await stack.storage.start_usage_event(UsageEventStart(
            event_id="synthetic-request", execution_id=run.task_id, call_index=0,
            turn_id=run.task_id, root_turn_id=run.task_id, session_id=context["sessionId"],
            session_epoch=context["epoch"], started_at_ms=now,
        ))
        records.append({"turn_id": run.task_id, "kind": "tool_request", "payload": {
            "name": "read_file", "arguments": {"path": str(case.workspace / "release.txt")},
        }})
        assert not (case.workspace / "release.txt").exists()
        if receipt == "unknown":
            await stack.storage.mark_usage_event_unknown(
                "synthetic-request", completed_at_ms=now + 1,
            )
        else:
            await stack.storage.finalize_usage_event("synthetic-request", UsageEventCompletion(
                completed_at_ms=now + 1, input_tokens=10, output_tokens=5, total_tokens=15,
                cache_read_tokens=4,
            ))

    async with _open_goal_rpc_stack(
        tmp_path / "state" / "sessions.db", handler=handler, wire_lifecycle=True,
    ) as stack:
        async def rpc(method, **params):
            if method == "sessions.messages.subscribe":
                stack.subscriptions.subscribe_messages(stack.context.conn_id, params["key"])
                return {}
            return await {
                "plans.setMode": _handle_plans_set_mode,
                "goals.edit": _handle_goals_edit,
                "goals.resume": _handle_goals_resume,
                "sessions.messages.hydrate": _handle_sessions_messages_hydrate,
            }[method](params, stack.context)

        monkeypatch.setattr(case, "rpc", rpc)
        monkeypatch.setattr(case, "records", lambda: records)
        if receipt == "unknown":
            with pytest.raises(live.CaseFailureError, match="budget_stops_goal"):
                await live.budget_case(case, historical=True)
            goal = await stack.storage.get_goal(KEY)
            assert goal is not None and goal.pause_reason == "usage_unknown"
            assert goal.usage_coverage == "partial_usage"
            assert goal.budget_tokens_used == 0
        else:
            await live.budget_case(case, historical=True)
            proof = case.evidence["historical_budget"]
            assert proof["fixture_kind"] == "synthetic_upgraded_goal_accounting"
            assert proof["retired_goal_conversion_tested"] is False
            assert all(case.assertions.values())
            assert proof["physical_receipts"] == 1
            goal = await stack.storage.get_goal(KEY)
            assert goal is not None
            assert (goal.budget_tokens_used, goal.total_tokens) == (11, 165)


async def test_historical_budget_is_opt_in_and_does_not_expand_all(monkeypatch):
    names = []

    async def run_case(name, *args, **kwargs):
        names.append(name)
        return {"case": name, "status": "passed"}

    monkeypatch.setattr(live, "run_case", run_case)
    environment = {"DEEPSEEK_API_KEY": "synthetic-offline-key"}
    result = await live.run("deepseek", "historical-budget", environment=environment)
    assert result["status"] == "passed" and names == ["historical-budget"]
    names.clear()
    await live.run("deepseek", "all", environment=environment)
    assert len(names) == 12
    assert "historical-budget" not in names
