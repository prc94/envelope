#!/usr/bin/env python3
"""Measure the facts that make a target comparable between jobs. Standard library only.

Runs on the target (the runner streams it to `python -`) and prints one JSON object:

    os          platform string (Windows: includes the build, e.g. Windows-10-10.0.19044-SP0)
    acp, oemcp  ANSI and OEM code pages (Windows: from the registry, so not the SSH session's)
    locale      system default locale id (Windows registry `Nls\\Language\\Default`)
    python      interpreter version
    versions    versions of the harnesses named in argv (`--tool qwen`, ...)
    hashes      SHA-256 of the python, git and named tool binaries
    wdac        SHA-256 of the active Windows Defender Application Control policy files
    applocker   SHA-256 of the AppLocker rule set (registry SrpV2 subtree), canonicalized
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version(name: str) -> str | None:
    path = shutil.which(name)
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (proc.stdout or proc.stderr).strip().splitlines()[0] if proc.returncode == 0 else None


def registry_value(key: str, name: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            return str(winreg.QueryValueEx(handle, name)[0])
    except OSError:
        return None


def registry_dump(root: str) -> list[tuple[str, str, str]]:
    """Every (key, value name, data) under a registry key, sorted, for hashing."""
    if sys.platform != "win32":
        return []
    import winreg

    found: list[tuple[str, str, str]] = []

    def walk(path: str) -> None:
        try:
            handle = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            return
        with handle:
            index = 0
            while True:
                try:
                    name, data, _ = winreg.EnumValue(handle, index)
                except OSError:
                    break
                found.append((path, name, repr(data)))
                index += 1
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(handle, index)
                except OSError:
                    break
                walk(f"{path}\\{sub}")
                index += 1

    walk(root)
    return sorted(found)


def measure(tools: list[str]) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "versions": {tool: tool_version(tool) for tool in tools},
    }
    binaries = {"python": sys.executable}
    for name in ["git", *tools]:
        path = shutil.which(name)
        if path:
            binaries[name] = path
    facts["hashes"] = {name: sha256(path) for name, path in sorted(binaries.items())}
    if sys.platform == "win32":
        nls = r"SYSTEM\CurrentControlSet\Control\Nls"
        facts["acp"] = registry_value(nls + r"\CodePage", "ACP")
        facts["oemcp"] = registry_value(nls + r"\CodePage", "OEMCP")
        facts["locale"] = registry_value(nls + r"\Language", "Default")
        policies = Path(r"C:\Windows\System32\CodeIntegrity\CIPolicies\Active")
        facts["wdac"] = {p.name: sha256(p) for p in sorted(policies.glob("*.cip"))}
        rules = registry_dump(r"SOFTWARE\Policies\Microsoft\Windows\SrpV2")
        facts["applocker"] = hashlib.sha256(json.dumps(rules).encode("utf-8")).hexdigest()
    return facts


if __name__ == "__main__":
    tools = [a for i, a in enumerate(sys.argv[1:], 1) if sys.argv[i - 1] == "--tool"]
    print(json.dumps(measure(tools), sort_keys=True))
