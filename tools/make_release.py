#!/usr/bin/env python3
"""Build a zip you can copy straight to another machine.

Why it exists: the Debian machine has no git and no wish to install anything
over the network, so the least painful route is to pack the whole tool into one
zip and unzip it there. The script uses only the standard library and runs on
both Windows and Linux.

Safety point: **never pack the user's crosspc.json / crosspc.cache.json into
the archive** -- they hold the IP and the token. The script excludes them
explicitly and asserts it once more at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import zipfile
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent

#: directories/files that must go into the archive
INCLUDE_DIRS = ("crosspc", "tools", "tests")
#: LICENSE must ship in the archive: the MIT terms require "this permission
#: notice to be included in all copies of the software"
INCLUDE_FILES = ("README.md", "pyproject.toml", ".gitignore", "LICENSE")

#: directory names and suffixes that are not packed
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "dist", "build"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".pyd")
#: user-private files (they contain the IP/token) that must never be packed
EXCLUDE_NAMES = {"crosspc.json", "crosspc.cache.json"}


def version() -> str:
    src = (ROOT / "crosspc" / "__init__.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def collect(with_tests: bool) -> List[Tuple[Path, str]]:
    """Return [(absolute path, path inside the zip)]."""
    files: List[Tuple[Path, str]] = []
    dirs = list(INCLUDE_DIRS)
    if not with_tests and "tests" in dirs:
        dirs.remove("tests")
    for name in dirs:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            parts = set(path.relative_to(ROOT).parts)
            if parts & EXCLUDE_DIRS:
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIX:
                continue
            if path.name in EXCLUDE_NAMES:
                continue
            files.append((path, str(path.relative_to(ROOT)).replace("\\", "/")))
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            files.append((path, name))
    return files


def build(out_dir: Path, with_tests: bool = True, top: str = "") -> Path:
    ver = version()
    top = top or ("crosspc-%s" % ver)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ("%s.zip" % top)
    files = collect(with_tests)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, rel in files:
            zf.write(path, "%s/%s" % (top, rel))
    return target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the CrossPC release zip")
    parser.add_argument("-o", "--out", default=str(ROOT / "dist"),
                        help="output directory (default: dist/)")
    parser.add_argument("--no-tests", action="store_true",
                        help="do not pack tests/ into the archive")
    parser.add_argument("--list", action="store_true", help="only list which files would be packed")
    args = parser.parse_args(argv)

    files = collect(not args.no_tests)
    if args.list:
        for _, rel in files:
            print(rel)
        print("%d files in total" % len(files))
        return 0

    for _, rel in files:
        if Path(rel).name in EXCLUDE_NAMES:
            print("refusing to pack the user-private file: %s" % rel, file=sys.stderr)
            return 1

    target = build(Path(args.out), with_tests=not args.no_tests)
    size = target.stat().st_size
    print("created: %s" % target)
    print("size   : %.1f KB (%d files)" % (size / 1024.0, len(files)))
    print("SHA256 : %s" % sha256(target))
    print()
    print("After copying it to Debian:")
    print("  unzip %s -d ~/CrossPC && cd ~/CrossPC" % target.name)
    print("  sudo bash tools/install_linux.sh          # installs Python/clipboard tools/uinput permissions")
    print("  python3 -m crosspc client --host <Windows IP>")
    print()
    print("On Windows:")
    print("  python -m crosspc init && python -m crosspc gui && python -m crosspc server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
