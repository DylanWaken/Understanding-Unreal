"""Capture the active frame of the fixed /Game/Labs/LumenRoom scene.

Run inside the isolated UnrealLab project with -ExecutePythonScript=<this file>.
The map, camera, materials, RectLight (attenuation 1000 cm) and histogram
exposure with min=max EV100 4 are the already-captured reference scene.  This
script only reads the active frame; it does not re-author the room.

Modes (environment variable ARDA_DUMP_MODE):
  metadata  one unfiltered frame, descriptors + pass parameters only
  binary    one unfiltered frame, descriptors + all texture/buffer binaries
  vsm       mask/vpage/cache virtual-shadow-map visualizations, screenshots
  pt1024    1024 spp / 8 bounce / denoiser-off path-traced reference image

Every generated file is written below ARDA_BUFFERS_ROOT (buffers/).
"""
import hashlib
import json
import math
import os
import struct
import sys
import time
import traceback
import zlib
from pathlib import Path

import unreal

ROOT = Path(os.environ['ARDA_BUFFERS_ROOT']).resolve()
MODE = os.environ.get('ARDA_DUMP_MODE', 'metadata').strip().lower()
PROJECT = Path(os.environ.get('ARDA_PROJECT_ROOT',
                              str(Path(__file__).resolve().parents[1] / 'UnrealLab')))
REFERENCE_CAPTURES = Path(os.environ.get('ARDA_REFERENCE_CAPTURES',
                                         str(ROOT.parent / 'reference' / 'captures')))
LEVEL_PATH = '/Game/Labs/LumenRoom'
RESOLUTION = (1280, 720)
WARMUP_DRAWS = 200
SETTLE_DRAWS = 90
RUN_ID = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
DUMP_DIR = ROOT / 'gpudump' / f'{MODE}-{RUN_ID}'
RUN_DIR = ROOT / 'runs'
LOG_DIR = ROOT / 'logs'
for d in (DUMP_DIR.parent, RUN_DIR, LOG_DIR, ROOT / 'figures', ROOT / 'runtime', ROOT / 'dump-manifests'):
    d.mkdir(parents=True, exist_ok=True)

LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
BRIDGE = unreal.ArdaLabCapture
WORLD = None

LUMEN_COMMANDS = [
    'r.ScreenPercentage 100',
    'r.AntiAliasingMethod 2',
    'r.MotionBlurQuality 0',
    'r.Lumen.HardwareRayTracing 0',
    'r.Lumen.ScreenProbeGather.ScreenTraces 1',
    'r.Lumen.Reflections.ScreenTraces 1',
    'r.DynamicGlobalIlluminationMethod 1',
    'r.ReflectionMethod 1',
    'r.Lumen.Visualize 0',
    'r.LumenScene.SurfaceCache.AtlasSize 4096',
    'showflag.BillboardSprites 0',
    'showflag.Grid 0',
    'showflag.Selection 0',
    'showflag.SelectionOutline 0',
    'showflag.ModeWidgets 0',
    'showflag.Bounds 0',
]

KEY_CVARS = [
    'r.Substrate', 'r.Substrate.Project', 'r.Substrate.OpaqueMaterial', 'r.Substrate.Debug',
    'r.MegaLights.Enable', 'r.MegaLights.EnableForProject', 'r.MegaLights.Supported',
    'r.MegaLights.Allowed', 'r.MegaLights.Debug',
    'r.Lumen.HardwareRayTracing', 'r.Lumen.ScreenProbeGather.ScreenTraces',
    'r.Lumen.Reflections.ScreenTraces', 'r.LumenScene.SurfaceCache.AtlasSize',
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.Shadow.Virtual.Enable', 'r.Shadow.Virtual.Visualize',
    'r.Shadow.Virtual.Visualize.Layout', 'r.Shadow.Virtual.Visualize.LightName',
    'r.RayTracing', 'r.RayTracing.Shadows', 'r.RayTracing.GlobalIllumination',
    'r.PathTracing', 'r.PathTracing.SamplesPerPixel', 'r.PathTracing.MaxBounces',
    'r.PathTracing.Denoiser', 'r.PathTracing.FilterWidth', 'r.PathTracing.MISMode',
    'r.PathTracing.EnableEmissive', 'r.PathTracing.EnableReferenceDOF',
    'r.PathTracing.VisibleLights', 'r.PathTracing.ProgressDisplay',
    'r.ScreenPercentage', 'r.AntiAliasingMethod', 'r.MotionBlurQuality',
    'r.DefaultFeature.AutoExposure', 'r.DefaultFeature.AutoExposure.Method',
    'r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange',
    'r.EyeAdaptation.MethodOverride', 'r.EyeAdaptation.ExposureCompensation',
    'r.EyeAdaptation.PreExposureOverride', 'r.EyeAdaptation.PreExposure',
    'r.EyeAdaptationQuality', 'r.PreExposure', 'r.Tonemapper.Quality', 'r.Tonemapper.Sharpen',
    'r.GenerateMeshDistanceFields', 'r.AllowStaticLighting',
    'r.DumpGPU.Root', 'r.DumpGPU.Texture', 'r.DumpGPU.Buffer', 'r.DumpGPU.PassParameters',
    'r.DumpGPU.ConsoleVariables', 'r.DumpGPU.Explore', 'r.DumpGPU.Upload',
    'r.DumpGPU.Directory', 'r.DumpGPU.MaxStagingSize', 'r.DumpGPU.Stream',
    'r.DumpGPU.RedumpInputs', 'r.DumpGPU.FrameCount', 'r.DumpGPU.FrameDelay',
    'r.DumpGPU.Delay', 'r.DumpGPU.Draws', 'r.DumpGPU.Mask', 'r.DumpGPU.EnableLogWrite',
    'r.DumpGPU.Test.EnableDiskWrite', 'r.DumpGPU.Test.PrettifyResourceFileNames',
]


