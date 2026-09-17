"""Operator CLI: key material is never passed as a command-line argument."""

from __future__ import annotations

import os
import stat
import subprocess
import sys

from cli_provider_core import hash_api_key, load_config

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def run_cli(args: list[str], *, stdin: str | None = None, env: dict | None = None):
    return subprocess.run(
        [sys.executable, "-m", "cli_provider_api", *args],
        cwd=REPO_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_hash_key_reads_the_key_from_stdin():
    result = run_cli(["hash-key"], stdin="secret-from-stdin\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hash_api_key("secret-from-stdin")


def test_hash_key_reads_the_key_from_a_file(tmp_path):
    key_file = tmp_path / "local.key"
    key_file.write_text("secret-from-file\n", encoding="utf-8")
    result = run_cli(["hash-key", "--key-file", str(key_file)])
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hash_api_key("secret-from-file")


def test_hash_key_reads_the_key_from_an_environment_variable():
    env = os.environ.copy()
    env["LOCAL_TEST_KEY"] = "secret-from-env"
    result = run_cli(["hash-key", "--key-env", "LOCAL_TEST_KEY"], env=env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == hash_api_key("secret-from-env")


def test_hash_key_refuses_a_raw_key_argument():
    # `--key <secret>` must not exist: argparse rejects the unknown option.
    result = run_cli(["hash-key", "--key", "leaked-secret"])
    assert result.returncode != 0
    assert "leaked-secret" not in result.stdout


def test_hash_key_rejects_an_empty_key():
    result = run_cli(["hash-key"], stdin="\n")
    assert result.returncode != 0


def test_new_key_writes_a_0600_file_that_authenticates(tmp_path):
    out = tmp_path / "local.key"
    created = run_cli(["new-key", "--out", str(out)])
    assert created.returncode == 0, created.stderr
    assert out.exists()
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    key = out.read_text(encoding="utf-8").strip()
    # The generated key is only in the file, never echoed in the CLI output.
    assert key not in created.stdout
    digest = run_cli(["hash-key", "--key-file", str(out)])
    assert digest.stdout.strip() == hash_api_key(key)


def test_new_key_refuses_to_overwrite_without_force(tmp_path):
    out = tmp_path / "local.key"
    assert run_cli(["new-key", "--out", str(out)]).returncode == 0
    again = run_cli(["new-key", "--out", str(out)])
    assert again.returncode != 0
    forced = run_cli(["new-key", "--out", str(out), "--force"])
    assert forced.returncode == 0


def test_public_docs_never_show_a_key_in_argv():
    readme = (REPO_ROOT + "/README.md")
    with open(readme, encoding="utf-8") as handle:
        readme_text = handle.read()
    with open(REPO_ROOT + "/config.example.yaml", encoding="utf-8") as handle:
        example_text = handle.read()
    assert "hash-key --key" not in readme_text
    assert "hash-key --key" not in example_text
    # The documented flows use stdin / a key file.
    assert "hash-key <" in readme_text or "--key-file" in readme_text
    assert "new-key --out" in readme_text


def test_example_config_is_valid_and_has_placeholder_hashes():
    config = load_config(REPO_ROOT + "/config.example.yaml")
    assert config.schema_version == 1
    assert "mock/text" in config.preset_map()
    assert all(p.key_hash.startswith("sha256:") for p in config.principals)
