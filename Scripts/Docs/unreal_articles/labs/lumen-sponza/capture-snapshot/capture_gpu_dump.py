"""Capture one complete native GPU frame of the approved Sponza hero view.

This is the GPU-dump step, not an image-comparison or packaging step.  It:
  * loads the approved saved SponzaTeaching map without saving it;
  * pilots the *saved* CAM_Hero_Reference at the approved transform/FOV;
  * applies the approved HW-Lumen lit/clean display state and EV100 11;
  * warms the viewport so Lumen/VSM/TSR histories are populated;
  * arms exactly one unfiltered DumpGPU frame (all textures, all buffers,
    pass parameters, console variables, native uniform-buffer textures,
    SkipTracking buffers, and RedumpInputs=1 for in-place versions);
  * records the exact commands, actual CVar values, raw directory, file
    inventory bytes, draw/frame invocations, and engine/module hashes.

Nothing is written under D:/UnrealEngine or the original capture package.  The
map is not saved; its SHA-256 is checked before and after.  Only the owned
UnrealEditor process is left to exit by the launcher.

Run inside the project with:
  UnrealEditor.exe <project> -ExecutePythonScript=<this file>
The launcher scripts/run_gpu_dump.ps1 supplies the startup
r.RHI.UniformBufferContentMap.Enable=1 ini override.
"""
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

import unreal

WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', r'D:\UnrealUnderstanding-Sponza'))
ENGINE_ROOT = Path(os.environ.get('ARDA_ENGINE_ROOT', r'D:\UnrealEngine'))
SCRIPTS = WORKSPACE / 'scripts'
if str(SCRIPTS) not in __import__('sys').path:
    __import__('sys').path.insert(0, str(SCRIPTS))
from image_stats import Png  # noqa: E402

OUTPUT = WORKSPACE / 'output'
GPU = OUTPUT / 'gpu-dump'
LOGS = GPU / 'logs'
PRE = GPU / 'pre'
MAP_PATH = '/Game/Labs/SponzaTeaching'
MAP_FILE = WORKSPACE / 'project' / 'UnrealLab' / 'Content' / 'Labs' / 'SponzaTeaching.umap'
SETUP_PATH = OUTPUT / 'final-setup.json'
PROJECT_FILE = WORKSPACE / 'project' / 'UnrealLab' / 'UnrealLab.uproject'
BRIDGE_DLL = WORKSPACE / 'project' / 'UnrealLab' / 'Binaries' / 'Win64' / 'UnrealEditor-ArdaLabCapture.dll'
PROJECT_MODULES = WORKSPACE / 'project' / 'UnrealLab' / 'Binaries' / 'Win64' / 'UnrealEditor.modules'
EDITOR_EXE = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.exe'
ENGINE_VERSION = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.version'
ENGINE_MODULES = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.modules'
BUILD_VERSION = ENGINE_ROOT / 'Engine' / 'Build' / 'Build.version'

ENGINE_ROOT_STR = str(ENGINE_ROOT.resolve())
DECLARED_SOURCE_BASE = '71fe36aac5a8df5ccd66c763ffc902b29b6a9c43'
DECLARED_BRIDGE_BUILD_ID = 'b4effda9-1b7d-4e62-b9a6-cb99e41a70c9'
RESOLUTION = (1280, 720)
WARMUP_DRAWS = 220
SETTLE_DRAWS = 80
DUMP_TIMEOUT_SECONDS = 4 * 60 * 60
BUDGET_BYTES = 100 * 1024 ** 3

LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
BRIDGE = unreal.ArdaLabCapture
WORLD = None
HANDLE = None
HERO = None
PP = None
SETUP = None

RUN_ID = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
RAW_DIR = GPU / 'raw' / f'sponza-{RUN_ID}'
SESSION_PATH = GPU / f'run-{RUN_ID}.json'
LATEST_PATH = GPU / 'latest-run.json'
ERROR_PATH = LOGS / f'capture-{RUN_ID}-error.txt'

