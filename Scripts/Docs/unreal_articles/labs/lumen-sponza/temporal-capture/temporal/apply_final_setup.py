"""Apply the authoritative Sponza final setup to the copied teaching level.

Reads output/final-setup.json and restores the saved hero camera, material
camera, sun, SkyLight, pinned EV100 and display/Lumen state into the level, then
saves the map.  This is the reusable reproduction entry point: the public flow
is download/split/import/author -> apply_final_setup.py -> capture.

Nothing here depends on the root clipboard images or the old camera/lighting
sweeps.  The only source of truth is output/final-setup.json.

Standalone (inside Unreal):
    UnrealEditor.exe <project> -ExecutePythonScript=<this file>

Optional verification capture during the apply run:
    set ARDA_APPLY_VERIFY=1
Writes output/final/apply/apply-hero-lumen.png and records its hash, but only
when explicitly requested; the normal reproduction applies and exits.
"""
import hashlib
import json
import math
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

OUTPUT = WORKSPACE / 'output'
LOGS = OUTPUT / 'logs'
SETUP_PATH = OUTPUT / 'final-setup.json'
MAP_PATH = '/Game/Labs/SponzaTeaching'
MAP_FILE = WORKSPACE / 'project' / 'UnrealLab' / 'Content' / 'Labs' / 'SponzaTeaching.umap'
PROJECT_FILE = WORKSPACE / 'project' / 'UnrealLab' / 'UnrealLab.uproject'
ENGINE_ROOT = Path(os.environ.get('ARDA_ENGINE_ROOT', r'D:\UnrealEngine'))
EDITOR_EXE = ENGINE_ROOT / 'Engine' / 'Binaries' / 'Win64' / 'UnrealEditor.exe'
BUILD_VERSION = ENGINE_ROOT / 'Engine' / 'Build' / 'Build.version'
BRIDGE_DLL = WORKSPACE / 'project' / 'UnrealLab' / 'Binaries' / 'Win64' / 'UnrealEditor-ArdaLabCapture.dll'

LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
BRIDGE = unreal.ArdaLabCapture
WORLD = None

CVAR_NAMES = [
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.Lumen.HardwareRayTracing', 'r.LumenScene.FarField',
    'r.Lumen.ScreenProbeGather.ScreenTraces', 'r.Lumen.Reflections.ScreenTraces',
    'r.LumenScene.SurfaceCache.AtlasSize', 'r.Lumen.Visualize',
    'r.PathTracing.SamplesPerPixel', 'r.PathTracing.MaxBounces',
    'r.PathTracing.Denoiser', 'r.PathTracing.VisibleLights',
    'r.PathTracing.EnableReferenceAtmosphere', 'r.PathTracing.SkylightCaching',
    'r.PathTracing.MISMode', 'r.PathTracing.MISCompensation',
    'r.DistanceFields.Visualize.Monochrome',
    'r.SkyLight.RealTimeReflectionCapture',
]

DISPLAY_COMMANDS = [
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
    'r.LumenScene.FarField 0',
    'r.LumenScene.SurfaceCache.AtlasSize 4096',
    'r.Lumen.Visualize 0',
    'r.Lumen.Visualize.CardPlacement 0',
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
    'r.PathTracing.EnableReferenceAtmosphere 1',
]


def log(message):
    unreal.log('ARDA_APPLY_FINAL_SETUP ' + str(message))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_setup(path=None):
    return json.loads(Path(path or SETUP_PATH).read_text(encoding='utf-8-sig'))


def vec(v):
    return [round(float(v.x), 4), round(float(v.y), 4), round(float(v.z), 4)]


def rot(v):
    return {'pitch': round(float(v.pitch), 5), 'yaw': round(float(v.yaw), 5),
            'roll': round(float(v.roll), 5)}


def console(command):
    unreal.log('ARDA_APPLY_FINAL_SETUP_COMMAND ' + command)
    return unreal.SystemLibrary.execute_console_command(WORLD, command)


def cvar(name):
    try:
        return json.loads(BRIDGE.get_console_variable(name))
    except Exception as exc:
        return {'name': name, 'found': False, 'error': str(exc)}


