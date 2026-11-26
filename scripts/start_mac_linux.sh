#!/usr/bin/env bash
# Starts the Liver HU website. Keep this terminal open while people use it.
# On computers with 8 GB RAM or less, low-memory mode is switched on automatically
# (set LIVERHU_LOW_MEMORY=0 to prevent that, or add --low-memory to force it).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Not installed yet - run scripts/install_mac_linux.sh first."; exit 1; }
MEM_GB="$( (sysctl -n hw.memsize 2>/dev/null || awk '/MemTotal/ {print $2 * 1024}' /proc/meminfo 2>/dev/null || echo 0) \
          | awk '{printf "%d", $1 / 1073741824 + 0.5}')"
LOWMEM=""
case " $* " in *" --low-memory "*) ;; *)
  if [ "${LIVERHU_LOW_MEMORY:-}" != "0" ] && [ "${MEM_GB:-0}" -gt 0 ] && [ "${MEM_GB}" -le 8 ]; then
    echo "This computer has ${MEM_GB} GB of RAM: using low-memory mode."
    LOWMEM="--low-memory"
  fi ;;
esac
echo "Others on the lab network open http://<one of these addresses>:8000"
(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null || true)
exec .venv/bin/python -m liverhu serve --data "$PWD/liverhu_data" $LOWMEM "$@"