CLEAN_COMMANDS = [
    'r.ScreenPercentage 100',
    'r.AntiAliasingMethod 2',
    'r.MotionBlurQuality 0',
    'r.BloomQuality 0',
    'r.DepthOfFieldQuality 0',
    'r.VolumetricFog 0',
    'r.DynamicGlobalIlluminationMethod 1',
    'r.ReflectionMethod 1',
    'r.Lumen.HardwareRayTracing 1',
    'r.LumenScene.FarField 0',
    'r.LumenScene.SurfaceCache.AtlasSize 4096',
    'r.Lumen.DiffuseIndirect.Allow 1',
    'r.Lumen.ScreenProbeGather 1',
    'r.Lumen.ScreenProbeGather.ScreenTraces 1',
    'r.Lumen.Reflections.ScreenTraces 1',
    'r.Lumen.Reflections.MaxRoughnessToTrace 0.4',
    'r.Lumen.Reflections.MaxRoughnessToTraceClamp 1.0',
    'r.Lumen.HardwareRayTracing.LightingMode 0',
    'r.Lumen.HardwareRayTracing.HitLighting.Allowed 1',
    'r.Lumen.Visualize 0',
    'r.Lumen.Visualize.CardPlacement 0',
    'r.Lumen.ScreenProbeGather.Debug 0',
    'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 0',
    'r.Lumen.ScreenProbeGather.VisualizeTraces 0',
    'r.ShaderPrint 0',
    'r.DistanceFields.Visualize.Monochrome 1',
    'showflag.VisualizeMeshDistanceFields 0',
    'showflag.VisualizeGlobalDistanceField 0',
    'showflag.BillboardSprites 0',
    'showflag.Grid 0',
    'showflag.Selection 0',
    'showflag.SelectionOutline 0',
    'showflag.ModeWidgets 0',
    'showflag.Bounds 0',
    'ShowFlag.Cameras 0',
    'ShowFlag.CameraFrustums 0',
    'ShowFlag.CompositeEditorPrimitives 0',
    'ShowFlag.Game 1',
    'ShowFlag.LumenGlobalTraces 1',
    'ShowFlag.LumenDetailTraces 1',
]

DUMP_CVARS = [
    ('r.DumpGPU.Root', '*'),
    ('r.DumpGPU.Texture', '2'),
    ('r.DumpGPU.Buffer', '2'),
    ('r.DumpGPU.PassParameters', '1'),
    ('r.DumpGPU.ConsoleVariables', '1'),
    ('r.DumpGPU.Explore', '0'),
    ('r.DumpGPU.Upload', '0'),
    ('r.DumpGPU.Directory', str((GPU / 'raw').resolve())),
    ('r.DumpGPU.MaxStagingSize', '1024'),
    ('r.DumpGPU.Stream', '0'),
    ('r.DumpGPU.RedumpInputs', '1'),
    ('r.DumpGPU.FrameCount', '1'),
    ('r.DumpGPU.FrameDelay', '0'),
    ('r.DumpGPU.Delay', '0'),
    ('r.DumpGPU.Draws', '0'),
    ('r.DumpGPU.Mask', '1'),
    ('r.DumpGPU.EnableLogWrite', '1'),
    ('r.DumpGPU.Test.EnableDiskWrite', '1'),
    ('r.DumpGPU.Test.PrettifyResourceFileNames', '0'),
    ('r.DumpGPU.NativeUniformBufferTextures', '1'),
    ('r.DumpGPU.Buffer.SkipTracking', '1'),
    ('r.RHI.UniformBufferContentMap.Enable', '1'),
]

HASH_FILES = [
    EDITOR_EXE,
    ENGINE_VERSION,
    ENGINE_MODULES,
    BUILD_VERSION,
    ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-RenderCore.dll',
    ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-Renderer.dll',
    ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-RHI.dll',
    ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-D3D12RHI.dll',
    ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-Engine.dll',
    ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-UnrealEd.dll',
    BRIDGE_DLL,
    PROJECT_MODULES,
    PROJECT_FILE,
    MAP_FILE,
    SETUP_PATH,
    Path(__file__).resolve(),
]

