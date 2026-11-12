# Measured workload (run 20261112-153914)

Every value below is read from a captured payload or descriptor; each row's exact
file, SHA-256, resource pointer and producer pass are in `workload.json`.
`capacity` = descriptor `NumElements`/`ByteSize`; `actual` = payload allocator or
counter; `args` = programmed dispatch groups `(x,y,z)` per record.

| graph | uniform probes | adaptive actual | adaptive capacity | screen-probe compacted texels actual/cap | screen-probe args | reflection compacted texels actual/cap | reflection args | direct card pages | indirect card pages | card page cap | card dispatch args | card tile allocator u32 | radiosity tiles actual/cap | radiosity args | world caches (probes,traces,tiles,indirection) | dispatch-arg buffers |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0001 | 3600 | 0 | 1800 | 82102/430080 | (1283,1,1) / (2566,1,1) / (0,1,1) / (0,1,1) | 19534/1136640 | (306,1,1) / (611,1,1) / (77,1,1) | 42 | 42 | 64 | (1,1,1) / (168,1,1) | [548, 0, 548, 0] | 2192/16384 | (548,1,1) / (1096,1,1) / (35,1,1) / (64,548,1) | probes=146,traces=146,tiles=2336,indir=192x48x48<br>probes=8021,traces=120,tiles=120,indir=72x24x24 | 47 |
| 0003 | 3600 | 0 | 1800 | 82175/430080 | (1284,1,1) / (2568,1,1) / (0,1,1) / (0,1,1) | 19551/1136640 | (306,1,1) / (611,1,1) / (77,1,1) | 42 | 42 | 64 | (1,1,1) / (168,1,1) | [548, 0, 548, 0] | 2192/16384 | (548,1,1) / (1096,1,1) / (35,1,1) / (64,548,1) | probes=146,traces=146,tiles=2336,indir=192x48x48<br>probes=8021,traces=127,tiles=127,indir=72x24x24 | 47 |
| 0004 | 3600 | 0 | 1800 | 81307/430080 | (1271,1,1) / (2541,1,1) / (0,1,1) / (0,1,1) | 19530/1136640 | (306,1,1) / (611,1,1) / (77,1,1) | 42 | 42 | 64 | (1,1,1) / (168,1,1) | [548, 0, 548, 0] | 2192/16384 | (548,1,1) / (1096,1,1) / (35,1,1) / (64,548,1) | probes=146,traces=146,tiles=2336,indir=192x48x48<br>probes=8021,traces=163,tiles=163,indir=72x24x24 | 47 |

## Notes

- Uniform probes: 80x45 = 3600 per graph, from `ScreenProbeIndirectArgs[0]` with the
  8x8 shader group (`LumenScreenProbeGather.usf:315-333`) and the captured
  `r.Lumen.ScreenProbeGather.DownsampleFactor=16` over the captured 1280x720 view.
- Adaptive probes actually spawned: **0** in all three graphs; capacity 1800.
  The probe-data atlas is allocated for 80x84 slots (80x56 viewport buffer + 50%
  adaptive rows), which is capacity, not occupancy.
- Compacted screen-probe trace records 2/3 read `CompactedTraceTexelAllocator[1]`,
  which is outside the 1-element allocation; they are recorded verbatim and marked
  undefined (see `workload.json` `fidelity_caveat`).
- Radiosity: 2192 tiles; record0/1/2 decode as ceil(NumTraces/64)=548,
  ceil(NumTraces/32)=1096, ceil(NumProbes/64)=35, implying 35072 traces
  (16 per tile). Record3 is the hardware-ray-tracing layout `(64,548,1)`.
- Card pages: 42 indices direct and indirect; draw args `(6,42,0,0)`;
  dispatch `(ceil(42/64)=1, 4*42=168)`.
- World cache instance 0: 146 probes / 146 traced / 2336 tiles / 192x48x48 indirection.
  Instance 1: 8021 probes / 120-163 traced / 120-163 tiles / 72x24x24 indirection.
- `dispatch triples` count is the number of indirect-arg buffers in that graph, not
  executions. No GPU timing or hardware counter exists in this dump; nothing here is
  a performance measurement.
