#!/usr/bin/env bash
# CrossPC client launcher (Debian, the machine without keyboard and mouse)
#
# Usage:
#   ./tools/run-client.sh                      # find the server over UDP
#   ./tools/run-client.sh --host 192.168.1.10  # use a fixed server address
#   CROSSPC_SCREEN=2560x1440 ./tools/run-client.sh --host 192.168.1.10
#
# If python3 or tkinter is reported missing: sudo bash tools/install_linux.sh
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found; run this first: sudo bash tools/install_linux.sh" >&2
    exit 1
fi

export PYTHONIOENCODING=utf-8
exec python3 -m crosspc client "$@"
