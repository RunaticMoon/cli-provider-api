"""Single-API-owner kernel lock bound to the canonical store path.

``Store`` documents "single API instance", and the controller depends on it:
startup reconciliation rewrites formerly-active rows to ``unknown`` and the
cancel path trusts the in-process ``_active`` map, so a second API process on
the same database would mutate runs it does not own — it could even durably
cancel another owner's queued run as "never dispatched" while the real owner
was about to dispatch it.

The enforcement is a lifetime kernel lock (POSIX ``fcntl.flock``) on a
dedicated file derived from the *canonical* database path, acquired before
any store initialize/reconcile or registry effect in ``create_app`` and held
until shutdown. ``realpath`` collapses aliased spellings of one database
(symlinks, ``..``, relative paths) to a single lock identity. The lock file
itself is opened ``O_NOFOLLOW`` and forced to mode 0600 owned by this uid —
a planted symlink or foreign-owned file is a refusal, never followed.

This is a kernel lock, not a stale-PID-file scheme: closing the descriptor
releases it, the kernel releases it on process death, and the file is never
unlinked, so a recycled inode can never alias a live owner's lock.

The lock lives at the API application boundary, not inside ``Store``: plain
Store connections (read-only tooling, tests) remain unrestricted.
"""

from __future__ import annotations

import fcntl
import os
import stat


class ApiOwnerError(RuntimeError):
    """Startup refusal: the store is already owned or its lock file is unsafe."""


def _lock_path(db_path: str) -> str:
    # The lock identity is the canonical store path, so aliased config paths
    # naming the same database resolve to the same lock (or a refusal).
    return os.path.realpath(db_path) + ".api-owner.lock"


def acquire_store_owner_lock(db_path: str) -> int:
    """Take the exclusive lifetime owner lock for ``db_path``.

    Returns the descriptor holding the lock; the caller must keep it open for
    the whole API lifetime and release it with ``release_store_owner_lock``
    at shutdown. Raises ``ApiOwnerError`` — before touching the store — when
    a live owner already holds it or the lock file is unsafe.
    """
    lock_path = _lock_path(db_path)
    try:
        os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
        fd = os.open(
            lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
    except OSError as exc:
        raise ApiOwnerError(
            f"cannot open the store owner lock {lock_path!r} safely: "
            f"{exc.strerror or exc}"
        ) from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1):
            raise ApiOwnerError(
                f"store owner lock {lock_path!r} is not a single-link regular file owned "
                "by this user; refusing to start"
            )
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ApiOwnerError(
                "another cli-provider-api process already owns this store; "
                "one API owner per database is required because startup "
                "reconciliation and cancellation state are per-owner"
            ) from exc
        # Informational owner marker only — never read for any decision.
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} db={os.path.realpath(db_path)}\n".encode())
    except BaseException:
        os.close(fd)
        raise
    return fd


def release_store_owner_lock(fd: int) -> None:
    """Release the owner lock by closing its descriptor. Never unlinks."""
    try:
        os.close(fd)
    except OSError:
        pass
