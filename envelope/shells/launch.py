"""Per-shell launch strategies (design D6).

* bash family: the command is written to a spool-dir file that the shell reads
  as data (no exec bit, no command-line limit, stdin stays free).
* PowerShell: the command plus a fixed prologue/epilogue is written to a
  ``.txt`` data file; a short ``-Command`` bootstrap reads it with
  ``Get-Content -Raw`` and dot-sources it as a script block. No ``.ps1`` file,
  no ``-EncodedCommand``, no ``-ExecutionPolicy Bypass``, no ``Invoke-Expression``.
  (Mechanism pending confirmation by spike S1.)
* cmd: ``cmd.exe /d /s /c "chcp 65001>nul & <command>"``; longer than
  cmd's 8,191-character limit is refused.
* custom shells: argument template with ``{script}`` (command text) and/or
  ``{file}`` (path of a script file with the configured extension).
"""

from __future__ import annotations

import ntpath
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from envelope.shells.pathconv import to_forward
from envelope.shells.registry import Shell

CMD_LIMIT = 8191
CMD_ENV = "__ENVELOPE_CMD"
PS_ENV = "__ENVELOPE_PS"
WINDOWS_COMMAND_LINE_LIMIT = 32_000  # CreateProcess allows 32,767 including the NUL
PS_DATA_FILE = "script.ps.txt"
PS_STATUS_FILE = "ps-status.txt"


class LaunchError(Exception):
    """The command cannot be delivered to this shell (a tool-level error)."""


@dataclass
class Launch:
    args: list[str] | str
    files: dict[Path, bytes] = field(default_factory=lambda: dict[Path, bytes]())
    status_file: Path | None = None
    env: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    """Extra environment variables the launch mechanism needs."""
    console_codepage: bool = False
    """Decode non-UTF-8 output with the console code page (cmd, Windows PowerShell)."""


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def powershell_script(command: str, status_file: Path) -> str:
    """Prologue + command + epilogue, executed as one script block.

    The epilogue must directly follow the command so ``$?`` reflects the
    command's last statement.
    """
    status = _ps_quote(str(status_file))
    prologue = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }\n"
        "try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }\n"
        "if ($PSVersionTable.PSVersion.Major -ge 7) { try { $PSStyle.OutputRendering = 'PlainText' } catch { } }\n"
        "$global:LASTEXITCODE = $null\n"
        "$__envelope_errors0 = $Error.Count\n"
    )
    epilogue = (
        "\n$__envelope_ok = $?\n"
        "$__envelope_errs = $Error.Count - $__envelope_errors0\n"
        f'try {{ Set-Content -LiteralPath {status} -Value "$__envelope_errs" -Encoding ASCII }} catch {{ }}\n'
        "if ($null -ne $global:LASTEXITCODE) { exit $global:LASTEXITCODE }\n"
        "if (-not $__envelope_ok) { exit 1 }\n"
        "exit 0\n"
    )
    return prologue + command + epilogue


def powershell_bootstrap(data_file: Path) -> str:
    path = _ps_quote(str(data_file))
    return (
        f"$__envelope_src = Get-Content -LiteralPath {path} -Raw -Encoding UTF8; "
        ". ([scriptblock]::Create($__envelope_src))"
    )


def powershell_utf8_command_line(
    cmd_path: str, ps_path: str, script: str
) -> tuple[str, dict[str, str]] | None:
    """Preferred PowerShell launch (spike S1, Windows 10 19044), or None if too long.

    The script is passed as the ``-Command`` argument and PowerShell is started from
    an outer cmd after ``chcp 65001``, so it inherits a UTF-8 console for output and
    for stdin (``$input``). This is the only way under Constrained Language Mode,
    which forbids ``[scriptblock]::Create`` and setting ``[Console]`` encodings. The
    argument travels through an environment variable (delayed expansion, see
    ``cmd_command_line``), pre-quoted for Windows argument parsing; cmd's 8,191
    character line limit applies after expansion.
    """
    argument = subprocess.list2cmdline([script])
    outer = f'chcp 65001>nul & "{ps_path}" -NoLogo -NoProfile -NonInteractive -Command !{PS_ENV}!'
    if len(outer) - len(PS_ENV) - 2 + len(argument) > CMD_LIMIT:
        return None
    return f'"{cmd_path}" /d /v:on /s /c "{outer}"', {PS_ENV: argument}


def cmd_command_line(cmd_path: str, command: str) -> tuple[str, dict[str, str]]:
    """Command line plus extra environment for running ``command`` in cmd with UTF-8 output.

    A cmd started with CREATE_NO_WINDOW keeps writing its own command line in the
    OEM code page even after ``chcp 65001`` (verified on Windows 10 19044), so an
    outer cmd switches the code page and starts an inner cmd that inherits it. The
    command reaches the inner cmd through an environment variable expanded with
    delayed expansion, after the outer cmd has parsed its own line, so ``&``,
    quotes, ``%`` and ``!`` are parsed only once, by the inner cmd.
    """
    joined = " & ".join(
        line.strip() for line in command.replace("\r\n", "\n").split("\n") if line.strip()
    )
    if len(joined) > CMD_LIMIT:
        raise LaunchError(
            f"the command is {len(joined):,} characters long, but cmd accepts at most "
            f'{CMD_LIMIT:,} characters on its command line. Use shell "pwsh", "powershell" '
            'or "git-bash" for long scripts (they receive the script as a file)'
        )
    outer = f'chcp 65001>nul & "{cmd_path}" /d /s /c "!{CMD_ENV}!"'
    return f'"{cmd_path}" /d /v:on /s /c "{outer}"', {CMD_ENV: joined}


def comspec() -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.environ.get("COMSPEC") or ntpath.join(root, "System32", "cmd.exe")


def build_launch(shell: Shell, command: str, run_dir: Path) -> Launch:
    assert shell.path is not None
    if shell.kind == "custom":
        assert shell.custom is not None
        extension = shell.custom.extension or ".txt"
        script = run_dir / f"script{extension}"
        files: dict[Path, bytes] = {}
        args = [shell.path]
        for template in shell.custom.args:
            if "{file}" in template:
                files[script] = command.encode("utf-8")
            args.append(template.replace("{script}", command).replace("{file}", str(script)))
        return Launch(args, files)

    if shell.family == "posix" or shell.id == "fish":
        script = run_dir / "script.sh"
        body = command if command.endswith("\n") else command + "\n"
        files = {script: body.encode("utf-8")}
        if shell.id == "git-bash":
            return Launch([shell.path, to_forward(str(script))], files)
        if shell.id == "zsh":
            return Launch([shell.path, "-f", str(script)], files)
        if shell.id == "fish":
            return Launch([shell.path, "--no-config", str(script)], files)
        return Launch([shell.path, str(script)], files)

    if shell.family == "powershell":
        data = run_dir / PS_DATA_FILE
        status = run_dir / PS_STATUS_FILE
        text = powershell_script(command, status)
        nested = powershell_utf8_command_line(comspec(), shell.path, text)
        if nested is not None:
            return Launch(nested[0], env=nested[1], status_file=status)
        if shell.language_mode == "ConstrainedLanguage":
            raise LaunchError(
                "PowerShell runs in Constrained Language Mode on this machine, where envelope "
                "must pass the script on a cmd command line (8,191 characters in total, about "
                f"7,500 of them for the command). This command is {len(command):,} characters. "
                'Split it into smaller calls, or use shell "git-bash" for long scripts'
            )
        direct = [shell.path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", text]
        if len(subprocess.list2cmdline(direct)) <= WINDOWS_COMMAND_LINE_LIMIT:
            # Still sees caller stdin as $input (a dot-sourced script block would not),
            # but decodes it with the console code page.
            return Launch(direct, status_file=status, console_codepage=shell.id == "powershell")
        args = [
            shell.path,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            powershell_bootstrap(data),
        ]
        # UTF-8 with BOM so Windows PowerShell 5.1 never guesses the ANSI code page.
        return Launch(
            args,
            {data: b"\xef\xbb\xbf" + text.encode("utf-8")},
            status_file=status,
            console_codepage=shell.id == "powershell",
        )

    if shell.id == "cmd":
        line, env = cmd_command_line(shell.path, command)
        return Launch(line, env=env, console_codepage=True)

    raise LaunchError(f"no launch strategy for shell '{shell.id}'")
