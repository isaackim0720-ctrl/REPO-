"""Finds this computer's Tailscale address, used to open HUnter from anywhere.

Tailscale is a free app that links your own devices (and people you invite) in a private,
encrypted network. `tailscale serve` then gives HUnter an https://<computer>.<network>.ts.net
address with a real certificate. Only devices in that network can open it; it is not on the
public internet and search engines cannot find it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

CANDIDATES = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",   # macOS app (App Store or standalone)
    r"C:\Program Files\Tailscale\tailscale.exe",              # Windows
)


def tailscale_cli() -> str | None:
    forced = os.environ.get("LIVERHU_TAILSCALE")               # tests / unusual installs
    if forced:
        return forced if os.path.exists(forced) else None
    found = shutil.which("tailscale")
    if found:
        return found
    return next((c for c in CANDIDATES if os.path.exists(c)), None)


def _run(cli: str, *args: str, timeout: float = 4.0) -> str | None:
    try:
        p = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def tailscale_state(port: int = 8000) -> dict:
    """{'state': 'not_installed' | 'not_connected' | 'not_serving' | 'serving', 'url': str | None}"""
    cli = tailscale_cli()
    if not cli:
        return {"state": "not_installed", "url": None}
    try:
        status = json.loads(_run(cli, "status", "--json") or "null")
    except ValueError:
        status = None
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return {"state": "not_connected", "url": None}
    name = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".")
    url = f"https://{name}" if name else None
    serve = _run(cli, "serve", "status", "--json") or ""
    serving = any(f"{host}:{port}" in serve for host in ("127.0.0.1", "localhost"))
    return {"state": "serving" if (serving and url) else "not_serving", "url": url}