def cvars(names=None):
    return {name: cvar(name) for name in (names or CVAR_NAMES)}


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
    return {
        'auto_exposure_min_brightness': float(settings.get_editor_property('auto_exposure_min_brightness')),
        'auto_exposure_max_brightness': float(settings.get_editor_property('auto_exposure_max_brightness')),
        'auto_exposure_bias': float(settings.get_editor_property('auto_exposure_bias')),
        'override_auto_exposure_min_brightness': bool(settings.get_editor_property('override_auto_exposure_min_brightness')),
        'override_auto_exposure_max_brightness': bool(settings.get_editor_property('override_auto_exposure_max_brightness')),
        'override_auto_exposure_bias': bool(settings.get_editor_property('override_auto_exposure_bias')),
        'auto_exposure_method': str(settings.get_editor_property('auto_exposure_method')),
    }


def set_camera(camera, spec):
    camera.set_actor_location(unreal.Vector(*[float(v) for v in spec['location_cm']]), False, False)
    rotation = spec['rotation_degrees']
    camera.set_actor_rotation(unreal.Rotator(pitch=float(rotation['pitch']),
                                             yaw=float(rotation['yaw']),
                                             roll=float(rotation['roll'])), False)
    component = camera.camera_component
    component.set_field_of_view(float(spec['horizontal_fov_deg']))
    try:
        component.set_editor_property('constrain_aspect_ratio', False)
    except Exception:
        pass
    try:
        component.set_editor_property('post_process_blend_weight', 1.0)
    except Exception:
        pass


def find_or_spawn_camera(spec, world):
    label = spec['actor']
    matches = [a for a in ACTORS.get_all_level_actors()
               if a.get_actor_label() == label and isinstance(a, unreal.CameraActor)]
    if len(matches) > 1:
        want_loc = spec['location_cm']
        want_rot = spec['rotation_degrees']

        def distance(actor):
            loc = actor.get_actor_location()
            rotation = actor.get_actor_rotation()
            return (abs(loc.x - want_loc[0]) + abs(loc.y - want_loc[1]) + abs(loc.z - want_loc[2]) +
                    abs(rotation.pitch - want_rot['pitch']) +
                    abs(rotation.yaw - want_rot['yaw']) +
                    abs(rotation.roll - want_rot['roll']))

        matches.sort(key=distance)
        keep = matches[0]
        renamed = []
        for index, extra in enumerate(matches[1:], start=1):
            old_label = extra.get_actor_label()
            extra.set_actor_label('%s_Obsolete_%d' % (label, index))
            renamed.append({'old_label': old_label,
                            'new_label': extra.get_actor_label(),
                            'location_cm': vec(extra.get_actor_location())})
        return keep, False, renamed
    if matches:
        return matches[0], False, []
    rotation = spec['rotation_degrees']
    camera = ACTORS.spawn_actor_from_class(
        unreal.CameraActor,
        unreal.Vector(*[float(v) for v in spec['location_cm']]),
        unreal.Rotator(pitch=float(rotation['pitch']), yaw=float(rotation['yaw']),
                       roll=float(rotation['roll'])))
    camera.set_actor_label(label)
    return camera, True, []


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
    rec['render_status'] = json.loads(BRIDGE.get_render_status())
    return rec


def actor_readback(camera):
    return {
        'actor': camera.get_actor_label(),
        'location_cm': vec(camera.get_actor_location()),
        'rotation_degrees': rot(camera.get_actor_rotation()),
        'horizontal_fov_deg': float(camera.camera_component.field_of_view),
    }


def scene_actors():
    return {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}


def saved_state(actor_map):
    state = {}
    hero = actor_map.get('CAM_Hero_Reference')
    material = actor_map.get('CAM_Materials')
    sun = actor_map.get('Sun_Sponza')
    sky = actor_map.get('SkyLight_Sponza')
    pp = actor_map.get('PostProcess_Sponza')
    if hero:
        state['hero_camera'] = {
            'actor': hero.get_actor_label(),
            'location_cm': vec(hero.get_actor_location()),
            'rotation_degrees': rot(hero.get_actor_rotation()),
            'horizontal_fov_deg': float(hero.camera_component.field_of_view),
        }
    if material:
        state['material_camera'] = {
            'actor': material.get_actor_label(),
            'location_cm': vec(material.get_actor_location()),
            'rotation_degrees': rot(material.get_actor_rotation()),
            'horizontal_fov_deg': float(material.camera_component.field_of_view),
        }
    if sun:
        comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
        state['sun'] = {
            'rotation_degrees': rot(sun.get_actor_rotation()),
            'intensity_lux': float(comp.intensity),
            'temperature_k': float(comp.get_editor_property('temperature')),
            'atmosphere_sun_light': bool(comp.get_editor_property('atmosphere_sun_light')),
        }
    if sky:
        comp = sky.get_component_by_class(unreal.SkyLightComponent)
        state['sky_light'] = {
            'intensity': float(comp.intensity),
            'real_time_capture': bool(comp.get_editor_property('real_time_capture')),
        }
    if pp:
        state['exposure'] = read_exposure(pp)
    return state


