"""Repair the two misleading diagnostic controls without recapturing good frames.

Bounded one-run repair for the Sponza final evidence set:

1. FINAL GATHER OFF.  `r.Lumen.ScreenProbeGather 0` makes
   AddLumenScreenProbeGather() return black diffuse-indirect textures 0/1 and a
   cleared black probe rough-specular texture 2 (LumenScreenProbeGather.cpp
   2194-2202).  It does NOT switch to another GI pipeline.  The old
   `r.Lumen.DiffuseIndirect.Allow 0` capture is kept only as a rejected,
   confounded control.  This run captures the correctly isolated gather-off
   control on the hero and material cameras with DiffuseIndirect.Allow=1 and
   dedicated reflections intact, then restores and verifies the baseline.

2. Probe placement.  `r.Lumen.ScreenProbeGather.Debug.ProbePlacement` only
   feeds FScreenProbeGatherDebugCS, which is dispatched only when
   `r.Lumen.ScreenProbeGather.Debug != 0` (LumenScreenProbeGather.cpp
   2755-2767).  The old image therefore has no overlay.  This run sets both
   CVars and captures the actual placement overlay.  If it still shows no
   overlay, it is rejected and the raw-dump decode owns placement evidence.

Also records the observed r.Lumen.Reflections.RoughnessFadeLength (default .1)
for the existing reflection-threshold experiment, and verifies the approved
saved map is still on disk unchanged.  No engine edit, no build, no original
capture-project write, and no capture of already-good PT/reference/pair frames.
"""
import json
import os
import sys
import time
import traceback
from pathlib import Path

import unreal

WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', r'D:\UnrealUnderstanding-Sponza'))
SCRIPTS = WORKSPACE / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Resolve every path before a slate callback can run; never depend on __file__
# from inside the callback/finish path.
SCRIPT_FILE = SCRIPTS / 'repair_controls.py'
FINAL = WORKSPACE / 'output' / 'final'
IMGDIR = FINAL / 'images'
DIAGDIR = FINAL / 'diagnostics'
RUN_PATH = FINAL / 'run-repair.json'
CHECKPOINT = FINAL / 'checkpoint-repair.json'
ERROR_LOG = WORKSPACE / 'output' / 'logs' / 'repair-controls-error.txt'
SETUP_PATH = WORKSPACE / 'output' / 'final-setup.json'

from apply_final_setup import (  # noqa: E402
    ACTORS, BRIDGE, LEVEL, MAP_FILE, MAP_PATH, OUTPUT, apply_config, atomic_json,
    cvar, cvars, load_setup, pilot, pilot_readback, pixel_metrics, set_exposure,
    sha256_file,
)

WARM_DRAWS = 120
GATHER_SETTLE = 60
RESTORE_SETTLE = 40
DIAG_SETTLE = 24
FINAL_SETTLE = 20

CVAR_NAMES = [
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.Lumen.HardwareRayTracing', 'r.LumenScene.FarField',
    'r.Lumen.DiffuseIndirect.Allow', 'r.Lumen.ScreenProbeGather',
    'r.Lumen.ScreenProbeGather.Debug',
    'r.Lumen.ScreenProbeGather.Debug.ProbePlacement',
    'r.Lumen.ScreenProbeGather.VisualizeTraces',
    'r.Lumen.ScreenProbeGather.ScreenTraces', 'r.Lumen.Reflections.ScreenTraces',
    'r.Lumen.Reflections.MaxRoughnessToTrace',
    'r.Lumen.Reflections.MaxRoughnessToTraceClamp',
    'r.Lumen.Reflections.RoughnessFadeLength',
    'r.Lumen.HardwareRayTracing.LightingMode',
    'r.Lumen.HardwareRayTracing.HitLighting.Allowed',
    'r.Lumen.Visualize', 'r.Lumen.Visualize.CardPlacement',
    'r.ShaderPrint', 'r.ScreenPercentage', 'r.AntiAliasingMethod',
]

