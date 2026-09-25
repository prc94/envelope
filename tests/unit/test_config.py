from pathlib import Path

import pytest

from envelope.cli import main
from envelope.config import ConfigError, Settings, load
from envelope.config.settings import DEFAULT_ENV, DEFAULT_SCRUB, MIB


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_defaults_match_specs() -> None:
    s = Settings()
    assert s.default_timeout_s == 120
    assert s.max_timeout_s == 3600
    assert s.max_concurrency == 8
    assert s.progress_interval_s == 5
    assert s.output_max_chars == 12_000
    assert s.output_max_lines == 400
    assert s.spool_cap_bytes == 64 * MIB
    assert s.spool_total_bytes == 1024 * MIB
    assert s.spool_retention_h == 24
    assert s.max_command_chars == 1_000_000
    assert s.allowed_shells is None
    assert s.allowed_cwd_roots is None
    assert s.audit_only is False
    assert s.audit_enabled is True
    assert s.log_level == "warn"
    assert dict(s.env_defaults) == {
        "NO_COLOR": "1",
        "TERM": "dumb",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "never",
        "DEBIAN_FRONTEND": "noninteractive",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    assert dict(DEFAULT_ENV) == dict(s.env_defaults)
    assert s.env_scrub == DEFAULT_SCRUB == ("*_API_KEY", "*_TOKEN", "*_SECRET", "OPENROUTER_*")


def test_no_config_file_uses_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    result = load(environ={})
    assert result.config_file is None
    assert result.settings == Settings()
    assert result.searched == [tmp_path / "envelope" / "config.toml"]


def test_each_layer_overrides_previous(tmp_path: Path) -> None:
    path = write(tmp_path, "default_timeout_s = 60\nmax_concurrency = 3\noutput_max_chars = 6000\n")
    file_only = load(environ={}, config_path=path).settings
    assert (file_only.default_timeout_s, file_only.max_concurrency) == (60, 3)
    assert file_only.output_max_chars == 6000

    env = {"ENVELOPE_DEFAULT_TIMEOUT_S": "45", "ENVELOPE_MAX_CONCURRENCY": "4"}
    with_env = load(environ=env, config_path=path).settings
    assert (with_env.default_timeout_s, with_env.max_concurrency) == (45, 4)

    with_flags = load(flags={"default_timeout_s": "30"}, environ=env, config_path=path).settings
    assert with_flags.default_timeout_s == 30
    assert with_flags.max_concurrency == 4
    assert with_flags.output_max_chars == 6000


def test_envelope_config_env_var_selects_file(tmp_path: Path) -> None:
    path = write(tmp_path, "log_level = 'debug'\n")
    result = load(environ={"ENVELOPE_CONFIG": path})
    assert result.config_file == Path(path)
    assert result.settings.log_level == "debug"


def test_flag_overrides_file_via_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from envelope import cli

    path = write(tmp_path, "default_timeout_s = 60\n")
    args = cli.build_parser().parse_args(["--config", path, "--default-timeout", "30"])
    assert cli.load_config(args).settings.default_timeout_s == 30


def test_invalid_value_is_fatal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, 'max_timeout_s = "forever"\n')
    with pytest.raises(ConfigError, match="max_timeout_s must be a positive number"):
        load(environ={}, config_path=path)
    assert main(["--config", path, "doctor"]) == 2
    err = capsys.readouterr().err
    assert "max_timeout_s" in err and '"forever"' in err and "positive number" in err


def test_lower_max_clamps_builtin_default(tmp_path: Path) -> None:
    s = load(environ={}, config_path=write(tmp_path, "max_timeout_s = 5\n")).settings
    assert s.default_timeout_s == 5


def test_unknown_key_warns(tmp_path: Path) -> None:
    path = write(tmp_path, 'defualt_shell = "pwsh"\n')
    result = load(environ={}, config_path=path)
    assert any("defualt_shell" in w for w in result.warnings)
    assert result.settings.default_shell is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('default_shell = "tcsh"', "default_shell must be one of"),
        ('allowed_shells = ["bash", "nope"]', "allowed_shells must contain only"),
        ('env_scrub = ["BAD-PATTERN"]', "env_scrub must be a list of environment"),
        ("max_concurrency = 0", "max_concurrency must be a positive integer"),
        ("default_timeout_s = 7200", "must not exceed max_timeout_s"),
        ('log_level = "loud"', "log_level must be one of"),
        ("[shells.nu]\npath = 'nu'\nargs = ['-c']", "must be a table of shells"),
        ("[shells.bash]\npath = 'x'\nargs = ['{script}']", "cannot redefine built-in shell"),
    ],
)
def test_invalid_values(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load(environ={}, config_path=write(tmp_path, text + "\n"))


def test_custom_shell_and_policy_table(tmp_path: Path) -> None:
    text = (
        'default_shell = "Nu"\n'
        "[shells.nu]\npath = '/usr/bin/nu'\nargs = ['-c', '{script}']\n"
        "[policy]\nallowed_cwd_roots = ['/work']\naudit_only = true\n"
    )
    s = load(environ={}, config_path=write(tmp_path, text)).settings
    assert s.default_shell == "nu"
    assert s.shells["nu"].args == ("-c", "{script}")
    assert s.allowed_cwd_roots == ("/work",)
    assert s.audit_only is True


def test_env_value_forms() -> None:
    env = {
        "ENVELOPE_ENV_DEFAULTS": "TERM=xterm\nFOO=bar",
        "ENVELOPE_ALLOWED_SHELLS": "bash, GitBash",
        "ENVELOPE_AUDIT_ONLY": "yes",
        "ENVELOPE_BOGUS": "1",
    }
    result = load(environ=env, config_path=None)
    s = result.settings
    assert dict(s.env_defaults) == {"TERM": "xterm", "FOO": "bar"}
    assert s.allowed_shells == ("bash", "git-bash")
    assert s.audit_only is True
    assert any("ENVELOPE_BOGUS" in w for w in result.warnings)
