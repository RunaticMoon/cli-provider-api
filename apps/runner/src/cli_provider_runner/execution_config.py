"""Operator-owned execution bindings for the standalone Runner.

Loaded once at ``serve`` startup from a protected JSON file selected with
``--execution-config PATH``. The file maps a request's ``workspace_id`` to an
operator-controlled absolute root directory plus the named actions the driver
may perform inside it (``allowed_actions``), with optional exact
``allowed_presets`` / ``allowed_models`` pins.

Nothing here is reachable from a run request: ids, roots, actions and pins are
all operator text from a validated local file, never model text, request fields,
environment variables or argv fragments supplied by a client.

This binding is NOT an OS sandbox. The native agent process can still reach the
host filesystem and network. The binding only decides which configured root (if
any) a ``workspace_id`` resolves to, which named actions are granted to the
driver, and that one bound workspace is claimed serially across runner
processes on this host.

File trust requirements (fail closed):

* the config path must be canonical — every component a real directory/file,
  no symlinks anywhere in the path;
* the file must be a regular file owned by the runner's uid;
* file mode must not grant group write/exec or any access to other users —
  ``0600`` or ``0640`` (the config is secret-free, so a group-read bit is
  acceptable and documented);
* the parent directory must be private: owned by the runner's uid with no
  group/other permissions (e.g. ``0700``).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cli_provider_sdk import ID_PATTERN, validate_alias

#: Named runtime actions an operator may grant per workspace. Unknown values
#: are rejected at config load so a typo never silently widens a grant. An
#: absent/empty list grants nothing — deny by default.
KNOWN_ACTIONS = frozenset({"hermes.yolo", "devin.acp.session_mode.bypass"})

CONFIG_MAX_BYTES = 65536
WORKSPACE_LOCK_NAME = ".cli-provider-runner.lock"
_WORKSPACE_ID_RE = re.compile(ID_PATTERN)


class ExecutionConfigError(Exception):
    """The operator execution config is missing, unreadable or untrusted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _unique_strings(values: list[str], field: str) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError(f"{field} must not contain duplicates")
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} entries must be non-empty strings")
    return values


class WorkspaceBinding(BaseModel):
    """One operator-pinned workspace root plus its granted actions/pins."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1)
    allowed_actions: list[str] = Field(default_factory=list)
    # None = "no runner-side pin" (the driver's own pinning still applies); an
    # explicit empty list = "deny every preset/model" for this workspace.
    allowed_presets: list[str] | None = None
    allowed_models: list[str] | None = None

    @field_validator("root")
    @classmethod
    def _root_is_canonical_existing_dir(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("root must not contain NUL")
        if not os.path.isabs(value):
            raise ValueError("root must be an absolute path")
        if os.path.realpath(value) != value:
            raise ValueError(
                "root must be canonical — no symlink or traversal components"
            )
        if not os.path.isdir(value):
            raise ValueError("root must be an existing directory")
        return value

    @field_validator("allowed_actions")
    @classmethod
    def _known_actions_only(cls, values: list[str]) -> list[str]:
        _unique_strings(values, "allowed_actions")
        unknown = [value for value in values if value not in KNOWN_ACTIONS]
        if unknown:
            raise ValueError(
                f"unknown allowed_actions {unknown!r}; known values: "
                f"{sorted(KNOWN_ACTIONS)}"
            )
        return values

    @field_validator("allowed_presets", "allowed_models")
    @classmethod
    def _aliases_only(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        _unique_strings(values, "allowed pins")
        for value in values:
            validate_alias(value)
        return values


class ExecutionConfig(BaseModel):
    """Top-level execution config: a map of workspace_id -> binding."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1)
    workspaces: dict[str, WorkspaceBinding] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version_supported(cls, value: int) -> int:
        if value != 1:
            raise ValueError("execution config version must be 1")
        return value

    @field_validator("workspaces")
    @classmethod
    def _workspace_ids_valid(
        cls, values: dict[str, WorkspaceBinding]
    ) -> dict[str, WorkspaceBinding]:
        for workspace_id in values:
            if _WORKSPACE_ID_RE.match(workspace_id) is None:
                raise ValueError(
                    f"workspace_id {workspace_id!r} is not a valid identifier"
                )
        return values

    def binding_for(self, workspace_id: str) -> WorkspaceBinding | None:
        return self.workspaces.get(workspace_id)


