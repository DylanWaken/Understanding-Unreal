# Sponza capture evidence

The [Lumen course](https://dylanwaken.github.io/Understanding-Unreal/Unreal/lumen.html) uses a Sponza model imported into Unreal Engine. Unreal rendered every capture in this directory. The decoders read those captures to recover the underlying values.

The lossless frame is in `sponza-20261115-083654/`. [Open it in the frame explorer](https://dylanwaken.github.io/Understanding-Unreal/Unreal/frame-explorer.html?package=https%3A%2F%2Fraw.githubusercontent.com%2FDylanWaken%2FUnderstanding-Unreal%2Fmain%2FData%2FSponza%2Fsponza-20261115-083654%2F). The site loads the small index first, and resource chunks on demand. The data lives outside `Docs/` so the Pages deployment stays within its size limit.

## What is preserved

The separate [temporal native archive](temporal-native/README.md) contains thirteen accepted selected-resource observations for scene-anchored cards, screen probes, SH, surface radiosity and world-cache placement over time. It preserves all 23,923 files from those state directories, totaling 27,141,983,250 native bytes. It is a separate package and timeline from the full static frame below.

- 12,458 source files: 11,446 versioned resource/structure payloads and 1,012 native metadata files.
- 95,161,036,158 payload bytes, stored losslessly through SHA-256 chunk deduplication and gzip. The complete package occupies 657,904,432 bytes in 6,852 files.
- 886 passes, 1,529 normalized resources, 6,675 subresource layouts, 803 native parameter-binding records, 521 native uniform records with 35,648 bindings, and 9,262 console variables.
- All three scene invocations and the Slate graph. The presented scene is graph index 3, passes 593–885, with the 1280×720 view inside 1280×888 allocations.

`evidence/frame-package-audit.json` records a source-versus-reconstruction SHA-256 comparison for all 11,446 payloads, all 1,012 native copies, and all 6,852 package files: zero missing files or hash mismatches. `evidence/` also retains capture settings, diagnostic/controlled-change images, run records, and packaging logs. Original run records contain historical workspace paths, which serve as provenance and are not requirements for viewing the published package.

The package holds every available byte, but those bytes do not show the full execution. `coverage.complete` is deliberately false. The 2,913 recorded gaps include absent draw counts, parameterless/unrecorded binding blocks, duplicated descriptor identities, native uniform timing limits, and 1,297 buffer versions without a matching completion record. Nine ray-tracing scene-buffer descriptors have an unsupported staging-copy warning: nonzero bytes do not prove usable acceleration structures. The archive leaves shader-local values, unrecorded sample choices, and uncaptured resources unrecorded rather than guessing them. See `evidence/frame-package-gaps.tsv` and the explorer's coverage ledger.

## Verify or reconstruct

From the repository root:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-frame/tools/package_frame.py verify --package Data/Sponza/sponza-20261115-083654
python Scripts/Docs/unreal_articles/labs/lumen-frame/tools/package_frame.py reconstruct --package Data/Sponza/sponza-20261115-083654 --dest D:/SponzaReconstructed
Copy-Item -Path Data/Sponza/sponza-20261115-083654/native/* -Destination D:/SponzaReconstructed -Recurse
```

The destination must be a new, intentionally chosen directory. Reconstructing the payloads requires about 95.2 GB; viewing selected resources does not require that space. `native/` holds the original non-payload files at their original relative paths. Verification reads all payloads logically, but does not write a reconstructed copy.

For a local same-origin explorer, serve the repository root with `python -m http.server 8778 --bind 127.0.0.1` and open `http://127.0.0.1:8778/Docs/Unreal/frame-explorer.html?package=../../Data/Sponza/sponza-20261115-083654/`.

## Interpretation corrections

`analysis-20261115-083654.original.json` is the unmodified capture analyzer output. The curated version corrects `FPackedView.TranslatedWorldToView` to byte offset 128 and names the matrix at offset 192 `TranslatedWorldToClip`; its correction ledger retains the original file hash and each payload hash. Origin, view rectangle, forward vector, and FOV checks were unaffected. The preserved analyzer script includes this correction. No raw capture bytes or lossless-package files were changed.

The original Lumen run record in this directory is retained as provenance. The course links a curated copy under `Docs/Unreal/assets/lumen-sponza/data/`, with an explicit correction ledger for diagnostic interpretations: notably the VSM cache legend, inert diagnostic controls, and the difference between disabling final gather and selecting a different GI pipeline.

## Asset and engine provenance

Khronos Sponza revision `8f492390ddc7a2ae221a1369ef560589cf8c70fb`; [upstream model and terms](https://github.com/KhronosGroup/glTF-Sample-Assets/tree/8f492390ddc7a2ae221a1369ef560589cf8c70fb/Models/Sponza). The model's listed Cryengine Limited License is distinct from the CC-BY-4.0 metadocumentation license. Attribution copies are under `Docs/Unreal/assets/lumen-sponza/attribution/`.

Engine source lineage: UE 5.8.1, base `71fe36aac5a8df5ccd66c763ffc902b29b6a9c43`, local instrumented build `b4effda9-1b7d-4e62-b9a6-cb99e41a70c9`. The saved map SHA-256 is `3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64`. Capture and decode scripts are retained under `Scripts/Docs/unreal_articles/labs/lumen-sponza/`; the capture scripts are an environment-specific provenance snapshot, not a turnkey installer, and they do not guarantee that a different engine build has the same layouts.
