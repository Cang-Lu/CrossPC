#!/usr/bin/env python3
"""打一个可以直接拷到另一台机器的 zip 包。

为什么要它: Debian 那台机器上没有 git、也不想联网装东西, 最省事的办法就是
把整个工具打成一个 zip 拷过去解压。这个脚本用标准库实现, Windows 和 Linux
上都能直接跑。

安全要点: **绝不把用户的 crosspc.json / crosspc.cache.json 打进包里** ——
那里面有 IP 和 token。脚本会显式排除, 并在结尾断言一遍。
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

#: 必须打进包里的目录/文件
INCLUDE_DIRS = ("crosspc", "tools", "tests")
#: LICENSE 必须进包: MIT 的条款要求"本许可声明需随软件的所有副本一起分发"
INCLUDE_FILES = ("README.md", "pyproject.toml", ".gitignore", "LICENSE")

#: 不打包的目录名与后缀
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "dist", "build"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".pyd")
#: 用户私有文件(含 IP/token), 一定不能进包
EXCLUDE_NAMES = {"crosspc.json", "crosspc.cache.json"}


def version() -> str:
    src = (ROOT / "crosspc" / "__init__.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def collect(with_tests: bool) -> List[Tuple[Path, str]]:
    """返回 [(绝对路径, zip 内的相对路径)]。"""
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
    parser = argparse.ArgumentParser(description="打包 CrossPC 发行 zip")
    parser.add_argument("-o", "--out", default=str(ROOT / "dist"),
                        help="输出目录(默认 dist/)")
    parser.add_argument("--no-tests", action="store_true",
                        help="不把 tests/ 打进包里")
    parser.add_argument("--list", action="store_true", help="只列出会打包哪些文件")
    args = parser.parse_args(argv)

    files = collect(not args.no_tests)
    if args.list:
        for _, rel in files:
            print(rel)
        print("共 %d 个文件" % len(files))
        return 0

    for _, rel in files:
        if Path(rel).name in EXCLUDE_NAMES:
            print("拒绝打包用户私有文件: %s" % rel, file=sys.stderr)
            return 1

    target = build(Path(args.out), with_tests=not args.no_tests)
    size = target.stat().st_size
    print("已生成: %s" % target)
    print("大小  : %.1f KB (%d 个文件)" % (size / 1024.0, len(files)))
    print("SHA256: %s" % sha256(target))
    print()
    print("拷到 Debian 上之后:")
    print("  unzip %s -d ~/CrossPC && cd ~/CrossPC" % target.name)
    print("  sudo bash tools/install_linux.sh          # 装 Python/剪辑板工具/uinput 权限")
    print("  python3 -m crosspc client --host <Windows的IP>")
    print()
    print("在 Windows 上:")
    print("  python -m crosspc init && python -m crosspc gui && python -m crosspc server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
