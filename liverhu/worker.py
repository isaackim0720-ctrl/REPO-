"""Process ONE case in its own process (used by the web server so memory is fully released
after every scan and a crash cannot take the website down).

    python -m liverhu.worker job.json      # job.json = {"case": {...}, "settings": {...}, "out_dir": "..."}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    job = json.loads(Path(argv[0]).read_text())
    from .config import Settings
    from .pipeline import process_case
    from .scanio import CaseInput
    case = CaseInput.from_dict(job["case"])
    res = process_case(case, Path(job["out_dir"]), Settings.from_dict(job.get("settings")),
                       log=lambda m: print(m, flush=True))
    print(f"RESULT_STATUS {res['status']}", flush=True)
    return 0


if __name__ == "__main__":   # required: model code may start helper processes (spawn)
    sys.exit(main())