SESSION = {
    'schema': 'arda-sponza-gpu-dump-session-v1',
    'status': 'running',
    'run_id': RUN_ID,
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'declared_source_base': DECLARED_SOURCE_BASE,
    'declared_bridge_build_id': DECLARED_BRIDGE_BUILD_ID,
    'engine_root': ENGINE_ROOT_STR,
    'project': str(PROJECT_FILE),
    'map': MAP_PATH,
    'map_file': str(MAP_FILE),
    'resolution': list(RESOLUTION),
    'raw_directory': str(RAW_DIR),
    'warmup_draws_requested': WARMUP_DRAWS,
    'settle_draws_requested': SETTLE_DRAWS,
    'dump_settings_requested': {name: value for name, value in DUMP_CVARS},
    'dump_command': f'DumpGPU -path="{RAW_DIR}" -noexplorer -oneframe',
    'limitations': [
        'DumpGPU captures RDG-visible resources only. Resources never referenced by an RDG pass, '
        'memoryless resources, writable SkipTracking resources, and shader-local registers are not '
        'part of the raw frame; the capture summary records explicit absent/unsupported branches.',
        'The bridge can issue several scene graphs per engine frame. The actual camera is identified '
        'from the dumped ViewData.PrimaryViews matrices and ViewRect, not from the bridge call count.',
        'Shader-local choices inside a pixel/thread are not DumpGPU resources; they are recorded as '
        'gaps, never as invented parameter values.',
    ],
}
STATE = {
    'phase': 'setup',
    'draws': 0,
    'warmup_start': None,
    'dump_armed': None,
    'dump_trigger_draws': 0,
    'dump_retriggers': 0,
    'dump_last_retrigger': None,
    'dump_done': None,
    'busy': False,
    'done': False,
    'over_budget': False,
}
COMMANDS = []
PROGRESS = {'last_write': 0.0}


def log(message):
    unreal.log('ARDA_GPU_DUMP ' + str(message))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 22), b''):
            h.update(chunk)
    return h.hexdigest()


def vec(v):
    return [round(float(v.x), 4), round(float(v.y), 4), round(float(v.z), 4)]


def rot(v):
    return {'pitch': round(float(v.pitch), 5), 'yaw': round(float(v.yaw), 5),
            'roll': round(float(v.roll), 5)}


