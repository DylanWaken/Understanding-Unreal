"""Final baseline capture: hardware-ray-traced Lumen, clean game view.

Software Lumen is broken in this UE 5.8.1 offscreen editor session (see
lumen-fix-report.md): the Lumen Scene data, mesh cards, surface-cache atlas and
card captures are all populated, but the software SDF trace resolves no valid
mesh-card hit (Surface Cache 99.9% yellow, Lumen Scene 100% black).  Hardware
ray tracing over the same Lumen Scene resolves the cards correctly, so the
baseline below runs with r.Lumen.HardwareRayTracing 1 and records the software
limitation explicitly.

Captures are real editor-viewport screenshots (ArdaLabCapture bridge) from the
first active level viewport switched to game view, with camera/frustum/editor
primitives/show-flag cleanup applied AFTER the view-mode reset.
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
from image_stats import Png  # noqa: E402

OUTPUT = WORKSPACE / 'output'
LOGS = OUTPUT / 'logs'
PREVIEW = OUTPUT / 'preview'
MAP_PATH = '/Game/Labs/SponzaTeaching'

LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
BRIDGE = unreal.ArdaLabCapture
WORLD = None

EV100 = 7.0
BASE_COMMANDS = [
    'r.ScreenPercentage 100',
    'r.AntiAliasingMethod 2',
    'r.MotionBlurQuality 0',
    'r.BloomQuality 0',
    'r.DepthOfFieldQuality 0',
    'r.VolumetricFog 0',
    'r.Lumen.ScreenProbeGather.ScreenTraces 1',
    'r.Lumen.Reflections.ScreenTraces 1',
    'r.DynamicGlobalIlluminationMethod 1',
    'r.ReflectionMethod 1',
    'r.Lumen.HardwareRayTracing 1',
    'r.LumenScene.FarField 1',
    'r.Lumen.Visualize 0',
    'r.Lumen.Visualize.CardPlacement 0',
    'r.LumenScene.SurfaceCache.AtlasSize 4096',
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

HERO_LOC = unreal.Vector(-1000.0, 0.0, 168.0)
HERO_ROT = unreal.Rotator(pitch=2.9338, yaw=0.0, roll=0.0)
MAT_LOC = unreal.Vector(250.0, 0.0, 168.0)
MAT_ROT = unreal.Rotator(pitch=-10.204, yaw=0.0, roll=0.0)

SESSION = {
    'schema': 'arda-sponza-lumen-final-v1',
    'status': 'running',
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'map': MAP_PATH,
    'baseline': {
        'gi': 'Lumen hardware ray tracing (r.Lumen.HardwareRayTracing 1)',
        'software_lumen': 'broken in this session: 99.9% yellow Surface Cache, 100% black Lumen Scene',
        'exposure_ev100': EV100,
        'resolution': [1280, 720],
        'view': 'level viewport game view, clean show flags',
    },
    'base_commands': BASE_COMMANDS,
    'images': {},
    'notes': [],
}
STATE = {'draws': 0, 'step': 0, 'step_draws': 0, 'busy': False, 'done': False}


def log(message):
    unreal.log('ARDA_LUMEN_FINAL ' + str(message))


def atomic_json(path, data):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def console(command):
    unreal.log('ARDA_LUMEN_FINAL_COMMAND ' + command)
    return unreal.SystemLibrary.execute_console_command(WORLD, command)


def vec(v):
    return [round(float(v.x), 3), round(float(v.y), 3), round(float(v.z), 3)]


def rot(v):
    return [round(float(v.pitch), 3), round(float(v.yaw), 3), round(float(v.roll), 3)]


def image_summary(path):
    png = Png(path)
    step = 2
    total = yellow = black = lit = 0
    lum_sum = 0.0
    for y in range(0, png.height, step):
        row = png.rows[y]
        for x in range(0, png.width, step):
            i = x * png.channels
            r, g, b = row[i], row[i + 1], row[i + 2]
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            total += 1
            lum_sum += l
            if r > 190 and g > 190 and b < 90:
                yellow += 1
            if l <= 2:
                black += 1
            if l > 8:
                lit += 1
    return {
        'width': png.width, 'height': png.height,
        'mean_luminance': round(lum_sum / total, 2),
        'yellow_fraction': round(yellow / total, 4),
        'near_black_fraction': round(black / total, 4),
        'nonblack_fraction_lum_gt_8': round(lit / total, 4),
    }


def set_camera(location, rotation):
    ACTORS.clear_actor_selection_set()
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(location, rotation)


def apply_clean_view():
    LEVEL.editor_set_game_view(True)
    BRIDGE.set_viewport_mode('lit', '')
    for command in BASE_COMMANDS:
        console(command)


def capture(name):
    path = PREVIEW / f'{name}.png'
    if path.exists():
        path.unlink()
    BRIDGE.capture_viewport(str(path))
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError('screenshot not written: ' + str(path))
    info = {'file': str(path), 'bytes': path.stat().st_size}
    info.update(image_summary(path))
    SESSION['images'][name] = info
    log('captured %s %s' % (name, json.dumps(info)))
    atomic_json(OUTPUT / 'lumen-final-session.json', SESSION)


def steps():
    return [
        ('warmup', 160, None),
        ('hero-lit', 24, lambda: (set_camera(HERO_LOC, HERO_ROT), apply_clean_view())),
        ('materials-lit', 16, lambda: (set_camera(MAT_LOC, MAT_ROT), apply_clean_view())),
        ('surface-cache', 30, lambda: (set_camera(HERO_LOC, HERO_ROT), apply_clean_view(),
                                       console('r.Lumen.Visualize 5'))),
        ('lumen-scene', 30, lambda: (apply_clean_view(), console('r.Lumen.Visualize 3'))),
        ('lumen-cards', 20, lambda: (apply_clean_view(), console('r.Lumen.Visualize.CardPlacement 1'))),
        ('mesh-sdf', 16, lambda: (apply_clean_view(), console('r.Lumen.Visualize.CardPlacement 0'),
                                  console('showflag.VisualizeMeshDistanceFields 1'))),
        ('gdf-probe', 20, lambda: (set_camera(unreal.Vector(0.0, 0.0, 420.0),
                                              unreal.Rotator(pitch=-55.0, yaw=0.0, roll=0.0)),
                                   apply_clean_view(),
                                   console('r.AOGlobalDistanceField.ForceFullUpdate 1'),
                                   console('showflag.VisualizeGlobalDistanceField 1'))),
        ('dump-stats', 4, None),
    ]


STEPS = []


def start_step():
    if STATE['step'] >= len(STEPS):
        finish('ok')
        return
    name, draws, setup = STEPS[STATE['step']]
    STATE['step_draws'] = 0
    try:
        if setup:
            setup()
    except Exception as exc:
        SESSION['notes'].append('setup %s: %s' % (name, exc))
    log('step %d %s draws=%d' % (STATE['step'], name, draws))


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        BRIDGE.render_viewport()
        STATE['draws'] += 1
        STATE['step_draws'] += 1
        name, draws, setup = STEPS[STATE['step']]
        if name == 'dump-stats' and STATE['step_draws'] == 1:
            console('r.LumenScene.DumpStats 2')
            console('r.LumenScene.DumpStats 4')
        if STATE['step_draws'] < draws:
            return
        if name not in ('warmup', 'dump-stats'):
            capture(name)
        STATE['step'] += 1
        if STATE['step'] >= len(STEPS):
            finish('ok')
            return
        start_step()
    except Exception:
        message = traceback.format_exc()
        (LOGS / 'lumen-final-error.txt').write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_LUMEN_FINAL_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


def finish(status='ok', error=None):
    SESSION['status'] = status
    if error:
        SESSION['error'] = error
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = time.monotonic() - SESSION['started_monotonic']
    SESSION['draws'] = STATE['draws']
    SESSION['render_status_at_end'] = json.loads(BRIDGE.get_render_status())
    atomic_json(OUTPUT / 'lumen-final-session.json', SESSION)
    log('final ' + status)
    try:
        unreal.unregister_slate_post_tick_callback(HANDLE)
    except Exception:
        pass
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


HANDLE = None
try:
    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    actors = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}
    SESSION['actors_found'] = sorted(actors)
    pp = actors['PostProcess_Sponza']
    settings = pp.get_editor_property('settings')
    settings.set_editor_property('auto_exposure_min_brightness', EV100)
    settings.set_editor_property('auto_exposure_max_brightness', EV100)
    settings.set_editor_property('auto_exposure_bias', 0.0)
    settings.set_editor_property('override_auto_exposure_min_brightness', True)
    settings.set_editor_property('override_auto_exposure_max_brightness', True)
    settings.set_editor_property('override_auto_exposure_bias', True)
    pp.set_editor_property('settings', settings)
    set_camera(HERO_LOC, HERO_ROT)
    LEVEL.editor_set_game_view(True)
    BRIDGE.set_viewport_mode('lit', '')
    for command in BASE_COMMANDS:
        console(command)
    SESSION['game_view'] = bool(LEVEL.editor_get_game_view())
    SESSION['cameras'] = {
        'hero': {'actor': 'CAM_Sponza', 'location_cm': vec(actors['CAM_Sponza'].get_actor_location()),
                 'rotation': rot(actors['CAM_Sponza'].get_actor_rotation())},
        'materials': {'actor': 'CAM_Materials', 'location_cm': vec(actors['CAM_Materials'].get_actor_location()),
                      'rotation': rot(actors['CAM_Materials'].get_actor_rotation())},
    }
    STEPS = steps()
    SESSION['steps'] = [s[0] for s in STEPS]
    start_step()
    atomic_json(OUTPUT / 'lumen-final-session.json', SESSION)
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('setup complete steps=' + ','.join(SESSION['steps']))
except Exception:
    message = traceback.format_exc()
    (LOGS / 'lumen-final-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_LUMEN_FINAL_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass

