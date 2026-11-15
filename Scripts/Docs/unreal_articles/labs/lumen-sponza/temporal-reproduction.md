# Temporal decoder reproduction runbook

This is a technical runbook for running the two published temporal decoders from
explicit inputs. It is not article prose and does not define new interpretation.

## Packaged files

The lab directory now contains the decoder dependencies it imports:

| File | Role |
|---|---|
| `decode_placements.py` | Placement/card/world-cache decoder. Imports `temporal_dump.DumpCtx`, not the absent scratch `decode_temporal`. |
| `temporal_dump.py` | Minimal read-only `DumpCtx` extraction plus the concatenated-JSON reader it needs. No decode algorithm lives here. |
| `decode_context.py` | Existing published context module; still provides `load_concat_json` for `decode_placements`. |
| `decode_radiosity.py` | Radiosity/SH decoder with explicit raw-root and metadata CLI. |
| `archive_temporal.py` | Existing source-free package verifier/reconstructor used to recreate raw state directories. |

The placement decoder keeps its own invoked-pass cache selection. The inherited
`DumpCtx.main_cache` helper is retained only as the historical
lowest-producer-id counter-example; placement data never uses it.

## Inputs and provenance

Raw state bytes come from:

- `Data/Sponza/temporal-native/package/` (index plus content-addressed chunks)
- `Data/Sponza/temporal-native/verification.json`
- `Data/Sponza/temporal-native/source-compare.json`

Capture metadata defaults for radiosity come from:

- `Data/Sponza/evidence/temporal-captures/radiosity/capture-manifest.json`
- `Data/Sponza/evidence/temporal-captures/radiosity/capture-report.md`
- `Data/Sponza/evidence/temporal-captures/radiosity/parameter-verification.json`

The historical producer snapshots remain unchanged and are not imported by the
lab decoders:

- `Data/Sponza/evidence/temporal-captures/producer-scripts/decode_placements.py`
- `Data/Sponza/evidence/temporal-captures/producer-scripts/decode_radiosity.py`

Neither decoder reads engine shader files. Source-anchor strings in the
radiosity report and constants remain provenance; there is no `--engine-root`
option because no shader file is opened.

## Reconstruct the two representative raw states

Run from the repository root. These commands create the run/state layout the
decoders expect and refuse to overwrite existing files.

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
$check = "Scripts/Docs/unreal_articles/labs/lumen-sponza/temporal-reproduction-check"

python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/archive_temporal.py reconstruct `
  --package Data/Sponza/temporal-native/package `
  --state radiosity/baseline `
  --dest "$check/raw/radiosity/baseline" `
  --out "$check/logs/reconstruct-radiosity-baseline.json"

python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/archive_temporal.py reconstruct `
  --package Data/Sponza/temporal-native/package `
  --state light/baseline `
  --dest "$check/raw/light/baseline" `
  --out "$check/logs/reconstruct-light-baseline.json"
```

Required reconstructed layout:

```text
temporal-reproduction-check/raw/radiosity/baseline/Base/Passes.json
temporal-reproduction-check/raw/radiosity/baseline/Resources/*.bin
temporal-reproduction-check/raw/radiosity/baseline/Structures/*.bin
temporal-reproduction-check/raw/radiosity/baseline/StructuresMetadata/*.json
temporal-reproduction-check/raw/light/baseline/Base/Passes.json
temporal-reproduction-check/raw/light/baseline/Resources/*.bin
temporal-reproduction-check/raw/light/baseline/Structures/*.bin
temporal-reproduction-check/raw/light/baseline/StructuresMetadata/*.json
```

The reconstructed raw trees are temporary. This check run removed them after
the decoders and comparisons completed; `logs/reconstruct-*.json` retain the
per-file reconstruction hashes. To rerun, recreate the same two state
directories with the commands above.

## Radiosity decoder

`--raw-root` is required and is never inferred. It is the run directory
containing state subdirectories, not a state directory itself. `--out` is also
required and must differ from the decoder source directory.

```powershell
python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_radiosity.py --help

python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_radiosity.py `
  --raw-root "$check/raw/radiosity" `
  --capture-manifest Data/Sponza/evidence/temporal-captures/radiosity/capture-manifest.json `
  --capture-report Data/Sponza/evidence/temporal-captures/radiosity/capture-report.md `
  --parameter-verification Data/Sponza/evidence/temporal-captures/radiosity/parameter-verification.json `
  --states baseline `
  --out "$check/radiosity"
```

`--states` accepts a comma-separated subset of
`baseline,light-001,light-064,camera-001,camera-064`. With no `--states` all
five run. Selected labels must exist both in the capture manifest and under
`--raw-root`; the decoder exits before decoding otherwise. Checks, the
manifest, and the report are scoped to the selected states (`selected_states`
and `selected_state_count` are recorded in `checks.json`).

Outputs are written only under `--out`:

```text
radiosity/checks.json
radiosity/manifest.json
radiosity/report.md
radiosity/publish/data/probes-<state>.npz
radiosity/publish/data/probes-<state>.json
radiosity/publish/data/{checks,schema,figure-legend,probes-sample}.json
radiosity/publish/data/capture-report.md
radiosity/publish/figures/{placement,placement-xray,sh-lobes}-<state>.png
radiosity/work/
```

## Placement decoder

`--out` is required and must differ from the decoder source directory.
`--light-root` is required when `light` is selected; `--motion-root` is required
when `motion` is selected. There are no workstation defaults.

```powershell
python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_placements.py --help

python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_placements.py `
  --sequences light `
  --light-states baseline `
  --light-root "$check/raw/light" `
  --out "$check/placement"
```

The sequence/state checks compare decoded labels against the requested labels,
not against a hard-coded full light plus motion set. This lets a one-sequence,
one-state run pass its checks while still rejecting a missing requested state.
For both sequences, pass both roots:

```powershell
python -B Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_placements.py `
  --light-root "$check/raw/light" `
  --motion-root "$check/raw/motion" `
  --out "$check/placement-both"
```

Outputs are written under `--out` as
`<sequence>/data/<state>/{cards,pages,worldcache}.npz` plus their JSON records,
`<sequence>/data/sequence.json`, `<sequence>/figures/`, and top-level
`manifest.json`, `checks.json`, and `report.md`.

## Baseline agreement checks

The representative run used the reconstructed `radiosity/baseline` and
`light/baseline` states. Comparison scripts and their original results are retained in
`Data/Sponza/evidence/temporal-decoder-validation/logs/`. Copy the comparison
scripts into the newly created `$check/logs/` directory before running the commands
below; they resolve rerun outputs relative to that directory and exit
nonzero on any missing key, shape/dtype mismatch, or value mismatch.

```powershell
python -B "$check/logs/compare_radiosity_baseline.py"
python -B "$check/logs/compare_placement_baseline.py"
```

Results:

| Check | Result |
|---|---|
| `decode_radiosity.py --help` | exit 0 |
| Radiosity baseline decode (`--states baseline`) | exit 0, `overall checks passed=True`, 13 checks |
| Radiosity checks scope | `selected_states: ["baseline"]`, `selected_state_count: 1` |
| Radiosity NPZ vs published `probes-baseline.npz` | 34/34 arrays exactly equal, max abs diff 0.0 |
| `decode_placements.py --help` | exit 0 |
| Placement baseline decode (`--sequences light --light-states baseline`) | exit 0, `checks: pass` (0 failed / 12) |
| Placement cards/pages/worldcache NPZ vs published baseline | 22/22, 11/11, 20/20 arrays exactly equal, max abs diff 0.0 |
| Placement raw source hashes | 386 files hashed, 0 changed |
| Radiosity raw source hashes | before == after, unchanged |

Matching published and decoded SHA-256 values:

| Artifact | SHA-256 |
|---|---|
| `radiosity/.../probes-baseline.npz` (both) | `66fe01bd3f09c592ed72537fc1d7f523a828592f6eb9dc902fe9d0155f61d630` |
| `placements/.../cards.npz` (both) | `5fdd3d2323c9aa87e2fbd3215136e8e5a934a4e4b604fba538e78af8c07e50e3` |
| `placements/.../pages.npz` (both) | `69e9fcda71e2cbd4e9bbaa7469cb19be88a641b1c14df4c3dd0e1ca37c5062c9` |
| `placements/.../worldcache.npz` (both) | `ede0408c4c9ae9ca6cd49984b55203b0211d4ae1beaa6353bfa365907eb9d935` |

The exact array equality is evidence that the path/CLI packaging did not change
the decode algorithms or the selected numerical operands.

## Source snapshots and write boundary

`Data/Sponza/evidence/temporal-decoder-validation/logs/input-hashes.json` records the verification-time SHA-256 and byte
sizes for the package index, package verification, radiosity metadata,
historical producer snapshots, packaged decoder sources, and the published and
decoded NPZ baselines. The packaged radiosity decoder subsequently received two
descriptive corrections: the plotted radius is 75 cm, and `card_tile_in_capture`
refers to `Lumen.CardTiles`, not the exact radiosity dispatch list. These changes
do not alter any numerical operand or output array. The source hashes below
identify the verification-time snapshots:

```text
eadf3ad8e902e934caf53d6e61696af457a414990b46072c9259eb1d6fba1941  Data/Sponza/temporal-native/package/index.json
ae8fcb47127854114c3ffe92f1f688b54024311a04c4d6866224b1f807a9cc2f  Data/Sponza/temporal-native/verification.json
a0e42ceba2e9d237e8f92d46f92e176f8b38c8427b4fb3a187cf7962091537ce  Data/Sponza/temporal-native/source-compare.json
0068439843323d7a2012aaf91ea32d39290230dd8ad1da20a6f6fe1fd480affa  Data/Sponza/evidence/temporal-captures/radiosity/capture-manifest.json
d022d5fb3778ad844b6e8ed7a3d2e98e0eb15eece6604a8f2ae195b48161481e  Data/Sponza/evidence/temporal-captures/radiosity/capture-report.md
dd85ae6b1fa48b08dca14dd507ec92008098d2d64ba2fa01894a8ae895de3037  Data/Sponza/evidence/temporal-captures/radiosity/parameter-verification.json
6043443be62654b4e670e51019a3a9af9e4f63ba0396c88be787cbc6bdef0abb  Data/Sponza/evidence/temporal-captures/producer-scripts/decode_placements.py
7699d91275554c52ec585f8429ad5d1e4ec1561feb19af3458fc49fdd10efbac  Data/Sponza/evidence/temporal-captures/producer-scripts/decode_radiosity.py
b543cf94afb25ed7782f7b9f32cb8b6e1f0c0b625d40248f2d1ddc6b6f621078  Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_placements.py
8c5557abd19b87ee2a2daae6009ddea150ce3a320e36b1578e42aa07ce051914  Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_radiosity.py
845a1221aeb07af8a212ec780e05cdae5fb1a93cf013162cd8c47a440f383c5d  Scripts/Docs/unreal_articles/labs/lumen-sponza/temporal_dump.py
```

The check run wrote its temporary outputs under
`Scripts/Docs/unreal_articles/labs/lumen-sponza/temporal-reproduction-check/`
(`logs/`, `radiosity/`, `placement/`). No authored chapter, `Docs/` asset,
published baseline, historical producer script, or raw source file was
modified by this packaging step. Its reconstruction and duplicate decode trees
are temporary and are removed after validation. The retained evidence directory
contains the comparison scripts, numerical results, command logs, and hashes;
the published NPZ files remain the canonical decoded outputs.
