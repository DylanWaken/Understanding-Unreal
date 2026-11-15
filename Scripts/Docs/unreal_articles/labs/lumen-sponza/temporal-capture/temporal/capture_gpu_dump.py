#!/usr/bin/env python3
"""Bounded temporal Lumen capture on the retained Sponza teaching scene.

The experiment is deliberately one bridge viewport draw per engine frame so the
two counters stay separable:

  * bridge draw counter: our own count of BRIDGE.render_viewport/capture calls;
  * engine frame counter: unreal.SystemLibrary.get_frame_count().

The engine can render more than one scene graph per engine frame.  Every draw is
written to a timeline with both counters.  After a selected DumpGPU frame
finishes, its Base/Passes.json "Frame N" scopes are matched back to that
timeline, so the state actually dumped is recorded instead of assumed.

Data is selected, not a complete 88 GiB frame.  DumpGPU is invoked with
-unified_filter so only the Lumen screen-probe, radiance-cache, card/page and
per-state View/depth resources are read back.  The filter pairs live in
scripts/validate_capture_plan.py and are statically checked against the engine
source and an existing complete dump used as a size oracle.

Run with the unchanged known-working launcher:
  scripts/run_gpu_dump.ps1 -Workspace D:\\UnrealUnderstanding-Temporal
The launcher supplies the required startup ini override
r.RHI.UniformBufferContentMap.Enable=1.  This script does not save the map.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path

_SCRIPT_WORKSPACE = (Path(__file__).resolve().parents[1]
                     if '__file__' in globals()
                     else Path(r'D:\UnrealUnderstanding-Temporal'))
_ENV_WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', str(_SCRIPT_WORKSPACE)))
# The script lives next to the project it owns; prefer that over an inherited
# environment pointer so an unchanged launcher cannot silently target Sponza.
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
    sha256_file, MAP_PATH, MAP_FILE, OUTPUT, LOGS,
)
from validate_capture_plan import (  # noqa: E402
    filters, all_resources, PROBE_RESOURCES, RADIANCE_RESOURCES,
    CARD_RESOURCES, BUDGET_BYTES,
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
TARGETS = [int(x) for x in
           os.environ.get('ARDA_TEMPORAL_TARGETS', '1,8,32,64').split(',')
           if x.strip()]
CAMERA_MOVE_CM = 150.0
# The main run keeps the six-family selected filter.  A camera-retry run uses a
# deliberately narrow filter so that it stays inside the remaining 20 GiB budget
# after the first attempt.  The retry is a repair for views that rendered empty.
MODE = os.environ.get('ARDA_TEMPORAL_MODE', 'full').strip().lower()
CAMERA_RETRY = MODE == 'camera-retry'
CAMERA_PROBE_OFFSETS = [150.0, -150.0, 100.0, -100.0, 75.0, -75.0,
                        50.0, -50.0, 25.0, -25.0]
NARROW_FILTER_PAIRS = [
    ('*DepthPassParallel*', 'ViewData.PrimaryViews'),
    ('*BasePassParallel*', 'ViewData.PrimaryViews'),
    ('*DepthPassParallel*', 'SceneDepthZ'),
    ('*ScreenProbeGatherScreenData*', 'Lumen.ScreenProbeGather.*'),
    ('*ScreenProbeIntegrate*', 'Lumen.ScreenProbeGather.*'),
    ('*TemporalReprojection*', 'Lumen.ScreenProbeGather.*'),
    ('*DiffuseIndirectComposite*', 'Lumen.ScreenProbeGather.*'),
    ('*UpdateCacheForUsedProbes*', 'Lumen.RadianceCache.*'),
    ('*AllocateUsedProbes*', 'Lumen.RadianceCache.*'),
    ('*CalculateProbeIrradiance*', 'Lumen.RadianceCache.*'),
    ('*TraceFromProbes*', 'Lumen.RadianceCache.*'),
    ('*ConvertToSH*', 'Lumen.Radiosity.*'),
    ('*Build*cards*', 'Lumen.Cards'),
    ('*Build*cards*', 'Lumen.PageTable'),
    ('*Build*cards*', 'Lumen.PageBuffer'),
    ('*Build*cards*', 'Lumen.MeshCards'),
    ('*CardPage*', 'Lumen.CardPageLastUsedBuffer'),
    ('*Build*cards*', 'Lumen.CardCaptureRects'),
]
NARROW_FILTER_STRING = '|'.join(f'{p}@{r}' for p, r in NARROW_FILTER_PAIRS)
DUMP_TIMEOUT_SECONDS = 10 * 60
SCREENSHOT_TIMEOUT_SECONDS = 180
RESOLUTION = (1280, 720)

GPU = WORKSPACE / 'output' / 'gpu-dump'
RAW_ROOT_BASE = GPU / 'raw'
RUN_ID = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
if (RAW_ROOT_BASE / RUN_ID).exists():
    RUN_ID = RUN_ID + '-' + str(int(time.time()) % 1000)
RAW_ROOT = RAW_ROOT_BASE / RUN_ID
TEMPORAL = WORKSPACE / 'output' / 'temporal'
IMAGES = TEMPORAL / 'images'
DEBUG_IMAGES = TEMPORAL / 'debug'
CAMERA_PROBES = TEMPORAL / 'camera-probes'
SESSION_PATH = TEMPORAL / f'run-{RUN_ID}.json'
TIMELINE_PATH = TEMPORAL / f'timeline-{RUN_ID}.json'
LATEST_PATH = GPU / 'latest-run.json'
CHECKPOINT_PATH = TEMPORAL / 'checkpoint.json'
ERROR_PATH = LOGS / f'temporal-{RUN_ID}-error.txt'
CAMERA_VALIDATION_PATH = TEMPORAL / 'camera-move-validation.json'
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
ACTIVE_FILTER_PAIRS = NARROW_FILTER_PAIRS if CAMERA_RETRY else FILTER_PAIRS
DUMP_FILTER_STRING = '|'.join(f'{p}@{r}' for p, r in ACTIVE_FILTER_PAIRS)
EXPECTED_RESOURCES = all_resources()

# One-selector state.  The generator below is advanced once per slate tick.
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
    'schema': 'arda-sponza-temporal-selected-v1',
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
    'targets_bridge_draws': TARGETS,
    'targets_definition': ('renders since the light change or camera move; '
                           'bridge viewport draws, not engine frames'),
    'mode': MODE,
    'camera_move_cm': CAMERA_MOVE_CM,
    'filters': [f'{p}@{r}' for p, r in ACTIVE_FILTER_PAIRS],
    'filter_count': len(ACTIVE_FILTER_PAIRS),
    'full_filter_count': len(FILTER_PAIRS),
    'expected_resources': EXPECTED_RESOURCES,
    'budget_bytes': BUDGET_BYTES,
    'budget_gib': round(BUDGET_BYTES / 1024 ** 3, 3),
    'phases': {},
    'timeline': [],
    'dumps': [],
    'clean': [],
    'debug': [],
    'camera': {},
    'camera_states': [],
    'camera_probe': None,
    'skips': [],
    'errors': [],
    'notes': [],
    'limitations': [
        'DumpGPU is used with -unified_filter; only selected RDG resources are read back. '
        'Resources never referenced by a selected pass, memoryless resources, shader-local '
        'registers and writable SkipTracking buffers are not present and are reported by name.',
        'A bridge viewport draw is not an engine frame. The engine can render several scene '
        'graphs in one engine frame; both counters are recorded and the dumped frame is matched '
        'to the timeline after the fact. An editor auto-render not initiated by the script is '
        'not in the bridge timeline; the dump graph count is recorded and any unmatched graph is '
        'reported, never silently assigned to the target.',
        'Debug visualizations are display-encoded editor views and are separate observations, '
        'not same-frame raw buffers.',
        'SH coefficients and world-cache samples are native readbacks. Any later glyph projection '
        'must use the ViewData.PrimaryViews buffer captured in the same selected dump.',
        'Screen-probe SH3 (nine-coefficient sphere average), radiance-cache directional radiance '
        'and card-radiosity SH (four-coefficient PDF-weighted hemisphere irradiance) use different '
        'conventions; decode and label them separately, never renormalise one as another.',
        'The map is restored in memory only; it is never saved.',
    ],
}


def log(message):
    unreal.log('ARDA_TEMPORAL_SELECTED ' + str(message))


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


def write_checkpoint():
    atomic_json(CHECKPOINT_PATH, {
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
        'schema': 'arda-sponza-temporal-timeline-v1',
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


def set_depth_mode():
    try:
        BRIDGE.set_viewport_mode('buffer', 'SceneDepth')
    except Exception as exc:
        SESSION['notes'].append('set_viewport_mode SceneDepth: ' + str(exc))
    console('r.BufferVisualizationTarget SceneDepth')


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
    'r.Lumen.RadianceCache.VisualizeRadiusScale 1.0',
    'r.Lumen.RadianceCache.VisualizeClipmapIndex -1',
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


DEBUG_VIEWS = [
    ('probe-placement', [
        'r.Lumen.ScreenProbeGather.Debug 1',
        'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1',
        'r.ShaderPrint 1',
    ], 'lit'),
    ('probe-history', ['r.Lumen.Visualize 23'], 'lit'),
    ('radiosity-probes', [
        'r.LumenScene.Radiosity.VisualizeProbes 1',
        'r.LumenScene.Radiosity.VisualizeProbeRadius 5',
        'r.LumenScene.Radiosity.VisualizeProbes.ShowInvalid 1',
    ], 'lit'),
    ('radiosity-history', ['r.Lumen.Visualize 24'], 'lit'),
    ('worldcache-probes', [
        'r.Lumen.RadianceCache.Visualize 1',
        'r.Lumen.RadianceCache.VisualizeRadiusScale 1.0',
        'r.Lumen.RadianceCache.VisualizeClipmapIndex -1',
    ], 'lit'),
    ('card-placement', [
        'r.Lumen.Visualize 3',
        'r.Lumen.Visualize.CardPlacement 1',
        'r.Lumen.Visualize.CardPlacementDistance 5000',
        'r.Lumen.Visualize.CardPlacementLOD 0',
        'r.Lumen.Visualize.CardPlacementPrimitives 1',
    ], 'lit'),
    ('lumen-scene', ['r.Lumen.Visualize 3'], 'lit'),
    ('surface-cache', ['r.Lumen.Visualize 5'], 'lit'),
    ('depth', [], 'depth'),
]

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


def capture_png(path, label, note, **extra):
    path = Path(path)
    while not frame_ready():
        yield
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    before_frame = engine_frame()
    before_phase = STATE['phase_draw']
    STATE['subphase'] = label
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    STATE['phase_draw'] += 1
    STATE['last_render_frame'] = engine_frame()
    timeline_add('capture', note, label=label, path=str(path),
                 phase_draw_before=before_phase,
                 phase_draw_after=STATE['phase_draw'])
    deadline = time.monotonic() + SCREENSHOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    if not path.exists() or path.stat().st_size <= 2048:
        raise RuntimeError('screenshot not written: ' + str(path))
    rec = {
        'label': label,
        'note': note,
        'phase': STATE['phase'],
        'phase_draw': STATE['phase_draw'],
        'phase_draw_before_render': before_phase,
        'capture_draw_total': STATE['draws'],
        'engine_frame_before': before_frame,
        'engine_frame_after': engine_frame(),
        'sun': sun_readback(),
        'camera': hero_light(),
        'pilot': pilot_readback(HERO),
        'cvars': cvars(CLEAN_CVAR_NAMES),
        'file': str(path),
        'bytes': path.stat().st_size,
        'sha256': sha256_file(path),
        'view_mode_index': json.loads(BRIDGE.get_render_status()).get('view_mode_index'),
    }
    try:
        rec.update(pixel_metrics(path))
    except Exception as exc:
        rec['pixel_metrics_error'] = str(exc)
    rec.update(extra)
    # A rendered view that is essentially one colour is not evidence of the
    # scene.  Record it explicitly instead of quietly treating it as a state;
    # depth visualisations are display-encoded and may legitimately be dark.
    is_depth_view = ('depth' in label.lower() or
                     rec.get('debug_view') == 'depth')
    std = rec.get('std_display_luma')
    mean = rec.get('mean_display_luma')
    black = rec.get('near_black_fraction')
    empty_view = bool(
        rec.get('pixel_metrics_error') or
        std is None or
        (std < 1.0) or
        (mean is not None and mean < 1.0) or
        (black is not None and black > 0.98))
    rec['view_kind'] = 'depth' if is_depth_view else 'lit'
    if is_depth_view:
        rec['empty_view'] = False
        rec['view_valid'] = bool(std is not None and std >= 2.0 and
                                 rec.get('bytes', 0) >= 20000)
        rec['view_status'] = 'depth-ok' if rec['view_valid'] else 'depth-empty'
    else:
        rec['empty_view'] = empty_view
        rec['view_valid'] = not empty_view
        rec['view_status'] = 'empty' if empty_view else 'ok'
    write_session()
    log('capture %s phase=%s draw=%s frame=%s' % (
        label, rec['phase'], rec['phase_draw'], rec['engine_frame_after']))
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
    SESSION['notes'].append('sun intensity -> %.1f at draw %d frame %s' % (
        float(lux), STATE['draws'], engine_frame()))
    timeline_add('sun', 'intensity %.1f' % float(lux), requested_lux=float(lux))
    log('sun -> %.1f' % float(lux))


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
    """All selected raw directories, so a retry cannot exceed the total cap."""
    return dir_bytes(RAW_ROOT_BASE) if RAW_ROOT_BASE.exists() else 0


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
        'cvars': {},
    }
    applied = {}
    for name, value in DUMP_CVARS:
        console('%s %s' % (name, value))
        applied[name] = {'requested': value, 'actual': cvars([name]).get(name)}
    console('r.DumpGPU.Directory ' + str(RAW_ROOT))
    command = ('DumpGPU -path="%s" -noexplorer -oneframe '
               '-unified_filter="%s"' % (directory, DUMP_FILTER_STRING))
    returned = console(command)
    STATE['dump'].update({
        'command': command,
        'command_returned': returned,
        'filter_count': len(ACTIVE_FILTER_PAIRS),
        'dump_cvars_applied': applied,
        'dump_cvars_actual': cvars([n for n, _ in DUMP_CVARS]),
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


def selected_resource_report(base):
    descs = list(stream_json(base / 'ResourceDescs.json')) if (base / 'ResourceDescs.json').exists() else []
    readbacks = list(stream_json(base / 'DumpReadbackStatus.json')) if (base / 'DumpReadbackStatus.json').exists() else []
    by_name = defaultdict(list)
    for d in descs:
        by_name[d.get('Name', '')].append(d)
    rb_by_key = defaultdict(list)
    for rb in readbacks:
        m = re.search(r'Resources/([0-9a-f]+)', rb.get('File', ''))
        if m:
            rb_by_key[m.group(1)].append(rb)

    res_dir = base / 'Resources'
    if not res_dir.exists():
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
        ds = by_name.get(name, [])
        if not ds:
            missing.append({
                'name': name,
                'reason': 'no RDG descriptor with this exact name in the selected frame; '
                          'it was not created or not bound to a selected pass',
            })
            continue
        versions = []
        readback_files = set()
        for d in ds:
            key = d.get('UniqueResourceName')
            for rb in rb_by_key.get(key, []):
                produced = re.search(r'\.v([0-9a-f]{20})\.', rb.get('File', ''))
                readback_files.add(rb.get('File'))
                versions.append({
                    'key': key,
                    'file': rb.get('File'),
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
                })
            for f in direct_files(key):
                rel = f['relative_file']
                if any(v.get('file') and str(v['file']).endswith(rel) for v in versions):
                    continue
                f['descriptor_bytes'] = d.get('ByteSize')
                f['desc'] = d.get('Desc')
                versions.append(f)
        # A payload is either a Dumped readback entry or a diagnostic resource
        # file.  SkipTracking buffers are written by the diagnostic compute path
        # with a zero producer pointer and are intentionally absent from
        # DumpReadbackStatus.json; the bytes still exist and are real readbacks.
        dumped = [v for v in versions
                  if ((v.get('status') == 'Dumped' and (v.get('bytes') or 0) > 0) or
                      (v.get('evidence') == 'resources-directory' and (v.get('bytes') or 0) > 0))]
        rec = {
            'name': name,
            'descriptor_count': len(ds),
            'versions': versions,
            'dumped_version_count': len(dumped),
            'readback_version_count': sum(1 for v in versions if v.get('evidence') == 'readback-status'),
            'direct_file_version_count': sum(1 for v in versions if v.get('evidence') == 'resources-directory'),
            'dumped_bytes': sum((v.get('bytes') or 0) for v in dumped),
            'present': bool(dumped),
            'evidence': sorted({v.get('evidence') for v in dumped}),
        }
        if dumped:
            present[name] = rec
        else:
            has_readbacks = any(v.get('evidence') == 'readback-status' for v in versions)
            rec['reason'] = (
                'descriptor exists but the selected readback entry is not Dumped and '
                'no Resources/<key> file exists; see DumpReadbackStatus entries'
                if has_readbacks else
                'descriptor exists but neither a Dumped readback entry nor a '
                'Resources/<key> diagnostic file exists for a selected version')
            missing.append(rec)
    return {
        'present': present,
        'missing': missing,
        'present_count': len(present),
        'missing_count': len(missing),
        'descriptor_count': len(descs),
        'readback_count': len(readbacks),
        'readback_status_counts': dict(Counter(r.get('Status') for r in readbacks)),
        'direct_file_count': len(res_files),
        'direct_file_evidence_note': (
            'SkipTracking buffers are read through the diagnostic path; that path '
            'writes Resources/<key>[.v<producer>].bin with a zero producer pointer '
            'and does not append a DumpReadbackStatus entry. Files are counted as '
            'real payloads even when the status JSON has no row for them.'),
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
            'unique_pass_names': sorted({p.get('EventName') for p in passes if p.get('EventName')}),
            'selected_resources': resources,
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


def finalize_dump(lit_record, depth_record):
    dump = STATE['dump']
    if dump is None:
        raise RuntimeError('finalize_dump without STATE dump')
    meta = parse_dump(dump['directory'])
    match = match_timeline(set(meta.get('engine_frames') or []), dump['phase'])
    target = dump.get('target')
    actual = match.get('actual_phase_draw')
    rec = {
        'label': dump['label'],
        'phase': dump['phase'],
        'target_phase_draw': target,
        'arm_phase_draw': dump.get('arm_phase_draw'),
        'arm_draw_total': dump.get('arm_draw_total'),
        'arm_engine_frame': dump.get('arm_engine_frame'),
        'command': dump.get('command'),
        'command_returned': dump.get('command_returned'),
        'status': meta.get('status'),
        'seconds': dump.get('seconds'),
        'keepalive_draws': dump.get('keepalive_draws', 0),
        'raw_directory': dump['directory'],
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
        'file_count': meta.get('file_count'),
        'total_bytes': meta.get('total_bytes'),
        'total_gib': meta.get('total_gib'),
        'metadata_error': meta.get('metadata_error'),
        'lit_record': lit_record,
        'depth_record': depth_record,
        'dump_cvars_actual': dump.get('dump_cvars_actual'),
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


def capture_clean(tag, note):
    set_clean()
    lit = yield from capture_png(IMAGES / f'{tag}-lit.png', f'{tag}-lit',
                                 note + ' (clean lit)', kind='clean')
    SESSION['clean'].append(lit)
    set_depth_mode()
    depth = yield from capture_png(IMAGES / f'{tag}-depth.png', f'{tag}-depth',
                                   note + ' (SceneDepth buffer visualization)', kind='clean')
    SESSION['clean'].append(depth)
    set_lit_mode()
    set_clean()
    return lit, depth


def dump_state(label, target, phase, note):
    used = workspace_raw_bytes()
    if used > BUDGET_BYTES:
        STATE['skips'].append({'label': label, 'reason': 'budget exceeded',
                               'used_bytes': used, 'budget_bytes': BUDGET_BYTES})
        SESSION['skips'].append(STATE['skips'][-1])
        log('skip dump %s: budget %d > %d' % (label, used, BUDGET_BYTES))
        return
    if target is not None:
        pre = max(int(target) - 2, 0)
        if STATE['phase_draw'] < pre:
            yield from advance_to(pre)
    # The dump frame must be lit; a previous state capture may have left the
    # viewport in SceneDepth buffer-visualisation mode.
    set_clean()
    arm_dump(label, target, phase)
    yield
    if frame_ready():
        render_once('dump-filler')
    yield
    lit = yield from capture_png(IMAGES / f'{label}-dump-lit.png', f'{label}-lit',
                                 note + ' (target lit)', kind='clean')
    SESSION['clean'].append(lit)
    dump_failed = None
    try:
        yield from wait_dump()
    except Exception as exc:
        dump_failed = str(exc)
        SESSION['errors'].append('%s: %s' % (label, dump_failed))
        if STATE['dump'] is not None:
            STATE['dump']['status'] = 'failed'
            STATE['dump']['error'] = dump_failed
        log('dump %s failed but screenshots/debug continue: %s' % (label, dump_failed))
    yield
    # Depth is a separate display observation and needs its own mode switch.
    set_depth_mode()
    depth = yield from capture_png(IMAGES / f'{label}-dump-depth.png', f'{label}-depth',
                                   note + ' (SceneDepth buffer visualisation, one update later)',
                                   kind='clean')
    SESSION['clean'].append(depth)
    set_lit_mode()
    set_clean()
    rec = finalize_dump(lit, depth)
    if dump_failed:
        rec['status'] = 'failed'
        rec['error'] = dump_failed
        write_session(force=True)
    yield


def debug_views(tag, note):
    for name, commands, mode in DEBUG_VIEWS:
        set_clean()
        if mode == 'depth':
            set_depth_mode()
        else:
            set_lit_mode()
        for command in commands:
            console(command)
        yield from advance_to(STATE['phase_draw'] + 2)
        rec = yield from capture_png(DEBUG_IMAGES / f'{tag}-{name}.png',
                                     f'{tag}-{name}',
                                     note + ' debug view %s; separate observation, '
                                            'not a same-frame raw buffer' % name,
                                     kind='debug', debug_view=name,
                                     debug_commands=commands)
        SESSION['debug'].append(rec)
        set_clean()
        set_lit_mode()
        yield


def capture_state_images(tag, note):
    """Clean lit + correctly switched SceneDepth view at the current update."""
    set_clean()
    lit = yield from capture_png(IMAGES / f'{tag}-lit.png', f'{tag}-lit',
                                 note + ' (clean lit)', kind='clean')
    SESSION['clean'].append(lit)
    set_depth_mode()
    depth = yield from capture_png(IMAGES / f'{tag}-depth.png', f'{tag}-depth',
                                   note + ' (SceneDepth buffer visualisation)', kind='clean')
    SESSION['clean'].append(depth)
    set_lit_mode()
    set_clean()
    return lit, depth


def set_camera_offset(offset):
    origin = [float(x) for x in SESSION['original_camera']['location_cm']]
    target = [origin[0], origin[1] + float(offset), origin[2]]
    HERO.set_actor_location(unreal.Vector(*target), False, True)
    pilot(HERO)
    readback = hero_light()
    applied = all(abs(readback['location_cm'][i] - target[i]) < 0.5 for i in range(3))
    return {
        'offset_cm': float(offset),
        'start_cm': origin,
        'target_cm': target,
        'applied': applied,
        'readback': readback,
        'pilot': pilot_readback(HERO),
        'applied_engine_frame': engine_frame(),
        'applied_utc': now_iso(),
    }


def restore_original_camera():
    cam = SESSION['original_camera']
    HERO.set_actor_location(unreal.Vector(*[float(x) for x in cam['location_cm']]), False, True)
    HERO.set_actor_rotation(unreal.Rotator(
        pitch=float(cam['rotation_degrees']['pitch']),
        yaw=float(cam['rotation_degrees']['yaw']),
        roll=float(cam['rotation_degrees']['roll'])), False)
    pilot(HERO)
    return hero_light()


def camera_offset_traces(offset):
    origin = [float(x) for x in SESSION['original_camera']['location_cm']]
    target = [origin[0], origin[1] + float(offset), origin[2]]
    rays = []
    for dx, dz in ((0, 0), (10, 0), (-10, 0), (0, 10), (0, -10)):
        a = [origin[0] + dx, origin[1], origin[2] + dz]
        b = [target[0] + dx, target[1], target[2] + dz]
        rays.append(line_trace(a, b))
    return {
        'start_cm': origin,
        'target_cm': target,
        'rays': rays,
        'blocked_ray_count': sum(1 for r in rays if r.get('blocking') is True),
        'unknown_ray_count': sum(1 for r in rays if not r.get('available')),
        'trace_clear': bool(rays) and not any(r.get('blocking') is True for r in rays) and
                       not any(not r.get('available') for r in rays),
    }


def probe_camera_offsets():
    """Test candidate lateral moves and reject any view that renders empty.

    The offline edge-clearance test cannot see that a point is inside a wall:
    it measures distance to triangle edges, not to triangle interiors.  The
    rendered image is the decisive check, and every failed candidate keeps its
    metrics so the failure is visible rather than silently replaced.
    """
    probes = []
    chosen = None
    for offset in CAMERA_PROBE_OFFSETS:
        trace = camera_offset_traces(offset)
        move = set_camera_offset(offset)
        yield from advance_to(STATE['phase_draw'] + 2)
        label = 'Y%+04d' % int(offset)
        rec = yield from capture_png(CAMERA_PROBES / f'{label}-lit.png',
                                     label + '-lit',
                                     'camera candidate %+d cm; rendered validity check'
                                     % int(offset), kind='probe',
                                     camera_offset_cm=offset)
        probe = {
            'offset_cm': float(offset),
            'trace': trace,
            'move': move,
            'image': rec,
            'view_valid': bool(rec.get('view_valid')),
            'empty_view': bool(rec.get('empty_view')),
            'accepted': False,
        }
        probe['accepted'] = bool(move.get('applied') and trace['trace_clear'] and
                                 rec.get('view_valid'))
        probes.append(probe)
        SESSION['notes'].append(
            'camera probe %+d cm: applied=%s trace_clear=%s view_valid=%s mean=%.3f std=%.3f'
            % (int(offset), move.get('applied'), trace['trace_clear'],
               rec.get('view_valid'), rec.get('mean_display_luma') or -1,
               rec.get('std_display_luma') or -1))
        restore_original_camera()
        yield from advance_to(STATE['phase_draw'] + 1)
        if probe['accepted'] and chosen is None:
            chosen = probe
            break
    return {'candidates': probes, 'chosen': chosen,
            'candidate_order_cm': CAMERA_PROBE_OFFSETS,
            'method': ('rendered lit frame plus five visibility line traces for every '
                       'candidate; a candidate is accepted only when the frame is not '
                       'empty and the traces are clear')}


def camera_retry_experiment():
    """Repair the camera experiment after the planned +150 cm view rendered empty.

    Uses the same accepted scene/exposure/sun and the same selected-data rules,
    but only one narrow DumpGPU frame at the settled camera state so the total
    selected raw budget stays below 20 GiB.
    """
    set_clean()
    set_phase('retry-warm', 'warm before the camera-move retry')
    yield from warm(60)
    yield from capture_clean('retry-baseline', 'baseline before the camera retry')
    SESSION['camera_probe'] = yield from probe_camera_offsets()
    probe = SESSION['camera_probe']
    write_session(force=True)
    chosen = probe.get('chosen')
    if chosen is None:
        SESSION['errors'].append('no tested camera offset produced a non-empty view')
        yield from debug_views('camera-refused', 'camera retry refused; baseline view')
        restore_original_camera()
        set_phase('final', 'original camera restored after a refused retry')
        yield from warm(FINAL_WARM_DRAWS)
        yield from capture_clean('retry-final', 'restored original camera')
        SESSION['phases_finished'] = sorted(SESSION['phases'])
        yield
        return

    applied = set_camera_offset(chosen['offset_cm'])
    SESSION['camera_move'] = {
        'applied': applied.get('applied'),
        'direction': '+Y' if chosen['offset_cm'] > 0 else '-Y',
        'offset_cm': chosen['offset_cm'],
        'target_cm': applied['target_cm'],
        'start_cm': applied['start_cm'],
        'readback': applied['readback'],
        'pilot': applied['pilot'],
        'chosen_from_probe': True,
        'offline_validation': 'edge-only check was insufficient; rendered view is authoritative',
    }
    SESSION['camera']['move'] = SESSION['camera_move']
    set_phase('camera', 'validated %+d cm lateral camera move' % int(chosen['offset_cm']))
    SESSION['camera']['moved_utc'] = now_iso()
    SESSION['camera']['moved_engine_frame'] = engine_frame()
    SESSION['camera']['moved_draw_total'] = STATE['draws']

    SESSION['camera_states'] = []
    for target in [t for t in TARGETS if t < 64]:
        yield from advance_to(max(int(target) - 1, 0))
        lit, depth = yield from capture_state_images(
            'camera-%03d' % target,
            'lateral camera %+d cm, target update %d'
            % (int(chosen['offset_cm']), target))
        SESSION['camera_states'].append({
            'target_update': target,
            'actual_update': lit.get('phase_draw'),
            'lit': lit,
            'depth': depth,
        })
    if 64 in TARGETS:
        yield from dump_state('camera-064', 64, 'camera',
                              'lateral camera %+d cm, target update 64'
                              % int(chosen['offset_cm']))
    yield from debug_views('camera-settled', 'camera-move settled state (retry)')

    restore_original_camera()
    set_sun(SUN_HIGH)
    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_phase('final', 'original camera/sun/exposure restored in memory')
    yield from warm(FINAL_WARM_DRAWS)
    yield from capture_clean('retry-final', 'restored original camera and sun')
    SESSION['phases_finished'] = sorted(SESSION['phases'])
    yield


# ---------------------------------------------------------------------------
# camera move validation (real geometry + runtime collision trace)
# ---------------------------------------------------------------------------
def _hit_record(raw):
    hit = None
    blocking = None
    if isinstance(raw, tuple):
        for item in raw:
            if hasattr(item, 'b_blocking_hit'):
                hit = item
            elif isinstance(item, (bool, int)):
                blocking = bool(item)
    elif hasattr(raw, 'b_blocking_hit'):
        hit = raw
    else:
        blocking = bool(raw)
    if hit is not None:
        try:
            blocking = bool(hit.b_blocking_hit)
        except Exception:
            pass
    rec = {'blocking': blocking}
    if hit is not None:
        for attr in ('location', 'impact_point', 'actor', 'component'):
            try:
                value = getattr(hit, attr)
                if attr in ('location', 'impact_point'):
                    rec[attr] = [round(float(value.x), 4), round(float(value.y), 4),
                                 round(float(value.z), 4)]
                elif value is not None:
                    rec[attr] = value.get_name() if hasattr(value, 'get_name') else str(value)
            except Exception:
                pass
    return rec


def line_trace(start, end):
    try:
        raw = unreal.SystemLibrary.line_trace_single(
            WORLD, unreal.Vector(float(start[0]), float(start[1]), float(start[2])),
            unreal.Vector(float(end[0]), float(end[1]), float(end[2])),
            unreal.TraceTypeQuery.TRACE_TYPE_QUERY1, True, [HERO],
            unreal.DrawDebugTrace.NONE, True)
    except Exception as exc:
        return {'available': False, 'error': str(exc)}
    rec = _hit_record(raw)
    rec['available'] = True
    rec['start'] = list(start)
    rec['end'] = list(end)
    return rec


def candidate_move(direction):
    start = [float(x) for x in SESSION['original_camera']['location_cm']]
    target = [start[0], start[1] + direction * CAMERA_MOVE_CM, start[2]]
    rays = []
    for dx, dz in ((0, 0), (10, 0), (-10, 0), (0, 10), (0, -10)):
        a = [start[0] + dx, start[1], start[2] + dz]
        b = [target[0] + dx, target[1], target[2] + dz]
        rays.append(line_trace(a, b))
    blocked = [r for r in rays if r.get('blocking') is True]
    unknown = [r for r in rays if not r.get('available')]
    offline = None
    if CAMERA_VALIDATION_PATH.exists():
        try:
            offline = json.loads(CAMERA_VALIDATION_PATH.read_text(encoding='utf-8'))
        except Exception as exc:
            offline = {'error': str(exc)}
    offline_clear = bool(offline and offline.get('clear') is True and direction > 0)
    clear = not blocked and (not unknown or offline_clear)
    return {
        'direction': '+Y' if direction > 0 else '-Y',
        'start_cm': start,
        'target_cm': target,
        'distance_cm': CAMERA_MOVE_CM,
        'probe_radius_cm': 10.0,
        'rays': rays,
        'blocked_ray_count': len(blocked),
        'unknown_ray_count': len(unknown),
        'offline_validation': offline,
        'offline_clear_for_this_direction': offline_clear,
        'clear': clear,
        'method': ('five parallel visibility line traces (centre +/-10 cm X/Z) plus the '
                   'offline split-geometry edge-clearance record for +Y'),
    }


def validate_and_apply_move():
    try:
        start = [float(x) for x in SESSION['original_camera']['location_cm']]
        # Keep 5 cm inside the measured scene bounds, even though the move is only 1.5 m.
        measured = SESSION.get('measured_bounds_cm') or {}
        candidates = []
        for direction in (1, -1):
            rec = candidate_move(direction)
            if measured:
                t = rec['target_cm']
                mn = measured.get('min', [-1e9, -1e9, -1e9])
                mx = measured.get('max', [1e9, 1e9, 1e9])
                rec['inside_measured_bounds'] = all(mn[i] + 5.0 <= t[i] <= mx[i] - 5.0
                                                    for i in range(3))
                rec['clear'] = bool(rec['clear'] and rec['inside_measured_bounds'])
            candidates.append(rec)
            if rec['clear']:
                break
        chosen = next((c for c in candidates if c['clear']), None)
        if chosen is None:
            return {'applied': False, 'reason': 'no 150 cm lateral candidate cleared the runtime trace',
                    'candidates': candidates}
        HERO.set_actor_location(
            unreal.Vector(*[float(x) for x in chosen['target_cm']]), False, True)
        pilot(HERO)
        readback = hero_light()
        applied = (abs(readback['location_cm'][0] - chosen['target_cm'][0]) < 0.5 and
                   abs(readback['location_cm'][1] - chosen['target_cm'][1]) < 0.5 and
                   abs(readback['location_cm'][2] - chosen['target_cm'][2]) < 0.5)
        chosen.update({
            'applied': applied,
            'readback': readback,
            'pilot': pilot_readback(HERO),
            'applied_utc': now_iso(),
            'applied_engine_frame': engine_frame(),
        })
        if not applied:
            chosen['reason'] = 'actor readback did not match the requested target'
            HERO.set_actor_location(unreal.Vector(*start), False, True)
            pilot(HERO)
        return chosen
    except Exception as exc:
        return {'applied': False, 'reason': 'camera move exception: ' + str(exc),
                'traceback': traceback.format_exc()}


# ---------------------------------------------------------------------------
# generator experiment
# ---------------------------------------------------------------------------
def experiment():
    set_clean()
    set_phase('warm-baseline', 'warm history before the baseline')
    yield from warm(WARM_DRAWS)
    set_phase('baseline', 'warm accepted baseline before any change')
    yield from capture_clean('baseline', 'warm accepted baseline')
    yield from dump_state('baseline', None, 'baseline',
                          'warm accepted baseline before the light step')
    yield from debug_views('baseline', 'warm accepted baseline')

    # Light step: sun 80000 -> 20000 lux, identical exposure.
    set_sun(SUN_LOW)
    set_phase('lightstep', 'sun 80000 -> 20000 lux')
    SESSION['lightstep'] = {
        'sun_from_lux': SUN_HIGH,
        'sun_to_lux': SUN_LOW,
        'changed_utc': now_iso(),
        'changed_engine_frame': engine_frame(),
        'changed_draw_total': STATE['draws'],
    }
    yield from advance_to(0)
    for target in TARGETS:
        if STATE['phase_draw'] > target:
            rec = {'phase': 'lightstep', 'target': target,
                   'reason': 'already past target when reached',
                   'actual_phase_draw': STATE['phase_draw']}
            STATE['skips'].append(rec)
            SESSION['skips'].append(rec)
            continue
        yield from dump_state('lightstep-%03d' % target, target, 'lightstep',
                              'sun %.0f->%.0f lux, target update %d' % (
                                  SUN_HIGH, SUN_LOW, target))
    yield from debug_views('lightstep-settled', 'settled low-light state')

    # Restore the baseline and show that it comes back.
    set_sun(SUN_HIGH)
    set_phase('restore-warm', 'restore sun 80000 lux and warm')
    yield from warm(RESTORE_WARM_DRAWS)
    set_phase('restored', 'baseline restored')
    yield from capture_clean('restored', 'baseline restored after the light step')

    # Camera move: baseline sun, identical exposure, 150 cm lateral.
    move = validate_and_apply_move()
    SESSION['camera_move'] = move
    SESSION['camera']['move'] = move
    write_session(force=True)
    if move.get('applied'):
        set_phase('camera', '150 cm lateral camera move at baseline sun')
        SESSION['camera']['moved_utc'] = now_iso()
        SESSION['camera']['moved_engine_frame'] = engine_frame()
        SESSION['camera']['moved_draw_total'] = STATE['draws']
        yield from advance_to(0)
        for target in TARGETS:
            if STATE['phase_draw'] > target:
                rec = {'phase': 'camera', 'target': target,
                       'reason': 'already past target when reached',
                       'actual_phase_draw': STATE['phase_draw']}
                STATE['skips'].append(rec)
                SESSION['skips'].append(rec)
                continue
            yield from dump_state('camera-%03d' % target, target, 'camera',
                                  'lateral camera move %+.0f cm, target update %d' % (
                                      float(move['target_cm'][1]) - float(move['start_cm'][1]),
                                      target))
        yield from debug_views('camera-settled', 'camera-move settled state')
    else:
        SESSION['notes'].append('camera move refused: ' + str(move.get('reason')))
        yield from debug_views('camera-refused', 'camera move refused; baseline view')

    # Restore in memory only.
    HERO.set_actor_location(
        unreal.Vector(*[float(x) for x in SESSION['original_camera']['location_cm']]),
        False, True)
    HERO.set_actor_rotation(
        unreal.Rotator(pitch=float(SESSION['original_camera']['rotation_degrees']['pitch']),
                       yaw=float(SESSION['original_camera']['rotation_degrees']['yaw']),
                       roll=float(SESSION['original_camera']['rotation_degrees']['roll'])),
        False)
    pilot(HERO)
    set_sun(SUN_HIGH)
    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_phase('final', 'original camera/sun/exposure restored in memory')
    yield from warm(FINAL_WARM_DRAWS)
    yield from capture_clean('final', 'restored original camera and sun')
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


def write_manifest():
    """Compact hand-off for the HTML temporal scrubber.

    Capture publishes ordered states, actual counters, camera/light readbacks,
    per-layer PNG paths, image validity and the selected dump directory.  The
    decode step fills the decoded geometry/data paths in `decoded`; capture
    never claims them.
    """
    def state_from_dump(d):
        lit = d.get('lit_record') or {}
        depth = d.get('depth_record') or {}
        selected = d.get('selected_resources') or {}
        return {
            'label': d.get('label'),
            'target_update': d.get('target_phase_draw'),
            'actual_update': d.get('actual_phase_draw'),
            'actual_updates': d.get('actual_phase_draws'),
            'target_matched': d.get('target_matched'),
            'bridge_draw': lit.get('phase_draw_before_render'),
            'bridge_draw_total': lit.get('capture_draw_total'),
            'engine_frame': lit.get('engine_frame_after'),
            'engine_frames_in_dump': d.get('engine_frames_in_dump'),
            'camera': lit.get('camera'),
            'pilot': lit.get('pilot'),
            'sun': lit.get('sun'),
            'layers': {
                'lit': lit.get('file'),
                'depth': depth.get('file'),
            },
            'lit_view_valid': lit.get('view_valid'),
            'depth_view_valid': depth.get('view_valid'),
            'lit_view_status': lit.get('view_status'),
            'dump_directory': d.get('raw_directory'),
            'dump_status': d.get('status'),
            'dump_total_bytes': d.get('total_bytes'),
            'present_resource_count': selected.get('present_count'),
            'missing_resource_count': selected.get('missing_count'),
            'missing_resources': [m.get('name') for m in
                                  (selected.get('missing') or [])],
        }

    def state_from_clean(item):
        lit = item.get('lit') or {}
        depth = item.get('depth') or {}
        return {
            'label': lit.get('label'),
            'target_update': item.get('target_update'),
            'actual_update': lit.get('phase_draw'),
            'actual_updates': [lit.get('phase_draw')] if lit.get('phase_draw') else [],
            'target_matched': (item.get('target_update') is not None and
                               lit.get('phase_draw') == item.get('target_update')),
            'bridge_draw': lit.get('phase_draw_before_render'),
            'bridge_draw_total': lit.get('capture_draw_total'),
            'engine_frame': lit.get('engine_frame_after'),
            'camera': lit.get('camera'),
            'pilot': lit.get('pilot'),
            'sun': lit.get('sun'),
            'layers': {'lit': lit.get('file'), 'depth': depth.get('file')},
            'lit_view_valid': lit.get('view_valid'),
            'depth_view_valid': depth.get('view_valid'),
            'lit_view_status': lit.get('view_status'),
            'dump_directory': None,
            'dump_status': 'clean-only',
            'dump_total_bytes': 0,
            'present_resource_count': None,
            'missing_resource_count': None,
            'missing_resources': [],
        }

    experiments = []
    for phase in ('baseline', 'lightstep', 'camera'):
        dumps = [d for d in SESSION['dumps'] if d.get('phase') == phase]
        if not dumps:
            continue
        states = [state_from_dump(d) for d in dumps]
        debug = [{
            'view': r.get('debug_view'),
            'file': r.get('file'),
            'phase_draw': r.get('phase_draw'),
            'engine_frame': r.get('engine_frame_after'),
            'label': r.get('label'),
            'view_valid': r.get('view_valid'),
            'view_status': r.get('view_status'),
            'empty_view': r.get('empty_view'),
        } for r in SESSION['debug'] if r.get('phase') == phase]
        experiments.append({
            'experiment': phase,
            'states': states,
            'debug_observations': debug,
        })

    manifest = {
        'schema': 'arda-sponza-temporal-manifest-v1',
        'run_id': RUN_ID,
        'mode': MODE,
        'status': SESSION.get('status'),
        'session': str(SESSION_PATH),
        'timeline': str(TIMELINE_PATH),
        'capture_spec': str(WORKSPACE / 'capture-spec.md'),
        'resolution': [1280, 720],
        'targets': TARGETS,
        'experiments': experiments,
        'decoded': {
            'status': 'pending_decode',
            'note': ('decode step must add decoded geometry/data paths here and in '
                     'the per-state records; placement must come from the selected '
                     'dump resources, not a schematic'),
        },
    }

    if CAMERA_RETRY:
        retry_states = [state_from_clean(s) for s in SESSION.get('camera_states') or []]
        retry_states += [state_from_dump(d) for d in SESSION['dumps']
                         if d.get('phase') == 'camera']
        retry_debug = [{
            'view': r.get('debug_view'),
            'file': r.get('file'),
            'phase_draw': r.get('phase_draw'),
            'engine_frame': r.get('engine_frame_after'),
            'label': r.get('label'),
            'view_valid': r.get('view_valid'),
            'view_status': r.get('view_status'),
            'empty_view': r.get('empty_view'),
        } for r in SESSION['debug'] if r.get('phase') == 'camera']
        chosen = (SESSION.get('camera_probe') or {}).get('chosen') or {}
        retry_experiment = {
            'experiment': 'camera',
            'status': 'retry' if retry_states else 'failed',
            'states': retry_states,
            'debug_observations': retry_debug,
            'chosen_offset_cm': chosen.get('offset_cm'),
            'candidate_order_cm': CAMERA_PROBE_OFFSETS,
        }
        manifest['experiments'] = [retry_experiment]
        manifest['camera_retry'] = {
            'run_id': RUN_ID,
            'session': str(SESSION_PATH),
            'chosen_offset_cm': chosen.get('offset_cm'),
            'candidates': (SESSION.get('camera_probe') or {}).get('candidates'),
            'filter_count': len(ACTIVE_FILTER_PAIRS),
            'reason': ('the planned +150 cm view and all four camera dumps from '
                       'run 20261115-101057 rendered empty; this retry tests the '
                       'rendered view before accepting an offset'),
        }
        # Merge into the main hand-off instead of replacing baseline/lightstep.
        main_path = TEMPORAL / 'manifest.json'
        if main_path.exists():
            try:
                main = json.loads(main_path.read_text(encoding='utf-8-sig'))
                for exp in main.get('experiments', []):
                    if exp.get('experiment') == 'camera':
                        exp['status'] = 'rejected'
                        exp['rejection_reason'] = (
                            'all clean camera views render empty/black at +150 cm; '
                            'raw dumps still exist but are not valid visual evidence')
                        exp['original_states'] = exp.get('states')
                        exp['original_debug_observations'] = exp.get('debug_observations')
                        exp['states'] = retry_states
                        exp['debug_observations'] = retry_debug
                        exp['retry_run_id'] = RUN_ID
                        exp['chosen_offset_cm'] = chosen.get('offset_cm')
                        exp['status'] = 'retry'
                main['camera_retry'] = manifest['camera_retry']
                main['camera_retry_session'] = str(SESSION_PATH)
                main['status'] = 'partial'
                main['status_note'] = (
                    'original camera states were rejected because every clean camera '
                    'view rendered empty; camera experiment replaced by the retry '
                    'recorded in camera_retry_session')
                manifest = main
            except Exception as exc:
                SESSION['notes'].append('manifest merge failed: ' + str(exc))

    atomic_json(TEMPORAL / (f'manifest-camera-retry-{RUN_ID}.json' if CAMERA_RETRY
                            else f'manifest-{RUN_ID}.json'), manifest)
    atomic_json(TEMPORAL / 'manifest.json', manifest)
    SESSION['manifest'] = str(TEMPORAL / 'manifest.json')
    return manifest


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
    return {
        'files': records,
        'engine_version': engine_version,
        'engine_build_id': engine_modules.get('BuildId'),
        'project_module_build_id': project_modules.get('BuildId'),
        'build_id_matches': (str(engine_modules.get('BuildId', '')).lower() ==
                             str(project_modules.get('BuildId', '')).lower()),
    }


def restore():
    restored = {'errors': []}
    try:
        sun = SESSION.get('original_sun') or {}
        SUN_COMP.set_intensity(float(sun.get('intensity_lux', SUN_HIGH)))
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


def read_exposure(pp):
    try:
        settings = pp.get_editor_property('settings')
        return {
            'auto_exposure_min_brightness': float(settings.get_editor_property('auto_exposure_min_brightness')),
            'auto_exposure_max_brightness': float(settings.get_editor_property('auto_exposure_max_brightness')),
            'auto_exposure_bias': float(settings.get_editor_property('auto_exposure_bias')),
            'auto_exposure_method': str(settings.get_editor_property('auto_exposure_method')),
        }
    except Exception as exc:
        return {'error': str(exc)}


def finish(status='ok', error=None):
    global HANDLE
    restore_rec = restore()
    SESSION['restored'] = restore_rec
    restored_camera = restore_rec.get('camera') or {}
    original_camera = SESSION.get('original_camera') or {}
    SESSION['camera_restored'] = bool(
        restored_camera.get('location_cm') and original_camera.get('location_cm') and
        abs(restored_camera['location_cm'][0] - original_camera['location_cm'][0]) < 0.5 and
        abs(restored_camera['location_cm'][1] - original_camera['location_cm'][1]) < 0.5 and
        abs(restored_camera['location_cm'][2] - original_camera['location_cm'][2]) < 0.5)
    SESSION['sun_restored'] = bool(
        restore_rec.get('sun') and SESSION.get('original_sun') and
        abs(restore_rec['sun']['intensity_lux'] - SESSION['original_sun']['intensity_lux']) < 0.5)
    SESSION['exposure_restored'] = bool(
        restore_rec.get('exposure') and
        abs(float(restore_rec['exposure'].get('auto_exposure_min_brightness', -99)) - EV100) < 1e-6 and
        abs(float(restore_rec['exposure'].get('auto_exposure_max_brightness', -99)) - EV100) < 1e-6 and
        abs(float(restore_rec['exposure'].get('auto_exposure_bias', -99)) - EXPOSURE_BIAS) < 1e-6)
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
    SESSION['env_hashes'] = environment_hashes()
    all_raw_bytes = workspace_raw_bytes()
    SESSION['budget'] = {
        'budget_bytes': BUDGET_BYTES,
        'budget_gib': round(BUDGET_BYTES / 1024 ** 3, 3),
        'used_bytes': raw_bytes(),
        'used_gib': round(raw_bytes() / 1024 ** 3, 4),
        'used_bytes_all_runs': all_raw_bytes,
        'used_gib_all_runs': round(all_raw_bytes / 1024 ** 3, 4),
        'exceeded': all_raw_bytes > BUDGET_BYTES,
    }
    empty_views = [r for r in SESSION['clean']
                   if r.get('kind') == 'clean' and r.get('view_status') == 'empty']
    empty_debug = [r for r in SESSION['debug'] if r.get('empty_view')]
    SESSION['empty_clean_views'] = [
        {'label': r.get('label'), 'file': r.get('file'),
         'mean': r.get('mean_display_luma'), 'std': r.get('std_display_luma'),
         'bytes': r.get('bytes')} for r in empty_views]
    SESSION['empty_debug_views'] = [
        {'label': r.get('label'), 'file': r.get('file'),
         'mean': r.get('mean_display_luma'), 'std': r.get('std_display_luma'),
         'bytes': r.get('bytes')} for r in empty_debug]
    SESSION['empty_capture_count'] = len(empty_views) + len(empty_debug)
    if CAMERA_RETRY and not (SESSION.get('camera_probe') or {}).get('chosen'):
        SESSION['status'] = 'failed'
    elif SESSION['empty_capture_count']:
        SESSION['status'] = 'partial'
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
    if status == 'ok' and (not SESSION['map_unchanged'] or SESSION['budget']['exceeded'] or
                           dump_status != 'ok'):
        SESSION['status'] = 'partial'
    if not SESSION['map_unchanged']:
        SESSION['errors'].append('map hash changed; the script never saves, investigate immediately')
    try:
        write_manifest()
    except Exception as exc:
        SESSION['errors'].append('manifest: ' + str(exc))
    write_session(force=True)
    atomic_json(LATEST_PATH, {
        'schema': 'arda-sponza-temporal-selected-latest-v1',
        'run_id': RUN_ID,
        'mode': MODE,
        'status': SESSION['status'],
        'session': str(SESSION_PATH),
        'raw_directory': str(RAW_ROOT),
        'dump_status': dump_status,
        'dump_count': len(dumps),
        'dump_file_count': SESSION['dump_file_count'],
        'dump_total_bytes': SESSION['dump_total_bytes'],
        'used_bytes_all_runs': SESSION['budget'].get('used_bytes_all_runs'),
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
        unreal.log_error('ARDA_TEMPORAL_SELECTED_ERROR ' + message)
        # The unchanged GPU-dump launcher looks for this exact marker.
        unreal.log_error('ARDA_GPU_DUMP_ERROR ' + message)
        SESSION['errors'].append(message)
        try:
            finish('error', message)
        except Exception:
            unreal.log_error('ARDA_TEMPORAL_SELECTED_ERROR finish failed')
    finally:
        STATE['busy'] = False


try:
    GPU.mkdir(parents=True, exist_ok=True)
    RAW_ROOT_BASE.mkdir(parents=True, exist_ok=True)
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    TEMPORAL.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    DEBUG_IMAGES.mkdir(parents=True, exist_ok=True)
    CAMERA_PROBES.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)

    SETUP = load_setup()
    if SETUP.get('schema') != 'arda-sponza-final-setup-v2':
        raise RuntimeError('unexpected final-setup schema: ' + str(SETUP.get('schema')))
    SESSION['setup_sha256'] = sha256_file(WORKSPACE / 'output' / 'final-setup.json')
    SESSION['script_sha256'] = sha256_file(WORKSPACE / 'scripts' / 'capture_gpu_dump.py')
    SESSION['validator_sha256'] = sha256_file(WORKSPACE / 'scripts' / 'validate_capture_plan.py')
    SESSION['map_sha256_before'] = sha256_file(MAP_FILE)
    SESSION['environment'] = environment_hashes()
    SESSION['filters_string'] = DUMP_FILTER_STRING
    SESSION['filters_used'] = [f'{p}@{r}' for p, r in ACTIVE_FILTER_PAIRS]
    SESSION['filter_count_used'] = len(ACTIVE_FILTER_PAIRS)
    SESSION['narrow_mode'] = CAMERA_RETRY
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

    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_clean()
    SESSION['render_status_start'] = json.loads(BRIDGE.get_render_status())
    SESSION['game_view_enabled'] = bool(LEVEL.editor_get_game_view())
    write_session(force=True)
    log('setup complete run=%s mode=%s raw=%s filters=%d' % (
        RUN_ID, MODE, RAW_ROOT, len(ACTIVE_FILTER_PAIRS)))

    CO = camera_retry_experiment() if CAMERA_RETRY else experiment()
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
except Exception:
    message = traceback.format_exc()
    ERROR_PATH.parent.mkdir(parents=True, exist_ok=True)
    ERROR_PATH.write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_TEMPORAL_SELECTED_ERROR ' + message)
    unreal.log_error('ARDA_GPU_DUMP_ERROR ' + message)
    try:
        SESSION['errors'].append(message)
        finish('error', message)
    except Exception:
        pass
