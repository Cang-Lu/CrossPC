#!/usr/bin/env python3
"""Fail if any repository file still contains CJK characters.

Why this exists
---------------
This repository is English-only by policy. The single exception is
``README.zh-CN.md``, which is kept as the original Chinese README for the
author's own reference.

A rule like that is easy to state and very easy to break by accident: one
Chinese comment pasted into a new module is nearly invisible in review. So it
is enforced mechanically here, called from the test suite
(``tests/test_language.py``) and from CI.

Usage
-----
    python tools/check_english.py          # scan, exit 1 if anything is found
    python tools/check_english.py -v       # also print what was skipped

Exit codes: 0 = clean, 1 = violations found, 2 = usage error.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Iterable, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The one file that is allowed to be Chinese.
ALLOWED = {"README.zh-CN.md"}

#: Directories that never belong to the shipped source.
SKIP_DIRS = {".git", "__pycache__", "dist", "build", "logs", ".ghcache",
             "node_modules", ".venv", "venv", ".idea", ".vscode"}

#: Prefixes of scratch directories that sandboxed runs like to leave behind.
SKIP_PREFIXES = (".crosspc", "probe-")

#: Extensions that are not text (a byte scan would produce noise).
BINARY_EXT = {".zip", ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".ico",
              ".pdf", ".exe", ".dll", ".so", ".bin", ".whl"}

CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")


def _git_file_list(root: str) -> List[str] | None:
    """Every file git would commit: tracked ones plus untracked-but-not-ignored.

    Going through git is the most faithful definition of "this repository's
    content": scratch files, caches, logs and build output are excluded by
    .gitignore automatically, and files that exist only on this machine's disk
    stop producing false alarms. Returns None when git or the repository is not
    available (for example a copy extracted from the release zip), in which case
    the caller falls back to walking the tree.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [line.strip().replace("\\", "/") for line in proc.stdout.splitlines()
            if line.strip()]


def _scannable(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if rel in ALLOWED:
        return False
    if name.startswith(SKIP_PREFIXES):
        return False
    if os.path.splitext(name)[1].lower() in BINARY_EXT:
        return False
    parts = set(rel.split("/")[:-1])
    if parts & SKIP_DIRS:
        return False
    if any(part.startswith(SKIP_PREFIXES) for part in parts):
        return False
    return True


def iter_files(root: str = ROOT) -> Iterable[str]:
    """Yield repository-relative paths of files worth scanning."""
    tracked = _git_file_list(root)
    if tracked is not None:
        for rel in tracked:
            if _scannable(rel):
                yield rel
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(SKIP_PREFIXES)]
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            rel = rel.replace("\\", "/")
            if _scannable(rel):
                yield rel


def scan(paths: Iterable[str], root: str = ROOT
         ) -> Tuple[List[Tuple[str, int, str]], List[str]]:
    """Return (violations, unreadable). Each violation is (path, line, text)."""
    violations: List[Tuple[str, int, str]] = []
    unreadable: List[str] = []
    for rel in paths:
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            with open(full, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except (OSError, UnicodeDecodeError):
            unreadable.append(rel)          # binary or locked: not our business
            continue
        for number, line in enumerate(lines, 1):
            if CJK.search(line):
                violations.append((rel, number, line.strip()[:110]))
    return violations, unreadable


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that the repository contains no CJK characters "
                    "outside README.zh-CN.md")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also print skipped/unreadable files")
    parser.add_argument("--root", default=ROOT, help="repository root")
    parser.add_argument("--max", type=int, default=25,
                        help="how many violations to print (default 25)")
    args = parser.parse_args(argv)

    files = list(iter_files(args.root))
    violations, unreadable = scan(files, args.root)
    print("scanned %d files, allowed Chinese file: %s"
          % (len(files), ", ".join(sorted(ALLOWED))))
    if args.verbose:
        print("skipped %d unreadable/binary files" % len(unreadable))
    if not violations:
        print("OK: no CJK characters found")
        return 0
    affected = sorted({v[0] for v in violations})
    print("FAIL: %d CJK lines in %d files" % (len(violations), len(affected)))
    for rel, number, text in violations[:max(0, args.max)]:
        print("  %s:%d: %s" % (rel, number, text))
    if len(violations) > args.max:
        print("  ... and %d more" % (len(violations) - args.max))
    print()
    print("Files affected: %s" % ", ".join(affected[:20]))
    return 1


if __name__ == "__main__":
    sys.exit(main())
