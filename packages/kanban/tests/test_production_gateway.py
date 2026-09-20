"""Production-gateway opt-in — ``execution.allow_installed_gateway``.

The reviewed production approval: in ``gateway`` mode ONLY, the SUBMIT data
plane may target the installed 9Router service at exactly
``http://127.0.0.1:20128``, under a strict trusted-policy boolean that
defaults off. Run control must stay on a distinct loopback wrapper base
with its own credential; the management compiler, the control plane and
direct mode keep refusing port 20128 unconditionally, and the opt-in never
widens past the literal approved base — no alias, prefix, non-loopback
host, or truthy-string value counts.

No test contacts the real installed service. The transport seam is
``cli_provider_kanban.wrapper_client._bounded_request`` — the single socket
choke point — replaced by a recorder that returns canned bodies, so the
real ``_LoopbackBase`` validation, client wiring, header and credential
selection all run with zero network.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import cli_provider_kanban.dispatch as dispatch_module
import cli_provider_kanban.wrapper_client as wrapper_client_module
from cli_provider_kanban.dispatch import dispatch_once
from cli_provider_kanban.kernel import KernelBridge
from cli_provider_kanban.policy import Policy
from cli_provider_kanban.store import DispatchStore
from cli_provider_kanban.wrapper_client import (
    WrapperClient,
    WrapperError,
    _LoopbackBase,
)

from conftest import (  # noqa: F401  (fixtures)
    dispatch_policy,
    policy_dict,
    requires_hermes,
    spec_dict,
    spec_body,
    write_policy,
)

DATA_URL = "http://127.0.0.1:20128"
CONTROL_URL = "http://127.0.0.1:9010"


def _write_cred(path: Path, token: str, mode: int = 0o600) -> str:
    path.write_text(token, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def _gateway_execution(**over) -> dict:
    """The approved installed-gateway execution shape (flag ON)."""
    exe = {
        "mode": "gateway",
        "base_url": DATA_URL,
        "control_base_url": CONTROL_URL,
        "allow_installed_gateway": True,
    }
    exe.update(over)
    return exe


# --- ExecutionTarget policy contract -----------------------------------------


class TestInstalledGatewayPolicy:
    def test_flag_defaults_false(self):
        data = policy_dict()
        data["execution"] = {
            "mode": "gateway",
            "base_url": "http://127.0.0.1:9099",
            "control_base_url": "http://127.0.0.1:9098",
        }
        policy = Policy.model_validate(data)
        assert policy.execution.allow_installed_gateway is False

    def test_approved_shape_loads(self):
        data = policy_dict()
        data["execution"] = _gateway_execution()
        policy = Policy.model_validate(data)
        assert policy.execution.allow_installed_gateway is True
        assert policy.execution.base_url == DATA_URL

    def test_direct_mode_never_opts_in(self):
        data = policy_dict()
        data["execution"] = {
            "mode": "direct",
            "base_url": DATA_URL,
            "model": "devin/swe-2-max",
            "allow_installed_gateway": True,
        }
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_missing_control_refused(self):
        exe = _gateway_execution()
        del exe["control_base_url"]
        data = policy_dict()
        data["execution"] = exe
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_equal_control_refused(self):
        """Control equal to the data base is never a distinct wrapper base."""
        data = policy_dict()
        data["execution"] = _gateway_execution(control_base_url=DATA_URL)
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_control_on_installed_port_refused(self):
        """A control base on 20128 — even via another loopback spelling — is
        still the installed port, never a control plane."""
        for control in (DATA_URL, "http://localhost:20128",
                        "http://[::1]:20128"):
            data = policy_dict()
            data["execution"] = _gateway_execution(control_base_url=control)
            with pytest.raises(ValidationError):
                Policy.model_validate(data)

    def test_nonloopback_control_refused(self):
        data = policy_dict()
        data["execution"] = _gateway_execution(
            control_base_url="http://10.0.0.5:9010")
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_https_control_refused(self):
        """The control boundary is explicit-http loopback — https is not."""
        data = policy_dict()
        data["execution"] = _gateway_execution(
            control_base_url="https://127.0.0.1:9010")
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_nonloopback_data_refused(self):
        data = policy_dict()
        data["execution"] = _gateway_execution(
            base_url="http://10.0.0.5:20128")
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_alias_data_base_refused(self):
        """``localhost:20128`` is the same endpoint but not the literal
        approved base — the opt-in does not extend to aliases."""
        for base in ("http://localhost:20128", "http://[::1]:20128"):
            data = policy_dict()
            data["execution"] = _gateway_execution(base_url=base)
            with pytest.raises(ValidationError):
                Policy.model_validate(data)

    def test_prefixed_data_base_refused(self):
        data = policy_dict()
        data["execution"] = _gateway_execution(
            base_url="http://127.0.0.1:20128/api")
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    def test_other_port_refused(self):
        data = policy_dict()
        data["execution"] = _gateway_execution(
            base_url="http://127.0.0.1:20129")
        with pytest.raises(ValidationError):
            Policy.model_validate(data)

    @pytest.mark.parametrize("bad", ["yes", "true", "on", "1", 1])
    def test_truthy_nonbool_refused(self, bad):
        """StrictBool only: a truthy YAML string/number is not consent."""
        data = policy_dict()
        data["execution"] = _gateway_execution(allow_installed_gateway=bad)
        with pytest.raises(ValidationError):
            Policy.model_validate(data)


# --- Loopback boundary defaults -----------------------------------------------


class TestInstalledPortBoundary:
    def test_default_refuses_installed_port(self):
        """Every loopback spelling of the installed port is refused by
        default — base and constructor both."""
        for url in (DATA_URL, "http://localhost:20128",
                    "http://[::1]:20128"):
            with pytest.raises(WrapperError, match="20128"):
                _LoopbackBase(url)
            with pytest.raises(WrapperError, match="20128"):
                WrapperClient(url)

    def test_control_base_refuses_installed_port(self):
        with pytest.raises(WrapperError, match="20128"):
            WrapperClient("http://127.0.0.1:9099",
                          control_base_url=DATA_URL)

    def test_opt_in_requires_explicit_control(self):
        """No control_base_url -> control would share the installed data
        port; refused rather than silently collapsing the planes."""
        with pytest.raises(WrapperError):
            WrapperClient(DATA_URL, allow_installed_gateway=True)

    def test_opt_in_control_still_refuses_installed_port(self):
        with pytest.raises(WrapperError, match="20128"):
            WrapperClient(DATA_URL, control_base_url="http://localhost:20128",
                          allow_installed_gateway=True)

    def test_opt_in_only_literal_true(self):
        """Truthy strings/ints never lift the port refusal."""
        for bad in ("yes", "true", 1):
            with pytest.raises(WrapperError, match="20128"):
                WrapperClient(DATA_URL, control_base_url=CONTROL_URL,
                              allow_installed_gateway=bad)

    def test_opt_in_never_widens_off_loopback(self):
        with pytest.raises(WrapperError, match="loopback"):
            WrapperClient("http://10.0.0.5:20128",
                          control_base_url=CONTROL_URL,
                          allow_installed_gateway=True)


# --- Transport seam ------------------------------------------------------------


@pytest.fixture
def transport_seam(monkeypatch):
    """Recorder replacing the single socket choke point — real validation,
    headers and credential reads run; no socket ever opens."""
    calls: list[dict] = []

    def seam(base, method, path, *, headers, data, timeout_seconds,
             max_response_bytes):
        calls.append({
            "host": base.host,
            "connect_host": base.connect_host,
            "port": base.port,
            "netloc": base.netloc,
            "method": method,
            "path": path,
            "headers": dict(headers),
            "body": json.loads(data) if data else None,
        })
        if path == "/v1/chat/completions":
            body = calls[-1]["body"]
            meta = body["metadata"]
            run = {
                "run_id": "run_0001",
                "task_id": meta["task_id"],
                "attempt_id": "att_0001",
                "status": "completed",
                "workspace_id": meta["workspace_id"],
                "outcome": "succeeded",
                "preset": body.get("model"),
                "summary": "seam done",
                "artifacts": [],
                "detail": None,
                "cached": False,
                "execution": meta.get("execution"),
            }
            return 200, {}, json.dumps({
                "id": "chatcmpl-seam",
                "choices": [{"message": {"content": "seam done"}}],
                "run": run,
            }).encode()
        if path.endswith("/cancel"):
            run_id = path.split("/")[-2]
            return 200, {}, json.dumps({
                "run_id": run_id, "status": "cancelled", "requested": True,
                "confirmed": True, "detail": "seam",
            }).encode()
        if path.startswith("/api/v1/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            return 200, {}, json.dumps({
                "run_id": run_id, "task_id": "t_seam",
                "workspace_id": "ws-alpha", "status": "running",
            }).encode()
        if path.startswith("/api/v1/artifacts/"):
            return 200, {}, b"artifact-bytes"
        return 404, {}, json.dumps(
            {"error": {"message": "no seam route"}}).encode()

    monkeypatch.setattr(wrapper_client_module, "_bounded_request", seam)
    return calls


class TestDataVsControlPlane:
    def test_submit_hits_data_port_control_hits_wrapper(
        self, transport_seam, tmp_path
    ):
        data_cred = _write_cred(tmp_path / "data.cred", "data-token\n")
        ctrl_cred = _write_cred(tmp_path / "ctrl.cred", "ctrl-token\n")
        client = WrapperClient(
            DATA_URL,
            credential_file=data_cred,
            control_base_url=CONTROL_URL,
            control_credential_file=ctrl_cred,
            allow_installed_gateway=True,
        )
        out = client.submit_chat(
            model="jev.worker.code.standard", task_id="t_seam",
            workspace_id="ws-alpha",
            messages=[{"role": "user", "content": "go"}],
        )
        assert out.status == "completed"
        submit = transport_seam[0]
        assert submit["method"] == "POST"
        assert submit["path"] == "/v1/chat/completions"
        assert submit["host"] == "127.0.0.1"
        assert submit["connect_host"] == "127.0.0.1"
        assert submit["port"] == 20128
        assert submit["headers"]["Authorization"] == "Bearer data-token"
        assert submit["body"]["model"] == "jev.worker.code.standard"

        client.get_run("run_0001")
        ctrl = transport_seam[1]
        assert ctrl["path"] == "/api/v1/runs/run_0001"
        assert ctrl["port"] == 9010
        assert ctrl["headers"]["Authorization"] == "Bearer ctrl-token"

        client.cancel_run("run_0001")
        assert transport_seam[2]["path"].endswith("/cancel")
        assert transport_seam[2]["port"] == 9010
        assert transport_seam[2]["headers"]["Authorization"] == \
            "Bearer ctrl-token"

        # Nothing control-shaped ever touched the data port.
        assert all(
            c["port"] == 9010
            for c in transport_seam
            if c["path"].startswith("/api/v1/")
        )

    def test_control_plane_never_opts_in(self, transport_seam, tmp_path):
        """``control._control_client`` uses the control base + credential;
        a control target on the installed port still refuses — the data-plane
        approval never propagates to run control."""
        from cli_provider_kanban.control import _control_client

        data = policy_dict()
        data["execution"] = _gateway_execution(
            credential_file=_write_cred(tmp_path / "d.cred", "data-token\n"),
            control_credential_file=_write_cred(
                tmp_path / "c.cred", "ctrl-token\n"),
        )
        policy = Policy.model_validate(data)
        client = _control_client(policy)
        client.get_run("run_1")
        assert transport_seam[0]["port"] == 9010
        assert transport_seam[0]["headers"]["Authorization"] == \
            "Bearer ctrl-token"

        # Without the flag the policy can still NAME the installed port for
        # control — the client boundary keeps refusing it anyway.
        data = policy_dict()
        data["execution"] = {
            "mode": "gateway",
            "base_url": "http://127.0.0.1:9099",
            "control_base_url": DATA_URL,
        }
        policy = Policy.model_validate(data)
        with pytest.raises(WrapperError, match="20128"):
            _control_client(policy)


# --- Real dispatch construction path --------------------------------------------


class _StubKernel:
    """Board-free kernel stand-in: claims the tick, lists zero cards. The
    WrapperClient construction under test is the REAL one — only the kernel
    bridge and the socket are stubbed."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def acquire_lock(self):
        return True

    def call(self, op, **kwargs):
        assert op == "list_scope", f"unexpected kernel op {op!r}"
        return {"tasks": []}


def test_dispatch_constructs_opted_submit_and_plain_control(
    tmp_path, monkeypatch
):
    """dispatch_once's own client wiring: the submit client carries the
    opt-in, the data credential and the split control fields; the separate
    control client is built on the control base/credential and never sees
    the flag."""
    built: list[tuple[tuple, dict]] = []
    real_cls = wrapper_client_module.WrapperClient

    def recording_client(*args, **kwargs):
        built.append((args, kwargs))
        return real_cls(*args, **kwargs)

    monkeypatch.setattr(dispatch_module, "WrapperClient", recording_client)
    monkeypatch.setattr(dispatch_module, "KernelBridge", _StubKernel)

    data = policy_dict()
    data["execution"] = _gateway_execution(
        credential_file=_write_cred(tmp_path / "d.cred", "data-token\n"),
        control_credential_file=_write_cred(
            tmp_path / "c.cred", "ctrl-token\n"),
    )
    policy_path = write_policy(tmp_path, data)
    report = dispatch_once(
        board_db=tmp_path / "kanban.db",
        policy_path=policy_path,
        store_path=tmp_path / "dispatch.db",
    )
    assert report["results"] == []

    assert len(built) == 2
    (sub_args, sub_kw), (ctrl_args, ctrl_kw) = built
    assert sub_args == (DATA_URL,)
    assert sub_kw["allow_installed_gateway"] is True
    assert sub_kw["credential_file"].endswith("d.cred")
    assert sub_kw["control_base_url"] == CONTROL_URL
    assert sub_kw["control_credential_file"].endswith("c.cred")
    assert ctrl_args == (CONTROL_URL,)
    assert "allow_installed_gateway" not in ctrl_kw
    assert ctrl_kw["credential_file"].endswith("c.cred")


