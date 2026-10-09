#!/usr/bin/env bash
# CrossPC one-shot acceptance tests (Debian side).
#
# Usage:  bash tools/run-tests.sh
#
# The Linux side can only be a client, so what runs here is: environment
# self-check -> single-machine loopback -> injection acceptance test ->
# clipboard acceptance test (everything that can run does run, everything that
# cannot is skipped automatically). Logs also land in logs/.
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found; run this first: sudo bash tools/install_linux.sh" >&2
    exit 1
fi

export PYTHONIOENCODING=utf-8
exec python3 tools/run-tests.py "$@"
