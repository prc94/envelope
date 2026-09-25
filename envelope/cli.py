"""Command-line entry point: ``envelope [options] [serve | doctor [--json]]``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from envelope import __version__
from envelope.config import ConfigError, LoadResult, load
from envelope.config.settings import KNOWN_KEYS
from envelope.log import logger, set_level


def _flag_names(key: str) -> list[str]:
    names = ["--" + key.replace("_", "-")]
    for suffix in ("_s", "_h", "_mib"):
        if key.endswith(suffix):
            names.append("--" + key.removesuffix(suffix).replace("_", "-"))
    return names


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="envelope",
        description="MCP server that runs commands in bash, git-bash, PowerShell, cmd and more.",
    )
    parser.add_argument("--version", action="version", version=f"envelope {__version__}")
    parser.add_argument("--config", metavar="PATH", help="configuration file (TOML)")
    settings = parser.add_argument_group("settings (override config file and ENVELOPE_* vars)")
    for key in KNOWN_KEYS:
        settings.add_argument(*_flag_names(key), dest="set_" + key, metavar="VALUE")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="serve MCP over stdio (default)")
    doctor = commands.add_parser("doctor", help="report shells, restrictions and paths")
    doctor.add_argument("--json", action="store_true", help="print one JSON document")
    return parser


def load_config(args: argparse.Namespace) -> LoadResult:
    flags = {key: value for key in KNOWN_KEYS if (value := getattr(args, "set_" + key)) is not None}
    result = load(flags=flags, environ=os.environ, config_path=args.config)
    set_level(result.settings.log_level)
    if result.config_file is None:
        logger.debug(
            "no config file found; searched %s", ", ".join(str(p) for p in result.searched)
        )
    else:
        logger.debug("loaded config file %s", result.config_file)
    for warning in result.warnings:
        logger.warning("%s", warning)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args)
    except ConfigError as exc:
        sys.stderr.write(f"envelope: configuration error: {exc}\n")
        return 2

    if args.command == "doctor":
        from envelope.doctor import run_doctor

        return run_doctor(config, as_json=args.json)

    from envelope.app import serve

    return serve(config)
