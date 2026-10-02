import re
from pathlib import Path

import pytest

from envelope.cli import main
from envelope.config import ConfigError, Settings, load
from envelope.config.settings import DEFAULT_ENV, DEFAULT_SCRUB, KNOWN_KEYS, MIB

REPO_ROOT = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_defaults_match_specs() -> None:
    s = Settings()
    assert s.default_timeout_s is None
    assert s.max_timeout_s == 3600
    assert s.max_concurrency == 8
    assert s.wait_default_s == 30
    assert s.wait_max_s == 300
    assert s.max_running == 16
    assert s.evict_policy == "lru-unnamed"
    assert s.max_lifetime_s is None
    assert s.stop_grace_s == 5
    assert s.stop_grace_max_s == 60
    assert s.input_hint_after_s == 10
    assert s.processes_limit == 20
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
    path = write(tmp_path, "default_timeout_s = 60\nmax_running = 3\noutput_max_chars = 6000\n")
    file_only = load(environ={}, config_path=path).settings
    assert (file_only.default_timeout_s, file_only.max_running) == (60, 3)
    assert file_only.output_max_chars == 6000

    env = {"ENVELOPE_DEFAULT_TIMEOUT_S": "45", "ENVELOPE_MAX_RUNNING": "4"}
    with_env = load(environ=env, config_path=path).settings
    assert (with_env.default_timeout_s, with_env.max_running) == (45, 4)

    with_flags = load(flags={"default_timeout_s": "30"}, environ=env, config_path=path).settings
    assert with_flags.default_timeout_s == 30
    assert with_flags.max_running == 4
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


def test_lower_max_leaves_unset_default_timeout_unset(tmp_path: Path) -> None:
    s = load(environ={}, config_path=write(tmp_path, "max_timeout_s = 5\n")).settings
    assert s.default_timeout_s is None


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
        ("wait_default_s = 0", "wait_default_s must be a positive number"),
        ('wait_max_s = "lots"', "wait_max_s must be a positive number"),
        ("max_running = 0", "max_running must be a positive integer"),
        ('evict_policy = "kill-everything"', "must be 'lru-unnamed' or 'refuse'"),
        ("max_lifetime_s = -1", "must be a positive number or empty/unset for none"),
        ("stop_grace_s = 0", "stop_grace_s must be a positive number"),
        ("stop_grace_max_s = 0", "stop_grace_max_s must be a positive number"),
        ("input_hint_after_s = 0", "input_hint_after_s must be a positive number"),
        ("processes_limit = 0", "processes_limit must be a positive integer"),
        ("wait_default_s = 400", "must not exceed wait_max_s"),
        ("stop_grace_s = 120", "must not exceed stop_grace_max_s"),
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


def test_max_lifetime_s_accepts_unset(tmp_path: Path) -> None:
    path = write(tmp_path, "max_lifetime_s = 600\n")
    s = load(environ={}, config_path=path).settings
    assert s.max_lifetime_s == 600
    env_unset = load(environ={"ENVELOPE_MAX_LIFETIME_S": ""}, config_path=path).settings
    assert env_unset.max_lifetime_s is None


def test_max_concurrency_alias_only(tmp_path: Path) -> None:
    path = write(tmp_path, "max_concurrency = 12\n")
    result = load(environ={}, config_path=path)
    assert result.settings.max_running == 12
    assert result.settings.max_concurrency == 8  # unset; the field keeps its own default
    assert any("max_concurrency is deprecated" in w and "max_running" in w for w in result.warnings)


def test_max_concurrency_alias_and_new_key_different_sources(tmp_path: Path) -> None:
    path = write(tmp_path, "max_concurrency = 12\n")
    result = load(environ={"ENVELOPE_MAX_RUNNING": "6"}, config_path=path)
    assert result.settings.max_running == 6


def test_max_concurrency_alias_and_new_key_same_source_is_an_error(tmp_path: Path) -> None:
    path = write(tmp_path, "max_concurrency = 12\nmax_running = 6\n")
    with pytest.raises(ConfigError, match="both max_concurrency and max_running are set"):
        load(environ={}, config_path=path)


def test_wait_max_s_high_warns(tmp_path: Path) -> None:
    path = write(tmp_path, "wait_max_s = 600\n")
    result = load(environ={}, config_path=path)
    assert any("wait_max_s" in w and "10-minute" in w for w in result.warnings)


def test_wait_max_s_below_threshold_does_not_warn(tmp_path: Path) -> None:
    path = write(tmp_path, "wait_max_s = 300\n")
    result = load(environ={}, config_path=path)
    assert not any("wait_max_s" in w for w in result.warnings)


def test_background_process_keys_layer_precedence(tmp_path: Path) -> None:
    path = write(tmp_path, "wait_default_s = 20\nmax_running = 4\nstop_grace_s = 3\n")
    file_only = load(environ={}, config_path=path).settings
    assert (file_only.wait_default_s, file_only.max_running, file_only.stop_grace_s) == (20, 4, 3)

    env = {"ENVELOPE_WAIT_DEFAULT_S": "15", "ENVELOPE_MAX_RUNNING": "6"}
    with_env = load(environ=env, config_path=path).settings
    assert (with_env.wait_default_s, with_env.max_running) == (15, 6)
    assert with_env.stop_grace_s == 3

    with_flags = load(flags={"wait_default_s": "10"}, environ=env, config_path=path).settings
    assert with_flags.wait_default_s == 10
    assert with_flags.max_running == 6


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


def test_readme_settings_table_lists_every_key() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"\| key \| default \| meaning \|\n\|---\|---\|---\|\n(.*?)\n\n", text, re.S)
    assert match, "settings table not found in README.md"
    keys: set[str] = set()
    for line in match.group(1).splitlines():
        cell = line.split("|")[1].strip()
        for part in cell.split("/"):
            keys.add(part.strip().strip("`"))
    assert keys == set(KNOWN_KEYS)
