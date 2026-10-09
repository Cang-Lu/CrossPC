#!/usr/bin/env bash
# CrossPC 一键验收(Debian 侧)。
#
# 用法:  bash tools/run-tests.sh
#
# Linux 端只能当 client, 所以这里跑的是: 环境自检 -> 单机回环 -> 注入验收 ->
# 剪辑板验收(能跑的项都会跑, 不能跑的自动跳过)。日志同样落在 logs/ 下。
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "找不到 python3, 请先运行: sudo bash tools/install_linux.sh" >&2
    exit 1
fi

export PYTHONIOENCODING=utf-8
exec python3 tools/run-tests.py "$@"
