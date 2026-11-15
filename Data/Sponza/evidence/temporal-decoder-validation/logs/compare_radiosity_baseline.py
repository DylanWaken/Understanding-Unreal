"""Compare the re-run radiosity baseline arrays with the published baseline."""
import hashlib
import json
from pathlib import Path

import numpy as np

CHECK = Path(__file__).resolve().parents[1]
PUB = Path(
    r"D:\UnrealUnderstanding\Docs\Unreal\assets\lumen-sponza\temporal\radiosity"
    r"\publish\data\probes-baseline.npz"
)
GOT = CHECK / "radiosity/publish/data/probes-baseline.npz"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    if not GOT.is_file():
        raise SystemExit("decoded baseline NPZ is missing: %s" % GOT)
    a = np.load(PUB)
    b = np.load(GOT)
    common = sorted(set(a.files) & set(b.files))
    differences = []
    max_abs = 0.0
    for key in common:
        x, y = a[key], b[key]
        if x.shape != y.shape or x.dtype != y.dtype:
            differences.append(
                {
                    "key": key,
                    "reason": "shape_or_dtype",
                    "published": [list(x.shape), str(x.dtype)],
                    "decoded": [list(y.shape), str(y.dtype)],
                }
            )
            continue
        if not np.array_equal(x, y, equal_nan=True):
            delta = (
                float(np.max(np.abs(x.astype(np.float64) - y.astype(np.float64))))
                if x.size
                else 0.0
            )
            max_abs = max(max_abs, delta)
            differences.append({"key": key, "reason": "values", "max_abs": delta})
    result = {
        "published_npz": str(PUB),
        "published_sha256": sha256(PUB),
        "decoded_npz": str(GOT),
        "decoded_sha256": sha256(GOT),
        "published_key_count": len(a.files),
        "decoded_key_count": len(b.files),
        "common_key_count": len(common),
        "missing_keys": sorted(set(a.files) - set(b.files)),
        "extra_keys": sorted(set(b.files) - set(a.files)),
        "exact_equal_all_common_arrays": not differences,
        "differing_keys": differences,
        "max_abs_diff": max_abs,
    }
    out = CHECK / "logs/radiosity-baseline-comparison.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if not differences and not result["missing_keys"] and not result["extra_keys"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
