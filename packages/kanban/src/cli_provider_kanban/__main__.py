"""Standalone CLI for the Jev shadow classifier.

    cli-provider-kanban schema [--out PATH]
    cli-provider-kanban shadow --board-db PATH --policy PATH --out PATH [--cache PATH]

The board DB is opened read-only (``mode=ro``); a missing file is an error,
never a reason to create or migrate one. No card, graph or board file is ever
written — the only outputs are ``--out`` (report) and ``--cache``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from .errors import CacheError, ShadowError
from .models import JevDecision, TaskSpec
from .policy import Policy, load_policy
from .shadow import run_shadow


def _schema(args: argparse.Namespace) -> int:
    doc = {
        "task_spec": TaskSpec.model_json_schema(),
        "jev_decision": JevDecision.model_json_schema(),
        "policy": Policy.model_json_schema(),
    }
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def _shadow(args: argparse.Namespace) -> int:
    report = run_shadow(
        board_db=args.board_db,
        policy_path=args.policy,
        out_path=args.out,
        cache_path=args.cache,
    )
    counts: dict[str, int] = {}
    for record in report["records"]:
        action = record["decision"]["recommended_action"]
        counts[action] = counts.get(action, 0) + 1
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    print(
        f"shadow: {len(report['records'])} in-scope card(s) -> {summary}; "
        f"report at {args.out}"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-provider-kanban",
        description="Jev rules-first shadow classifier for Hermes kanban cards",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    schema = sub.add_parser("schema", help="emit the TaskSpec/JevDecision/policy JSON schemas")
    schema.add_argument("--out", default=None, help="write schemas to this file instead of stdout")

    shadow = sub.add_parser(
        "shadow", help="read-only shadow classification of in-scope cards"
    )
    shadow.add_argument("--board-db", required=True,
                        help="explicit path to a Hermes kanban.db (opened read-only)")
    shadow.add_argument("--policy", required=True,
                        help="central routing policy (YAML or JSON)")
    shadow.add_argument("--out", required=True,
                        help="write the shadow report JSON here")
    shadow.add_argument("--cache", default=None,
                        help="decision cache path (default: <out>.cache.json)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            return _schema(args)
        if args.command == "shadow":
            return _shadow(args)
    except (ShadowError, CacheError, FileNotFoundError, ValidationError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