SETTINGS = {
    'hero_camera': 'CAM_Hero_Reference',
    'material_camera': 'CAM_Materials',
    'resolution': [1280, 720],
    'exposure_ev100': 11.0,
    'exposure_bias': 0.0,
    'final_gather_off': {
        'requested_commands': ['r.Lumen.ScreenProbeGather 0'],
        'restore_commands': ['r.Lumen.ScreenProbeGather 1'],
        'preserved': ['r.Lumen.DiffuseIndirect.Allow 1',
                      'r.Lumen.HardwareRayTracing 1',
                      'r.ReflectionMethod 1',
                      'r.Lumen.Reflections.ScreenTraces 1'],
        'label': 'FINAL GATHER OFF (black diffuse textures 0/1 + cleared probe rough-specular texture 2; NOT pure diffuse only)',
    },
    'probe_placement': {
        'old_requested': ['r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1'],
        'repaired_requested': ['r.Lumen.ScreenProbeGather.Debug 1',
                               'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1'],
        'normals_requested': ['r.Lumen.ScreenProbeGather.Debug 1',
                              'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 3'],
        'source_gate': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp:2755-2767 dispatches FScreenProbeGatherDebugCS only when r.Lumen.ScreenProbeGather.Debug!=0; :2210-2215 enables ShaderPrint in that case',
    },
}

SOURCE_EVIDENCE = {
    'gather_off': {
        'file': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp',
        'lines': '2194-2202',
        'observed_behavior': 'if (!GLumenScreenProbeGather): Textures[0]=Black, Textures[1]=Black, Textures[2]=new float RGB texture cleared to black; return ScreenSpaceDenoiserInputs. No alternate GI pipeline is selected.',
    },
    'probe_debug_gate': {
        'file': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp',
        'lines': '2755-2767',
        'observed_behavior': 'FScreenProbeGatherDebugCS is only added when GVarLumenScreenProbeGatherDebug != 0; its VisualizeProbePlacement parameter is read from r.Lumen.ScreenProbeGather.Debug.ProbePlacement. With Debug=0 the placement CVar is inert.',
    },
    'probe_shaderprint': {
        'file': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp',
        'lines': '2210-2215',
        'observed_behavior': 'Debug!=0 calls ShaderPrint::SetEnabled(true) and requests line space; the debug shader draws points/labels through ShaderPrint.',
    },
    'roughness_fade': {
        'file': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenReflections.cpp',
        'lines': '129-132',
        'observed_behavior': 'GLumenReflectionRoughnessFadeLength defaults to .1f and is exposed as r.Lumen.Reflections.RoughnessFadeLength.',
    },
}

SESSION = {
    'schema': 'arda-sponza-final-repair-v1',
    'status': 'running',
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'resolution': [1280, 720],
    'script_sha256': sha256_file(SCRIPT_FILE),
    'setup_sha256': sha256_file(SETUP_PATH),
    'map_sha256_start': sha256_file(MAP_FILE),
    'settings': SETTINGS,
    'source_evidence': SOURCE_EVIDENCE,
    'captures': [],
    'notes': [],
}
STATE = {'draws': 0, 'phase': 0, 'phase_draws': 0, 'busy': False, 'done': False}
HANDLE = None
SETUP = None
HERO = None
MATERIAL = None
PP = None
PHASES = []


def log(message):
    unreal.log('ARDA_REPAIR ' + str(message))


def console(command):
    unreal.log('ARDA_REPAIR_COMMAND ' + command)
    try:
        return unreal.SystemLibrary.execute_console_command(
            unreal.EditorLevelLibrary.get_editor_world(), command)
    except Exception as exc:
        SESSION['notes'].append('console %s: %s' % (command, exc))
        return False