def atomic_json(path, data):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def vec(v):
    return [float(v.x), float(v.y), float(v.z)]


def rot(v):
    return {'pitch': float(v.pitch), 'yaw': float(v.yaw), 'roll': float(v.roll)}


def console(command):
    unreal.log('ARDA_GPUDUMP_CONSOLE ' + command)
    ok = BRIDGE.exec_console_command(command)
    if not ok:
        unreal.log_warning('ARDA_GPUDUMP_CONSOLE_RETURNED_FALSE ' + command)
    return ok


def cvar(name):
    try:
        return json.loads(BRIDGE.get_console_variable(name))
    except Exception:
        return {'name': name, 'found': False}


def cvar_value(name):
    rec = cvar(name)
    return rec.get('value') if rec.get('found') else None


def all_cvars(prefix):
    return json.loads(BRIDGE.get_console_variables(prefix))


def render_status():
    return json.loads(BRIDGE.get_render_status())


STATE = {
    'phase': 'setup',
    'busy': False,
    'done': False,
    'draws': 0,
    'dump_armed_monotonic': None,
    'dump_done_monotonic': None,
    'vsm_index': 0,
    'vsm_capture_draw': None,
    'pt_started_monotonic': None,
    'pt_first_draw': None,
    'pt_last_sample': -1,
    'pt_last_progress': 0.0,
    'pt_reset_draw': None,
}

SESSION = {
    'schema': 'unreal-active-frame-gpudump-session-v1',
    'mode': MODE,
    'run_id': RUN_ID,
    'pid': os.getpid(),
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'engine_version': unreal.SystemLibrary.get_engine_version(),
    'project': str(PROJECT),
    'map': LEVEL_PATH,
    'resolution': list(RESOLUTION),
    'lumen_commands': LUMEN_COMMANDS,
    'warmup_draws': WARMUP_DRAWS,
    'settle_draws': SETTLE_DRAWS,
    'dump_directory': None,
    'status': 'running',
    'limitations': [
        'DumpGPU captures RDG-visible resources only; resources never referenced by an RDG pass, '
        'or resources flagged SkipTracking/memoryless, are absent and are listed as unsupported.',
        'Shader-local intermediate registers and values inside shader invocations are not DumpGPU '
        'resources; shader source mapping is evidence, not a memory capture.',
        'r.DumpGPU.RedumpInputs=0 keeps the producer version of in-place-modified resources; the '
        'version linkage is recorded in Passes.json rather than forcing per-read duplicates.',
    ],
}


def progress(state, **extra):
    data = {
        'step': 'buffers',
        'state': state,
        'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'mode': MODE,
        'run_id': RUN_ID,
        'pid': os.getpid(),
        'project': str(PROJECT),
        'map': LEVEL_PATH,
        'resolution': list(RESOLUTION),
        'draws': STATE['draws'],
        'dump_directory': SESSION.get('dump_directory'),
        'session_file': SESSION.get('session_file'),
        'figures': SESSION.get('figures', []),
    }
    data.update(extra)
    atomic_json(ROOT / 'progress.json', data)


