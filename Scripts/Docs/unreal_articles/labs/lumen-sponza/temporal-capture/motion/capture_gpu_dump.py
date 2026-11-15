#!/usr/bin/env python3
"""Same-run camera-motion Lumen capture on the retained Sponza teaching scene.

This workspace is deliberately camera-only and uses the full 136-pair
FILTER_PAIRS (never the old narrow camera-retry filter).  One Unreal process
captures:

  * warm baseline (120 bridge draws) at (-850, 0, 90), pitch 13, yaw 0, HFOV 70;
  * the direct, already-validated -150 cm Y move to (-850, -150, 90);
  * raw camera updates 1, 8 and 64 after that move.

The bridge draw counter is our own count of BRIDGE.render_viewport/capture
calls; the engine frame counter is unreal.SystemLibrary.get_frame_count().
Every draw is written to a timeline with both counters.  After each selected
DumpGPU frame finishes, its Base/Passes.json "Frame N" scopes are matched back
to that timeline, so the actual dumped update is recorded instead of assumed.

The experimental lit/depth PNGs are separate bridge observations rendered one
and two engine frames after the dumped frame.  Their offsets are recorded
explicitly.  The native DumpGPU Base/Screenshot.png is the same-frame image and
ViewData.PrimaryViews / SceneDepthZ from the same dump are the projection
source.

Run with the unchanged known-working launcher:
  scripts/run_gpu_dump.ps1 -Workspace D:\\UnrealUnderstanding-Motion
The launcher supplies the required startup ini override
r.RHI.UniformBufferContentMap.Enable=1.  This script does not save the map.
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
                     else Path(r'D:\UnrealUnderstanding-Motion'))
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
EV100 = 11.0
EXPOSURE_BIAS = 0.0
WARM_DRAWS = 120
FINAL_WARM_DRAWS = 24
TARGETS = [int(x) for x in
           os.environ.get('ARDA_TEMPORAL_TARGETS', '1,8,64').split(',')
           if x.strip()]
# A valid -150 cm view is larger than the empty +150 cm view that informed the
# original "four dumps ~9 GiB" estimate: each full-filter dump is ~2.99 GB.
# Required states are baseline, early update 1 and settled update 64.  Target 8
# is attempted after the settled dump only if the hard 10 GiB cap still allows
# it; the budget guard records the exact skip otherwise.
TARGET_ORDER = ([t for t in (1, 64) if t in TARGETS] +
                [t for t in TARGETS if t not in (1, 64)])
REQUIRED_DUMP_LABELS = ('baseline', 'camera-001', 'camera-064')
CAMERA_START_CM = [-850.0, 0.0, 90.0]
CAMERA_TARGET_CM = [-850.0, -150.0, 90.0]
CAMERA_ROTATION = {'pitch': 13.0, 'yaw': 0.0, 'roll': 0.0}
CAMERA_HFOV = 70.0
CAMERA_MOVE_CM = -150.0
MODE = 'camera-motion'
PRIOR_RETRY_RUN = Path(
    r'D:\UnrealUnderstanding-Temporal\output\temporal\run-20261115-101923.json')
PRIOR_RAW_ROOT = Path(r'D:\UnrealUnderstanding-Temporal\output\gpu-dump\raw')
CUMULATIVE_BUDGET_BYTES = 35 * 1024 ** 3
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
# Run-specific observation directories: a second attempt can never overwrite a
# previous run's screenshots or debug views.
IMAGES = TEMPORAL / f'images-{RUN_ID}'
DEBUG_IMAGES = TEMPORAL / f'debug-{RUN_ID}'
RAW_SCREENSHOTS = TEMPORAL / f'raw-screenshots-{RUN_ID}'
SESSION_PATH = TEMPORAL / f'run-{RUN_ID}.json'
TIMELINE_PATH = TEMPORAL / f'timeline-{RUN_ID}.json'
LATEST_PATH = GPU / 'latest-run.json'
CHECKPOINT_PATH = TEMPORAL / 'checkpoint.json'
ERROR_PATH = LOGS / f'temporal-{RUN_ID}-error.txt'
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
# Camera-only selection is a subset of what is captured, never a smaller
# filter.  The old narrow retry filter is deliberately not present here.
ACTIVE_FILTER_PAIRS = FILTER_PAIRS
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
    'exposure_ev100': EV100,
    'exposure_bias': EXPOSURE_BIAS,
    'targets_bridge_draws': TARGETS,
    'target_evaluation_order': TARGET_ORDER,
    'required_dump_labels': list(REQUIRED_DUMP_LABELS),
    'optional_targets': [t for t in TARGETS if t not in (1, 64)],
    'targets_definition': ('renders since the direct -150 cm camera move; '
                           'bridge viewport draws, not engine frames'),
    'mode': MODE,
    'camera_start_cm': CAMERA_START_CM,
    'camera_target_cm': CAMERA_TARGET_CM,
    'camera_rotation_degrees': CAMERA_ROTATION,
    'camera_hfov_deg': CAMERA_HFOV,
    'camera_move_cm': CAMERA_MOVE_CM,
    'filters': [f'{p}@{r}' for p, r in ACTIVE_FILTER_PAIRS],
    'filter_count': len(ACTIVE_FILTER_PAIRS),
    'full_filter_count': len(FILTER_PAIRS),
    'expected_resources': EXPECTED_RESOURCES,
    'budget_bytes': BUDGET_BYTES,
    'budget_gib': round(BUDGET_BYTES / 1024 ** 3, 3),
    'cumulative_budget_bytes': CUMULATIVE_BUDGET_BYTES,
    'cumulative_budget_gib': round(CUMULATIVE_BUDGET_BYTES / 1024 ** 3, 3),
    'phases': {},
    'timeline': [],
    'dumps': [],
    'clean': [],
    'debug': [],
    'camera': {},
    'camera_states': [],
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
        'All four dumps use the full 136-pair FILTER_PAIRS. Camera-only means the sequence is '
        'baseline plus camera updates 1/8/64, not a smaller filter.',
        'The native DumpGPU Base/Screenshot.png is the image of the dumped frame. The experimental '
        'lit/depth PNGs are separate bridge observations rendered one and two engine frames later; '
        'their phase and engine-frame offsets are recorded explicitly.',
        'The -150 cm camera target was validated by the Temporal rendered-view retry '
        'run-20261115-101923; this run does not repeat camera exploration.',
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


def image_file_record(path, label, note, **extra):
    """Describe an existing image without rendering anything.

    Used for DumpGPU's native Base/Screenshot.png, which belongs to the dumped
    frame itself rather than to a later bridge observation.
    """
    path = Path(path)
    rec = {
        'label': label,
        'note': note,
        'file': str(path),
        'exists': path.exists(),
        'kind': 'native-rawframe',
    }
    if not path.exists():
        rec['view_status'] = 'missing'
        rec['view_valid'] = False
        rec['empty_view'] = True
        rec.update(extra)
        return rec
    rec['bytes'] = path.stat().st_size
    rec['sha256'] = sha256_file(path)
    try:
        rec.update(pixel_metrics(path))
    except Exception as exc:
        rec['pixel_metrics_error'] = str(exc)
    std = rec.get('std_display_luma')
    mean = rec.get('mean_display_luma')
    black = rec.get('near_black_fraction')
    empty = bool(rec.get('pixel_metrics_error') or std is None or
                 std < 1.0 or (mean is not None and mean < 1.0) or
                 (black is not None and black > 0.98))
    rec['empty_view'] = empty
    rec['view_valid'] = not empty
    rec['view_status'] = 'empty' if empty else 'ok'
    rec['view_kind'] = 'lit'
    rec.update(extra)
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
    dump_frames = meta.get('engine_frames') or []
    dump_frame = dump_frames[0] if dump_frames else None

    def observation_offset(rec):
        if not rec:
            return None
        rendered_phase = rec.get('phase_draw_before_render')
        rendered_frame = rec.get('engine_frame_after')
        return {
            'file': rec.get('file'),
            'phase_draw_when_rendered': rendered_phase,
            'phase_draw_offset_from_dump': (
                None if rendered_phase is None or actual is None
                else int(rendered_phase) - int(actual)),
            'engine_frame_when_rendered': rendered_frame,
            'engine_frame_offset_from_dump': (
                None if rendered_frame is None or dump_frame is None
                else int(rendered_frame) - int(dump_frame)),
            'same_frame_as_dump': bool(rendered_frame is not None and
                                       dump_frame is not None and
                                       int(rendered_frame) == int(dump_frame)),
            'view_valid': rec.get('view_valid'),
            'view_status': rec.get('view_status'),
            'mean_display_luma': rec.get('mean_display_luma'),
            'std_display_luma': rec.get('std_display_luma'),
            'bytes': rec.get('bytes'),
            'sha256': rec.get('sha256'),
        }

    base = Path(dump['directory']) / 'Base'
    native_src = base / 'Screenshot.png'
    native = image_file_record(
        native_src, dump['label'] + '-rawframe',
        'native DumpGPU Base/Screenshot.png of the selected frame')
    if native_src.exists():
        RAW_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        native_copy = RAW_SCREENSHOTS / f"{dump['label']}-rawframe.png"
        shutil.copy2(native_src, native_copy)
        native['run_copy'] = str(native_copy)
        native['run_copy_sha256'] = sha256_file(native_copy)
    native['same_frame_as_dump'] = True
    native['engine_frame'] = dump_frame
    native['phase_draw'] = actual
    native['source'] = 'DumpGPU Base/Screenshot.png'

    stage = 'baseline' if dump['phase'] == 'baseline' else (
        'settled' if target is not None and int(target) >= 64 else 'early')
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
        'experimental_lit_offset': observation_offset(lit_record),
        'experimental_depth_offset': observation_offset(depth_record),
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
    # Baseline / first camera dump are larger (screen-probe gather + first-frame
    # state); updates 8/64 reuse more versions.  These reserves are measured from
    # the Temporal full-filter dumps and keep the four required dumps inside the
    # 10 GiB cap while still stopping a runaway fifth capture.
    if label in ('baseline', 'camera-001'):
        reserve = int(3.1 * 1024 ** 3)
    else:
        reserve = int(2.0 * 1024 ** 3)
    if used >= BUDGET_BYTES or used + reserve > BUDGET_BYTES:
        rec = {'label': label, 'reason': 'budget guard: workspace raw at/above cap',
               'used_bytes': used, 'reserve_bytes': reserve,
               'budget_bytes': BUDGET_BYTES}
        STATE['skips'].append(rec)
        SESSION['skips'].append(rec)
        log('skip dump %s: budget %d + reserve %d > %d' % (
            label, used, reserve, BUDGET_BYTES))
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


def apply_camera_state(target_cm, rotation, fov, label='camera-move'):
    """Set the exact approved camera state directly; no candidate probing.

    The -150 cm Y target was already validated by the rendered-view retry in
    D:\\UnrealUnderstanding-Temporal run 20261115-101923.  This run must not
    repeat camera exploration, so the requested target is applied once and its
    actor and piloted-viewport readbacks are recorded.
    """
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
        'method': ('direct target state, no candidate probing; -150 cm was '
                   'validated by Temporal run 20261115-101923'),
    }
    if not applied:
        rec['restored_after_failure'] = restore_original_camera()
        rec['reason'] = 'readback did not match the approved target state'
    return rec


def prior_camera_validation():
    """Read-only reference to the already-executed -150 cm rendered-view retry."""
    rec = {'source': str(PRIOR_RETRY_RUN), 'available': PRIOR_RETRY_RUN.exists()}
    if not PRIOR_RETRY_RUN.exists():
        rec['ok'] = False
        return rec
    try:
        run = json.loads(PRIOR_RETRY_RUN.read_text(encoding='utf-8-sig'))
        move = run.get('camera_move') or {}
        chosen = ((run.get('camera_probe') or {}).get('chosen') or {})
        rec.update({
            'run_id': run.get('run_id'),
            'applied': bool(move.get('applied')),
            'offset_cm': move.get('offset_cm'),
            'target_cm': move.get('target_cm'),
            'readback': move.get('readback'),
            'chosen_view_valid': chosen.get('view_valid'),
            'chosen_empty_view': chosen.get('empty_view'),
            'map_sha256_after': run.get('map_sha256_after'),
        })
        rec['ok'] = bool(
            move.get('applied') and
            [round(float(x), 3) for x in (move.get('target_cm') or [])] == CAMERA_TARGET_CM and
            chosen.get('view_valid') is True and chosen.get('empty_view') is False)
    except Exception as exc:
        rec['ok'] = False
        rec['error'] = str(exc)
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
    """Same-process camera-only sequence: baseline, move -150 cm, updates 1/8/64.

    No light step, no camera probing and no debug view before the settled dump.
    Baseline and all three camera states use the full 136-pair FILTER_PAIRS.
    """
    set_clean()
    SESSION['prior_camera_validation'] = prior_camera_validation()

    set_phase('warm-baseline', 'warm 120 bridge draws before the baseline')
    yield from warm(WARM_DRAWS)

    set_phase('baseline', 'warm accepted baseline at (-850,0,90)')
    baseline_lit, baseline_depth = yield from capture_state_images(
        'baseline', 'warm accepted baseline before the camera move')
    SESSION['camera_states'].append({
        'label': 'baseline',
        'target_update': None,
        'actual_update': baseline_lit.get('phase_draw'),
        'lit': baseline_lit,
        'depth': baseline_depth,
    })
    yield from dump_state('baseline', None, 'baseline',
                          'warm accepted baseline before the camera move')

    # Direct, already-validated move.  No candidate sweep: the +150 cm plan was
    # rejected by the rendered view and -150 cm was accepted in run 20261115-101923.
    move = apply_camera_state(CAMERA_TARGET_CM, CAMERA_ROTATION, CAMERA_HFOV)
    SESSION['camera_move'] = move
    SESSION['camera']['move'] = move
    write_session(force=True)
    if not move.get('applied'):
        SESSION['errors'].append(
            'camera move readback did not match the approved -150 cm target state')
        SESSION['notes'].append('camera move refused; no camera-state dumps claimed')
        yield from debug_views('camera-refused', 'camera move failed readback; baseline view')
    else:
        set_phase('camera', 'direct -150 cm Y camera move at baseline sun/exposure')
        SESSION['camera']['moved_utc'] = now_iso()
        SESSION['camera']['moved_engine_frame'] = engine_frame()
        SESSION['camera']['moved_draw_total'] = STATE['draws']
        yield from advance_to(0)
        for target in TARGET_ORDER:
            if STATE['phase_draw'] > target:
                rec = {'phase': 'camera', 'target': target,
                       'reason': 'already past target when reached',
                       'actual_phase_draw': STATE['phase_draw']}
                STATE['skips'].append(rec)
                SESSION['skips'].append(rec)
                continue
            yield from dump_state('camera-%03d' % target, target, 'camera',
                                  'direct -150 cm camera move, target update %d' % target)
        # Debug views are only after the settled raw dump; they are later display
        # observations, not same-frame raw buffers.
        yield from debug_views('camera-settled',
                               'camera-move settled state, debug observations after update 64')

    # Restore baseline camera/sun/exposure in memory only; never save the map.
    restore_original_camera()
    set_sun(SUN_HIGH)
    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_phase('final', 'original camera/sun/exposure restored in memory')
    yield from warm(FINAL_WARM_DRAWS)
    final_lit, final_depth = yield from capture_state_images(
        'final', 'restored original camera, sun and exposure')
    SESSION['final_state'] = {
        'lit': final_lit,
        'depth': final_depth,
        'camera': hero_light(),
        'sun': sun_readback(),
        'exposure': read_exposure(PP),
    }
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
    """Camera-motion hand-off with exact raw paths, frames and image offsets.

    The native DumpGPU screenshot belongs to the dumped frame.  The experimental
    lit/depth PNGs are later bridge observations; both phase-draw and engine-frame
    offsets are published so no image is mistaken for the raw frame.
    """
    def state_from_dump(d):
        lit = d.get('lit_record') or {}
        selected = d.get('selected_resources') or {}
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
            'bridge_draw': lit.get('phase_draw_before_render'),
            'bridge_draw_total': lit.get('capture_draw_total'),
            'engine_frame': (d.get('raw_frame') or {}).get('engine_frame'),
            'camera': lit.get('camera'),
            'pilot': lit.get('pilot'),
            'sun': lit.get('sun'),
            'exposure': SESSION.get('original_exposure'),
            'layers': {
                'experimental_lit': (d.get('experimental_lit_offset') or {}).get('file'),
                'experimental_depth': (d.get('experimental_depth_offset') or {}).get('file'),
                'native_rawframe': native.get('file'),
                'native_rawframe_run_copy': native.get('run_copy'),
            },
            'experimental_lit_offset': d.get('experimental_lit_offset'),
            'experimental_depth_offset': d.get('experimental_depth_offset'),
            'native_screenshot': native,
            'lit_view_valid': lit.get('view_valid'),
            'depth_view_valid': (d.get('depth_record') or {}).get('view_valid'),
            'lit_view_status': lit.get('view_status'),
            'dump_directory': d.get('raw_directory'),
            'dump_status': d.get('status'),
            'dump_file_count': d.get('file_count'),
            'dump_total_bytes': d.get('total_bytes'),
            'present_resource_count': selected.get('present_count'),
            'missing_resource_count': selected.get('missing_count'),
            'missing_resources': [m.get('name') for m in
                                  (selected.get('missing') or [])],
        }

    def clean_record(item):
        if not item:
            return None
        return {
            'file': item.get('file'),
            'phase_draw': item.get('phase_draw'),
            'engine_frame': item.get('engine_frame_after'),
            'view_valid': item.get('view_valid'),
            'view_status': item.get('view_status'),
            'mean_display_luma': item.get('mean_display_luma'),
            'std_display_luma': item.get('std_display_luma'),
            'sha256': item.get('sha256'),
        }

    dumps = list(SESSION['dumps'])
    states = [state_from_dump(d) for d in dumps]
    baseline = next((s for s in states if s.get('stage') == 'baseline'), None)
    camera_states = [s for s in states if s.get('stage') in ('early', 'settled')]
    missing_union = sorted({name for s in states for name in (s.get('missing_resources') or [])})
    present_counts = [s.get('present_resource_count') for s in states]
    required_missing = [s.get('label') for s in states
                        if s.get('required') and s.get('dump_status') != 'ok']
    native_invalid = [s.get('label') for s in states
                      if not ((s.get('native_screenshot') or {}).get('view_valid'))]
    exp_invalid = [s.get('label') for s in states
                   if not s.get('lit_view_valid') or not s.get('depth_view_valid')]
    camera_move = SESSION.get('camera_move') or {}
    readback = camera_move.get('readback') or {}
    camera_applied = bool(
        camera_move.get('applied') and
        readback.get('location_cm') == CAMERA_TARGET_CM and
        readback.get('rotation_degrees') == CAMERA_ROTATION and
        abs(float(readback.get('horizontal_fov_deg', -1)) - CAMERA_HFOV) < 1e-6)

    debug = [{
        'view': r.get('debug_view'),
        'file': r.get('file'),
        'phase_draw': r.get('phase_draw'),
        'engine_frame': r.get('engine_frame_after'),
        'label': r.get('label'),
        'view_valid': r.get('view_valid'),
        'view_status': r.get('view_status'),
        'empty_view': r.get('empty_view'),
        'note': 'post-settled display observation; not a same-frame raw buffer',
    } for r in SESSION['debug']]

    checks = {
        'same_process_run': True,
        'required_states_present': bool(
            all(any(s.get('label') == label for s in states)
                for label in REQUIRED_DUMP_LABELS)),
        'required_dumps_ok': not required_missing,
        'full_filter_count': len(ACTIVE_FILTER_PAIRS),
        'camera_move_applied': camera_applied,
        'camera_target_cm': CAMERA_TARGET_CM,
        'camera_readback': readback,
        'map_unchanged': SESSION.get('map_unchanged'),
        'workspace_budget_ok': not ((SESSION.get('budget') or {}).get('exceeded')),
        'cumulative_budget_ok': not ((SESSION.get('budget') or {}).get('cumulative_exceeded')),
        'native_screenshots_valid': not native_invalid,
        'experimental_views_valid': not exp_invalid,
    }

    manifest = {
        'schema': 'arda-motion-camera-manifest-v1',
        'run_id': RUN_ID,
        'mode': MODE,
        'status': SESSION.get('status'),
        'workspace': str(WORKSPACE),
        'session': str(SESSION_PATH),
        'timeline': str(TIMELINE_PATH),
        'raw_root': str(RAW_ROOT),
        'resolution': list(RESOLUTION),
        'targets': TARGETS,
        'camera': {
            'start_cm': CAMERA_START_CM,
            'target_cm': CAMERA_TARGET_CM,
            'rotation_degrees': CAMERA_ROTATION,
            'horizontal_fov_deg': CAMERA_HFOV,
            'move_cm': CAMERA_MOVE_CM,
            'applied': camera_move.get('applied'),
            'readback': readback,
            'pilot': camera_move.get('pilot'),
            'prior_validation': SESSION.get('prior_camera_validation'),
            'no_exploration': True,
        },
        'sun': SESSION.get('original_sun'),
        'exposure': SESSION.get('original_exposure'),
        'states': states,
        'baseline': baseline,
        'camera_states': camera_states,
        'clean_states': {
            'baseline': {
                'lit': clean_record((SESSION.get('camera_states') or [{}])[0].get('lit')),
                'depth': clean_record((SESSION.get('camera_states') or [{}])[0].get('depth')),
            },
            'final': {
                'lit': clean_record((SESSION.get('final_state') or {}).get('lit')),
                'depth': clean_record((SESSION.get('final_state') or {}).get('depth')),
                'camera': (SESSION.get('final_state') or {}).get('camera'),
                'sun': (SESSION.get('final_state') or {}).get('sun'),
                'exposure': (SESSION.get('final_state') or {}).get('exposure'),
            },
        },
        'debug_observations': debug,
        'missing_coverage': {
            'union_missing_names': missing_union,
            'per_dump_present_counts': present_counts,
            'note': ('Exact-name absences are reported; they are not replaced by '
                     'schematic data. Selected resources that the frame did not '
                     'create/bind are listed per state.'),
        },
        'budget': SESSION.get('budget'),
        'checks': checks,
        'images': {
            'experimental': {
                'directory': str(IMAGES),
                'note': 'bridge capture_viewport at a later engine frame; offsets recorded per state',
            },
            'native_rawframe': {
                'directory': str(RAW_SCREENSHOTS),
                'note': 'copy of DumpGPU Base/Screenshot.png, same frame as the dump',
            },
            'debug': {
                'directory': str(DEBUG_IMAGES),
                'note': 'post-settled display observations, not same-frame raw buffers',
            },
        },
        'decoded': {
            'status': 'pending_decode',
            'note': ('decode must use ViewData.PrimaryViews and SceneDepthZ from the '
                     'same dump frame; the experimental screenshots are not projection '
                     'sources'),
        },
    }
    atomic_json(TEMPORAL / f'manifest-{RUN_ID}.json', manifest)
    atomic_json(TEMPORAL / 'manifest.json', manifest)
    atomic_json(WORKSPACE / 'output' / 'capture-manifest.json', manifest)
    SESSION['manifest'] = str(TEMPORAL / 'manifest.json')
    SESSION['machine_manifest'] = str(WORKSPACE / 'output' / 'capture-manifest.json')
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
    prior_raw = dir_bytes(PRIOR_RAW_ROOT) if PRIOR_RAW_ROOT.exists() else 0
    cumulative_raw = all_raw_bytes + prior_raw
    SESSION['budget'] = {
        'workspace_budget_bytes': BUDGET_BYTES,
        'workspace_budget_gib': round(BUDGET_BYTES / 1024 ** 3, 3),
        'workspace_used_bytes': raw_bytes(),
        'workspace_used_gib': round(raw_bytes() / 1024 ** 3, 4),
        'workspace_used_all_runs_bytes': all_raw_bytes,
        'workspace_used_all_runs_gib': round(all_raw_bytes / 1024 ** 3, 4),
        'workspace_exceeded': all_raw_bytes > BUDGET_BYTES,
        'existing_temporal_raw_bytes': prior_raw,
        'existing_temporal_raw_gib': round(prior_raw / 1024 ** 3, 4),
        'cumulative_budget_bytes': CUMULATIVE_BUDGET_BYTES,
        'cumulative_budget_gib': round(CUMULATIVE_BUDGET_BYTES / 1024 ** 3, 3),
        'cumulative_used_bytes': cumulative_raw,
        'cumulative_used_gib': round(cumulative_raw / 1024 ** 3, 4),
        'cumulative_exceeded': cumulative_raw > CUMULATIVE_BUDGET_BYTES,
        'exceeded': (all_raw_bytes > BUDGET_BYTES or
                     cumulative_raw > CUMULATIVE_BUDGET_BYTES),
        'approved_by_supervisor': True,
        'note': ('10 GiB cap for this workspace raw data and 35 GiB cumulative '
                 'with the existing Temporal 19.5536 GiB; both explicitly approved.'),
    }
    dumps = SESSION['dumps']
    empty_views = [r for r in SESSION['clean']
                   if r.get('kind') == 'clean' and r.get('view_status') == 'empty']
    empty_debug = [r for r in SESSION['debug'] if r.get('empty_view')]
    empty_native = [d for d in dumps
                    if not ((d.get('native_screenshot') or {}).get('view_valid'))]
    SESSION['empty_clean_views'] = [
        {'label': r.get('label'), 'file': r.get('file'),
         'mean': r.get('mean_display_luma'), 'std': r.get('std_display_luma'),
         'bytes': r.get('bytes')} for r in empty_views]
    SESSION['empty_debug_views'] = [
        {'label': r.get('label'), 'file': r.get('file'),
         'mean': r.get('mean_display_luma'), 'std': r.get('std_display_luma'),
         'bytes': r.get('bytes')} for r in empty_debug]
    SESSION['empty_native_views'] = [
        {'label': d.get('label'), 'file': (d.get('native_screenshot') or {}).get('file'),
         'mean': (d.get('native_screenshot') or {}).get('mean_display_luma'),
         'std': (d.get('native_screenshot') or {}).get('std_display_luma'),
         'bytes': (d.get('native_screenshot') or {}).get('bytes')}
        for d in empty_native]
    SESSION['empty_capture_count'] = (len(empty_views) + len(empty_debug) +
                                      len(empty_native))
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
    required_ok = bool(all(
        any(d.get('label') == label and d.get('status') == 'ok' for d in dumps)
        for label in REQUIRED_DUMP_LABELS))
    SESSION['required_states_ok'] = required_ok
    camera_move_applied = bool((SESSION.get('camera_move') or {}).get('applied'))
    SESSION['camera_move_applied'] = camera_move_applied
    if status == 'ok' and (SESSION['empty_capture_count'] or
                           not SESSION['map_unchanged'] or
                           SESSION['budget']['exceeded'] or
                           dump_status != 'ok' or not required_ok or
                           not camera_move_applied):
        SESSION['status'] = 'partial'
    if not camera_move_applied:
        SESSION['errors'].append(
            'camera move was not applied or did not read back the approved target')
    if not required_ok:
        SESSION['errors'].append(
            'one or more required full-filter states are missing or not ok: ' +
            ', '.join(REQUIRED_DUMP_LABELS))
    if not SESSION['map_unchanged']:
        SESSION['errors'].append('map hash changed; the script never saves, investigate immediately')
    try:
        write_manifest()
    except Exception as exc:
        SESSION['errors'].append('manifest: ' + str(exc))
    write_session(force=True)
    atomic_json(LATEST_PATH, {
        'schema': 'arda-motion-camera-latest-v1',
        'run_id': RUN_ID,
        'mode': MODE,
        'status': SESSION['status'],
        'session': str(SESSION_PATH),
        'machine_manifest': str(WORKSPACE / 'output' / 'capture-manifest.json'),
        'raw_directory': str(RAW_ROOT),
        'dump_status': dump_status,
        'dump_count': len(dumps),
        'dump_file_count': SESSION['dump_file_count'],
        'dump_total_bytes': SESSION['dump_total_bytes'],
        'required_states_ok': SESSION['required_states_ok'],
        'camera_move_applied': SESSION['camera_move_applied'],
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
    RAW_SCREENSHOTS.mkdir(parents=True, exist_ok=True)
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
    SESSION['narrow_mode'] = False
    SESSION['full_filters'] = True
    SESSION['capture_mode'] = 'camera-only-same-run'
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

    set_exposure(PP, EV100, EXPOSURE_BIAS)
    set_clean()
    SESSION['render_status_start'] = json.loads(BRIDGE.get_render_status())
    SESSION['game_view_enabled'] = bool(LEVEL.editor_get_game_view())
    write_session(force=True)
    log('setup complete run=%s mode=%s raw=%s filters=%d' % (
        RUN_ID, MODE, RAW_ROOT, len(ACTIVE_FILTER_PAIRS)))

    CO = experiment()
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
