from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from cli_provider_sdk import (
    ANSWER_KINDS,
    TERMINAL_KINDS,
    Capabilities,
    CompletionStatus,
    DriverManifest,
    EventKind,
    Message,
    MessageDeltaEvent,
    MessageDeltaPayload,
    NormalizedRequest,
    Outcome,
    RunCompletedEvent,
    RunCompletedPayload,
    RunEvent,
    RunResult,
    SDK_VERSION,
    StructuredOutputMode,
    TransportKind,
    Usage,
    UsageProvenance,
    Verification,
    VerificationStatus,
    WorkspaceRef,
    is_terminal_kind,
    validate_alias,
)

RUN_EVENT = TypeAdapter(RunEvent)


def ts() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def request(**overrides) -> NormalizedRequest:
    values = {
        "run_id": "run-1",
        "task_id": "task-1",
        "attempt_id": "att-1",
        "preset": "mock/text",
        "workspace": WorkspaceRef(workspace_id="ws-1"),
        "messages": [{"role": "user", "content": "hi"}],
    }
    values.update(overrides)
    return NormalizedRequest(**values)


def delta(sequence: int = 1) -> MessageDeltaEvent:
    return MessageDeltaEvent(
        run_id="run-1",
        sequence=sequence,
        timestamp=ts(),
        payload=MessageDeltaPayload(text="hi"),
    )


def make_result(status, outcome, *, terminal_kind=None, **overrides) -> RunResult:
    values = {
        "run_id": "run-1",
        "status": status,
        "outcome": outcome,
        "verification": Verification(status=VerificationStatus.NOT_RUN, source="runner"),
        "usage": Usage(provenance=UsageProvenance.UNKNOWN),
        "terminal_kind": terminal_kind,
    }
    values.update(overrides)
    return RunResult(**values)


