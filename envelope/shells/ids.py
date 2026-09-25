"""Built-in shell identifiers and alias normalization (pure, no I/O)."""

from __future__ import annotations

BUILTIN_SHELLS: tuple[str, ...] = (
    "bash",
    "sh",
    "zsh",
    "fish",
    "git-bash",
    "pwsh",
    "powershell",
    "cmd",
)

BASH_FAMILY = frozenset({"bash", "sh", "zsh", "git-bash"})
POWERSHELLS = frozenset({"pwsh", "powershell"})

_ALIASES: dict[str, str] = {
    "gitbash": "git-bash",
    "git_bash": "git-bash",
    "git bash": "git-bash",
    "powershell7": "pwsh",
    "pwsh7": "pwsh",
    "powershell_7": "pwsh",
    "powershell-7": "pwsh",
    "ps7": "pwsh",
    "windows-powershell": "powershell",
    "windowspowershell": "powershell",
    "powershell5": "powershell",
    "cmd.exe": "cmd",
    "bash.exe": "bash",
    "pwsh.exe": "pwsh",
    "powershell.exe": "powershell",
}


def canonical_shell_id(name: str) -> str:
    """Map a user- or model-supplied shell name to its canonical identifier.

    Only normalizes spelling; whether the shell exists, and platform-specific
    aliases such as ``bash`` -> ``git-bash`` on Windows, are the registry's job.
    """
    key = name.strip().lower()
    return _ALIASES.get(key, key)