class BoundWorkspace:
    """``WorkspaceService`` bound to one operator-configured real root.

    ``resolve`` rejects absolute paths and any relative path that escapes the
    root — lexically (``..``) or through a symlink inside the root (the result
    is canonicalized and must remain under the real root). This is a naming
    boundary for the driver's own file bookkeeping, not an OS sandbox.
    """

    def __init__(self, workspace_id: str, root: str) -> None:
        self._workspace_id = workspace_id
        self._root = root

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    @property
    def root(self) -> str:
        return self._root

    def resolve(self, relative: str) -> str:
        if not isinstance(relative, str) or not relative or "\x00" in relative:
            raise ValueError("workspace paths must be non-empty strings")
        if os.path.isabs(relative):
            raise ValueError("absolute paths are not allowed inside a workspace")
        candidate = os.path.realpath(os.path.join(self._root, relative))
        if candidate != self._root and not candidate.startswith(
            self._root + os.sep
        ):
            raise ValueError(
                f"workspace path {relative!r} escapes the bound root"
            )
        return candidate


class BoundPermissions:
    """``PermissionPolicy`` backed only by the workspace's granted actions.

    Deny-by-default: an action is allowed only when the operator explicitly
    listed it under ``allowed_actions`` for the bound workspace.
    """

    def __init__(self, workspace_id: str, allowed_actions: list[str]) -> None:
        self._workspace_id = workspace_id
        self._allowed = frozenset(allowed_actions)

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def allows(self, action: str) -> bool:
        return action in self._allowed


def _fail(code: str, message: str) -> None:
    raise ExecutionConfigError(code, message)


def _check_trusted_file(path: str) -> str:
    """Validate ownership/permissions; return the canonical path."""
    if not isinstance(path, str) or not path:
        _fail("CONFIG_UNTRUSTED", "execution config path must be non-empty")
    absolute = os.path.abspath(path)
    canonical = os.path.realpath(absolute)
    if canonical != absolute:
        _fail(
            "CONFIG_UNTRUSTED",
            "execution config path must be canonical: no symlink components",
        )
    try:
        st = os.lstat(canonical)
    except OSError as exc:
        _fail(
            "CONFIG_UNREADABLE",
            f"execution config is not readable: {type(exc).__name__}",
        )
    if not stat.S_ISREG(st.st_mode):
        _fail(
            "CONFIG_UNTRUSTED", "execution config must be a regular file"
        )
    euid = os.geteuid()
    if st.st_uid != euid:
        _fail(
            "CONFIG_UNTRUSTED",
            "execution config must be owned by the runner uid",
        )
    # No owner-exec bit, no group write/exec, no access for others. 0640 is
    # permitted because this file is secret-free by contract.
    if st.st_mode & 0o137:
        _fail(
            "CONFIG_PERMISSIONS",
            "execution config file permissions must be 0600 or 0640 "
            "(group-read is allowed; the file holds no secrets)",
        )
    parent = os.path.dirname(canonical)
    try:
        pst = os.lstat(parent)
    except OSError as exc:
        _fail(
            "CONFIG_UNREADABLE",
            f"execution config parent is not readable: {type(exc).__name__}",
        )
    if not stat.S_ISDIR(pst.st_mode):
        _fail("CONFIG_UNTRUSTED", "execution config parent is not a directory")
    if pst.st_uid != euid:
        _fail(
            "CONFIG_UNTRUSTED",
            "execution config parent must be owned by the runner uid",
        )
    if pst.st_mode & 0o077:
        _fail(
            "CONFIG_PERMISSIONS",
            "execution config parent directory must be private (0700)",
        )
    return canonical


def load_execution_config(path: str) -> ExecutionConfig:
    """Read and validate the operator execution config. Fails closed."""
    canonical = _check_trusted_file(path)
    try:
        fd = os.open(canonical, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        _fail(
            "CONFIG_UNREADABLE",
            f"execution config could not be opened: {type(exc).__name__}",
        )
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(CONFIG_MAX_BYTES + 1)
    except OSError as exc:
        _fail(
            "CONFIG_UNREADABLE",
            f"execution config could not be read: {type(exc).__name__}",
        )
    if len(raw) > CONFIG_MAX_BYTES:
        _fail(
            "CONFIG_INVALID",
            f"execution config exceeds {CONFIG_MAX_BYTES} bytes",
        )
    try:
        data: Any = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail("CONFIG_INVALID", "execution config is not valid JSON")
    if not isinstance(data, dict):
        _fail("CONFIG_INVALID", "execution config root must be a JSON object")
    try:
        return ExecutionConfig.model_validate(data)
    except ValueError as exc:
        _fail("CONFIG_INVALID", f"execution config failed validation: {exc}")
    raise AssertionError("unreachable")


def claim_workspace_lock(root: str) -> int:
    """Claim the bound workspace for this run; returns the held fd.

    An exclusive non-blocking ``flock`` on ``<root>/.cli-provider-runner.lock``
    serializes runs against the same root across runner *processes* on this
    host (the in-process semaphore already serializes runs within one runner).
    The lock file is a small operator-visible artifact inside the bound root —
    it is the claim record, not a security boundary.
    """
    lock_path = os.path.join(root, WORKSPACE_LOCK_NAME)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        _fail(
            "workspace_busy",
            f"workspace root {root!r} is already claimed by another run",
        )
    return fd