def test_dispatch_refuses_installed_port_without_optin(tmp_path, monkeypatch):
    """Same policy shape without the flag: the real construction path fails
    closed at WrapperClient — before any store, kernel or HTTP work."""
    monkeypatch.setattr(dispatch_module, "KernelBridge", _StubKernel)
    data = policy_dict()
    data["execution"] = _gateway_execution(allow_installed_gateway=False)
    policy_path = write_policy(tmp_path, data)
    with pytest.raises(WrapperError, match="20128"):
        dispatch_once(
            board_db=tmp_path / "kanban.db",
            policy_path=policy_path,
            store_path=tmp_path / "dispatch.db",
        )
    assert not (tmp_path / "dispatch.db").exists()


def test_dispatch_refuses_flag_on_direct_mode(tmp_path, monkeypatch):
    """A direct-mode target can never opt in — policy load itself fails."""
    monkeypatch.setattr(dispatch_module, "KernelBridge", _StubKernel)
    data = policy_dict()
    data["execution"] = {
        "mode": "direct",
        "base_url": DATA_URL,
        "model": "devin/swe-2-max",
        "allow_installed_gateway": True,
    }
    policy_path = write_policy(tmp_path, data)
    with pytest.raises(ValidationError):
        dispatch_once(
            board_db=tmp_path / "kanban.db",
            policy_path=policy_path,
            store_path=tmp_path / "dispatch.db",
        )


