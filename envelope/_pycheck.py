# pyright: basic
"""Interpreter version check.

Written without modern syntax (no annotations) so it can run, and fail
politely, on any Python 3.
"""

import sys

MIN_VERSION = (3, 12)


def version_error(version_info=None):
    """Return an error message if the interpreter is too old, else None."""
    info = sys.version_info if version_info is None else version_info
    if tuple(info[:2]) >= MIN_VERSION:
        return None
    return "envelope requires Python %d.%d or newer (running %d.%d.%d at %s)" % (
        MIN_VERSION[0],
        MIN_VERSION[1],
        info[0],
        info[1],
        info[2],
        sys.executable,
    )


def ensure_supported(version_info=None):
    """Exit with status 2 and a message on stderr if the interpreter is too old."""
    message = version_error(version_info)
    if message is not None:
        sys.stderr.write("error: " + message + "\n")
        sys.exit(2)
