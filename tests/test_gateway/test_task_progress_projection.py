from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_sessions import _task_summary


def test_task_summary_projects_common_progress_without_unrelated_task_metadata():
    progress = {
        "revision": 2,
        "explanation": "Inspection is complete.",
        "steps": [{"step": "Inspect files", "status": "completed"}],
    }
    result = _task_summary(SimpleNamespace(
        task_id="ordinary-task",
        status="running",
        details={"metadata": {"progress": progress, "internal_context": "private"}},
    ))

    assert result["task_id"] == "ordinary-task"
    assert result["progress"] == progress
    assert "metadata" not in result
    assert "internal_context" not in result


@pytest.mark.parametrize("status", ["submitted", "discussion"])
def test_task_summary_projects_plan_result_with_only_public_revision_fields(status):
    result = _task_summary(SimpleNamespace(details={"metadata": {"plan_result": {
        "status": status,
        "previousRevisionId": None,
        "revisionId": "revision-current",
        "internal_context": "private",
    }}}))

    assert result["plan_result"] == {
        "status": status, "previousRevisionId": None, "revisionId": "revision-current",
    }


def test_task_summary_ignores_unknown_plan_result_status():
    result = _task_summary(SimpleNamespace(details={"metadata": {
        "plan_result": {"status": "future", "revisionId": "revision-current"},
    }}))
    assert "plan_result" not in result