# --- End-to-end: real kernel + real client, stubbed socket ----------------------


HERMES_ENV_KEYS = ("HERMES_HOME", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME",
                   "HERMES_KANBAN_BOARD")


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A real Hermes kanban.db + bridge, scoped to tmp_path."""
    for key in HERMES_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    db = tmp_path / "kanban.db"
    bridge = KernelBridge(
        db, env_extra={"HERMES_HOME": str(tmp_path / "hermes_home")}
    )
    yield db, bridge
    bridge.close()


@requires_hermes
def test_dispatch_submits_through_installed_gateway_seam(
    tmp_path, monkeypatch, board, dispatch_policy, transport_seam
):
    """The REAL path: dispatch_once -> real WrapperClient (opt-in) -> the
    stubbed socket records exactly one data-plane POST to
    127.0.0.1:20128 with the data credential — combo name, never a preset."""
    db, bridge = board
    data, rev = dispatch_policy
    data["execution"] = _gateway_execution(
        credential_file=_write_cred(tmp_path / "d.cred", "data-token\n"),
        control_credential_file=_write_cred(
            tmp_path / "c.cred", "ctrl-token\n"),
    )
    tid = bridge.call("create_task", title="c", assignee="jev-native",
                      body="x")["task_id"]
    spec = spec_dict(
        task_id=tid, base_revision=rev, effort_hint="auto",
        verification={"argv": ["true"], "criteria": "exit 0"},
        artifacts=[],
    )
    del spec["task_id"]
    task_map = tmp_path / "tm.json"
    task_map.write_text(json.dumps({tid: spec}))
    data["task_map"] = str(task_map)
    policy_path = write_policy(tmp_path, data)

    report = dispatch_once(
        board_db=db, policy_path=policy_path,
        store_path=tmp_path / "dispatch.db",
    )

    rec = next(r for r in report["results"] if r["task_id"] == tid)
    assert rec["action"] == "review", rec
    # Exactly one wire call: the submit POST on the approved data endpoint.
    assert len(transport_seam) == 1
    submit = transport_seam[0]
    assert submit["method"] == "POST"
    assert submit["path"] == "/v1/chat/completions"
    assert submit["host"] == "127.0.0.1"
    assert submit["connect_host"] == "127.0.0.1"
    assert submit["port"] == 20128
    assert submit["headers"]["Authorization"] == "Bearer data-token"
    assert submit["body"]["model"] == "jev.worker.code.standard"
    assert submit["body"]["metadata"]["execution"]["route"] == \
        "worker.code.standard"
    task = bridge.call("get_task", task_id=tid)["task"]
    assert task["status"] == "review"
