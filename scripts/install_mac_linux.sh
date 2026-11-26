#!/usr/bin/env bash
# Liver HU - one-time install on the computer that will run the website (macOS or Linux).
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3.12 python3.13 python3.11 python3; do command -v "$c" >/dev/null && { PY="$c"; break; }; done
fi
[ -n "$PY" ] || { echo "Install Python 3.12 from https://www.python.org/downloads/ first."; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
# Linux without an NVIDIA GPU: use the much smaller CPU-only PyTorch build.
if [ "$(uname)" = "Linux" ] && ! command -v nvidia-smi >/dev/null; then
  python -m pip install torch --index-url https://download.pytorch.org/whl/cpu || true
fi
python -m pip install -r requirements.txt
python -m liverhu download-models
echo "Installed. Start the website with: scripts/start_mac_linux.sh"
