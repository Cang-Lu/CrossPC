#!/usr/bin/env bash
# CrossPC client(Debian, 没键鼠的那台)启动器
#
# 用法:
#   ./tools/run-client.sh                      # 用 UDP 自动发现 server
#   ./tools/run-client.sh --host 192.168.1.10  # 指定 server 地址
#   CROSSPC_SCREEN=2560x1440 ./tools/run-client.sh --host 192.168.1.10
#
# 如果报找不到 python3 或 tkinter: sudo bash tools/install_linux.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "找不到 python3, 请先运行: sudo bash tools/install_linux.sh" >&2
    exit 1
fi

export PYTHONIOENCODING=utf-8
exec python3 -m crosspc client "$@"
