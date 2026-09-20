"""Normalized run/result views shared by the HTTP routes."""

from __future__ import annotations

from typing import Any

from cli_provider_core import ArtifactRecord, AttemptRecord, RunResultView


def run_view(
    record: AttemptRecord, artifacts: list[ArtifactRecord] | None = None
) -> dict[str, Any]:
    view = RunResultView(
        run_id=record.run_id,
        task_id=record.task_id,
        attempt_id=record.attempt_id,
        preset=record.preset,
        driver_id=record.driver_id,
        runner_instance=record.runner_instance,
        workspace_id=record.workspace_id,
        status=record.status,
        outcome=record.outcome,
        cached=record.cached,
        synthetic=record.synthetic,
        execution=record.execution,
        summary=record.summary,
        verification=record.verification or {},
        usage=record.usage or {"provenance": "unknown"},
        artifacts=[a.artifact_id for a in (artifacts or [])],
        detail=record.detail,
        created_at=record.created_at,
        finished_at=record.finished_at,
    )
    return view.model_dump(mode="json")
