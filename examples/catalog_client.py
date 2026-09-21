#!/usr/bin/env python3
"""Stdlib-only consumer for the dynamic catalog endpoint.

Reads the authenticated catalog (``GET /api/v1/catalog``), picks an
``executable`` model entry, and submits a validated chat request — including
``reasoning_effort`` only when the descriptor advertises ``selectable``
support for that exact option.

Usage:
    CPA_BASE=http://127.0.0.1:8080 CPA_KEY_FILE=./runtime/local.key \
        python examples/catalog_client.py [--source NAME] [--effort low]

No third-party packages. The API key is read from a file so it never appears
in argv (``ps``) or the process environment of children.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _key() -> str:
    path = os.environ.get("CPA_KEY_FILE")
    if not path:
        sys.exit("set CPA_KEY_FILE to a 0600 file containing the API key")
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def _request(base: str, key: str, path: str, body: dict | None = None) -> dict:
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        sys.exit(f"{path} -> HTTP {exc.code}: {detail}")


def pick_model(catalog: dict, *, source: str | None, effort: str | None) -> tuple[str, str | None]:
    """Choose an executable alias; effort only when advertised as selectable."""
    for src in catalog.get("data", []):
        if source is not None and src.get("source") != source:
            continue
        for model in src.get("models", []):
            if not model.get("executable"):
                continue
            if effort is not None:
                if model.get("effort") != "selectable":
                    continue
                if effort not in (model.get("effort_options") or []):
                    continue
            return model["alias"], effort if model.get("effort") == "selectable" else None
    sys.exit("no executable catalog model matches (check grants/effort)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", help="catalog source name to restrict to")
    parser.add_argument("--effort", help="reasoning_effort token (must be advertised)")
    parser.add_argument("--task-id", default="catalog-client-demo")
    args = parser.parse_args()

    base = os.environ.get("CPA_BASE", "http://127.0.0.1:8080")
    key = _key()

    catalog = _request(base, key, "/api/v1/catalog")
    alias, effort = pick_model(catalog, source=args.source, effort=args.effort)

    # When the client may sit behind 9Router, duplicate the effort inside
    # ``metadata`` — the gateway forwards metadata verbatim but strips the
    # top-level field. The API requires both carriers to agree exactly.
    metadata = {"task_id": args.task_id, "workspace_id": "ws-alpha"}
    if effort is not None:
        metadata["reasoning_effort"] = effort
    body = {
        "model": alias,
        "messages": [{"role": "user", "content": "Say hello in one word."}],
        "metadata": metadata,
    }
    if effort is not None:
        body["reasoning_effort"] = effort

    result = _request(base, key, "/v1/chat/completions", body)
    run = result.get("run") or {}
    print(
        json.dumps(
            {
                "alias": alias,
                "effort": effort,
                "run_id": run.get("run_id"),
                "status": run.get("status"),
                "outcome": run.get("outcome"),
                "model_binding": run.get("model"),
                "text": "".join(
                    c.get("delta", {}).get("content", "")
                    for c in result.get("choices", [{}])
                ) or result.get("choices", [{}])[0].get("message", {}).get("content"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
