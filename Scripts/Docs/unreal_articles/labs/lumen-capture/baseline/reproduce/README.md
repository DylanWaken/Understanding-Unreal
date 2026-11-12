# Reproduction scripts

These are portable copies. The originals under `reference/scripts/` and
`buffers/scripts/` were not modified. Differences are limited to path handling:
`EngineRoot`, output/evidence roots and `ARDA_*` environment overrides.

| Script | Purpose | Portability |
|---|---|---|
| `capture_reference.py` | Unreal-side Lumen/path-tracer capture | `ARDA_PROJECT_ROOT`, `ARDA_REFERENCE_ROOT`, `ARDA_REFERENCE_OUTPUT`, optional `ARDA_SCENE_INVENTORY` / `ARDA_REQUIRE_SCENE_MATCH` |
| `run_reference.ps1` | Build/run reference capture | mandatory `-EngineRoot`; `-OutputRoot`, `-ProjectRoot` |
| `image_stats.py` | dependency-free PNG statistics | no Unreal import, no fixed paths |
| `gpudump_capture.py` | Unreal-side DumpGPU/VSM/1024 capture | `ARDA_BUFFERS_ROOT`, `ARDA_PROJECT_ROOT`, `ARDA_DUMP_MODE`, `ARDA_REFERENCE_CAPTURES` |
| `run_gpudump.ps1` | run a DumpGPU mode | mandatory `-EngineRoot`; `-BuffersRoot`, `-ProjectRoot`, `-ReferenceCaptures` |
| `analyze_dump.py` | parse DumpGPU metadata | `dump_dir` argument, optional `--out` |
| `build_inventory.py` | build resource/pass/gap inventories | `dump_dir` argument, optional `--out-dir` |
| `decode_figures.py` | decode native resources to PNG | `ARDA_BUFFERS_ROOT`, `ARDA_DUMP_DIR`, `ARDA_DECODED_DIR` |
| `extract_runtime.py` | extract actual renderer route/exposure | `ARDA_BUFFERS_ROOT`, `ARDA_RUNS_DIR`, `ARDA_DUMP_DIR` |
| `publish_progress.py` | update progress JSON | `ARDA_BUFFERS_ROOT` |
| `verify_package.py` | offline package verifier | `--root` auto-detected; optional original-image comparison |

All Python scripts can be syntax-checked without importing Unreal:

```powershell
python -m py_compile reproduce/scripts/*.py
```

`verify_package.py` has no third-party dependencies. It uses the same
dependency-free PNG reader as `image_stats.py`.
