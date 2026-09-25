from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration.client import StdioClient

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shells")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux containment")


@pytest.fixture
def client(tmp_path: Path) -> Iterator[StdioClient]:
    c = StdioClient(tmp_path, config='default_shell = "bash"\n')
    c.initialize()
    yield c
    c.close()


def pgrep(marker: str) -> list[str]:
    import subprocess

    out = subprocess.run(["ps", "-e", "-o", "pid=,args="], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if marker in line and "ps -e" not in line]
