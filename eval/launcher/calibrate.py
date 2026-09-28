#!/usr/bin/env python3
"""Capture what native Windows tools write, byte for byte, plus what this process's console looks
like. Standard library only; runs on the target under the launcher.

    python calibrate.py OUTPUT.json WORKDIR

WORKDIR must contain a directory `docs` with Cyrillic file names. The commands are the ones an
agent meets in a console: `dir`, `ipconfig`, `net`, `echo` of Cyrillic text, PowerShell, and the
same through git-bash. Each runs the way envelope starts its children (no console window).
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
BASH = r"C:\Program Files\Git\bin\bash.exe"
PWSH = r"C:\Program Files\PowerShell\7\pwsh.exe"

COMMANDS = [
    ("cmd-dir-cyrillic", ["cmd", "/c", "dir", "/b", "docs"]),
    ("cmd-chcp", ["cmd", "/c", "chcp"]),
    ("cmd-echo-cyrillic", ["cmd", "/c", "echo", "привет мир"]),
    ("cmd-ipconfig", ["cmd", "/c", "ipconfig"]),
    ("net-help", ["net", "help"]),
    ("net-user", ["net", "user"]),
    (
        "powershell-list-cyrillic",
        ["powershell", "-NoProfile", "-Command", "Get-ChildItem -Name docs"],
    ),
    (
        "powershell-encoding",
        ["powershell", "-NoProfile", "-Command", "[Console]::OutputEncoding.CodePage"],
    ),
    ("pwsh-encoding", [PWSH, "-NoProfile", "-Command", "[Console]::OutputEncoding.CodePage"]),
    ("gitbash-ls-cyrillic", [BASH, "-c", "ls docs"]),
    ("gitbash-native-dir", [BASH, "-c", 'cmd //c "dir /b docs"']),
    ("gitbash-native-echo", [BASH, "-c", 'cmd //c "echo привет мир"']),
    ("gitbash-echo-utf8", [BASH, "-c", "echo привет мир"]),
]


def session_info() -> dict[str, object]:
    if sys.platform != "win32":
        return {"platform": sys.platform}
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = ctypes.c_ulong()
    k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    return {
        "session_id": sid.value,
        "sessionname_env": os.environ.get("SESSIONNAME"),
        "username": os.environ.get("USERNAME"),
        "acp": k32.GetACP(),
        "oemcp": k32.GetOEMCP(),
        "console_input_cp": k32.GetConsoleCP(),
        "console_output_cp": k32.GetConsoleOutputCP(),
        "has_console_window": bool(k32.GetConsoleWindow()),
        "stdout_is_console": os.isatty(1),
    }


def main(argv: list[str]) -> int:
    output, workdir = argv[1], argv[2]
    results = []
    for name, command in COMMANDS:
        try:
            proc = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=90,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
            entry = {
                "name": name,
                "returncode": proc.returncode,
                "stdout": base64.b64encode(proc.stdout).decode(),
                "stderr": base64.b64encode(proc.stderr).decode(),
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            entry = {"name": name, "error": f"{type(exc).__name__}: {exc}"}
        results.append(entry)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump({"info": session_info(), "commands": results}, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