def apply_config(setup, world):
    global WORLD
    WORLD = world
    config = setup['config']
    actors = scene_actors()
    required = ['PostProcess_Sponza', 'Sun_Sponza', 'SkyLight_Sponza']
    missing = [label for label in required if label not in actors]
    if missing:
        raise RuntimeError('required level actors missing: ' + ', '.join(missing))

    pre = saved_state(actors)
    hero, hero_spawned, hero_duplicates = find_or_spawn_camera(config['hero_camera'], world)
    material, material_spawned, material_duplicates = find_or_spawn_camera(config['material_camera'], world)
    set_camera(hero, config['hero_camera'])
    set_camera(material, config['material_camera'])

    sun = actors['Sun_Sponza']
    sun_spec = config['light']['sun']
    sun_comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
    sun.set_actor_rotation(unreal.Rotator(pitch=float(sun_spec['rotation_degrees']['pitch']),
                                          yaw=float(sun_spec['rotation_degrees']['yaw']),
                                          roll=float(sun_spec['rotation_degrees']['roll'])), False)
    sun_comp.set_intensity(float(sun_spec['intensity_lux']))
    sun_comp.set_editor_property('temperature', float(sun_spec['temperature_k']))
    sun_comp.set_editor_property('atmosphere_sun_light', bool(sun_spec['atmosphere_sun_light']))
    try:
        sun_comp.set_editor_property('light_source_angle', float(sun_spec['light_source_angle_deg']))
    except Exception:
        pass

    sky = actors['SkyLight_Sponza']
    sky_spec = config['light']['sky_light']
    sky_comp = sky.get_component_by_class(unreal.SkyLightComponent)
    sky_comp.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky_comp.set_editor_property('real_time_capture', bool(sky_spec['real_time_capture']))
    sky_comp.set_intensity(float(sky_spec['intensity']))

    pp = actors['PostProcess_Sponza']
    light = config['light']
    set_exposure(pp, float(light['exposure_ev100']), float(light.get('exposure_bias', 0.0)))

    for command in DISPLAY_COMMANDS:
        console(command)
    pilot(hero)

    post = saved_state(scene_actors())
    return {
        'pre_apply_saved_state': pre,
        'hero_camera_spawned': hero_spawned,
        'material_camera_spawned': material_spawned,
        'renamed_duplicate_hero_cameras': hero_duplicates,
        'renamed_duplicate_material_cameras': material_duplicates,
        'post_apply_saved_state': post,
        'hero_camera_readback': pilot_readback(hero),
        'material_camera_readback': actor_readback(material),
        'material_camera_note': 'material detail camera is saved and read back from its actor; the viewport is deliberately left piloted on the hero camera',
        'cvars_applied': cvars(),
    }, hero, material


def runtime_hashes(setup):
    hashes = {
        'map_sha256': sha256_file(MAP_FILE),
        'project_uproject_sha256': sha256_file(PROJECT_FILE),
        'setup_sha256': sha256_file(SETUP_PATH),
        'apply_script_sha256': sha256_file(Path(__file__).resolve()),
        'approved_image_sha256': setup.get('approved_image', {}).get('sha256'),
        'unreal_editor_sha256': sha256_file(EDITOR_EXE) if EDITOR_EXE.exists() else None,
        'engine_build_version_sha256': sha256_file(BUILD_VERSION) if BUILD_VERSION.exists() else None,
        'bridge_dll_sha256': sha256_file(BRIDGE_DLL) if BRIDGE_DLL.exists() else None,
    }
    try:
        hashes['engine_build_version'] = json.loads(BUILD_VERSION.read_text(encoding='utf-8-sig'))
    except Exception as exc:
        hashes['engine_build_version_error'] = str(exc)
    try:
        hashes['bridge_dll_paths'] = [str(p) for p in
                                      (WORKSPACE / 'project' / 'UnrealLab' / 'Binaries').rglob('*ArdaLabCapture*')]
    except Exception:
        pass
    return hashes


