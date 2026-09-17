"""Offline oracles/protocol checks; no substituted live-provider verdicts."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import live_plan_goal_runtime as live


def _rows(turn: str, action: str, seq: int = 1) -> list[dict]:
    return [
        {
            "kind": "tool_request",
            "turn_id": turn,
            "seq": seq,
            "payload": {
                "name": "exec_command",
                "tool_use_id": action,
                "arguments": {"command": f"python3 effect_fixture.py {action}"},
            },
        },
        {
            "kind": "tool_response",
            "turn_id": turn,
            "seq": seq + 1,
            "payload": {
                "tool_use_id": action,
                "result": "exit_code=0\n"
                + ("RECORDED" if action == "record" else "FINISHED")
                + "\n",
            },
        },
    ]


async def _fixture(workspace: Path, action: str) -> int:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-s",
        "effect_fixture.py",
        action,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": os.defpath,
            **{key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ},
        },
    )
    try:
        async with asyncio.timeout(10):
            await process.communicate()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode is not None
    return process.returncode


async def test_effect_fixture_is_non_idempotent_and_replay_is_detectable(tmp_path):
    path = tmp_path / "effect_fixture.py"
    path.write_text(live.EFFECT_FIXTURE)
    fixture = path.read_bytes()
    assert await _fixture(tmp_path, "record") == 0
    proof = live.effect_evidence(_rows("first", "record"), tmp_path, fixture, "first")
    assert proof["record_succeeded_on_original_turn"]
    assert proof["receipt_exactly_once"] and proof["finish_marker_absent"]
    assert await _fixture(tmp_path, "record") == 0
    proof = live.effect_evidence(_rows("first", "record"), tmp_path, fixture, "first")
    assert not proof["receipt_exactly_once"]
    assert await _fixture(tmp_path, "finish") != 0
    assert not (tmp_path / "effect-finished.txt").exists()


async def test_effect_recovery_requires_both_real_actions_on_their_own_turns(tmp_path):
    path = tmp_path / "effect_fixture.py"
    path.write_text(live.EFFECT_FIXTURE)
    fixture = path.read_bytes()
    assert await _fixture(tmp_path, "record") == 0
    assert await _fixture(tmp_path, "finish") == 0
    records = _rows("old", "record") + _rows("new", "finish")
    proof = live.effect_evidence(records, tmp_path, fixture, "old", "new")
    assert proof["record_succeeded_on_original_turn"]
    assert proof["finish_succeeded_on_recovery_turn"] and proof["finish_exactly_once"]
    assert not live.effect_evidence(records, tmp_path, fixture, "new", "old")[
        "finish_succeeded_on_recovery_turn"
    ]
    records.extend(_rows("new", "record", 3))
    assert not live.effect_evidence(records, tmp_path, fixture, "old", "new")[
        "record_succeeded_on_original_turn"
    ]
    # Neither changing the fixture nor reading its source can satisfy the oracle.
    path.write_text("print('RECORDED')\n")
    records[0]["payload"]["arguments"]["command"] = "cat effect_fixture.py"
    proof = live.effect_evidence(records, tmp_path, fixture, "old", "new")
    assert not proof["fixture_unchanged"] and proof["opaque_exec_requests"] == 1


@pytest.mark.parametrize(
    "command",
    [
        "cat effect_fixture.py",
        "python3 effect_fixture.py record || true",
        "python3 effect_fixture.py record; echo RECORDED",
        "python3 -c 'print(1)'",
        "python3 other.py record",
        "python3 effect_fixture.py finish extra",
        "python3 effect_fixture.py record\n",
        "python3 effect_fixture.py $ACTION",
        "python3 `echo effect_fixture.py` record",
    ],
)
def test_effect_execution_proof_rejects_wrappers_and_non_fixture_commands(tmp_path, command):
    assert live._effect_command({"command": command}, tmp_path) is None


def test_effect_execution_proof_uses_actual_public_workdir(tmp_path):
    command = {"command": "python3 effect_fixture.py record", "workdir": str(tmp_path.parent)}
    assert live._effect_command(command, tmp_path) is None
    command["workdir"] = str(tmp_path)
    assert live._effect_command(command, tmp_path) == "record"


def test_opaque_execution_is_scoped_to_exact_implementation_and_recovery_tasks(tmp_path):
    records = _rows("original", "record")
    for turn in ("planning", "original", "recovery", "other-session"):
        records.append({"kind": "tool_request", "turn_id": turn, "seq": 5,
                        "payload": {"name": "exec_command",
                                    "arguments": {"command": "cat effect-receipt.txt"}}})
    proof = live.effect_evidence(records, tmp_path, b"", "original", "recovery")
    assert proof["opaque_exec_requests"] == 2
    assert proof["opaque_exec_outside_implementation"] == 2
    assert proof["opaque_exec_by_phase"] == {"original": 1, "recovery": 1, "other": 2}
    # Direct fixture actions remain global: another turn must not hide replay.
    records.extend(_rows("other-session", "record", 7))
    proof = live.effect_evidence(records, tmp_path, b"", "original", "recovery")
    assert proof["record_requests"] == 2 and not proof["record_succeeded_on_original_turn"]


@pytest.mark.parametrize("mutation", ["wrong_turn", "reversed_seq", "duplicate", "failed"])
def test_effect_response_proof_rejects_ambiguous_or_unsuccessful_results(tmp_path, mutation):
    records = _rows("old", "record")
    if mutation == "wrong_turn":
        records[1]["turn_id"] = "other"
    elif mutation == "reversed_seq":
        records[1]["seq"] = 0
    elif mutation == "duplicate":
        records.append(dict(records[1]))
    else:
        records[1]["payload"]["result"] = "exit_code=1\nRECORDED\n"
    proof = live.effect_evidence(records, tmp_path, b"", "old")
    assert not proof["record_succeeded_on_original_turn"]


def test_plan_suite_requires_running_stop_and_restart_cases():
    assert {"plan-stop", "plan-recovery"} <= set(live.SCENARIOS["plan"])
    assert live.SCENARIOS["plan-stop"] == ("plan-stop",)
    assert live.SCENARIOS["plan-recovery"] == ("plan-recovery",)
    assert live.LIMITS["physical_calls"] == 24
    assert live.LIMITS["root_turns"] == 4 and live.LIMITS["children"] == 2


class _ProtocolCase:
    """Synthetic RPC responses check orchestration only, never run_case/report success."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.assertions = {}
        self.evidence = {}
        self.calls = []
        self.state = "running"
        self.guard = SimpleNamespace(snapshot=lambda: {"counts": {"physical_calls": 3}})
        self.run = {
            "runId": "run",
            "planRevisionId": "revision",
            "status": "running",
            "activeTaskId": "old",
            "stateRevision": 2,
        }
        self.params = {
            "sessionKey": "synthetic",
            "planRevisionId": "revision",
            "clientRequestId": "old-nonce",
            "message": "original",
        }
        self.accepted = {"turn_id": "old", "planRun": dict(self.run)}
        self.records_rows = _rows("old", "record")
        (workspace / "effect_fixture.py").write_text(live.EFFECT_FIXTURE)
        (workspace / "effect-receipt.txt").write_text("EFFECT\n")
        (workspace / "effect-recorded.txt").write_text("RECORDED\n")

    check = live.LiveCase.check

    def records(self):
        return self.records_rows

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "plans.cancelRun":
            self.state = "cancelled"
            self.run.update(status="cancelled", activeTaskId=None, stateRevision=3)
            return {"planRun": dict(self.run)}
        assert method == "plans.implement"
        if params["clientRequestId"] == "old-nonce":
            return {**self.accepted, "replayed": True}
        replayed = (
            sum(p.get("clientRequestId") == params["clientRequestId"] for _, p in self.calls) > 1
        )
        if self.state != "completed":
            self.state = "running"
            self.run.update(status="running", activeTaskId="new")
        return {"turn_id": "new", "planRun": dict(self.run), "replayed": replayed}

    async def snapshot(self, key):
        return {
            "currentPlan": {"revisionId": "revision"},
            "activePlanRun": None if self.state in {"completed", "cancelled"} else dict(self.run),
            "tasks": [
                {
                    "task_id": "old",
                    "status": "cancelled" if self.state == "cancelled" else "abandoned",
                }
            ],
            "pendingUserInputs": [],
        }

    async def reconnect(self):
        self.calls.append(("reconnect", {}))

    async def until(self, key, predicate):
        snapshot = await self.snapshot(key)
        assert predicate(snapshot)
        return snapshot

    async def answer(self, key, request):
        self.calls.append(("answer", {}))
        raise live.GatewayRPCError("chat.clarify_submit", code="USER_INPUT_EXPIRED")

    async def send(self, key, message):
        self.calls.append(("send", {}))
        return "followup"

    async def done(self, key, turn):
        if turn == "new":
            self.state = "completed"
            self.run.update(status="completed", activeTaskId=None)
            (self.workspace / "effect-finished.txt").write_text("FINISHED\n")
            self.records_rows.extend(_rows("new", "finish"))
        return await self.snapshot(key)

    async def stop(self, *, crash=False):
        assert crash
        self.calls.append(("stop", {"crash": crash}))

    async def start(self):
        self.calls.append(("start", {}))
        self.state = "paused"
        self.run.update(status="paused", pauseReason="process_restart", activeTaskId=None)


