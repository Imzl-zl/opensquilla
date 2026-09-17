"""Offline safety/protocol tests; these never substitute a live-provider pass."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from pathlib import Path

import httpx
import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.user_input_broker import validate_user_input_fields
from scripts import live_plan_goal_runtime as live


@pytest.fixture(autouse=True)
def restore_disconnect_instrumentation(monkeypatch):
    from opensquilla.gateway.websocket import ConnectionRegistry

    # install_dispatch_guard wraps this process-wide boundary only in the
    # isolated Gateway; unit tests must restore it along with the HTTP hooks.
    monkeypatch.setattr(ConnectionRegistry, "unregister", ConnectionRegistry.unregister)


def _request(model: str = "deepseek-chat", **kwargs: object) -> bytes:
    return json.dumps({"model": model, "max_tokens": 4096, **kwargs}).encode()


def _guard(tmp_path: Path) -> live.DispatchGuard:
    return live.DispatchGuard(tmp_path / "guard.sqlite", provider="deepseek", model="deepseek-chat")


def test_guard_and_projection_close_sqlite_handles_without_garbage_collection(
    tmp_path, monkeypatch,
):
    # Keep strong references so CPython cannot hide a leaked Windows file lock
    # with reference counting or a later garbage collection cycle.
    opened = []
    connect = sqlite3.connect

    def tracked_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        opened.append(db)
        return db

    monkeypatch.setattr(live.sqlite3, "connect", tracked_connect)
    guard = _guard(tmp_path)
    guard.claim("root_turns", 1, turn_id="synthetic")
    with pytest.raises(live.DispatchLimitError):
        guard.claim("root_turns", 1, turn_id="over-limit")
    with pytest.raises(RuntimeError, match="rollback"):
        with guard.connect() as db:
            db.execute("UPDATE counters SET value=99 WHERE name='root_turns'")
            raise RuntimeError("rollback")
    assert guard.snapshot()["counts"] == {"root_turns": 1}
    guard.path.unlink()

    with closing(connect(tmp_path / "sessions.db")) as db, db:
        db.execute(
            "CREATE TABLE usage_events (status, input_tokens, output_tokens, "
            "reasoning_tokens, total_tokens)"
        )
        db.execute("INSERT INTO usage_events VALUES ('finalized',1,2,0,3)")
    assert live.ledger_projection(tmp_path)["rows"][0]["total"] == 3
    # Include a failing read path, which must release its handle as well.
    with pytest.raises(sqlite3.OperationalError):
        live.child_usage_evidence(tmp_path, "synthetic")
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
    (tmp_path / "sessions.db").unlink()


def test_gateway_child_has_owned_home_for_windows_and_posix(tmp_path, monkeypatch):
    import ntpath
    import subprocess
    import sys

    env = live.child_environment("deepseek", {}, base_environment={})
    for name in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        monkeypatch.delenv(name, raising=False)
    assert ntpath.expanduser("~") == "~"  # the old minimal Windows child environment
    owned = live.isolated_user_environment(tmp_path)
    env.update(owned)
    for name, value in owned.items():
        monkeypatch.setenv(name, value)
    assert ntpath.expanduser("~") == str(tmp_path / "user-home")
    assert not any("KEY" in name or "TOKEN" in name for name in env)
    assert all(Path(owned[name]).is_relative_to(tmp_path)
               for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"))
    # Real child interpreter resolution covers Path.home(), not only env shape.
    child_env = live.minimal_child_environment()
    child_env.update(owned)
    result = subprocess.run(
        [sys.executable, "-c", "from pathlib import Path; print(Path.home())"],
        env=child_env, capture_output=True, text=True, timeout=10, check=True,
    )
    assert result.stdout.strip() == str(tmp_path / "user-home")


def test_gateway_startup_diagnostics_preserve_only_machine_categories(tmp_path):
    (tmp_path / "gateway.stderr.log").write_text(
        "Traceback (most recent call last):\n"
        "RuntimeError: private prompt and key material\n"
        "ValueError: private path\n", encoding="utf-8",
    )
    assert live.gateway_startup_diagnostics(tmp_path, 1) == {
        "phase": "early_exit", "exit_code": 1,
        "exception_types": ["RuntimeError", "ValueError"],
    }


@pytest.mark.parametrize("provider", tuple(live.MODELS))
def test_config_uses_selected_real_deployment_and_normal_runtime(
    provider: str, tmp_path: Path
) -> None:
    rendered = live.render_config(provider, live.MODELS[provider], tmp_path / "workspace")
    cfg = GatewayConfig(**tomllib.loads(rendered))
    assert cfg.llm.provider == provider
    assert cfg.llm.model == live.MODELS[provider]
    assert cfg.llm.base_url == live.registry_endpoint(provider)
    assert cfg.llm.max_tokens == 4096
    assert cfg.llm_request_timeout_seconds == cfg.agent_request_timeout_seconds == 90
    assert cfg.task_runtime.max_concurrency == 1
    assert cfg.agent_max_provider_retries == 1  # the guard bounds retries across turns
    assert not cfg.squilla_router.enabled and not cfg.llm_ensemble.enabled
    assert cfg.sandbox.run_mode == "safe"
    assert "submit_plan" in cfg.tools.allow and "create_goal" in cfg.tools.allow
    assert "provider_response" not in rendered


def test_thinking_setting_keeps_model_and_execution_bounds(tmp_path: Path) -> None:
    cfg = GatewayConfig(**tomllib.loads(live.render_config(
        "openrouter", live.MODELS["openrouter"], tmp_path, thinking="high",
    )))
    assert cfg.llm.thinking == "high"
    assert cfg.llm.model == live.MODELS["openrouter"]
    assert cfg.llm.max_tokens == live.LIMITS["output_tokens"]
    assert cfg.agent_request_timeout_seconds == live.LIMITS["request_seconds"]
    with pytest.raises(ValueError, match="thinking"):
        live.render_config("openrouter", live.MODELS["openrouter"], tmp_path, thinking="unknown")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("15", True),
        ("15.0\n", True),
        (" 1.5e1 ", True),
        ("14", False),
        ("15.00000000000000000001", False),
        ("14.99999999999999999999", False),
        ("NaN", False),
        ("Infinity", False),
        ("", False),
        ("The sum is 15", False),
        ("15\n0", False),
    ],
)
def test_child_sum_accepts_equal_numbers_without_rounding_or_extracting_prose(value, expected):
    # The workload asks for the sum, not a particular integer/decimal spelling.
    assert live.numeric_result_matches(value, 15) is expected


def test_physical_limit_is_atomic_and_survives_restart(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    url = guard.endpoint + "/chat/completions"

    def claim(_: int) -> bool:
        try:
            guard.request("POST", url, _request())
        except live.DispatchLimitError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(claim, range(40))) == 24
    restarted = _guard(tmp_path)
    with pytest.raises(live.DispatchLimitError, match="physical_calls_limit"):
        restarted.request("POST", url, _request())
    assert restarted.snapshot()["counts"]["physical_calls"] == 24


@pytest.mark.parametrize("name,limit", [("root_turns", 4), ("children", 2)])
def test_task_budget_deduplicates_same_task_and_bounds_all_turns(
    tmp_path: Path,
    name: str,
    limit: int,
) -> None:
    guard = _guard(tmp_path)
    for index in range(limit):
        assert guard.claim(name, limit, turn_id=str(index)) == index + 1
        assert guard.claim(name, limit, turn_id=str(index)) == 0
    with pytest.raises(live.DispatchLimitError):
        guard.claim(name, limit, turn_id="excess")


@pytest.mark.parametrize(
    "url,body,reason",
    [
        ("https://unselected.invalid/v1/chat/completions", _request(), "unselected_provider"),
        ("/chat/completions", _request("foreign-model"), "unselected_model"),
        ("/chat/completions", _request(max_tokens=4097), "output_limit"),
        ("/chat/completions", _request(max_tokens=True), "output_limit"),
        ("/chat/completions", _request(n=2), "multiple_completions"),
        ("/images/generations", _request(), "unreviewed_provider"),
    ],
)
def test_unreviewed_request_never_consumes_dispatch(
    tmp_path: Path,
    url: str,
    body: bytes,
    reason: str,
) -> None:
    guard = _guard(tmp_path)
    url = url if url.startswith("https:") else guard.endpoint + url
    with pytest.raises(live.DispatchLimitError, match=reason):
        guard.request("POST", url, body)
    assert guard.snapshot()["counts"] == {}


async def test_missing_credential_does_not_start_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: object) -> None:
        raise AssertionError("must not launch gateway")

    monkeypatch.setattr(live, "run_case", forbidden)
    result = await live.run("deepseek", "all", environment={"OPENROUTER_API_KEY": "unused"})
    assert result["status"] == "not_run"
    assert result["failure_class"] == "missing-credential"
    assert not result["cases"]


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (401, b"invalid api key", 1),
        (402, b"insufficient balance", 1),
        (429, b"quota exceeded", 1),
        (500, b"internal server error", 2),
        (429, b"rate limit", 2),
    ],
)
async def test_transport_guard_allows_only_one_production_transient_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    status: int,
    body: bytes,
    expected: int,
) -> None:
    # This tests the safety wrapper in isolation. No scenario or live verdict is run.
    from opensquilla.gateway.task_runtime import TaskRuntime
    from opensquilla.observability.turn_call_log import TurnCallLogger

    calls: list[str] = []

    async def original(_transport: object, request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, content=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", original)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", httpx.HTTPTransport.handle_request)
    monkeypatch.setattr(TaskRuntime, "_mark_running", TaskRuntime._mark_running)
    monkeypatch.setattr(TurnCallLogger, "write", TurnCallLogger.write)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-key")
    guard = _guard(tmp_path)
    live.install_dispatch_guard(guard)
    async with httpx.AsyncClient() as client:
        response = await client.post(guard.endpoint + "/chat/completions", content=_request())
        assert len(calls) == 1  # wrapper never synthesizes its own attempt
        if expected == 2:
            await client.post(guard.endpoint + "/chat/completions", content=_request())
        with pytest.raises(live.DispatchLimitError):
            await client.post(guard.endpoint + "/chat/completions", content=_request())
    assert response.status_code == status
    assert len(calls) == expected
    assert guard.snapshot()["counts"]["physical_calls"] == expected


def test_error_events_inside_successful_http_stream_share_retry_budget(tmp_path: Path) -> None:
    guard = _guard(tmp_path)
    url = guard.endpoint + "/chat/completions"
    first = guard.request("POST", url, _request())
    guard.finish(first, 200)
    guard.logical_failure("rate_limit")
    second = guard.request("POST", url, _request())
    guard.finish(second, 200)
    guard.logical_failure("rate_limit")
    with pytest.raises(live.DispatchLimitError, match="transient_retry_limit"):
        guard.request("POST", url, _request())
    assert guard.snapshot()["counts"]["physical_calls"] == 2


def test_provider_projection_excludes_text_arguments_and_unknown_models() -> None:
    records = [
        {
            "kind": "llm_request",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "payload": {"messages": [{"content": "private prompt"}]},
        },
        {
            "kind": "llm_response",
            "payload": {
                "text": "private reply",
                "usage": {"provider": "deepseek", "model": "deepseek-v4-flash"},
                "tool_calls": [{"name": "read_file", "arguments": {"path": "/private/path"}}],
            },
        },
    ]
    result = live.provider_evidence(records, "deepseek", "deepseek-chat")
    assert result["request_identity_matches"] and result["response_identity_matches"]
    assert result["response_models"] == ["deepseek-v4-flash"]
    assert "private" not in json.dumps(result)
    records[1]["payload"]["usage"]["model"] = "foreign-model"
    result = live.provider_evidence(records, "deepseek", "deepseek-chat")
    assert not result["response_identity_matches"] and result["response_models"] == []


async def test_answer_matches_production_questionnaire_protocol(tmp_path: Path) -> None:
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    request = {
        "request_id": "synthetic-request",
        "clarify_schema": {
            "fields": [
                {"name": "color", "type": "enum", "choices": ["Blue", "Green"], "required": True},
            ]
        },
    }
    captured: dict[str, object] = {}

    async def capture(method: str, **params: object) -> dict:
        captured.update({"method": method, **params})
        return {}

    case.rpc = capture
    await case.answer("agent:main:webchat:synthetic", request)
    assert captured["method"] == "chat.clarify_submit"
    assert validate_user_input_fields(request, captured["fields"]) == {"color": "Blue"}


def test_ledger_projection_contains_accounting_only(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "sessions.db") as db:
        db.execute(
            "CREATE TABLE usage_events (status, input_tokens, output_tokens, "
            "reasoning_tokens, total_tokens, session_id)"
        )
        db.execute("INSERT INTO usage_events VALUES ('finalized',10,4,2,16,'private-id')")
    result = live.ledger_projection(tmp_path)
    assert result == {
        "rows": [
            {
                "status": "finalized",
                "calls": 1,
                "input": 10,
                "output": 4,
                "reasoning": 2,
                "total": 16,
            }
        ]
    }
    assert "private-id" not in json.dumps(result)


def test_child_usage_proof_detects_omitted_and_double_counted_child(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "sessions.db") as db:
        db.execute(
            "CREATE TABLE usage_events (turn_id,root_turn_id,parent_turn_id,status,"
            "input_tokens,output_tokens,cache_read_tokens,total_tokens,goal_id)"
        )
        db.execute("CREATE TABLE session_goals (goal_id,budget_tokens_used,total_tokens)")
        db.executemany(
            "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("root", "root", None, "finalized", 20, 3, 5, 23, "private-goal"),
                ("child", "root", "root", "finalized", 10, 5, 2, 15, "private-goal"),
                ("other", "other", None, "finalized", 100, 100, 0, 200, "other-goal"),
            ],
        )
        db.execute("INSERT INTO session_goals VALUES ('private-goal',31,38)")
    proof = live.child_usage_evidence(tmp_path, "private-goal")
    assert proof["root_calls"] == proof["child_calls"] == 1
    assert proof["physical_budget_tokens"] == proof["goal_budget_tokens"] == 31
    assert proof["physical_total_tokens"] == proof["goal_total_tokens"] == 38
    assert proof["root_budget_tokens"] == 18 and proof["child_budget_tokens"] == 13
    assert proof["lineage_present"] and proof["all_calls_finalized"]
    assert "private-goal" not in json.dumps(proof)
    for broken_total in (18, 44):
        with sqlite3.connect(tmp_path / "sessions.db") as db:
            db.execute("UPDATE session_goals SET budget_tokens_used=?", (broken_total,))
        proof = live.child_usage_evidence(tmp_path, "private-goal")
        assert proof["physical_budget_tokens"] != proof["goal_budget_tokens"]


def test_goal_suite_requires_real_child_case() -> None:
    assert "childbudget" in live.SCENARIOS["goal"]
    assert {"sessions_spawn", "sessions_yield"} <= set(live.ALLOWED_TOOLS)
    assert live.MODELS["tokenrhythm"] == "deepseek-v4-pro-0813"
    assert live.MODELS["deepseek"] == "deepseek-flash"


def test_child_environment_drops_other_keys_and_injection() -> None:
    env = live.child_environment(
        "deepseek",
        {"DEEPSEEK_API_KEY": "synthetic-key"},
        base_environment={
            "PATH": os.defpath,
            "HOME": "/private-home",
            "PYTHONPATH": "injected",
            "HTTPS_PROXY": "https://proxy.invalid",
            "OPENROUTER_API_KEY": "unselected",
            "OPENSQUILLA_LLM_MODEL": "foreign",
        },
    )
    assert env == {
        "PATH": os.defpath,
        "DEEPSEEK_API_KEY": "synthetic-key",
        "OPENSQUILLA_LIVE_DISABLE_DOTENV": "1",
    }


async def test_real_gateway_boot_and_control_rpc_without_provider_credentials() -> None:
    # Real subprocess/CLI/WS/storage preflight, no inference. The installed
    # transport guard rejects every network request when the provider key is absent.
    root = Path(tempfile.mkdtemp(prefix="opensquilla-live-runtime-offline-"))
    case = live.LiveCase(root, "deepseek", "deepseek-chat", {})
    try:
        async with asyncio.timeout(60):
            try:
                await case.start()
            except live.CaseFailureError as exc:
                raise AssertionError(case.evidence.get("gateway_startup", {})) from exc
            ledger = live.usage_ledger_path(root / "state")
            assert ledger == root / "state" / "state" / "sessions.db"
            assert live.ledger_projection(root / "state") == {"rows": []}
            key = "agent:main:webchat:offline-control"
            changed = await case.rpc(
                "plans.setMode", sessionKey=key, mode="plan", expectedRevision=0
            )
            assert changed["collaboration"]["mode"] == "plan"
            await case.subscribe(key)
            await case.reconnect()
            snapshot = await case.snapshot(key)
            assert snapshot["collaboration"]["mode"] == "plan"
            assert snapshot["pendingUserInputs"] == []
            assert case.guard.snapshot()["counts"].get("physical_calls", 0) == 0
    finally:
        shutdown = await case.stop()
        live.scan_and_remove_temporary_tree(root, {})
        assert shutdown == {"forced": False, "process_exited": True}


async def test_done_accepts_production_succeeded_task_status(tmp_path, monkeypatch):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-v4-flash", {})
    snapshot = {"tasks": [{"task_id": "synthetic", "status": "succeeded"}]}

    async def hydrate(_key):
        return snapshot

    monkeypatch.setattr(case, "snapshot", hydrate)
    async with asyncio.timeout(1):
        assert await case.done("synthetic", "synthetic") == snapshot
    assert "succeeded" in live.TERMINAL


async def test_send_explicitly_queues_instead_of_inheriting_default_steer(tmp_path, monkeypatch):
    from opensquilla.session.models import SessionNode

    assert SessionNode(session_key="synthetic", agent_id="main").queue_mode == "steer"
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-v4-flash", {})
    calls = []

    async def rpc(method, **params):
        calls.append((method, params))
        return {"task_id": "synthetic-task"}

    monkeypatch.setattr(case, "rpc", rpc)
    assert await case.send("synthetic", "Synthetic request") == "synthetic-task"
    assert calls[-1][0] == "chat.send"
    assert calls[-1][1]["intent"] == "continue"
    assert calls[-1][1]["queueMode"] == "followup"


async def test_missing_questionnaire_fails_immediately_after_successful_plain_answer(
    tmp_path,
    monkeypatch,
):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-v4-flash", {})

    async def snapshot(_key):
        return {"pendingUserInputs": [], "tasks": [{"status": "succeeded"}]}

    monkeypatch.setattr(case, "snapshot", snapshot)
    with pytest.raises(live.CaseFailureError, match="questionnaire_requested"):
        async with asyncio.timeout(1):
            await case.pending("synthetic")


def test_runtime_diagnostics_keep_machine_evidence_without_private_content(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with sqlite3.connect(tmp_path / "sessions.db") as db:
        db.executescript(
            "CREATE TABLE usage_events (event_id TEXT);"
            "CREATE TABLE agent_tasks (status TEXT,queue_mode TEXT,run_kind TEXT,"
            "terminal_reason TEXT,error_class TEXT,details TEXT,created_at INTEGER);"
        )
        db.execute(
            "INSERT INTO agent_tasks VALUES (?,?,?,?,?,?,1)",
            (
                "failed",
                "followup",
                "web_turn",
                "error",
                "PermissionError",
                json.dumps({"message": "PRIVATE PROMPT", "turn_outcome": {"code": "TOOL_DENIED"}}),
            ),
        )
    records = [
        {
            "kind": "llm_request",
            "payload": {
                "messages": [{"content": f"PRIVATE PROMPT in {workspace}"}],
                "tools": [{"name": "read_file"}, {"name": "private-tool-name"}],
            },
        },
        {
            "kind": "llm_response",
            "payload": {
                "tool_calls": [
                    {"name": "read_file", "arguments": {"path": "fixture.txt"}},
                ]
            },
        },
        {
            "kind": "tool_response",
            "payload": {
                "name": "read_file",
                "is_error": True,
                "result": json.dumps(
                    {
                        "status": "error",
                        "code": "TOOL_DENIED",
                        "reason": "PRIVATE ERROR MESSAGE",
                        "content": "PRIVATE CONTENT",
                    }
                ),
            },
        },
    ]
    diagnostic = live.runtime_diagnostics(tmp_path, records, workspace)
    assert diagnostic["tasks"][0]["error_class"] == "PermissionError"
    assert diagnostic["tasks"][0]["outcome_code"] == "TOOL_DENIED"
    assert diagnostic["requests"] == [
        {
            "tools": ["read_file"],
            "other_tool_count": 1,
            "workspace_context_present": True,
        }
    ]
    assert diagnostic["tool_results"][0]["code"] == "TOOL_DENIED"
    assert diagnostic["file_tool_workspace_hits"] == [True]
    encoded = json.dumps(diagnostic)
    assert "PRIVATE" not in encoded and "private-tool-name" not in encoded
    assert str(workspace) not in encoded


async def test_case_report_is_saved_before_starting_next_case(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    reports = []

    async def run_case(name, provider, model, secrets):
        if reports:
            assert reports[-1]["status"] == "running"
            expected = list(live.SCENARIOS["plan"][: live.SCENARIOS["plan"].index(name)])
            assert [row["case"] for row in reports[-1]["cases"]] == expected
        return {"case": name, "status": "passed"}

    def write(_path, report, _secrets):
        reports.append(json.loads(json.dumps(report)))

    monkeypatch.setattr(live, "run_case", run_case)
    monkeypatch.setattr(live, "write_safe_report", write)
    report = await live.run(
        "deepseek",
        "plan",
        environment={"DEEPSEEK_API_KEY": "synthetic-key"},
        output=output,
    )
    assert report["status"] == "passed"
    assert len(reports) == len(live.SCENARIOS["plan"])
    assert "synthetic-key" not in json.dumps(reports)


def _verification_records():
    def row(seq, kind, tool, call, **extra):
        return {
            "turn_id": "implementation",
            "seq": seq,
            "kind": kind,
            "payload": {"name": tool, "tool_use_id": call, **extra},
        }

    return [
        row(
            1, "tool_request", "exec_command", "before", arguments={"command": "python3 verify.py"}
        ),
        row(
            2,
            "tool_response",
            "exec_command",
            "before",
            result=(
                "exit_code=1\nTraceback (most recent call last):\n"
                '  File "verify.py", line 2, in <module>\nAssertionError\n'
            ),
            is_error=True,
        ),
        row(3, "tool_request", "write_file", "write", arguments={"path": "clamp.py"}),
        row(4, "tool_response", "write_file", "write", result="ok", is_error=False),
        row(5, "tool_request", "exec_command", "after", arguments={"command": "python3 verify.py"}),
        row(
            6,
            "tool_response",
            "exec_command",
            "after",
            result="exit_code=0\nPASS\n",
            is_error=False,
        ),
    ]


def test_verification_matches_actual_turn_and_strict_tool_order(tmp_path):
    rows = _verification_records()
    unrelated = {**rows[0], "turn_id": "investigation", "seq": 999}
    proof = live.verification_evidence([unrelated, *reversed(rows)], "implementation", tmp_path)
    assert proof["failure_before_first_write"]
    assert proof["success_after_last_write"]
    assert proof["direct_verification_calls"] == 2
    assert proof["verification_exits"] == [1, 0]


@pytest.mark.parametrize(
    "command",
    [
        "cat verify.py",
        "python3 -c \"print('verify.py PASS')\"",
        "python3 verify.py || true",
        "python3 verify.py | cat",
        "python3 verify.py; true",
    ],
)
def test_non_execution_and_masked_shell_commands_cannot_prove_verification(tmp_path, command):
    rows = _verification_records()
    rows[0]["payload"]["arguments"]["command"] = command
    proof = live.verification_evidence(rows, "implementation", tmp_path)
    assert not proof["failure_before_first_write"]
    assert proof["direct_verification_calls"] == 1


def test_zero_exit_assertion_is_diagnostic_and_never_a_passing_failure_probe(tmp_path):
    rows = _verification_records()
    rows[1]["payload"]["result"] = rows[1]["payload"]["result"].replace(
        "exit_code=1", "exit_code=0"
    )
    proof = live.verification_evidence(rows, "implementation", tmp_path)
    assert proof["assertion_failure_observed"]
    assert proof["zero_exit_assertion_observed"]
    assert not proof["failure_before_first_write"]


def test_failure_must_return_before_write_and_success_must_start_after_write(tmp_path):
    rows = _verification_records()
    rows[1]["seq"], rows[2]["seq"] = 3, 2
    proof = live.verification_evidence(rows, "implementation", tmp_path)
    assert not proof["failure_before_first_write"]
    rows = _verification_records()
    rows[3]["seq"], rows[4]["seq"] = 5, 4
    proof = live.verification_evidence(rows, "implementation", tmp_path)
    assert not proof["success_after_last_write"]


def test_investigation_failure_and_duplicate_sequences_cannot_satisfy_implementation(tmp_path):
    rows = _verification_records()
    rows[0]["turn_id"] = rows[1]["turn_id"] = "investigation"
    assert not live.verification_evidence(rows, "implementation", tmp_path)[
        "failure_before_first_write"
    ]
    rows = _verification_records()
    rows[1]["seq"] = rows[0]["seq"]
    assert not live.verification_evidence(rows, "implementation", tmp_path)["valid_turn_sequence"]


def test_workspace_context_checks_provider_system_configuration(tmp_path):
    records = [
        {
            "kind": "llm_request",
            "payload": {
                "messages": [{"content": "Synthetic prompt"}],
                "tools": [],
                "config": {"system": f"Workspace: {tmp_path}"},
            },
        }
    ]
    proof = live.runtime_diagnostics(tmp_path, records, tmp_path)
    assert proof["requests"][0]["workspace_context_present"]
    assert str(tmp_path) not in json.dumps(proof)


@pytest.mark.parametrize("path_text", [r"C:\Synthetic\工程\workspace", "/synthetic/工程/workspace"])
@pytest.mark.parametrize("present", [False, True])
def test_workspace_context_compares_json_escaped_path_without_disclosing_it(
    tmp_path, path_text, present,
):
    from pathlib import PurePosixPath, PureWindowsPath

    workspace = (
        PureWindowsPath(path_text) if path_text.startswith("C:") else PurePosixPath(path_text)
    )
    records = [{"kind": "llm_request", "payload": {
        "messages": [{"content": "Synthetic ordinary request"}],
        "config": {"system": f"Workspace: {workspace}" if present else "No workspace supplied"},
    }}]
    proof = live.runtime_diagnostics(tmp_path, records, workspace)
    assert proof["requests"][0]["workspace_context_present"] is present
    assert str(workspace) not in json.dumps(proof)


async def test_external_verifier_executes_real_artifact_without_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-private-key")
    verifier = (
        b"import os\nfrom clamp import clamp\n"
        b"assert os.environ.get('DEEPSEEK_API_KEY') is None\n"
        b"assert clamp(-2, 0, 10) == 0\nprint('PASS')\n"
    )
    (tmp_path / "verify.py").write_bytes(verifier)
    (tmp_path / "clamp.py").write_text("def clamp(v, low, high): return min(v, high)\n")
    failed = await live.verify_final_artifact(tmp_path, verifier)
    assert failed["exit_code"] != 0 and failed["assertion_failure"]
    assert not failed["passed"]
    (tmp_path / "clamp.py").write_text("def clamp(v, low, high): return max(low, min(v, high))\n")
    passed = await live.verify_final_artifact(tmp_path, verifier)
    assert passed["passed"] and passed["fixture_unchanged"]
    (tmp_path / "verify.py").write_text("print('PASS')\n")
    changed = await live.verify_final_artifact(tmp_path, verifier)
    assert not changed["ran"] and not changed["passed"]


async def test_all_model_waits_share_case_deadline_instead_of_resetting_timeouts(
    tmp_path, monkeypatch
):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-v4-flash", {})
    case.process = type("LiveProcess", (), {"poll": lambda self: None})()
    case.deadline = asyncio.get_running_loop().time() + 0.02

    async def snapshot(_key):
        return {"tasks": [{"status": "running"}], "pendingUserInputs": []}

    monkeypatch.setattr(case, "snapshot", snapshot)
    with pytest.raises(TimeoutError):
        await case.until("synthetic", lambda _s: False)
    # A later wait receives no fresh 180/240-second allowance.
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.1):
            await case.pending("synthetic")


@pytest.mark.parametrize("details", [
    {}, {"goal_context": None}, {"goal_effective_context": None},
    {"goal_effective_context": None, "goal_context": {"automatic": True}},
])
def test_exact_user_task_evidence_rejects_hidden_auto_turn(tmp_path: Path, details) -> None:
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    with sqlite3.connect(case.root / "state" / "sessions.db") as db:
        db.executescript(
            "CREATE TABLE usage_events (turn_id,status);"
            "CREATE TABLE session_goals "
            "(session_key,goal_id,status,active_task_id,continuation_seq,background);"
            "CREATE TABLE agent_tasks (task_id,status,details,session_key,created_at);"
        )
        db.execute("INSERT INTO session_goals VALUES ('synthetic','goal','paused',NULL,0,0)")
        for index in range(3):
            task = f"task-{index}"
            db.execute("INSERT INTO agent_tasks VALUES (?, 'succeeded', ?, 'synthetic', ?)",
                       (task, json.dumps(details), index))
            case.guard.claim("root_turns", 4, turn_id=task)
    live.exact_user_tasks(case, "synthetic", ["task-0", "task-1", "task-2"])
    with sqlite3.connect(case.root / "state" / "sessions.db") as db:
        db.execute("INSERT INTO agent_tasks VALUES (?, 'succeeded', ?, 'synthetic', 4)",
                   ("unexpected", json.dumps({"goal_context": {"automatic": True}})))
    case.guard.claim("root_turns", 4, turn_id="unexpected")
    with pytest.raises(live.CaseFailureError, match="natural_exact_3_user_tasks"):
        live.exact_user_tasks(case, "synthetic", ["task-0", "task-1", "task-2"])


@pytest.mark.skipif(os.name == "nt", reason="private POSIX permissions unavailable")
@pytest.mark.parametrize("secret_file", [False, True])
def test_failed_state_retention_scans_sqlite_wal_and_private_permissions(secret_file: bool) -> None:
    from scripts.live_harness_security import retain_failed_temporary_tree

    root = Path(tempfile.mkdtemp(prefix="opensquilla-retention-offline-"))
    try:
        (root / "private").mkdir()
        private = root / "private" / "sessions.db-wal"
        private.write_bytes(b"x" * (64 * 1024 - 5) + (
            b"synthetic-credential" if secret_file else b"synthetic transcript"
        ))
        retained = retain_failed_temporary_tree(root, {"KEY": "synthetic-credential"})
        assert retained is not secret_file
        if retained:
            assert root.stat().st_mode & 0o777 == 0o700
            assert private.stat().st_mode & 0o777 == 0o600
        else:
            assert not root.exists()
    finally:
        if root.exists():
            live.scan_and_remove_temporary_tree(root, {})


async def test_final_usage_capture_survives_stop_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    base = live.LiveCase

    class SyntheticCase(base):
        async def start(self) -> None:
            pass

        async def stop(self, **kwargs: object) -> dict:
            captured.append("stop")
            raise RuntimeError("synthetic stop failure")

    async def failed_case(case) -> None:
        raise live.CaseFailureError("synthetic_failure")

    def ledger(state) -> dict:
        captured.append("ledger")
        return {"rows": [{"status": "unknown", "calls": 1, "total": 0}]}

    monkeypatch.setattr(live, "LiveCase", SyntheticCase)
    monkeypatch.setattr(live, "goal_case", failed_case)
    monkeypatch.setattr(live, "ledger_projection", ledger)
    monkeypatch.setattr(live, "runtime_diagnostics", lambda *args: {})
    result = await live.run_case("goal", "deepseek", "deepseek-chat", {})
    assert captured == ["stop", "ledger"]
    assert result["ledger"]["rows"][0]["status"] == "unknown"
    assert result["shutdown"]["usage_complete"] is False
    assert result["failed_assertion"] == "synthetic_failure"


@pytest.mark.parametrize("scope", ["case_deadline", "gateway_rpc", "operation"])
async def test_timeout_report_distinguishes_expired_case_from_internal_operation(
    monkeypatch, scope,
):
    base = live.LiveCase

    class Client:
        async def call(self, method, params):
            raise TimeoutError("synthetic private response must not appear in report")

    class SyntheticCase(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if scope == "case_deadline":
                self.deadline = asyncio.get_running_loop().time() - 1

        async def start(self):
            self.client = Client()
            await asyncio.sleep(0)

        async def stop(self, **kwargs):
            return {"process_exited": True, "forced": False}

    async def failed_case(case):
        if scope == "gateway_rpc":
            await case.rpc("sessions.messages.hydrate", key="synthetic")
        raise TimeoutError("synthetic private response must not appear in report")

    monkeypatch.setattr(live, "LiveCase", SyntheticCase)
    monkeypatch.setattr(live, "goal_case", failed_case)
    monkeypatch.setattr(live, "ledger_projection", lambda *_: {"rows": []})
    monkeypatch.setattr(live, "runtime_diagnostics", lambda *_: {})
    result = await live.run_case("goal", "deepseek", "deepseek-chat", {})
    assert result["status"] == "failed" and result["failure_class"] == "timeout"
    assert result["timeout_scope"] == scope
    assert result.get("timeout_method") == (
        "sessions.messages.hydrate" if scope == "gateway_rpc" else None
    )
    assert "synthetic private response" not in json.dumps(result)


async def test_stop_at_case_deadline_kills_without_extending_execution(tmp_path: Path) -> None:
    import time

    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    case.deadline = time.monotonic() - 1
    calls = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")
            self.returncode = -9

        def wait(self, timeout):
            calls.append("wait")
            return self.returncode

    case.process = Process()
    result = await case.stop()
    assert calls == ["kill", "wait"]
    assert result["forced"] and result["process_exited"]


async def test_stop_requests_owner_drain_before_production_kill_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import boot

    calls = []
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            raise AssertionError("graceful HTTP drain must work without POSIX signals")

        def kill(self):
            raise AssertionError("must allow the production drain deadline")

        def wait(self, timeout):
            calls.append(("wait", timeout))
            self.returncode = 0
            return 0

    async def post(_client, url, **kwargs):
        calls.append(("shutdown", url))
        return httpx.Response(202)

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr(boot, "gateway_shutdown_deadline", lambda: 75)
    case.process = Process()
    result = await case.stop()
    assert result == {"forced": False, "process_exited": True}
    assert calls == [("shutdown", f"http://127.0.0.1:{case.port}/api/system/shutdown"),
                     ("wait", 75), ("wait", 5)]


def test_goal_control_diagnostics_keep_shape_not_user_arguments(tmp_path: Path) -> None:
    records = [{"kind": "tool_request", "turn_id": "private-id", "payload": {
        "name": "update_goal", "arguments": {
            "objective": "private objective", "status": "paused", "reason": "private reason",
            "unknown-private-key": "private value",
        },
    }}]
    result = live.runtime_diagnostics(tmp_path, records, tmp_path)
    call = result["goal_control_calls"][0]
    assert call["status"] == "paused" and call["objective_present"] and call["reason_present"]
    assert call["argument_keys"] == ["objective", "reason", "status"]
    assert call["other_argument_count"] == 1 and call["turn_index"] == 1
    assert "private" not in json.dumps(result)


async def test_pending_waits_for_specific_new_task_after_resume(tmp_path, monkeypatch):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    snapshots = [
        {"tasks": [{"task_id": "old", "status": "abandoned"}]},
        {"tasks": [{"task_id": "old", "status": "abandoned"},
                   {"task_id": "new", "status": "running"}]},
        {"pendingUserInputs": [{"request_id": "synthetic-request"}],
         "tasks": [{"task_id": "new", "status": "running"}]},
    ]

    async def snapshot(key):
        return snapshots.pop(0)

    class Process:
        def poll(self):
            return None

    case.process = Process()
    monkeypatch.setattr(case, "snapshot", snapshot)
    result = await case.pending("synthetic", "new")
    assert result["request_id"] == "synthetic-request" and not snapshots


async def test_background_case_creates_session_before_subscribing(tmp_path, monkeypatch):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    calls = []

    async def rpc(method, **params):
        calls.append(method)
        return {}

    async def subscribe(key):
        assert calls == ["plans.setMode"]
        raise live.CaseFailureError("synthetic-stop-before-provider")

    monkeypatch.setattr(case, "rpc", rpc)
    monkeypatch.setattr(case, "subscribe", subscribe)
    with pytest.raises(live.CaseFailureError, match="synthetic-stop-before-provider"):
        await live.background_case(case)


async def test_restart_accepts_null_resume_task_until_idle_admission(tmp_path, monkeypatch):
    case = live.LiveCase(tmp_path, "deepseek", "deepseek-chat", {})
    phase = "active"
    goal = {"goalId": "synthetic-goal", "stateRevision": 1,
            "status": phase, "executionPolicy": "background"}
    requested = []

    async def rpc(method, **params):
        nonlocal phase
        if method == "goals.set":
            return {"taskId": "old"}
        if method == "goals.resume":
            phase = "active"
            return {"accepted": True, "taskId": None}
        if method == "goals.status":
            return {"goal": dict(goal, status=phase)}
        if method == "goals.pause":
            phase = "paused"
        return {}

    async def snapshot(key):
        return {"goal": dict(goal, status=phase)}

    async def pending(key, task=None):
        requested.append(task)
        assert task in {"old", "new"}
        return {"request_id": "synthetic-question"}

    async def until(key, predicate):
        assert not predicate({"tasks": [{"task_id": "old", "status": "abandoned"}]})
        state = {"tasks": [{"task_id": "old", "status": "abandoned"},
                           {"task_id": "new", "status": "running"}]}
        assert predicate(state)
        return state

    async def stop(**kwargs):
        nonlocal phase
        phase = "paused"

    async def noop(*args, **kwargs):
        pass

    for name, fn in {"rpc": rpc, "snapshot": snapshot, "pending": pending, "until": until,
                     "stop": stop, "start": noop, "subscribe": noop, "reconnect": noop}.items():
        monkeypatch.setattr(case, name, fn)
    await live.restart_case(case)
    assert requested == ["old", "new"]


@pytest.mark.parametrize("clock_resolution", [None, 0.015625], ids=["native", "windows-coarse"])
async def test_stop_total_deadline_bounds_dripping_http_before_terminal_cleanup(
    tmp_path, monkeypatch, clock_resolution,
):
    # This is a real local HTTP stream, not a provider or a mocked live result.
    from opensquilla.gateway import boot  # noqa: F401 - warm production import before deadline

    if clock_resolution is not None:
        monkeypatch.setattr(asyncio.get_running_loop(), "_clock_resolution", clock_resolution)
    response_finished = asyncio.Event()
    request_started = asyncio.Event()
    handlers = set()

    async def drip(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            warmup = await reader.readuntil(b"\r\n\r\n")
            assert warmup.startswith(b"GET /ready ")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            request = await reader.readuntil(b"\r\n\r\n")
            assert request.startswith(b"POST /api/system/shutdown ")
            request_started.set()
            writer.write(b"HTTP/1.1 202 Accepted\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            # asyncio may wake a timer up to one clock-resolution interval
            # early. On Windows 100 sleeps of 10ms can finish within 200ms
            # while socket callbacks keep the loop ready. Measure elapsed
            # monotonic time instead of treating sleep count as a duration.
            finish_at = time.monotonic() + 1.0
            while time.monotonic() < finish_at:
                await asyncio.sleep(0.01)
                writer.write(b"1\r\nx\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
            response_finished.set()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    order = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            order.append("gateway-killed")
            self.returncode = -9

        def terminate(self):
            raise AssertionError("deadline must kill inference before cleanup")

        def wait(self, timeout):
            assert self.returncode == -9 and timeout == 5
            return self.returncode

    class Terminal:
        def terminate(self):
            assert order == ["gateway-killed"]
            order.append("terminal-cleaned")

    server = await asyncio.start_server(drip, "127.0.0.1", 0)
    client = httpx.AsyncClient(trust_env=False)
    try:
        case = live.LiveCase(tmp_path, "deepseek", "deepseek-v4-flash", {})
        case.port = server.sockets[0].getsockname()[1]
        case.process, case.terminal = Process(), Terminal()
        # SSL/AnyIO import and the first Windows connection are setup, not the
        # dripping response under test. Warm a real keepalive connection before
        # assigning the same strict 200ms case deadline used by this regression.
        async with asyncio.timeout(5):
            await client.get(f"http://127.0.0.1:{case.port}/ready")

        @asynccontextmanager
        async def shutdown_client(**kwargs):
            assert kwargs == {"trust_env": False}
            yield client

        monkeypatch.setattr(httpx, "AsyncClient", shutdown_client)
        case.deadline = asyncio.get_running_loop().time() + 0.2
        async with asyncio.timeout(2):
            result = await case.stop()
        assert request_started.is_set() and not response_finished.is_set()
        assert result["graceful_request_error"] == "TimeoutError"
        assert result["forced"] and result["process_exited"]
        assert order == ["gateway-killed", "terminal-cleaned"]
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()
        for handler in handlers:
            handler.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.parametrize("run_kind,counter", [("agent", "root_turns"), ("subagent", "children")])
async def test_unstarted_turn_returns_only_its_new_guard_reservation(
    tmp_path, monkeypatch, run_kind, counter,
):
    from types import SimpleNamespace

    from opensquilla.gateway.task_runtime import TaskRuntime
    from opensquilla.observability.turn_call_log import TurnCallLogger

    async def original(_runtime, task):
        if task.task_id == "uncertain":
            raise RuntimeError("activation state is uncertain")
        return task.running

    monkeypatch.setattr(TaskRuntime, "_mark_running", original)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                        httpx.AsyncHTTPTransport.handle_async_request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", httpx.HTTPTransport.handle_request)
    monkeypatch.setattr(TurnCallLogger, "write", TurnCallLogger.write)
    guard = _guard(tmp_path)
    live.install_dispatch_guard(guard)

    async def start(task_id, *, running=False):
        return await TaskRuntime._mark_running(None, SimpleNamespace(
            task_id=task_id, run_kind=run_kind, running=running,
        ))

    assert not await start("cancelled-before-start")
    assert guard.snapshot()["counts"][counter] == 0
    assert await start("running", running=True)
    # An already-counted task returning False on a later call cannot undo its
    # original reservation (including a concurrent duplicate activation).
    assert not await start("running")
    assert guard.snapshot()["counts"][counter] == 1
    with pytest.raises(RuntimeError, match="uncertain"):
        await start("uncertain")
    assert guard.snapshot()["counts"][counter] == 2


def test_concurrent_unstarted_reservations_keep_started_turns(tmp_path):
    guard = _guard(tmp_path)
    guard.claim("root_turns", 4, turn_id="started")
    for index in range(3):
        guard.claim("root_turns", 4, turn_id=f"cancelled-{index}")

    def release(index):
        guard.release_unstarted_turn("root_turns", f"cancelled-{index % 3}")

    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(release, range(30)))
    assert guard.snapshot()["counts"]["root_turns"] == 1
    for index in range(3):
        guard.claim("root_turns", 4, turn_id=f"replacement-{index}")
    with pytest.raises(live.DispatchLimitError, match="root_turns_limit"):
        guard.claim("root_turns", 4, turn_id="excess")


def test_source_fingerprint_covers_imported_helper_without_inheriting_credentials(
    monkeypatch,
):
    helper = "scripts/smoke_v4_phase3_router.py"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "other-synthetic-secret")
    calls = []

    def git(args, **kwargs):
        calls.append(args)
        assert kwargs["timeout"] == 10
        assert "DEEPSEEK_API_KEY" not in kwargs["env"]
        assert "OPENROUTER_API_KEY" not in kwargs["env"]
        if args[1] == "ls-files":
            assert helper in args
            return helper.encode() + b"\0"
        return b"synthetic-head\n"

    monkeypatch.setattr(live.subprocess, "check_output", git)
    before = live.execution_source_fingerprint()
    original_read = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes",
        lambda path: (b"synthetic helper changed" if path == live.ROOT / helper
                      else original_read(path)),
    )
    after = live.execution_source_fingerprint()
    assert before["execution_source_sha256"] != after["execution_source_sha256"]
    assert before["head"] == after["head"] == "synthetic-head"
    assert len(calls) == 4


@pytest.mark.parametrize("compact", [False, True])
def test_workspace_cd_preserves_actual_verifier_exit_and_order(tmp_path, compact):
    import shlex

    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    connector = "&&" if compact else " && "
    command = f"cd {shlex.quote(str(workspace))}{connector}python3 verify.py"
    rows = _verification_records()
    for index in (0, 4):
        rows[index]["payload"]["arguments"]["command"] = command
    proof = live.verification_evidence(rows, "implementation", workspace)
    assert proof["failure_before_first_write"]
    assert proof["success_after_last_write"]
    assert proof["verification_exits"] == [1, 0]
    assert not proof["opaque_exec_before_first_write"]


@pytest.mark.parametrize("command", [
    "cd .. && python3 verify.py",
    "cd . ; python3 verify.py",
    "cd . || python3 verify.py",
    "cd . && python3 verify.py || true",
    "cd . && python3 verify.py | cat",
    "cd . && python3 verify.py; true",
    "cd . && python3 verify.py && echo PASS",
    "cd . && python3 verify.py > replacement.txt",
    "cd . && python3 verify.py\ntrue",
    "cd $(pwd) && python3 verify.py",
    "cd `pwd` && python3 verify.py",
    "cd . && python3 -c 'print(\"PASS\")'",
    "cd . && cat verify.py",
])
def test_cd_wrappers_cannot_hide_failures_or_substitute_verification(tmp_path, command):
    rows = _verification_records()
    rows[0]["payload"]["arguments"]["command"] = command
    proof = live.verification_evidence(rows, "implementation", tmp_path)
    assert not proof["failure_before_first_write"]
    assert proof["direct_verification_calls"] == 1


def test_cd_from_explicit_other_cwd_requires_exact_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert live._direct_verification_command(
        {"command": "cd workspace && python3 verify.py", "workdir": str(tmp_path)}, workspace
    )
    assert not live._direct_verification_command(
        {"command": "cd . && python3 verify.py", "workdir": str(tmp_path)}, workspace
    )
