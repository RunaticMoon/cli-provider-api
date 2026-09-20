"""Standalone CLI for the Jev kanban path.

    cli-provider-kanban schema [--out PATH]
    cli-provider-kanban shadow --board-db PATH --policy PATH --out PATH [--cache PATH]
    cli-provider-kanban dispatch --board-db PATH --policy PATH --store PATH --once
    cli-provider-kanban status  --board-db PATH --policy PATH --store PATH
                                (--dispatch-id ID | --task-id ID)
    cli-provider-kanban control --board-db PATH --policy PATH --store PATH
                                <cancel|approve|deny|accept|resolve> ...
    cli-provider-kanban compile --policy PATH [--apply TARGET --credential-file PATH]

``shadow`` is read-only. ``dispatch --once`` runs a single bounded tick under
the kernel's singleton lock — it is a one-shot worker, not a scheduler; the
disabled systemd unit in examples/ is the (still-disabled) timer form.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from .compiler import CompileError, apply_plan, compile_from_path
from .control import (
    ControlError,
    cmd_accept,
    cmd_approve,
    cmd_cancel,
    cmd_deny,
    cmd_resolve,
    cmd_status,
)
from .dispatch import DispatchError, dispatch_once
from .errors import CacheError, ShadowError
from .kernel import KernelError
from .models import JevDecision, TaskSpec
from .policy import Policy, PolicyError, load_policy
from .shadow import run_shadow
from .store import StoreError


def _print_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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


def _dispatch(args: argparse.Namespace) -> int:
    report = dispatch_once(
        board_db=args.board_db,
        policy_path=args.policy,
        store_path=args.store,
    )
    _print_json(report)
    return 0


def _status(args: argparse.Namespace) -> int:
    out = cmd_status(
        store_path=args.store,
        board_db=args.board_db,
        policy_path=args.policy,
        dispatch_id=args.dispatch_id,
        task_id=args.task_id,
    )
    _print_json(out)
    return 0


def _control(args: argparse.Namespace) -> int:
    common = dict(
        store_path=args.store,
        policy_path=args.policy,
    )
    if args.control_op == "cancel":
        out = cmd_cancel(
            **common, board_db=args.board_db,
            dispatch_id=args.dispatch_id, actor=args.actor,
        )
    elif args.control_op == "approve":
        out = cmd_approve(
            **common, board_db=args.board_db,
            approval_id=args.approval_id, actor=args.actor,
        )
    elif args.control_op == "deny":
        out = cmd_deny(
            **common, approval_id=args.approval_id, actor=args.actor,
        )
    elif args.control_op == "accept":
        out = cmd_accept(
            **common, board_db=args.board_db,
            task_id=args.task_id, actor=args.actor,
            integrated_revision=args.integrated_revision,
        )
    elif args.control_op == "resolve":
        out = cmd_resolve(
            **common, board_db=args.board_db,
            dispatch_id=args.dispatch_id, actor=args.actor,
        )
    else:  # pragma: no cover - argparse enforces choices
        raise ControlError(f"unknown control op {args.control_op!r}")
    _print_json(out)
    return 0


def _compile(args: argparse.Namespace) -> int:
    if args.apply:
        policy = load_policy(args.policy)
        result = apply_plan(
            policy,
            target_name=args.apply,
            credential_file=args.credential_file,
        )
        _print_json(result)
        return 0
    _print_json(compile_from_path(args.policy))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-provider-kanban",
        description="Jev kanban path: shadow classifier, thin dispatcher, control",
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

    dispatch = sub.add_parser(
        "dispatch", help="one bounded dispatch tick over ready jev-native cards"
    )
    dispatch.add_argument("--board-db", required=True)
    dispatch.add_argument("--policy", required=True)
    dispatch.add_argument("--store", required=True,
                          help="sidecar receipt store (dispatch.db)")
    dispatch.add_argument("--once", action="store_true",
                          help="run exactly one tick (required; there is no loop)")

    status = sub.add_parser(
        "status", help="reconcile a dispatch receipt with wrapper+kanban truth"
    )
    status.add_argument("--board-db", required=True)
    status.add_argument("--policy", required=True)
    status.add_argument("--store", required=True)
    group = status.add_mutually_exclusive_group(required=True)
    group.add_argument("--dispatch-id", default=None)
    group.add_argument("--task-id", default=None)

    control = sub.add_parser(
        "control", help="cancel/approve/deny/accept/resolve by receipt"
    )
    control.add_argument("--board-db", required=True)
    control.add_argument("--policy", required=True)
    control.add_argument("--store", required=True)
    control.add_argument("--actor", required=True,
                         help="configured operator identity (see control.operators)")
    csub = control.add_subparsers(dest="control_op", required=True)
    csub.add_parser("cancel").add_argument("--dispatch-id", required=True)
    csub.add_parser("approve").add_argument("--approval-id", required=True)
    csub.add_parser("deny").add_argument("--approval-id", required=True)
    accept = csub.add_parser("accept")
    accept.add_argument("--task-id", required=True)
    accept.add_argument("--integrated-revision", required=True,
                        help="full 40-hex commit that integrated the work")
    csub.add_parser("resolve").add_argument("--dispatch-id", required=True)

    compile_cmd = sub.add_parser(
        "compile", help="render the policy into 9Router payloads (dry-run default)"
    )
    compile_cmd.add_argument("--policy", required=True)
    compile_cmd.add_argument("--apply", default=None, metavar="TARGET",
                             help="apply to this disposable gateway target "
                                  "(explicit opt-in; refused for production)")
    compile_cmd.add_argument("--credential-file", default=None,
                             help="JSON file with management_token + upstream_key")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "schema":
            return _schema(args)
        if args.command == "shadow":
            return _shadow(args)
        if args.command == "dispatch":
            if not args.once:
                parser.exit(2, "error: dispatch requires --once (no scheduling loop)\n")
            return _dispatch(args)
        if args.command == "status":
            return _status(args)
        if args.command == "control":
            return _control(args)
        if args.command == "compile":
            if args.apply and not args.credential_file:
                parser.exit(2, "error: --apply requires --credential-file\n")
            return _compile(args)
    except (ShadowError, CacheError, DispatchError, ControlError, StoreError,
            KernelError, CompileError, PolicyError, FileNotFoundError,
            ValidationError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
