"""Client for the *existing* wrapper API — no invented endpoints.

Endpoints used (all pre-existing):
- ``POST /v1/chat/completions``  (stream:false, metadata.task_id/workspace_id
  and, post-merge, metadata.execution={task_revision, base_revision, route,
  policy_version})
- ``GET  /api/v1/runs/{run_id}``
- ``POST /api/v1/runs/{run_id}/cancel``
- ``GET  /api/v1/artifacts/{artifact_id}``

The wrapper's ``run_id``/``attempt_id`` stay canonical. A duplicate
``task_id`` returns the cached run (``X-Run-Cached``) — we reconcile against
that view rather than create a second execution. Transport failures raise
``WrapperTransportError``: the caller must treat the run as UNKNOWN (the
request may have reached the wrapper) and never retry blindly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class WrapperError(Exception):
    """Wrapper interaction failure."""


class WrapperTransportError(WrapperError):
    """No usable HTTP response — execution may have started (UNKNOWN)."""


class WrapperHTTPError(WrapperError):
    """HTTP error response; carries the parsed error body if present."""

    def __init__(self, status: int, message: str, *, body: dict | None = None):
        super().__init__(f"wrapper HTTP {status}: {message}")
        self.status = status
        self.body = body or {}

    @property
    def run_id(self) -> str | None:
        run = self.body.get("run") or {}
        err = self.body.get("error") or {}
        return run.get("run_id") or err.get("run_id")

    @property
    def run(self) -> dict | None:
        run = self.body.get("run")
        return run if isinstance(run, dict) else None


@dataclass(frozen=True)
class SubmitOutcome:
    """Result of POST /v1/chat/completions (which is synchronous)."""

    status: str            # completed | failed | cancelled | unknown
    run: dict              # the run view (canonical run_id/attempt_id)
    cached: bool           # duplicate task_id served from the store
    content: str

    @property
    def run_id(self) -> str:
        return self.run["run_id"]

    @property
    def attempt_id(self) -> str | None:
        return self.run.get("attempt_id")


def _read_credential(credential_file: str | None) -> str | None:
    """Read the bearer token from its file at call time — never argv, never
    logged, never stored on the client object."""
    if not credential_file:
        return None
    try:
        token = Path(credential_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WrapperError(f"cannot read credential file: {exc}") from exc
    return token or None


class WrapperClient:
    def __init__(
        self,
        base_url: str,
        *,
        credential_file: str | None = None,
        timeout_seconds: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self._credential_file = credential_file
        self.timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> tuple[int, dict | bytes, dict]:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        token = _read_credential(self._credential_file)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                out_headers = {k.lower(): v for k, v in resp.headers.items()}
                if "application/json" in (resp.headers.get("Content-Type") or ""):
                    return resp.status, json.loads(raw.decode("utf-8")), out_headers
                return resp.status, raw, out_headers
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = {"error": {"message": raw[:400].decode("utf-8", "replace")}}
            err = parsed.get("error") or {}
            raise WrapperHTTPError(
                exc.code, str(err.get("message") or exc.reason), body=parsed
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WrapperTransportError(
                f"wrapper transport failure on {method} {path}: {exc}"
            ) from exc

    # -- endpoints ------------------------------------------------------------

    def submit_chat(
        self,
        *,
        model: str,
        task_id: str,
        workspace_id: str,
        messages: list[dict],
        execution: dict | None = None,
    ) -> SubmitOutcome:
        """One synchronous completion. ``execution`` rides only when the core
        contract is merged (baseline API rejects unknown metadata keys)."""
        metadata: dict = {"task_id": task_id, "workspace_id": workspace_id}
        if execution is not None:
            metadata["execution"] = execution
        status, body, headers = self._request(
            "POST",
            "/v1/chat/completions",
            body={
                "model": model,
                "messages": messages,
                "stream": False,
                "metadata": metadata,
            },
        )
        if not isinstance(body, dict):
            raise WrapperTransportError("submit returned a non-JSON body")
        run = body.get("run")
        if not isinstance(run, dict) or not run.get("run_id"):
            raise WrapperTransportError("submit response carried no run view")
        content = ""
        choices = body.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = str(
                (choices[0].get("message") or {}).get("content") or ""
            )
        return SubmitOutcome(
            status=str(run.get("status") or "unknown"),
            run=run,
            cached=headers.get("x-run-cached", "").lower() == "true",
            content=content,
        )

    def get_run(self, run_id: str) -> dict | None:
        """Canonical run view; None only on a clean 404."""
        try:
            status, body, _ = self._request("GET", f"/api/v1/runs/{run_id}")
        except WrapperHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        return body if isinstance(body, dict) else None

    def cancel_run(self, run_id: str) -> dict:
        """-> {run_id, status, requested, confirmed, detail}."""
        _, body, _ = self._request("POST", f"/api/v1/runs/{run_id}/cancel")
        if not isinstance(body, dict):
            raise WrapperTransportError("cancel returned a non-JSON body")
        return body

    def get_artifact(self, artifact_id: str) -> bytes:
        _, body, _ = self._request("GET", f"/api/v1/artifacts/{artifact_id}")
        return body if isinstance(body, bytes) else bytes(str(body), "utf-8")


__all__ = [
    "SubmitOutcome",
    "WrapperClient",
    "WrapperError",
    "WrapperHTTPError",
    "WrapperTransportError",
]
