#!/usr/bin/env python3
"""Build a clearly-labelled synthetic demo site in a temporary directory.

This helper exists only for manual visual testing (CUA) in the absence of a real
capture.  It copies the site shell, generates the synthetic fixture, and packages
it into the copied site's assets/lumen-frame path.  It never modifies the source
site or presents the fixture as runtime evidence.

    python tests/make_demo.py
    python tests/make_demo.py C:\\Temp\\frame-demo
    python -m http.server 8765 --directory C:\\Temp\\frame-demo\\site

The generated package has fixture=true and a visible synthetic-fixture banner.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a labelled synthetic frame-explorer demo.")
    parser.add_argument("output", nargs="?", help="Output directory (default: a new temp directory).")
    args = parser.parse_args()
    output = Path(args.output).resolve() if args.output else Path(tempfile.mkdtemp(prefix="frame-explorer-demo-"))
    if output.exists() and any(output.iterdir()):
        print(f"error: output is not empty: {output}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)

    site = output / "site"
    shutil.copytree(ROOT / "site", site)
    dump = output / "synthetic-dump"
    subprocess.run([sys.executable, str(HERE / "synthetic_fixture.py"), str(dump)], check=True)
    package = site / "assets" / "lumen-frame"
    result = subprocess.run([
        sys.executable, str(ROOT / "tools" / "package_frame.py"), "build",
        "--input", str(dump), "--output", str(package),
        "--schema", str(HERE / "demo_schema.json"), "--force",
    ], text=True)
    if result.returncode != 0:
        return result.returncode
    print()
    print("Labelled synthetic demo site: " + str(site))
    print("Open frame-explorer.html from a local HTTP server, for example:")
    print(f"  python -m http.server 8765 --directory {site}")
    print("  http://127.0.0.1:8765/frame-explorer.html")
    print("The package sets fixture=true and shows a synthetic-fixture banner; it is not runtime evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
