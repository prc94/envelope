"""Deterministic hint catalog. A hint is emitted only when its condition is
certain from exit status, signal, or a catalogued output pattern."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from envelope.shells.ids import BASH_FAMILY, POWERSHELLS


@dataclass(frozen=True)
class Hint:
    id: str
    text: str


@dataclass
class HintContext:
    shell: str
    exit_code: int | None
    signal: int | None = None
    timed_out: bool = False
    timeout_s: float = 0.0
    max_timeout_s: float = 3600.0
    tree_terminated: bool | None = None
    output_tail: Sequence[str] = ()
    """Last cleaned output lines (both streams)."""
    last_partial: str | None = None
    """Final line when output ended without a newline (typical for prompts)."""
    stdin_supplied: bool = False
    binary: Sequence[tuple[str, int, str | None]] = ()
    """(stream, byte count, detected type) for streams that were binary."""
    replaced: int = 0
    encodings: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    ps_errors: int | None = None
    locate: Callable[[str], list[tuple[str, str]]] | None = None
    """command name -> [(other shell id, path)] where the command exists."""
    command: str = ""
    leftover_count: int | None = None
    """Descendants still running (and terminated) when the shell exited on
    its own; ``None`` while stopping/evicting/timing out, or on a platform
    that cannot tell (design B11)."""
    port_owner: str | None = None
    """Resolved by the caller (it needs the process table/port lookup, which
    this module stays free of); named only when the owner is one of
    envelope's own tracked processes (design B11: "omitted when unknown")."""


SIGNAL_NAMES = {
    1: "SIGHUP",
    2: "SIGINT",
    3: "SIGQUIT",
    4: "SIGILL",
    5: "SIGTRAP",
    6: "SIGABRT",
    7: "SIGBUS",
    8: "SIGFPE",
    9: "SIGKILL",
    10: "SIGUSR1",
    11: "SIGSEGV",
    12: "SIGUSR2",
    13: "SIGPIPE",
    14: "SIGALRM",
    15: "SIGTERM",
    24: "SIGXCPU",
    25: "SIGXFSZ",
}

WINDOWS_CRASHES = {
    0xC0000005: "access violation (0xC0000005)",
    0xC00000FD: "stack overflow (0xC00000FD)",
    0xC0000409: "stack buffer overrun / fail-fast exception (0xC0000409)",
    0xC0000374: "heap corruption (0xC0000374)",
    0xC0000094: "integer division by zero (0xC0000094)",
    0xC000001D: "illegal instruction (0xC000001D)",
    0xC0000135: "a required DLL was not found (0xC0000135)",
    0xC0000142: "DLL initialization failed (0xC0000142)",
    0xC000013A: "terminated by Ctrl+C / Ctrl+Break (0xC000013A)",
    0xC0000017: "out of memory (0xC0000017)",
}

_NOT_FOUND_POSIX = (
    re.compile(r"(?:^|: )(?:line \d+: )?([^\s:'\"]+): (?:command )?not found\s*$"),
    re.compile(r"command not found: (\S+)\s*$"),
)
_NOT_FOUND_PS = re.compile(r"The term '([^']+)' is not recognized as")
_NOT_FOUND_CMD = re.compile(r"^'([^']+)' is not recognized as an internal or external command")
_APP_CONTROL = re.compile(
    r"blocked by group policy|blocked by your system administrator|Device Guard|AppLocker"
    r"|Application Control policy has blocked",
    re.IGNORECASE,
)
_CLM = re.compile(
    r"language mode|only (?:on )?core types|ConstrainedLanguage",
    re.IGNORECASE,
)
_CLM_CONTEXT = re.compile(r"not (?:supported|allowed)|only|cannot", re.IGNORECASE)
_EXEC_POLICY = re.compile(
    r"running scripts is disabled on this system|is not digitally signed\. You cannot run this script"
    r"|about_Execution_Policies",
    re.IGNORECASE,
)
_PROMPT = re.compile(
    r"(\[[yn]/[yn]\]|\((?:yes/no|y/n)[^)]*\)|password[^:]*:|passphrase[^:]*:|continue\?"
    r"|proceed\?|are you sure\??|press any key[^.]*\.?|overwrite\?)\s*$",
    re.IGNORECASE,
)
# Background constructs (design B11): a lone `&` that isn't part of `&&`,
# `2>&1`, `>&`, `&>` or `|&`, or `nohup`/`setsid`/`disown`. Every other `&`
# construct is stripped first, so any `&` still left over is a backgrounding
# operator, wherever in the command it falls.
_BG_REDIRECT_AMP = re.compile(r"&&|\|&|\d*>&\d*|&>")
_BG_LONE_AMP = re.compile(r"&")
_BG_POSIX_CMD = re.compile(r"\b(nohup|setsid|disown)\b")
_BG_CMD_START = re.compile(r"(?<![a-zA-Z])start\b", re.IGNORECASE)
_BG_PS_CMDLET = re.compile(r"\bstart-(?:process|job)\b", re.IGNORECASE)
_PORT_IN_USE = re.compile(
    r"EADDRINUSE|address already in use|Only one usage of each socket address|\b10048\b",
    re.IGNORECASE,
)
_PORT_NUMBER = re.compile(r":(\d{2,5})\b")