@pytest.mark.parametrize("extra,expected", [
    (None, None),
    ("read_file", None),
    ("inspect_original", "effect_no_opaque_exec_before_interrupt"),
    ("inspect_planning", None),
    ("unknown_command", "effect_no_opaque_exec_before_interrupt"),
    ("finish", "effect_finish_not_started_before_interrupt"),
    ("record", "side_effect_recorded_once_before_interrupt"),
])
async def test_effect_barrier_distinguishes_observation_from_replay_or_early_finish(
    tmp_path, extra, expected,
):
    class BarrierCase(_ProtocolCase):
        async def rpc(self, method, **params):
            assert method == "plans.implement"
            self.calls.append((method, params))
            return self.accepted

        async def pending(self, key, task):
            assert task == "old"
            return {"request_id": "pending"}

        async def snapshot(self, key):
            return {"activePlanRun": dict(self.run),
                    "tasks": [{"task_id": "old", "status": "running"}]}

    case = BarrierCase(tmp_path)
    fixture = (tmp_path / "effect_fixture.py").read_bytes()
    if extra in {"finish", "record"}:
        case.records_rows.extend(_rows("old", extra, 3))
    elif extra:
        case.records_rows.append({
            "kind": "tool_request", "turn_id": "planning" if extra == "inspect_planning"
            else "old", "seq": 3, "payload": {
                "name": "read_file" if extra == "read_file" else "exec_command",
                "arguments": {"command": "cat effect-receipt.txt" if extra.startswith("inspect")
                              else "python3 -c 'print(1)'"},
            },
        })
    if expected:
        with pytest.raises(live.CaseFailureError, match=f"^{expected}$"):
            await live._hold_effect_implementation(case, "synthetic", "revision", fixture)
        if expected == "effect_no_opaque_exec_before_interrupt":
            assert case.assertions["side_effect_recorded_once_before_interrupt"]
            assert case.assertions["effect_finish_not_started_before_interrupt"]
            proof = case.evidence["effect_before_interrupt"]
            assert proof["opaque_exec_requests"] == 1
            phase = "other" if extra == "inspect_planning" else "original"
            assert proof["opaque_exec_by_phase"][phase] == 1
    else:
        await live._hold_effect_implementation(case, "synthetic", "revision", fixture)
        assert all(case.assertions.values())
        if extra == "inspect_planning":
            proof = case.evidence["effect_before_interrupt"]
            assert proof["opaque_exec_requests"] == 0
            assert proof["opaque_exec_outside_implementation"] == 1
    assert "read_file or list_dir" in case.calls[0][1]["message"]


