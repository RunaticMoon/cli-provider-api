"""FastAPI application factory.

The API process never imports a driver package: Runners are reached only through
the validated UDS client session held by the core registry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from cli_provider_core import (
    HeadersTooLarge,
    OperatorConfig,
    RunController,
    RunnerRegistry,
    Store,
)

from . import chat, health, models, runs
from .errors import error_payload, install_handlers


def _header_bytes(request: Request) -> int:
    return sum(
        len(name.encode("latin-1")) + len(value.encode("latin-1")) + 4
        for name, value in request.headers.items()
    )


def create_app(config: OperatorConfig) -> FastAPI:
    store = Store(config.db_path())
    store.initialize()
    registry = RunnerRegistry(config)
    controller = RunController(config=config, store=store, registry=registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await registry.refresh()
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="cli-provider-api",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.store = store
    app.state.registry = registry
    app.state.controller = controller

    install_handlers(app)

    @app.middleware("http")
    async def _bound_headers(request: Request, call_next):
        limit = config.api.limits.max_headers_bytes
        if _header_bytes(request) > limit:
            error = HeadersTooLarge(
                f"request headers exceed the configured limit ({limit} bytes)"
            )
            return JSONResponse(
                status_code=error.http_status, content=error_payload(error)
            )
        return await call_next(request)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        from cli_provider_core import CoreError

        if isinstance(exc, CoreError):  # defensive: handled above
            return JSONResponse(
                status_code=exc.http_status, content=error_payload(exc)
            )
        return JSONResponse(
            status_code=500,
            content=error_payload(
                type("_E", (CoreError,), {})("internal server error")
            ),
        )

    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(runs.router)
    return app
