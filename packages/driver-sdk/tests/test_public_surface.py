"""The exact public SDK surface, and a driver defined against it directly.

``UserShapeDriver`` below is written only from ``cli_provider_sdk.types`` and is
exercised through the same call signatures the Runner uses. No adapter layer is
involved.
"""

import asyncio
import inspect
from datetime import datetime, timezone
from typing import AsyncIterator

from cli_provider_sdk import ProviderDriver, SDK_VERSION, TransportKind
from cli_provider_sdk import models as _models
from cli_provider_sdk.types import (
    CancelResult,
    DriverManifest,
    ModelDescriptor,
    NormalizedRequest,
    ProbeReport,
    RunEvent,
    RuntimeContext,
)


def test_types_module_exposes_exact_public_surface():
    import cli_provider_sdk.types as types

    assert types.__all__ == [
        "CancelResult",
        "DriverManifest",
        "ModelDescriptor",
        "NormalizedRequest",
        "ProbeReport",
        "RunEvent",
        "RuntimeContext",
    ]
    # The public names are the canonical validated models themselves.
    assert ModelDescriptor is _models.ModelDescriptor
    assert NormalizedRequest is _models.NormalizedRequest
    assert ProbeReport is _models.ProbeReport
    assert DriverManifest is _models.DriverManifest
    assert CancelResult is _models.CancelResult


class UserShapeDriver:
    """A driver written exactly to the user's ProviderDriver Protocol."""

    def __init__(self) -> None:
        self._cancelled = asyncio.Event()
        self.closed = False

    @property
    def manifest(self) -> DriverManifest:
        return DriverManifest(
            driver_id="user-shape",
            name="User Shape Driver",
            version="1.2.3",
            sdk_version=SDK_VERSION,
            protocol_family="user-shape",
            supported_transports=[TransportKind.STDIO],
        )

    async def probe(self, ctx: RuntimeContext) -> ProbeReport:
        return ProbeReport(
            ok=True,
            driver_id=self.manifest.driver_id,
            driver_version=self.manifest.version,
            capabilities=_models.Capabilities(
                streaming="native",
                sessions="none",
                roles="serialized",
                structured_output="none",
                external_tool_calls=False,
                internal_tools=False,
                vision=False,
                workspace_write=False,
                web_search=False,
                usage="unknown",
            ),
        )

    async def discover_models(self, ctx: RuntimeContext) -> list[ModelDescriptor]:
        return [
            ModelDescriptor(
                model_id="user-shape-model",
                display_name="User Shape Model",
                verification=_models.Verification(
                    status="unknown", source="user-shape"
                ),
            )
        ]

    def execute(
        self, request: NormalizedRequest, ctx: RuntimeContext
    ) -> AsyncIterator[RunEvent]:
        async def generator() -> AsyncIterator[RunEvent]:
            now = datetime.now(timezone.utc)
            yield _models.MessageDeltaEvent(
                run_id=request.run_id,
                sequence=1,
                timestamp=now,
                payload=_models.MessageDeltaPayload(text="hello from user shape"),
            )
            yield _models.RunCompletedEvent(
                run_id=request.run_id,
                sequence=2,
                timestamp=now,
                payload=_models.RunCompletedPayload(
                    outcome="succeeded",
                    usage=_models.Usage(provenance="unknown"),
                ),
            )

        return generator()

    async def cancel(self, run_id: str, ctx: RuntimeContext) -> CancelResult:
        self._cancelled.set()
        now = datetime.now(timezone.utc)
        return CancelResult(
            run_id=run_id,
            requested=True,
            requested_at=now,
            confirmed=True,
            confirmed_at=now,
            deadline_seconds=1.0,
        )

    async def aclose(self) -> None:
        self.closed = True


def test_user_shape_driver_satisfies_protocol_without_adaptation():
    driver = UserShapeDriver()
    assert isinstance(driver, ProviderDriver)


def test_protocol_signatures_take_context():
    for name in ("probe", "discover_models"):
        params = list(inspect.signature(getattr(UserShapeDriver, name)).parameters)
        assert params == ["self", "ctx"]
    params = list(inspect.signature(UserShapeDriver.cancel).parameters)
    assert params == ["self", "run_id", "ctx"]
    params = list(inspect.signature(UserShapeDriver.execute).parameters)
    assert params == ["self", "request", "ctx"]
    assert list(inspect.signature(ProviderDriver.probe).parameters) == ["self", "ctx"]
    assert list(inspect.signature(ProviderDriver.cancel).parameters) == [
        "self",
        "run_id",
        "ctx",
    ]


def test_user_shape_driver_runs_with_context_arguments():
    async def scenario() -> tuple:
        driver = UserShapeDriver()
        ctx = RuntimeContext()
        probe = await driver.probe(ctx)
        models = await driver.discover_models(ctx)
        events = [event async for event in driver.execute(_request(), ctx)]
        cancel = await driver.cancel("run-1", ctx)
        await driver.aclose()
        return probe, models, events, cancel

    probe, models, events, cancel = asyncio.run(scenario())
    assert probe.driver_id == "user-shape"
    assert models[0].model_id == "user-shape-model"
    assert [event.kind for event in events] == ["message.delta", "run.completed"]
    assert cancel.confirmed is True


def _request() -> NormalizedRequest:
    return NormalizedRequest(
        run_id="run-1",
        task_id="task-1",
        attempt_id="att-1",
        preset="mock/text",
        workspace=_models.WorkspaceRef(workspace_id="ws-1"),
        messages=[_models.Message(role="user", content="hi")],
    )


def test_manifest_carries_sdk_version_and_transports():
    manifest = UserShapeDriver().manifest
    assert manifest.sdk_version == SDK_VERSION
    assert manifest.supported_transports == [TransportKind.STDIO]
