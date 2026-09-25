"""Build dist/envelope.pyz with the standard library's zipapp module.

Usage: python scripts/build.py [--output PATH]
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The archive's entry point must stay parseable by old interpreters so the
# version check can report a clear error instead of a SyntaxError.
MAIN = """\
import sys
from envelope._pycheck import ensure_supported

ensure_supported()

from envelope.cli import main

sys.exit(main())
"""


def build(output: Path) -> Path:
    with tempfile.TemporaryDirectory() as staging_dir:
        staging = Path(staging_dir)
        shutil.copytree(
            ROOT / "envelope",
            staging / "envelope",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (staging / "__main__.py").write_text(MAIN, encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(
            staging, target=output, interpreter="/usr/bin/env python3", compressed=True
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "envelope.pyz")
    args = parser.parse_args()
    path = build(args.output)
    print(f"built {path} ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
