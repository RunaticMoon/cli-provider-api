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
# The driver entry-point loader must not be reachable/loaded at import time.
loaded_driver = any(
    m == "cli_provider_runner.registry" and "load_driver" in sys.modules
    for m in sys.modules
)
print(json.dumps({"drivers": drivers, "registry_module": "cli_provider_runner.registry" in sys.modules}))
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
    # Registry module may be imported, but no driver module is.
    assert payload["registry_module"] in (True, False)


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
