# Sponza capture and numerical decoders

The Sponza images in the course come from Unreal Engine. This lab records how the frames were captured and how the CPU decoders recovered the values used in `chapters/lumen-course*.html`.

See [the data package](../../../../../Data/Sponza/README.md) for lossless verification, reconstruction, capture coverage, and asset/engine identities. Reconstruct payloads and copy the package's `native/` contents into the same root before running decoders that consume a native DumpGPU directory. You can view the published figures and check their retained records without an Unreal installation.

## Checks without raw reconstruction

From the repository root:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-sponza/verify_package_copy.py
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_receivers.py --out Docs/Unreal/assets/lumen-sponza/receivers --stage verify
python Scripts/Docs/unreal_articles/labs/lumen-sponza/cache-replay/verify_queries.py
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_context.py --out Docs/Unreal/assets/lumen-sponza/context --stage verify
```

The package-copy check compares all published bytes against the independent source/reconstruction audit. Full decompression/reconstruction verification is available through `lumen-frame/tools/package_frame.py`; the compact check does not claim to replace it. The receiver check verifies view identity, material/depth/direct-light assertions, figure hashes, dimensions, and masks. The cache check validates the saved query operands and the eight distinct world-probe preview tiles.

## Recompute from captured bytes

The receiver decoder uses Python's standard library. The cache/context decoders also need NumPy and Pillow. They are CPU operations and do not launch Unreal.

Install the tested decoder versions with `python -m pip install -r Scripts/Docs/unreal_articles/labs/lumen-sponza/requirements.txt`. The context check validates 252 relationships covering camera projection, packed counters, active allocation bounds, SH normalization, ray-layout distinctions, figure dimensions and retained native arrays. Its `curation-notes.json` records interpretation-label corrections separately from captured numeric data.

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_receivers.py --dump D:/SponzaReconstructed --analysis Data/Sponza/evidence/analysis-20261115-083654.json --out D:/SponzaReceiverDecode --stage all
python Scripts/Docs/unreal_articles/labs/lumen-sponza/cache-replay/surface_cache_query.py --root D:/SponzaReconstructed --out D:/SponzaCacheDecode
python Scripts/Docs/unreal_articles/labs/lumen-sponza/cache-replay/world_cache_query.py --root D:/SponzaReconstructed --out D:/SponzaCacheDecode --engine D:/UnrealEngine
python Scripts/Docs/unreal_articles/labs/lumen-sponza/cache-replay/verify_queries.py --data D:/SponzaCacheDecode
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_context.py --dump D:/SponzaReconstructed --out D:/SponzaContextDecode --stage all
```

Source-range hashes in the cache replay are read from `D:/UnrealEngine` by default; adapt that source root when reproducing elsewhere. Replaying the captured arithmetic is distinct from proving the original shader-local query or dispatch selection. Each result carries that distinction and its coverage gaps explicitly.

## Virtual shadow storage

`decode_vsm_storage.py` follows the captured sun page table, physical pool, projection records, and page metadata. The main chapter shows physical allocation, virtual addresses, a contiguous light-space depth reconstruction, and native page closeups. JSON/NPZ retain the numerical fields; the full two-slice pool remains in the lossless frame archive.

To check the published data without reconstructing the frame or installing Unreal, copy `Docs/Unreal/assets/lumen-sponza/vsm-storage/` to a temporary directory and run:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_vsm_storage.py --out D:/VSMVerify --verify
```

The checker writes its results to that copy's `checks.json`. To regenerate from reconstructed native bytes:

```powershell
python Scripts/Docs/unreal_articles/labs/lumen-sponza/decode_vsm_storage.py --dump D:/SponzaReconstructed --out D:/VSMDecode --all --engine D:/UnrealEngine --receivers Docs/Unreal/assets/lumen-sponza/receivers/data/receivers.json --view Docs/Unreal/assets/lumen-sponza/context/data/orientation.json --archive Data/Sponza/sponza-20261115-083654
```

PageTable hardware mip 0 contains eight packed logical shadow-mip regions. PageFlags H0 to H6 occupy a separate hardware mip chain. Physical metadata is an earlier pass-810 snapshot, while the table and depth pool were captured for consumer pass 840. The two receiver queries replay declared center-address arithmetic; they do not recover unrecorded SMRT sample directions or shader-local bias decisions.

## Capture-source snapshot

`capture-snapshot/` preserves the scripts used to download/split/import Sponza, author the scene, apply the final rendering configuration, capture Lumen/path-traced/controlled-change views, and request/analyze DumpGPU. These scripts include the original workstation's paths and bridge assumptions. They are provenance source, not a portable installer or a promise that arbitrary engine builds support the instrumented exporter.

The authoritative final settings are `Docs/Unreal/assets/lumen-sponza/data/scene-config.json` and the actual readbacks in the run records. Initial `author_sponza.py` defaults precede that final configuration. Apply the final configuration before comparing captures. Original records remain preserved with explicit interpretation corrections; no raw payload was rewritten to agree with a replay.