def extract_port_in_use(lines: Sequence[str]) -> int | None:
    """A port number from a line matching an address-in-use error, or
    ``None`` if no such line (or no extractable port) is present."""
    for line in reversed(lines):
        if _PORT_IN_USE.search(line):
            match = _PORT_NUMBER.search(line)
            if match:
                port = int(match.group(1))
                if 1 <= port <= 65535:
                    return port
    return None


def _missing_command(ctx: HintContext) -> str | None:
    lines = list(ctx.output_tail)
    patterns: tuple[re.Pattern[str], ...]
    if ctx.shell in POWERSHELLS:
        patterns = (_NOT_FOUND_PS,)
    elif ctx.shell == "cmd":
        patterns = (_NOT_FOUND_CMD,)
    else:
        patterns = _NOT_FOUND_POSIX
    for line in reversed(lines):
        for pattern in patterns:
            if match := pattern.search(line):
                return match.group(1)
    return None


def not_found(ctx: HintContext) -> Hint | None:
    bash_like = ctx.shell in BASH_FAMILY
    name = _missing_command(ctx)
    if bash_like and ctx.exit_code != 127:
        return None
    if not bash_like and name is None:
        return None
    if name is None:
        return Hint("command_not_found", "note: exit 127 means a command was not found on PATH")
    others = (
        ctx.locate(name) if ctx.locate is not None and "/" not in name and "\\" not in name else []
    )
    if others:
        where = ", ".join(f"{shell} ({path})" for shell, path in others)
        first = others[0][0]
        return Hint(
            "command_not_found",
            f"note: `{name}` was not found in {ctx.shell}; it is available in {where} "
            f'-- rerun with shell="{first}"',
        )
    return Hint(
        "command_not_found",
        f"note: `{name}` was not found in {ctx.shell}, nor on the PATH of any other available shell",
    )


def not_executable(ctx: HintContext) -> Hint | None:
    if ctx.shell in BASH_FAMILY and ctx.exit_code == 126:
        return Hint(
            "not_executable",
            "note: exit 126 means the command was found but could not be executed "
            "(permission denied or not a valid executable); check its mode or run it via its interpreter",
        )
    return None


def signaled(ctx: HintContext) -> Hint | None:
    if ctx.timed_out:
        return None
    number = ctx.signal
    if (
        number is None
        and ctx.shell in BASH_FAMILY
        and ctx.exit_code is not None
        and 129 <= ctx.exit_code <= 192
    ):
        number = ctx.exit_code - 128
    if number is None:
        return None
    name = SIGNAL_NAMES.get(number, f"signal {number}")
    text = f"note: the process was terminated by {name} (signal {number})"
    if number == 9:
        text += ", possibly by the out-of-memory killer"
    return Hint("signal", text)


def windows_crash(ctx: HintContext) -> Hint | None:
    if ctx.exit_code is None:
        return None
    description = WINDOWS_CRASHES.get(ctx.exit_code & 0xFFFFFFFF)
    if description is None:
        return None
    return Hint("windows_crash", f"note: the process crashed: {description}")


def timed_out(ctx: HintContext) -> Hint | None:
    if not ctx.timed_out:
        return None
    cleanup = (
        "the command and all its child processes were terminated"
        if ctx.tree_terminated
        else "some child processes could not be confirmed terminated"
    )
    more = (
        f"; pass a larger timeout_s (up to {ctx.max_timeout_s:g}) if it needs longer, or omit "
        "timeout_s and let it run in the background"
        if ctx.timeout_s < ctx.max_timeout_s
        else ""
    )
    return Hint("timed_out", f"note: timed out after {ctx.timeout_s:g}s; {cleanup}{more}")


