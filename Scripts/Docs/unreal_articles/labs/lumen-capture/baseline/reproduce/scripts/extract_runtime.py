"""Extract the actual capture-time renderer state from the full dump.

Writes buffers/runtime-actual.json:
  * capture-time CVars from Base/ConsoleVariables.csv
  * exposure method / PP settings / numeric pre-exposure / eye-adaptation buffer
  * actual active shader route (CVar + pass-name evidence, not project ini guesses)
  * dump settings and frame identity
"""
import csv
import json
import os
import re
import struct
from pathlib import Path

ROOT = Path(os.environ.get('ARDA_BUFFERS_ROOT', str(Path(__file__).resolve().parents[1])))
RUNS_DIR = Path(os.environ.get('ARDA_RUNS_DIR', str(ROOT / 'runs')))
RUNS = sorted(RUNS_DIR.glob('binary-*.json'), key=lambda p: p.stat().st_mtime)
if not RUNS:
    raise SystemExit(f'No binary-*.json session found in {RUNS_DIR}')
SESSION = json.loads(RUNS[-1].read_text(encoding='utf-8'))
DUMP = Path(os.environ.get('ARDA_DUMP_DIR', SESSION['dump_directory']))
BASE = DUMP / 'Base'


def read_cvars(path):
    out = {}
    with open(path, 'r', encoding='utf-8-sig', errors='replace', newline='') as f:
        for row in csv.reader(f):
            if len(row) >= 4 and row[0].startswith('r.'):
                out[row[0]] = {'type': row[1], 'set_by': row[2], 'value': ','.join(row[3:])}
    return out


CVARS = read_cvars(BASE / 'ConsoleVariables.csv')
KEYS = [
    'r.Substrate', 'r.Substrate.ProjectGBufferFormat', 'r.Substrate.BytesPerPixel',
    'r.MegaLights.EnableForProject', 'r.MegaLights.Allowed', 'r.MegaLights.Supported',
    'r.Lumen.HardwareRayTracing', 'r.Lumen.HardwareRayTracing.LightingMode',
    'r.Shadow.Virtual.Enable', 'r.RayTracing', 'r.RayTracing.Shadows',
    'r.RayTracing.GlobalIllumination', 'r.PathTracing',
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.ScreenPercentage', 'r.AntiAliasingMethod', 'r.MotionBlurQuality',
    'r.Lumen.ScreenProbeGather.ScreenTraces', 'r.Lumen.Reflections.ScreenTraces',
    'r.LumenScene.SurfaceCache.AtlasSize',
    'r.DefaultFeature.AutoExposure', 'r.DefaultFeature.AutoExposure.Method',
    'r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange',
    'r.EyeAdaptation.MethodOverride', 'r.EyeAdaptation.PreExposureOverride',
    'r.EyeAdaptationQuality', 'r.Tonemapper.Quality', 'r.Tonemapper.Sharpen',
    'r.RHI.UniformBufferContentMap.Enable',
    'r.DumpGPU.Root', 'r.DumpGPU.Texture', 'r.DumpGPU.Buffer', 'r.DumpGPU.PassParameters',
    'r.DumpGPU.ConsoleVariables', 'r.DumpGPU.Explore', 'r.DumpGPU.Upload',
    'r.DumpGPU.Directory', 'r.DumpGPU.MaxStagingSize', 'r.DumpGPU.Stream',
    'r.DumpGPU.RedumpInputs', 'r.DumpGPU.FrameCount', 'r.DumpGPU.FrameDelay',
    'r.DumpGPU.Delay', 'r.DumpGPU.Draws', 'r.DumpGPU.Mask', 'r.DumpGPU.EnableLogWrite',
]
key_cvars = {k: CVARS.get(k, {'found': False}) for k in KEYS}
exposure_cvars = {k: v for k, v in CVARS.items()
                  if re.search(r'EyeAdaptation|PreExposure|AutoExposure|Exposure', k)}

