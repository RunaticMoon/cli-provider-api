#!/usr/bin/env python3
"""Stdlib-only consumer for the authenticated catalog endpoint.

Default mode is read-only: it prints the catalog sources and models the API
key may see, including each entry's ``executable`` flag and ``rejection``
reason. Submitting a run requires an explicit opt-in plus the exact alias,
workspace and task id — the client never picks a model for you:

    # Discovery only (no run, no side effects):
    CPA_BASE=http://127.0.0.1:8080 CPA_KEY_FILE=./runtime/local.key \
        python examples/catalog_client.py

    # Explicit run against an exact alias the catalog marks executable:
    ... --run --model mock/mock-effort --workspace ws-alpha --task-id t-1
    ... [--effort low]   # only when the descriptor advertises it selectable

This talks to the wrapper API directly. Behind a 9Router-style gateway the
same calls apply with the gateway's ``<prefix>/<model>`` alias form, and
``reasoning_effort`` should additionally be duplicated inside ``metadata`` —
the verified carrier (the gateway forwards metadata verbatim; the API
requires the two carriers to agree exactly when both are sent). This example
does not implement any gateway proxy itself.

No third-party packages. The API key is read from a file so it never appears
in argv (``ps``). Redirects are refused rather than followed, so the bearer
credential can never leak to a different origin.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a followed redirect would forward the Authorization
    header to an origin the operator never configured."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _key() -> str:
    path = os.environ.get("CPA_KEY_FILE")
    if not path:
        sys.exit("set CPA_KEY_FILE to a 0600 file containing the API key")
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def _request(opener, base: str, key: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with opener.open(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # Never echo the provider body — an error page can carry data the
        # caller did not intend to surface. The status code is enough.
        sys.exit(f"{path} -> HTTP {exc.code}")
    except urllib.error.URLError as exc:
        sys.exit(f"{path} -> connection failed: {exc.reason}")


def _catalog_entry(catalog: dict, alias: str) -> dict | None:
    for src in catalog.get("data", []):
        for model in src.get("models", []):
            if model.get("alias") == alias:
                return model
    return None


def _print_catalog(catalog: dict) -> None:
    for src in catalog.get("data", []):
        stale = " STALE" if src.get("stale") else ""
        print(
            f"[{src.get('source')}] driver={src.get('driver_id')} "
            f"ok={src.get('ok')}{stale} observed_at={src.get('observed_at')}"
        )
        for model in src.get("models", []):
            flag = "executable" if model.get("executable") else "read-only "
            effort = model.get("effort", "unknown")
            print(f"  {flag} {model.get('alias')}  effort={effort}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read the authenticated catalog; run only on explicit opt-in."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="actually submit a chat completion (default: discovery only)",
    )
    parser.add_argument("--model", help="exact catalog alias to run")
    parser.add_argument("--workspace", help="workspace_id for the run")
    parser.add_argument("--task-id", help="task_id for the run")
    parser.add_argument(
        "--effort",
        help="reasoning_effort token; must be advertised selectable on the model",
    )
    parser.add_argument(
        "--prompt", default="Say hello in one word.", help="user prompt text"
    )
    args = parser.parse_args()

    if args.run:
        missing = [
            flag
            for flag, value in (
                ("--model", args.model),
                ("--workspace", args.workspace),
                ("--task-id", args.task_id),
            )
            if not value
        ]
        if missing:
            parser.error(f"--run requires {', '.join(missing)}")
    elif args.model or args.workspace or args.task_id:
        parser.error("--model/--workspace/--task-id only apply with --run")

    base = os.environ.get("CPA_BASE", "http://127.0.0.1:8080")
    key = _key()

    catalog = _request(_OPENER, base, key, "/api/v1/catalog")
    _print_catalog(catalog)
    if not args.run:
        return 0

    entry = _catalog_entry(catalog, args.model)
    if entry is None:
        sys.exit(f"{args.model!r} is not a visible catalog alias")
    if not entry.get("executable"):
        sys.exit(
            f"{args.model!r} is visible but not executable: "
            f"{entry.get('rejection') or 'no execution grant'}"
        )
    if args.effort is not None:
        if entry.get("effort") != "selectable" or args.effort not in (
            entry.get("effort_options") or []
        ):
            sys.exit(
                f"{args.model!r} does not advertise selectable effort "
                f"{args.effort!r} (effort={entry.get('effort', 'unknown')})"
            )

    # Duplicate the effort inside metadata: when this client sits behind a
    # gateway that forwards metadata verbatim, the metadata carrier is the one
    # that survives; the API requires both carriers to agree when both exist.
    metadata = {"task_id": args.task_id, "workspace_id": args.workspace}
    if args.effort is not None:
        metadata["reasoning_effort"] = args.effort
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": args.prompt}],
        "metadata": metadata,
    }
    if args.effort is not None:
        body["reasoning_effort"] = args.effort

    result = _request(_OPENER, base, key, "/v1/chat/completions", body)
    run = result.get("run") or {}
    choice = (result.get("choices") or [{}])[0]
    print(
        json.dumps(
            {
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "outcome": run.get("outcome"),
                "model_binding": run.get("model"),
                "text": choice.get("message", {}).get("content"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
