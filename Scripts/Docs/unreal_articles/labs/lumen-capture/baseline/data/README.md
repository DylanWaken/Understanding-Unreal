# Data directory

`data/reference/` holds the original reference-run artifacts: scene, capture
manifest, capture session, verification and report. Image paths in these copies point
to `figures/reference/`. Absolute host paths were replaced by portable placeholders;
embedded hashes still refer to the source artifacts before the portability edits.

`data/native/` holds the native DumpGPU artifacts:

- `inventory.json` / `.csv` — all 1389 resources, including the 105 descriptor-only
  records. Nothing was dropped to reduce size.
- `pass-inventory.json` / `.csv` — all 928 passes and their parameter payload links.
- `resource-files.json` / `.csv` — all 2481 resource file records and their hashes.
- `missing-coverage.json` — the exact SkipTracking warnings, every descriptor-only
  resource and the structural limits of this DumpGPU implementation.
- `runtime-actual.json` — actual capture-time CVars, exposure, eye-adaptation and
  pass-route evidence.
- `scene.json` — the active scene, light, camera and exposure.
- `figure-manifest.json` — decoded figure metadata with resource/pass/frame mapping.
- `figure-to-native-map.json` — compact figure-to-resource/pass/frame map.
- `native-dump-summary.json` — counts, known gaps and the no-exhaustive-coverage
  statement.
- `runs/` — exact native/reference session JSONs with absolute host paths replaced
  by portable placeholders.
- `dump-metadata/` — native `Base/Passes.json`, `ResourceDescs.json`,
  `ConsoleVariables.csv`, `DumpService.json`, `ReferencedStructs.json`, `Status.txt`,
  plus all `Structures/` and `StructuresMetadata/` files. `Base/UnrealLab.log` is not
  included.

The 14.5 GB raw `Resources/` binaries are intentionally absent. `source_raw_file`
values in the manifests are relative to that external raw dump. The package records
what the native run produced; it does not claim exhaustive computational coverage.

For portability, the `r.DumpGPU.Directory` and `LiveCoding.*` path rows in
`ConsoleVariables.csv` are labelled (`external-raw-dump`, `reproduce/UnrealLab`,
`EXTERNAL_ENGINE_ROOT`) and the non-rendering AndroidFileServer `SecurityToken` is
redacted in the public Config and the embedded ini copy. The 871
`StructuresMetadata/*.json` files are byte-identical to the native dump, so their
hashes still match `pass-inventory.json`; their `FileName` fields necessarily retain
the engine-source root. Renderer/RHI values and all resource records are unchanged.
