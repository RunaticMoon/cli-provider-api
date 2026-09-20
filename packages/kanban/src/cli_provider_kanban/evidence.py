"""Durable evidence persistence — sanitized bytes under private permissions.

One helper for BOTH the dispatch and resolve paths so neither can drift:

- the diff is sanitized (known credential values + generic secret shapes)
  BEFORE persistence; the recorded digest covers the persisted sanitized
  bytes, never the raw payload;
- the evidence directory is created 0700 and the file 0600 AT CREATION —
  nothing relies on a permissive umask or a post-hoc chmod of a freshly
  written world-readable file;
- writes are no-follow: a pre-planted symlink/hardlink at the target is
  refused and left untouched, and existing files must be regular, owned by
  this euid, and single-linked before they may be replaced;
- ancestors are checked: the store directory and the evidence dir must be
  real, owned directories — never symlinks.

Redaction is a known-value/shape screen, not full DLP — the labels on the
returned dict say exactly that.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .worktree import sanitize_text, sha256_bytes


class EvidenceError(Exception):
    """Evidence persistence precondition failure (fail closed)."""


_REDACTION_LABEL = (
    "known credential values + generic secret shapes "
    "(sanitize_text; not full DLP)"
)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _lstat(path: Path) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError as exc:
        raise EvidenceError(
            f"evidence path {path} is not inspectable: {exc}"
        ) from exc


def _require_owned_dir(path: Path) -> None:
    st = _lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise EvidenceError(f"{path} is not a real directory")
    if st.st_uid != os.geteuid():
        raise EvidenceError(f"{path} is not owned by this uid")


def evidence_dir(store_path: str | Path) -> Path:
    """The ``evidence/`` directory beside the dispatch store — 0700, owned,
    no symlink components in the resolved location."""
    euid = os.geteuid()
    # resolve() lands on the canonical location; every mutation below then
    # happens at the real path, and each checked component is lstat'd (a
    # symlink never passes S_ISDIR under lstat).
    base = Path(store_path).resolve().parent
    _require_owned_dir(base)
    d = base / "evidence"
    if d.is_symlink():
        raise EvidenceError(f"evidence dir {d} is a symlink — refusing")
    if d.exists():
        _require_owned_dir(d)
        if _lstat(d).st_mode & 0o077:
            # Our own pre-existing directory (e.g. created by an older
            # umask): tighten it — never chmod a foreign target.
            os.chmod(d, 0o700)
    else:
        os.mkdir(d, 0o700)
    return d


def _write_private(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` 0600, refusing planted links.

    A pre-existing file must be a regular, single-linked file owned by this
    euid before it is replaced; anything else is an attacker sentinel and is
    left untouched.
    """
    euid = os.geteuid()
    if os.path.lexists(path):
        st = _lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != euid or st.st_nlink != 1:
            raise EvidenceError(
                f"refusing to replace {path}: not a singly-linked regular "
                "file owned by this uid — left untouched"
            )
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW,
        0o600,
    )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != euid or st.st_nlink != 1:
            raise EvidenceError(f"refusing to write {path}: unsafe target")
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def persist_diff(
    store_path: str | Path,
    dispatch_id: str,
    diff_text: str,
    secrets: list[str] | tuple[str, ...] = (),
) -> dict:
    """Sanitize ``diff_text`` and persist it privately; digest the PERSISTED
    (sanitized) bytes. Returns the evidence descriptor stored on the receipt."""
    sanitized = sanitize_text(diff_text, secrets)
    data = sanitized.encode("utf-8", "replace")
    d = evidence_dir(store_path)
    path = d / f"{dispatch_id}.diff"
    _write_private(path, data)
    return {
        "path": str(path),
        "sha256": sha256_bytes(data),
        "bytes": len(data),
        "sanitized": True,
        "redaction": _REDACTION_LABEL,
    }