def console(command):
    started = time.monotonic()
    try:
        ok = bool(unreal.SystemLibrary.execute_console_command(WORLD, command))
    except Exception as exc:
        ok = False
        log('console exception %s: %s' % (command, exc))
    COMMANDS.append({'command': command, 'returned': ok,
                     'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                     'draw': STATE['draws'], 'seconds': round(time.monotonic() - started, 6)})
    if not ok:
        log('console returned false: ' + command)
    return ok


def cvar(name):
    try:
        return json.loads(BRIDGE.get_console_variable(name))
    except Exception as exc:
        return {'name': name, 'found': False, 'error': str(exc)}


def cvar_records(names):
    return {name: cvar(name) for name in names}


def render_status():
    return json.loads(BRIDGE.get_render_status())


def pilot(camera):
    ACTORS.clear_actor_selection_set()
    LEVEL.editor_set_game_view(True)
    LEVEL.set_allows_cinematic_control(True)
    LEVEL.set_exact_camera_view(True)
    LEVEL.pilot_level_actor(camera)


def view_fov():
    try:
        return float(LEVEL.get_level_viewport_fov(LEVEL.get_active_viewport_config_key()))
    except Exception as exc:
        return {'error': str(exc)}


def pilot_readback(camera):
    rec = {
        'requested_config_key': str(LEVEL.get_active_viewport_config_key()),
        'pilot_actor': None,
        'pilot_matches': None,
        'actual_view_fov': view_fov(),
    }
    try:
        actor = LEVEL.get_pilot_level_actor()
        rec['pilot_actor'] = actor.get_actor_label() if actor else None
        rec['pilot_matches'] = bool(actor == camera)
    except Exception as exc:
        rec['pilot_error'] = str(exc)
    try:
        rec['exact_camera_view'] = bool(LEVEL.get_exact_camera_view())
        rec['allows_cinematic_control'] = bool(LEVEL.get_allows_cinematic_control())
    except Exception as exc:
        rec['lock_readback_error'] = str(exc)
    try:
        loc, rotation = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
        rec['viewport_camera'] = {'location_cm': vec(loc), 'rotation': rot(rotation)}
    except Exception as exc:
        rec['viewport_camera_error'] = str(exc)
    rec['render_status'] = render_status()
    return rec


def actor_readback(camera):
    return {
        'actor': camera.get_actor_label(),
        'location_cm': vec(camera.get_actor_location()),
        'rotation_degrees': rot(camera.get_actor_rotation()),
        'horizontal_fov_deg': round(float(camera.camera_component.field_of_view), 6),
    }


def set_exposure(pp, ev100, bias=0.0):
    settings = pp.get_editor_property('settings')
    settings.set_editor_property('auto_exposure_min_brightness', float(ev100))
    settings.set_editor_property('auto_exposure_max_brightness', float(ev100))
    settings.set_editor_property('auto_exposure_bias', float(bias))
    settings.set_editor_property('override_auto_exposure_min_brightness', True)
    settings.set_editor_property('override_auto_exposure_max_brightness', True)
    settings.set_editor_property('override_auto_exposure_bias', True)
    try:
        settings.set_editor_property('auto_exposure_method', unreal.AutoExposureMethod.AEM_HISTOGRAM)
        settings.set_editor_property('override_auto_exposure_method', True)
    except Exception:
        pass
    pp.set_editor_property('settings', settings)


def read_exposure(pp):
    settings = pp.get_editor_property('settings')
    try:
        return {
            'auto_exposure_min_brightness': float(settings.get_editor_property('auto_exposure_min_brightness')),
            'auto_exposure_max_brightness': float(settings.get_editor_property('auto_exposure_max_brightness')),
            'auto_exposure_bias': float(settings.get_editor_property('auto_exposure_bias')),
            'override_auto_exposure_min_brightness': bool(settings.get_editor_property('override_auto_exposure_min_brightness')),
            'override_auto_exposure_max_brightness': bool(settings.get_editor_property('override_auto_exposure_max_brightness')),
            'override_auto_exposure_bias': bool(settings.get_editor_property('override_auto_exposure_bias')),
            'auto_exposure_method': str(settings.get_editor_property('auto_exposure_method')),
        }
    except Exception as exc:
        return {'error': str(exc)}


def pixel_metrics(path):
    png = Png(path)
    step = 2
    total = black = white = 0
    lum_sum = lum_sq = 0.0
    for y in range(0, png.height, step):
        row = png.rows[y]
        for x in range(0, png.width, step):
            i = x * png.channels
            r, g, b = row[i], row[i + 1], row[i + 2]
            lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
            total += 1
            lum_sum += lum
            lum_sq += lum * lum
            if lum <= 2:
                black += 1
            if lum >= 253:
                white += 1
    mean = lum_sum / total if total else 0.0
    return {
        'width': png.width,
        'height': png.height,
        'mean_display_luma': round(mean, 4),
        'std_display_luma': round(max(lum_sq / total - mean * mean, 0.0) ** 0.5, 4),
        'near_black_fraction': round(black / total, 6) if total else None,
        'near_white_fraction': round(white / total, 6) if total else None,
    }


def capture_screenshot(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    if not path.exists() or path.stat().st_size <= 2048:
        raise RuntimeError('screenshot not written: ' + str(path))
    info = {'file': str(path), 'bytes': path.stat().st_size, 'sha256': sha256_file(path),
            'capture_draw': STATE['draws']}
    info.update(pixel_metrics(path))
    return info


def hash_environment():
    records = {}
    for path in HASH_FILES:
        key = str(path)
        try:
            records[key] = {
                'exists': path.exists(),
                'bytes': path.stat().st_size if path.exists() else None,
                'sha256': sha256_file(path) if path.exists() else None,
            }
        except Exception as exc:
            records[key] = {'exists': path.exists(), 'error': str(exc)}
    try:
        engine_version = json.loads(ENGINE_VERSION.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        engine_version = {'error': str(exc)}
    try:
        engine_modules = json.loads(ENGINE_MODULES.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        engine_modules = {'error': str(exc)}
    try:
        project_modules = json.loads(PROJECT_MODULES.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        project_modules = {'error': str(exc)}
    return {
        'files': records,
        'engine_version': engine_version,
        'engine_build_id': engine_modules.get('BuildId'),
        'project_module_build_id': project_modules.get('BuildId'),
        'bridge_build_id_matches_declared': (
            str(project_modules.get('BuildId', '')).lower() == DECLARED_BRIDGE_BUILD_ID),
        'engine_build_id_matches_project': (
            str(engine_modules.get('BuildId', '')).lower() == str(project_modules.get('BuildId', '')).lower()),
    }


def inventory(path):
    files = []
    total = 0
    by_top = {}
    for p in sorted(Path(path).rglob('*')):
        if p.is_file():
            size = p.stat().st_size
            rel = str(p.relative_to(path)).replace('\\', '/')
            total += size
            files.append({'path': rel, 'bytes': size})
            top = rel.split('/', 1)[0]
            by_top[top] = by_top.get(top, 0) + size
    return {
        'dump_directory': str(path),
        'file_count': len(files),
        'total_bytes': total,
        'total_gib': round(total / (1024 ** 3), 4),
        'bytes_by_top_level': by_top,
        'files': files,
    }


def dump_status():
    status_file = Path(RAW_DIR) / 'Base' / 'Status.txt'
    if not status_file.exists():
        return None
    try:
        return status_file.read_text(encoding='utf-8', errors='replace').strip()
    except Exception:
        return None


def dump_size():
    total = 0
    try:
        for p in Path(RAW_DIR).rglob('*'):
            if p.is_file():
                total += p.stat().st_size
    except Exception:
        pass
    return total


def write_progress(state, **extra):
    now = time.monotonic()
    if now - PROGRESS['last_write'] < 2.0 and state not in ('complete', 'error'):
        return
    PROGRESS['last_write'] = now
    data = {
        'schema': SESSION['schema'],
        'state': state,
        'run_id': RUN_ID,
        'raw_directory': str(RAW_DIR),
        'draws': STATE['draws'],
        'dump_status': dump_status(),
        'dump_bytes': dump_size(),
        'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    }
    data.update(extra)
    atomic_json(GPU / 'progress.json', data)


def finish_warmup():
    status = render_status()
    readback = pilot_readback(HERO)
    SESSION['warmup'] = {
        'draws': STATE['draws'],
        'elapsed_seconds': round(time.monotonic() - STATE['warmup_start'], 3),
        'render_status': status,
        'pilot_readback': readback,
        'hero_actor_readback': actor_readback(HERO),
        'exposure': read_exposure(PP),
        'cvars_after_warmup': cvar_records([
            'r.Lumen.HardwareRayTracing', 'r.DynamicGlobalIlluminationMethod',
            'r.ReflectionMethod', 'r.LumenScene.FarField',
            'r.Lumen.ScreenProbeGather', 'r.Lumen.ScreenProbeGather.ScreenTraces',
            'r.Lumen.Reflections.ScreenTraces', 'r.Lumen.Visualize',
            'r.Lumen.ScreenProbeGather.Debug', 'r.Lumen.ScreenProbeGather.Debug.ProbePlacement',
            'r.Lumen.ScreenProbeGather.VisualizeTraces', 'r.ShaderPrint',
            'r.ScreenPercentage', 'r.AntiAliasingMethod', 'r.MotionBlurQuality',
            'r.BloomQuality', 'r.DepthOfFieldQuality', 'r.VolumetricFog',
            'r.RHI.UniformBufferContentMap.Enable',
        ]),
    }
    problems = []
    if not readback.get('pilot_matches'):
        problems.append('pilot actor mismatch')
    if not readback.get('exact_camera_view') or not readback.get('allows_cinematic_control'):
        problems.append('camera locks not active')
    if readback.get('actual_view_fov') != 70.0:
        problems.append('actual ViewFOV %r != 70.0' % (readback.get('actual_view_fov'),))
    if status.get('viewport_size') != [1280, 720]:
        problems.append('viewport_size %r != [1280,720]' % (status.get('viewport_size'),))
    if status.get('view_mode_index') != 3:
        problems.append('view_mode_index %r != 3 (lit)' % (status.get('view_mode_index'),))
    if problems:
        raise RuntimeError('warmup validation failed: ' + '; '.join(problems))

    SESSION['pre_dump_screenshot'] = capture_screenshot(PRE / 'hero-predump.png')
    SESSION['pre_dump_readback'] = pilot_readback(HERO)
    SESSION['cvars_before_dump'] = cvar_records([name for name, _ in DUMP_CVARS])
    applied = {}
    for name, value in DUMP_CVARS:
        console('%s %s' % (name, value))
        applied[name] = {'requested': value, 'actual': cvar(name)}
    SESSION['dump_cvars_applied'] = applied
    SESSION['dump_cvars_actual'] = cvar_records([
        'r.DumpGPU.Root', 'r.DumpGPU.Texture', 'r.DumpGPU.Buffer',
        'r.DumpGPU.PassParameters', 'r.DumpGPU.ConsoleVariables',
        'r.DumpGPU.RedumpInputs', 'r.DumpGPU.NativeUniformBufferTextures',
        'r.DumpGPU.Buffer.SkipTracking', 'r.DumpGPU.MaxStagingSize',
        'r.DumpGPU.FrameCount', 'r.DumpGPU.Stream', 'r.DumpGPU.Upload',
        'r.DumpGPU.Test.EnableDiskWrite', 'r.DumpGPU.Test.PrettifyResourceFileNames',
        'r.RHI.UniformBufferContentMap.Enable',
    ])
    SESSION['all_r_cvars_before_dump'] = json.loads(BRIDGE.get_console_variables('r.'))
    SESSION['all_showflag_cvars_before_dump'] = json.loads(BRIDGE.get_console_variables('showflag.'))
    RAW_DIR.parent.mkdir(parents=True, exist_ok=True)
    SESSION['dump_umbrella_directory'] = str(RAW_DIR.parent)
    SESSION['dump_directory'] = str(RAW_DIR)
    SESSION['dump_armed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['dump_armed_monotonic'] = time.monotonic()
    ok = console(SESSION['dump_command'])
    SESSION['dump_command_returned'] = ok
    STATE['dump_armed'] = time.monotonic()
    STATE['phase'] = 'dump_trigger'
    atomic_json(SESSION_PATH, SESSION)
    write_progress('dump_armed', dump_command=SESSION['dump_command'])
    log('dump armed: ' + SESSION['dump_command'])


def finish(status='ok', error=None):
    global HANDLE
    handle, HANDLE = HANDLE, None
    if handle is not None:
        try:
            unreal.unregister_slate_post_tick_callback(handle)
        except Exception as exc:
            SESSION.setdefault('notes', []).append('unregister: ' + str(exc))
    try:
        SESSION['status'] = status
        if error:
            SESSION['error'] = error
        SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        SESSION['elapsed_seconds'] = round(time.monotonic() - SESSION['started_monotonic'], 3)
        SESSION['draws'] = STATE['draws']
        SESSION['dump_status'] = dump_status()
        SESSION['dump_done_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        SESSION['map_sha256_after'] = sha256_file(MAP_FILE)
        SESSION['map_sha256_before'] = SESSION.get('map_sha256_before')
        SESSION['map_unchanged'] = SESSION['map_sha256_after'] == SESSION.get('map_sha256_before')
        if HERO is not None:
            SESSION['hero_actor_readback_final'] = actor_readback(HERO)
            SESSION['hero_pilot_readback_final'] = pilot_readback(HERO)
        if RAW_DIR.exists():
            inv = inventory(RAW_DIR)
            inv.pop('files', None)
            SESSION['dump_inventory_summary'] = inv
            SESSION['dump_file_count'] = inv['file_count']
            SESSION['dump_total_bytes'] = inv['total_bytes']
            SESSION['dump_total_gib'] = inv['total_gib']
            SESSION['over_100gib'] = inv['total_bytes'] > BUDGET_BYTES
            # Keep a complete path/byte inventory next to the run record.
            full = inventory(RAW_DIR)
            atomic_json(GPU / f'inventory-{RUN_ID}.json', {
                'schema': 'arda-sponza-gpu-dump-file-inventory-v1',
                'run_id': RUN_ID,
                'dump_directory': str(RAW_DIR),
                **full,
            })
        SESSION['commands'] = COMMANDS
        atomic_json(SESSION_PATH, SESSION)
        atomic_json(LATEST_PATH, {
            'schema': 'arda-sponza-gpu-dump-latest-v1',
            'run_id': RUN_ID,
            'status': status,
            'session': str(SESSION_PATH),
            'raw_directory': str(RAW_DIR),
            'dump_status': SESSION.get('dump_status'),
            'dump_file_count': SESSION.get('dump_file_count'),
            'dump_total_bytes': SESSION.get('dump_total_bytes'),
        })
        write_progress('complete' if status == 'ok' else 'error', error=error)
        log('finish %s files=%s bytes=%s' % (status, SESSION.get('dump_file_count'),
                                             SESSION.get('dump_total_bytes')))
    finally:
        try:
            unreal.EditorPythonScripting.set_keep_python_script_alive(False)
        except Exception:
            pass
        STATE['done'] = True


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        if STATE['phase'] == 'warmup':
            BRIDGE.render_viewport()
            STATE['draws'] += 1
            if STATE['draws'] % 50 == 0:
                write_progress('warmup', warmup_draws=STATE['draws'])
            if STATE['draws'] >= WARMUP_DRAWS + SETTLE_DRAWS:
                finish_warmup()
            return

        if STATE['phase'] == 'dump_trigger':
            # Two draws straddle the 'next frame' arming point; FrameCount=1 and
            # -oneframe still guarantee at most one dumped engine frame.
            BRIDGE.render_viewport()
            STATE['draws'] += 1
            STATE['dump_trigger_draws'] += 1
            STATE['dump_last_retrigger'] = time.monotonic()
            if STATE['dump_trigger_draws'] >= 2:
                STATE['phase'] = 'dump_wait'
                write_progress('dump_waiting')
            return

        if STATE['phase'] == 'dump_wait':
            status = dump_status()
            SESSION['dump_status_last'] = status
            if status == 'ok':
                STATE['dump_done'] = time.monotonic()
                SESSION['dump_completed_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                SESSION['dump_seconds'] = round(STATE['dump_done'] - STATE['dump_armed'], 3)
                SESSION['dump_trigger_draws'] = STATE['dump_trigger_draws']
                SESSION['dump_retriggers'] = STATE['dump_retriggers']
                finish('ok')
                return
            elapsed = time.monotonic() - STATE['dump_armed']
            size = dump_size()
            if size > BUDGET_BYTES:
                STATE['over_budget'] = True
                SESSION['over_budget_observed_bytes'] = size
                SESSION['notes'] = SESSION.get('notes', []) + [
                    'Raw dump exceeded 100 GiB while running; no further dump was armed.']
            if elapsed > DUMP_TIMEOUT_SECONDS:
                raise RuntimeError('DumpGPU did not reach status ok in %ss (status=%r, dir=%s, bytes=%s)'
                                   % (DUMP_TIMEOUT_SECONDS, status, RAW_DIR, size))
            # Keep the render graph flowing only if the armed dump has not
            # started.  These draws do not arm another dump.
            if (not STATE['over_budget'] and elapsed > 12.0 and STATE['dump_retriggers'] < 3
                    and time.monotonic() - (STATE['dump_last_retrigger'] or 0) > 12.0):
                BRIDGE.render_viewport()
                STATE['draws'] += 1
                STATE['dump_retriggers'] += 1
                STATE['dump_last_retrigger'] = time.monotonic()
            write_progress('dump_waiting', dump_status=status, elapsed_seconds=round(elapsed, 1),
                           dump_bytes=size, retriggers=STATE['dump_retriggers'])
            return

        raise RuntimeError('unhandled phase ' + str(STATE['phase']))
    except Exception:
        message = traceback.format_exc()
        ERROR_PATH.parent.mkdir(parents=True, exist_ok=True)
        ERROR_PATH.write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_GPU_DUMP_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


try:
    GPU.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    PRE.mkdir(parents=True, exist_ok=True)
    (GPU / 'raw').mkdir(parents=True, exist_ok=True)

    SETUP = json.loads(SETUP_PATH.read_text(encoding='utf-8-sig'))
    if SETUP.get('schema') != 'arda-sponza-final-setup-v2':
        raise RuntimeError('unexpected final-setup schema: ' + str(SETUP.get('schema')))
    SESSION['setup_sha256'] = sha256_file(SETUP_PATH)
    SESSION['map_sha256_before'] = sha256_file(MAP_FILE)
    SESSION['environment'] = hash_environment()

    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    if WORLD is None:
        raise RuntimeError('no editor world after load')

    actors = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}
    HERO = actors.get('CAM_Hero_Reference')
    PP = actors.get('PostProcess_Sponza')
    if HERO is None or PP is None:
        raise RuntimeError('saved CAM_Hero_Reference or PostProcess_Sponza is missing')
    SESSION['actor_labels'] = sorted(actors)
    SESSION['saved_hero_actor_readback'] = actor_readback(HERO)
    SESSION['saved_exposure'] = read_exposure(PP)

    expected = SETUP['config']['hero_camera']
    got = SESSION['saved_hero_actor_readback']
    expected_loc = [float(v) for v in expected['location_cm']]
    expected_rot = expected['rotation_degrees']
    loc_ok = all(abs(got['location_cm'][i] - expected_loc[i]) < 0.01 for i in range(3))
    rot_ok = (abs(got['rotation_degrees']['pitch'] - float(expected_rot['pitch'])) < 0.001 and
              abs(got['rotation_degrees']['yaw'] - float(expected_rot['yaw'])) < 0.001 and
              abs(got['rotation_degrees']['roll'] - float(expected_rot['roll'])) < 0.001)
    fov_ok = abs(got['horizontal_fov_deg'] - float(expected['horizontal_fov_deg'])) < 0.001
    SESSION['saved_hero_matches_approved'] = {
        'location': loc_ok, 'rotation': rot_ok, 'horizontal_fov': fov_ok,
        'expected': expected, 'actual': got,
    }
    if not (loc_ok and rot_ok and fov_ok):
        raise RuntimeError('saved CAM_Hero_Reference does not match output/final-setup.json')

    set_exposure(PP, float(SETUP['config']['light']['exposure_ev100']),
                 float(SETUP['config']['light'].get('exposure_bias', 0.0)))
    try:
        BRIDGE.set_viewport_mode('lit', '')
    except Exception as exc:
        SESSION['notes'] = SESSION.get('notes', []) + ['set_viewport_mode lit: ' + str(exc)]
    for command in CLEAN_COMMANDS:
        console(command)
    pilot(HERO)
    SESSION['initial_pilot_readback'] = pilot_readback(HERO)
    SESSION['game_view_enabled'] = bool(LEVEL.editor_get_game_view())

    STATE['warmup_start'] = time.monotonic()
    STATE['phase'] = 'warmup'
    write_progress('warmup')
    atomic_json(SESSION_PATH, SESSION)
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('setup complete run=%s raw=%s' % (RUN_ID, RAW_DIR))
except Exception:
    message = traceback.format_exc()
    ERROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    ERROR_PATH.write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_GPU_DUMP_SETUP_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass
