from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from envelope.audit import AuditLog
from envelope.config.settings import CustomShell, Settings
from envelope.execution.backend import SpawnError
from envelope.execution.environment import build_env
from envelope.instructions import MAX_CHARS, build_instructions
from envelope.policy.decide import make_decide
from envelope.policy.gateway import Gateway, Permit, PolicyRefusal, audit_only_note, refusal_text
from envelope.policy.model import Decision, ExecRequest, InputRequest, Request
from envelope.render.hints import HintContext, hints_for
from envelope.shells.launch import LaunchError, build_launch, cmd_command_line, powershell_script
from envelope.shells.registry import HostInfo, Shell, detect
from envelope.tools.coercion import ArgumentError, Param, coerce

# --- environment ------------------------------------------------------------------


def test_env_scrub_defaults_and_explicit() -> None:
    inherited = {
        "PATH": "/bin",
        "OPENROUTER_API_KEY": "x",
        "GITHUB_TOKEN": "t",
        "MY_SECRET": "s",
        "TERM": "xterm-256color",
    }
    built = build_env(
        inherited,
        scrub=Settings().env_scrub,
        defaults=Settings().env_defaults,
        explicit={"GITHUB_TOKEN": "explicit"},
        windows=False,
    )
    assert "OPENROUTER_API_KEY" not in built.env and "MY_SECRET" not in built.env
    assert built.env["GITHUB_TOKEN"] == "explicit"
    assert built.env["TERM"] == "dumb" and built.env["PAGER"] == "cat"
    assert built.removed == ["GITHUB_TOKEN", "MY_SECRET", "OPENROUTER_API_KEY"]
    override = build_env(
        inherited, scrub=(), defaults={"TERM": "dumb"}, explicit={"TERM": "xterm"}, windows=False
    )
    assert override.env["TERM"] == "xterm"


def test_env_windows_case_insensitive() -> None:
    built = build_env(
        {"Path": "C:\\x", "openrouter_api_key": "k"},
        scrub=("OPENROUTER_*",),
        defaults={"PATH_EXTRA": "1"},
        explicit={"PATH": "C:\\y"},
        windows=True,
    )
    assert built.env == {"PATH": "C:\\y", "PATH_EXTRA": "1"}


# --- launch builders -------------------------------------------------------------------


def shell(shell_id: str, path: str, **kw: object) -> Shell:
    return Shell(shell_id, "builtin", path, True, **kw)  # type: ignore[arg-type]


def test_bash_script_file(tmp_path: Path) -> None:
    launch = build_launch(shell("bash", "/bin/bash"), "echo hi", tmp_path)
    script = tmp_path / "script.sh"
    assert launch.args == ["/bin/bash", str(script)]
    assert launch.files[script] == b"echo hi\n"


def test_git_bash_uses_forward_slash_script_path(tmp_path: Path) -> None:
    launch = build_launch(
        shell("git-bash", r"C:\Program Files\Git\bin\bash.exe"), "ls", Path("C:\\spool\\out_1")
    )
    assert isinstance(launch.args, list) and launch.args[1].endswith("script.sh")
    assert "\\" not in launch.args[1]


def test_powershell_launch_never_uses_blocked_mechanisms(tmp_path: Path) -> None:
    small = build_launch(shell("pwsh", r"C:\ps\pwsh.exe"), "Get-Item C:\\nope", tmp_path)
    assert isinstance(small.args, str) and "chcp 65001" in small.args and small.files == {}
    assert "Get-Item C:\\nope" in small.env["__ENVELOPE_PS"]
    medium = build_launch(shell("pwsh", r"C:\ps\pwsh.exe"), "Get-Item x" + " " * 12_000, tmp_path)
    assert isinstance(medium.args, list) and medium.args[4] == "-Command" and medium.files == {}
    launch = build_launch(
        shell("pwsh", r"C:\ps\pwsh.exe"), "Get-Item C:\\nope" + " " * 40_000, tmp_path
    )
    assert isinstance(launch.args, list)
    joined = " ".join(launch.args)
    for banned in ("-EncodedCommand", "-ExecutionPolicy", "Invoke-Expression", "iex ", ".ps1"):
        assert banned not in joined
    assert launch.args[1:5] == ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    data = next(iter(launch.files))
    assert data.suffix == ".txt"
    text = launch.files[data].decode("utf-8-sig")
    assert "$ProgressPreference = 'SilentlyContinue'" in text
    assert "OutputRendering = 'PlainText'" in text
    assert text.index("Get-Item C:\\nope") < text.index("$__envelope_ok = $?")
    assert launch.status_file is not None and not launch.console_codepage
    assert not build_launch(shell("powershell", "p.exe"), "x", tmp_path).console_codepage
    assert build_launch(shell("powershell", "p.exe"), "x" * 12_000, tmp_path).console_codepage


def test_powershell_clm_uses_command_argument(tmp_path: Path) -> None:
    pwsh = shell("pwsh", r"C:\ps\pwsh.exe", language_mode="ConstrainedLanguage")
    launch = build_launch(pwsh, 'Write-Output "a & b"', tmp_path)
    assert isinstance(launch.args, str) and "/v:on" in launch.args and "chcp 65001" in launch.args
    assert "-Command !__ENVELOPE_PS!" in launch.args and "[scriptblock]" not in launch.args
    argument = launch.env["__ENVELOPE_PS"]
    assert argument.startswith('"') and 'Write-Output \\"a & b\\"' in argument
    assert launch.files == {} and launch.status_file is not None
    with pytest.raises(LaunchError, match="Constrained Language Mode"):
        build_launch(pwsh, "x" * 9000, tmp_path)


def test_powershell_exit_code_mapping_text() -> None:
    script = powershell_script("cmd /c exit 7", Path("status.txt"))
    assert "if ($null -ne $global:LASTEXITCODE) { exit $global:LASTEXITCODE }" in script
    assert "if (-not $__envelope_ok) { exit 1 }" in script


def test_cmd_command_line_and_limit() -> None:
    cmd = r"C:\Windows\System32\cmd.exe"
    line, env = cmd_command_line(cmd, 'echo a & b\necho "c"')
    assert line == f'"{cmd}" /d /v:on /s /c "chcp 65001>nul & "{cmd}" /d /s /c "!__ENVELOPE_CMD!""'
    assert env == {"__ENVELOPE_CMD": 'echo a & b & echo "c"'}
    with pytest.raises(LaunchError, match="8,191"):
        cmd_command_line("cmd.exe", "echo " + "x" * 10_000)


def test_custom_shell_templates(tmp_path: Path) -> None:
    custom = Shell(
        "nu",
        "custom",
        "/usr/bin/nu",
        True,
        custom=CustomShell("nu", "/usr/bin/nu", ("-c", "{script}")),
    )
    assert build_launch(custom, "ls | length", tmp_path).args == [
        "/usr/bin/nu",
        "-c",
        "ls | length",
    ]
    filed = Shell(
        "xs", "custom", "/x", True, custom=CustomShell("xs", "/x", ("--run", "{file}"), ".xsh")
    )
    launch = build_launch(filed, "print 1", tmp_path)
    assert launch.args == ["/x", "--run", str(tmp_path / "script.xsh")]
    assert launch.files[tmp_path / "script.xsh"] == b"print 1"


# --- policy ----------------------------------------------------------------------------------


def request(**kw: object) -> ExecRequest:
    base: dict[str, object] = {
        "shell": "bash",
        "shell_path": "/bin/bash",
        "command": "ls",
        "cwd": "/work/app",
    }
    base.update(kw)
    return ExecRequest(**base)  # type: ignore[arg-type]


def test_default_policy_allows() -> None:
    decide = make_decide(windows=False)
    assert decide(request(command="rm -rf ./build"), Settings()).verdict == "allow"
    assert decide(InputRequest("out_1", "bash", "y\n"), Settings()).verdict == "allow"


def test_cwd_roots_windows() -> None:
    decide = make_decide(windows=True)
    settings = Settings(allowed_cwd_roots=("C:\\work",))
    denied = decide(request(cwd="C:\\Windows"), settings)
    assert denied.verdict == "deny" and denied.rule == "allowed_cwd_roots"
    assert "C:\\work" in denied.reasons[0]
    assert decide(request(cwd="c:\\Work\\sub"), settings).verdict == "allow"
    assert decide(request(cwd="C:\\workshop"), settings).verdict == "deny"


def test_max_length() -> None:
    decided = make_decide(windows=False)(request(command="x" * 11), Settings(max_command_chars=10))
    assert decided.verdict == "deny" and decided.rule == "max_command_chars"


