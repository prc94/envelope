"""Server instructions generated at startup (at most 2,000 characters)."""

from __future__ import annotations

import platform as platform_module

from envelope.shells.registry import Registry

MAX_CHARS = 2000

_OS_NAMES = {"linux": "Linux", "darwin": "macOS", "win32": "Windows"}


def build_instructions(registry: Registry, *, machine: str | None = None) -> str:
    os_name = _OS_NAMES[registry.platform]
    arch = machine or platform_module.machine() or "unknown"
    shells: list[str] = []
    for shell in registry.available():
        text = shell.describe()
        if shell.id == registry.default:
            text += " (default)"
        shells.append(text)
    lines = [
        f"Envelope runs shell commands on this {os_name} ({arch}) machine with the `run` tool.",
        f"Shells: {', '.join(shells) or 'none detected'}.",
    ]
    for shell in registry.available():
        if shell.language_mode or shell.execution_policy:
            parts: list[str] = []
            if shell.language_mode == "ConstrainedLanguage":
                parts.append(
                    "runs in Constrained Language Mode (.NET method calls, Add-Type and New-Object "
                    "for non-core types are blocked; prefer cmdlets)"
                )
            elif shell.language_mode:
                parts.append(f"language mode {shell.language_mode}")
            if shell.execution_policy:
                parts.append(f"execution policy {shell.execution_policy}")
            lines.append(f"PowerShell {shell.id}: {'; '.join(parts)}.")
    if registry.shells.get("git-bash") is not None and registry.shells["git-bash"].available:
        lines.append(
            "git-bash is Git for Windows bash: use POSIX syntax; paths may be written as /c/Users/x, "
            "C:/Users/x or C:\\Users\\x; Windows programs (e.g. where.exe, cmd /c) can be called from it."
        )
    if registry.platform == "win32" and registry.shells["cmd"].available:
        lines.append(
            "cmd commands are limited to 8,191 characters; use pwsh or git-bash for long scripts."
        )
    lines.extend(
        [
            "Every call starts a fresh process: cd, exports and variables do not persist, so pass "
            "cwd and env explicitly. Stdin is closed unless the stdin argument is given; interactive "
            "prompts fail fast, so use non-interactive flags.",
            "Long output is shortened to its first and last lines; the result then names a handle "
            '(out_...). Retrieve the rest with output(handle=..., grep="regex") or '
            "output(handle=..., offset=N, limit=M); output(handle=...) returns the tail.",
            "A non-zero exit code is a normal result, not a tool failure; read the output and notes.",
        ]
    )
    text = "\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 3].rstrip() + "..."
    return text
