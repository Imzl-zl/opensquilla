"""Offline evidence/control tests; they do not represent a live-provider result."""
from __future__ import annotations

import copy

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
    "response_before_request", "tool_error", "wrong_content", "reply_before_read",
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


class Case:
    def __init__(self, workspace):
        self.workspace = workspace
        self.assertions = {}
        self.evidence = {}
        self.sent = []
        self.calls = []
        self.completed = []
        self.rows = []
        self.reconnected = False
        self.finished_first = False
        self.answers = 0

    def check(self, name, value):
        self.assertions[name] = bool(value)
        assert value, name

    async def rpc(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if method == "chat.abort":
            self.finished_first = True
        return {}

    async def send(self, key, prompt):
        task = "task-" + str(len(self.sent))
        self.sent.append((key, prompt, task))
        return task

    async def pending(self, key, task):
        assert task == "task-0"
        return {"request_id": "question"}

    async def done(self, key, task):
        if task == "task-0":
            assert self.answers == 2
            self.finished_first = True
        else:
            # Prove the independent task completes before the original wait ends,
            # while the same-session task runs only after answer/cancel.
            assert self.finished_first == (task == "task-1")
            name = "wait-queued.txt" if task == "task-1" else "wait-independent.txt"
            path = self.workspace / name
            if path.exists():
                value = path.read_text(encoding="utf-8").strip()
                self.rows.extend(records(task=task, path=name, expected=value))
        self.completed.append(task)

    async def snapshot(self, key):
        return {
            "pendingUserInputs": [] if self.finished_first else [{"request_id": "question"}],
            "tasks": [
                {"task_id": "task-0", "status": "cancelled" if self.finished_first else "running"},
                {"task_id": "task-1", "status": "queued"},
            ],
        }

    async def reconnect(self):
        self.reconnected = True

    async def until(self, key, predicate):
        result = await self.snapshot(key)
        assert predicate(result)
        return result

    async def answer(self, key, request):
        assert self.reconnected
        self.answers += 1
        return {"replayed": self.answers == 2}

    def records(self):
        return self.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_wait_control_keeps_three_roots_and_original_fence_checks(live, tmp_path, cancel):
    case = Case(tmp_path)
    await live.wait_case(case, cancel=cancel)
    assert len(case.sent) == 3
    assert case.sent[0][0] == case.sent[1][0] != case.sent[2][0]
    assert case.completed == (["task-2", "task-1"] if cancel else ["task-2", "task-0", "task-1"])
    assert case.reconnected
    assert case.answers == (0 if cancel else 2)
    for task in case.sent[1:]:
        assert "read_file" in task[1]
        assert "new, independent task" in task[1]
        assert "does not inherit" in task[1]
        for path in tmp_path.glob("wait-*.txt"):
            assert path.read_text(encoding="utf-8").strip() not in task[1]
    assert all(case.assertions.values())
    assert {
        "other_session_runs_while_waiting", "same_session_remains_fenced",
        "pending_survives_reconnect", "independent_task_reads_and_reports_fixture",
        "queued_task_reads_and_reports_fixture",
        "cancel_removed_question" if cancel else "question_answer_idempotent",
    } <= case.assertions.keys()
    assert [name for name, _ in case.calls].count("chat.abort") == int(cancel)
    assert live.LIMITS["case_seconds"] == 600 and live.LIMITS["physical_calls"] == 24
