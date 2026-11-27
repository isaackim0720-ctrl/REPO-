#!/usr/bin/env bash
# Update HUnter to the latest version on GitHub.
# Keeps the install (.venv), the website password and all results (liverhu_data/).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Not installed here yet - run: bash scripts/install_mac_linux.sh"; exit 1; }
[ -d liverhu ] || { echo "This does not look like the HUnter folder; nothing changed."; exit 1; }
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
echo "Downloading the latest version..."
curl -fsSL "https://github.com/isaackim0720-ctrl/REPO-/archive/refs/heads/main.zip" -o "$T/latest.zip"
unzip -q "$T/latest.zip" -d "$T"
SRC="$(find "$T" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -f "$SRC/liverhu/__init__.py" ] || { echo "The download looks wrong; nothing changed."; exit 1; }
rm -rf liverhu
cp -R "$SRC/liverhu" .
cp -R "$SRC/scripts/." scripts/
[ -d "$SRC/tests" ] && rm -rf tests && cp -R "$SRC/tests" .
cp "$SRC/requirements.txt" "$SRC/README.md" .
# NOTE: keep lines 1-17 of this file exactly as they are. Older copies of this script replace
# themselves at line 17 and then carry on reading the new copy from that point.
PY="$PWD/.venv/bin/python"   # used directly: still works after the HUnter folder is moved
echo "Checking packages and AI models (first time after an update may download ~250 MB)..."
"$PY" -m pip install -q -r requirements.txt
"$PY" -m liverhu download-models
echo ""
if [ "$(uname)" = "Darwin" ] && launchctl print "gui/$(id -u)/org.hunter.website" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/org.hunter.website"
  echo "== Updated! HUnter restarted in the background with the new version."
  echo "   Wait a minute, then reload the page (Command + R). =="
else
  echo "== Updated! If the website is running: press Ctrl+C in its Terminal window, then run"
  echo "   caffeinate -i bash scripts/start_mac_linux.sh"
  echo "   (or, to run it with no Terminal window:  bash scripts/background_mac.sh on)"
  echo "=="
fi
exit 0
