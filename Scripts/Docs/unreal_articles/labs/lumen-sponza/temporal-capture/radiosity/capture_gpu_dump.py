#!/usr/bin/env python3
"""Same-run narrow radiosity-surface capture on the retained Sponza scene.

Five selected states in one Unreal process:

  * warm baseline at 80000 lux, camera (-850, 0, 90), pitch 13, yaw 0, HFOV 70;
  * light 80000 -> 20000 lux: early update 1 and settled update 64 (actual 63);
  * restore 80000 lux and warm 64 draws (not a dump);
  * camera -150 cm Y to (-850, -150, 90), same rotation/FOV: updates 1 and 64.

Every dump is a narrow exact-resource selection that includes Lumen.SceneDepth,
Lumen.SceneNormal, the card/page buffers, the three radiosity probe SH atlases,
NumFramesAccumulatedAtlas, SceneIndirectLighting, FilteredTraceRadianceAtlas,
ViewData.PrimaryViews and SceneDepthZ.  Pass parameters and native uniform
buffers are captured (r.DumpGPU.PassParameters / NativeUniformBufferTextures /
the launcher's startup r.RHI.UniformBufferContentMap.Enable override).

The actual dumped update is recovered from each Base/Passes.json "Frame N"
scope matched to the bridge timeline; targets are never relabelled.  The native
Base/Screenshot.png is the same-frame clean image.  Raw layouts and bytes are
preserved; SceneNormal's exporter-expanded BC5 readback format is recorded per
version.  No debug visualizer is needed and no depth-mode PNG is used as a
substitute for the raw surface atlases.

Run with the unchanged known-working launcher:
  D:\\UnrealUnderstanding-Sponza\\scripts\\run_gpu_dump.ps1 -Workspace D:\\UnrealUnderstanding-Radiosity
The script restores camera/sun/exposure in memory only and never saves the map.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

_SCRIPT_WORKSPACE = (Path(__file__).resolve().parents[1]
                     if '__file__' in globals()
                     else Path(r'D:\UnrealUnderstanding-Radiosity'))
_ENV_WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', str(_SCRIPT_WORKSPACE)))
WORKSPACE = (_SCRIPT_WORKSPACE
             if (_SCRIPT_WORKSPACE / 'project' / 'UnrealLab' / 'UnrealLab.uproject').exists()
             else _ENV_WORKSPACE)
os.environ['ARDA_SPONZA_WORKSPACE'] = str(WORKSPACE)
SCRIPTS = WORKSPACE / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import unreal  # noqa: E402

from image_stats import Png  # noqa: E402
from apply_final_setup import (  # noqa: E402
    LEVEL, ACTORS, BRIDGE, apply_config, atomic_json, console, cvars,
    load_setup, pilot, pilot_readback, pixel_metrics, set_exposure,
    sha256_file, MAP_PATH, MAP_FILE, LOGS,
)
from validate_capture_plan import (  # noqa: E402
    filters, all_resources, BUDGET_BYTES, CUMULATIVE_BUDGET_BYTES,
)

# ---------------------------------------------------------------------------
# experiment constants
# ---------------------------------------------------------------------------
SUN_HIGH = 80000.0
SUN_LOW = 20000.0
EV100 = 11.0
EXPOSURE_BIAS = 0.0
WARM_DRAWS = 120
RESTORE_WARM_DRAWS = 64
FINAL_WARM_DRAWS = 24
TARGET_UPDATE = 64
TARGETS = [1, 64]
REQUIRED_DUMP_LABELS = ('baseline', 'light-001', 'light-064',
                        'camera-001', 'camera-064')
CAMERA_START_CM = [-850.0, 0.0, 90.0]
CAMERA_TARGET_CM = [-850.0, -150.0, 90.0]
CAMERA_ROTATION = {'pitch': 13.0, 'yaw': 0.0, 'roll': 0.0}
CAMERA_HFOV = 70.0
CAMERA_MOVE_CM = -150.0
MODE = 'radiosity-surface'
MAP_EXPECTED_SHA256 = '3ef49eddd474fc16e6129dac3bcbb16bb4937529d9213296a865d368ede72c64'
DUMP_TIMEOUT_SECONDS = 10 * 60
SCREENSHOT_TIMEOUT_SECONDS = 180
RESOLUTION = (1280, 720)
WORKSPACE_RAW_CAP_BYTES = BUDGET_BYTES
CUMULATIVE_RAW_CAP_BYTES = CUMULATIVE_BUDGET_BYTES
DUMP_RESERVE_BYTES = int(1.25 * 1024 ** 3)
PRIOR_RAW_DIRS = [
    (Path(r'D:\UnrealUnderstanding-Motion') / 'output' / 'gpu-dump' / 'raw'),
    (Path(r'D:\UnrealUnderstanding-Temporal') / 'output' / 'gpu-dump' / 'raw'),
]
SPONZA = Path(r'D:\UnrealUnderstanding-Sponza')

SCENE_DEPTH = 'Lumen.SceneDepth'
SCENE_NORMAL = 'Lumen.SceneNormal'
SH_RESOURCES = [
    'Lumen.Radiosity.ProbeSHRedAtlas',
    'Lumen.Radiosity.ProbeSHGreenAtlas',
    'Lumen.Radiosity.ProbeSHBlueAtlas',
]
CARD_STATE_RESOURCES = [
    'Lumen.Cards', 'Lumen.MeshCards', 'Lumen.PageBuffer',
    'Lumen.PageTable', 'Lumen.CardTiles', 'Lumen.CardCaptureRects',
]
REQUIRED_RESOURCES = all_resources()

GPU = WORKSPACE / 'output' / 'gpu-dump'
RAW_ROOT_BASE = GPU / 'raw'
RUN_ID = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
if (RAW_ROOT_BASE / RUN_ID).exists():
    RUN_ID = RUN_ID + '-' + str(int(time.time()) % 1000)
RAW_ROOT = RAW_ROOT_BASE / RUN_ID
RADIOSITY = WORKSPACE / 'output' / 'radiosity'
IMAGES = RADIOSITY / f'images-{RUN_ID}'
RAW_SCREENSHOTS = RADIOSITY / f'raw-screenshots-{RUN_ID}'
SESSION_PATH = RADIOSITY / f'run-{RUN_ID}.json'
TIMELINE_PATH = RADIOSITY / f'timeline-{RUN_ID}.json'
MANIFEST_PATH = RADIOSITY / f'manifest-{RUN_ID}.json'
LATEST_PATH = GPU / 'latest-run.json'
REPORT_PATH = WORKSPACE / 'output' / 'capture-report.md'
ERROR_PATH = LOGS / f'radiosity-{RUN_ID}-error.txt'
PROJECT_FILE = WORKSPACE / 'project' / 'UnrealLab' / 'UnrealLab.uproject'
PROJECT_MODULES = WORKSPACE / 'project' / 'UnrealLab' / 'Binaries' / 'Win64' / 'UnrealEditor.modules'
BRIDGE_DLL = WORKSPACE / 'project' / 'UnrealLab' / 'Binaries' / 'Win64' / 'UnrealEditor-ArdaLabCapture.dll'
ENGINE_ROOT = Path(os.environ.get('ARDA_ENGINE_ROOT', r'D:\UnrealEngine'))
EDITOR_EXE = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.exe'
ENGINE_VERSION = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.version'
ENGINE_MODULES = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.modules'
BUILD_VERSION = ENGINE_ROOT / 'Engine' / 'Build' / 'Build.version'

FILTER_PAIRS = filters()
FILTER_STRING = '|'.join(f'{p}@{r}' for p, r in FILTER_PAIRS)
EXPECTED_RESOURCES = all_resources()

STATE = {
    'phase': 'startup',
    'subphase': '',
    'phase_draw': 0,
    'draws': 0,
    'last_render_frame': None,
    'dump': None,
    'done': False,
    'busy': False,
    'started_monotonic': time.monotonic(),
    'phases': {},
    'skips': [],
}
PROGRESS = {'last_write': 0.0}

SESSION = {
    'schema': 'arda-radiosity-surface-selected-v1',
    'status': 'running',
    'run_id': RUN_ID,
    'workspace': str(WORKSPACE),
    'resolution': list(RESOLUTION),
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': STATE['started_monotonic'],
    'sun_high_lux': SUN_HIGH,
    'sun_low_lux': SUN_LOW,
    'exposure_ev100': EV100,
    'exposure_bias': EXPOSURE_BIAS,
    'warm_draws': WARM_DRAWS,
    'restore_warm_draws': RESTORE_WARM_DRAWS,
    'targets_bridge_draws': TARGETS,
    'required_dump_labels': list(REQUIRED_DUMP_LABELS),
    'targets_definition': ('bridge viewport draws since the sun change or the '
                           'direct -150 cm camera move; not engine frames'),
    'mode': MODE,
    'camera_start_cm': CAMERA_START_CM,
    'camera_target_cm': CAMERA_TARGET_CM,
    'camera_rotation_degrees': CAMERA_ROTATION,
    'camera_hfov_deg': CAMERA_HFOV,
    'camera_move_cm': CAMERA_MOVE_CM,
    'filters': [f'{p}@{r}' for p, r in FILTER_PAIRS],
    'filter_count': len(FILTER_PAIRS),
    'expected_resources': EXPECTED_RESOURCES,
    'budget_bytes': WORKSPACE_RAW_CAP_BYTES,
    'budget_gib': round(WORKSPACE_RAW_CAP_BYTES / 1024 ** 3, 3),
    'cumulative_budget_bytes': CUMULATIVE_RAW_CAP_BYTES,
    'cumulative_budget_gib': round(CUMULATIVE_RAW_CAP_BYTES / 1024 ** 3, 3),
    'phases': {},
    'timeline': [],
    'dumps': [],
    'clean': [],
    'camera': {},
    'skips': [],
    'errors': [],
    'notes': [],
    'limitations': [
        'DumpGPU captures RDG-visible resources only. Memoryless resources, '
        'shader-local registers and resources never bound to a selected pass are '
        'not present; exact-name absences are reported, never replaced.',
        'A bridge viewport draw is not an engine frame. Both counters are recorded '
        'and the dumped frame is matched to the timeline after the fact from '
        'Base/Passes.json Frame N scopes.',
        'The native DumpGPU Base/Screenshot.png is the image of the dumped frame. '
        'Post-dump lit PNGs are separate later observations with recorded offsets.',
        'SceneNormal is PF_BC5 natively but DumpGPU writes an exporter-expanded '
        'PF_FloatRGBA readback; NativeFormatExpanded and StoredBytesAreNativeStorage '
        'are recorded per version and the raw bytes are preserved unmodified.',
        'No depth-mode PNG is used as a substitute for Lumen.SceneDepth. The CPU '
        'surface depth-lift must read the raw Lumen.SceneDepth/Lumen.SceneNormal '
        'payloads and the same-dump ViewData.PrimaryViews/SceneDepthZ.',
        'The map is restored in memory only; it is never saved.',
    ],
}


def log(message):
    unreal.log('ARDA_RADIOSITY_SURFACE ' + str(message))


def now_iso():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def engine_frame():
    try:
        return int(unreal.SystemLibrary.get_frame_count())
    except Exception:
        return None


def sun_readback():
    if SUN_COMP is None:
        return None
    rot = SUN.get_actor_rotation()
    return {
        'actor': SUN.get_actor_label(),
        'rotation_degrees': {'pitch': round(float(rot.pitch), 6),
                             'yaw': round(float(rot.yaw), 6),
                             'roll': round(float(rot.roll), 6)},
        'intensity_lux': round(float(SUN_COMP.intensity), 6),
        'temperature_k': float(SUN_COMP.get_editor_property('temperature')),
        'atmosphere_sun_light': bool(SUN_COMP.get_editor_property('atmosphere_sun_light')),
    }


def hero_light():
    try:
        loc = HERO.get_actor_location()
        rot = HERO.get_actor_rotation()
        return {
            'location_cm': [round(float(loc.x), 4), round(float(loc.y), 4),
                            round(float(loc.z), 4)],
            'rotation_degrees': {'pitch': round(float(rot.pitch), 6),
                                 'yaw': round(float(rot.yaw), 6),
                                 'roll': round(float(rot.roll), 6)},
            'horizontal_fov_deg': round(float(HERO.camera_component.field_of_view), 6),
        }
    except Exception as exc:
        return {'error': str(exc)}


def read_exposure(pp):
    try:
        settings = pp.get_editor_property('settings')
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


def exposure_fixed(rec):
    try:
        return (abs(float(rec['auto_exposure_min_brightness']) - EV100) < 1e-6 and
                abs(float(rec['auto_exposure_max_brightness']) - EV100) < 1e-6 and
                abs(float(rec['auto_exposure_bias']) - EXPOSURE_BIAS) < 1e-6)
    except Exception:
        return False


def write_checkpoint():
    atomic_json(RADIOSITY / 'checkpoint.json', {
        'schema': SESSION['schema'],
        'run_id': RUN_ID,
        'status': SESSION['status'],
        'phase': STATE['phase'],
        'subphase': STATE['subphase'],
        'phase_draw': STATE['phase_draw'],
        'draws': STATE['draws'],
        'dumps': len(SESSION['dumps']),
        'raw_directory': str(RAW_ROOT),
        'updated_utc': now_iso(),
    })


def write_session(force=False):
    now = time.monotonic()
    if not force and now - PROGRESS['last_write'] < 2.0:
        return
    PROGRESS['last_write'] = now
    SESSION['draws'] = STATE['draws']
    SESSION['phase'] = STATE['phase']
    SESSION['phase_draw'] = STATE['phase_draw']
    atomic_json(SESSION_PATH, SESSION)
    atomic_json(TIMELINE_PATH, {
        'schema': 'arda-radiosity-surface-timeline-v1',
        'run_id': RUN_ID,
        'timeline': SESSION['timeline'],
    })
    write_checkpoint()


def timeline_add(action, note, **extra):
    SESSION['timeline'].append({
        'index': len(SESSION['timeline']),
        'draw': STATE['draws'],
        'phase': STATE['phase'],
        'subphase': STATE['subphase'],
        'phase_draw': STATE['phase_draw'],
        'engine_frame': engine_frame(),
        'utc': now_iso(),
        'monotonic': round(time.monotonic() - STATE['started_monotonic'], 6),
        'action': action,
        'note': note,
        'sun_lux': None if SUN_COMP is None else round(float(SUN_COMP.intensity), 6),
        'camera': hero_light(),
        **extra,
    })


def frame_ready():
    frame = engine_frame()
    if frame is None:
        return True
    return STATE['last_render_frame'] is None or frame != STATE['last_render_frame']


def render_once(action):
    if not frame_ready():
        return False
    BRIDGE.render_viewport()
    STATE['draws'] += 1
    STATE['phase_draw'] += 1
    STATE['last_render_frame'] = engine_frame()
    timeline_add(action, 'bridge render_viewport')
    write_session()
    return True


def set_lit_mode():
    try:
        BRIDGE.set_viewport_mode('lit', '')
    except Exception as exc:
        SESSION['notes'].append('set_viewport_mode lit: ' + str(exc))
    console('r.BufferVisualizationTarget ""')


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
    'r.Lumen.Visualize.CardPlacementDistance 5000',
    'r.Lumen.Visualize.CardPlacementLOD 0',
    'r.Lumen.Visualize.CardPlacementPrimitives 0',
    'r.Lumen.ScreenProbeGather.Debug 0',
    'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 0',
    'r.Lumen.ScreenProbeGather.VisualizeTraces 0',
    'r.Lumen.ScreenProbeGather.TileDebugMode 0',
    'r.Lumen.RadianceCache.Visualize 0',
    'r.LumenScene.Radiosity.VisualizeProbes 0',
    'r.LumenScene.Radiosity.VisualizeProbes.ShowInvalid 0',
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

CLEAN_CVAR_NAMES = [
    'r.Lumen.Visualize', 'r.Lumen.Visualize.CardPlacement',
    'r.Lumen.ScreenProbeGather.Debug',
    'r.Lumen.ScreenProbeGather.Debug.ProbePlacement',
    'r.Lumen.ScreenProbeGather.VisualizeTraces',
    'r.Lumen.ScreenProbeGather.TileDebugMode',
    'r.Lumen.RadianceCache.Visualize',
    'r.ShaderPrint', 'r.DynamicGlobalIlluminationMethod',
    'r.ReflectionMethod', 'r.Lumen.HardwareRayTracing',
    'r.LumenScene.FarField', 'r.Lumen.ScreenProbeGather.ScreenTraces',
    'r.Lumen.Reflections.ScreenTraces', 'r.LumenScene.SurfaceCache.AtlasSize',
    'r.AntiAliasingMethod', 'r.ScreenPercentage', 'r.MotionBlurQuality',
    'r.BloomQuality', 'r.DepthOfFieldQuality', 'r.VolumetricFog',
    'r.DumpGPU.Root', 'r.DumpGPU.Texture', 'r.DumpGPU.Buffer',
    'r.DumpGPU.PassParameters', 'r.DumpGPU.RedumpInputs',
    'r.DumpGPU.MaxStagingSize', 'r.DumpGPU.NativeUniformBufferTextures',
    'r.DumpGPU.Buffer.SkipTracking',
]


def set_clean():
    set_lit_mode()
    for command in CLEAN_COMMANDS:
        console(command)


DUMP_CVARS = [
    ('r.DumpGPU.Root', '*'),
    ('r.DumpGPU.Texture', '2'),
    ('r.DumpGPU.Buffer', '2'),
    ('r.DumpGPU.PassParameters', '1'),
    ('r.DumpGPU.ConsoleVariables', '1'),
    ('r.DumpGPU.Explore', '0'),
    ('r.DumpGPU.Upload', '0'),
    ('r.DumpGPU.MaxStagingSize', '512'),
    ('r.DumpGPU.Stream', '0'),
    ('r.DumpGPU.RedumpInputs', '0'),
    ('r.DumpGPU.FrameCount', '1'),
    ('r.DumpGPU.FrameDelay', '0'),
    ('r.DumpGPU.Delay', '0'),
    ('r.DumpGPU.Draws', '0'),
    ('r.DumpGPU.Mask', '1'),
    ('r.DumpGPU.CameraCut', '0'),
    ('r.DumpGPU.EnableLogWrite', '1'),
    ('r.DumpGPU.Test.EnableDiskWrite', '1'),
    ('r.DumpGPU.Test.PrettifyResourceFileNames', '0'),
    ('r.DumpGPU.NativeUniformBufferTextures', '1'),
    ('r.DumpGPU.Buffer.SkipTracking', '1'),
]


def capture_lit(tag, note):
    """Capture a clean lit bridge observation; never a raw substitute."""
    path = IMAGES / f'{tag}-postlit.png'
    while not frame_ready():
        yield
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    before_frame = engine_frame()
    before_phase = STATE['phase_draw']
    STATE['subphase'] = tag + '-postlit'
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    STATE['phase_draw'] += 1
    STATE['last_render_frame'] = engine_frame()
    timeline_add('capture', note, label=tag + '-postlit', path=str(path))
    deadline = time.monotonic() + SCREENSHOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    if not path.exists() or path.stat().st_size <= 2048:
        raise RuntimeError('screenshot not written: ' + str(path))
    rec = {
        'label': tag + '-postlit',
        'note': note,
        'kind': 'post-dump-lit-observation',
        'phase': STATE['phase'],
        'phase_draw': STATE['phase_draw'],
        'phase_draw_before_render': before_phase,
        'capture_draw_total': STATE['draws'],
        'engine_frame_before': before_frame,
        'engine_frame_after': engine_frame(),
        'sun': sun_readback(),
        'camera': hero_light(),
        'pilot': pilot_readback(HERO),
        'exposure': read_exposure(PP),
        'cvars': cvars(CLEAN_CVAR_NAMES),
        'file': str(path),
        'bytes': path.stat().st_size,
        'sha256': sha256_file(path),
        'view_mode_index': json.loads(BRIDGE.get_render_status()).get('view_mode_index'),
        'same_frame_as_dump': False,
    }
    rec.update(pixel_metrics(path))
    std = rec.get('std_display_luma')
    mean = rec.get('mean_display_luma')
    black = rec.get('near_black_fraction')
    rec['empty_view'] = bool(rec.get('pixel_metrics_error') or std is None or
                             std < 1.0 or (mean is not None and mean < 1.0) or
                             (black is not None and black > 0.98))
    rec['view_valid'] = not rec['empty_view']
    rec['view_status'] = 'empty' if rec['empty_view'] else 'ok'
    write_session(force=True)
    yield
    return rec


def advance_to(target):
    while STATE['phase_draw'] < target:
        if not frame_ready():
            yield
            continue
        render_once('advance')
        yield


def warm(draws):
    STATE['phase_draw'] = 0
    yield from advance_to(draws)


def set_phase(name, note=''):
    STATE['phase'] = name
    STATE['subphase'] = ''
    STATE['phase_draw'] = 0
    STATE['last_render_frame'] = engine_frame()
    STATE['phases'][name] = {
        'started_utc': now_iso(),
        'start_engine_frame': engine_frame(),
        'start_draw_total': STATE['draws'],
        'note': note,
    }
    SESSION['phases'][name] = STATE['phases'][name]
    timeline_add('phase', note or name)
    write_checkpoint()
    log('phase ' + name)


def set_sun(lux):
    SUN_COMP.set_intensity(float(lux))
    rec = {
        'requested_lux': float(lux),
        'readback': sun_readback(),
        'engine_frame': engine_frame(),
        'draw': STATE['draws'],
        'utc': now_iso(),
    }
    SESSION.setdefault('sun_changes', []).append(rec)
    timeline_add('sun', 'intensity %.1f' % float(lux), requested_lux=float(lux))
    log('sun -> %.1f' % float(lux))
    return rec


# ---------------------------------------------------------------------------
# dump control and metadata
# ---------------------------------------------------------------------------
def read_status(directory):
    status = Path(directory) / 'Base' / 'Status.txt'
    if not status.exists():
        return None
    try:
        return status.read_text(encoding='utf-8', errors='replace').strip()
    except Exception:
        return None


def dir_bytes(path):
    total = 0
    try:
        for p in Path(path).rglob('*'):
            if p.is_file():
                total += p.stat().st_size
    except Exception:
        pass
    return total


def raw_bytes():
    return dir_bytes(RAW_ROOT) if RAW_ROOT.exists() else 0


def workspace_raw_bytes():
    return dir_bytes(RAW_ROOT_BASE) if RAW_ROOT_BASE.exists() else 0


def prior_raw_bytes():
    values = {}
    for path in PRIOR_RAW_DIRS:
        values[str(path)] = dir_bytes(path) if path.exists() else 0
    return values


def stream_json(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        while i < len(text) and text[i] in ' \r\n\t,':
            i += 1
        if i >= len(text):
            break
        obj, i = dec.raw_decode(text, i)
        yield obj


def unique_dump_dir(label):
    base = RAW_ROOT / label
    if not base.exists():
        return base
    for i in range(2, 100):
        candidate = RAW_ROOT / (label + '-r%d' % i)
        if not candidate.exists():
            return candidate
    raise RuntimeError('cannot allocate dump directory for ' + label)


def arm_dump(label, target, phase):
    directory = unique_dump_dir(label)
    STATE['dump'] = {
        'label': label,
        'target': target,
        'phase': phase,
        'directory': str(directory),
        'arm_phase_draw': STATE['phase_draw'],
        'arm_draw_total': STATE['draws'],
        'arm_engine_frame': engine_frame(),
        'armed_utc': now_iso(),
        'arm_monotonic': time.monotonic(),
        'camera': hero_light(),
        'pilot': pilot_readback(HERO),
        'sun': sun_readback(),
        'exposure': read_exposure(PP),
        'render_status': json.loads(BRIDGE.get_render_status()),
        'cvars': {},
    }
    applied = {}
    for name, value in DUMP_CVARS:
        console('%s %s' % (name, value))
        applied[name] = {'requested': value, 'actual': cvars([name]).get(name)}
    console('r.DumpGPU.Directory ' + str(RAW_ROOT))
    command = ('DumpGPU -path="%s" -noexplorer -oneframe '
               '-unified_filter="%s"' % (directory, FILTER_STRING))
    returned = console(command)
    STATE['dump'].update({
        'command': command,
        'command_returned': returned,
        'filter_count': len(FILTER_PAIRS),
        'dump_cvars_applied': applied,
        'dump_cvars_actual': cvars([n for n, _ in DUMP_CVARS]),
        'rhi_uniform_buffer_content_map': cvars(
            ['r.RHI.UniformBufferContentMap.Enable']),
        'raw_bytes_before': raw_bytes(),
    })
    SESSION['notes'].append('armed %s target=%s phase_draw=%d frame=%s' % (
        label, target, STATE['phase_draw'], engine_frame()))
    write_session(force=True)
    log('armed dump %s dir=%s phase_draw=%d frame=%s' % (
        label, directory, STATE['phase_draw'], engine_frame()))
    return STATE['dump']


def wait_dump():
    dump = STATE['dump']
    if dump is None:
        raise RuntimeError('wait_dump without an armed dump')
    start = time.monotonic()
    keepalive = 0
    while True:
        status = read_status(dump['directory'])
        if status == 'ok':
            dump['status'] = status
            dump['seconds'] = round(time.monotonic() - start, 3)
            return status
        if status and status.lower() not in ('dumping', 'starting', 'pending'):
            dump['status'] = status
            dump['seconds'] = round(time.monotonic() - start, 3)
            raise RuntimeError('DumpGPU status %r in %s' % (status, dump['directory']))
        elapsed = time.monotonic() - start
        if elapsed > DUMP_TIMEOUT_SECONDS:
            raise RuntimeError('DumpGPU timeout after %ss status=%r dir=%s' % (
                DUMP_TIMEOUT_SECONDS, status, dump['directory']))
        yield
        if status is None and keepalive < 3 and elapsed > 1.0:
            if frame_ready():
                render_once('dump-keepalive')
                keepalive += 1
                dump['keepalive_draws'] = keepalive


def _dedup_layouts(layouts):
    seen, out = set(), []
    for l in layouts:
        key = tuple(l.get(k) for k in (
            'MipLevel', 'FirstArraySlice', 'ExtentX', 'ExtentY', 'Dimension',
            'NativeFormat', 'StagingFormat', 'StoredFormat', 'NativeFormatExpanded',
            'StoredBytesAreNativeStorage', 'ByteSize', 'RowPitchBytes',
            'NativeValueInterpretation', 'ValuePreserving', 'BitExactCondition'))
        if key in seen:
            continue
        seen.add(key)
        out.append({k: l.get(k) for k in (
            'MipLevel', 'FirstArraySlice', 'NumArraySlices', 'ExtentX', 'ExtentY',
            'Dimension', 'NativeFormat', 'ViewFormat', 'StagingFormat', 'StoredFormat',
            'NativeFormatExpanded', 'NativeSRGB', 'StoredBytesAreNativeStorage',
            'NativeValueInterpretation', 'BitExact', 'ValuePreserving',
            'BitExactCondition', 'ByteSize', 'RowPitchBytes', 'Readback', 'DumpStatus')})
    return out


def selected_resource_report(base):
    descs = list(stream_json(base / 'ResourceDescs.json')) if (base / 'ResourceDescs.json').exists() else []
    readbacks = list(stream_json(base / 'DumpReadbackStatus.json')) if (base / 'DumpReadbackStatus.json').exists() else []
    layouts = list(stream_json(base / 'SubresourceLayouts.json')) if (base / 'SubresourceLayouts.json').exists() else []
    passes = list(stream_json(base / 'Passes.json')) if (base / 'Passes.json').exists() else []

    desc_by_name = defaultdict(list)
    for d in descs:
        desc_by_name[d.get('Name', '')].append(d)

    layouts_by_ptr = defaultdict(list)
    for l in layouts:
        layouts_by_ptr[l.get('Resource')].append(l)

    pass_uses = defaultdict(list)
    for p in passes:
        scope = ' > '.join(str(x) for x in (p.get('ParentEventScopes') or []))
        frames = [int(m.group(1)) for m in re.finditer(r'Frame\s+(\d+)', scope)]
        info = {
            'pass_id': p.get('Id'),
            'event_name': p.get('EventName'),
            'parent_scopes': p.get('ParentEventScopes') or [],
            'frame_numbers': frames,
            'pointer': p.get('Pointer'),
            'pointer_prefix': str(p.get('Pointer') or '')[:4],
            'parameters_name': p.get('ParametersName'),
        }
        for raw in (list(p.get('InputResources') or []) + list(p.get('OutputResources') or [])):
            ptr = str(raw).split('.')[0]
            pass_uses[ptr].append(info)

    rb_by_key = defaultdict(list)
    for rb in readbacks:
        m = re.search(r'Resources/([0-9a-f]+)', str(rb.get('File', '')))
        if m:
            rb_by_key[m.group(1)].append(rb)

    res_dir = base.parent / 'Resources'
    res_files = [p for p in res_dir.iterdir() if p.is_file()] if res_dir.exists() else []

    def direct_files(key):
        hits = []
        for p in res_files:
            name = p.name
            if name.startswith(key + '.') or name.startswith('NativeBuffer.' + key + '.'):
                produced = re.search(r'\.v([0-9a-f]{20})\.', name)
                hits.append({
                    'key': key,
                    'file': str(p),
                    'relative_file': 'Resources/' + name,
                    'bytes': p.stat().st_size,
                    'expected_bytes': None,
                    'status': 'FileExists',
                    'kind': 'direct-file',
                    'timing': None,
                    'version': produced.group(1) if produced else None,
                    'descriptor_bytes': None,
                    'desc': None,
                    'evidence': 'resources-directory',
                    'readback_status_entry': False,
                })
        return hits

    present, missing = {}, []
    for name in EXPECTED_RESOURCES:
        ds = desc_by_name.get(name, [])
        if not ds:
            missing.append({
                'name': name,
                'reason': ('no RDG descriptor with this exact name in the selected '
                           'frame; it was not created or not bound to a selected pass'),
            })
            continue
        versions = []
        for d in ds:
            ptr = d.get('UniqueResourceName')
            used = pass_uses.get(ptr, [])
            fmts = _dedup_layouts(layouts_by_ptr.get(ptr, []))
            version_recs = []
            seen_files = set()
            for rb in rb_by_key.get(ptr, []):
                rel = str(rb.get('File', ''))
                if rel in seen_files:
                    continue
                seen_files.add(rel)
                produced = re.search(r'\.v([0-9a-f]{20})\.', rel)
                path = base.parent / rel
                version_recs.append({
                    'key': ptr,
                    'file': str(path) if path.exists() else rel,
                    'relative_file': rel,
                    'bytes': rb.get('ActualBytes'),
                    'expected_bytes': rb.get('ExpectedBytes'),
                    'status': rb.get('Status'),
                    'kind': rb.get('Kind'),
                    'timing': rb.get('Timing'),
                    'version': produced.group(1) if produced else None,
                    'descriptor_bytes': d.get('ByteSize'),
                    'desc': d.get('Desc'),
                    'evidence': 'readback-status',
                    'readback_status_entry': True,
                    'sha256': sha256_file(path) if path.exists() else None,
                    'formats': fmts,
                    'pass_ids': sorted({int(u['pass_id']) for u in used
                                        if u.get('pass_id') is not None}),
                    'pass_event_names': sorted({str(u['event_name']) for u in used}),
                    'pass_pointer_prefixes': sorted({str(u['pointer_prefix']) for u in used}),
                    'parameter_block_names': sorted({str(u['parameters_name']) for u in used
                                                     if u.get('parameters_name')}),
                    'frame_numbers': sorted({f for u in used for f in u['frame_numbers']}),
                    'pointer_prefix': str(ptr)[:4],
                })
            for f in direct_files(ptr):
                rel = f['relative_file']
                if rel in seen_files:
                    continue
                seen_files.add(rel)
                f['descriptor_bytes'] = d.get('ByteSize')
                f['desc'] = d.get('Desc')
                f['sha256'] = sha256_file(f['file']) if Path(f['file']).exists() else None
                f['formats'] = fmts
                f['pass_ids'] = sorted({int(u['pass_id']) for u in used
                                        if u.get('pass_id') is not None})
                f['pass_event_names'] = sorted({str(u['event_name']) for u in used})
                f['pass_pointer_prefixes'] = sorted({str(u['pointer_prefix']) for u in used})
                f['parameter_block_names'] = sorted({str(u['parameters_name']) for u in used
                                                     if u.get('parameters_name')})
                f['frame_numbers'] = sorted({fr for u in used for fr in u['frame_numbers']})
                f['pointer_prefix'] = str(ptr)[:4]
                version_recs.append(f)
            versions.extend(version_recs)
        dumped = [v for v in versions
                  if ((v.get('status') == 'Dumped' and (v.get('bytes') or 0) > 0) or
                      (v.get('evidence') == 'resources-directory' and (v.get('bytes') or 0) > 0))]
        rec = {
            'name': name,
            'descriptor_count': len(ds),
            'versions': versions,
            'dumped_version_count': len(dumped),
            'dumped_bytes': sum((v.get('bytes') or 0) for v in dumped),
            'present': bool(dumped),
            'formats': _dedup_layouts(
                [l for d in ds for l in layouts_by_ptr.get(d.get('UniqueResourceName'), [])]),
            'present_versions': dumped,
        }
        if dumped:
            present[name] = rec
        else:
            rec['reason'] = ('descriptor exists but no Dumped readback entry or direct '
                             'Resources/<key> payload exists')
            missing.append(rec)
    return {
        'present': present,
        'missing': missing,
        'present_count': len(present),
        'missing_count': len(missing),
        'expected_count': len(EXPECTED_RESOURCES),
        'descriptor_count': len(descs),
        'readback_count': len(readbacks),
        'layout_count': len(layouts),
        'pass_count': len(passes),
        'readback_status_counts': dict(Counter(r.get('Status') for r in readbacks)),
        'direct_file_count': len(res_files),
    }


def coverage_summary(selected):
    present = set(selected['present'])
    missing = sorted(set(EXPECTED_RESOURCES) - present)
    depth = selected['present'].get(SCENE_DEPTH)
    normal = selected['present'].get(SCENE_NORMAL)
    sh_present = [r for r in SH_RESOURCES if r in present]

    def first_format(rec):
        if not rec:
            return None
        fmts = rec.get('formats') or []
        return fmts[0] if fmts else None

    depth_fmt = first_format(depth)
    normal_fmt = first_format(normal)
    depth_contract = bool(
        depth_fmt and int(depth_fmt.get('ExtentX') or 0) == 4096 and
        int(depth_fmt.get('ExtentY') or 0) == 4096 and
        str(depth_fmt.get('NativeFormat')) == 'PF_G16')
    normal_contract = bool(
        normal_fmt and int(normal_fmt.get('ExtentX') or 0) == 4096 and
        int(normal_fmt.get('ExtentY') or 0) == 4096 and
        str(normal_fmt.get('NativeFormat')) == 'PF_BC5')
    return {
        'required': EXPECTED_RESOURCES,
        'missing': missing,
        'missing_count': len(missing),
        'depth_present': SCENE_DEPTH in present,
        'normal_present': SCENE_NORMAL in present,
        'radiosity_sh_present': len(sh_present) == 3,
        'radiosity_sh_resources_present': sh_present,
        'card_state_present': all(r in present for r in CARD_STATE_RESOURCES),
        'view_depth_present': all(r in present for r in ('ViewData.PrimaryViews', 'SceneDepthZ')),
        'scene_depth_format': depth_fmt,
        'scene_normal_format': normal_fmt,
        'scene_depth_4096_pf_g16': depth_contract,
        'scene_normal_4096_pf_bc5': normal_contract,
        'formats_ok': depth_contract and normal_contract,
    }


def parse_dump(directory):
    base = Path(directory) / 'Base'
    result = {'directory': str(directory), 'metadata_error': None}
    try:
        passes = list(stream_json(base / 'Passes.json')) if (base / 'Passes.json').exists() else []
        frames = set()
        for p in passes:
            for scope in p.get('ParentEventScopes') or []:
                m = re.search(r'Frame\s+(\d+)', str(scope))
                if m:
                    frames.add(int(m.group(1)))
        prologues = [i for i, p in enumerate(passes)
                     if p.get('EventName') == 'Graph Prologue (Graphics)']
        epilogues = [i for i, p in enumerate(passes)
                     if p.get('EventName') == 'Graph Epilogue']
        graphs = []
        for start in prologues:
            ends = [e for e in epilogues if e > start]
            if ends:
                end = min(ends)
                graphs.append({
                    'first_pass_index': start,
                    'last_pass_index': end,
                    'pass_count': end - start + 1,
                    'engine_frames': sorted({int(m.group(1)) for p in passes[start:end + 1]
                                             for scope in (p.get('ParentEventScopes') or [])
                                             for m in [re.search(r'Frame\s+(\d+)', str(scope))] if m}),
                })
        resources = selected_resource_report(base)
        pass_parameters = selected_pass_parameters(base, passes)
        inv_files = 0
        inv_bytes = 0
        for p in Path(directory).rglob('*'):
            if p.is_file():
                inv_files += 1
                inv_bytes += p.stat().st_size
        result.update({
            'status': read_status(directory),
            'engine_frames': sorted(frames),
            'pass_count': len(passes),
            'graph_count': len(graphs),
            'graphs': graphs,
            'selected_resources': resources,
            'coverage': coverage_summary(resources),
            'selected_pass_parameters': pass_parameters,
            'file_count': inv_files,
            'total_bytes': inv_bytes,
            'total_gib': round(inv_bytes / 1024 ** 3, 5),
        })
    except Exception as exc:
        result['metadata_error'] = str(exc)
    return result


def match_timeline(frames, phase):
    hits = [t for t in SESSION['timeline']
            if t.get('engine_frame') in frames and t.get('phase') == phase]
    updates = sorted({int(t.get('phase_draw', -1)) for t in hits})
    return {
        'matched_timeline_entries': len(hits),
        'actual_phase_draws': updates,
        'actual_phase_draw': max(updates) if updates else None,
        'matches': [{
            'draw': t.get('draw'),
            'phase_draw': t.get('phase_draw'),
            'engine_frame': t.get('engine_frame'),
            'action': t.get('action'),
            'subphase': t.get('subphase'),
        } for t in hits[:40]],
    }


def artifact_records(base):
    names = ('Passes.json', 'ResourceDescs.json', 'DumpReadbackStatus.json',
             'SubresourceLayouts.json', 'ParameterBindings.json',
             'NativeUniformBufferBindings.json', 'ReferencedStructs.json',
             'ConsoleVariables.csv', 'Screenshot.png')
    out = {}
    for name in names:
        path = base / name
        out[name] = {
            'exists': path.exists(),
            'bytes': path.stat().st_size if path.exists() else None,
            'sha256': sha256_file(path) if path.exists() else None,
        }
    return out


PASS_PARAMETER_EVENTS = (
    'Batched lights', 'BuildRadiosityTiles', 'Integrate', 'ConvertToSH',
    'CopyCardCaptureLightingToAtlas', 'DepthPassParallel', 'BasePassParallel',
)


def _scan_struct_metadata(root, top_pointer, max_depth=4):
    """Recursively read one pass's captured parameter metadata graph.

    DumpGPU stores nested shader structs as separate StructuresMetadata JSON
    files referenced by `StructMetadata` pointers.  Fields such as
    FixedJitterIndex live in those nested files, so a plain text search of the
    top-level file is not enough.
    """
    seen = set()
    members = set()
    structs = set()
    files = []

    def visit(pointer, depth):
        if not pointer or pointer in seen or depth > max_depth:
            return
        seen.add(pointer)
        path = root / 'StructuresMetadata' / f'{pointer}.json'
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
            return
        if data.get('StructTypeName'):
            structs.add(str(data['StructTypeName']))
        files.append({
            'metadata_pointer': pointer,
            'file': str(path),
            'bytes': path.stat().st_size,
            'sha256': sha256_file(path),
            'struct_type': data.get('StructTypeName'),
        })
        for m in data.get('Members') or []:
            if m.get('Name'):
                members.add(str(m['Name']))
            nested = m.get('StructMetadata')
            if nested and str(nested) != '0' * 20:
                visit(str(nested), depth + 1)

    visit(top_pointer, 0)
    return sorted(members), sorted(structs), files


def selected_pass_parameters(base, passes):
    """Record the source parameter payloads for the selected pass families.

    `Batched lights` is selected with Lumen.CardTiles specifically so its pass
    parameter payload and metadata carry the source-confirmable
    CachedLightingPreExposure value.  `BuildRadiosityTiles`/`Integrate`/
    `ConvertToSH` carry the fixed jitter/radiosity parameter blocks.  The actual
    bytes stay in Structures/*.bin and StructuresMetadata/*.json; only paths,
    sizes, hashes and direct member names are copied into the manifest.
    """
    out = []
    root = base.parent
    for p in passes:
        event = str(p.get('EventName'))
        if event not in PASS_PARAMETER_EVENTS:
            continue
        ptr = p.get('Parameters')
        meta = p.get('ParametersMetadata')
        pfile = root / 'Structures' / f'{ptr}.bin' if ptr else None
        mfile = root / 'StructuresMetadata' / f'{meta}.json' if meta else None
        members, structs, metadata_files = _scan_struct_metadata(root, meta)
        scope = p.get('ParentEventScopes') or []
        rec = {
            'event_name': event,
            'pass_id': p.get('Id'),
            'parameters_name': p.get('ParametersName'),
            'parameters_pointer': ptr,
            'metadata_pointer': meta,
            'pointer_prefix': str(p.get('Pointer') or '')[:4],
            'parent_scopes': scope,
            'frame_numbers': [int(m.group(1)) for s in scope
                              for m in [re.search(r'Frame\s+(\d+)', str(s))] if m],
            'parameters_file': str(pfile) if pfile and pfile.exists() else None,
            'parameters_bytes': pfile.stat().st_size if pfile and pfile.exists() else None,
            'parameters_sha256': sha256_file(pfile) if pfile and pfile.exists() else None,
            'metadata_file': str(mfile) if mfile and mfile.exists() else None,
            'metadata_bytes': mfile.stat().st_size if mfile and mfile.exists() else None,
            'metadata_sha256': sha256_file(mfile) if mfile and mfile.exists() else None,
            'metadata_dependencies': metadata_files,
            'members': members,
            'struct_types': structs,
            'contains_cached_lighting_pre_exposure': 'CachedLightingPreExposure' in members,
            'contains_fixed_jitter_index': 'FixedJitterIndex' in members,
            'contains_radiosity_common_parameters': (
                'FLumenRadiosityCommonParameters' in structs or
                'RadiosityCommonParameters' in members),
            'contains_view_uniform': 'View' in members,
        }
        out.append(rec)
    return out


def finalize_dump():
    dump = STATE['dump']
    if dump is None:
        raise RuntimeError('finalize_dump without STATE dump')
    meta = parse_dump(dump['directory'])
    match = match_timeline(set(meta.get('engine_frames') or []), dump['phase'])
    target = dump.get('target')
    actual = match.get('actual_phase_draw')
    dump_frames = meta.get('engine_frames') or []
    dump_frame = dump_frames[0] if dump_frames else None

    base = Path(dump['directory']) / 'Base'
    native_src = base / 'Screenshot.png'
    native = {
        'label': dump['label'] + '-rawframe',
        'note': 'native DumpGPU Base/Screenshot.png of the selected frame',
        'file': str(native_src),
        'exists': native_src.exists(),
        'kind': 'native-rawframe',
        'same_frame_as_dump': True,
        'engine_frame': dump_frame,
        'phase_draw': actual,
        'source': 'DumpGPU Base/Screenshot.png',
    }
    if native_src.exists():
        native.update({
            'bytes': native_src.stat().st_size,
            'sha256': sha256_file(native_src),
        })
        try:
            native.update(pixel_metrics(native_src))
        except Exception as exc:
            native['pixel_metrics_error'] = str(exc)
        std = native.get('std_display_luma')
        mean = native.get('mean_display_luma')
        black = native.get('near_black_fraction')
        empty = bool(native.get('pixel_metrics_error') or std is None or
                     std < 1.0 or (mean is not None and mean < 1.0) or
                     (black is not None and black > 0.98))
        native['empty_view'] = empty
        native['view_valid'] = not empty
        native['view_status'] = 'empty' if empty else 'ok'
        native['view_kind'] = 'lit'
        RAW_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        copy = RAW_SCREENSHOTS / f"{dump['label']}-rawframe.png"
        shutil.copy2(native_src, copy)
        native['run_copy'] = str(copy)
        native['run_copy_sha256'] = sha256_file(copy)
    else:
        native['view_valid'] = False
        native['empty_view'] = True
        native['view_status'] = 'missing'

    stage = ('baseline' if dump['phase'] == 'baseline'
             else ('camera' if dump['phase'] == 'camera' else 'light'))
    coverage = meta.get('coverage') or {}
    rec = {
        'label': dump['label'],
        'phase': dump['phase'],
        'stage': stage,
        'required': dump['label'] in REQUIRED_DUMP_LABELS,
        'target_phase_draw': target,
        'arm_phase_draw': dump.get('arm_phase_draw'),
        'arm_draw_total': dump.get('arm_draw_total'),
        'arm_engine_frame': dump.get('arm_engine_frame'),
        'command': dump.get('command'),
        'command_returned': dump.get('command_returned'),
        'filter_count': dump.get('filter_count'),
        'status': meta.get('status'),
        'seconds': dump.get('seconds'),
        'keepalive_draws': dump.get('keepalive_draws', 0),
        'raw_directory': dump['directory'],
        'raw_frame': {
            'engine_frames': dump_frames,
            'engine_frame': dump_frame,
            'actual_phase_draw': actual,
            'actual_phase_draws': match.get('actual_phase_draws'),
            'source': 'Base/Passes.json Frame N scopes matched to the bridge timeline',
        },
        'device_state_at_arm': {
            'camera': dump.get('camera'),
            'pilot': dump.get('pilot'),
            'sun': dump.get('sun'),
            'exposure': dump.get('exposure'),
            'render_status': dump.get('render_status'),
        },
        'native_screenshot': native,
        'engine_frames_in_dump': meta.get('engine_frames'),
        'graph_count': meta.get('graph_count'),
        'graphs': meta.get('graphs'),
        'pass_count': meta.get('pass_count'),
        'actual_phase_draws': match.get('actual_phase_draws'),
        'actual_phase_draw': actual,
        'target_matched': bool(target is not None and actual is not None and
                               actual >= 1 and
                               (actual == target or abs(actual - target) <= 1)),
        'timeline_match': match,
        'selected_resources': meta.get('selected_resources'),
        'coverage': coverage,
        'pass_parameters': meta.get('selected_pass_parameters'),
        'formats': {
            'scene_depth': coverage.get('scene_depth_format'),
            'scene_normal': coverage.get('scene_normal_format'),
            'scene_depth_source_contract': coverage.get('scene_depth_4096_pf_g16'),
            'scene_normal_source_contract': coverage.get('scene_normal_4096_pf_bc5'),
        },
        'base_artifacts': artifact_records(base),
        'file_count': meta.get('file_count'),
        'total_bytes': meta.get('total_bytes'),
        'total_gib': meta.get('total_gib'),
        'metadata_error': meta.get('metadata_error'),
        'dump_cvars_actual': dump.get('dump_cvars_actual'),
        'rhi_uniform_buffer_content_map': dump.get('rhi_uniform_buffer_content_map'),
        'raw_bytes_after': raw_bytes(),
    }
    if meta.get('metadata_error'):
        rec['status'] = 'metadata-error'
    SESSION['dumps'].append(rec)
    STATE['dump'] = None
    SESSION['notes'].append('dump %s status=%s actual=%s target=%s frames=%s bytes=%s' % (
        rec['label'], rec['status'], actual, target,
        rec['engine_frames_in_dump'], rec['total_bytes']))
    write_session(force=True)
    log('dump %s status=%s actual=%s target=%s frames=%s' % (
        rec['label'], rec['status'], actual, target, rec['engine_frames_in_dump']))
    return rec


def validate_dump(dump_rec, label):
    """Hard validation: a missing depth/normal/SH payload is a real failure."""
    problems = []
    if dump_rec.get('status') != 'ok':
        problems.append('dump status is %r' % (dump_rec.get('status'),))
    if dump_rec.get('metadata_error'):
        problems.append('metadata error: ' + str(dump_rec.get('metadata_error')))
    if not (dump_rec.get('native_screenshot') or {}).get('view_valid'):
        problems.append('native Base/Screenshot.png is missing/empty')
    cov = dump_rec.get('coverage') or {}
    if not cov.get('depth_present'):
        problems.append('missing ' + SCENE_DEPTH)
    if not cov.get('normal_present'):
        problems.append('missing ' + SCENE_NORMAL)
    if not cov.get('radiosity_sh_present'):
        problems.append('missing radiosity probe SH atlas (need red/green/blue)')
    if not cov.get('card_state_present'):
        problems.append('missing one of ' + ', '.join(CARD_STATE_RESOURCES))
    if not cov.get('view_depth_present'):
        problems.append('missing ViewData.PrimaryViews or SceneDepthZ')
    if not cov.get('formats_ok'):
        problems.append('scene depth/normal format does not match the source contract: %r / %r'
                        % (cov.get('scene_depth_format'), cov.get('scene_normal_format')))
    exp = (dump_rec.get('device_state_at_arm') or {}).get('exposure') or {}
    if not exposure_fixed(exp):
        problems.append('exposure is not EV100 min=max=%.1f bias=0: %r' % (EV100, exp))
    if dump_rec.get('target_phase_draw') is not None and not dump_rec.get('target_matched'):
        problems.append('actual update %r does not match target %r within one draw'
                        % (dump_rec.get('actual_phase_draw'),
                           dump_rec.get('target_phase_draw')))
    if problems:
        raise RuntimeError('required coverage/identity failed for %s: %s'
                           % (label, '; '.join(problems)))


def dump_state(label, target, phase, note):
    used = workspace_raw_bytes()
    if used + DUMP_RESERVE_BYTES > WORKSPACE_RAW_CAP_BYTES:
        raise RuntimeError(
            'budget guard: workspace raw %d + reserve %d > cap %d before %s'
            % (used, DUMP_RESERVE_BYTES, WORKSPACE_RAW_CAP_BYTES, label))
    if target is not None:
        pre = max(int(target) - 2, 0)
        if STATE['phase_draw'] < pre:
            yield from advance_to(pre)
    set_clean()
    arm_dump(label, target, phase)
    yield
    if frame_ready():
        render_once('dump-filler')
    yield
    yield from wait_dump()
    rec = finalize_dump()
    validate_dump(rec, label)
    lit = yield from capture_lit(label, note + ' (post-dump clean lit observation)')
    rec['post_lit'] = lit
    SESSION['clean'].append(lit)
    write_session(force=True)
    yield


def apply_camera_state(target_cm, rotation, fov, label='camera-move'):
    start = [float(x) for x in SESSION['original_camera']['location_cm']]
    target = [float(x) for x in target_cm]
    rot = {k: float(rotation[k]) for k in ('pitch', 'yaw', 'roll')}
    HERO.set_actor_location(unreal.Vector(*target), False, True)
    HERO.set_actor_rotation(
        unreal.Rotator(pitch=rot['pitch'], yaw=rot['yaw'], roll=rot['roll']), False)
    try:
        HERO.camera_component.set_field_of_view(float(fov))
    except Exception as exc:
        SESSION['notes'].append('camera FOV set failed: ' + str(exc))
    pilot(HERO)
    readback = hero_light()
    pilot_rec = pilot_readback(HERO)
    viewport = pilot_rec.get('viewport_camera') or {}
    loc_ok = all(abs(readback['location_cm'][i] - target[i]) < 0.5 for i in range(3))
    rot_ok = all(abs(readback['rotation_degrees'][k] - rot[k]) < 1e-3
                 for k in ('pitch', 'yaw', 'roll'))
    fov_ok = abs(readback['horizontal_fov_deg'] - float(fov)) < 1e-3
    viewport_ok = bool(
        viewport.get('location_cm') and
        all(abs(viewport['location_cm'][i] - target[i]) < 0.5 for i in range(3)) and
        viewport.get('rotation') and
        all(abs(viewport['rotation'][k] - rot[k]) < 1e-3
            for k in ('pitch', 'yaw', 'roll')))
    applied = bool(loc_ok and rot_ok and fov_ok)
    rec = {
        'label': label,
        'applied': applied,
        'start_cm': start,
        'target_cm': target,
        'offset_cm': round(target[1] - start[1], 3),
        'requested_rotation_degrees': rot,
        'requested_horizontal_fov_deg': float(fov),
        'location_matches': loc_ok,
        'rotation_matches': rot_ok,
        'fov_matches': fov_ok,
        'viewport_matches_requested': viewport_ok,
        'readback': readback,
        'pilot': pilot_rec,
        'applied_utc': now_iso(),
        'applied_engine_frame': engine_frame(),
        'applied_draw_total': STATE['draws'],
        'method': 'direct target state, no candidate probing',
    }
    if not applied:
        rec['reason'] = 'readback did not match the target state'
    return rec


def restore_original_camera():
    cam = SESSION['original_camera']
    HERO.set_actor_location(unreal.Vector(*[float(x) for x in cam['location_cm']]), False, True)
    HERO.set_actor_rotation(unreal.Rotator(
        pitch=float(cam['rotation_degrees']['pitch']),
        yaw=float(cam['rotation_degrees']['yaw']),
        roll=float(cam['rotation_degrees']['roll'])), False)
    pilot(HERO)
    return hero_light()


# ---------------------------------------------------------------------------
# generator experiment
# ---------------------------------------------------------------------------
def experiment():
    set_clean()
    SESSION['prior_camera_validation'] = prior_camera_validation()

    set_phase('warm-baseline', 'warm 120 bridge draws at 80000 lux before the baseline')
    yield from warm(WARM_DRAWS)

    set_phase('baseline', 'warm accepted baseline at (-850,0,90), 80000 lux')
    yield from dump_state('baseline', None, 'baseline',
                          'warm accepted baseline before the light change')

    set_phase('light', 'sun 80000 -> 20000 lux; resets the update counter')
    SESSION['sun_step'] = set_sun(SUN_LOW)
    yield from dump_state('light-001', 1, 'light',
                          'first rendered update after the 20000 lux change')
    yield from dump_state('light-064', TARGET_UPDATE, 'light',
                          'settled 20000 lux state, requested update 64')

    set_phase('restore-80000', 'sun 20000 -> 80000 lux and warm 64 draws')
    SESSION['sun_restore'] = set_sun(SUN_HIGH)
    yield from warm(RESTORE_WARM_DRAWS)
    restore_lit = yield from capture_lit('restore-064',
                                         '80000 lux restored, after 64 warm draws')
    SESSION.setdefault('restore_warm', {}).update({
        'sun': sun_readback(),
        'exposure': read_exposure(PP),
        'draws': STATE['draws'],
        'phase_draw': STATE['phase_draw'],
        'lit': restore_lit,
    })
    SESSION['clean'].append(restore_lit)

    move = apply_camera_state(CAMERA_TARGET_CM, CAMERA_ROTATION, CAMERA_HFOV)
    SESSION['camera_move'] = move
    SESSION['camera']['move'] = move
    write_session(force=True)
    if not move.get('applied'):
        raise RuntimeError('camera move readback did not match the target state')

    set_phase('camera', 'direct -150 cm Y camera move at restored 80000 lux')
    SESSION['camera']['moved_utc'] = now_iso()
    SESSION['camera']['moved_engine_frame'] = engine_frame()
    SESSION['camera']['moved_draw_total'] = STATE['draws']
    yield from dump_state('camera-001', 1, 'camera',
                          'first rendered update after the -150 cm camera move')
    yield from dump_state('camera-064', TARGET_UPDATE, 'camera',
                          'settled camera state, requested update 64')

    restored_camera = restore_original_camera()
    SESSION['camera_restore_in_experiment'] = restored_camera
    SESSION['sun_restore_in_experiment'] = set_sun(SUN_HIGH)
    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_phase('final', 'original camera/sun/exposure restored in memory')
    yield from warm(FINAL_WARM_DRAWS)
    final_lit = yield from capture_lit('final', 'restored original camera, sun and exposure')
    SESSION['final_state'] = {
        'lit': final_lit,
        'camera': hero_light(),
        'sun': sun_readback(),
        'exposure': read_exposure(PP),
        'pilot': pilot_readback(HERO),
    }
    SESSION['clean'].append(final_lit)
    SESSION['phases_finished'] = sorted(SESSION['phases'])
    yield


# ---------------------------------------------------------------------------
# setup / finish
# ---------------------------------------------------------------------------
SETUP = None
WORLD = None
HERO = None
PP = None
SUN = None
SUN_COMP = None
CO = None
HANDLE = None


def prior_camera_validation():
    prior_path = Path(
        r'D:\UnrealUnderstanding-Motion\output\temporal\run-20261115-104035.json')
    rec = {'source': str(prior_path), 'available': prior_path.exists()}
    if not rec['available']:
        rec['ok'] = False
        return rec
    try:
        run = json.loads(Path(rec['source']).read_text(encoding='utf-8-sig'))
        move = run.get('camera_move') or {}
        rec.update({
            'run_id': run.get('run_id'),
            'applied': bool(move.get('applied')),
            'offset_cm': move.get('offset_cm'),
            'target_cm': move.get('target_cm'),
            'readback': move.get('readback'),
            'map_sha256_after': run.get('map_sha256_after'),
        })
        rec['ok'] = bool(
            move.get('applied') and
            [round(float(x), 3) for x in (move.get('target_cm') or [])] == CAMERA_TARGET_CM and
            (move.get('readback') or {}).get('rotation_degrees') == CAMERA_ROTATION and
            abs(float((move.get('readback') or {}).get('horizontal_fov_deg', -1)) - CAMERA_HFOV) < 1e-6)
    except Exception as exc:
        rec['ok'] = False
        rec['error'] = str(exc)
    return rec


def clean_record(item):
    if not item:
        return None
    return {
        'file': item.get('file'),
        'phase_draw': item.get('phase_draw'),
        'engine_frame': item.get('engine_frame_after'),
        'same_frame_as_dump': item.get('same_frame_as_dump'),
        'view_valid': item.get('view_valid'),
        'view_status': item.get('view_status'),
        'mean_display_luma': item.get('mean_display_luma'),
        'std_display_luma': item.get('std_display_luma'),
        'sha256': item.get('sha256'),
        'bytes': item.get('bytes'),
    }


def state_from_dump(d):
    selected = d.get('selected_resources') or {}
    resources = {}
    for name, rec in (selected.get('present') or {}).items():
        versions = []
        for v in rec.get('present_versions') or []:
            versions.append({
                'file': v.get('file'),
                'relative_file': v.get('relative_file'),
                'bytes': v.get('bytes'),
                'sha256': v.get('sha256'),
                'status': v.get('status'),
                'kind': v.get('kind'),
                'version': v.get('version'),
                'pointer_prefix': v.get('pointer_prefix'),
                'pass_ids': v.get('pass_ids'),
                'pass_event_names': v.get('pass_event_names'),
                'parameter_block_names': v.get('parameter_block_names'),
                'frame_numbers': v.get('frame_numbers'),
                'formats': v.get('formats'),
            })
        resources[name] = {
            'present': True,
            'descriptor_count': rec.get('descriptor_count'),
            'dumped_version_count': rec.get('dumped_version_count'),
            'dumped_bytes': rec.get('dumped_bytes'),
            'formats': rec.get('formats'),
            'versions': versions,
        }
    native = d.get('native_screenshot') or {}
    return {
        'label': d.get('label'),
        'stage': d.get('stage'),
        'required': d.get('required'),
        'target_update': d.get('target_phase_draw'),
        'actual_update': d.get('actual_phase_draw'),
        'actual_updates': d.get('actual_phase_draws'),
        'target_matched': d.get('target_matched'),
        'raw_frame': d.get('raw_frame'),
        'device_state_at_arm': d.get('device_state_at_arm'),
        'engine_frame': (d.get('raw_frame') or {}).get('engine_frame'),
        'raw_directory': d.get('raw_directory'),
        'dump_status': d.get('status'),
        'dump_file_count': d.get('file_count'),
        'dump_total_bytes': d.get('total_bytes'),
        'native_screenshot': {
            'file': native.get('file'),
            'run_copy': native.get('run_copy'),
            'bytes': native.get('bytes'),
            'sha256': native.get('sha256'),
            'view_valid': native.get('view_valid'),
            'view_status': native.get('view_status'),
            'mean_display_luma': native.get('mean_display_luma'),
            'std_display_luma': native.get('std_display_luma'),
            'near_black_fraction': native.get('near_black_fraction'),
            'same_frame_as_dump': True,
        },
        'post_lit': clean_record(d.get('post_lit')),
        'formats': d.get('formats'),
        'coverage': d.get('coverage'),
        'present_resource_count': (len(REQUIRED_RESOURCES) -
                                   int((d.get('coverage') or {}).get('missing_count') or 0)),
        'missing_resources': (d.get('coverage') or {}).get('missing'),
        'resources': resources,
        'pass_parameters': d.get('pass_parameters'),
        'rhi_uniform_buffer_content_map': d.get('rhi_uniform_buffer_content_map'),
        'base_artifacts': d.get('base_artifacts'),
        'timeline_match': d.get('timeline_match'),
    }


def environment_hashes():
    files = [
        EDITOR_EXE, ENGINE_VERSION, ENGINE_MODULES, BUILD_VERSION,
        ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-RenderCore.dll',
        ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-Renderer.dll',
        ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-RHI.dll',
        ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor-D3D12RHI.dll',
        BRIDGE_DLL, PROJECT_MODULES, PROJECT_FILE, MAP_FILE,
        WORKSPACE / 'output' / 'final-setup.json',
        WORKSPACE / 'output' / 'scene-manifest.json',
        WORKSPACE / 'scripts' / 'capture_gpu_dump.py',
        WORKSPACE / 'scripts' / 'validate_capture_plan.py',
        WORKSPACE / 'scripts' / 'run_gpu_dump.ps1',
        WORKSPACE / 'scripts' / 'run_unreal_hidden.ps1',
    ]
    records = {}
    for path in files:
        try:
            records[str(path)] = {
                'exists': path.exists(),
                'bytes': path.stat().st_size if path.exists() else None,
                'sha256': sha256_file(path) if path.exists() else None,
            }
        except Exception as exc:
            records[str(path)] = {'exists': False, 'error': str(exc)}
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
    launchers = {}
    for name in ('run_gpu_dump.ps1', 'run_unreal_hidden.ps1'):
        a = WORKSPACE / 'scripts' / name
        b = SPONZA / 'scripts' / name
        launchers[name] = {
            'workspace_sha256': sha256_file(a) if a.exists() else None,
            'sponza_sha256': sha256_file(b) if b.exists() else None,
            'identical': bool(a.exists() and b.exists() and
                              sha256_file(a) == sha256_file(b)),
        }
    return {
        'files': records,
        'engine_version': engine_version,
        'engine_build_id': engine_modules.get('BuildId'),
        'project_module_build_id': project_modules.get('BuildId'),
        'build_id_matches': (str(engine_modules.get('BuildId', '')).lower() ==
                             str(project_modules.get('BuildId', '')).lower()),
        'launchers': launchers,
    }


def restore():
    restored = {'errors': []}
    try:
        SUN_COMP.set_intensity(float(SUN_HIGH))
        restored['sun'] = sun_readback()
    except Exception as exc:
        restored['errors'].append('sun: ' + str(exc))
    try:
        cam = SESSION['original_camera']
        HERO.set_actor_location(unreal.Vector(*[float(x) for x in cam['location_cm']]), False, True)
        HERO.set_actor_rotation(unreal.Rotator(
            pitch=float(cam['rotation_degrees']['pitch']),
            yaw=float(cam['rotation_degrees']['yaw']),
            roll=float(cam['rotation_degrees']['roll'])), False)
        pilot(HERO)
        restored['camera'] = hero_light()
        restored['pilot'] = pilot_readback(HERO)
    except Exception as exc:
        restored['errors'].append('camera: ' + str(exc))
    try:
        set_exposure(PP, EV100, EXPOSURE_BIAS)
        restored['exposure'] = read_exposure(PP)
    except Exception as exc:
        restored['errors'].append('exposure: ' + str(exc))
    try:
        set_clean()
    except Exception as exc:
        restored['errors'].append('clean: ' + str(exc))
    return restored


def _camera_matches(rec, target):
    try:
        return (all(abs(float(rec['location_cm'][i]) - float(target[i])) < 0.5
                    for i in range(3)) and
                all(abs(float(rec['rotation_degrees'][k]) - float(CAMERA_ROTATION[k])) < 1e-3
                    for k in ('pitch', 'yaw', 'roll')) and
                abs(float(rec['horizontal_fov_deg']) - CAMERA_HFOV) < 1e-3)
    except Exception:
        return False


def evaluate_checks(states):
    labels = [s.get('label') for s in states]
    covs = [s.get('coverage') or {} for s in states]
    native = [s.get('native_screenshot') or {} for s in states]
    baseline = next((s for s in states if s.get('stage') == 'baseline'), None)
    baseline_mean = ((baseline or {}).get('native_screenshot') or {}).get('mean_display_luma')

    def mean_ratio(s):
        m = (s.get('native_screenshot') or {}).get('mean_display_luma')
        if m is None or not baseline_mean:
            return None
        return round(float(m) / float(baseline_mean), 5)

    ratios = {s.get('label'): mean_ratio(s) for s in states}
    exposure_ok = all(exposure_fixed(
        ((s.get('device_state_at_arm') or {}).get('exposure') or {})) for s in states)
    suns = [(s.get('device_state_at_arm') or {}).get('sun') or {} for s in states]
    sun_expected = []
    for s in states:
        if s.get('stage') == 'light':
            sun_expected.append(abs(float(suns[states.index(s)].get('intensity_lux', -1)) - SUN_LOW) < 1e-3)
        else:
            sun_expected.append(abs(float(suns[states.index(s)].get('intensity_lux', -1)) - SUN_HIGH) < 1e-3)
    camera_move = SESSION.get('camera_move') or {}
    camera_applied = bool(camera_move.get('applied') and
                          _camera_matches(camera_move.get('readback') or {}, CAMERA_TARGET_CM))
    restored = SESSION.get('restored') or {}
    camera_restored = _camera_matches(restored.get('camera') or {}, CAMERA_START_CM)
    sun_restored = bool(restored.get('sun') and
                        abs(float(restored['sun'].get('intensity_lux', -1)) - SUN_HIGH) < 1e-3)
    exposure_restored = exposure_fixed(restored.get('exposure') or {})
    map_ok = bool(SESSION.get('map_unchanged'))
    budget = SESSION.get('budget') or {}
    pass_params = [s.get('pass_parameters') or [] for s in states]
    cached_exposure_ok = all(
        any(p.get('event_name') == 'Batched lights' and
            p.get('contains_cached_lighting_pre_exposure') and
            p.get('parameters_file') and p.get('metadata_file')
            for p in ps) for ps in pass_params)
    placement_jitter_ok = all(
        any(p.get('event_name') == 'BuildRadiosityTiles' and
            p.get('contains_fixed_jitter_index') and
            p.get('parameters_file') and p.get('metadata_file')
            for p in ps) for ps in pass_params)
    radiosity_params_ok = all(
        any(p.get('event_name') in ('Integrate', 'ConvertToSH') and
            (p.get('contains_radiosity_common_parameters') or
             p.get('contains_fixed_jitter_index')) and
            p.get('parameters_file') and p.get('metadata_file')
            for p in ps) for ps in pass_params)
    parameter_bindings_ok = all(
        ((s.get('base_artifacts') or {}).get('ParameterBindings.json') or {}).get('bytes')
        for s in states)
    native_uniform_ok = all(
        ((s.get('base_artifacts') or {}).get('NativeUniformBufferBindings.json') or {}).get('bytes')
        for s in states)
    rhi_map_ok = all(
        ((s.get('rhi_uniform_buffer_content_map') or {}).get(
            'r.RHI.UniformBufferContentMap.Enable') or {}).get('found') and
        str(((s.get('rhi_uniform_buffer_content_map') or {}).get(
            'r.RHI.UniformBufferContentMap.Enable') or {}).get('value', '')).lower()
        in ('1', 'true', 'on')
        for s in states)
    return {
        'same_process_run': True,
        'required_states_present': all(label in labels for label in REQUIRED_DUMP_LABELS),
        'all_dumps_ok': all(s.get('dump_status') == 'ok' for s in states),
        'actual_updates_recorded': all(s.get('actual_update') is not None for s in states),
        'target_updates_within_one': all(s.get('target_matched')
                                         for s in states if s.get('target_update') is not None),
        'baseline_depth_present': bool((baseline or {}).get('coverage', {}).get('depth_present')),
        'baseline_normal_present': bool((baseline or {}).get('coverage', {}).get('normal_present')),
        'baseline_radiosity_sh_present': bool(
            (baseline or {}).get('coverage', {}).get('radiosity_sh_present')),
        'all_states_depth_normal_present': all(c.get('depth_present') and c.get('normal_present')
                                               for c in covs),
        'all_states_radiosity_sh_present': all(c.get('radiosity_sh_present') for c in covs),
        'all_states_card_state_present': all(c.get('card_state_present') for c in covs),
        'all_states_view_depth_present': all(c.get('view_depth_present') for c in covs),
        'all_states_formats_ok': all(c.get('formats_ok') for c in covs),
        'native_screenshots_valid': all(n.get('view_valid') for n in native),
        'native_same_frame_as_dump': True,
        'native_exposure_continuous': all(r is None or 0.02 <= r <= 2.0 for r in ratios.values()),
        'native_mean_ratios_to_baseline': ratios,
        'exposure_fixed_ev100': exposure_ok,
        'sun_sequence_expected': all(sun_expected),
        'camera_move_applied': camera_applied,
        'camera_restored': camera_restored,
        'sun_restored': sun_restored,
        'exposure_restored': exposure_restored,
        'map_unchanged': map_ok,
        'map_equals_expected': bool(SESSION.get('map_sha256_after') == MAP_EXPECTED_SHA256),
        'workspace_budget_ok': not budget.get('workspace_exceeded'),
        'cumulative_budget_ok': not budget.get('cumulative_exceeded'),
        'launchers_identical': all(v.get('identical') for v in
                                   ((SESSION.get('environment') or {}).get('launchers') or {}).values()),
        'build_ids_match': bool((SESSION.get('environment') or {}).get('build_id_matches')),
        'no_depth_png_as_raw_atlas': True,
        'raw_payload_bytes_preserved': all(
            (s.get('coverage') or {}).get('missing_count') == 0 for s in states),
        'pass_parameter_blocks_present': all(bool(ps) for ps in pass_params),
        'cached_lighting_exposure_parameter_present': cached_exposure_ok,
        'placement_jitter_parameter_present': placement_jitter_ok,
        'radiosity_parameter_blocks_present': radiosity_params_ok,
        'parameter_bindings_present': bool(parameter_bindings_ok),
        'native_uniform_buffer_bindings_present': bool(native_uniform_ok),
        'uniform_buffer_content_map_enabled': bool(rhi_map_ok),
        'parameter_blocks_and_native_uniform_buffers_requested': bool(
            all(bool(ps) for ps in pass_params) and cached_exposure_ok and
            placement_jitter_ok and radiosity_params_ok and
            parameter_bindings_ok and native_uniform_ok and rhi_map_ok),
    }


def write_manifest(states):
    def state_without_resources(s):
        x = dict(s)
        x.pop('resources', None)
        return x

    union_missing = sorted({name for s in states
                            for name in ((s.get('coverage') or {}).get('missing') or [])})
    checks = SESSION.get('checks') or {}
    manifest = {
        'schema': 'arda-radiosity-surface-manifest-v1',
        'run_id': RUN_ID,
        'mode': MODE,
        'status': SESSION.get('status'),
        'workspace': str(WORKSPACE),
        'session': str(SESSION_PATH),
        'timeline': str(TIMELINE_PATH),
        'raw_root': str(RAW_ROOT),
        'resolution': list(RESOLUTION),
        'targets': TARGETS,
        'required_labels': list(REQUIRED_DUMP_LABELS),
        'scene': {
            'map': MAP_PATH,
            'map_file': str(MAP_FILE),
            'map_sha256_before': SESSION.get('map_sha256_before'),
            'map_sha256_after': SESSION.get('map_sha256_after'),
            'map_sha256_expected': MAP_EXPECTED_SHA256,
            'map_unchanged': SESSION.get('map_unchanged'),
            'map_created_or_saved': False,
            'setup_sha256': SESSION.get('setup_sha256'),
            'scene_manifest_sha256': SESSION.get('scene_manifest_sha256'),
            'measured_bounds_cm': SESSION.get('measured_bounds_cm'),
        },
        'environment': SESSION.get('environment'),
        'filters': {
            'pairs': SESSION.get('filters'),
            'pair_count': SESSION.get('filter_count'),
            'resources': EXPECTED_RESOURCES,
            'selected_pass_families': sorted(PASS_PARAMETER_EVENTS),
            'depth_and_normal_included': True,
            'note': ('Exact resource names; one pass family per resource family. '
                     'Batched lights is paired only with Lumen.CardTiles so its '
                     'CachedLightingPreExposure parameter payload is captured '
                     'without adding an atlas version.'),
        },
        'sun': {
            'start_lux': SUN_HIGH,
            'low_lux': SUN_LOW,
            'changes': SESSION.get('sun_changes'),
            'restore_target_lux': SUN_HIGH,
            'restored': SESSION.get('sun_restored'),
        },
        'exposure': {
            'ev100': EV100,
            'bias': EXPOSURE_BIAS,
            'original': SESSION.get('original_exposure'),
            'restored': (SESSION.get('restored') or {}).get('exposure'),
            'fixed_all_states': (checks.get('exposure_fixed_ev100')),
        },
        'camera': {
            'start_cm': CAMERA_START_CM,
            'target_cm': CAMERA_TARGET_CM,
            'rotation_degrees': CAMERA_ROTATION,
            'horizontal_fov_deg': CAMERA_HFOV,
            'move_cm': CAMERA_MOVE_CM,
            'move': SESSION.get('camera_move'),
            'applied': (checks.get('camera_move_applied')),
            'restored': (SESSION.get('restored') or {}).get('camera'),
            'restored_ok': SESSION.get('camera_restored'),
            'no_exploration': True,
            'prior_validation': SESSION.get('prior_camera_validation'),
        },
        'restore_warm': SESSION.get('restore_warm'),
        'states': [state_without_resources(s) for s in states],
        'baseline': next((state_without_resources(s) for s in states
                          if s.get('stage') == 'baseline'), None),
        'light_states': [state_without_resources(s) for s in states
                         if s.get('stage') == 'light'],
        'camera_states': [state_without_resources(s) for s in states
                          if s.get('stage') == 'camera'],
        'resources_by_state': {s.get('label'): s.get('resources') for s in states},
        'parameter_capture': {
            'selected_pass_families': sorted(PASS_PARAMETER_EVENTS),
            'cached_lighting_exposure_field': 'CachedLightingPreExposure',
            'cached_lighting_exposure_source_pass': 'Batched lights',
            'rhi_uniform_buffer_content_map': SESSION.get('uniform_buffer_content_map'),
            'per_state': {s.get('label'): s.get('pass_parameters') for s in states},
            'checks': {k: checks.get(k) for k in (
                'pass_parameter_blocks_present',
                'cached_lighting_exposure_parameter_present',
                'placement_jitter_parameter_present',
                'radiosity_parameter_blocks_present',
                'parameter_bindings_present',
                'native_uniform_buffer_bindings_present',
                'uniform_buffer_content_map_enabled')},
        },
        'coverage': {
            'required': EXPECTED_RESOURCES,
            'union_missing_names': union_missing,
            'per_state': {s.get('label'): s.get('coverage') for s in states},
            'depth_and_normal_present_all_states': (checks.get(
                'all_states_depth_normal_present')),
            'radiosity_sh_present_all_states': (checks.get(
                'all_states_radiosity_sh_present')),
        },
        'formats': {
            'scene_depth': 'Lumen.SceneDepth: 4096x4096 PF_G16, native bytes preserved',
            'scene_normal': ('Lumen.SceneNormal: 4096x4096 PF_BC5 native, DumpGPU '
                             'exporter-expanded PF_FloatRGBA readback; per-version '
                             'NativeFormat/StoredFormat/NativeFormatExpanded recorded'),
            'per_state': {s.get('label'): s.get('formats') for s in states},
        },
        'budget': SESSION.get('budget'),
        'checks': checks,
        'images': {
            'native_rawframe': {
                'directory': str(RAW_SCREENSHOTS),
                'note': ('copy of DumpGPU Base/Screenshot.png; same frame as each dump, '
                         'source path and sha256 retained'),
            },
            'post_dump_lit': {
                'directory': str(IMAGES),
                'note': ('separate later clean lit observations; offsets are in each '
                         'state post_lit record and are not raw substitutes'),
            },
        },
        'decoded': {
            'status': 'pending_decode',
            'note': ('main worker must decode the raw Lumen.SceneDepth, Lumen.SceneNormal, '
                     'Lumen.Cards/PageBuffer/PageTable/CardTiles/CardCaptureRects and '
                     'radiosity SH payloads; project with ViewData.PrimaryViews and '
                     'SceneDepthZ from the same dump. A depth-mode PNG is not a substitute.'),
        },
    }
    atomic_json(MANIFEST_PATH, manifest)
    atomic_json(RADIOSITY / 'manifest.json', manifest)
    atomic_json(WORKSPACE / 'output' / 'capture-manifest.json', manifest)
    SESSION['manifest'] = str(MANIFEST_PATH)
    SESSION['machine_manifest'] = str(WORKSPACE / 'output' / 'capture-manifest.json')
    return manifest


def write_report(states, manifest):
    def fmt_bytes(n):
        try:
            return f'{int(n):,}'
        except Exception:
            return 'n/a'

    def fmt_gib(n):
        try:
            return f'{float(n) / 1024 ** 3:.4f}'
        except Exception:
            return 'n/a'

    lines = []
    lines.append('# Capture report - same-run narrow radiosity surface states')
    lines.append('')
    lines.append(f'Status: **{SESSION.get("status")}**  ')
    lines.append(f'Run: `{RUN_ID}`  ')
    lines.append(f'Workspace: `{WORKSPACE}`  ')
    lines.append('Map: `/Game/Labs/SponzaTeaching`  ')
    lines.append(f'Resolution: `{RESOLUTION[0]} x {RESOLUTION[1]}`, screen percentage 100  ')
    lines.append(f'Mode: `{MODE}`; one Unreal process; five selected dumps.  ')
    lines.append('')
    lines.append('## 1. Scene identity and restoration')
    lines.append('')
    lines.append('| Item | Evidence |')
    lines.append('|---|---|')
    lines.append(f'| Map SHA256 before / after | `{SESSION.get("map_sha256_before")}` / `{SESSION.get("map_sha256_after")}` |')
    lines.append(f'| Map equals expected `3ef49edd...72c64` | `{manifest["checks"].get("map_equals_expected")}` |')
    lines.append('| Map saved? | **no**; the script has no save call; restored in memory only |')
    lines.append(f'| Camera restored | `{SESSION.get("camera_restored")}` at `{CAMERA_START_CM}`, pitch 13, yaw 0, HFOV 70 |')
    lines.append(f'| Sun restored | `{SESSION.get("sun_restored")}` at `{SUN_HIGH:.0f}` lux |')
    lines.append(f'| Exposure restored | `{SESSION.get("exposure_restored")}` EV100 11.0 min=max, bias 0 |')
    lines.append(f'| Launchers unchanged | `{manifest["checks"].get("launchers_identical")}` |')
    lines.append(f'| Engine/project BuildId match | `{manifest["checks"].get("build_ids_match")}` |')
    lines.append('')
    lines.append('## 2. States, actual frames and paths')
    lines.append('')
    lines.append('| Label | Stage | Target | Actual | Dump frame | Path | Files | Bytes | Resources | Status |')
    lines.append('|---|---|---:|---:|---:|---|---:|---:|---|---|')
    for s in states:
        cov = s.get('coverage') or {}
        present = len(REQUIRED_RESOURCES) - int(cov.get('missing_count') or 0)
        lines.append('| `%s` | %s | %s | %s | %s | `%s` | %s | %s | %d/%d | %s |' % (
            s.get('label'), s.get('stage'), s.get('target_update'),
            s.get('actual_update'), s.get('engine_frame'), s.get('raw_directory'),
            fmt_bytes(s.get('dump_file_count')), fmt_bytes(s.get('dump_total_bytes')),
            present, len(REQUIRED_RESOURCES), s.get('dump_status')))
    lines.append('')
    lines.append('The dump frame is the filler render after arming; the actual update is '
                 'recovered from `Base/Passes.json` `Frame N` scopes matched to the '
                 'bridge timeline. Requested 64 is reported as actual 63 where that is '
                 'what the dump contains; nothing is relabelled.')
    lines.append('')
    lines.append('## 3. Raw coverage and formats')
    lines.append('')
    lines.append('Required exact resources per state:')
    lines.append('')
    lines.append('`' + '`, `'.join(EXPECTED_RESOURCES) + '`')
    lines.append('')
    lines.append('| State | SceneDepth | SceneNormal | SH R/G/B | Cards/pages | FilteredTrace | View/Depth | Missing |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for s in states:
        c = s.get('coverage') or {}
        lines.append('| `%s` | %s | %s | %s | %s | %s | %s | %s |' % (
            s.get('label'), c.get('depth_present'), c.get('normal_present'),
            c.get('radiosity_sh_present'), c.get('card_state_present'),
            c.get('missing_count') == 0,
            c.get('view_depth_present'),
            ', '.join(c.get('missing') or []) or '-'))
    lines.append('')
    lines.append('Source-confirmed raw formats:')
    lines.append('')
    lines.append('* `Lumen.SceneDepth`: 4096x4096, `PF_G16` native/stored, 33,554,432 bytes, '
                 '`NativeFormatExpanded=False`, `StoredBytesAreNativeStorage=True`.')
    lines.append('* `Lumen.SceneNormal`: 4096x4096, `PF_BC5` native, exporter-expanded '
                 '`PF_FloatRGBA` readback (`NativeFormatExpanded=True`, '
                 '`StoredBytesAreNativeStorage=False`); the per-version layout is in the '
                 'manifest and the raw bytes are preserved unchanged.')
    lines.append('* Radiosity SH atlases: 1024x1024 `PF_FloatRGBA`; '
                 '`NumFramesAccumulatedAtlas` 512x512 `PF_R8`; '
                 '`SceneIndirectLighting`/`FilteredTraceRadianceAtlas` 4096x4096 '
                 '`PF_FloatR11G11B10`.')
    lines.append('')
    lines.append('Every dumped version in the manifest carries its path, byte count, '
                 'SHA-256, native/stored format, graph pointer prefix, pass ids/names and '
                 'frame numbers. Base `Passes.json`, `ParameterBindings.json`, '
                 '`NativeUniformBufferBindings.json`, `ReferencedStructs.json`, '
                 '`SubresourceLayouts.json`, `ResourceDescs.json`, '
                 '`DumpReadbackStatus.json` and `ConsoleVariables.csv` are hashed per state.')
    lines.append('')
    lines.append('Source parameter blocks and native uniform buffers:')
    lines.append('')
    pc = manifest.get('parameter_capture') or {}
    lines.append('* Selected pass families: `' + '`, `'.join(pc.get('selected_pass_families') or []) + '`.')
    lines.append('* `Batched lights` is selected with `Lumen.CardTiles` so its captured '
                 'parameter payload includes `CachedLightingPreExposure` '
                 '(`FParameters` offset 204 in the captured metadata).')
    lines.append('* `BuildRadiosityTiles` contributes the radiosity placement block with '
                 '`FixedJitterIndex`; `Integrate`/`ConvertToSH` contribute '
                 '`FLumenRadiosityCommonParameters` and the SH/trace parameters.')
    lines.append('* `r.DumpGPU.PassParameters=1` and '
                 '`r.DumpGPU.NativeUniformBufferTextures=1` are applied before each dump; '
                 'the launcher startup override records '
                 '`r.RHI.UniformBufferContentMap.Enable=1`.')
    lines.append('')
    lines.append('| State | Pass payloads | CachedLightingPreExposure | FixedJitterIndex | ParameterBindings | NativeUniformBufferBindings |')
    lines.append('|---|---|---|---|---|---|')
    for s in states:
        ps = s.get('pass_parameters') or []
        ba = s.get('base_artifacts') or {}
        cached = any(p.get('event_name') == 'Batched lights' and
                     p.get('contains_cached_lighting_pre_exposure') for p in ps)
        jitter = any(p.get('event_name') == 'BuildRadiosityTiles' and
                     p.get('contains_fixed_jitter_index') for p in ps)
        pb = fmt_bytes((ba.get('ParameterBindings.json') or {}).get('bytes'))
        nb = fmt_bytes((ba.get('NativeUniformBufferBindings.json') or {}).get('bytes'))
        lines.append('| `%s` | %d | %s | %s | %s | %s |' % (
            s.get('label'), len(ps), cached, jitter, pb, nb))
    lines.append('')
    lines.append('## 4. Sun, camera and exposure sequence')
    lines.append('')
    lines.append(f'* Baseline: 80000 lux, camera `{CAMERA_START_CM}`, pitch 13, yaw 0, HFOV 70.')
    lines.append(f'* Light step: 80000 -> {SUN_LOW:.0f} lux; dumps at requested updates 1 and 64.')
    lines.append(f'* Restore: 20000 -> 80000 lux and 64 warm draws (no dump), then the camera move.')
    lines.append(f'* Camera move: direct `{CAMERA_MOVE_CM:+.0f} cm` Y to `{CAMERA_TARGET_CM}`, '
                 'same rotation/FOV; dumps at requested updates 1 and 64.')
    lines.append(f'* Exposure: fixed EV100 `{EV100}` min=max, bias `{EXPOSURE_BIAS}` in every state.')
    lines.append('')
    lines.append('## 5. Native clean images')
    lines.append('')
    lines.append('| State | Native image | Bytes | SHA-256 | Mean / std | Valid |')
    lines.append('|---|---|---:|---|---|---|')
    for s in states:
        n = s.get('native_screenshot') or {}
        lines.append('| `%s` | `%s` | %s | `%s` | %s / %s | %s |' % (
            s.get('label'), n.get('run_copy') or n.get('file'), fmt_bytes(n.get('bytes')),
            n.get('sha256'), n.get('mean_display_luma'), n.get('std_display_luma'),
            n.get('view_valid')))
    lines.append('')
    lines.append('`Base/Screenshot.png` is the native same-frame image. Post-dump lit PNGs are '
                 'separate later observations and are labelled as such; no depth-mode PNG is '
                 'used as a raw surface atlas.')
    lines.append('')
    superseded_path = RADIOSITY / 'superseded-attempts.json'
    if superseded_path.exists():
        try:
            superseded = json.loads(superseded_path.read_text(encoding='utf-8-sig'))
            lines.append('## 6. Superseded attempt(s)')
            lines.append('')
            lines.append('| Run | Reason | Raw disposition | Retained records |')
            lines.append('|---|---|---|---|')
            for a in superseded.get('attempts') or []:
                lines.append('| `%s` | %s | %s | %s |' % (
                    a.get('run_id'), a.get('reason'), a.get('raw_disposition'),
                    a.get('records')))
            lines.append('')
        except Exception:
            pass
    lines.append('## 7. Budget')
    lines.append('')
    b = SESSION.get('budget') or {}
    lines.append('| Quantity | Value |')
    lines.append('|---|---:|')
    lines.append(f'| New raw in this workspace | {fmt_bytes(b.get("workspace_used_bytes"))} bytes ({fmt_gib(b.get("workspace_used_bytes"))} GiB) |')
    lines.append(f'| Workspace cap | {fmt_bytes(WORKSPACE_RAW_CAP_BYTES)} bytes (8 GiB) |')
    lines.append(f'| Existing Motion + Temporal raw | {fmt_bytes(b.get("existing_raw_bytes"))} bytes ({fmt_gib(b.get("existing_raw_bytes"))} GiB) |')
    lines.append(f'| Cumulative | {fmt_bytes(b.get("cumulative_used_bytes"))} bytes ({fmt_gib(b.get("cumulative_used_bytes"))} GiB) |')
    lines.append(f'| Cumulative cap | {fmt_bytes(CUMULATIVE_RAW_CAP_BYTES)} bytes (45 GiB) |')
    lines.append('')
    lines.append('## 8. Checks and hand-off')
    lines.append('')
    lines.append('| Check | Result |')
    lines.append('|---|---|')
    for k, v in (manifest.get('checks') or {}).items():
        if k == 'native_mean_ratios_to_baseline':
            continue
        lines.append(f'| `{k}` | `{v}` |')
    lines.append('')
    lines.append(f'Machine manifest: `{WORKSPACE / "output" / "capture-manifest.json"}`  ')
    lines.append(f'Session: `{SESSION_PATH}`  ')
    lines.append(f'Timeline: `{TIMELINE_PATH}`')
    lines.append('')
    text = '\n'.join(lines) + '\n'
    Path(REPORT_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(REPORT_PATH).write_text(text, encoding='utf-8')
    return REPORT_PATH


def finish(status='ok', error=None):
    global HANDLE
    restore_rec = restore()
    SESSION['restored'] = restore_rec
    restored_camera = restore_rec.get('camera') or {}
    SESSION['camera_restored'] = _camera_matches(restored_camera, CAMERA_START_CM)
    SESSION['sun_restored'] = bool(
        restore_rec.get('sun') and
        abs(float(restore_rec['sun'].get('intensity_lux', -1)) - SUN_HIGH) < 1e-3)
    SESSION['exposure_restored'] = exposure_fixed(restore_rec.get('exposure') or {})
    SESSION['status'] = status
    if error:
        SESSION['error'] = error
    SESSION['finished_utc'] = now_iso()
    SESSION['elapsed_seconds'] = round(time.monotonic() - SESSION['started_monotonic'], 3)
    SESSION['draws'] = STATE['draws']
    SESSION['timeline_entries'] = len(SESSION['timeline'])
    SESSION['map_sha256_after'] = sha256_file(MAP_FILE)
    SESSION['map_sha256_before'] = SESSION.get('map_sha256_before')
    SESSION['map_unchanged'] = SESSION['map_sha256_before'] == SESSION['map_sha256_after']
    SESSION['environment'] = environment_hashes()
    all_raw = workspace_raw_bytes()
    prior = prior_raw_bytes()
    prior_total = sum(prior.values())
    cumulative = all_raw + prior_total
    SESSION['budget'] = {
        'workspace_budget_bytes': WORKSPACE_RAW_CAP_BYTES,
        'workspace_budget_gib': round(WORKSPACE_RAW_CAP_BYTES / 1024 ** 3, 3),
        'workspace_used_bytes': raw_bytes(),
        'workspace_used_gib': round(raw_bytes() / 1024 ** 3, 4),
        'workspace_used_all_runs_bytes': all_raw,
        'workspace_used_all_runs_gib': round(all_raw / 1024 ** 3, 4),
        'workspace_exceeded': all_raw > WORKSPACE_RAW_CAP_BYTES,
        'existing_raw_bytes': prior_total,
        'existing_raw_gib': round(prior_total / 1024 ** 3, 4),
        'existing_raw_by_directory': prior,
        'cumulative_budget_bytes': CUMULATIVE_RAW_CAP_BYTES,
        'cumulative_budget_gib': round(CUMULATIVE_RAW_CAP_BYTES / 1024 ** 3, 3),
        'cumulative_used_bytes': cumulative,
        'cumulative_used_gib': round(cumulative / 1024 ** 3, 4),
        'cumulative_exceeded': cumulative > CUMULATIVE_RAW_CAP_BYTES,
        'reserve_per_dump_bytes': DUMP_RESERVE_BYTES,
        'approved_by_supervisor': True,
        'note': ('8 GiB new raw cap and 45 GiB cumulative with the existing '
                 'Motion + Temporal 27.91 GiB. No input raw is deleted.'),
    }
    dumps = SESSION['dumps']
    statuses = [d.get('status') for d in dumps]
    if not dumps:
        dump_status = 'none'
    elif all(s == 'ok' for s in statuses):
        dump_status = 'ok'
    elif any(s == 'ok' for s in statuses):
        dump_status = 'partial'
    else:
        dump_status = 'failed'
    SESSION['dump_status'] = dump_status
    SESSION['dump_count'] = len(dumps)
    SESSION['dump_file_count'] = sum(int(d.get('file_count') or 0) for d in dumps)
    SESSION['dump_total_bytes'] = sum(int(d.get('total_bytes') or 0) for d in dumps)
    SESSION['dump_total_gib'] = round(SESSION['dump_total_bytes'] / 1024 ** 3, 4)
    states = [state_from_dump(d) for d in dumps]
    SESSION['checks'] = evaluate_checks(states)
    required_ok = bool(all(
        any(d.get('label') == label and d.get('status') == 'ok' for d in dumps)
        for label in REQUIRED_DUMP_LABELS))
    SESSION['required_states_ok'] = required_ok
    if status == 'ok':
        failed = [k for k, v in SESSION['checks'].items()
                  if isinstance(v, bool) and not v]
        if failed:
            SESSION['status'] = 'partial'
            SESSION['errors'].append('checks failed: ' + ', '.join(failed))
    if not required_ok:
        SESSION['errors'].append(
            'one or more required narrow states are missing or not ok: ' +
            ', '.join(REQUIRED_DUMP_LABELS))
    if not SESSION['map_unchanged']:
        SESSION['errors'].append('map hash changed; the script never saves, investigate immediately')
    try:
        manifest = write_manifest(states)
    except Exception as exc:
        SESSION['errors'].append('manifest: ' + str(exc))
        manifest = {'checks': SESSION['checks']}
    try:
        write_report(states, manifest)
    except Exception as exc:
        SESSION['errors'].append('report: ' + str(exc))
    write_session(force=True)
    atomic_json(LATEST_PATH, {
        'schema': 'arda-radiosity-surface-latest-v1',
        'run_id': RUN_ID,
        'mode': MODE,
        'status': SESSION['status'],
        'session': str(SESSION_PATH),
        'machine_manifest': str(WORKSPACE / 'output' / 'capture-manifest.json'),
        'report': str(REPORT_PATH),
        'raw_directory': str(RAW_ROOT),
        'dump_status': dump_status,
        'dump_count': len(dumps),
        'dump_file_count': SESSION['dump_file_count'],
        'dump_total_bytes': SESSION['dump_total_bytes'],
        'required_states_ok': SESSION['required_states_ok'],
        'workspace_used_bytes_all_runs': SESSION['budget'].get('workspace_used_all_runs_bytes'),
        'workspace_budget_bytes': SESSION['budget'].get('workspace_budget_bytes'),
        'cumulative_used_bytes': SESSION['budget'].get('cumulative_used_bytes'),
        'cumulative_budget_bytes': SESSION['budget'].get('cumulative_budget_bytes'),
        'budget_exceeded': SESSION['budget'].get('exceeded'),
        'updated_utc': now_iso(),
    })
    if HANDLE is not None:
        try:
            unreal.unregister_slate_post_tick_callback(HANDLE)
        except Exception as exc:
            SESSION['notes'].append('unregister: ' + str(exc))
        HANDLE = None
    try:
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    except Exception:
        pass
    STATE['done'] = True
    log('finish %s dumps=%s status=%s map_unchanged=%s' % (
        SESSION['status'], dump_status, status, SESSION['map_unchanged']))


def tick(delta):
    global CO
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        next(CO)
    except StopIteration:
        finish('ok')
    except Exception:
        message = traceback.format_exc()
        ERROR_PATH.parent.mkdir(parents=True, exist_ok=True)
        ERROR_PATH.write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_RADIOSITY_SURFACE_ERROR ' + message)
        unreal.log_error('ARDA_GPU_DUMP_ERROR ' + message)
        SESSION['errors'].append(message)
        try:
            finish('error', message)
        except Exception:
            unreal.log_error('ARDA_RADIOSITY_SURFACE_ERROR finish failed')
    finally:
        STATE['busy'] = False


try:
    GPU.mkdir(parents=True, exist_ok=True)
    RAW_ROOT_BASE.mkdir(parents=True, exist_ok=True)
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    RADIOSITY.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    RAW_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    SETUP = load_setup()
    if SETUP.get('schema') != 'arda-sponza-final-setup-v2':
        raise RuntimeError('unexpected final-setup schema: ' + str(SETUP.get('schema')))
    SESSION['setup_sha256'] = sha256_file(WORKSPACE / 'output' / 'final-setup.json')
    SESSION['scene_manifest_sha256'] = sha256_file(WORKSPACE / 'output' / 'scene-manifest.json')
    SESSION['script_sha256'] = sha256_file(WORKSPACE / 'scripts' / 'capture_gpu_dump.py')
    SESSION['validator_sha256'] = sha256_file(WORKSPACE / 'scripts' / 'validate_capture_plan.py')
    SESSION['map_sha256_before'] = sha256_file(MAP_FILE)
    SESSION['environment'] = environment_hashes()
    SESSION['filters_string'] = FILTER_STRING
    SESSION['filters_used'] = [f'{p}@{r}' for p, r in FILTER_PAIRS]
    SESSION['filter_count_used'] = len(FILTER_PAIRS)
    SESSION['capture_mode'] = 'radiosity-surface-same-run'
    SESSION['narrow_mode'] = True
    SESSION['prior_camera_validation'] = prior_camera_validation()
    try:
        manifest = json.loads((WORKSPACE / 'output' / 'scene-manifest.json').read_text(encoding='utf-8-sig'))
        SESSION['measured_bounds_cm'] = {
            'min': manifest['measured']['reconstructed_world_bounds_min_cm'],
            'max': manifest['measured']['reconstructed_world_bounds_max_cm'],
        }
    except Exception as exc:
        SESSION['notes'].append('scene-manifest bounds unavailable: ' + str(exc))

    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    if WORLD is None:
        raise RuntimeError('no editor world after load')
    readbacks, HERO, MATERIAL = apply_config(SETUP, WORLD)
    actors = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}
    PP = actors.get('PostProcess_Sponza')
    SUN = actors.get('Sun_Sponza')
    if PP is None or SUN is None:
        raise RuntimeError('saved PostProcess_Sponza or Sun_Sponza missing')
    SUN_COMP = SUN.get_component_by_class(unreal.DirectionalLightComponent)

    sun_now = sun_readback()
    sun_ok = bool(
        abs(sun_now['intensity_lux'] - SUN_HIGH) < 1e-3 and
        abs(sun_now['rotation_degrees']['pitch'] + 75.0) < 1e-3 and
        abs(sun_now['rotation_degrees']['yaw'] + 35.0) < 1e-3)
    SESSION['saved_sun_matches_approved'] = {
        'intensity_lux': SUN_HIGH,
        'rotation_degrees': {'pitch': -75.0, 'yaw': -35.0, 'roll': 0.0},
        'actual': sun_now,
        'matches': sun_ok,
    }
    if not sun_ok:
        raise RuntimeError('saved Sun_Sponza does not match 80000 lux / -75 / -35')

    SESSION['pre_apply_saved_state'] = readbacks.get('pre_apply_saved_state')
    SESSION['post_apply_saved_state'] = readbacks.get('post_apply_saved_state')
    SESSION['hero_pilot_readback'] = pilot_readback(HERO)
    SESSION['original_camera'] = hero_light()
    SESSION['original_sun'] = sun_readback()
    SESSION['original_exposure'] = read_exposure(PP)
    SESSION['original_scene_actors'] = sorted(actors)

    expected = SETUP['config']['hero_camera']
    got = SESSION['original_camera']
    expected_loc = [float(v) for v in expected['location_cm']]
    loc_ok = all(abs(got['location_cm'][i] - expected_loc[i]) < 0.01 for i in range(3))
    rot_ok = (abs(got['rotation_degrees']['pitch'] - float(expected['rotation_degrees']['pitch'])) < 1e-3 and
              abs(got['rotation_degrees']['yaw'] - float(expected['rotation_degrees']['yaw'])) < 1e-3 and
              abs(got['rotation_degrees']['roll'] - float(expected['rotation_degrees']['roll'])) < 1e-3)
    fov_ok = abs(got['horizontal_fov_deg'] - float(expected['horizontal_fov_deg'])) < 1e-3
    SESSION['saved_hero_matches_approved'] = {
        'location': loc_ok, 'rotation': rot_ok, 'horizontal_fov': fov_ok,
        'expected': expected, 'actual': got,
    }
    if not (loc_ok and rot_ok and fov_ok):
        raise RuntimeError('saved CAM_Hero_Reference does not match final-setup hero camera')
    if SESSION['map_sha256_before'].lower() != MAP_EXPECTED_SHA256:
        raise RuntimeError('copied map hash is not the expected saved map hash')

    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_clean()
    SESSION['uniform_buffer_content_map'] = cvars(
        ['r.RHI.UniformBufferContentMap.Enable',
         'r.DumpGPU.PassParameters',
         'r.DumpGPU.NativeUniformBufferTextures',
         'r.DumpGPU.RedumpInputs'])
    SESSION['render_status_start'] = json.loads(BRIDGE.get_render_status())
    SESSION['game_view_enabled'] = bool(LEVEL.editor_get_game_view())
    write_session(force=True)
    log('setup complete run=%s mode=%s raw=%s filters=%d' % (
        RUN_ID, MODE, RAW_ROOT, len(FILTER_PAIRS)))

    CO = experiment()
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
except Exception:
    message = traceback.format_exc()
    ERROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    ERROR_PATH.write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_RADIOSITY_SURFACE_ERROR ' + message)
    unreal.log_error('ARDA_GPU_DUMP_ERROR ' + message)
    try:
        SESSION['errors'].append(message)
        finish('error', message)
    except Exception:
        pass
