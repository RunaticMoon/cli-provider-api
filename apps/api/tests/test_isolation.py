"""The API process must not import any driver package, proven in a fresh subprocess."""

from __future__ import annotations

import subprocess
import sys

CHECK = r"""
import json
import sys

import cli_provider_api  # noqa: F401
from cli_provider_api.app import create_app  # noqa: F401

drivers = sorted(m for m in sys.modules if m.startswith("cli_driver"))
# The API needs only the Runner *protocol* and the bounded UDS client. It must
# never reach the driver-loading module or the Runner server module.
loader_imported = "cli_provider_runner.registry" in sys.modules
server_imported = "cli_provider_runner.server" in sys.modules
print(json.dumps({
    "drivers": drivers,
    "loader_imported": loader_imported,
    "server_imported": server_imported,
}))
"""


def test_api_import_does_not_pull_in_driver_packages():
    result = subprocess.run(
        [sys.executable, "-c", CHECK],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    import json

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["drivers"] == [], payload
    # Meaningful isolation: importing the API must not reach the driver loader
    # (which can call entry_point.load()) or the Runner server (which imports
    # drivers and owns the socket).
    assert payload["loader_imported"] is False, payload
    assert payload["server_imported"] is False, payload


def test_core_import_does_not_pull_in_driver_packages():
    code = (
        "import json, sys; import cli_provider_core; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('cli_driver'))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "[]"