def picture_file(path, timeout=30.0):
    p = Path(path)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if p.exists() and p.stat().st_size > 1024:
            return {'file': str(p), 'bytes': p.stat().st_size, 'sha256': sha256(p),
                    'modified_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p.stat().st_mtime))}
        time.sleep(0.1)
    raise RuntimeError(f'Screenshot was not written: {p}')


def component_materials(component):
    try:
        return [component.get_material(i).get_path_name() if component.get_material(i) else None
                for i in range(component.get_num_materials())]
    except Exception:
        return []


def actor_record(actor):
    rotation = actor.get_actor_rotation()
    record = {
        'label': actor.get_actor_label(),
        'class': actor.get_class().get_name(),
        'path': actor.get_path_name(),
        'location_cm': vec(actor.get_actor_location()),
        'rotation_degrees': rot(rotation),
        'scale': vec(actor.get_actor_scale3d()),
        'components': [],
    }
    for component in actor.get_components_by_class(unreal.PrimitiveComponent):
        c = {
            'class': component.get_class().get_name(),
            'path': component.get_path_name(),
            'location_cm': vec(component.get_world_location()),
            'rotation_degrees': rot(component.get_world_rotation()),
            'scale': vec(component.get_world_scale()),
            'materials': component_materials(component),
        }
        if isinstance(component, unreal.StaticMeshComponent):
            c['mesh'] = component.static_mesh.get_path_name() if component.static_mesh else None
        record['components'].append(c)
    return record


def get_pp_settings(pp):
    settings = pp.get_editor_property('settings')
    keys = [
        'override_auto_exposure_min_brightness', 'auto_exposure_min_brightness',
        'override_auto_exposure_max_brightness', 'auto_exposure_max_brightness',
        'override_auto_exposure_bias', 'auto_exposure_bias',
        'auto_exposure_method', 'override_auto_exposure_method',
        'override_path_tracing_samples_per_pixel', 'path_tracing_samples_per_pixel',
        'override_path_tracing_max_bounces', 'path_tracing_max_bounces',
        'override_path_tracing_enable_denoiser', 'path_tracing_enable_denoiser',
    ]
    values = {}
    for key in keys:
        try:
            values[key] = str(settings.get_editor_property(key))
        except Exception:
            continue
    return settings, values


def setup():
    global WORLD
    LEVEL.load_level(LEVEL_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    if WORLD is None:
        raise RuntimeError('No editor world after loading ' + LEVEL_PATH)

    all_actors = ACTORS.get_all_level_actors()
    camera = next((a for a in all_actors if isinstance(a, unreal.CameraActor)), None)
    pp = next((a for a in all_actors if isinstance(a, unreal.PostProcessVolume)), None)
    light = next((a for a in all_actors if isinstance(a, unreal.RectLight)), None)
    if not camera or not pp or not light:
        raise RuntimeError('The saved room is missing its camera, post-process volume or RectLight.')

    location = (0.0, -400.0, 165.0)
    target = (0.0, 280.0, 140.0)
    rotation = unreal.MathLibrary.find_look_at_rotation(unreal.Vector(*location), unreal.Vector(*target))
    camera.set_actor_location(unreal.Vector(*location), False, False)
    camera.set_actor_rotation(rotation, False)
    camera.camera_component.set_field_of_view(
        math.degrees(2 * math.atan(math.tan(math.radians(43 / 2)) * 16 / 9)))
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(camera.get_actor_location(), rotation)
    ACTORS.clear_actor_selection_set()
    LEVEL.editor_set_game_view(True)
    LEVEL.pilot_level_actor(camera)

    settings, _ = get_pp_settings(pp)
    settings.set_editor_property('auto_exposure_min_brightness', 4.0)
    settings.set_editor_property('auto_exposure_max_brightness', 4.0)
    settings.set_editor_property('auto_exposure_bias', 0.0)
    settings.set_editor_property('override_auto_exposure_min_brightness', True)
    settings.set_editor_property('override_auto_exposure_max_brightness', True)
    settings.set_editor_property('override_auto_exposure_bias', True)
    pp.set_editor_property('settings', settings)
    settings, pp_values = get_pp_settings(pp)

    for command in LUMEN_COMMANDS:
        console(command)
    BRIDGE.set_viewport_mode('lit', '')

    light_component = light.get_component_by_class(unreal.RectLightComponent)
    scene = {
        'schema': 'unreal-active-frame-scene-v1',
        'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'engine_version': unreal.SystemLibrary.get_engine_version(),
        'project': str(PROJECT),
        'map': LEVEL_PATH,
        'map_sha256': sha256(PROJECT / 'Content' / 'Labs' / 'LumenRoom.umap'),
        'capture_resolution': list(RESOLUTION),
        'camera': {
            'actor': camera.get_actor_label(),
            'location_cm': vec(camera.get_actor_location()),
            'rotation_degrees': rot(camera.get_actor_rotation()),
            'target_cm': list(target),
            'vertical_fov_degrees': 43.0,
            'component_field_of_view_degrees': float(camera.camera_component.field_of_view),
        },
        'exposure': {
            'effective_method': 'histogram (AEM_HISTOGRAM) with min_brightness == max_brightness == EV100 4',
            'auto_exposure_method': pp_values.get('auto_exposure_method'),
            'override_auto_exposure_method': pp_values.get('override_auto_exposure_method'),
            'ev100': 4.0,
            'min_brightness': 4.0,
            'max_brightness': 4.0,
            'bias': 0.0,
            'postprocess_values_after_set': pp_values,
            'note': 'Not manual exposure. The unbound PostProcessVolume leaves AEM_HISTOGRAM active; '
                    'min=max=4 locks the histogram-derived exposure to EV100 4.',
        },
        'lighting': {
            'rect_light': {
                'actor': light.get_actor_label(),
                'location_cm': vec(light.get_actor_location()),
                'rotation_degrees': rot(light.get_actor_rotation()),
                'intensity_lumens': float(light_component.intensity),
                'intensity_units': str(light_component.intensity_units),
                'source_width_cm': float(light_component.source_width),
                'source_height_cm': float(light_component.source_height),
                'attenuation_radius_cm': float(light_component.attenuation_radius),
                'light_color': [float(light_component.light_color.r),
                                float(light_component.light_color.g),
                                float(light_component.light_color.b)],
            },
            'lumen': 'settings applied by LUMEN_COMMANDS; actual route recorded from runtime CVars',
        },
        'lumen_commands': LUMEN_COMMANDS,
        'actors': [actor_record(a) for a in all_actors],
    }
    atomic_json(ROOT / 'scene.json', scene)
    return camera, pp, light, light_component, scene, pp_values


def runtime_record(light_label, light_component, pp_values):
    key = {name: cvar(name) for name in KEY_CVARS}
    record = {
        'schema': 'unreal-active-frame-runtime-v1',
        'mode': MODE,
        'run_id': RUN_ID,
        'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'pid': os.getpid(),
        'engine_version': unreal.SystemLibrary.get_engine_version(),
        'render_status': render_status(),
        'key_cvars': key,
        'postprocess_values': pp_values,
        'path_tracing_status': json.loads(BRIDGE.get_path_tracing_status()),
        'light_name': light_label,
        'light_attenuation_radius_cm': float(light_component.attenuation_radius),
        'all_r_cvars': all_cvars('r.'),
        'all_showflag_cvars': all_cvars('showflag.'),
    }
    atomic_json(ROOT / 'runtime' / f'{MODE}-{RUN_ID}.json', record)
    atomic_json(ROOT / 'runtime-cvars-r.json', record['all_r_cvars'])
    atomic_json(ROOT / 'runtime-cvars-showflag.json', record['all_showflag_cvars'])
    return record


def set_dump_cvars(texture, buffer_, root='*', pass_parameters=1, console_variables=1,
                   max_staging=64, stream=0, frame_count=1):
    values = [
        ('r.DumpGPU.Root', root),
        ('r.DumpGPU.Texture', str(texture)),
        ('r.DumpGPU.Buffer', str(buffer_)),
        ('r.DumpGPU.PassParameters', str(pass_parameters)),
        ('r.DumpGPU.ConsoleVariables', str(console_variables)),
        ('r.DumpGPU.Explore', '0'),
        ('r.DumpGPU.Upload', '0'),
        ('r.DumpGPU.Directory', str(DUMP_DIR.parent)),
        ('r.DumpGPU.MaxStagingSize', str(max_staging)),
        ('r.DumpGPU.Stream', str(stream)),
        ('r.DumpGPU.RedumpInputs', '0'),
        ('r.DumpGPU.FrameCount', str(frame_count)),
        ('r.DumpGPU.FrameDelay', '0'),
        ('r.DumpGPU.Delay', '0'),
        ('r.DumpGPU.Draws', '0'),
        ('r.DumpGPU.Mask', '1'),
        ('r.DumpGPU.EnableLogWrite', '1'),
        ('r.DumpGPU.Test.EnableDiskWrite', '1'),
        ('r.DumpGPU.Test.PrettifyResourceFileNames', '0'),
    ]
    applied = {}
    for name, value in values:
        console(f'{name} {value}')
        applied[name] = {'requested': value, 'actual': cvar(name)}
    return applied


def prepare_dump():
    texture = 1 if MODE == 'metadata' else 2
    buffer_ = 1 if MODE == 'metadata' else 2
    max_staging = 64 if MODE == 'metadata' else 256
    applied = set_dump_cvars(texture=texture, buffer_=buffer_, max_staging=max_staging)
    command = f'DumpGPU -path="{DUMP_DIR}" -noexplorer -oneframe'
    SESSION['dump_directory'] = str(DUMP_DIR)
    SESSION['dump_command'] = command
    SESSION['dump_cvars'] = applied
    SESSION['dump_settings'] = {
        'mode': MODE,
        'Texture': texture,
        'Buffer': buffer_,
        'PassParameters': 1,
        'ConsoleVariables': 1,
        'Root': '*',
        'Explore': 0,
        'Upload': 0,
        'MaxStagingSize': max_staging,
        'Stream': 0,
        'RedumpInputs': 0,
        'FrameCount': 1,
        'frame_index': None,
        'warmup_draws': WARMUP_DRAWS,
        'settle_draws': SETTLE_DRAWS,
        'resolution': list(RESOLUTION),
    }
    progress('dump_arming', dump_directory=str(DUMP_DIR), dump_command=command,
             dump_mode=MODE, dump_texture=texture, dump_buffer=buffer_)
    ok = console(command)
    SESSION['dump_command_returned'] = ok
    STATE['dump_armed_monotonic'] = time.monotonic()
    STATE['phase'] = 'dump_wait'
    progress('dump_armed', dump_directory=str(DUMP_DIR), dump_command=command,
             dump_mode=MODE, dump_texture=texture, dump_buffer=buffer_)


def dump_status():
    status_file = DUMP_DIR / 'Base' / 'Status.txt'
    if not status_file.exists():
        return None
    try:
        return status_file.read_text(encoding='utf-8', errors='replace').strip()
    except Exception:
        return None


def dump_inventory():
    files = []
    total = 0
    for p in sorted(DUMP_DIR.rglob('*')):
        if p.is_file():
            size = p.stat().st_size
            total += size
            files.append({'path': str(p.relative_to(DUMP_DIR)).replace('\\', '/'), 'bytes': size})
    manifest = {
        'schema': 'unreal-gpudump-file-manifest-v1',
        'mode': MODE,
        'run_id': RUN_ID,
        'dump_directory': str(DUMP_DIR),
        'dump_command': SESSION.get('dump_command'),
        'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'file_count': len(files),
        'total_bytes': total,
        'files': files,
    }
    manifest_path = ROOT / 'dump-manifests' / f'{MODE}-{RUN_ID}.json'
    atomic_json(manifest_path, manifest)
    SESSION['dump_manifest'] = str(manifest_path)
    SESSION['dump_file_count'] = len(files)
    SESSION['dump_total_bytes'] = total
    return manifest


def decode_png(path):
    """Decode an 8-bit non-interlaced PNG to (width, height, channels, rows)."""
    data = Path(path).read_bytes()
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('not a PNG')
    pos = 8
    width = height = None
    color_type = None
    idat = []
    while pos < len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b'IHDR':
            width, height, bit_depth, color_type, _c, _f, interlace = struct.unpack('>IIBBBBB', payload)
            if bit_depth != 8 or interlace != 0:
                raise ValueError('unsupported PNG')
        elif kind == b'IDAT':
            idat.append(payload)
        elif kind == b'IEND':
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(b''.join(idat))
    stride = width * channels
    prev = bytearray(stride)
    p = 0
    rows = []
    for _y in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ftype != 0:
            raise ValueError('unknown PNG filter')
        prev = line
        rows.append(bytes(line))
    return width, height, channels, rows


def collect_png_stats(path, expected_size=RESOLUTION):
    width, height, channels, rows = decode_png(path)
    lums = []
    colors = set()
    nonblack = 0
    for line in rows:
        for x in range(width):
            i = x * channels
            r, g, b = line[i], line[i + 1], line[i + 2]
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            lums.append(l)
            colors.add((r, g, b))
            if l > 8:
                nonblack += 1
    mean = sum(lums) / len(lums)
    std = (sum((v - mean) ** 2 for v in lums) / len(lums)) ** 0.5
    return {
        'file': str(path),
        'width': width,
        'height': height,
        'channels': channels,
        'mean_luminance': mean,
        'std_luminance': std,
        'min_luminance': min(lums),
        'max_luminance': max(lums),
        'unique_rgb_colors': len(colors),
        'nonblack_fraction_lum_gt_8': nonblack / len(lums),
        'size_ok': [width, height] == list(expected_size),
        # A shadow-mask view is legitimately two-tone, so color count alone is
        # not a no-op test; a different-image comparison is done separately.
        'blank_rejected': not (std > 3.0 and len(colors) > 32 and nonblack / len(lums) > 0.02),
    }


def compare_pngs(path_a, path_b, step=4):
    """Mean absolute RGB difference and fraction of differing pixels (sampled)."""
    wa, ha, ca, ra = decode_png(path_a)
    wb, hb, cb, rb = decode_png(path_b)
    if (wa, ha) != (wb, hb):
        return {'error': 'size mismatch', 'mean_abs_rgb_difference': None}
    total = 0
    diff_sum = 0.0
    big = 0
    for y in range(0, ha, step):
        la = ra[y]
        lb = rb[y]
        for x in range(0, wa, step):
            i = x * ca
            j = x * cb
            d = (abs(la[i] - lb[j]) + abs(la[i + 1] - lb[j + 1]) + abs(la[i + 2] - lb[j + 2])) / 3.0
            diff_sum += d
            if d > 8:
                big += 1
            total += 1
    return {
        'mean_abs_rgb_difference_0_255': diff_sum / total,
        'fraction_sampled_pixels_rgb_difference_gt_8': big / total,
        'sample_step': step,
    }


def capture_vsm_baseline():
    image = ROOT / 'figures' / 'vsm-lit-baseline.png'
    if image.exists():
        image.unlink()
    BRIDGE.set_viewport_mode('lit', '')
    BRIDGE.capture_viewport(str(image))
    STATE['draws'] += 1
    info = picture_file(image)
    stats = collect_png_stats(image)
    info['stats'] = stats
    SESSION['vsm_baseline'] = info
    SESSION['figures'] = SESSION.get('figures', []) + [info]
    if stats.get('blank_rejected') or not stats.get('size_ok'):
        raise RuntimeError(f'VSM baseline lit image rejected: {json.dumps(stats)}')
    unreal.log(f'ARDA_GPUDUMP_VSM_BASELINE sha256={info["sha256"]}')
    progress('vsm_baseline', image=info)


def capture_vsm(light_label):
    modes = ['mask', 'vpage', 'cache']
    mode = modes[STATE['vsm_index']]
    STATE['vsm_capture_draw'] = STATE['draws']
    state_after = render_status()
    vsm_cvar = cvar('r.Shadow.Virtual.Visualize')
    image = ROOT / 'figures' / f'vsm-{mode}.png'
    if image.exists():
        image.unlink()
    BRIDGE.capture_viewport(str(image))
    STATE['draws'] += 1
    info = picture_file(image)
    try:
        stats = collect_png_stats(image)
    except Exception as exc:
        stats = {'error': str(exc), 'blank_rejected': True, 'size_ok': False}
    baseline = SESSION.get('vsm_baseline') or {}
    comparison = None
    if baseline.get('file'):
        try:
            comparison = compare_pngs(image, baseline['file'])
        except Exception as exc:
            comparison = {'error': str(exc)}
    info['stats'] = stats
    info['vsm_mode'] = mode
    info['view_mode_after'] = state_after
    info['cvar'] = vsm_cvar
    info['capture_draw'] = STATE['vsm_capture_draw']
    info['comparison_to_lit_baseline'] = comparison
    SESSION.setdefault('figures', []).append(info)
    SESSION.setdefault('vsm', []).append({
        'mode': mode,
        'image': info,
        'render_status_after': state_after,
        'visualize_cvar': vsm_cvar,
        'layout': cvar('r.Shadow.Virtual.Visualize.Layout'),
        'light_name_cvar': cvar('r.Shadow.Virtual.Visualize.LightName'),
        'showflags': {
            'VisualizeVirtualShadowMap': cvar_value('showflag.VisualizeVirtualShadowMap'),
        },
        'comparison_to_lit_baseline': comparison,
        'exact_commands': [
            'r.Shadow.Virtual.Visualize.Layout 0',
            f'r.Shadow.Virtual.Visualize.LightName "{light_label}"',
            f'Viewport view mode VMI_VisualizeVirtualShadowMap, mode {mode}',
        ],
    })
    if stats.get('blank_rejected') or not stats.get('size_ok'):
        raise RuntimeError(f'VSM {mode} view rejected by numerical check: {json.dumps(stats)}')
    if not comparison or comparison.get('mean_abs_rgb_difference_0_255') is None:
        raise RuntimeError(f'VSM {mode} view has no valid baseline comparison: {json.dumps(comparison)}')
    if comparison['mean_abs_rgb_difference_0_255'] < 1.0:
        raise RuntimeError(f'VSM {mode} view is a no-op versus lit baseline: {json.dumps(comparison)}')
    unreal.log(f'ARDA_GPUDUMP_VSM_CAPTURED mode={mode} draw={STATE["draws"]} sha256={info["sha256"]}')
    progress('vsm_captured', vsm_mode=mode, image=info)
    STATE['vsm_index'] += 1
    if STATE['vsm_index'] >= len(modes):
        console('r.Shadow.Virtual.Visualize off')
        finish_ok()


def start_pt1024():
    commands = [
        'r.PathTracing.SamplesPerPixel 1024',
        'r.PathTracing.MaxBounces 8',
        'r.PathTracing.Denoiser 0',
        'r.PathTracing.ProgressDisplay 0',
        'r.PathTracing.FilterWidth 3.0',
        'r.PathTracing.MISMode 2',
        'r.PathTracing.EnableEmissive 1',
        'r.PathTracing.EnableReferenceDOF 0',
        'r.PathTracing.VisibleLights 0',
    ]
    for command in commands:
        console(command)
    BRIDGE.set_viewport_mode('pathtracing', '')
    SESSION['path_tracer_commands'] = commands
    SESSION['pt_target_spp'] = 1024
    SESSION['pt_target_bounces'] = 8
    SESSION['pt_denoiser'] = 0
    STATE['phase'] = 'pt_warmup'
    STATE['pt_warmup_start_draw'] = STATE['draws']
    progress('path_tracing_warmup', draws=STATE['draws'])


def capture_pt1024():
    image = REFERENCE_CAPTURES / 'pathtracer-1024spp-8bounces.png'
    image.parent.mkdir(parents=True, exist_ok=True)
    if image.exists():
        image.unlink()
    samples_before = BRIDGE.get_path_tracing_samples()
    count = BRIDGE.get_path_tracing_sample_count()
    started = time.monotonic()
    BRIDGE.capture_viewport(str(image))
    STATE['draws'] += 1
    samples_after = BRIDGE.get_path_tracing_samples()
    info = picture_file(image)
    try:
        stats = collect_png_stats(image)
    except Exception as exc:
        stats = {'error': str(exc), 'blank_rejected': True, 'size_ok': False}
    info['stats'] = stats
    SESSION['pt_result'] = {
        'image': info,
        'sample_index_before_screenshot': samples_before,
        'sample_index_after_screenshot': samples_after,
        'sample_count_reported': count,
        'requested_samples_per_pixel': 1024,
        'max_bounces': 8,
        'denoiser': 0,
        'capture_draw': STATE['draws'],
        'first_accumulation_draw': STATE['pt_first_draw'],
        'reset_draw': STATE['pt_reset_draw'],
        'status_at_capture': json.loads(BRIDGE.get_path_tracing_status()),
    }
    if samples_before < 1024 or count != 1024:
        raise RuntimeError(f'PT sample mismatch before screenshot: {samples_before}/{count}')
    if stats.get('blank_rejected') or not stats.get('size_ok'):
        raise RuntimeError(f'PT 1024 image rejected by numerical check: {json.dumps(stats)}')
    SESSION['figures'] = [info]
    finish_ok()


def collect_session():
    SESSION['draws'] = STATE['draws']
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = time.monotonic() - SESSION['started_monotonic']
    SESSION['dump_status'] = dump_status()
    if DUMP_DIR.exists():
        SESSION['dump_inventory'] = dump_inventory()
    SESSION['status'] = 'ok'
    path = ROOT / 'runs' / f'{MODE}-{RUN_ID}.json'
    atomic_json(path, SESSION)
    SESSION['session_file'] = str(path)
    progress('complete', dump_status=SESSION.get('dump_status'),
             dump_bytes=SESSION.get('dump_total_bytes'),
             dump_file_count=SESSION.get('dump_file_count'),
             figures=SESSION.get('figures', []))


def finish_ok():
    try:
        collect_session()
    except Exception:
        SESSION['status'] = 'error'
        SESSION['error'] = traceback.format_exc()
        atomic_json(ROOT / 'runs' / f'{MODE}-{RUN_ID}.json', SESSION)
        raise
    unreal.log(f'ARDA_GPUDUMP_COMPLETE mode={MODE} draws={STATE["draws"]} '
               f'dir={SESSION.get("dump_directory")} bytes={SESSION.get("dump_total_bytes")}')
    unreal.unregister_slate_post_tick_callback(HANDLE)
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


def finish_error(message):
    SESSION['status'] = 'error'
    SESSION['error'] = message
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = time.monotonic() - SESSION['started_monotonic']
    SESSION['draws'] = STATE['draws']
    atomic_json(ROOT / 'runs' / f'{MODE}-{RUN_ID}.json', SESSION)
    (LOG_DIR / f'{MODE}-{RUN_ID}-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_GPUDUMP_ERROR ' + message)
    progress('error', error=message)
    try:
        unreal.unregister_slate_post_tick_callback(HANDLE)
    except Exception:
        pass
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        STATE['draws'] += 1
        BRIDGE.render_viewport()

        if STATE['phase'] == 'warmup':
            if STATE['draws'] < WARMUP_DRAWS + SETTLE_DRAWS:
                if STATE['draws'] % 25 == 0:
                    progress('warmup', draws=STATE['draws'])
                return
            runtime = runtime_record(SESSION['light_label'], LIGHT_COMPONENT, PP_VALUES)
            SESSION['runtime_file'] = str(ROOT / 'runtime' / f'{MODE}-{RUN_ID}.json')
            SESSION['runtime_key_cvars'] = runtime['key_cvars']
            SESSION['render_status'] = runtime['render_status']
            unreal.log('ARDA_GPUDUMP_RUNTIME ' + json.dumps(runtime['render_status']))
            progress('warmed', draws=STATE['draws'], runtime_file=SESSION['runtime_file'])
            if MODE in ('metadata', 'binary'):
                prepare_dump()
            elif MODE == 'vsm':
                if 'vsm_baseline' not in SESSION:
                    capture_vsm_baseline()
                STATE['phase'] = 'vsm'
            elif MODE == 'pt1024':
                start_pt1024()
            else:
                raise RuntimeError('Unknown ARDA_DUMP_MODE=' + MODE)
            return

        if STATE['phase'] == 'dump_wait':
            status = dump_status()
            if status == 'ok':
                STATE['dump_done_monotonic'] = time.monotonic()
                SESSION['dump_completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                SESSION['dump_seconds'] = STATE['dump_done_monotonic'] - STATE['dump_armed_monotonic']
                finish_ok()
                return
            elapsed = time.monotonic() - STATE['dump_armed_monotonic']
            timeout = 1800 if MODE == 'metadata' else 14400
            if elapsed > timeout:
                raise RuntimeError(
                    f'DumpGPU did not reach status ok in {timeout}s (status={status}, dir={DUMP_DIR})')
            if int(elapsed) % 30 == 0:
                progress('dump_waiting', dump_status=status, elapsed_seconds=elapsed,
                         dump_directory=str(DUMP_DIR))
            return

        if STATE['phase'] == 'vsm':
            modes = ['mask', 'vpage', 'cache']
            mode = modes[STATE['vsm_index']]
            if STATE.get('vsm_active_mode') != mode:
                console('r.Shadow.Virtual.Visualize.Layout 0')
                console(f'r.Shadow.Virtual.Visualize.LightName "{SESSION["light_label"]}"')
                BRIDGE.set_viewport_mode('vsm', mode)
                STATE['vsm_active_mode'] = mode
                STATE['vsm_settle'] = 0
                return
            # Let each visualization mode settle for several draws before capture.
            STATE['vsm_settle'] = STATE.get('vsm_settle', 0) + 1
            if STATE['vsm_settle'] < 8:
                return
            STATE['vsm_settle'] = 0
            capture_vsm(SESSION['light_label'])
            return

        if STATE['phase'] == 'pt_warmup':
            if STATE['draws'] - STATE['pt_warmup_start_draw'] < 64:
                return
            SESSION['pt_warmup_status'] = json.loads(BRIDGE.get_path_tracing_status())
            console('r.PathTracing.SamplesPerPixel 1023')
            STATE['phase'] = 'pt_reset_low'
            return

        if STATE['phase'] == 'pt_reset_low':
            console('r.PathTracing.SamplesPerPixel 1024')
            STATE['phase'] = 'pt_reset_high'
            return

        if STATE['phase'] == 'pt_reset_high':
            STATE['pt_reset_draw'] = STATE['draws']
            STATE['pt_started_monotonic'] = time.monotonic()
            SESSION['pt_sample_after_reset'] = BRIDGE.get_path_tracing_samples()
            SESSION['pt_sample_count_after_reset'] = BRIDGE.get_path_tracing_sample_count()
            STATE['phase'] = 'pt_accumulate'
            progress('path_tracing_reset', draws=STATE['draws'],
                     samples=BRIDGE.get_path_tracing_samples(),
                     sample_count=BRIDGE.get_path_tracing_sample_count())
            return

        if STATE['phase'] == 'pt_accumulate':
            status_obj = json.loads(BRIDGE.get_path_tracing_status())
            samples = BRIDGE.get_path_tracing_samples()
            count = BRIDGE.get_path_tracing_sample_count()
            if samples > 0 and STATE['pt_first_draw'] is None:
                STATE['pt_first_draw'] = STATE['draws']
            if samples != STATE['pt_last_sample']:
                STATE['pt_last_sample'] = samples
                now = time.monotonic()
                if now - STATE['pt_last_progress'] >= 2.0 or samples >= 1024:
                    STATE['pt_last_progress'] = now
                    progress('path_tracing', samples=samples, sample_count=count,
                             elapsed_seconds=now - STATE['pt_started_monotonic'])
                    unreal.log(f'ARDA_GPUDUMP_PT_SAMPLE {samples}/{count} draw={STATE["draws"]}')
            if count > 0 and count != 1024:
                raise RuntimeError(f'Path tracer reports sample count {count}, expected 1024.')
            if samples >= 1024:
                capture_pt1024()
                return
            if time.monotonic() - STATE['pt_started_monotonic'] > 1800:
                raise RuntimeError(f'Path tracer did not reach 1024 spp (samples={samples}/{count}).')
            return

        raise RuntimeError('Unhandled phase ' + str(STATE['phase']))
    except Exception:
        finish_error(traceback.format_exc())
    finally:
        STATE['busy'] = False


try:
    CAMERA, PP, LIGHT, LIGHT_COMPONENT, SCENE, PP_VALUES = setup()
    SESSION['camera'] = SCENE['camera']
    SESSION['exposure'] = SCENE['exposure']
    SESSION['light'] = SCENE['lighting']['rect_light']
    SESSION['light_label'] = LIGHT.get_actor_label()
    SESSION['pp_values'] = PP_VALUES
    SESSION['scene_file'] = str(ROOT / 'scene.json')
    SESSION['phase'] = 'warmup'
    STATE['phase'] = 'warmup'
    progress('loading')
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    unreal.log(f'ARDA_GPUDUMP_SETUP_COMPLETE mode={MODE} run_id={RUN_ID} dump_dir={DUMP_DIR}')
except Exception:
    message = traceback.format_exc()
    (LOG_DIR / f'{MODE}-{RUN_ID}-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_GPUDUMP_SETUP_ERROR ' + message)
    progress('error', error=message)
