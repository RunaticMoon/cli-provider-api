"""Deterministic regression tests for the reviewer's API findings.

Started as RED reproductions from the stopped worker; expectations were checked
against the intended contract (never fabricate zero, never silently drop an
execution-selecting message field) before use as GREEN guards.
"""

import pytest

from cli_provider_api.chat import run_error_classification
from cli_provider_api.schemas import chat_completion, parse_chat_request
from cli_provider_core.errors import InvalidRequest, UnsupportedCapability
from cli_provider_core.models import (
    FAILED,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_QUEUE_TIMEOUT,
    OUTCOME_REJECTED,
    UNKNOWN,
)


def test_repro_issue_2_queue_timeout_maps_to_429():
    """Finding 2: a queue timeout is a rate-limit, never a provider failure."""
    code, status, error_type, message = run_error_classification(
        FAILED, OUTCOME_QUEUE_TIMEOUT
    )
    assert (code, status, error_type) == ("queue_timeout", 429, "rate_limit_error")
    assert "provider" not in message.lower()


def test_repro_issue_2_rejected_is_rejection_specific():
    """Finding 2: a pre-execution rejection is classified as such, not provider."""
    code, status, error_type, message = run_error_classification(FAILED, OUTCOME_REJECTED)
    assert code == "run_rejected"
    assert status == 502
    assert error_type == "run_error"
    assert "rejected" in message.lower()
    assert code != "provider_failed"


def test_repro_issue_2_provider_failure_still_maps_to_provider_failed():
    code, status, _error_type, _message = run_error_classification(
        FAILED, OUTCOME_PROVIDER_ERROR
    )
    assert (code, status) == ("provider_failed", 502)


def test_repro_issue_2_unknown_status_still_maps_to_run_unknown():
    code, status, _error_type, _message = run_error_classification(UNKNOWN, "unknown")
    assert (code, status) == ("run_unknown", 502)


def test_repro_issue_3_usage_missing_counts_not_zero():
    """Finding 3: Usage with missing counts must not fabricate zero."""
    body = chat_completion(
        chat_id="chatcmpl-test",
        model="mock/text",
        created=1234567890,
        content="test content",
        run={"run_id": "test-run"},
        usage={"provenance": "reported", "input_tokens": None, "output_tokens": None},
    )
    assert body["usage"] is not None
    assert body["usage"]["prompt_tokens"] is None
    assert body["usage"]["completion_tokens"] is None
    assert body["usage"]["total_tokens"] is None


def test_repro_issue_3_partial_counts_keep_unknown_side_null():
    """A count the driver did not supply stays null; total cannot be invented."""
    body = chat_completion(
        chat_id="chatcmpl-test",
        model="mock/text",
        created=1234567890,
        content="test content",
        run={"run_id": "test-run"},
        usage={"provenance": "estimated", "input_tokens": 7, "output_tokens": None},
    )
    assert body["usage"]["prompt_tokens"] == 7
    assert body["usage"]["completion_tokens"] is None
    assert body["usage"]["total_tokens"] is None


def test_repro_issue_3_unknown_usage_stays_null():
    body = chat_completion(
        chat_id="chatcmpl-test",
        model="mock/text",
        created=1234567890,
        content="test content",
        run={"run_id": "test-run"},
        usage={"provenance": "unknown", "input_tokens": None, "output_tokens": None},
    )
    assert body["usage"] is None


def _request_for(message: dict) -> dict:
    return {
        "model": "mock/text",
        "messages": [message],
        "metadata": {"task_id": "task-1", "workspace_id": "ws-1"},
    }


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "hi", "function_call": {"name": "test"}},
        {"role": "assistant", "content": "hi", "tool_calls": []},
        {"role": "user", "content": "hi", "name": "spoof"},
    ],
)
def test_repro_issue_4_execution_shaped_message_keys_rejected(message):
    """Finding 4: execution-selecting message fields are unsupported, not dropped."""
    with pytest.raises(UnsupportedCapability):
        parse_chat_request(_request_for(message))


def test_repro_issue_4_arbitrary_message_key_rejected():
    with pytest.raises(InvalidRequest):
        parse_chat_request(_request_for({"role": "user", "content": "hi", "extra": 1}))


def test_message_role_content_control_is_accepted():
    """Positive control: the exact allowed message shape still parses."""
    request = parse_chat_request(_request_for({"role": "user", "content": "hi"}))
    assert request.messages == [{"role": "user", "content": "hi"}]
