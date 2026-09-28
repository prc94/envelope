from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Fingerprint


class DockerTarget:
    def __init__(self, image: str, name: str):
        self.image, self.name = image, name

    def run(self, argv: list[str], timeout: float | None = None) -> tuple[int, bytes, bytes]:
        proc = subprocess.run(
            ["docker", "exec", self.name, *argv], capture_output=True, timeout=timeout, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr

    def put(self, source: Path, destination: str) -> None:
        subprocess.run(["docker", "cp", str(source), f"{self.name}:{destination}"], check=True)

    def get(self, source: str, destination: Path) -> None:
        subprocess.run(["docker", "cp", f"{self.name}:{source}", str(destination)], check=True)

    def fingerprint(self) -> Fingerprint:
        proc = subprocess.run(
            ["docker", "inspect", "--format={{.Image}}", self.name], capture_output=True, check=True
        )
        return Fingerprint({"image": proc.stdout.decode().strip()})
