#!/usr/bin/env bash
# Lets the people you invite open HUnter from anywhere (home, school, phone data), encrypted,
# through the free app Tailscale. Nobody else can reach it, and search engines cannot find it.
#   bash scripts/remote_access_mac.sh        turn it on and show the address
#   bash scripts/remote_access_mac.sh off    turn it off again
set -euo pipefail
cd "$(dirname "$0")/.."

TS=""
if command -v tailscale >/dev/null 2>&1; then
  TS="$(command -v tailscale)"
elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
  TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
fi
if [ -z "$TS" ]; then
  echo "Tailscale is not installed yet."
  echo "1. Install the free Tailscale app from the Mac App Store (or https://tailscale.com/download)."
  echo "2. Open it, click its icon in the menu bar at the top of the screen, and log in."
  echo "3. Run this again:  bash scripts/remote_access_mac.sh"
  exit 1
fi

if [ "${1:-on}" = "off" ]; then
  "$TS" serve --https=443 off || true
  echo "Access from anywhere is off. HUnter still works on this Mac and on the same Wi-Fi."
  exit 0
fi

if ! "$TS" status >/dev/null 2>&1; then
  # Its menu-bar icon is easy to miss (on MacBooks it can hide behind the notch), so start the
  # app and log in from here instead.
  echo "Tailscale is not connected yet. Starting it..."
  open -a Tailscale 2>/dev/null || true
  sleep 5
  if ! "$TS" status >/dev/null 2>&1; then
    echo "If the Mac asks to allow a VPN configuration for Tailscale, click Allow."
    echo "Log in: hold Command and double-click the link below, sign in, then come back here."
    "$TS" up || true
  fi
  if ! "$TS" status >/dev/null 2>&1; then
    echo "Tailscale is still not connected. Open the Tailscale app from Applications, log in,"
    echo "then run this again."
    exit 1
  fi
fi

echo "Turning on encrypted access through Tailscale..."
echo "(The first time, a link may appear: open it, click Enable, then come back here.)"
"$TS" serve --bg http://127.0.0.1:8000
echo ""
if [ -x .venv/bin/python ]; then .venv/bin/python -m liverhu addresses || true; fi
echo ""
echo "This stays on, also after a restart. HUnter itself must be running for the address to work."
echo "To let lab members in: https://login.tailscale.com/admin/users -> Invite users."
echo "They install Tailscale on their own computer or phone and accept the invite."
