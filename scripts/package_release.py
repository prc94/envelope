"""Assemble a single-file release bundle and SHA-256 manifest.

Usage: python scripts/package_release.py --artifact dist/envelope.pyz
The artifact must already have been built with scripts/build.py.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version() -> str:
    tree = ast.parse((ROOT / "envelope" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ValueError("Missing package version")


def package(artifact: Path, destination: Path) -> Path:
    release = version()
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(artifact.resolve()), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.stdout.strip() != f"envelope {release}":
        raise ValueError("Artifact version differs from source; rebuild before packaging")
    with zipfile.ZipFile(artifact) as archive:
        if archive.read("LICENSE") != (ROOT / "LICENSE").read_bytes():
            raise ValueError("Artifact license differs from source")
    sources = {
        "envelope.pyz": artifact,
        "INSTALL.md": ROOT / "docs" / "INSTALL.md",
        "RELEASE_NOTES.md": ROOT / "docs" / "releases" / f"{release}.md",
        "LICENSE": ROOT / "LICENSE",
    }
    for source in sources.values():
        if not source.is_file():
            raise FileNotFoundError(source)
    destination.mkdir(parents=True, exist_ok=False)
    for name, source in sources.items():
        shutil.copyfile(source, destination / name)
    manifest = "".join(
        f"{hashlib.sha256((destination / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(sources)
    )
    (destination / "SHA256SUMS").write_bytes(manifest.encode("ascii"))
    bundle = destination.parent / f"envelope-{release}.zip"
    if bundle.exists():
        raise FileExistsError(bundle)
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(destination.iterdir()):
            archive.write(source, f"envelope-{release}/{source.name}")
    checksum = hashlib.sha256(bundle.read_bytes()).hexdigest()
    bundle.with_suffix(".zip.sha256").write_bytes(f"{checksum}  {bundle.name}\n".encode("ascii"))
    return bundle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=ROOT / "dist" / "envelope.pyz")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    destination = args.output or ROOT / "dist" / f"envelope-{version()}"
    print(package(args.artifact, destination))


if __name__ == "__main__":
    main()
