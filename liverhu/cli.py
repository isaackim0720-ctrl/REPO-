"""Command line:

  python -m liverhu serve            start the private lab website (default port 8000)
  python -m liverhu set-password     change the website password
  python -m liverhu process IN... --out DIR   measure scans without the website
  python -m liverhu download-models  fetch the AI models (~400 MB, once)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DATA = Path.cwd() / "liverhu_data"


def _cmd_process(a) -> int:
    import os
    if a.low_memory:
        os.environ["LIVERHU_LOW_MEMORY"] = "1"
    from .config import Settings
    from .pipeline import process_case, write_tables
    from .scanio import discover_cases
    s = Settings() if a.diameter is None else Settings(roi_diameter_mm=a.diameter)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for inp in a.inputs:
        cases, warns = discover_cases(Path(inp), label=a.label, preferred_slice_mm=s.preferred_slice_mm,
                                      include_identifiers=a.include_identifiers)
        for w in warns:
            print("WARNING:", w)
        for i, case in enumerate(cases, 1):
            print(f"--- {case.case_id} ({i}/{len(cases)})")
            results.append(process_case(case, out / case.case_id, s))
    if results:
        csv_path, xlsx_path = write_tables(results, out)
        print(f"\nDone: {len(results)} case(s). Results: {xlsx_path}  and  {csv_path}")
        print("Check each case's qa.png to confirm the circles are where you expect.")
    return 0 if results else 1


def _cmd_serve(a) -> int:
    import os
    import uvicorn
    from .web import auth
    if a.low_memory:   # inherited by the per-scan worker processes
        os.environ["LIVERHU_LOW_MEMORY"] = "1"
    data = Path(a.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    if not auth.has_password(data):
        auth.first_time_password(data)
    import asyncio
    from .web.app import create_app
    from .web import tls
    app = create_app(data, keep_uploads=a.keep_uploads)
    # Plain (unencrypted) http only answers this computer itself, unless --lan-http is given.
    # Everyone else uses encrypted https: on the same network, or through Tailscale from anywhere.
    http_host = a.host if a.lan_http else "127.0.0.1"
    configs = [uvicorn.Config(app, host=http_host, port=a.port, log_level="warning")]
    https_ok = False
    if a.https_port:
        try:
            cert, key = tls.ensure_cert(data)
            configs.append(uvicorn.Config(app, host=a.host, port=a.https_port, log_level="warning",
                                          ssl_certfile=str(cert), ssl_keyfile=str(key)))
            https_ok = True
        except Exception as e:   # e.g. 'cryptography' not installed yet
            print(f"(https not available: {e}; run the update/install script to enable it)")
    from . import __version__
    mode = " [low-memory mode]" if os.environ.get("LIVERHU_LOW_MEMORY") == "1" else ""
    try:
        from .segment import device_name, _HW
        hw = _HW.get(device_name(), device_name())
    except Exception:
        hw = "CPU"
    print(f"\nHUnter website v{__version__} (liver + spleen){mode} running. AI runs on: {hw}")
    for line in _address_lines(a.port, a.https_port if https_ok else 0, a.lan_http):
        print(line)
    if os.environ.get("LIVERHU_BACKGROUND") == "1":
        print("Running in the background. Stop with: bash scripts/background_mac.sh off\n")
    else:
        print("Leave this window open while people use the website. Press Ctrl+C to stop.\n")
    servers = [uvicorn.Server(c) for c in configs]

    async def _run():
        await asyncio.gather(*(s.serve() for s in servers))
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    print("Website stopped.")
    return 0


def _address_lines(port: int, https_port: int, lan_http: bool = False) -> list[str]:
    """The addresses people open, for the start message and `liverhu addresses`."""
    import platform
    from .remote import tailscale_state
    from .web import tls
    import ipaddress
    tailnet = ipaddress.ip_network("100.64.0.0/10")   # Tailscale's own addresses; listed separately below
    ips = [ip for ip in tls.local_ips() if ipaddress.ip_address(ip) not in tailnet] or ["<this-computer's-address>"]
    lines = [f"  On this computer:            http://localhost:{port}"]
    if https_port:
        for ip in ips:
            lines.append(f"  On the same Wi-Fi / network: https://{ip}:{https_port}")
        lines.append("     (the first visit shows a 'not private' warning: choose Advanced -> Proceed / visit website)")
    if lan_http:
        for ip in ips:
            lines.append(f"  Also, NOT encrypted:         http://{ip}:{port}")
    if not https_port and not lan_http:
        lines.append("  Other computers cannot connect: https is off or not working (run the update script).")
    ts = tailscale_state(port)
    mac = platform.system() == "Darwin"
    if ts["state"] == "serving":
        lines.append(f"  From anywhere (Tailscale):   {ts['url']}")
        lines.append("     (only people in your Tailscale network can open it)")
    elif ts["state"] == "not_serving":
        how = "bash scripts/remote_access_mac.sh" if mac else f"tailscale serve --bg http://127.0.0.1:{port}"
        lines.append(f"  From anywhere: Tailscale is connected; turn it on with:  {how}")
    elif ts["state"] == "not_connected":
        lines.append("  From anywhere: open the Tailscale app and log in, then run:  bash scripts/remote_access_mac.sh"
                     if mac else "  From anywhere: open the Tailscale app and log in (see README).")
    elif mac:
        lines.append("  From anywhere (optional):    bash scripts/remote_access_mac.sh")
    return lines


def _cmd_addresses(a) -> int:
    for line in _address_lines(a.port, a.https_port):
        print(line)
    return 0


def _cmd_set_password(a) -> int:
    from .web import auth
    auth.set_password_interactive(Path(a.data).resolve())
    return 0


def _cmd_download(a) -> int:
    from .segment import download_models
    download_models()
    print("Models ready.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="liverhu", description="Automated liver CT HU measurement (3 ROIs)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="start the private lab website")
    sp.add_argument("--host", default="0.0.0.0",
                    help="network address the https website listens on (0.0.0.0 = reachable from the lab network)")
    sp.add_argument("--port", type=int, default=8000,
                    help="plain http port; answers only this computer (and Tailscale Serve) unless --lan-http")
    sp.add_argument("--https-port", type=int, default=8443,
                    help="encrypted https with a self-made certificate for other computers (0 = off)")
    sp.add_argument("--lan-http", action="store_true",
                    help="also let other computers use plain, unencrypted http (not recommended)")
    sp.add_argument("--data", default=str(DEFAULT_DATA), help="folder for results and settings")
    sp.add_argument("--keep-uploads", action="store_true", help="keep uploaded scans after processing")
    sp.add_argument("--low-memory", action="store_true",
                    help="smaller model tiles (~3.4 GB instead of ~4 GB peak RAM); use one setting per study")
    sp.set_defaults(func=_cmd_serve)
    ad = sub.add_parser("addresses", help="print the addresses people open")
    ad.add_argument("--port", type=int, default=8000)
    ad.add_argument("--https-port", type=int, default=8443)
    ad.set_defaults(func=_cmd_addresses)
    pw = sub.add_parser("set-password", help="change the website password")
    pw.add_argument("--data", default=str(DEFAULT_DATA))
    pw.set_defaults(func=_cmd_set_password)
    pr = sub.add_parser("process", help="measure scans from folders/files without the website")
    pr.add_argument("inputs", nargs="+", help="DICOM folder(s), .zip, or .nii/.nii.gz")
    pr.add_argument("--out", required=True)
    pr.add_argument("--diameter", type=float, default=None,
                    help="liver circle diameter in mm (default 30; smaller only if it does not fit)")
    pr.add_argument("--label", default="")
    pr.add_argument("--include-identifiers", action="store_true",
                    help="add DICOM PatientID/AccessionNumber/StudyDate to the results")
    pr.add_argument("--low-memory", action="store_true", help="smaller model tiles (less RAM)")
    pr.set_defaults(func=_cmd_process)
    dl = sub.add_parser("download-models", help="download the AI models")
    dl.set_defaults(func=_cmd_download)
    a = p.parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
