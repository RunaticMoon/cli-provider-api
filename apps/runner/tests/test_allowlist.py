import json

import pytest

from cli_provider_sdk import SDK_VERSION, DriverManifest, TransportKind
from cli_provider_runner import RunnerServer
from cli_provider_runner.registry import (
    DriverAllowlistEntry,
    DriverLoadError,
    load_driver,
    validate_manifest,
)


def entry(**overrides) -> DriverAllowlistEntry:
    values = {"driver_id": "mock", "distribution": "cli-driver-mock", "version": "0.1.0"}
    values.update(overrides)
    return DriverAllowlistEntry(**values)


def manifest(**overrides) -> DriverManifest:
    values = {
        "driver_id": "mock",
        "name": "Mock",
        "version": "0.1.0",
        "sdk_version": SDK_VERSION,
        "protocol_family": "mock",
        "supported_transports": [TransportKind.STDIO],
    }
    values.update(overrides)
    return DriverManifest(**values)


def test_allowlisted_installed_driver_loads():
    driver = load_driver(entry())
    assert driver.manifest.driver_id == "mock"
    assert driver.manifest.synthetic is True
    assert driver.manifest.sdk_version == SDK_VERSION


def test_unknown_driver_id_is_refused_before_load():
    with pytest.raises(DriverLoadError) as excinfo:
        load_driver(entry(driver_id="not-installed"))
    assert excinfo.value.code == "NOT_ALLOWLISTED"


def test_distribution_mismatch_is_refused():
    with pytest.raises(DriverLoadError) as excinfo:
        load_driver(entry(distribution="some-other-dist"))
    assert excinfo.value.code == "NOT_ALLOWLISTED"


def test_version_mismatch_is_refused():
    with pytest.raises(DriverLoadError) as excinfo:
        load_driver(entry(version="9.9.9"))
    assert excinfo.value.code == "DRIVER_VERSION_MISMATCH"


def test_manifest_validation_fails_closed():
    assert validate_manifest(manifest()).driver_id == "mock"

    with pytest.raises(DriverLoadError) as excinfo:
        validate_manifest(None)
    assert excinfo.value.code == "MANIFEST_INVALID"

    with pytest.raises(DriverLoadError) as excinfo:
        validate_manifest({"driver_id": "mock", "name": "Mock", "version": "0.1.0"})
    assert excinfo.value.code == "MANIFEST_INVALID"

    with pytest.raises(DriverLoadError) as excinfo:
        validate_manifest(manifest(sdk_version="0.0"))
    assert excinfo.value.code == "SDK_VERSION_UNSUPPORTED"

    with pytest.raises(DriverLoadError) as excinfo:
        validate_manifest(manifest(), driver_id="other")
    assert excinfo.value.code == "MANIFEST_INVALID"


class _BadManifestDriver:
    """A structurally valid driver whose manifest contract is unsupported."""

    def __init__(self, sdk_version: str) -> None:
        self._sdk_version = sdk_version

    @property
    def manifest(self) -> DriverManifest:
        return manifest(sdk_version=self._sdk_version)

    async def probe(self, ctx):  # pragma: no cover - never reached
        raise AssertionError("must not run")

    async def discover_models(self, ctx):  # pragma: no cover - never reached
        raise AssertionError("must not run")

    def execute(self, request, ctx):  # pragma: no cover - never reached
        raise AssertionError("must not run")

    async def cancel(self, run_id, ctx):  # pragma: no cover - never reached
        raise AssertionError("must not run")

    async def aclose(self) -> None:
        return None


def test_injected_driver_with_bad_manifest_fails_closed(tmp_path):
    server = RunnerServer(
        socket_path=str(tmp_path / "bad.sock"),
        instance_id="bad",
        driver=_BadManifestDriver("0.0"),
    )
    with pytest.raises(DriverLoadError) as excinfo:
        server.load()
    assert excinfo.value.code == "SDK_VERSION_UNSUPPORTED"


def test_runner_refuses_unknown_driver_with_structured_error(runner_factory):
    runner = runner_factory("success", driver_id="nope", wait=False)
    code, _out, err = runner.wait_exit()
    assert code == 2
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["code"] == "NOT_ALLOWLISTED"


def test_runner_refuses_version_mismatch_with_structured_error(runner_factory):
    runner = runner_factory("success", version="9.9.9", wait=False)
    code, _out, err = runner.wait_exit()
    assert code == 2
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["code"] == "DRIVER_VERSION_MISMATCH"


def test_runner_refuses_distribution_mismatch_with_structured_error(runner_factory):
    runner = runner_factory("success", distribution="other-dist", wait=False)
    code, _out, err = runner.wait_exit()
    assert code == 2
    payload = json.loads(err.strip().splitlines()[-1])
    assert payload["code"] == "NOT_ALLOWLISTED"