def set_lit_clean():
    try:
        BRIDGE.set_viewport_mode('lit', '')
    except Exception as exc:
        SESSION['notes'].append('lit mode: ' + str(exc))
    for command in (
        'r.ScreenPercentage 100', 'r.AntiAliasingMethod 2',
        'r.MotionBlurQuality 0', 'r.BloomQuality 0', 'r.DepthOfFieldQuality 0',
        'r.VolumetricFog 0',
        'r.DynamicGlobalIlluminationMethod 1', 'r.ReflectionMethod 1',
        'r.Lumen.HardwareRayTracing 1', 'r.LumenScene.FarField 0',
        'r.Lumen.DiffuseIndirect.Allow 1',
        'r.Lumen.ScreenProbeGather 1',
        'r.Lumen.ScreenProbeGather.ScreenTraces 1',
        'r.Lumen.Reflections.ScreenTraces 1',
        'r.Lumen.Reflections.MaxRoughnessToTrace 0.4',
        'r.Lumen.Reflections.MaxRoughnessToTraceClamp 1.0',
        'r.Lumen.HardwareRayTracing.LightingMode 0',
        'r.Lumen.HardwareRayTracing.HitLighting.Allowed 1',
        'r.Lumen.Visualize 0', 'r.Lumen.Visualize.CardPlacement 0',
        'r.Lumen.ScreenProbeGather.Debug 0',
        'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 0',
        'r.Lumen.ScreenProbeGather.VisualizeTraces 0',
        'r.ShaderPrint 0',
        'r.DistanceFields.Visualize.Monochrome 1',
        'showflag.VisualizeMeshDistanceFields 0',
        'showflag.VisualizeGlobalDistanceField 0',
        'showflag.BillboardSprites 0', 'showflag.Grid 0', 'showflag.Selection 0',
        'showflag.SelectionOutline 0', 'showflag.ModeWidgets 0', 'showflag.Bounds 0',
        'ShowFlag.Cameras 0', 'ShowFlag.CameraFrustums 0',
        'ShowFlag.CompositeEditorPrimitives 0', 'ShowFlag.Game 1',
        'ShowFlag.LumenGlobalTraces 1', 'ShowFlag.LumenDetailTraces 1',
    ):
        console(command)


def read_exposure():
    try:
        settings = PP.get_editor_property('settings')
        return {
            'auto_exposure_min_brightness': float(settings.get_editor_property('auto_exposure_min_brightness')),
            'auto_exposure_max_brightness': float(settings.get_editor_property('auto_exposure_max_brightness')),
            'auto_exposure_bias': float(settings.get_editor_property('auto_exposure_bias')),
        }
    except Exception as exc:
        return {'error': str(exc)}


def observed_state(camera):
    return {
        'camera': pilot_readback(camera),
        'exposure': read_exposure(),
        'cvars': cvars(CVAR_NAMES),
    }


