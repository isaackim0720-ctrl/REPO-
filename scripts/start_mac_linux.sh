#!/usr/bin/env bash
# Starts the Liver HU website. Keep this terminal open while people use it.
# Add --low-memory if the computer runs short of memory.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Not installed yet - run scripts/install_mac_linux.sh first."; exit 1; }
echo "Others on the lab network open http://<one of these addresses>:8000"
(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null || true)
exec .venv/bin/python -m liverhu serve --data "$PWD/liverhu_data" "$@"
