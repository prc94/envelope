import subprocess
import sys

import pytest

from envelope import _pycheck


def test_supported_version_passes() -> None:
    assert _pycheck.version_error((3, 12, 0, "final", 0)) is None
    assert _pycheck.version_error((3, 14, 1, "final", 0)) is None


def test_old_version_exits_nonzero_with_message(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        _pycheck.ensure_supported((3, 11, 9, "final", 0))
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "requires Python 3.12 or newer" in err
    assert "3.11.9" in err


def test_main_module_checks_version_before_importing_app() -> None:
    # Simulate an old interpreter by patching sys.version_info before running __main__.
    code = (
        "import sys, runpy; sys.version_info = (3, 11, 0, 'final', 0); "
        "runpy.run_module('envelope', run_name='__main__')"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 2
    assert "requires Python 3.12" in proc.stderr
    assert proc.stdout == ""