def capture_image(name, kind, camera, requested, note, record_list):
    path = (IMGDIR if kind in ('gather_off', 'restore', 'validation') else DIAGDIR) / (name + '.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    before = observed_state(camera)
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    after = observed_state(camera)
    info = {
        'name': name, 'kind': kind, 'camera': camera.get_actor_label(),
        'note': note, 'requested_commands': requested,
        'file': str(path), 'capture_draw': STATE['draws'],
        'observed_before': before, 'observed_after': after,
    }
    if not path.exists() or path.stat().st_size <= 2048:
        info['error'] = 'screenshot not written'
        SESSION['notes'].append(name + ': screenshot not written')
    else:
        info.update({'bytes': path.stat().st_size, 'sha256': sha256_file(path)})
        try:
            info.update(pixel_metrics(path))
        except Exception as exc:
            info['pixel_metrics_error'] = str(exc)
    record_list.append(info)
    atomic_json(RUN_PATH, SESSION)
    log('captured %s mean=%s black=%s' % (
        name, info.get('mean_display_luma'), info.get('near_black_fraction')))
    return info


def add_phase(name, camera, draws, setup, capture):
    PHASES.append({'name': name, 'camera': camera, 'draws': draws,
                   'setup': setup, 'capture': capture})


def build_phases():
    def gather_off(camera, prefix):
        def setup():
            set_lit_clean()
            console('r.Lumen.ScreenProbeGather 0')

        def capture():
            capture_image(prefix + '-case-final_gather_off', 'gather_off', camera,
                          SETTINGS['final_gather_off']['requested_commands'],
                          SETTINGS['final_gather_off']['label'], SESSION['captures'])
        return setup, capture

    def restore(camera, prefix):
        def setup():
            set_lit_clean()
            console('r.Lumen.ScreenProbeGather 1')

        def capture():
            capture_image(prefix + '-restore-after-final_gather_off', 'restore', camera,
                          SETTINGS['final_gather_off']['restore_commands'],
                          'baseline restore readback after FINAL GATHER OFF', SESSION['captures'])
        return setup, capture

    add_phase('hero-warm', HERO, WARM_DRAWS, set_lit_clean, None)
    setup, capture = gather_off(HERO, 'hero')
    add_phase('hero-case-final-gather-off', HERO, GATHER_SETTLE, setup, capture)
    setup, capture = restore(HERO, 'hero')
    add_phase('hero-restore-final-gather-off', HERO, RESTORE_SETTLE, setup, capture)
    setup, capture = gather_off(MATERIAL, 'material')
    add_phase('material-case-final-gather-off', MATERIAL, GATHER_SETTLE, setup, capture)
    setup, capture = restore(MATERIAL, 'material')
    add_phase('material-restore-final-gather-off', MATERIAL, RESTORE_SETTLE, setup, capture)

    def probe_setup(mode):
        def setup():
            set_lit_clean()
            console('r.Lumen.ScreenProbeGather.Debug 1')
            console('r.Lumen.ScreenProbeGather.Debug.ProbePlacement %d' % mode)
        return setup

    def probe_capture(name, mode, note):
        requested = (SETTINGS['probe_placement']['normals_requested'] if mode == 3
                     else SETTINGS['probe_placement']['repaired_requested'])
        return lambda: capture_image(name, 'diagnostic', HERO, requested, note,
                                     SESSION['captures'])

    add_phase('diag-probe-placement-debug', HERO, DIAG_SETTLE, probe_setup(1),
              probe_capture('diag-hero-probe-placement-debug', 1,
                            'screen probe placement overlay: Debug=1 gates the pass, ProbePlacement=1 draws uniform (yellow) and adaptive (magenta) probe points plus ShaderPrint counts'))
    add_phase('diag-probe-placement-normals', HERO, DIAG_SETTLE, probe_setup(3),
              probe_capture('diag-hero-probe-placement-normals', 3,
                            'screen probe placement overlay mode 3: Debug=1 + ProbePlacement=3 draws probe crosses and normals'))
    add_phase('hero-repair-restored', HERO, FINAL_SETTLE, set_lit_clean,
              lambda: capture_image('lumen-hero-repair-restored', 'validation', HERO,
                                    [], 'baseline restored after FINAL GATHER OFF and probe debug; same saved camera, EV100 11',
                                    SESSION['captures']))


def start_phase():
    if STATE['phase'] >= len(PHASES):
        finish('ok')
        return
    phase = PHASES[STATE['phase']]
    STATE['phase_draws'] = 0
    SESSION['current_phase'] = phase['name']
    try:
        pilot(phase['camera'])
    except Exception as exc:
        SESSION['notes'].append('pilot %s: %s' % (phase['name'], exc))
        log('pilot error %s: %s' % (phase['name'], exc))
    try:
        if phase['setup']:
            phase['setup']()
    except Exception as exc:
        SESSION['notes'].append('setup %s: %s' % (phase['name'], exc))
        log('setup error %s: %s' % (phase['name'], exc))
    try:
        pilot(phase['camera'])
    except Exception as exc:
        SESSION['notes'].append('re-pilot %s: %s' % (phase['name'], exc))
    log('phase %d %s draws=%d camera=%s' % (
        STATE['phase'], phase['name'], phase['draws'], phase['camera'].get_actor_label()))
    atomic_json(CHECKPOINT, {'schema': 'arda-sponza-final-repair-checkpoint-v1',
                             'phase': STATE['phase'], 'name': phase['name'],
                             'draws': STATE['draws'],
                             'session_status': SESSION['status']})


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        BRIDGE.render_viewport()
        STATE['draws'] += 1
        STATE['phase_draws'] += 1
        phase = PHASES[STATE['phase']]
        if STATE['phase_draws'] < phase['draws']:
            return
        if phase['capture']:
            phase['capture']()
        STATE['phase'] += 1
        if STATE['phase'] >= len(PHASES):
            finish('ok')
            return
        start_phase()
    except Exception:
        message = traceback.format_exc()
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        ERROR_LOG.write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_REPAIR_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


def finish(status='ok', error=None):
    global HANDLE
    # Unregister before writing the report or quitting: a stale callback must
    # never outlive this script and fire against a torn-down Python state.
    handle, HANDLE = HANDLE, None
    if handle is not None:
        try:
            unreal.unregister_slate_post_tick_callback(handle)
        except Exception as exc:
            SESSION['notes'].append('unregister: ' + str(exc))
    try:
        SESSION['status'] = status
        if error:
            SESSION['error'] = error
        try:
            set_lit_clean()
            set_exposure(PP, 11.0, 0.0)
            pilot(HERO)
            SESSION['hero_restored_readback'] = pilot_readback(HERO)
            SESSION['material_saved_readback'] = {
                'actor': MATERIAL.get_actor_label(),
                'location_cm': [float(MATERIAL.get_actor_location().x),
                                float(MATERIAL.get_actor_location().y),
                                float(MATERIAL.get_actor_location().z)],
                'horizontal_fov_deg': float(MATERIAL.camera_component.field_of_view),
            }
        except Exception as exc:
            SESSION['notes'].append('finish restore: ' + str(exc))
        SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        SESSION['elapsed_seconds'] = round(time.monotonic() - SESSION['started_monotonic'], 2)
        SESSION['draws'] = STATE['draws']
        SESSION['map_sha256_after'] = sha256_file(MAP_FILE)
        SESSION['map_unchanged'] = SESSION['map_sha256_after'] == SESSION['map_sha256_start']
        SESSION['approved_map_sha256'] = (SETUP.get('hashes', {}) or {}).get('map_sha256_after_save')
        SESSION['approved_map_matches_disk'] = (
            SESSION['approved_map_sha256'] == SESSION['map_sha256_after'])
        SESSION['script_sha256_final'] = sha256_file(SCRIPT_FILE)
        SESSION['roughness_fade_length'] = cvar('r.Lumen.Reflections.RoughnessFadeLength')
        SESSION['final_cvars'] = cvars(CVAR_NAMES)
        atomic_json(RUN_PATH, SESSION)
        log('finish ' + status)
    finally:
        try:
            unreal.EditorPythonScripting.set_keep_python_script_alive(False)
        except Exception:
            pass
        STATE['done'] = True


try:
    SETUP = load_setup()
    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    readbacks, HERO, MATERIAL = apply_config(SETUP, WORLD)
    PP = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}['PostProcess_Sponza']
    set_exposure(PP, 11.0, 0.0)
    pilot(HERO)
    set_lit_clean()
    SESSION['applied_readbacks'] = readbacks
    SESSION['hero_readback'] = pilot_readback(HERO)
    SESSION['material_actor_readback'] = {
        'actor': MATERIAL.get_actor_label(),
        'location_cm': [float(MATERIAL.get_actor_location().x),
                        float(MATERIAL.get_actor_location().y),
                        float(MATERIAL.get_actor_location().z)],
        'horizontal_fov_deg': float(MATERIAL.camera_component.field_of_view),
    }
    SESSION['roughness_fade_length_start'] = cvar('r.Lumen.Reflections.RoughnessFadeLength')
    IMGDIR.mkdir(parents=True, exist_ok=True)
    DIAGDIR.mkdir(parents=True, exist_ok=True)
    build_phases()
    SESSION['phases'] = [p['name'] for p in PHASES]
    atomic_json(RUN_PATH, SESSION)
    start_phase()
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('repair capture started phases=%d' % len(PHASES))
except Exception:
    message = traceback.format_exc()
    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    ERROR_LOG.write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_REPAIR_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass
