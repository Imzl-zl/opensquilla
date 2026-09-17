"""Offline safety and evidence checks; never substitute a live-provider verdict."""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import live_plan_goal_runtime as live


async def _start_fixture(workspace: Path, maximum: str = "5"):
    (workspace / "child_wait.py").write_text(live.CHILD_STOP_FIXTURE)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-E", "-s", "child_wait.py", maximum, cwd=workspace,
        env=live.minimal_child_environment(), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(5):
            assert await process.stdout.readline() == b"STARTED\n"
    except BaseException:
        process.kill()
        await process.wait()
        raise
    return process


@pytest.mark.parametrize("cancel", [False, True])
async def test_real_child_fixture_exposes_an_orphan_after_release(tmp_path, cancel):
    process = await _start_fixture(tmp_path)
    try:
        if cancel:
            process.kill()
            await process.wait()
        (tmp_path / "child-release.txt").write_text("RELEASE\n")
        async with asyncio.timeout(5):
            await process.communicate()
        assert (tmp_path / "child-finished.txt").exists() is not cancel
        assert not (tmp_path / "child-timed-out.txt").exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_child_fixture_has_a_finite_wait_without_release(tmp_path):
    process = await _start_fixture(tmp_path, "0.05")
    async with asyncio.timeout(5):
        await process.communicate()
    assert process.returncode == 3
    assert (tmp_path / "child-timed-out.txt").read_text() == "TIMEOUT\n"
    assert not (tmp_path / "child-finished.txt").exists()


def _spawn_records():
    return [
        {"turn_id": "parent", "kind": "tool_request", "seq": 1,
         "payload": {"name": "sessions_spawn", "tool_use_id": "spawn"}},
        {"turn_id": "parent", "kind": "tool_response", "seq": 2,
         "payload": {"name": "sessions_spawn", "tool_use_id": "spawn",
                     "result": json.dumps({"session_key": "child-key", "task_id": "child"})}},
    ]


@pytest.mark.parametrize("fault", ["other_turn", "duplicate", "failed", "sequence", "identity"])
def test_spawn_receipt_requires_exact_parent_and_unambiguous_success(fault):
    records = _spawn_records()
    if fault == "other_turn":
        records[1]["turn_id"] = "unrelated"
    elif fault == "duplicate":
        records.append(copy.deepcopy(records[0]))
    elif fault == "failed":
        records[1]["payload"]["is_error"] = True
    elif fault == "sequence":
        records[1]["seq"] = 0
    else:
        records[1]["payload"]["result"] = '{"session_key": "child-key"}'
    with pytest.raises(live.CaseFailureError, match="plan_stop_child"):
        live._plan_stop_child_binding(records, "parent")


@pytest.mark.parametrize("command,allowed", [
    ("python3 child_wait.py", True),
    ("cd . && python3 child_wait.py", True),
    ("cat child_wait.py", False),
    ("python3 child_wait.py || true", False),
    ("python3 child_wait.py &", False),
    ("python3 other.py", False),
    ("cd .. && python3 child_wait.py", False),
    ("python3 child_wait.py\n", False),
    ("python3 child_wait.py\r", False),
    ("python3 $SCRIPT", False),
    ("python3 `echo child_wait.py`", False),
])
def test_execution_evidence_accepts_only_the_foreground_fixture(tmp_path, command, allowed):
    assert live._plan_stop_child_exec({"command": command}, tmp_path) is allowed


def _evidence_case(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "child_wait.py").write_text(live.CHILD_STOP_FIXTURE)
    (tmp_path / "child-started.txt").write_text("STARTED\n")
    records = _spawn_records() + [
        {"turn_id": "child", "kind": "tool_request", "seq": 1,
         "payload": {"name": "exec_command", "arguments": {"command": "python3 child_wait.py"}}},
    ]
    with sqlite3.connect(state / "ledger.db") as db:
        db.executescript("CREATE TABLE usage_events(turn_id);"
                         "CREATE TABLE agent_tasks(task_id,session_key,status,details);")
        db.executemany("INSERT INTO usage_events VALUES (?)", [("parent",), ("child",)])
        db.executemany("INSERT INTO agent_tasks VALUES (?,?,?,?)", [
            ("planning", "parent-key", "succeeded", "{}"),
            ("parent", "parent-key", "cancelled", "{}"),
            ("child", "child-key", "cancelled", json.dumps({"metadata": {
                "parent_task_id": "parent", "parent_session_key": "parent-key",
            }})),
        ])
    return SimpleNamespace(root=tmp_path, workspace=tmp_path, records=lambda: records)


@pytest.mark.parametrize("fault,failed_key", [
    ("none", None), ("wake", "exact_parent_turns"), ("lineage", "child_lineage_matches"),
    ("running", "child_cancelled"), ("missing_usage", "parent_and_child_have_usage"),
    ("finish", "child_finish_absent"), ("timeout", "child_timeout_absent"),
    ("fixture", "fixture_unchanged"), ("read_only", "one_real_child_execution"),
    ("replay", "started_once"),
])
def test_child_stop_oracle_rejects_false_cancellation_proof(tmp_path, fault, failed_key):
    case = _evidence_case(tmp_path)
    fixture = (tmp_path / "child_wait.py").read_bytes()
    with sqlite3.connect(tmp_path / "state" / "ledger.db") as db:
        if fault == "wake":
            db.execute("INSERT INTO agent_tasks VALUES ('wake','parent-key','queued','{}')")
        elif fault == "lineage":
            db.execute("UPDATE agent_tasks SET details='{}' WHERE task_id='child'")
        elif fault == "running":
            db.execute("UPDATE agent_tasks SET status='running' WHERE task_id='child'")
        elif fault == "missing_usage":
            db.execute("DELETE FROM usage_events WHERE turn_id='child'")
    if fault == "finish":
        (tmp_path / "child-finished.txt").write_text("FINISHED\n")
    elif fault == "timeout":
        (tmp_path / "child-timed-out.txt").write_text("TIMEOUT\n")
    elif fault == "fixture":
        (tmp_path / "child_wait.py").write_text("pass\n")
    elif fault == "read_only":
        case.records()[-1]["payload"]["arguments"]["command"] = "cat child_wait.py"
    elif fault == "replay":
        (tmp_path / "child-started.txt").write_text("STARTED\nSTARTED\n")
    proof = live.plan_stop_child_evidence(
        case, "parent-key", "child-key", "parent", "child", {"planning", "parent"}, fixture,
    )
    if failed_key:
        assert not proof[failed_key] and not all(proof.values())
    else:
        assert all(proof.values())
    assert "parent-key" not in json.dumps(proof) and "child-key" not in json.dumps(proof)


def test_child_stop_is_independent_and_uses_existing_limits():
    assert live.SCENARIOS["plan-stop-child"] == ("plan-stop-child",)
    assert live.SCENARIOS["plan-stop"] == ("plan-stop",)
    assert live.SCENARIOS["plan-recovery"] == ("plan-recovery",)
    assert live.LIMITS["physical_calls"] == 24
    assert live.LIMITS["root_turns"] == 4 and live.LIMITS["children"] == 2
    assert live.LIMITS["case_seconds"] == 600


async def test_stop_child_control_releases_only_after_both_cancelled(tmp_path, monkeypatch):
    # Synthetic orchestration only: this never enters run_case or reports a
    # provider pass. Real process cancellation is covered separately above.
    class ProtocolCase:
        check = live.LiveCase.check

        def __init__(self):
            self.root = self.workspace = tmp_path
            self.deadline = asyncio.get_running_loop().time() + 5
            self.assertions, self.evidence, self.calls = {}, {}, []
            self.phase = "planning"
            self.parent_checked = self.child_checked = False
            self.guard = SimpleNamespace(snapshot=lambda: {"counts": {
                "root_turns": 3, "children": 1,
            }})

        async def rpc(self, method, **params):
            self.calls.append(method)
            if method == "plans.setMode":
                return {}
            if method == "plans.implement":
                self.phase = "waiting"
                return {"turn_id": "parent"}
            assert method == "plans.cancelRun"
            assert self.phase == "waiting" and "pending" in self.calls
            assert (tmp_path / "child-started.txt").is_file()
            assert not (tmp_path / "child-release.txt").exists()
            assert params["runId"] == "run" and params["expectedStateRevision"] == 2
            self.phase = "cancelled"
            return {"planRun": {"status": "cancelled"}}

        async def send(self, key, _prompt):
            if self.phase == "planning":
                return "planning"
            assert self.parent_checked and self.child_checked
            assert (tmp_path / "child-release.txt").is_file()
            self.calls.append("followup")
            return "followup"

        async def done(self, key, task):
            return {"currentPlan": {"revisionId": "revision"}}

        async def pending(self, key, task):
            assert task == "parent"
            self.calls.append("pending")
            (tmp_path / "child-started.txt").write_text("STARTED\n")
            return {"request_id": "synthetic-question"}

        def records(self):
            return _spawn_records()

        async def subscribe(self, key):
            assert key == "child-key"

        async def snapshot(self, key):
            return {
                "tasks": [{"task_id": "child" if key == "child-key" else "parent",
                           "status": "cancelled" if self.phase == "cancelled" else "running"}],
                "activePlanRun": None if self.phase == "cancelled" else {
                    "runId": "run", "stateRevision": 2, "status": "running",
                    "activeTaskId": "parent",
                },
                "pendingUserInputs": [] if self.phase == "cancelled" else [{"id": "question"}],
                "active_task_group_ids": [],
            }

        async def until(self, key, predicate):
            snapshot = await self.snapshot(key)
            assert predicate(snapshot)
            if self.phase == "cancelled":
                assert not (tmp_path / "child-release.txt").exists()
                if key == "child-key":
                    self.child_checked = True
                else:
                    self.parent_checked = True
            return snapshot

        async def reconnect(self):
            assert self.parent_checked and self.child_checked
            self.calls.append("reconnect")

        async def answer(self, key, request):
            raise live.GatewayRPCError("chat.clarify_submit", code="USER_INPUT_EXPIRED")

    def evidence(case, key, child_key, parent, child, expected, fixture):
        assert parent == "parent" and child == "child" and child_key == "child-key"
        assert fixture == (tmp_path / "child_wait.py").read_bytes()
        assert expected == ({"planning", "parent", "followup"} if "followup" in case.calls
                            else {"planning", "parent"})
        return {"child_cancelled": case.phase == "cancelled", "fixture_unchanged": True}

    monkeypatch.setattr(live, "plan_stop_child_evidence", evidence)
    case = ProtocolCase()
    await live.plan_stop_child_case(case)
    assert all(case.assertions.values())
    assert case.calls.count("plans.cancelRun") == 1 and "chat.abort" not in case.calls
    assert case.calls.index("pending") < case.calls.index("plans.cancelRun")
    assert case.calls.index("plans.cancelRun") < case.calls.index("followup")