def pixel_metrics(path):
    from image_stats import Png
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
        'width': png.width, 'height': png.height,
        'mean_display_luma': round(mean, 3),
        'std_display_luma': round(max(lum_sq / total - mean * mean, 0.0) ** 0.5, 3),
        'near_black_fraction': round(black / total, 5) if total else None,
        'near_white_fraction': round(white / total, 5) if total else None,
    }


def write_setup(setup, readbacks, hashes, status):
    setup['status'] = status
    setup['readbacks'] = readbacks
    setup['hashes'] = hashes
    setup['updated_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    atomic_json(SETUP_PATH, setup)


def verification_capture(setup, readbacks, hashes, hero):
    out = OUTPUT / 'final' / 'apply' / 'apply-hero-lumen.png'
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    state = {'draws': 0, 'start': time.monotonic(), 'handle': None, 'done': False}

    def finish(captured):
        readbacks['apply_verification'] = {
            'file': str(out),
            'exists': out.exists(),
            'captured': captured,
            'draws': state['draws'],
            'warmup_draws': 120,
            'hero_camera_readback': pilot_readback(hero),
        }
        if out.exists():
            readbacks['apply_verification']['sha256'] = sha256_file(out)
            readbacks['apply_verification']['bytes'] = out.stat().st_size
            try:
                readbacks['apply_verification'].update(pixel_metrics(out))
            except Exception as exc:
                readbacks['apply_verification']['pixel_metrics_error'] = str(exc)
            hashes['apply_frame_sha256'] = sha256_file(out)
        write_setup(setup, readbacks, hashes, 'ok' if captured else 'apply_capture_failed')
        try:
            unreal.unregister_slate_post_tick_callback(state['handle'])
        except Exception:
            pass
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)
        state['done'] = True
        log('verification finish captured=%s draws=%d' % (captured, state['draws']))

    def tick(delta):
        if state['done']:
            return
        try:
            BRIDGE.render_viewport()
            state['draws'] += 1
            if state['draws'] >= 120:
                BRIDGE.capture_viewport(str(out))
                state['draws'] += 1
                deadline = time.monotonic() + 120.0
                while time.monotonic() < deadline:
                    if out.exists() and out.stat().st_size > 2048:
                        break
                    time.sleep(0.05)
                finish(out.exists() and out.stat().st_size > 2048)
            elif time.monotonic() - state['start'] > 300.0:
                finish(False)
        except Exception:
            message = traceback.format_exc()
            (LOGS / 'apply-final-setup-error.txt').write_text(message, encoding='utf-8')
            unreal.log_error('ARDA_APPLY_FINAL_SETUP_ERROR ' + message)
            finish(False)

    state['handle'] = unreal.register_slate_post_tick_callback(tick)
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    log('verification capture scheduled')


def main():
    global WORLD
    setup = load_setup()
    if setup.get('schema') != 'arda-sponza-final-setup-v2':
        raise RuntimeError('unexpected final-setup schema: ' + str(setup.get('schema')))
    map_before = sha256_file(MAP_FILE)
    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    if WORLD is None:
        raise RuntimeError('no editor world after load')

    readbacks, hero, material = apply_config(setup, WORLD)
    readbacks['map_sha256_before_load'] = map_before
    readbacks['saved_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    if not unreal.EditorLoadingAndSavingUtils.save_map(WORLD, MAP_PATH):
        raise RuntimeError('save_map failed')
    readbacks['map_sha256_after_save'] = sha256_file(MAP_FILE)
    readbacks['saved_state_after_save'] = saved_state(scene_actors())
    readbacks['saved_hero_camera_readback'] = pilot_readback(hero)
    readbacks['saved_material_camera_readback'] = actor_readback(material)
    readbacks['saved_material_camera_note'] = 'actor readback; viewport remains piloted on CAM_Hero_Reference'
    readbacks['render_status'] = json.loads(BRIDGE.get_render_status())
    readbacks['cvars_after_save'] = cvars()
    hashes = runtime_hashes(setup)
    hashes['map_sha256_before_load'] = map_before
    hashes['map_sha256_after_save'] = readbacks['map_sha256_after_save']
    write_setup(setup, readbacks, hashes, 'applied_and_saved')
    log('applied and saved map sha256=' + readbacks['map_sha256_after_save'])

    if os.environ.get('ARDA_APPLY_VERIFY', '0') == '1':
        verification_capture(setup, readbacks, hashes, hero)
    else:
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        message = traceback.format_exc()
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / 'apply-final-setup-error.txt').write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_APPLY_FINAL_SETUP_ERROR ' + message)
        raise