def test_canonical_event_names_match_contract():
    assert {kind.value for kind in EventKind} == {
        "run.started",
        "message.delta",
        "tool.started",
        "tool.completed",
        "permission.required",
        "artifact.created",
        "usage.updated",
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
    assert {k.value for k in TERMINAL_KINDS} == {
        "run.completed",
        "run.failed",
        "run.cancelled",
    }
    assert is_terminal_kind(EventKind.RUN_COMPLETED)
    assert not is_terminal_kind(EventKind.MESSAGE_DELTA)


def test_only_message_delta_is_answer_text():
    assert {k.value for k in ANSWER_KINDS} == {"message.delta"}


def test_event_has_schema_version_sequence_and_aware_timestamp():
    event = delta()
    assert event.schema_version == 1
    assert event.sequence == 1
    assert event.timestamp.tzinfo is not None
    with pytest.raises(ValidationError):
        MessageDeltaEvent(
            run_id="run-1",
            sequence=1,
            timestamp=datetime(2026, 1, 1),
            payload=MessageDeltaPayload(text="hi"),
        )


def test_event_kind_selects_validated_payload():
    raw = {
        "kind": "run.completed",
        "run_id": "run-1",
        "sequence": 2,
        "timestamp": ts().isoformat(),
        "payload": {"usage": {"provenance": "unknown"}},
    }
    event = RUN_EVENT.validate_python(raw)
    assert isinstance(event, RunCompletedEvent)
    assert event.payload.outcome == "succeeded"
    with pytest.raises(ValidationError):
        RUN_EVENT.validate_python({**raw, "payload": {"nope": 1}})


def test_tool_events_are_structural_not_answer_deltas():
    raw = {
        "kind": "tool.started",
        "run_id": "run-1",
        "sequence": 3,
        "timestamp": ts().isoformat(),
        "payload": {"tool_call_id": "t1", "name": "read_file"},
    }
    event = RUN_EVENT.validate_python(raw)
    assert event.kind == "tool.started"
    assert event.kind not in {k.value for k in ANSWER_KINDS}


def test_event_sequence_must_be_positive():
    with pytest.raises(ValidationError):
        delta(sequence=0)


def test_unknown_usage_cannot_carry_token_counts():
    with pytest.raises(ValidationError):
        Usage(provenance=UsageProvenance.UNKNOWN, input_tokens=0, output_tokens=0)
    assert Usage(provenance=UsageProvenance.UNKNOWN).input_tokens is None


def test_request_rejects_extra_executable_selection():
    with pytest.raises(ValidationError):
        request(package="cli-dev-driver")


def test_request_requires_at_least_one_message():
    with pytest.raises(ValidationError):
        NormalizedRequest(
            run_id="run-1",
            task_id="task-1",
            attempt_id="att-1",
            preset="mock/text",
            workspace=WorkspaceRef(workspace_id="ws-1"),
            messages=[],
        )


# ------------------------------------------------------------- aliases


@pytest.mark.parametrize(
    "alias", ["mock/text", "agy/review", "mock/text:latest", "standard", "a/b/c/d"]
)
def test_alias_accepts_exact_slash_names(alias):
    assert request(preset=alias).preset == alias
    assert request(model_alias=alias).model_alias == alias
    assert validate_alias(alias) == alias


@pytest.mark.parametrize(
    "alias",
    [
        "",
        "a//b",
        "/leading",
        "trailing/",
        "..",
        "a/../b",
        "a/..",
        "a/b/c/d/e",
        "bad alias",
        "tab\tchar",
        "x" * 300,
    ],
)
def test_alias_rejects_invalid_forms(alias):
    with pytest.raises(ValidationError):
        request(preset=alias)
    with pytest.raises(ValidationError):
        request(model_alias=alias)


def test_alias_round_trips_through_json_unchanged():
    original = request(preset="agy/review", model_alias="mock/text")
    again = NormalizedRequest.model_validate_json(original.model_dump_json())
    assert again.preset == "agy/review"
    assert again.model_alias == "mock/text"


def test_strict_ids_still_reject_slashes():
    for field in ("run_id", "task_id", "attempt_id"):
        with pytest.raises(ValidationError):
            request(**{field: "bad/id"})
    with pytest.raises(ValidationError):
        WorkspaceRef(workspace_id="bad/id")


# ------------------------------------------------- capabilities / manifest


def test_structured_output_is_a_mode_not_a_bool():
    assert {m.value for m in StructuredOutputMode} == {"native", "validated", "none"}
    with pytest.raises(ValidationError):
        Capabilities(
            streaming="native",
            sessions="none",
            roles="serialized",
            structured_output=True,
            external_tool_calls=False,
            internal_tools=False,
            vision=False,
            workspace_write=False,
            web_search=False,
            usage="unknown",
        )


def test_manifest_requires_sdk_version_and_transports():
    manifest = DriverManifest(
        driver_id="mock",
        name="Mock",
        version="0.1.0",
        sdk_version=SDK_VERSION,
        protocol_family="mock",
        supported_transports=[TransportKind.STDIO],
    )
    assert manifest.sdk_version == SDK_VERSION
    assert manifest.supported_transports == [TransportKind.STDIO]

    with pytest.raises(ValidationError):
        DriverManifest(
            driver_id="mock",
            name="Mock",
            version="0.1.0",
            protocol_family="mock",
            supported_transports=[TransportKind.STDIO],
        )
    with pytest.raises(ValidationError):
        DriverManifest(
            driver_id="mock",
            name="Mock",
            version="0.1.0",
            sdk_version=SDK_VERSION,
            protocol_family="mock",
            supported_transports=[],
        )


# --------------------------------------------------------------- RunResult


def test_completed_partial_is_allowed_and_not_forced_to_succeeded():
    result = make_result(
        CompletionStatus.COMPLETED,
        Outcome.PARTIAL,
        terminal_kind=EventKind.RUN_COMPLETED,
    )
    assert result.status is CompletionStatus.COMPLETED
    assert result.outcome is Outcome.PARTIAL
    assert result.verification.status is VerificationStatus.NOT_RUN


def test_completed_succeeded_still_allowed():
    result = make_result(
        CompletionStatus.COMPLETED,
        Outcome.SUCCEEDED,
        terminal_kind=EventKind.RUN_COMPLETED,
    )
    assert result.outcome is Outcome.SUCCEEDED


def test_completed_does_not_accept_unknown_outcome():
    with pytest.raises(ValidationError):
        make_result(
            CompletionStatus.COMPLETED,
            Outcome.UNKNOWN,
            terminal_kind=EventKind.RUN_COMPLETED,
        )


def test_completed_requires_completed_terminal():
    with pytest.raises(ValidationError):
        make_result(CompletionStatus.COMPLETED, Outcome.SUCCEEDED, terminal_kind=None)
    with pytest.raises(ValidationError):
        make_result(
            CompletionStatus.COMPLETED,
            Outcome.SUCCEEDED,
            terminal_kind=EventKind.RUN_FAILED,
        )


def test_failed_cancelled_unknown_semantics_preserved():
    failed = make_result(
        CompletionStatus.FAILED,
        Outcome.PROVIDER_ERROR,
        terminal_kind=EventKind.RUN_FAILED,
    )
    assert failed.outcome is Outcome.PROVIDER_ERROR

    cancelled = make_result(CompletionStatus.CANCELLED, Outcome.CANCELLED)
    assert cancelled.terminal_kind is None

    unknown = make_result(CompletionStatus.UNKNOWN, Outcome.UNKNOWN)
    assert unknown.terminal_kind is None

    with pytest.raises(ValidationError):
        make_result(CompletionStatus.UNKNOWN, Outcome.SUCCEEDED)
    with pytest.raises(ValidationError):
        make_result(
            CompletionStatus.UNKNOWN,
            Outcome.UNKNOWN,
            terminal_kind=EventKind.RUN_COMPLETED,
        )


def test_terminal_event_json_round_trip():
    event = RunCompletedEvent(
        run_id="run-1",
        sequence=3,
        timestamp=ts(),
        synthetic=True,
        payload=RunCompletedPayload(
            outcome="partial", usage=Usage(provenance=UsageProvenance.UNKNOWN)
        ),
    )
    dumped = event.model_dump(mode="json")
    again = RUN_EVENT.validate_python(dumped)
    assert again == event
    assert again.schema_version == 1
    assert again.sequence == 3


def test_message_model_is_available_for_requests():
    assert Message(role="user", content="x").role == "user"