@pytest.mark.parametrize("scenario", ["stop", "recovery"])
async def test_plan_interrupt_orchestration_uses_public_stop_or_idempotent_resume(
    tmp_path, monkeypatch, scenario
):
    case = _ProtocolCase(tmp_path)

    async def prepare(case, key):
        return "revision", (tmp_path / "effect_fixture.py").read_bytes()

    async def hold(case, key, revision, fixture):
        return case.params, case.accepted, {"request_id": "expired"}

    monkeypatch.setattr(live, "_prepare_effect_plan", prepare)
    monkeypatch.setattr(live, "_hold_effect_implementation", hold)
    if scenario == "stop":
        await live.plan_stop_case(case)
        assert [m for m, _ in case.calls].count("plans.cancelRun") == 2
        assert all(m != "chat.abort" for m, _ in case.calls)
    else:
        await live.plan_recovery_case(case)
        methods = [m for m, _ in case.calls]
        assert methods.index("stop") < methods.index("start") < methods.index("plans.implement")
        requests = [p for m, p in case.calls if m == "plans.implement"]
        assert requests[0] == case.params
        assert requests[1] == requests[2] == requests[3]
        assert requests[1]["clientRequestId"] != requests[0]["clientRequestId"]
        assert case.evidence["recovery_fault"] == "gateway_process_kill_after_completed_tool"
    assert all(case.assertions.values())
    assert "synthetic" not in json.dumps(case.evidence)
