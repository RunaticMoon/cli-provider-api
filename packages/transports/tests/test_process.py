"""Bounded stdio process transport tests (synthetic processes only)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap

import pytest

from cli_provider_transports import (
    LocalProcessExecutor,
    NdjsonProcessTransport,
    ProcessStartError,
    terminate_process_group,
)


def script(tmp_path, body: str):
    path = tmp_path / "child.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def executor_for(mode: str | None = None) -> LocalProcessExecutor:
    env = dict(os.environ)
    if mode is not None:
        env["TRANSPORT_TEST_MODE"] = mode
    return LocalProcessExecutor(env=env)


async def test_argv_is_validated_and_never_a_shell():
    executor = LocalProcessExecutor()
    with pytest.raises(ProcessStartError):
        await executor.spawn([])
    with pytest.raises(ProcessStartError):
        await executor.spawn(["echo", ""])
    with pytest.raises(ProcessStartError):
        await executor.spawn(["/definitely/not/here/binary"])


async def test_frames_round_trip_and_stderr_stays_separate(tmp_path):
    child = script(
        tmp_path,
        """
        import json, sys
        sys.stderr.write("noise-on-stderr\\n")
        sys.stderr.flush()
        line = sys.stdin.readline()
        sys.stdout.write(json.dumps({"echo": json.loads(line)}) + "\\n")
        sys.stdout.flush()
        """,
    )
    process = await executor_for().spawn([sys.executable, str(child)])
    transport = NdjsonProcessTransport(process)
    try:
        await transport.send({"hello": "world"})
        frame = await transport.recv_within(10)
        assert frame == {"echo": {"hello": "world"}}
        await asyncio.sleep(0.1)
        assert transport.stderr_bytes_seen() > 0
        assert "noise-on-stderr" not in json.dumps(frame)
        assert transport.stderr_classification().startswith("stderr output present")
    finally:
        assert await transport.aclose() is True


async def test_stderr_is_bounded_to_a_tail(tmp_path):
    child = script(
        tmp_path,
        """
        import sys
        for _ in range(200):
            sys.stderr.write("x" * 100 + "\\n")
        sys.stderr.flush()
        sys.stdout.write('{"done": true}\\n')
        sys.stdout.flush()
        """,
    )
    process = await executor_for().spawn([sys.executable, str(child)])
    transport = NdjsonProcessTransport(process, max_stderr_bytes=256)
    try:
        assert await transport.recv_within(10) == {"done": True}
        for _ in range(50):
            if transport.stderr_bytes_seen() >= 20_000:
                break
            await asyncio.sleep(0.05)
        assert transport.stderr_bytes_seen() > 256
        assert len(transport.stderr_tail()) <= 256
        assert "truncated" in transport.stderr_classification()
    finally:
        await transport.aclose()


async def test_clean_eof_at_a_frame_boundary_is_none(tmp_path):
    child = script(
        tmp_path,
        """
        import sys
        sys.stdout.write('{"first": 1}\\n')
        sys.stdout.flush()
        """,
    )
    process = await executor_for().spawn([sys.executable, str(child)])
    transport = NdjsonProcessTransport(process)
    try:
        assert await transport.recv_within(10) == {"first": 1}
        assert await transport.recv_within(10) is None
    finally:
        await transport.aclose()


async def test_termination_is_confirmed_even_when_sigterm_is_ignored(tmp_path):
    ready = tmp_path / "ignoring.ready"
    child = script(
        tmp_path,
        f"""
        import pathlib, signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pathlib.Path({str(ready)!r}).write_text("ready")
        time.sleep(60)
        """,
    )
    process = await executor_for().spawn([sys.executable, str(child)])
    transport = NdjsonProcessTransport(process, grace_seconds=0.3)
    for _ in range(100):
        if ready.exists():
            break
        await asyncio.sleep(0.05)
    assert ready.exists(), "the child never installed SIG_IGN"
    # Prove the child really ignores SIGTERM, so the escalation path is the one
    # actually exercised below.
    os.killpg(process.pid, signal.SIGTERM)
    await asyncio.sleep(0.3)
    assert process.returncode is None, "the child did not ignore SIGTERM"
    confirmed = await terminate_process_group(process, grace_seconds=0.3)
    assert confirmed is True
    assert process.returncode is not None
    await transport.aclose()


def test_deeply_nested_frame_is_a_malformed_frame_not_a_recursion_error():
    from cli_provider_transports import MalformedFrame, decode_frame

    with pytest.raises(MalformedFrame):
        decode_frame(b"[" * 200_000)


async def test_descendant_that_outlives_the_leader_is_reported_not_chased(tmp_path):
    # The leader is responsive to SIGTERM; the descendant ignores it. Chasing the
    # group after the leader is reaped is unsafe, so the survivor must be
    # reported instead of being claimed as cleaned up.
    pidfile = tmp_path / "survivor.pid"
    ready = tmp_path / "survivor.ready"
    # The descendant must have installed SIG_IGN before the kill, otherwise it
    # dies from the group SIGTERM and the case under test never happens.
    descendant = (
        "import signal,time,pathlib;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('ready');"
        "time.sleep(60)"
    )
    child = script(
        tmp_path,
        f"""
        import pathlib, subprocess, sys, time
        grand = subprocess.Popen([sys.executable, "-c", {descendant!r}])
        for _ in range(200):
            if pathlib.Path({str(ready)!r}).exists():
                break
            time.sleep(0.05)
        open({str(pidfile)!r}, "w").write(str(grand.pid))
        time.sleep(60)
        """,
    )
    process = await executor_for().spawn([sys.executable, str(child)])
    transport = NdjsonProcessTransport(process, grace_seconds=0.5)
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.05)
    assert pidfile.exists(), "the fixture never reported its descendant"
    survivor = int(pidfile.read_text().strip())
    try:
        assert await transport.aclose() is True
        assert process.returncode is not None
        assert survivor in transport.surviving_group_members()
        os.kill(survivor, 0)
    finally:
        try:
            os.kill(survivor, signal.SIGKILL)
        except ProcessLookupError:
            pass


async def test_an_already_exited_process_is_confirmed(tmp_path):
    child = script(tmp_path, "print('{}')")
    process = await executor_for().spawn([sys.executable, str(child)])
    transport = NdjsonProcessTransport(process)
    try:
        await process.wait()
        assert await terminate_process_group(process) is True
    finally:
        # Release the pipes as well: an unclosed subprocess transport is garbage
        # collected after the loop closes and surfaces as an unraisable warning.
        assert await transport.aclose() is True
