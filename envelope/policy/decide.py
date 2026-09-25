"""P1 policy: allow everything, subject to basic configurable limits (deny)."""

from __future__ import annotations

from collections.abc import Callable

from envelope.config.settings import Settings
from envelope.policy.model import Decision, ExecRequest, Request
from envelope.shells.pathconv import to_windows

DecideFn = Callable[[Request, Settings], Decision]


def _normalize(path: str, windows: bool) -> str:
    if windows:
        return to_windows(path).rstrip("\\").lower()
    return path.rstrip("/") or "/"


def _within(path: str, root: str, windows: bool) -> bool:
    p, r = _normalize(path, windows), _normalize(root, windows)
    sep = "\\" if windows else "/"
    return p == r or p.startswith(r if r.endswith(sep) else r + sep)


def make_decide(*, windows: bool) -> DecideFn:
    def decide(request: Request, settings: Settings) -> Decision:
        if not isinstance(request, ExecRequest):
            return Decision(
                "allow", "default", ("input to a running process is allowed by default",)
            )
        if settings.allowed_shells is not None and request.shell not in settings.allowed_shells:
            return Decision(
                "deny",
                "allowed_shells",
                (
                    f"shell '{request.shell}' is not in allowed_shells "
                    f"({', '.join(settings.allowed_shells) or 'none'})",
                ),
                remedy=f'add "{request.shell}" to allowed_shells in the envelope configuration',
            )
        if settings.allowed_cwd_roots is not None and not any(
            _within(request.cwd, root, windows) for root in settings.allowed_cwd_roots
        ):
            return Decision(
                "deny",
                "allowed_cwd_roots",
                (
                    f"working directory {request.cwd} is outside the allowed roots: "
                    f"{', '.join(settings.allowed_cwd_roots)}",
                ),
                remedy="use a working directory inside an allowed root, or add a root to "
                "allowed_cwd_roots in the envelope configuration",
            )
        if len(request.command) > settings.max_command_chars:
            return Decision(
                "deny",
                "max_command_chars",
                (
                    f"the command is {len(request.command):,} characters long; the limit is "
                    f"{settings.max_command_chars:,}",
                ),
                remedy="shorten the command, or raise max_command_chars in the envelope configuration",
            )
        return Decision("allow", "default", ("no policy limit applies",))

    return decide
