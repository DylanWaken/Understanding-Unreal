#!/usr/bin/env python3
"""Check published package bytes against the independent reconstruction audit.

This is a transport/copy check, not a replacement for package_frame.py verify.
The audit records the prior full raw-source and reconstructed-payload comparison.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
PACKAGE = ROOT / 'Data/Sponza/sponza-20261115-083654'
AUDIT = ROOT / 'Data/Sponza/evidence/frame-package-audit.json'


def main():
    audit = json.loads(AUDIT.read_text(encoding='utf-8'))
    assert audit['ok'] and not audit['errors'], 'Source reconstruction audit failed'
    records = audit['files']
    expected = {r['path'] for r in records}
    actual = {p.relative_to(PACKAGE).as_posix() for p in PACKAGE.rglob('*') if p.is_file()}
    assert actual == expected, f'Inventory mismatch: missing {expected-actual}, extra {actual-expected}'

    def check(rec):
        path = PACKAGE / rec['path']
        assert path.stat().st_size == rec['bytes'], f"Size mismatch: {rec['path']}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == rec['sha256'], f"Hash mismatch: {rec['path']}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(check, records))
    print(f"Sponza published package: {len(records)} files, {sum(r['bytes'] for r in records)} bytes; all SHA-256 hashes match the reconstruction audit")


if __name__ == '__main__':
    main()
