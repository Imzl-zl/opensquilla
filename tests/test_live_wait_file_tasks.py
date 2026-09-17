"""Offline evidence/control tests; they do not represent a live-provider result."""
from __future__ import annotations

import copy
from unittest.mock import create_autospec

import pytest


@pytest.fixture
def live():
    from scripts import live_plan_goal_runtime

    return live_plan_goal_runtime


def records(task="task", path="wait.txt", expected="SYNTHETIC_VALUE"):
    return [
        {"turn_id": task, "seq": 2, "kind": "tool_request", "payload": {
            "name": "read_file", "tool_use_id": "read", "arguments": {"path": path},
        }},
        {"turn_id": task, "seq": 3, "kind": "tool_response", "payload": {
            "name": "read_file", "tool_use_id": "read", "is_error": False,
            "result": "1 | " + expected + "\n",
        }},
        {"turn_id": task, "seq": 5, "kind": "llm_response", "payload": {
            "text": "The file contains: " + expected, "got_done_event": True,
            "tool_calls": [],
        }},
    ]


@pytest.mark.parametrize("absolute", [False, True])
def test_read_evidence_requires_owned_pair_and_later_reply(live, tmp_path, absolute):
    target = tmp_path / "wait.txt"
    target.write_text("SYNTHETIC_VALUE\n", encoding="utf-8")
    rows = records(path=str(target) if absolute else target.name)
    proof = live.wait_file_evidence(
        list(reversed(rows)), "task", tmp_path, target.name, "SYNTHETIC_VALUE",
    )
    assert proof == {
        "read_file_observed": True,
        "reply_uses_observed_value": True,
        "fixture_unchanged": True,
    }


@pytest.mark.parametrize("mutation", [
    "wrong_turn", "wrong_path", "wrong_tool", "unpaired", "missing_id",
    "response_before_request", "duplicate_response", "wrong_response_tool", "tool_error",
    "wrong_content", "reply_before_read",
    "reply_unrelated", "reply_still_tool_call", "reply_incomplete", "later_reply_unrelated",
    "fixture_changed",
])
def test_unrelated_or_incomplete_evidence_cannot_pass(live, tmp_path, mutation):
    target = tmp_path / "wait.txt"
    target.write_text("SYNTHETIC_VALUE\n", encoding="utf-8")
    rows = records()
    if mutation == "wrong_turn":
        rows[1]["turn_id"] = "different-task"
    elif mutation == "wrong_path":
        rows[0]["payload"]["arguments"]["path"] = "other.txt"
    elif mutation == "wrong_tool":
        rows[0]["payload"]["name"] = "exec_command"
    elif mutation == "unpaired":
        rows[1]["payload"]["tool_use_id"] = "different-read"
    elif mutation == "missing_id":
        rows[0]["payload"].pop("tool_use_id")
        rows[1]["payload"].pop("tool_use_id")
    elif mutation == "response_before_request":
        rows[1]["seq"] = 1
    elif mutation == "duplicate_response":
        rows.append(copy.deepcopy(rows[1]))
    elif mutation == "wrong_response_tool":
        rows[1]["payload"]["name"] = "exec_command"
    elif mutation == "tool_error":
        rows[1]["payload"]["is_error"] = True
    elif mutation == "wrong_content":
        rows[1]["payload"]["result"] = "some other content"
    elif mutation == "reply_before_read":
        rows[2]["seq"] = 1
    elif mutation == "reply_unrelated":
        rows[2]["payload"]["text"] = "OTHER"
    elif mutation == "reply_still_tool_call":
        rows[2]["payload"]["tool_calls"] = [{"name": "request_user_input"}]
    elif mutation == "reply_incomplete":
        rows[2]["payload"]["got_done_event"] = False
    elif mutation == "later_reply_unrelated":
        later = copy.deepcopy(rows[2])
        later["seq"] = 6
        later["payload"]["text"] = "I could not read the file."
        rows.append(later)
    else:
        target.write_text("CHANGED\n", encoding="utf-8")
    proof = live.wait_file_evidence(rows, "task", tmp_path, target.name, "SYNTHETIC_VALUE")
    assert not all(proof.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_wait_control_keeps_three_roots_and_original_fence_checks(live, tmp_path, cancel):
    # Script only the public responses needed by this orchestration check.
    case = create_autospec(live.LiveCase, instance=True)
    case.workspace, case.assertions, case.evidence = tmp_path, {}, {}
    case.check.side_effect = lambda name, value: live.LiveCase.check(case, name, value)
    case.send.side_effect = ["task-0", "task-1", "task-2"]
    case.records.return_value = []
    finished_first = False

    def pending(_key, task):
        assert task == "task-0"
        return {"request_id": "question"}

    def answer(_key, _request):
        case.reconnect.assert_awaited_once()
        return {"replayed": case.answer.await_count == 2}

    def rpc(method, **_params):
        nonlocal finished_first
        if method == "chat.abort":
            finished_first = True
        return {}

    def snapshot(_key):
        return {
            "pendingUserInputs": [] if finished_first else [{"request_id": "question"}],
            "tasks": [
                {"task_id": "task-0", "status": "cancelled" if finished_first else "running"},
                {"task_id": "task-1", "status": "queued"},
            ],
        }

    def until(key, predicate):
        result = snapshot(key)
        assert predicate(result)
        return result

    def done(_key, task):
        nonlocal finished_first
        if task == "task-0":
            assert case.answer.await_count == 2
            finished_first = True
        else:
            assert finished_first == (task == "task-1")
            name = "wait-queued.txt" if task == "task-1" else "wait-independent.txt"
            value = (tmp_path / name).read_text(encoding="utf-8").strip()
            case.records.return_value.extend(records(task=task, path=name, expected=value))

    case.rpc.side_effect, case.snapshot.side_effect = rpc, snapshot
    case.until.side_effect, case.done.side_effect = until, done
    case.pending.side_effect, case.answer.side_effect = pending, answer
    await live.wait_case(case, cancel=cancel)
    sent = [call.args for call in case.send.await_args_list]
    assert len(sent) == 3 and sent[0][0] == sent[1][0] != sent[2][0]
    completed = [call.args[1] for call in case.done.await_args_list]
    assert completed == (["task-2", "task-1"] if cancel else ["task-2", "task-0", "task-1"])
    case.reconnect.assert_awaited_once()
    assert case.answer.await_count == (0 if cancel else 2)
    for _key, prompt in sent[1:]:
        for path in tmp_path.glob("wait-*.txt"):
            assert path.read_text(encoding="utf-8").strip() not in prompt
    assert {
        "other_session_runs_while_waiting", "same_session_remains_fenced",
        "pending_survives_reconnect", "independent_task_reads_and_reports_fixture",
        "queued_task_reads_and_reports_fixture",
        "cancel_removed_question" if cancel else "question_answer_idempotent",
    } <= case.assertions.keys()
    assert all(case.assertions.values())
    assert sum(call.args[0] == "chat.abort" for call in case.rpc.await_args_list) == int(cancel)
