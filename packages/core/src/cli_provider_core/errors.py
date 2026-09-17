"""Core error taxonomy with safe HTTP classifications.

Messages here are safe for clients: they never contain prompts, headers, native
stderr or secrets.
"""

from __future__ import annotations


class CoreError(Exception):
    code: str = "internal_error"
    error_type: str = "server_error"
    http_status: int = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class InvalidRequest(CoreError):
    code = "invalid_request"
    error_type = "invalid_request_error"
    http_status = 400


class UnsupportedCapability(CoreError):
    code = "unsupported_capability"
    error_type = "unsupported_capability"
    http_status = 422


class AuthenticationError(CoreError):
    code = "invalid_api_key"
    error_type = "authentication_error"
    http_status = 401


class AuthorizationError(CoreError):
    code = "not_authorized"
    error_type = "authorization_error"
    http_status = 403


class NotFound(CoreError):
    code = "not_found"
    error_type = "not_found_error"
    http_status = 404


class Conflict(CoreError):
    code = "conflict"
    error_type = "conflict_error"
    http_status = 409

    def __init__(self, message: str, *, code: str = "conflict", run_id: str | None = None) -> None:
        super().__init__(message, code=code)
        self.run_id = run_id


class BodyTooLarge(CoreError):
    code = "body_too_large"
    error_type = "invalid_request_error"
    http_status = 413


class BodyTimeout(CoreError):
    code = "body_timeout"
    error_type = "invalid_request_error"
    http_status = 408


class HeadersTooLarge(CoreError):
    code = "headers_too_large"
    error_type = "invalid_request_error"
    http_status = 431


class QueueTimeout(CoreError):
    code = "queue_timeout"
    error_type = "rate_limit_error"
    http_status = 429


class QueueFull(CoreError):
    """Bounded admission: too many outstanding runs for this runner/principal."""

    code = "queue_full"
    error_type = "rate_limit_error"
    http_status = 429


class RunnerUnavailable(CoreError):
    code = "runner_unavailable"
    error_type = "runner_unavailable"
    http_status = 503


class RunnerQuarantined(CoreError):
    code = "runner_quarantined"
    error_type = "runner_unavailable"
    http_status = 503


class UpstreamProtocolError(CoreError):
    code = "runner_protocol_error"
    error_type = "server_error"
    http_status = 502
