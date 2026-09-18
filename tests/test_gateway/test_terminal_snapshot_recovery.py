"""Hydration must not resurrect a task whose terminal write was delayed."""

from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_sessions import _overlay_runtime_task_snapshot, _task_state_summary
from opensquilla.gateway.session_lifecycle import SessionTaskSnapshot
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus


@pytest.mark.parametrize("durable_status", [AgentTaskStatus.QUEUED, AgentTaskStatus.RUNNING])
@pytest.mark.parametrize("terminal_status", [AgentTaskStatus.FAILED, AgentTaskStatus.SUCCEEDED])
@pytest.mark.parametrize("next_accepted", [False, True])
async def test_terminal_fallback_wins_without_hiding_unactivated_acceptance(
    durable_status, terminal_status, next_accepted,
):
    key = "agent:main:webchat:synthetic-recovery"
    stale = AgentTaskRecord(task_id="settled", session_key=key, status=durable_status, created_at=1)
    terminal = stale.model_copy(update={
        "status": terminal_status, "finished_at": 10, "terminal_reason": "completed",
    })
    rows = [stale]
    if next_accepted:
        rows.append(AgentTaskRecord(
            task_id="accepted-next", session_key=key, status=AgentTaskStatus.QUEUED, created_at=20,
        ))
    state = _task_state_summary(rows)
    runtime = SimpleNamespace(session_task_snapshot=lambda _key: SessionTaskSnapshot(
        running_task_id=None, queued_task_ids=(), terminal_tasks=(terminal,),
    ))
    await _overlay_runtime_task_snapshot(SimpleNamespace(task_runtime=runtime), key, state)
    assert state["tasks"][-1]["status"] == terminal_status.value
    if next_accepted:
        assert state["active_task"]["task_id"] == "accepted-next"
        assert state["queued_task_ids"] == ["accepted-next"]
        assert state["run_status"] == "queued"
    else:
        assert state["active_task"] is None
        assert state["queued_task_ids"] == []
        assert state["last_task"]["status"] == terminal_status.value
        expected = "failed" if terminal_status == AgentTaskStatus.FAILED else "idle"
        assert state["run_status"] == expected


async def test_terminal_snapshot_does_not_cross_session_boundary():
    row = AgentTaskRecord(task_id="task", session_key="agent:main:webchat:one")
    state = _task_state_summary([row])
    foreign = row.model_copy(update={
        "session_key": "agent:main:webchat:two", "status": AgentTaskStatus.FAILED,
    })
    runtime = SimpleNamespace(session_task_snapshot=lambda _key: SessionTaskSnapshot(
        None, (), terminal_tasks=(foreign,),
    ))
    await _overlay_runtime_task_snapshot(
        SimpleNamespace(task_runtime=runtime), row.session_key, state,
    )
    assert state["active_task"]["task_id"] == row.task_id
    assert state["run_status"] == "queued"
