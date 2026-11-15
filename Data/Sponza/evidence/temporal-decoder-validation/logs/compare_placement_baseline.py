"""Compare the re-run placement baseline arrays with the published baseline."""
import hashlib
import json
from pathlib import Path

import numpy as np

CHECK = Path(__file__).resolve().parents[1]
PUB = Path(
    r"D:\UnrealUnderstanding\Docs\Unreal\assets\lumen-sponza\temporal\placements"
    r"\light\data\baseline"
)
GOT = CHECK / "placement/light/data/baseline"
FILES = ("cards.npz", "pages.npz", "worldcache.npz")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def compare_one(name: str) -> dict:
    pub_p = PUB / name
    got_p = GOT / name
    if not got_p.is_file():
        return {"file": name, "error": "decoded file missing", "decoded": str(got_p)}
    a = np.load(pub_p)
    b = np.load(got_p)
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
    return {
        "file": name,
        "published": str(pub_p),
        "published_sha256": sha256(pub_p),
        "decoded": str(got_p),
        "decoded_sha256": sha256(got_p),
        "published_key_count": len(a.files),
        "decoded_key_count": len(b.files),
        "common_key_count": len(common),
        "missing_keys": sorted(set(a.files) - set(b.files)),
        "extra_keys": sorted(set(b.files) - set(a.files)),
        "exact_equal_all_common_arrays": not differences,
        "differing_keys": differences,
        "max_abs_diff": max_abs,
    }


def main() -> int:
    result = {
        "published_dir": str(PUB),
        "decoded_dir": str(GOT),
        "files": [compare_one(name) for name in FILES],
    }
    result["all_files_exact_equal"] = all(
        item.get("exact_equal_all_common_arrays")
        and not item.get("missing_keys")
        and not item.get("extra_keys")
        for item in result["files"]
    )
    out = CHECK / "logs/placement-baseline-comparison.json"
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["all_files_exact_equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
