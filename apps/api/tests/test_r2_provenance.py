"""The run view exposes the persisted synthetic provenance on every surface.

The mock Runner is synthetic, so every attempt is labelled `synthetic: true`
(never a hard-coded/default claim). Covered: the non-stream body, the status
read and the SSE final chunk.
"""

from __future__ import annotations

import json

from conftest import MockSystem

CHAT = "/v1/chat/completions"


def body(task="task-prov", workspace="ws-alpha", **extra):
    payload = {
        "model": "mock/text",
        "messages": [{"role": "user", "content": "hello"}],
        "metadata": {"task_id": task, "workspace_id": workspace},
    }
    payload.update(extra)
    return payload


def _sse_payloads(text: str) -> list:
    out = []
    for line in text.splitlines():
        if line.startswith("data: "):
            raw = line[len("data: ") :]
            out.append("[DONE]" if raw == "[DONE]" else json.loads(raw))
    return out


def test_nonstream_and_status_expose_synthetic_provenance(system: MockSystem):
    with system.client() as client:
        response = client.post(CHAT, json=body())
        assert response.status_code == 200
        run = response.json()["run"]
        assert run["synthetic"] is True

        fetched = client.get(f"/api/v1/runs/{run['run_id']}").json()
        assert fetched["synthetic"] is True


def test_sse_final_chunk_exposes_synthetic_provenance(system: MockSystem):
    with system.client() as client:
        with client.stream("POST", CHAT, json=body(task="task-prov-sse", stream=True)) as response:
            run_id = response.headers["X-Run-Id"]
            text = "".join(response.iter_text())
    chunks = [p for p in _sse_payloads(text) if p != "[DONE]"]
    assert chunks[-1]["run"]["run_id"] == run_id
    assert chunks[-1]["run"]["synthetic"] is True
