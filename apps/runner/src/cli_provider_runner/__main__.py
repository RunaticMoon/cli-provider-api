"""Operator CLI for the standalone Runner.

The operator (not a run request) selects the allowlisted driver distribution,
version and instance id:

    cli-provider-runner serve --socket /run/cli-provider/runner.sock \
        --instance-id runner-local-1 \
        --driver-id mock --distribution cli-driver-mock --version 0.1.0

Mock fixture behaviour is likewise operator-controlled via the environment
(CLI_DRIVER_MOCK_BEHAVIOR / CLI_DRIVER_MOCK_MALFORMED_MODE), never via RPC.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any, Sequence

from cli_provider_transports import DEFAULT_MAX_FRAME_BYTES

from .client import RunnerClient
from .registry import DriverAllowlistEntry, DriverLoadError
from .server import DEFAULT_MAX_RUN_SECONDS, RunnerServer


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload), file=sys.stderr, flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-provider-runner")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the runner over a Unix socket")
    serve.add_argument("--socket", required=True)
    serve.add_argument("--instance-id", required=True)
    serve.add_argument("--driver-id", required=True)
    serve.add_argument("--distribution", required=True)
    serve.add_argument("--version", required=True)
    serve.add_argument("--max-queue", type=int, default=8)
    serve.add_argument("--max-frame-bytes", type=int, default=DEFAULT_MAX_FRAME_BYTES)
    serve.add_argument("--cancel-deadline", type=float, default=5.0)
    serve.add_argument("--max-run-seconds", type=float, default=DEFAULT_MAX_RUN_SECONDS)

    for name in ("manifest", "probe", "discover-models"):
        call = sub.add_parser(name)
        call.add_argument("--socket", required=True)

    run = sub.add_parser("run", help="start a run and stream its events")
    run.add_argument("--socket", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--task-id", required=True)
    run.add_argument("--attempt-id", required=True)
    run.add_argument("--preset", required=True)
    run.add_argument("--workspace-id", required=True)
    run.add_argument("--message", action="append", default=[])

    cancel = sub.add_parser("cancel", help="cancel an active run")
    cancel.add_argument("--socket", required=True)
    cancel.add_argument("--run-id", required=True)

    return parser


_METHOD_NAMES = {
    "manifest": "manifest",
    "probe": "probe",
    "discover-models": "discover_models",
}


async def _serve(args: argparse.Namespace) -> int:
    entry = DriverAllowlistEntry(
        driver_id=args.driver_id, distribution=args.distribution, version=args.version
    )
    server = RunnerServer(
        socket_path=args.socket,
        instance_id=args.instance_id,
        entry=entry,
        max_queue=args.max_queue,
        max_frame_bytes=args.max_frame_bytes,
        cancel_deadline_seconds=args.cancel_deadline,
        max_run_seconds=args.max_run_seconds,
    )
    try:
        server.load()
    except DriverLoadError as exc:
        _emit({"type": "error", "code": exc.code, "message": exc.message})
        return 2

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, server.request_stop)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    _emit(
        {
            "type": "ready",
            "instance_id": args.instance_id,
            "socket": args.socket,
            "driver_id": args.driver_id,
            "driver_version": args.version,
        }
    )
    await server.serve()
    return 0


async def _call(args: argparse.Namespace) -> int:
    client = await RunnerClient.connect(args.socket)
    try:
        response = await client.call(_METHOD_NAMES[args.command])
        print(json.dumps(response.model_dump(mode="json")))
        return 0 if response.ok else 1
    finally:
        await client.aclose()


async def _run(args: argparse.Namespace) -> int:
    messages = [
        {"role": "user", "content": text} for text in (args.message or ["hello"])
    ]
    params = {
        "run_id": args.run_id,
        "task_id": args.task_id,
        "attempt_id": args.attempt_id,
        "preset": args.preset,
        "workspace": {"workspace_id": args.workspace_id},
        "messages": messages,
    }
    client = await RunnerClient.connect(args.socket)
    try:
        async for envelope in client.run(params):
            print(json.dumps(envelope.model_dump(mode="json")))
        result = client.last_run_response
        print(json.dumps(result.model_dump(mode="json") if result else {"ok": False}))
        return 0
    finally:
        await client.aclose()


async def _cancel(args: argparse.Namespace) -> int:
    client = await RunnerClient.connect(args.socket)
    try:
        response = await client.call("cancel", {"run_id": args.run_id})
        print(json.dumps(response.model_dump(mode="json")))
        return 0 if response.ok else 1
    finally:
        await client.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        return asyncio.run(_serve(args))
    if args.command == "run":
        return asyncio.run(_run(args))
    if args.command == "cancel":
        return asyncio.run(_cancel(args))
    return asyncio.run(_call(args))


if __name__ == "__main__":
    raise SystemExit(main())
