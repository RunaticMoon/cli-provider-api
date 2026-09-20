"""Client for the *existing* wrapper API — no invented endpoints.

Endpoints used (all pre-existing):
- ``POST /v1/chat/completions``  (stream:false, metadata.task_id/workspace_id
  and, post-merge, metadata.execution={task_revision, base_revision, route,
  policy_version}) — submitted to ``base_url`` (the wrapper directly, or a
  9Router gateway fronting it)
- ``GET  /api/v1/runs/{run_id}``            — direct wrapper control
- ``POST /api/v1/runs/{run_id}/cancel``     — direct wrapper control
- ``GET  /api/v1/artifacts/{artifact_id}``  — direct wrapper control

The wrapper's ``run_id``/``attempt_id`` stay canonical. A duplicate
``task_id`` returns the cached run (``run.cached`` in the body — the
``X-Run-Cached`` header is only a fallback since a gateway may drop custom
headers); we reconcile against that view rather than create a second
execution. Transport failures raise ``WrapperTransportError``: the caller
must treat the run as UNKNOWN (the request may have reached the wrapper)
and never retry blindly.

HTTP boundary hardening (shared with the 9Router compiler via the private
helpers below):

* explicit ``http://`` loopback targets only — 127.0.0.1 / localhost / ::1,
  with ``localhost`` pinned to 127.0.0.1 so no DNS lookup can escape;
  the installed service port (20128) is refused outright;
* no redirects — a 3xx is a terminal error, never a re-issue (urllib's
  default handler would forward ``Authorization`` off-host);
* no environment proxy — ``http.client`` never consults proxy config;
* bounded response and error bodies and a whole-response deadline (a slow
  drip cannot renew a per-socket timeout);
* parse failures raise ``WrapperTransportError`` with a fixed safe message —
  a raw provider body may carry secrets and is never echoed;
* run_id/artifact_id path segments are canonical ids — never traversal.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
import stat
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from cli_provider_sdk import ID_PATTERN


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


class _TransportFailure(Exception):
    """Socket-level failure carrying only a fixed safe message."""


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# The installed 9Router service listens on 20128 — never a disposable target
# and never a wrapper boundary, even when an operator marks it so.
_INSTALLED_SERVICE_PORT = 20128
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_CREDENTIAL_BYTES = 64 * 1024
_ID_RE = re.compile(ID_PATTERN)

# Finite known statuses mirroring cli_provider_core.models — anything else is
# normalized to 'unknown' rather than trusted.
KNOWN_RUN_STATUSES = frozenset({
    "reserved", "queued", "starting", "running", "cancelling",
    "completed", "failed", "cancelled", "unknown",
})


class _LoopbackBase:
    """A validated explicit-http loopback base URL.

    ``connect_host`` pins localhost to 127.0.0.1 so no DNS resolution ever
    runs for this boundary; ``netloc`` preserves the original Host header.
    """

    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http":
            raise WrapperError(
                f"wrapper URL {url!r} must be explicit http loopback — "
                f"scheme {parsed.scheme!r} is not trusted"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WrapperError(
                "wrapper URL must not carry userinfo/query/fragment"
            )
        host = parsed.hostname or ""
        if host not in _LOOPBACK_HOSTS:
            raise WrapperError(
                f"wrapper URL host {host!r} is not loopback — refusing"
            )
        try:
            port = parsed.port or 80
        except ValueError as exc:
            raise WrapperError(f"wrapper URL port is invalid: {exc}") from exc
        if port == _INSTALLED_SERVICE_PORT:
            raise WrapperError(
                "refusing the installed service port 20128 — it is never a "
                "wrapper/gateway boundary"
            )
        self.host = host
        self.port = port
        self.connect_host = "127.0.0.1" if host == "localhost" else host
        self.netloc = (
            f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        )
        path = (parsed.path or "").rstrip("/")
        self.prefix = path if path != "/" else ""


def _bounded_request(
    base: _LoopbackBase,
    method: str,
    path: str,
    *,
    headers: dict,
    data: bytes | None,
    timeout_seconds: float,
    max_response_bytes: int,
) -> tuple[int, dict, bytes]:
    """One request under a single whole-response deadline.

    Never follows redirects, never consults proxy configuration, and reads at
    most ``max_response_bytes``. ``timeout_seconds`` bounds the entire
    connect+response exchange — the socket budget is the *remaining* time, so
    a drip-feed cannot stretch the deadline.
    """
    deadline = time.monotonic() + timeout_seconds
    conn = http.client.HTTPConnection(
        base.connect_host, base.port, timeout=max(timeout_seconds, 0.001)
    )
    try:
        conn.connect()
        # getresponse() hands the socket to the HTTPResponse and clears
        # conn.sock — capture it now and keep driving the deadline on it.
        sock = conn.sock
        sock.settimeout(max(deadline - time.monotonic(), 0.001))
        req_path = (base.prefix + path) or "/"
        conn.putrequest(
            method, req_path, skip_host=True, skip_accept_encoding=True
        )
        conn.putheader("Host", base.netloc)
        conn.putheader("Accept-Encoding", "identity")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(data) if data else 0))
        conn.endheaders(data)
        resp = conn.getresponse()

        resp_headers: dict[str, str] = {}
        set_cookies: list[str] = []
        for key, value in resp.getheaders():
            lowered = key.lower()
            if lowered == "set-cookie":
                set_cookies.append(value)
            resp_headers[lowered] = value
        if set_cookies:
            resp_headers["set-cookie"] = "\n".join(set_cookies)

        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportFailure("response deadline exceeded")
            sock.settimeout(max(remaining, 0.001))
            try:
                chunk = resp.read1(65536)
            except (socket.timeout, TimeoutError) as exc:
                raise _TransportFailure(
                    "response deadline exceeded") from exc
            if not chunk:
                break
            total += len(chunk)
            if total > max_response_bytes:
                raise _TransportFailure(
                    f"response exceeds {max_response_bytes} bytes")
            chunks.append(chunk)
        return resp.status, resp_headers, b"".join(chunks)
    except _TransportFailure:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise _TransportFailure(type(exc).__name__) from exc
    finally:
        conn.close()


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


def _check_credential_file(path: Path) -> None:
    """Credential files are operator-private: regular, owned, 0600-ish, small."""
    try:
        st = path.lstat()
    except OSError as exc:
        raise WrapperError(f"cannot read credential file: {exc}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise WrapperError("credential file must not be a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise WrapperError("credential file must be a regular file")
    if st.st_uid != os.getuid():
        raise WrapperError(
            "credential file must be owned by the current user")
    if st.st_mode & 0o077:
        raise WrapperError(
            "credential file mode must not allow group/world access "
            "(chmod 600)"
        )
    if st.st_size > _MAX_CREDENTIAL_BYTES:
        raise WrapperError("credential file is too large")


def _read_credential(credential_file: str | None) -> str | None:
    """Read the bearer token from its file at call time — never argv, never
    logged, never stored on the client object."""
    if not credential_file:
        return None
    path = Path(credential_file)
    _check_credential_file(path)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WrapperError(f"cannot read credential file: {exc}") from exc
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in token):
        raise WrapperError("credential file token carries invalid characters")
    return token or None


def _parse_json_object(raw: bytes, context: str) -> dict:
    """Parse a response body that must be a JSON object — a fixed safe
    transport error otherwise (a raw body may carry secrets)."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise WrapperTransportError(
            f"wrapper returned an unparsable response to {context}"
        ) from exc
    if not isinstance(value, dict):
        raise WrapperTransportError(
            f"wrapper returned a non-object response to {context}"
        )
    return value


