"""Capture final path-traced references at 256 and 1024 spp on both saved cameras.

1280x720, 8 bounces, denoiser off, VisibleLights=2 (skydome visible),
EnableReferenceAtmosphere=0 (same SkyLight cubemap the Lumen frame uses),
SkylightCaching=0, MISMode=2, MISCompensation=1.  Exposure stays at the approved
EV100 11; nothing is changed to compensate for the path tracer.

The actual accumulation index is GetPathTracingSampleIndex.  It is queried
before the screenshot, after the screenshot and after the camera readback; a
capture whose index drops is moved to output/final/rejected/ and retried.
GetPathTracingSampleCount is recorded but treated as target-only.

Outputs:
  output/final/run-pt.json
  output/final/checkpoint-pt.json
  output/final/images/pt-*.png
  output/final/rejected/pt-*.png
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
from apply_final_setup import (LEVEL, ACTORS, BRIDGE, apply_config, atomic_json,  # noqa: E402
                               cvar, cvars, load_setup, pilot, pilot_readback,
                               pixel_metrics, sha256_file, MAP_PATH, MAP_FILE,
                               OUTPUT, LOGS)

FINAL = OUTPUT / 'final'
IMGDIR = FINAL / 'images'
REJDIR = FINAL / 'rejected'
RUN_PATH = FINAL / 'run-pt.json'
CHECKPOINT = FINAL / 'checkpoint-pt.json'
SCRIPT_FILE = SCRIPTS / 'capture_pt.py'

TARGETS = [
    {'camera': 'hero', 'spp': 256},
    {'camera': 'hero', 'spp': 1024},
    {'camera': 'material', 'spp': 256},
    {'camera': 'material', 'spp': 1024},
]
WARM_SPP = 16
TARGET_TIMEOUT = 480.0
SECOND_CAPTURE = True

CVAR_NAMES = [
    'r.PathTracing.SamplesPerPixel', 'r.PathTracing.MaxBounces',
    'r.PathTracing.Denoiser', 'r.PathTracing.VisibleLights',
    'r.PathTracing.EnableReferenceAtmosphere', 'r.PathTracing.SkylightCaching',
    'r.PathTracing.MISMode', 'r.PathTracing.MISCompensation',
    'r.PathTracing.FilterWidth', 'r.PathTracing.EnableEmissive',
    'r.PathTracing.EnableReferenceDOF', 'r.PathTracing.ProgressDisplay',
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.Lumen.HardwareRayTracing', 'r.LumenScene.FarField',
    'r.SkyLight.RealTimeReflectionCapture',
]

SESSION = {
    'schema': 'arda-sponza-final-path-tracer-v1',
    'status': 'running',
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'resolution': [1280, 720],
    'targets': TARGETS,
    'warm_spp': WARM_SPP,
    'script_sha256': sha256_file(SCRIPT_FILE),
    'map_sha256_start': sha256_file(MAP_FILE),
    'attempts': [], 'rejected': [], 'target_records': [],
    'notes': [], 'limits': [],
    'sample_counter_contract': {
        'accumulation_index': 'GetPathTracingSampleIndex (FSceneViewState::SampleIndex)',
        'target_only': 'GetPathTracingSampleCount returns LastConfig.PathTracingData.MaxSamples, not an observation',
        'acceptance_rule': 'accept only if sample index >= target before screenshot, after screenshot and after camera readback',
    },
}
STATE = {
    'draws': 0, 'phase': 'warm', 'target_index': 0, 'attempt': 0,
    'busy': False, 'done': False, 'wait_start': 0.0, 'wait_draws': 0,
    'index': 0, 'status': {}, 'camera': None,
}
SETUP = None
HERO = None
MATERIAL = None


def log(message):
    unreal.log('ARDA_FINAL_PT ' + str(message))


def console(command):
    unreal.log('ARDA_FINAL_PT_COMMAND ' + command)
    try:
        return unreal.SystemLibrary.execute_console_command(
            unreal.EditorLevelLibrary.get_editor_world(), command)
    except Exception as exc:
        SESSION['notes'].append('console %s: %s' % (command, exc))
        return False


def status():
    try:
        return json.loads(BRIDGE.get_path_tracing_status())
    except Exception as exc:
        return {'error': str(exc)}


def set_pt_mode(target_spp):
    BRIDGE.set_viewport_mode('pathtracing', '')
    commands = [
        'r.PathTracing.MaxBounces 8',
        'r.PathTracing.Denoiser 0',
        'r.PathTracing.ProgressDisplay 0',
        'r.PathTracing.FilterWidth 3.0',
        'r.PathTracing.MISMode 2',
        'r.PathTracing.MISCompensation 1',
        'r.PathTracing.EnableEmissive 1',
        'r.PathTracing.EnableReferenceDOF 0',
        'r.PathTracing.EnableReferenceAtmosphere 0',
        'r.PathTracing.SkylightCaching 0',
        'r.PathTracing.VisibleLights 2',
        'r.PathTracing.SamplesPerPixel %d' % target_spp,
        # force a clean invalidation without touching camera/exposure
        'r.PathTracing.SamplesPerPixel %d' % (target_spp + 1),
        'r.PathTracing.SamplesPerPixel %d' % target_spp,
    ]
    for command in commands:
        console(command)


def write_checkpoint(extra=None):
    data = {
        'schema': 'arda-sponza-final-pt-checkpoint-v1',
        'phase': STATE['phase'],
        'target_index': STATE['target_index'],
        'targets': TARGETS,
        'draws': STATE['draws'],
        'wait_seconds': round(time.monotonic() - STATE['wait_start'], 3) if STATE['wait_start'] else None,
        'observed_sample_index': STATE['index'],
        'status': STATE['status'],
        'attempt': STATE['attempt'],
        'elapsed_seconds': round(time.monotonic() - SESSION['started_monotonic'], 2),
    }
    if extra:
        data.update(extra)
    atomic_json(CHECKPOINT, data)
    atomic_json(RUN_PATH, SESSION)


def camera_for(name):
    return HERO if name == 'hero' else MATERIAL


def start_target():
    if STATE['target_index'] >= len(TARGETS):
        finish('ok')
        return
    target = TARGETS[STATE['target_index']]
    STATE['camera'] = camera_for(target['camera'])
    STATE['phase'] = 'wait'
    STATE['attempt'] = 0
    STATE['wait_start'] = time.monotonic()
    STATE['wait_draws'] = 0
    STATE['index'] = 0
    try:
        pilot(STATE['camera'])
        set_pt_mode(int(target['spp']))
        STATE['status'] = status()
        log('target %d camera=%s spp=%d start index=%s' % (
            STATE['target_index'], target['camera'], target['spp'],
            STATE['status'].get('samples')))
    except Exception as exc:
        SESSION['notes'].append('start target %s: %s' % (target, exc))
        log('target setup error: %s' % exc)
    write_checkpoint()


def capture_attempt():
    target = TARGETS[STATE['target_index']]
    spp = int(target['spp'])
    cam_name = target['camera']
    STATE['attempt'] += 1
    stem = 'pt-%s-%dspp' % (cam_name, spp)
    path = IMGDIR / (stem + '.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    before = status()
    before_rb = pilot_readback(STATE['camera'])
    before_camera_status = status()
    log('capture %s attempt=%d before=%s/%s after_camera_readback=%s' % (
        stem, STATE['attempt'], before.get('samples'), before.get('sample_count'),
        before_camera_status.get('samples')))
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    deadline = time.monotonic() + 180.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    after = status()
    after_camera = pilot_readback(STATE['camera'])
    after_camera_status = status()
    rec = {
        'stem': stem, 'camera': cam_name, 'camera_actor': STATE['camera'].get_actor_label(),
        'spp_target': spp, 'attempt': STATE['attempt'],
        'file': str(path), 'capture_draw': STATE['draws'],
        'before_screenshot': before,
        'before_camera_readback': before_camera_status,
        'after_screenshot': after,
        'camera_readback': after_camera,
        'after_camera_readback': after_camera_status,
        'cvars': cvars(CVAR_NAMES),
    }
    accepted = (int(before.get('samples', -1)) >= spp and
                int(after.get('samples', -1)) >= spp and
                int(before_camera_status.get('samples', -1)) >= spp and
                int(after_camera_status.get('samples', -1)) >= spp and
                path.exists() and path.stat().st_size > 2048)
    rec['accepted'] = accepted
    if path.exists():
        rec.update({'bytes': path.stat().st_size, 'sha256': sha256_file(path)})
        try:
            rec.update(pixel_metrics(path))
        except Exception as exc:
            rec['pixel_metrics_error'] = str(exc)
    if not accepted:
        reason = 'sample index dropped below target across screenshot/readback, or screenshot missing'
        rec['reject_reason'] = reason
        REJDIR.mkdir(parents=True, exist_ok=True)
        rejected_path = REJDIR / (stem + '-attempt%d.png' % STATE['attempt'])
        try:
            if path.exists():
                os.replace(path, rejected_path)
                rec['rejected_file'] = str(rejected_path)
        except Exception as exc:
            rec['rejected_move_error'] = str(exc)
        SESSION['rejected'].append(rec)
        SESSION['attempts'].append(rec)
        STATE['status'] = status()
        STATE['index'] = int(STATE['status'].get('samples', 0))
        STATE['wait_start'] = time.monotonic()
        STATE['wait_draws'] = 0
        if STATE['attempt'] >= 3:
            SESSION['notes'].append('target %s failed after 3 capture attempts' % stem)
            STATE['target_index'] += 1
            start_target()
        else:
            write_checkpoint({'last_reject': stem})
        return

    # Stable primary capture.  A second screenshot at the cap checks that the
    # screenshot path itself did not reset accumulation; it is a stability
    # repeat, not an independent sample.
    second = None
    if SECOND_CAPTURE:
        second_path = IMGDIR / (stem + '-stability-repeat.png')
        if second_path.exists():
            second_path.unlink()
        second_before = status()
        BRIDGE.capture_viewport(str(second_path))
        STATE['draws'] += 1
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if second_path.exists() and second_path.stat().st_size > 2048:
                break
            time.sleep(0.05)
        second_after = status()
        second = {
            'file': str(second_path),
            'before_screenshot': second_before,
            'after_screenshot': second_after,
            'accepted': int(second_before.get('samples', -1)) >= spp and
                        int(second_after.get('samples', -1)) >= spp,
            'role': 'stability repeat, not an independent sample',
        }
        if second_path.exists():
            second.update({'bytes': second_path.stat().st_size,
                           'sha256': sha256_file(second_path)})
        rec['stability_repeat'] = second
    SESSION['attempts'].append(rec)
    SESSION['target_records'].append(rec)
    atomic_json(RUN_PATH, SESSION)
    log('accepted %s observed=%s after=%s after_readback=%s' % (
        stem, before.get('samples'), after.get('samples'),
        after_camera_status.get('samples')))
    STATE['target_index'] += 1
    start_target()


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        if STATE['phase'] == 'warm':
            BRIDGE.render_viewport()
            STATE['draws'] += 1
            STATE['wait_draws'] += 1
            st = status()
            STATE['index'] = int(st.get('samples', 0))
            if STATE['wait_draws'] % 20 == 0:
                log('warm draw=%d index=%d' % (STATE['draws'], STATE['index']))
            if STATE['index'] >= WARM_SPP:
                SESSION['warmup'] = {
                    'observed_index': STATE['index'],
                    'status': st,
                    'draws': STATE['wait_draws'],
                    'cvars': cvars(CVAR_NAMES),
                }
                log('warmup complete index=%d' % STATE['index'])
                start_target()
            elif time.monotonic() - STATE['wait_start'] > 300.0:
                SESSION['notes'].append('warmup timeout at index %d' % STATE['index'])
                start_target()
            return

        target = TARGETS[STATE['target_index']]
        spp = int(target['spp'])
        if STATE['phase'] == 'wait':
            BRIDGE.render_viewport()
            STATE['draws'] += 1
            STATE['wait_draws'] += 1
            st = status()
            STATE['index'] = int(st.get('samples', 0))
            STATE['status'] = st
            if STATE['wait_draws'] % 50 == 0:
                elapsed = time.monotonic() - STATE['wait_start']
                log('target %d spp=%d draw=%d index=%d elapsed=%.1f' % (
                    STATE['target_index'], spp, STATE['wait_draws'],
                    STATE['index'], elapsed))
                write_checkpoint()
            if STATE['index'] >= spp:
                capture_attempt()
                return
            if time.monotonic() - STATE['wait_start'] > TARGET_TIMEOUT:
                rec = {
                    'camera': target['camera'], 'spp_target': spp,
                    'accepted': False,
                    'reject_reason': 'accumulation timeout',
                    'last_index': STATE['index'],
                    'last_status': STATE['status'],
                    'cvars': cvars(CVAR_NAMES),
                }
                SESSION['rejected'].append(rec)
                SESSION['notes'].append('target %s/%d timeout at index %d' % (
                    target['camera'], spp, STATE['index']))
                STATE['target_index'] += 1
                start_target()
    except Exception:
        message = traceback.format_exc()
        (LOGS / 'final-pt-error.txt').write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_FINAL_PT_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


def finish(status_value='ok', error=None):
    SESSION['status'] = status_value
    if error:
        SESSION['error'] = error
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = round(time.monotonic() - SESSION['started_monotonic'], 2)
    SESSION['draws'] = STATE['draws']
    SESSION['map_sha256_after'] = sha256_file(MAP_FILE)
    SESSION['map_unchanged'] = SESSION['map_sha256_after'] == SESSION['map_sha256_start']
    SESSION['script_sha256_final'] = sha256_file(SCRIPT_FILE)
    SESSION['cvars_at_end'] = cvars(CVAR_NAMES)
    atomic_json(RUN_PATH, SESSION)
    log('finish ' + status_value)
    try:
        unreal.unregister_slate_post_tick_callback(HANDLE)
    except Exception:
        pass
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


HANDLE = None
try:
    SETUP = load_setup()
    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    readbacks, HERO, MATERIAL = apply_config(SETUP, WORLD)
    SESSION['pre_apply_saved_state'] = readbacks.get('pre_apply_saved_state')
    SESSION['applied_readbacks'] = readbacks
    SESSION['setup_path_tracer'] = SETUP['config']['path_tracer']
    PILOT_BEFORE = pilot(HERO)
    SESSION['hero_readback_before'] = pilot_readback(HERO)
    IMGDIR.mkdir(parents=True, exist_ok=True)
    REJDIR.mkdir(parents=True, exist_ok=True)
    pilot(HERO)
    set_pt_mode(WARM_SPP)
    STATE['wait_start'] = time.monotonic()
    STATE['status'] = status()
    SESSION['warmup_start_status'] = STATE['status']
    SESSION['render_status_start'] = json.loads(BRIDGE.get_render_status())
    atomic_json(RUN_PATH, SESSION)
    write_checkpoint()
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('PT capture started')
except Exception:
    message = traceback.format_exc()
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / 'final-pt-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_FINAL_PT_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass
