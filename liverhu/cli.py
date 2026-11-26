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
    s = Settings(roi_diameter_mm=a.diameter)
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
    from .web.app import create_app
    app = create_app(data, keep_uploads=a.keep_uploads)
    print(f"\nLiver HU website running. On this computer open: http://localhost:{a.port}")
    print(f"Others on the lab network open: http://<this-computer's-name-or-IP>:{a.port}")
    print("Leave this window open while people use the website. Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
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
    sp.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = reachable from the lab network")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--data", default=str(DEFAULT_DATA), help="folder for results and settings")
    sp.add_argument("--keep-uploads", action="store_true", help="keep uploaded scans after processing")
    sp.add_argument("--low-memory", action="store_true",
                    help="smaller model tiles (~3.4 GB instead of ~4 GB peak RAM); use one setting per study")
    sp.set_defaults(func=_cmd_serve)
    pw = sub.add_parser("set-password", help="change the website password")
    pw.add_argument("--data", default=str(DEFAULT_DATA))
    pw.set_defaults(func=_cmd_set_password)
    pr = sub.add_parser("process", help="measure scans from folders/files without the website")
    pr.add_argument("inputs", nargs="+", help="DICOM folder(s), .zip, or .nii/.nii.gz")
    pr.add_argument("--out", required=True)
    pr.add_argument("--diameter", type=float, default=15.0, help="ROI diameter in mm (default 15)")
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
