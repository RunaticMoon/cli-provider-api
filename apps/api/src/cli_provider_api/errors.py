"""API-level run errors carrying the normalized run view, plus handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cli_provider_core import CoreError


class ApiRunError(CoreError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        http_status: int,
        run: dict[str, Any],
        error_type: str = "run_error",
    ) -> None:
        super().__init__(message, code=code)
        self.run = run
        self.http_status = http_status
        self.error_type = error_type


def error_payload(error: CoreError, *, run: dict[str, Any] | None = None) -> dict[str, Any]:
    run_id = run.get("run_id") if run else getattr(error, "run_id", None)
    body: dict[str, Any] = {
        "error": {
            "message": error.message,
            "type": error.error_type,
            "code": error.code,
        }
    }
    if run_id:
        body["error"]["run_id"] = run_id
    if run is not None:
        body["run"] = run
    return body


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(CoreError)
    async def _core_error(_request: Request, exc: CoreError) -> JSONResponse:
        run = getattr(exc, "run", None)
        run_id = run["run_id"] if run else getattr(exc, "run_id", None)
        headers = {"X-Run-Id": run_id} if run_id else None
        return JSONResponse(
            status_code=exc.http_status,
            content=error_payload(exc, run=run),
            headers=headers,
        )
