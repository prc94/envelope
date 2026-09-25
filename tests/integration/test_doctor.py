from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.client import REPO
from tests.integration.conftest import posix_only

pytestmark = [pytest.mark.integration, posix_only]


def run_doctor(
    tmp_path: Path, *args: str, config: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PYTHONPATH": os.pathsep.join([str(REPO), *filter(None, [os.environ.get("PYTHONPATH")])]),
    }
    if config is not None:
        (tmp_path / "c.toml").write_text(config)
        env["ENVELOPE_CONFIG"] = str(tmp_path / "c.toml")
    return subprocess.run(
        [sys.executable, "-m", "envelope", "doctor", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_doctor_text(tmp_path: Path) -> None:
    proc = run_doctor(tmp_path, config='defualt_shell = "bash"\n')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "envelope 0." in out and "python   3." in out
    assert "bash" in out and "default:" in out
    assert "unknown key 'defualt_shell'" in out
    assert "spool" in out and "(writable)" in out
    assert "status: OK" in out


def test_doctor_json(tmp_path: Path) -> None:
    proc = run_doctor(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert {"shells", "restrictions", "smoke_tests", "paths", "default_shell"} <= set(report)
    bash = next(s for s in report["shells"] if s["id"] == "bash")
    assert bash["available"] and bash["version"]
    assert report["smoke_tests"]["bash"] == {
        "trivial": {"status": "pass"},
        "large_script": {"status": "pass"},
        "utf8": {"status": "pass"},
    }
    assert report["paths"]["spool"]["writable"] is True


def test_doctor_reports_failures_nonzero(tmp_path: Path) -> None:
    fake = tmp_path / "liar"
    fake.write_text("#!/bin/sh\necho nope\n")
    fake.chmod(0o755)
    config = f'[shells.liar]\npath = "{fake}"\nargs = ["{{file}}"]\n'
    proc = run_doctor(tmp_path, "--json", config=config)
    report = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert report["smoke_tests"]["liar"]["trivial"]["status"] == "fail"
    assert report["smoke_tests"]["bash"]["trivial"]["status"] == "pass"
    assert any("liar" in p for p in report["problems"])
