"""Temporal Lumen experiment at the fixed approved hero camera.

Warm baseline -> change sun 80000 -> 20000 lux -> capture bridge viewport draws
1, 2, 4, 8, 16, 32, 64 after the change.  The capture itself is the target draw;
extra draws are inserted before it so the target is exact.  Direct (Lumen
visualize mode 12) and indirect (mode 13) signals are captured in separate
repeat runs at frames 1, 8 and 64.  The sun is restored and a final baseline is
captured.

Only bridge viewport-draw identity is claimed.  The engine frame counter is
recorded for every frame so any draw/frame mismatch is visible; the earlier
bridge evidence showed several scene renders can occur per engine frame.

Outputs: output/final/run-temporal.json and output/final/images/temporal-*.png
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
                               console, cvars, load_setup, pilot, pilot_readback,
                               pixel_metrics, set_exposure, sha256_file,
                               MAP_PATH, MAP_FILE, OUTPUT, LOGS)

FINAL = OUTPUT / 'final'
IMGDIR = FINAL / 'images'
RUN_PATH = FINAL / 'run-temporal.json'
CHECKPOINT = FINAL / 'checkpoint-temporal.json'
SCRIPT_FILE = SCRIPTS / 'capture_temporal.py'

SUN_HIGH = 80000.0
SUN_LOW = 20000.0
WARM_DRAWS = 120
JOB_WARM_DRAWS = 64
RESTORE_WARM_DRAWS = 40
TARGETS = [1, 2, 4, 8, 16, 32, 64]
SIGNAL_TARGETS = [1, 8, 64]

JOBS = [
    {'id': 'lit', 'visualize': 0, 'targets': TARGETS,
     'note': 'final lit display'},
    {'id': 'direct', 'visualize': 12, 'targets': SIGNAL_TARGETS,
     'note': 'Lumen direct-lighting visualization mode 12 (display-space overlay)'},
    {'id': 'indirect', 'visualize': 13, 'targets': SIGNAL_TARGETS,
     'note': 'Lumen indirect-lighting visualization mode 13 (display-space overlay)'},
]

CVAR_NAMES = [
    'r.Lumen.Visualize', 'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.Lumen.HardwareRayTracing', 'r.LumenScene.FarField',
    'r.Lumen.ScreenProbeGather.ScreenTraces', 'r.Lumen.Reflections.ScreenTraces',
    'r.Lumen.DiffuseIndirect.Allow', 'r.Lumen.Visualize.ToneMap',
]

SESSION = {
    'schema': 'arda-sponza-temporal-lumen-v1',
    'status': 'running',
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'resolution': [1280, 720],
    'sun_high_lux': SUN_HIGH,
    'sun_low_lux': SUN_LOW,
    'targets_bridge_draws': TARGETS,
    'signal_targets_bridge_draws': SIGNAL_TARGETS,
    'identity_caveat': ('frame numbers are bridge viewport draws after the sun change; '
                        'the engine frame counter is recorded too, and several scene renders '
                        'may occur per engine frame in this capture path'),
    'script_sha256': sha256_file(SCRIPT_FILE),
    'map_sha256_start': sha256_file(MAP_FILE),
    'frames': [], 'jobs': [], 'notes': [], 'limits': [],
}
STATE = {
    'draws': 0, 'busy': False, 'done': False,
    'job_index': 0, 'stage': 'initial-warm', 'warm_remaining': WARM_DRAWS,
    'target_index': 0, 'pending': 0, 'current': 0,
    'sun_changed_frame': None, 'sun_changed_draw': None,
}
SETUP = None
WORLD = None
HERO = None
MATERIAL = None
PP = None
SUN = None
SUN_COMP = None


def log(message):
    unreal.log('ARDA_TEMPORAL ' + str(message))


def frame_count():
    try:
        return int(unreal.SystemLibrary.get_frame_count())
    except Exception:
        return None


def sun_readback():
    return {
        'rotation_degrees': {
            'pitch': float(SUN.get_actor_rotation().pitch),
            'yaw': float(SUN.get_actor_rotation().yaw),
            'roll': float(SUN.get_actor_rotation().roll),
        },
        'intensity_lux': float(SUN_COMP.intensity),
        'temperature_k': float(SUN_COMP.get_editor_property('temperature')),
        'atmosphere_sun_light': bool(SUN_COMP.get_editor_property('atmosphere_sun_light')),
    }


def set_sun(lux):
    SUN_COMP.set_intensity(float(lux))
    log('sun intensity -> %.1f' % lux)


def set_visualize(mode):
    console('r.Lumen.Visualize %d' % mode)


def capture(path, name, note, signal, target):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    before_frame = frame_count()
    before_camera = pilot_readback(HERO)
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    after_frame = frame_count()
    after_camera = pilot_readback(HERO)
    rec = {
        'name': name, 'signal': signal, 'note': note,
        'target_bridge_draw_after_sun_change': target,
        'actual_bridge_draw_after_sun_change': (
            None if signal in ('baseline', 'restored-baseline') else STATE['current'] + 1),
        'capture_draw_total': STATE['draws'],
        'sun_changed_frame': STATE['sun_changed_frame'],
        'sun_changed_draw': STATE['sun_changed_draw'],
        'frame_before': before_frame,
        'frame_after': after_frame,
        'frame_delta': (after_frame - before_frame) if (before_frame is not None and after_frame is not None) else None,
        'sun': sun_readback(),
        'camera_before': before_camera,
        'camera_after': after_camera,
        'cvars': cvars(CVAR_NAMES),
        'file': str(path),
    }
    if path.exists():
        rec.update({'bytes': path.stat().st_size, 'sha256': sha256_file(path)})
        try:
            rec.update(pixel_metrics(path))
        except Exception as exc:
            rec['pixel_metrics_error'] = str(exc)
    SESSION['frames'].append(rec)
    atomic_json(RUN_PATH, SESSION)
    log('captured %s target=%d actual=%d mean=%s' % (
        name, target, STATE['current'], rec.get('mean_display_luma')))
    return rec


def job():
    return JOBS[STATE['job_index']]


def start_job():
    if STATE['job_index'] >= len(JOBS):
        finish('ok')
        return
    j = job()
    STATE['stage'] = 'job-warm'
    STATE['warm_remaining'] = JOB_WARM_DRAWS
    STATE['target_index'] = 0
    STATE['pending'] = 0
    STATE['current'] = 0
    STATE['sun_changed_frame'] = None
    STATE['sun_changed_draw'] = None
    STATE['job_start_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    pilot(HERO)
    set_sun(SUN_HIGH)
    set_exposure(PP, 11.0, 0.0)
    set_visualize(int(j['visualize']))
    log('job %s start visualize=%d' % (j['id'], j['visualize']))
    atomic_json(CHECKPOINT, {'job': j['id'], 'stage': STATE['stage'],
                             'draws': STATE['draws'],
                             'visualize': j['visualize']})


def begin_change():
    j = job()
    set_sun(SUN_LOW)
    STATE['sun_changed_frame'] = frame_count()
    STATE['sun_changed_draw'] = STATE['draws']
    STATE['current'] = 0
    STATE['target_index'] = 0
    STATE['pending'] = 0
    STATE['stage'] = 'run'
    log('job %s sun changed at frame=%s draw=%s' % (
        j['id'], STATE['sun_changed_frame'], STATE['sun_changed_draw']))


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        if STATE['stage'] == 'initial-warm' or STATE['stage'] == 'job-warm' or STATE['stage'] in ('job-restore-warm', 'final-restore-warm'):
            BRIDGE.render_viewport()
            STATE['draws'] += 1
            STATE['warm_remaining'] -= 1
            if STATE['warm_remaining'] <= 0:
                if STATE['stage'] == 'initial-warm':
                    capture(IMGDIR / 'temporal-baseline.png', 'temporal-baseline',
                            'warm Lumen baseline before the sun change', 'baseline', 0)
                    start_job()
                elif STATE['stage'] == 'job-warm':
                    begin_change()
                elif STATE['stage'] == 'job-restore-warm':
                    start_job()
                else:
                    capture(IMGDIR / 'temporal-restored.png', 'temporal-restored',
                            'baseline restored after the temporal experiment', 'restored-baseline', 0)
                    finish('ok')
            return

        if STATE['stage'] == 'run':
            j = job()
            targets = j['targets']
            if STATE['pending'] > 0:
                BRIDGE.render_viewport()
                STATE['draws'] += 1
                STATE['current'] += 1
                STATE['pending'] -= 1
                return
            target = targets[STATE['target_index']]
            if STATE['current'] + 1 < target:
                # The capture below is the next draw, so insert exactly the
                # missing non-capture draws before it.
                STATE['pending'] = target - STATE['current'] - 1
                return
            name = 'temporal-%s-f%03d.png' % (j['id'], target)
            capture(IMGDIR / name, 'temporal-%s-f%03d' % (j['id'], target),
                    'sun %.0f->%.0f lux; %s; capture is bridge draw %d' % (
                        SUN_HIGH, SUN_LOW, j['note'], target),
                    j['id'], target)
            STATE['current'] = target
            STATE['target_index'] += 1
            if STATE['target_index'] >= len(targets):
                set_sun(SUN_HIGH)
                set_visualize(0)
                STATE['warm_remaining'] = RESTORE_WARM_DRAWS
                STATE['job_index'] += 1
                if STATE['job_index'] >= len(JOBS):
                    STATE['stage'] = 'final-restore-warm'
                    log('job %s restore; final baseline warm' % j['id'])
                else:
                    STATE['stage'] = 'job-restore-warm'
                    log('job %s restore; next job warm' % j['id'])
                atomic_json(CHECKPOINT, {'job': j['id'], 'stage': STATE['stage'],
                                         'draws': STATE['draws']})
            return

        raise RuntimeError('unknown temporal stage ' + str(STATE['stage']))
    except Exception:
        message = traceback.format_exc()
        (LOGS / 'temporal-error.txt').write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_TEMPORAL_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


def finish(status='ok', error=None):
    SESSION['status'] = status
    if error:
        SESSION['error'] = error
    try:
        set_sun(SUN_HIGH)
        set_visualize(0)
        set_exposure(PP, 11.0, 0.0)
        pilot(HERO)
        SESSION['restored_sun'] = sun_readback()
        SESSION['restored_camera'] = pilot_readback(HERO)
    except Exception as exc:
        SESSION['notes'].append('finish restore: ' + str(exc))
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = round(time.monotonic() - SESSION['started_monotonic'], 2)
    SESSION['draws'] = STATE['draws']
    SESSION['map_sha256_after'] = sha256_file(MAP_FILE)
    SESSION['map_unchanged'] = SESSION['map_sha256_after'] == SESSION['map_sha256_start']
    SESSION['script_sha256_final'] = sha256_file(SCRIPT_FILE)
    SESSION['cvars_at_end'] = cvars(CVAR_NAMES)
    atomic_json(RUN_PATH, SESSION)
    log('finish ' + status)
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
    actors = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}
    PP = actors['PostProcess_Sponza']
    SUN = actors['Sun_Sponza']
    SUN_COMP = SUN.get_component_by_class(unreal.DirectionalLightComponent)
    set_exposure(PP, 11.0, 0.0)
    pilot(HERO)
    SESSION['hero_readback'] = pilot_readback(HERO)
    SESSION['render_status_start'] = json.loads(BRIDGE.get_render_status())
    SESSION['sun_start'] = sun_readback()
    IMGDIR.mkdir(parents=True, exist_ok=True)
    atomic_json(RUN_PATH, SESSION)
    atomic_json(CHECKPOINT, {'stage': STATE['stage'], 'warm_draws': WARM_DRAWS})
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('temporal capture started')
except Exception:
    message = traceback.format_exc()
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / 'temporal-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_TEMPORAL_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass
