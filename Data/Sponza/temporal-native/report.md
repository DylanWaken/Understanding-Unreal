# Temporal native archive report

**Scope: SELECTED-resource temporal captures - native DumpGPU bytes, not complete GPU frames and not shader locals.** Every regular file under each accepted state is stored as opaque bytes; nothing is filtered by resource name, and line endings are never normalized.

## Accepted capture runs

| Run | Run id | Capture manifest | States | Source directory |
|---|---|---|---:|---|
| `light` | `20261115-101057` | `D:\UnrealUnderstanding-Temporal\output\temporal\run-20261115-101057.json` | 5 | `D:\UnrealUnderstanding-Temporal\output\gpu-dump\raw\20261115-101057` |
| `motion` | `20261115-104035` | `D:\UnrealUnderstanding-Motion\output\temporal\run-20261115-104035.json` | 3 | `D:\UnrealUnderstanding-Motion\output\gpu-dump\raw\20261115-104035` |
| `radiosity` | `20261115-105951` | `D:\UnrealUnderstanding-Radiosity\output\radiosity\run-20261115-105951.json` | 5 | `D:\UnrealUnderstanding-Radiosity\output\gpu-dump\raw\20261115-105951` |

## Per-state inventory and capture sequence

| State | Files | Manifest files | Native bytes | Capture update | Engine frame(s) | Camera |
|---|---:|---:|---:|---|---:|---|
| `light/baseline` | 3,169 | 3,169 | 2,989,801,808 | 3 | 127 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `light/lightstep-001` | 2,269 | 2,269 | 1,998,063,689 | 1 (target 1) | 169 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `light/lightstep-008` | 1,631 | 1,631 | 1,981,110,588 | 7 (target 8) | 178 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `light/lightstep-032` | 1,613 | 1,613 | 1,980,570,491 | 31 (target 32) | 205 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `light/lightstep-064` | 1,615 | 1,615 | 1,980,536,932 | 63 (target 64) | 240 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `motion/baseline` | 3,199 | 3,199 | 2,990,620,330 | 3 | 127 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `motion/camera-001` | 3,212 | 3,212 | 2,990,386,206 | 1 (target 1) | 133 | [-850.0, -150.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `motion/camera-064` | 3,215 | 3,215 | 2,991,210,068 | 63 (target 64) | 198 | [-850.0, -150.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `radiosity/baseline` | 800 | 800 | 1,448,004,599 | 1 | 124 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `radiosity/light-001` | 800 | 800 | 1,447,958,622 | 1 (target 1) | 128 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `radiosity/light-064` | 800 | 800 | 1,447,633,834 | 63 (target 64) | 192 | [-850.0, 0.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `radiosity/camera-001` | 800 | 800 | 1,447,986,939 | 1 (target 1) | 262 | [-850.0, -150.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |
| `radiosity/camera-064` | 800 | 800 | 1,448,099,144 | 63 (target 64) | 326 | [-850.0, -150.0, 90.0] rot(p=13.0,y=0.0,r=-0.0) hfov=70.0 |

## Archive construction

| Quantity | Value |
|---|---:|
| States | 13 |
| Files preserved | 23,923 |
| Native raw bytes (all states) | 27,141,983,250 bytes |
| Unique file hashes | 8,431 |
| Unique chunks | 8,468 |
| Unique chunk raw bytes | 9,874,820,751 bytes |
| Chunk gzip bytes | 1,946,607,594 bytes |
| gzip / raw ratio | 0.197128 |
| Reconstruction space, all 13 states | 27,141,983,250 bytes |
| Reconstruction space, largest state | 2,991,210,068 bytes |
| Package bytes on disk | 1,957,882,466 bytes |
| index.json bytes | 11,274,872 bytes |
| Max chunk raw / gzip bytes | 33,554,432 bytes / 14,298,622 bytes |
| Max package file | `chunks/fd/8e/fd8e7b6fc18f37ecf9f58c1dd642bbe0aef0c1e25ce944f674ced73ae1806112.bin.gz` (14,298,622 bytes) |
| Max source file | `radiosity/baseline/Resources/00010000026048559cc0.mip0.v00000000000000000000.bin` (134,217,728 bytes) |
| Source file records over 90 MiB (split into chunks) | 15 |
| Chunk size limit | 33,554,432 bytes |
| Git LFS | not used |

## Independent verification (no source)

- Status: **PASS**
- Chunks checked with gzip SHA-256 and decompressed raw SHA-256: 8,468
- gzip MTIME=0 for every chunk: **PASS**
- Files whose record size/SHA-256 was validated: 23,923
- Unique chunk sequences hashed: 8,431; duplicate sequences reused: 15,492
- Max archive/Git blob check: **PASS** (limit 90 MiB). Source files over 90 MiB: 15 (each split into <=32 MiB chunks, so no archive blob is large).
- Failures: None

## Source before/after comparison

- Status: **PASS**
- Files checked after build: 23,923
- Bytes checked after build: 27,141,983,250 bytes
- Mismatched states: 0
- Missing, extra, size and hash mismatches: none

## Bounded reconstruction demonstration

| File | Bytes | SHA-256 | Matches index |
|---|---:|---|---|
| `GPUDumpViewer.html` | 195,428 | `eb599b5171e99d7c2e35732abf9525baeb39b9c2ac86619522f307c57cc90384` | yes |
| `OpenGPUDumpViewer.sh` | 2,060 | `36c1b4731dc096b05bea7024259aa375479ba631c44b7f81f1f10d85fcd3d1f3` | yes |

Reconstructed from `package/index.json` + `package/chunks/` only; the source tree was not read by the reconstruct command. State: `light/baseline`.

## Capture coverage, gaps and notices

- Radiosity required-resource coverage: **5/5 accepted states report 16/16 required resources present, 0 missing**.
- Radiosity pass-parameter blocks recorded per state: 21 (all referenced Structures and StructuresMetadata bytes are preserved).
- `light` manifest selected-resource coverage (same counts in every accepted state): 33 present, 42 listed as not present in that selected filter; these are manifest filter statistics, not archive omissions.
- `r.RHI.UniformBufferContentMap.Enable` values in accepted manifest: true.
- `motion` manifest selected-resource coverage (same counts in every accepted state): 59 present, 16 listed as not present in that selected filter; these are manifest filter statistics, not archive omissions.
- `r.RHI.UniformBufferContentMap.Enable` values in accepted manifest: true.
- Known gaps recorded in index: none
- Notice: Scope: SELECTED-resource temporal captures only; these are native DumpGPU bytes, not complete GPU frames and not shader locals.
- Notice: Light roots come from accepted main run 20261115-101057 (gpu-dump/latest-main-run.json). gpu-dump/latest-run.json points at the rejected camera-retry 20261115-101923, which is not included.
- Notice: The plan named radiosity run 20261115-105838, but that run was superseded and its raw removed. The accepted replacement is 20261115-105951 with the same five required states.
- Notice: Accepted radiosity manifest 20261115-105951 records r.RHI.UniformBufferContentMap.Enable=true, not false; all 16 required resources are present (missing_count=0) and 21 pass-parameter blocks per state are recorded. The superseded 105838 false value was an evaluator artifact, not a gap in these selected bytes.

## Failures

None.

Reproduction commands are in `README.md`; the portable CLI is `archive.py`.