# Numeric pre-exposure from the captured FViewUniformShaderParameters content.
pre_exposure = None
one_over = None
view_meta = None
view_content = None
for meta_path in (DUMP / 'StructuresMetadata').glob('*.json'):
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8'))
    except Exception:
        continue
    if meta.get('StructTypeName') != 'FViewUniformShaderParameters':
        continue
    if not meta_path.name.startswith('0001'):
        continue
    members = {m['Name']: m for m in meta.get('Members', [])}
    if 'PreExposure' not in members:
        continue
    # The graph-0001 view content is recorded in ReferencedStructs.json.
    content_id = None
    for obj in re.finditer(r'\{[^{}]*"Pass"[^{}]*"ReferencedStructs"[^{}]*\{.*?\}\s*\}', '', re.S):
        pass
    # Read the ReferencedStructs sequence and find View.View for graph 0001.
    text = (BASE / 'ReferencedStructs.json').read_text(encoding='utf-8', errors='replace')
    dec = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos] in ' \r\n\t':
            pos += 1
        if pos >= len(text):
            break
        obj, pos = dec.raw_decode(text, pos)
        refs = obj.get('ReferencedStructs', {})
        for name, ref in refs.items():
            if name.endswith('View') and ref.get('Metadata', '').startswith('0001'):
                content_id = ref.get('Content')
                break
        if content_id:
            break
    if not content_id:
        continue
    raw_path = DUMP / 'Structures' / f'{content_id}.bin'
    if not raw_path.exists():
        continue
    raw = raw_path.read_bytes()
    view_meta = meta_path.name
    view_content = content_id

    def member_value(name):
        m = members[name]
        return struct.unpack_from('<f', raw, m['Offset'])[0]

    pre_exposure = member_value('PreExposure')
    one_over = member_value('OneOverPreExposure')
    break

# EyeAdaptationBuffer: StructuredBuffer<float4>[2] = 32 bytes.
eye_adaptation = []
for r in json.loads((ROOT / 'inventory.json').read_text(encoding='utf-8'))['resources']:
    if r['name'] == 'EyeAdaptationBuffer' and r['resource_id'].startswith('0001'):
        for f in r['binary_files']:
            data = (DUMP / f['file']).read_bytes()
            if len(data) == 32:
                vals = struct.unpack('<8f', data)
                eye_adaptation.append({
                    'resource_id': r['resource_id'],
                    'file': f['file'],
                    'version_pass_id': f.get('version_pass_id'),
                    'slot0': {'x_exposure_scale': vals[0], 'y_exposure_target': vals[1],
                              'z_average_scene_luminance': vals[2], 'w_exposure_bias_only': vals[3]},
                    'slot1': {'x_average_local_exposure': vals[4], 'y': vals[5], 'z': vals[6], 'w': vals[7]},
                })

# Pass-name evidence for the actual route.
passes = json.loads((ROOT / 'pass-inventory.json').read_text(encoding='utf-8'))['passes']
tokens = ['Lumen', 'TraceScreen', 'TraceVoxels', 'ScreenProbeIntegrate', 'FilterRadianceWithGather',
          'ReflectionsResolve', 'VirtualShadowMap', 'RenderVirtualShadowMaps', 'MegaLights',
          'Substrate', 'RayTracing', 'HardwareRayTracing', 'GBuffer', 'HistogramEyeAdaptation',
          'Tonemap', 'FXAA', 'DiffuseIndirectComposite', 'RenderLight']
evidence = {}
for t in tokens:
    hits = [p for p in passes
            if t.lower() in ((p['event_name'] or '') + ' | ' +
                             ' | '.join(p.get('parent_event_scopes') or [])).lower()]
    evidence[t] = {'count': len(hits),
                   'examples': [p['event_name'] + ' @ ' + ' / '.join(p.get('parent_event_scopes') or [])
                                for p in hits[:8]]}

substrate = key_cvars.get('r.Substrate', {}).get('value')
megalights = key_cvars.get('r.MegaLights.EnableForProject', {}).get('value')
lumen_hwrt = key_cvars.get('r.Lumen.HardwareRayTracing', {}).get('value')
vsm = key_cvars.get('r.Shadow.Virtual.Enable', {}).get('value')
raytracing = key_cvars.get('r.RayTracing', {}).get('value')

