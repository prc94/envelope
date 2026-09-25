"""Shared test configuration.

Async tests run on a fresh event loop via a small hook, so the suite needs no
async plugin (keeps dev dependencies to pytest alone).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    names = pyfuncitem._fixtureinfo.argnames  # pyright: ignore[reportPrivateUsage]
    kwargs: dict[str, Any] = {name: pyfuncitem.funcargs[name] for name in names}
    asyncio.run(pyfuncitem.obj(**kwargs))
    return True
