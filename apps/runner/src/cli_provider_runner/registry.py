"""Allowlisted driver loading.

Only the Runner loads drivers, and only when the installed distribution *name*
and *version* match an explicit operator allowlist entry. Nothing here is
reachable from a run request, and matching happens before ``entry_point.load()``
so a mismatched or unknown plugin is refused rather than imported.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cli_provider_sdk import SDK_VERSION, DriverManifest, ID_PATTERN, ProviderDriver

ENTRY_POINT_GROUP = "cli_provider.drivers"


class DriverAllowlistEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    driver_id: str = Field(pattern=ID_PATTERN)
    distribution: str = Field(min_length=1)
    version: str = Field(min_length=1)


class DriverLoadError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize_distribution(name: str) -> str:
    return name.replace("_", "-").lower()


def validate_manifest(
    manifest: Any, *, driver_id: str | None = None
) -> DriverManifest:
    """Fail closed on a missing or contract-invalid manifest.

    A driver that cannot present an explicit SDK version, at least one supported
    transport and a matching driver id is refused rather than loaded.
    """
    if manifest is None:
        raise DriverLoadError("MANIFEST_INVALID", "driver exposes no manifest")
    try:
        validated = (
            manifest
            if isinstance(manifest, DriverManifest)
            else DriverManifest.model_validate(manifest)
        )
    except ValidationError as exc:
        raise DriverLoadError(
            "MANIFEST_INVALID", f"driver manifest is invalid: {exc.error_count()} problem(s)"
        ) from exc

    if validated.sdk_version != SDK_VERSION:
        raise DriverLoadError(
            "SDK_VERSION_UNSUPPORTED",
            f"driver {validated.driver_id!r} targets SDK version "
            f"{validated.sdk_version!r}, runner supports {SDK_VERSION!r}",
        )
    if driver_id is not None and validated.driver_id != driver_id:
        raise DriverLoadError(
            "MANIFEST_INVALID",
            f"driver manifest driver_id {validated.driver_id!r} does not match "
            f"allowlisted id {driver_id!r}",
        )
    return validated


def _find_entry_point(driver_id: str) -> Any:
    entry_points = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    for entry_point in entry_points:
        if entry_point.name == driver_id:
            return entry_point
    return None


def load_driver(entry: DriverAllowlistEntry) -> ProviderDriver:
    entry_point = _find_entry_point(entry.driver_id)
    if entry_point is None:
        raise DriverLoadError(
            "NOT_ALLOWLISTED",
            f"no installed driver entry point named {entry.driver_id!r} in group "
            f"{ENTRY_POINT_GROUP!r}",
        )

    distribution = getattr(entry_point, "dist", None)
    if distribution is None:
        raise DriverLoadError(
            "NOT_ALLOWLISTED",
            f"driver {entry.driver_id!r} has no distribution metadata",
        )

    installed_name = distribution.name
    if _normalize_distribution(installed_name) != _normalize_distribution(
        entry.distribution
    ):
        raise DriverLoadError(
            "NOT_ALLOWLISTED",
            f"driver {entry.driver_id!r} comes from distribution {installed_name!r}, "
            f"not allowlisted {entry.distribution!r}",
        )

    installed_version = distribution.version
    if installed_version != entry.version:
        raise DriverLoadError(
            "DRIVER_VERSION_MISMATCH",
            f"driver {entry.driver_id!r} distribution {installed_name!r} is version "
            f"{installed_version!r}, not allowlisted {entry.version!r}",
        )

    loaded = entry_point.load()
    driver = loaded() if isinstance(loaded, type) else loaded

    validate_manifest(getattr(driver, "manifest", None), driver_id=entry.driver_id)
    return driver