def app_control(ctx: HintContext) -> Hint | None:
    if any(_APP_CONTROL.search(line) for line in ctx.output_tail):
        return Hint(
            "application_control",
            "note: a program was blocked by application control / group policy (AppLocker or WDAC); "
            "use an approved tool or shell instead",
        )
    return None


def constrained_language(ctx: HintContext) -> Hint | None:
    if ctx.shell not in POWERSHELLS:
        return None
    if any(_CLM.search(line) and _CLM_CONTEXT.search(line) for line in ctx.output_tail):
        return Hint(
            "constrained_language",
            "note: PowerShell runs in Constrained Language Mode here: .NET method calls, Add-Type and "
            "New-Object for non-core types are blocked; use cmdlets, or run the step in another shell",
        )
    return None


def execution_policy(ctx: HintContext) -> Hint | None:
    if any(_EXEC_POLICY.search(line) for line in ctx.output_tail):
        return Hint(
            "execution_policy",
            "note: PowerShell's execution policy refused to run a script file (.ps1); put the "
            "script's content directly in the command instead of invoking the file",
        )
    return None


def ps_errors(ctx: HintContext) -> Hint | None:
    if ctx.shell in POWERSHELLS and ctx.exit_code == 0 and ctx.ps_errors:
        return Hint(
            "powershell_errors_exit_0",
            f"note: PowerShell reported {ctx.ps_errors} error(s) although the exit code is 0 "
            "(non-terminating errors); check the output",
        )
    return None


def binary(ctx: HintContext) -> Hint | None:
    if not ctx.binary:
        return None
    parts = [
        f"{stream} ({count:,} bytes{', ' + kind if kind else ''})"
        for stream, count, kind in ctx.binary
    ]
    return Hint(
        "binary_output",
        f"note: binary output on {', '.join(parts)} was not rendered as text; redirect it to a file "
        "or pipe it through a converter (e.g. base64, xxd) if you need to inspect it",
    )


def replacement(ctx: HintContext) -> Hint | None:
    if ctx.replaced <= 0:
        return None
    encodings = ", ".join(f"{k}: {v}" for k, v in ctx.encodings.items() if v)
    return Hint(
        "replacement_chars",
        f"note: {ctx.replaced:,} undecodable byte(s) were shown as U+FFFD ({encodings})",
    )


def stdin_prompt(ctx: HintContext) -> Hint | None:
    if ctx.stdin_supplied or not ctx.last_partial:
        return None
    prompt = ctx.last_partial.strip()
    if not _PROMPT.search(prompt):
        return None
    return Hint(
        "stdin_prompt",
        f"note: the command prompted for input ({prompt[-60:]!r}) but stdin is closed; pass the "
        'answer with the stdin parameter (e.g. stdin="y\\n") or use a non-interactive flag',
    )


def background_job_killed(ctx: HintContext) -> Hint | None:
    if not ctx.leftover_count:
        return None
    if ctx.shell in BASH_FAMILY:
        detected = bool(_BG_LONE_AMP.search(_BG_REDIRECT_AMP.sub(" ", ctx.command))) or bool(
            _BG_POSIX_CMD.search(ctx.command)
        )
    elif ctx.shell == "cmd":
        detected = bool(_BG_CMD_START.search(ctx.command))
    elif ctx.shell in POWERSHELLS:
        detected = bool(_BG_PS_CMDLET.search(ctx.command))
    else:
        detected = False
    if not detected:
        return None
    plural = "es" if ctx.leftover_count != 1 else ""
    return Hint(
        "background_job_killed",
        f"note: {ctx.leftover_count} background process{plural} started by this command were "
        "still running when it finished and have been terminated; start it as its own run() "
        "call instead (optionally with name and wait_for) to keep it alive",
    )


def port_in_use(ctx: HintContext) -> Hint | None:
    port = extract_port_in_use(ctx.output_tail)
    if port is None:
        return None
    owner = f" by {ctx.port_owner}" if ctx.port_owner is not None else ""
    return Hint("port_in_use", f"note: port {port} is already in use{owner}")


CATALOG: tuple[Callable[[HintContext], Hint | None], ...] = (
    timed_out,
    background_job_killed,
    port_in_use,
    not_found,
    not_executable,
    signaled,
    windows_crash,
    app_control,
    constrained_language,
    execution_policy,
    ps_errors,
    binary,
    replacement,
    stdin_prompt,
)


def hints_for(ctx: HintContext) -> list[Hint]:
    return [hint for check in CATALOG if (hint := check(ctx)) is not None]
