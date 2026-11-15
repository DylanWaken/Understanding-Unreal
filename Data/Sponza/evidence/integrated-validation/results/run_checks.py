#!/usr/bin/env python3
"""Run the bounded final-validation command set and record exact outcomes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(r"D:/UnrealUnderstanding-Review/final-validation")
REPO = Path(r"D:/UnrealUnderstanding")
ARTICLE = REPO / "Scripts/Docs/unreal_articles"
LOGS = HERE / "results/logs"

COMMANDS = [
    ("01", "build.py --check", [sys.executable, "build.py", "--check"], ARTICLE),
    ("02", "math_typesetting.py --check --node node",
     [sys.executable, "math_typesetting.py", "--check", "--node", "node"], ARTICLE),
    ("03", "validate.py", [sys.executable, "validate.py"], ARTICLE),
    ("04", "validate_labs.py", [sys.executable, "validate_labs.py"], ARTICLE),
    ("05", "validate_lumen_v2.py", [sys.executable, "validate_lumen_v2.py"], ARTICLE),
    ("06", "check_site_independence.py",
     [sys.executable, "Scripts/Docs/check_site_independence.py"], REPO),
    ("07", "labs/lumen-reading/generate.py --check",
     [sys.executable, "labs/lumen-reading/generate.py", "--check"], ARTICLE),
    ("08", "labs/lumen-course/generate.py --check",
     [sys.executable, "labs/lumen-course/generate.py", "--check"], ARTICLE),
    ("09", "node --check course.js",
     ["node", "--check", "Docs/Unreal/assets/lumen-course/course.js"], REPO),
    ("10", "node --check temporal.js",
     ["node", "--check", "Docs/Unreal/assets/lumen-course/temporal.js"], REPO),
    ("11", "build_temporal_manifest.py --check",
     [sys.executable, "Scripts/Docs/unreal_articles/labs/lumen-sponza/build_temporal_manifest.py", "--check"],
     REPO),
]


def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    results = []
    for num, label, argv, cwd in COMMANDS:
        start = time.time()
        proc = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                              encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        elapsed = time.time() - start
        out = proc.stdout
        err = proc.stderr
        (LOGS / f"{num}-{label.split()[0].replace('/','_')}.out.log").write_text(out, encoding="utf-8")
        (LOGS / f"{num}-{label.split()[0].replace('/','_')}.err.log").write_text(err, encoding="utf-8")
        summary = (out.strip().splitlines()[-1] if out.strip() else "")[:400]
        rec = {
            "id": f"{num}-{label.split()[0]}",
            "command": " ".join([Path(argv[0]).name] + argv[1:]),
            "cwd": str(cwd),
            "exit": proc.returncode,
            "seconds": round(elapsed, 2),
            "stdout_tail": out.strip().splitlines()[-6:],
            "stderr_tail": err.strip().splitlines()[-6:],
            "summary": summary,
        }
        results.append(rec)
        print(f"[{num}] exit={proc.returncode} {elapsed:6.2f}s  {summary}")
        if err.strip():
            print("  stderr:", err.strip().splitlines()[-2:])
    (HERE / "results/commands.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [r["id"] for r in results if r["exit"] != 0]
    print(f"\ncommands={len(results)} passed={len(results)-len(failed)} failed={failed}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
