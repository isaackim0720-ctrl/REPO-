#!/usr/bin/env bash
# Run HUnter in the background on a Mac, so no Terminal window has to stay open.
# It starts by itself whenever you log in, keeps the Mac from dozing off while it runs,
# and starts again if it ever stops.
#   bash scripts/background_mac.sh on       start it now and at every login
#   bash scripts/background_mac.sh off      stop it, and don't start it at login any more
#   bash scripts/background_mac.sh status   is it running, and at which addresses
#   bash scripts/background_mac.sh log      show its latest messages
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$PWD"
LABEL="org.hunter.website"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
CMD="${1:-status}"

[ "$(uname)" = "Darwin" ] || { echo "This script is for macOS."; exit 1; }
[ -x .venv/bin/python ] || { echo "Not installed here yet - run: bash scripts/install_mac_linux.sh"; exit 1; }

loaded()    { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
answering() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8000/"; }
logfile()   { printf '%s' "$DIR/liverhu_data/website_log.txt"; }
xml()       { local s="$1"; s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; printf '%s' "$s"; }
stop_agent() {
  if loaded; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    for _ in $(seq 1 20); do answering || return 0; sleep 1; done
  fi
  return 0
}

case "$CMD" in
on)
  # macOS does not let programs running in the background use Downloads, Desktop or Documents.
  case "$DIR/" in
    "$HOME/Downloads/"* | "$HOME/Desktop/"* | "$HOME/Documents/"* | "$HOME/Library/Mobile Documents/"*)
      NEW="$HOME/HUnter"
      echo "HUnter is in your $(basename "$(dirname "$DIR")") folder. macOS does not let programs that run"
      echo "in the background use that folder, so HUnter has to move to:  $NEW"
      echo "Your results, password and install move with it."
      if [ -e "$NEW" ]; then echo "$NEW already exists. Rename or remove it first, then run this again."; exit 1; fi
      if answering && ! loaded; then
        echo "HUnter is running in a Terminal window right now. Press Control + C there first,"
        echo "then run this again."
        exit 1
      fi
      read -r -p "Move it now? (y/n) " ans
      case "$ans" in y | Y | yes | Yes | YES) ;; *) echo "Nothing changed."; exit 1 ;; esac
      stop_agent
      mv "$DIR" "$NEW"
      cd "$NEW"
      DIR="$NEW"
      echo "Moved. The HUnter folder is now $NEW  (to open it in Finder: open ~/HUnter)"
      ;;
  esac
  if [ ! -f liverhu_data/auth.json ]; then
    echo "First choose the password lab members will use to open the website."
    .venv/bin/python -m liverhu set-password --data "$DIR/liverhu_data"
  fi
  stop_agent
  if answering; then
    echo "HUnter is running in a Terminal window right now. Go to that window, press Control + C,"
    echo "then run this again."
    exit 1
  fi
  mkdir -p "$HOME/Library/LaunchAgents" liverhu_data
  LOG="$(logfile)"
  if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5000000 ]; then mv -f "$LOG" "$LOG.old"; fi
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-i</string>
    <string>/bin/bash</string>
    <string>$(xml "$DIR/scripts/start_mac_linux.sh")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$DIR")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>LIVERHU_BACKGROUND</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF
  plutil -lint "$PLIST" >/dev/null
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
  echo "Starting HUnter in the background (this takes up to a minute)..."
  for _ in $(seq 1 120); do answering && break; sleep 1; done
  if answering; then
    echo ""
    echo "== HUnter is running in the background. You can close Terminal now. =="
    echo "It starts by itself whenever you log in to this Mac."
    .venv/bin/python -m liverhu addresses || true
    echo ""
    echo "The Mac still has to be on and awake: keep it plugged in with the lid open."
    echo "To stop it:  bash scripts/background_mac.sh off"
  else
    echo "HUnter did not start. Its last messages:"
    tail -n 25 "$LOG" 2>/dev/null || true
    exit 1
  fi
  ;;
off)
  stop_agent
  rm -f "$PLIST"
  echo "HUnter is stopped and will not start at login."
  echo "To run it in a Terminal window instead:  caffeinate -i bash scripts/start_mac_linux.sh"
  ;;
status)
  if loaded && answering; then
    echo "HUnter is running in the background."
    .venv/bin/python -m liverhu addresses || true
  elif loaded; then
    echo "HUnter is set to run in the background but is not answering yet (it may still be starting)."
    echo "Latest messages:"
    tail -n 15 "$(logfile)" 2>/dev/null || true
  elif answering; then
    echo "HUnter is running in a Terminal window (not in the background)."
  else
    echo "HUnter is not running. Start it in the background with:  bash scripts/background_mac.sh on"
  fi
  ;;
log)
  tail -n 40 "$(logfile)" 2>/dev/null || echo "No messages yet."
  ;;
*)
  echo "Use one of:  bash scripts/background_mac.sh on | off | status | log"
  exit 1
  ;;
esac
