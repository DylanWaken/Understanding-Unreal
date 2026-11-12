# Warmed LumenRoom GPU timestamps

UE 5.8.1 on NVIDIA RTX PRO 6000 Blackwell Workstation Edition, driver 32.0.16.1062, D3D12 SM6. The offscreen draw uses 1280×720, as recorded by the fixed draw target and the renderer event labels. The camera component reports 70.00598° FOV, set by converting a 43° vertical FOV to the 16:9 horizontal FOV. The scene uses the same software Lumen, VSM and histogram exposure settings as the reference.

Unreal’s ProfileGPU logged seven GPU timestamp samples after 240 warmup draw requests, with 28 draw requests between samples. DumpGPU and path tracing were disabled. Each measured engine frame contained three sequential SceneRender subtrees on the graphics queue. The tables contain graphics queue rows. No async queue table was emitted.

| Measure | n | Min ms | Median ms | Max ms |
|---|---:|---:|---:|---:|
| Graphics queue busy | 7 | 2.910 | 3.004 | 3.329 |
| Graphics queue busy + wait | 7 | 3.430 | 3.570 | 3.970 |
| One SceneRender subtree | 21 | 0.886 | 0.958 | 1.170 |

The following figures take the largest single inclusive occurrence of the named event within each profiled engine frame, then summarize those seven values. Each event occurred three times per frame. Parent and child event spans can overlap.

| Named event | n | Min ms | Median ms | Max ms |
|---|---:|---:|---:|---:|
| Lumen scene update | 7 | 0.100 | 0.101 | 0.105 |
| Lumen reflections | 7 | 0.112 | 0.126 | 0.139 |
| VSM mask projection | 7 | 0.070 | 0.085 | 0.093 |
| Diffuse indirect composite | 7 | 0.013 | 0.015 | 0.022 |

The queue measurements include all three scene submissions. The per-event table leaves the profiler hierarchy intact and gives no aggregate Lumen or VSM share.

See [timestamp rows](gpu-profile-rows.json), [original profiler tables](gpu-profile-blocks.txt) and [measurement data and raw hashes](profile-result.json).