def test_gateway_records_decision_before_permit(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")
    gateway = Gateway(Settings(), audit, make_decide(windows=False))
    permit = gateway.authorize(request())
    assert isinstance(permit, Permit) and permit.decision.verdict == "allow"
    assert '"event":"decision"' in (tmp_path / "a.jsonl").read_text()
    with pytest.raises(RuntimeError, match="only be issued by the execution gateway"):
        Permit(request(), permit.decision)


def test_run_cannot_execute_without_permit() -> None:
    import asyncio

    from envelope.execution.runner import RunRequest, start
    from envelope.shells.launch import Launch

    async def attempt() -> None:
        await start(
            object(),  # type: ignore[arg-type]
            RunRequest("bash", Launch(["/bin/true"]), "/", {}, None, 0.0, 1.0),
            object(),  # type: ignore[arg-type]
            permit=object(),  # type: ignore[arg-type]
        )

    with pytest.raises(RuntimeError, match="requires a permit"):
        asyncio.run(attempt())


def test_ask_is_refused_with_guidance(tmp_path: Path) -> None:
    def ask(req: Request, settings: Settings) -> Decision:
        return Decision(
            "ask",
            "network_commands",
            ("curl reaches the network",),
            remedy="set allow_network = true",
        )

    gateway = Gateway(Settings(), AuditLog(tmp_path / "a.jsonl"), ask)
    with pytest.raises(PolicyRefusal) as excinfo:
        gateway.authorize(request(command="curl example.com"))
    text = refusal_text(excinfo.value.decision)
    assert "approval required by policy (rule network_commands)" in text
    assert "cannot ask the user for approval" in text
    assert "set allow_network = true" in text
    assert "not executed" in text


def test_audit_only_mode(tmp_path: Path) -> None:
    settings = Settings(allowed_shells=("sh",), audit_only=True)
    gateway = Gateway(settings, AuditLog(tmp_path / "a.jsonl"), make_decide(windows=False))
    permit = gateway.authorize(request())
    assert permit.decision.verdict == "deny" and not permit.decision.enforced
    note = audit_only_note(permit.decision)
    assert note is not None and "would have been denied" in note


# --- hints ----------------------------------------------------------------------------------


def ctx(
    shell_id: str = "bash", exit_code: int | None = 1, tail: Sequence[str] = (), **kw: object
) -> HintContext:
    return HintContext(shell=shell_id, exit_code=exit_code, output_tail=list(tail), **kw)  # type: ignore[arg-type]


def ids(context: HintContext) -> list[str]:
    return [h.id for h in hints_for(context)]


def test_no_hint_without_certainty() -> None:
    assert hints_for(ctx(exit_code=1, tail=["something failed", "error: generic"])) == []
    assert hints_for(ctx(exit_code=0, tail=["all good"])) == []


def test_not_found_cross_shell() -> None:
    def locate(name: str) -> list[tuple[str, str]]:
        return [("git-bash", r"C:\Program Files\Git\usr\bin\jq.exe")] if name == "jq" else []

    cmd = ctx(
        "cmd", 9009, ["'jq' is not recognized as an internal or external command,"], locate=locate
    )
    text = hints_for(cmd)[0].text
    assert "`jq` was not found in cmd; it is available in git-bash" in text
    pwsh = ctx(
        "pwsh",
        1,
        ["jq: The term 'jq' is not recognized as a name of a cmdlet, function"],
        locate=locate,
    )
    assert "available in git-bash" in hints_for(pwsh)[0].text
    bash = ctx("bash", 127, ["/tmp/s.sh: line 1: jq: command not found"], locate=lambda n: [])
    assert "`jq` was not found in bash, nor on the PATH" in hints_for(bash)[0].text
    assert ids(ctx("sh", 127, ["/tmp/s.sh: 1: foo: not found"])) == ["command_not_found"]
    assert ids(ctx("bash", 1, ["jq: command not found"])) == []  # exit code disagrees


def test_signal_and_crash_hints() -> None:
    assert "out-of-memory" in hints_for(ctx(exit_code=None, signal=9))[0].text
    assert "SIGSEGV" in hints_for(ctx(exit_code=139))[0].text
    assert "access violation" in hints_for(ctx("cmd", 0xC0000005))[0].text
    assert "stack buffer overrun" in hints_for(ctx("pwsh", -1073740791))[0].text
    assert ids(ctx(exit_code=126)) == ["not_executable"]


def test_timeout_hint() -> None:
    hint = hints_for(ctx(exit_code=None, timed_out=True, timeout_s=2, tree_terminated=True))[0]
    assert hint.id == "timed_out" and "all its child processes were terminated" in hint.text


def test_windows_restriction_hints() -> None:
    assert ids(
        ctx(
            "powershell",
            1,
            ["Program 'x.exe' failed to run: This program is blocked by group policy."],
        )
    ) == ["application_control"]
    clm = [
        "Cannot invoke method. Method invocation is supported only on core types in this language mode."
    ]
    assert ids(ctx("pwsh", 1, clm)) == ["constrained_language"]
    policy = ["File C:\\a.ps1 cannot be loaded because running scripts is disabled on this system."]
    assert ids(ctx("pwsh", 1, policy)) == ["execution_policy"]
    assert ids(ctx("pwsh", 0, ["Get-Item : Cannot find path"], ps_errors=1)) == [
        "powershell_errors_exit_0"
    ]


def test_output_hints() -> None:
    assert ids(ctx(exit_code=0, binary=[("stdout", 100, "PNG")])) == ["binary_output"]
    assert ids(ctx(exit_code=0, replaced=3, encodings={"stdout": "utf-8"})) == ["replacement_chars"]
    assert ids(ctx(exit_code=1, last_partial="Continue? [y/N] ")) == ["stdin_prompt"]
    assert ids(ctx(exit_code=1, last_partial="Continue? [y/N] ", stdin_supplied=True)) == []


def test_background_job_killed_bash() -> None:
    assert ids(ctx(exit_code=0, command="sleep 30 &", leftover_count=1)) == [
        "background_job_killed"
    ]
    assert ids(ctx(exit_code=0, command="nohup sleep 30", leftover_count=1)) == [
        "background_job_killed"
    ]


def test_background_job_killed_cmd() -> None:
    assert ids(ctx(shell_id="cmd", exit_code=0, command="start notepad", leftover_count=1)) == [
        "background_job_killed"
    ]


def test_background_job_killed_powershell() -> None:
    assert ids(
        ctx(shell_id="pwsh", exit_code=0, command="Start-Process notepad", leftover_count=1)
    ) == ["background_job_killed"]


def test_background_job_killed_no_leftovers_no_hint() -> None:
    assert ids(ctx(exit_code=0, command="sleep 30 &", leftover_count=None)) == []
    assert ids(ctx(exit_code=0, command="sleep 30 &", leftover_count=0)) == []


def test_background_job_killed_redirect_not_mistaken_for_ampersand() -> None:
    # `gradle ... 2>&1` has no lone backgrounding `&`, even though leftovers exist
    # for some other reason (e.g. a daemon the build itself spawned).
    assert ids(ctx(exit_code=0, command="./gradlew build 2>&1", leftover_count=1)) == []
    assert ids(ctx(exit_code=0, command="cmd1 && cmd2", leftover_count=1)) == []


def test_port_in_use_with_owner() -> None:
    hint = ids(
        ctx(
            exit_code=1,
            tail=["Error: listen EADDRINUSE: address already in use :::3000"],
            port_owner='out_1a2b3c4d ("web")',
        )
    )
    assert hint == ["port_in_use"]


def test_port_in_use_without_owner() -> None:
    hints = hints_for(
        ctx(exit_code=1, tail=["bind: address already in use :8080"], port_owner=None)
    )
    assert len(hints) == 1 and hints[0].id == "port_in_use"
    assert "8080" in hints[0].text and " by " not in hints[0].text


def test_no_port_in_use_hint_without_a_match() -> None:
    assert ids(ctx(exit_code=1, tail=["some other error"])) == []


def test_spawn_error_policy_codes() -> None:
    for code in (1260, 4551):
        text = SpawnError("pwsh", r"C:\ps\pwsh.exe", code, "blocked").describe()
        assert "blocked by group policy / application control" in text
    assert "group policy" not in SpawnError("bash", "/x", 13, "Permission denied").describe()


# --- instructions --------------------------------------------------------------------------------


def test_instructions_windows_clm() -> None:
    files = {
        r"c:\windows\system32\cmd.exe",
        r"c:\windows\system32\windowspowershell\v1.0\powershell.exe",
        r"c:\program files\powershell\7\pwsh.exe",
        r"c:\program files\git\bin\bash.exe",
    }
    probes = {
        "bash.exe": "GNU bash, version 5.2.37(1)-release",
        "pwsh.exe": "7.4.6|ConstrainedLanguage|RemoteSigned",
        "powershell.exe": "5.1.22621.4391|ConstrainedLanguage|AllSigned",
        "cmd.exe": "Microsoft Windows [Version 10.0.22631.4460]",
    }

    def probe(argv: Sequence[str], timeout: float) -> tuple[int, str]:
        return 0, probes[argv[0].lower().rsplit("\\", 1)[-1]]

    env = {"SystemRoot": r"C:\Windows", "ProgramFiles": r"C:\Program Files", "PATH": ""}
    host = HostInfo("win32", env, is_file=lambda p: p.lower() in files, registry=list)
    registry = detect(host, probe=probe)
    text = build_instructions(registry, machine="AMD64")
    assert len(text) <= MAX_CHARS
    assert "git-bash 5.2.37 (default)" in text and "pwsh 7.4.6" in text
    assert "Constrained Language Mode" in text
    assert "Windows" in text and "/c/Users/x" in text
    assert "output(handle=" in text


def test_coercion_rules() -> None:
    params = (
        Param("n", "number", "must be a number"),
        Param("b", "boolean", "must be true/false"),
        Param("e", "env", "must be env"),
    )
    values, notes = coerce({"n": "2.5", "b": "false", "e": ["A=1", "B=2"], "zzz": 1}, params)
    assert values == {"n": 2.5, "b": False, "e": {"A": "1", "B": "2"}}
    assert {n["param"] for n in notes} == {"n", "b", "e", "zzz"}
    with pytest.raises(ArgumentError, match="`n` must be a number \\(received string"):
        coerce({"n": "soon"}, params)
    with pytest.raises(ArgumentError):
        coerce({"e": "not env"}, params)