def _require_id(value: str, kind: str) -> str:
    """Path-segment ids are canonical ids — never traversal."""
    if not isinstance(value, str) or _ID_RE.match(value) is None:
        raise WrapperError(f"{kind} {value!r} is not a canonical id")
    return value


def _normalize_status(run: dict) -> str:
    status = run.get("status")
    return status if status in KNOWN_RUN_STATUSES else "unknown"


def _consistent_run(run: dict, *, task_id: str, workspace_id: str) -> bool:
    """The run view must agree with the submitted identity to be trusted."""
    if run.get("task_id") != task_id:
        return False
    if run.get("workspace_id") != workspace_id:
        return False
    attempt = run.get("attempt_id")
    if attempt is not None and (
        not isinstance(attempt, str) or _ID_RE.match(attempt) is None
    ):
        return False
    return True


class WrapperClient:
    """Submit goes to ``base_url`` (wrapper direct or a gateway); control
    reads go to ``control_base_url`` (default: same base) with
    ``control_credential_file`` (default: same credential)."""

    def __init__(
        self,
        base_url: str,
        *,
        credential_file: str | None = None,
        timeout_seconds: float = 120.0,
        control_base_url: str | None = None,
        control_credential_file: str | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ):
        self.base_url = base_url.rstrip("/")
        self._submit_base = _LoopbackBase(base_url)
        self._control_base = (
            _LoopbackBase(control_base_url)
            if control_base_url is not None
            else self._submit_base
        )
        self._credential_file = credential_file
        self._control_credential_file = (
            control_credential_file
            if control_credential_file is not None
            else credential_file
        )
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes

    def _request(
        self,
        base: _LoopbackBase,
        credential_file: str | None,
        method: str,
        path: str,
        *,
        body: dict | None = None,
    ) -> tuple[int, bytes, dict]:
        headers = {"Accept": "application/json"}
        token = _read_credential(credential_file)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, resp_headers, raw = _bounded_request(
                base, method, path,
                headers=headers, data=data,
                timeout_seconds=self.timeout_seconds,
                max_response_bytes=self.max_response_bytes,
            )
        except _TransportFailure as exc:
            raise WrapperTransportError(
                f"wrapper transport failure on {method} {path}: {exc}"
            ) from exc
        if status >= 300:
            # A real HTTP response (incl. redirects we refuse to follow). The
            # parsed body is kept only for run-view recovery; the message is
            # fixed — provider error text may carry secrets.
            parsed = None
            try:
                candidate = json.loads(raw.decode("utf-8"))
                if isinstance(candidate, dict):
                    parsed = candidate
            except (ValueError, UnicodeDecodeError):
                parsed = None
            raise WrapperHTTPError(
                status, "request refused by wrapper", body=parsed
            )
        return status, raw, resp_headers

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
        status, raw, headers = self._request(
            self._submit_base,
            self._credential_file,
            "POST",
            "/v1/chat/completions",
            body={
                "model": model,
                "messages": messages,
                "stream": False,
                "metadata": metadata,
            },
        )
        body = _parse_json_object(raw, "POST /v1/chat/completions")
        run = body.get("run")
        if not isinstance(run, dict) or not isinstance(run.get("run_id"), str):
            raise WrapperTransportError("submit response carried no run view")
        if _ID_RE.match(run["run_id"]) is None:
            raise WrapperTransportError(
                "submit response carried an invalid run view")
        status_value = _normalize_status(run)
        if not _consistent_run(run, task_id=task_id,
                               workspace_id=workspace_id):
            # Inconsistent body — the caller treats this as unknown; there is
            # never an automatic retry.
            status_value = "unknown"
        content = ""
        choices = body.get("choices") or []
        if choices and isinstance(choices[0], dict):
            content = str(
                (choices[0].get("message") or {}).get("content") or ""
            )
        return SubmitOutcome(
            status=status_value,
            run=run,
            # The body flag survives gateway header loss; the header is a
            # fallback only.
            cached=bool(run.get("cached"))
            or headers.get("x-run-cached", "").lower() == "true",
            content=content,
        )

    def get_run(self, run_id: str) -> dict | None:
        """Canonical run view; None only on a clean 404."""
        _require_id(run_id, "run_id")
        try:
            _, raw, _ = self._request(
                self._control_base,
                self._control_credential_file,
                "GET", f"/api/v1/runs/{run_id}",
            )
        except WrapperHTTPError as exc:
            if exc.status == 404:
                return None
            raise
        view = _parse_json_object(raw, f"GET /api/v1/runs/{run_id}")
        view["status"] = _normalize_status(view)
        return view

    def cancel_run(self, run_id: str) -> dict:
        """-> {run_id, status, requested, confirmed, detail}."""
        _require_id(run_id, "run_id")
        _, raw, _ = self._request(
            self._control_base,
            self._control_credential_file,
            "POST", f"/api/v1/runs/{run_id}/cancel",
        )
        view = _parse_json_object(
            raw, f"POST /api/v1/runs/{run_id}/cancel")
        view["status"] = _normalize_status(view)
        return view

    def get_artifact(self, artifact_id: str) -> bytes:
        _require_id(artifact_id, "artifact_id")
        _, raw, _ = self._request(
            self._control_base,
            self._control_credential_file,
            "GET", f"/api/v1/artifacts/{artifact_id}",
        )
        return raw


__all__ = [
    "SubmitOutcome",
    "WrapperClient",
    "WrapperError",
    "WrapperHTTPError",
    "WrapperTransportError",
]
