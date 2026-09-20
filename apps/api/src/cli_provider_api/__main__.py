"""Operator CLI for the API.

    cli-provider-api serve --config config.yaml --host 127.0.0.1 --port 8080
    cli-provider-api hash-key < ./runtime/local.key
    cli-provider-api hash-key --key-file ./runtime/local.key
    cli-provider-api hash-key --key-env LOCAL_KEY
    cli-provider-api new-key --out ./runtime/local.key

The raw API key is never taken as a command-line argument (which would leak it
through ``ps``/``/proc/<pid>/cmdline`` and shell history). ``hash-key`` reads it
from stdin, a key file, or an environment variable name. Secrets are generated
into a 0600 local file and only their hashes are stored in config.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from typing import Sequence

import uvicorn

from cli_provider_core import hash_api_key, load_config

from .app import create_app
from .ownerlock import ApiOwnerError


def _serve(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    host = args.host or config.api.host
    port = args.port if args.port is not None else config.api.port
    try:
        app = create_app(config)
    except ApiOwnerError as exc:
        raise SystemExit(f"cli-provider-api: {exc}") from exc
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-provider-api")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API")
    serve.add_argument("--config", required=True)
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--log-level", default="warning")

    hash_key = sub.add_parser("hash-key", help="hash an API key (never via argv)")
    source = hash_key.add_mutually_exclusive_group()
    source.add_argument("--key-file", default=None)
    source.add_argument("--key-env", default=None)

    new_key = sub.add_parser("new-key", help="generate a local API key")
    new_key.add_argument("--out", default=None, help="write the key to this 0600 file")
    new_key.add_argument("--force", action="store_true", help="overwrite an existing file")
    return parser


def _read_key(args: argparse.Namespace) -> str:
    if args.key_file:
        with open(args.key_file, "r", encoding="utf-8") as handle:
            key = handle.readline()
    elif args.key_env:
        key = os.environ.get(args.key_env, "")
        if not key:
            raise SystemExit(f"environment variable {args.key_env!r} is unset or empty")
    else:
        if sys.stdin.isatty():
            sys.stderr.write("reading API key from stdin...\n")
        key = sys.stdin.readline()
    key = key.strip()
    if not key:
        raise SystemExit("no API key was provided")
    return key


def _new_key(args: argparse.Namespace) -> int:
    key = secrets.token_urlsafe(32)
    if not args.out:
        print(key)
        return 0
    flags = os.O_CREAT | os.O_WRONLY | (os.O_TRUNC if args.force else os.O_EXCL)
    try:
        fd = os.open(args.out, flags, 0o600)
    except FileExistsError:
        raise SystemExit(
            f"{args.out!r} already exists; pass --force to overwrite"
        )
    try:
        # os.open mode is masked by umask; force the exact 0600 mode.
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key + "\n")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    print(args.out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "hash-key":
        print(hash_api_key(_read_key(args)))
        return 0
    if args.command == "new-key":
        return _new_key(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