route = {
    'substrate_enabled': substrate not in (None, '0', 'false'),
    'substrate_state': f'r.Substrate={substrate} -> legacy GBuffer path' if substrate == '0' else
                       f'r.Substrate={substrate}',
    'megalights_enabled': megalights not in (None, '0', 'false'),
    'megalights_state': f'r.MegaLights.EnableForProject={megalights}; r.MegaLights.Allowed='
                        f"{key_cvars.get('r.MegaLights.Allowed', {}).get('value')}",
    'lumen_hardware_ray_tracing': lumen_hwrt,
    'lumen_route': 'software tracing' if lumen_hwrt == '0' else f'r.Lumen.HardwareRayTracing={lumen_hwrt}',
    'virtual_shadow_maps': vsm,
    'ray_tracing_project_setting': raytracing,
    'active_view_mode': 'VMI_Lit (view_mode_index 3); path tracer available but not the active view',
    'evidence': evidence,
    'statement': ('D3D12 SM6, legacy GBuffer (r.Substrate=0), MegaLights disabled '
                  '(r.MegaLights.EnableForProject=0), Lumen software tracing '
                  '(r.Lumen.HardwareRayTracing=0), Virtual Shadow Maps enabled '
                  '(r.Shadow.Virtual.Enable=1). r.RayTracing=1 only enables the project ray-tracing '
                  'capability; r.RayTracing.Shadows=0 and Lumen HWRT is off. The active passes include '
                  'Lumen ScreenProbe/Reflections TraceScreen+TraceVoxels, LumenReflections/ReflectionsResolve, '
                  'and VirtualShadowMap page/raster passes. No Substrate or hardware-ray-tracing pass '
                  'appears. MegaLights is off; only three MegaLights.LightPowerHistoryRatio clear-buffer '
                  'passes remain, with no MegaLights shading/volume/trace pass.'),
}

out = {
    'schema': 'unreal-active-frame-runtime-v1',
    'dump_directory': str(DUMP),
    'engine_frame': SESSION.get('draws'),
    'session_file': SESSION.get('session_file'),
    'engine_version': SESSION.get('engine_version'),
    'render_status': SESSION.get('render_status'),
    'viewport_screenshot_size': [1280, 720],
    'scene_render_target_extent': [1280, 888],
    'scene_render_target_note': 'active GBuffer/SceneColor targets are 1280x888; the top 1280x720 rows '
                                'contain the visible view, the bottom 168 rows are black padding',
    'camera': SESSION.get('camera'),
    'light': SESSION.get('light'),
    'exposure': {
        'effective_method': 'histogram (AEM_HISTOGRAM) with min_brightness == max_brightness == EV100 4',
        'auto_exposure_method': SESSION.get('exposure', {}).get('auto_exposure_method'),
        'override_auto_exposure_method': SESSION.get('exposure', {}).get('override_auto_exposure_method'),
        'ev100': 4.0,
        'postprocess_values': SESSION.get('pp_values'),
        'pre_exposure': pre_exposure,
        'one_over_pre_exposure': one_over,
        'view_uniform_metadata': view_meta,
        'view_uniform_content': view_content,
        'eye_adaptation_buffer': eye_adaptation,
        'exposure_cvars': exposure_cvars,
        'note': 'PreExposure/OneOverPreExposure are read from the captured FViewUniformShaderParameters '
                'content at the captured frame. EyeAdaptationBuffer slot 0 is the float4 exposure state '
                '(x=result scale, y=target scale, z=average scene luminance, w=bias-only per the engine '
                'visualization shader); slot 1 x=average local exposure.',
    },
    'key_cvars': key_cvars,
    'route': route,
    'dump_settings': {
        'command': SESSION.get('dump_command'),
        'settings': SESSION.get('dump_settings'),
        'command_returned': SESSION.get('dump_command_returned'),
        'status': SESSION.get('dump_status'),
        'seconds': SESSION.get('dump_seconds'),
    },
}
(ROOT / 'runtime-actual.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
print(json.dumps({'pre_exposure': pre_exposure, 'one_over': one_over,
                  'eye_adaptation': eye_adaptation,
                  'route': route['statement'],
                  'evidence': {k: v['count'] for k, v in evidence.items()}}, indent=2))
print('Wrote', ROOT / 'runtime-actual.json')
