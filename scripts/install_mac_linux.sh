#!/usr/bin/env bash
# Liver HU - one-time install on the computer that will run the website (macOS or Linux).
# Needs internet; downloads about 1.5 GB and takes 10-20 minutes.
set -euo pipefail
cd "$(dirname "$0")/.."

# Some medical-imaging packages only ship ready-made builds for Python 3.10-3.14.
ok_py() { "$1" -c 'import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) else 1)' >/dev/null 2>&1; }

echo "== Liver HU installer: 10-20 minutes, about 1.5 GB download. Keep this window open. =="

# An install folder made with an unsupported Python (e.g. 3.15) is removed and redone.
if [ -x .venv/bin/python ] && ! ok_py .venv/bin/python; then
  echo "Removing an earlier install made with an unsupported Python version..."
  rm -rf .venv
fi

if [ ! -x .venv/bin/python ]; then
  PY=""
  if [ -z "${LIVERHU_FORCE_PRIVATE_PYTHON:-}" ]; then
    for c in ${PYTHON:-} python3.12 python3.13 python3.11 python3.14 python3; do
      if command -v "$c" >/dev/null 2>&1 && ok_py "$c"; then PY="$c"; break; fi
    done
  fi
  if [ -n "$PY" ]; then
    echo "Using $("$PY" --version 2>&1)"
    "$PY" -m venv .venv
  else
    BASE="$(command -v python3 || true)"
    if [ -z "$BASE" ]; then
      echo "Python was not found. Install it from https://www.python.org/downloads/ and run this again."
      exit 1
    fi
    echo "Your Python ($("$BASE" --version 2>&1)) does not work with some medical-imaging packages."
    echo "Downloading a private copy of Python 3.12 just for this program (your own Python is not changed)..."
    "$BASE" -m venv .bootstrap
    .bootstrap/bin/python -m pip install -q --upgrade pip uv
    .bootstrap/bin/python -m uv venv --seed --python 3.12 .venv
  fi
fi

. .venv/bin/activate
echo "Installing into: $(python --version 2>&1)"
python -m pip install --upgrade pip
# Linux without an NVIDIA GPU: use the much smaller CPU-only PyTorch build.
if [ "$(uname)" = "Linux" ] && ! command -v nvidia-smi >/dev/null; then
  nice -n 10 python -m pip install torch --index-url https://download.pytorch.org/whl/cpu || true
fi
nice -n 10 python -m pip install -r requirements.txt
nice -n 10 python -m liverhu download-models
echo ""
echo "== Installed! Start the website with:  bash scripts/start_mac_linux.sh =="
