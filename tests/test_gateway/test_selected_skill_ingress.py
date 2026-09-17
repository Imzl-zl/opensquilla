"""Explicit skills keep their identity across acceptance and durable queues."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.contracts.selected_skills import normalize_selected_skills
from opensquilla.gateway.adapters.pending_input_queue import GatewayPendingInputQueueAdapter
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.pending_input_primitives import (
    pending_input_payload,
    pending_input_projection,
    stored_pending_input,
)
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import _reusable_route_envelope
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.gateway.turn_steering import decode_steering_command

REF = {"name": "synthetic-table", "instanceId": "instance-one", "digest": "digest-one"}
PARAMS = {
    "key": "agent:main:synthetic", "message": "Make a table",
    "clientRequestId": "request-one", "clientMessageId": "message-one",
}


@pytest.mark.parametrize("surface", ["webchat", "session"])
async def test_adapters_keep_bound_skills_and_normalized_fingerprint(surface):
    application = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    adapter = GatewayTurnAdmissionAdapter(application)
    params = {**PARAMS, "selectedSkills": [REF, REF]}
    await adapter.admit(params, surface=surface)
    command = application.admit.await_args.args[0]
    assert command.selected_skills == (REF,)
    assert command.request_fingerprint == request_fingerprint({**params, "selectedSkills": [REF]})
    assert command.request_fingerprint != request_fingerprint({
        **params, "selectedSkills": [{**REF, "digest": "digest-two"}],
    })


@pytest.mark.parametrize("value", [[], None])
def test_empty_selection_keeps_old_request_fingerprint(value):
    assert request_fingerprint({**PARAMS, "selectedSkills": value}) == request_fingerprint(PARAMS)


@pytest.mark.parametrize("value", [
    "synthetic-table", [{}], [{**REF, "path": "/unexpected"}], [{**REF, "digest": ""}],
    [REF, {**REF, "instanceId": "other-instance"}], [REF] * 17,
])
def test_malformed_or_conflicting_selection_rejected_before_acceptance(value):
    with pytest.raises(ValueError, match="selectedSkills"):
        decode_admit_turn({**PARAMS, "selectedSkills": value})


def test_normalization_preserves_selection_order_and_does_not_modify_input():
    spaced = {**REF, "name": " synthetic-table "}
    second = {**REF, "name": "synthetic-paper", "instanceId": "instance-two"}
    assert normalize_selected_skills([spaced, second, REF]) == (REF, second)
    assert spaced["name"] == " synthetic-table "


def test_pending_queue_roundtrip_retains_skill_selection_and_blocks_steering():
    adapter = GatewayPendingInputQueueAdapter(SimpleNamespace(), turns=SimpleNamespace())
    command = adapter._enqueue_command({
        **PARAMS, "pendingInputId": "pending-one", "selectedSkills": [REF],
    })
    assert command.turn.selected_skills == (REF,)
    payload = pending_input_payload(command.turn, False)
    row = SimpleNamespace(
        pending_input_id="pending-one", session_key=PARAMS["key"],
        client_request_id="request-one", client_message_id="message-one",
        source_scope=command.turn.source_scope, request_fingerprint=request_fingerprint(payload),
        state_revision=1, position=0, created_at=1, updated_at=1, schema_version=1, payload=payload,
    )
    restored = stored_pending_input(row)
    assert restored.turn.selected_skills == (REF,)
    assert restored.has_non_text_semantics
    assert pending_input_projection(row)["selectedSkills"] == [REF]
    assert restored.request_fingerprint == row.request_fingerprint


def test_skill_bearing_steer_is_non_text_input():
    command = decode_steering_command(
        {**PARAMS, "expectedTurnId": "turn-one", "selectedSkills": [REF]},
        key=PARAMS["key"], principal_role="operator",
    )
    assert command.has_non_text_input


def test_reused_route_does_not_select_skills_for_later_turns():
    envelope = RouteEnvelope(
        SourceKind.WEB, "synthetic", "main", PARAMS["key"],
        metadata={"selected_skills": [REF], "synthetic": "retained"},
    )
    reused = _reusable_route_envelope(envelope)
    assert "selected_skills" not in reused.metadata
    assert reused.metadata["synthetic"] == "retained"
    assert envelope.metadata["selected_skills"] == [REF]
    assert replace(envelope).metadata["selected_skills"] == [REF]


def test_user_transcript_preserves_selected_skills_for_history_and_retry(tmp_path):
    from opensquilla.chat.history import transcript_entries_to_chat_messages

    content, writes = build_transcript_attachment_envelope(
        text="Make a table", attachments=[], selected_skills=[REF], session_id="synthetic",
        media_root=tmp_path, persist_enabled=True,
    )
    assert json.loads(content) == {
        "text": "Make a table", "attachments": [], "selected_skills": [REF],
    }
    assert writes == []
    entry = SimpleNamespace(
        id=1, message_id="message-one", role="user", content=content, created_at="now",
        provenance_kind=None, provenance_source_session_key=None, provenance_source_tool=None,
        turn_usage=None, tool_calls=None,
    )
    messages = transcript_entries_to_chat_messages([entry])
    assert messages[0]["text"] == "Make a table"
    assert messages[0]["selectedSkills"] == [REF]
