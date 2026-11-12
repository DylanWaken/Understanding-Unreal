"""Capture the existing Lumen laboratory room with Lumen and Unreal's path tracer.

Run inside the isolated UnrealLab project with -ExecutePythonScript=<this file>.
The room is the saved /Game/Labs/LumenRoom map copied byte-for-byte from the
original laboratory run.  No geometry, material, light or camera is redesigned.
The only authored changes are the capture-time view mode and path-tracer CVars.
"""
import hashlib
import json
import math
import os
import time
import traceback
from pathlib import Path

import unreal

ROOT = Path(os.environ.get('ARDA_REFERENCE_ROOT', str(Path(__file__).resolve().parent.parent / 'output')))
PROJECT = Path(os.environ.get('ARDA_PROJECT_ROOT', str(Path(__file__).resolve().parent.parent / 'UnrealLab')))
CAPTURES = Path(os.environ.get('ARDA_REFERENCE_OUTPUT', str(ROOT / 'captures')))
CAPTURES.mkdir(parents=True, exist_ok=True)
(ROOT / 'logs').mkdir(parents=True, exist_ok=True)
LEVEL_PATH = '/Game/Labs/LumenRoom'
RESOLUTION = (1280, 720)
LUMEN_WARMUP_DRAWS = 200       # same initial warm-up as the original capture.py
LUMEN_SETTLE_DRAWS = 90        # same per-state settle as the original capture
TARGET_SPP = 256
TARGET_BOUNCES = 8
PT_TIMEOUT_SECONDS = 30 * 60
PT_PSO_WARMUP_DRAWS = 64       # compile RTPSOs and discard those frames before counting

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
PT_COMMANDS = [
    'r.PathTracing.SamplesPerPixel 256',
    'r.PathTracing.MaxBounces 8',
    'r.PathTracing.Denoiser 0',
    'r.PathTracing.ProgressDisplay 0',
    'r.PathTracing.FilterWidth 3.0',
    'r.PathTracing.MISMode 2',
    'r.PathTracing.EnableEmissive 1',
    'r.PathTracing.EnableReferenceDOF 0',
    'r.PathTracing.VisibleLights 0',
]


def atomic_json(path, data):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def vec(v):
    return [float(v.x), float(v.y), float(v.z)]


def rot(v):
    return {'pitch': float(v.pitch), 'yaw': float(v.yaw), 'roll': float(v.roll)}


def console(command):
    unreal.log('ARDA_REFERENCE_COMMAND ' + command)
    unreal.SystemLibrary.execute_console_command(WORLD, command)


def progress(state, **extra):
    data = {
        'step': 'reference',
        'state': state,
        'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'pid': os.getpid(),
        'project': str(PROJECT),
        'map': LEVEL_PATH,
        'resolution': list(RESOLUTION),
        'lumen_warmup_draws': LUMEN_WARMUP_DRAWS,
        'lumen_settle_draws': LUMEN_SETTLE_DRAWS,
        'target_samples_per_pixel': TARGET_SPP,
        'target_bounces': TARGET_BOUNCES,
    }
    data.update(extra)
    atomic_json(ROOT / 'progress.json', data)


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
        try:
            origin, extent, radius = unreal.SystemLibrary.get_component_bounds(component)
            c['bounds_origin_cm'] = vec(origin)
            c['bounds_extent_cm'] = vec(extent)
            c['bounds_sphere_radius_cm'] = float(radius)
        except Exception:
            pass
        if isinstance(component, unreal.StaticMeshComponent):
            c['mesh'] = component.static_mesh.get_path_name() if component.static_mesh else None
        record['components'].append(c)
    return record


def expected_scene():
    path = Path(os.environ.get('ARDA_SCENE_INVENTORY', str(ROOT / 'source-reference' / 'scene-inventory.json')))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding='utf-8'))


def compare_scene(scene, original):
    if not original:
        return {'available': False}
    expected = {a['label']: a for a in original['actors']}
    actual = {a['label']: a for a in scene['actors']}
    problems = []
    for label, exp in expected.items():
        act = actual.get(label)
        if act is None:
            problems.append(f'missing actor {label}')
            continue
        for key in ('location_cm', 'scale'):
            for i, (a, b) in enumerate(zip(exp.get(key, []), act.get(key, []))):
                if abs(a - b) > 0.01:
                    problems.append(f'{label}.{key}[{i}] {b} != original {a}')
        if exp.get('class') != act.get('class'):
            problems.append(f'{label}.class {act.get("class")} != original {exp.get("class")}')
    extra = sorted(set(actual) - set(expected))
    if extra:
        problems.append(f'extra actors {extra}')
    return {
        'available': True,
        'source': 'source-reference/scene-inventory.json',
        'actor_count_expected': len(expected),
        'actor_count_actual': len(actual),
        'matched': not problems,
        'problems': problems,
    }


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
        'override_path_tracing_max_path_intensity', 'path_tracing_max_path_intensity',
    ]
    values = {}
    for key in keys:
        try:
            values[key] = settings.get_editor_property(key)
        except Exception:
            continue
    return settings, values


def picture_file(path):
    p = Path(path)
    for _ in range(300):
        if p.exists() and p.stat().st_size > 1024:
            return {'file': str(p), 'bytes': p.stat().st_size, 'sha256': sha256(p),
                    'modified_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(p.stat().st_mtime))}
        time.sleep(0.1)
    raise RuntimeError(f'Screenshot was not written: {p}')


def setup():
    global WORLD
    progress('loading')
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

    # Re-apply the original capture camera exactly (capture.py: (0,-400,165) -> (0,280,140)).
    location = (0.0, -400.0, 165.0)
    target = (0.0, 280.0, 140.0)
    rotation = unreal.MathLibrary.find_look_at_rotation(unreal.Vector(*location), unreal.Vector(*target))
    camera.set_actor_location(unreal.Vector(*location), False, False)
    camera.set_actor_rotation(rotation, False)
    camera.camera_component.set_field_of_view(math.degrees(2 * math.atan(math.tan(math.radians(43 / 2)) * 16 / 9)))
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(camera.get_actor_location(), rotation)
    ACTORS.clear_actor_selection_set()
    LEVEL.editor_set_game_view(True)
    LEVEL.pilot_level_actor(camera)

    settings, pp_values = get_pp_settings(pp)
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

    scene = {
        'schema': 'unreal-lumen-reference-scene-v1',
        'captured_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'engine_version': unreal.SystemLibrary.get_engine_version(),
        'project': str(PROJECT),
        'map': LEVEL_PATH,
        'map_sha256': sha256(PROJECT / 'Content' / 'Labs' / 'LumenRoom.umap'),
        'room_source': 'copied saved map /Game/Labs/LumenRoom from the original laboratory run',
        'coordinate_mapping': 'UE centimeters = (-100*CPU x, 100*CPU y, 100*CPU z)',
        'capture_resolution': list(RESOLUTION),
        'camera': {
            'actor': camera.get_actor_label(),
            'location_cm': vec(camera.get_actor_location()),
            'rotation_degrees': rot(camera.get_actor_rotation()),
            'target_cm': list(target),
            'vertical_fov_degrees': 43.0,
            'component_field_of_view_degrees': float(camera.camera_component.field_of_view),
            'note': 'Camera component FOV is Unreal horizontal FOV; 43 deg vertical at 16:9 yields this value.',
        },
        'exposure': {
            'method': 'histogram (AEM_HISTOGRAM) with min_brightness == max_brightness == EV100 4',
            'effective_method': 'histogram metering pinned to EV100 4 by min=max=4 and bias=0; not manual mode',
            'auto_exposure_method': pp_values.get('auto_exposure_method'),
            'ev100': 4.0,
            'min_brightness': 4.0,
            'max_brightness': 4.0,
            'bias': 0.0,
            'postprocess_values_after_set': {k: str(v) for k, v in pp_values.items()},
        },
        'lighting': {
            'rect_light': {
                'actor': light.get_actor_label(),
                'location_cm': vec(light.get_actor_location()),
                'rotation_degrees': rot(light.get_actor_rotation()),
                'intensity_lumens': float(light.get_component_by_class(unreal.RectLightComponent).intensity),
                'intensity_units': str(light.get_component_by_class(unreal.RectLightComponent).intensity_units),
                'source_width_cm': float(light.get_component_by_class(unreal.RectLightComponent).source_width),
                'source_height_cm': float(light.get_component_by_class(unreal.RectLightComponent).source_height),
                'attenuation_radius_cm': float(light.get_component_by_class(unreal.RectLightComponent).attenuation_radius),
                'light_color': [float(light.get_component_by_class(unreal.RectLightComponent).light_color.r),
                                float(light.get_component_by_class(unreal.RectLightComponent).light_color.g),
                                float(light.get_component_by_class(unreal.RectLightComponent).light_color.b)],
            },
            'sky': 'none authored; engine defaults retained',
            'lumen': 'software tracing; hardware ray tracing disabled',
        },
        'render_settings_ini': (PROJECT / 'Config' / 'DefaultEngine.ini').read_text(encoding='utf-8'),
        'lumen_commands': LUMEN_COMMANDS,
        'actors': [actor_record(a) for a in all_actors],
    }
    original = expected_scene()
    scene['verification_vs_original_scene_inventory'] = compare_scene(scene, original)
    atomic_json(ROOT / 'scene.json', scene)
    if (os.environ.get('ARDA_REQUIRE_SCENE_MATCH', '0') == '1' and
            not scene['verification_vs_original_scene_inventory'].get('matched', False)):
        raise RuntimeError('Loaded room does not match the original scene inventory: ' +
                           json.dumps(scene['verification_vs_original_scene_inventory'], indent=2))
    return camera, pp, light, scene


SESSION = {
    'schema': 'unreal-reference-capture-session-v1',
    'status': 'running',
    'pid': os.getpid(),
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'engine_version': unreal.SystemLibrary.get_engine_version(),
    'project': str(PROJECT),
    'map': LEVEL_PATH,
    'resolution': list(RESOLUTION),
    'lumen_warmup_draws': LUMEN_WARMUP_DRAWS,
    'lumen_settle_draws': LUMEN_SETTLE_DRAWS,
    'lumen_commands': LUMEN_COMMANDS,
    'path_tracer_commands': PT_COMMANDS,
    'target_samples_per_pixel': TARGET_SPP,
    'target_bounces': TARGET_BOUNCES,
    'pso_warmup_draws': PT_PSO_WARMUP_DRAWS,
    'denoiser': 'disabled (r.PathTracing.Denoiser 0; no denoiser plugin assumed)',
    'draws': 0,
    'path_tracer': {},
    'images': {},
    'limitations': [
        'Editor viewport capture, not Movie Render Queue or a packaged build.',
        'The editor viewport is SDR (GetSceneHDREnabled false), so the capture path writes display-referred 8-bit PNG; no linear EXR is produced by this procedure.',
        'Path-tracer sample accumulation is not one sample per viewport draw: 256 samples accumulated across about 86 explicit draw requests (sample reset/first sample at draw 357, nonzero progression from draw 358, capture draw 443). The engine GetPathTracingSampleIndex() is authoritative; this is not a timing or performance benchmark.',
        'Lumen screen-probe/radiosity temporal state is warmed but not proven converged; stochastic noise is expected.',
        'The saved room keeps its recorded RectLight attenuation radius (1000 cm), even though the original builder requested 2000 cm.',
    ],
}

STATE = {
    'phase': 'lumen_warmup',
    'busy': False,
    'done': False,
    'draws': 0,
    'lumen_capture_draw': None,
    'pt_first_draw': None,
    'pt_first_sample': None,
    'pt_last_sample': -1,
    'pt_last_progress_write': 0.0,
    'pt_started_monotonic': None,
    'pt_completed_monotonic': None,
    'pt_timeout_logged': False,
    'pt_warmup_start_draw': None,
    'pt_reset_draw': None,
}


def draw():
    STATE['draws'] += 1
    BRIDGE.render_viewport()


def finish_error(message):
    SESSION['status'] = 'error'
    SESSION['error'] = message
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = time.monotonic() - SESSION['started_monotonic']
    SESSION['draws'] = STATE['draws']
    atomic_json(CAPTURES / 'capture-session.json', SESSION)
    (ROOT / 'logs' / 'capture-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_REFERENCE_ERROR ' + message)
    progress('error', error=message, draws=STATE['draws'])
    unreal.unregister_slate_post_tick_callback(HANDLE)
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


def capture_lumen():
    image = CAPTURES / 'lumen-lit.png'
    if image.exists():
        image.unlink()
    started = time.monotonic()
    BRIDGE.capture_viewport(str(image))
    STATE['draws'] += 1
    info = picture_file(image)
    SESSION['images']['lumen_lit'] = info
    SESSION['lumen_capture'] = {
        'mode': 'lit',
        'view_mode': 'VMI_Lit',
        'draw_number': STATE['draws'],
        'warmup_draws': LUMEN_WARMUP_DRAWS,
        'settle_draws': LUMEN_SETTLE_DRAWS,
        'capture_seconds': time.monotonic() - started,
        'commands': LUMEN_COMMANDS,
        'image': info,
    }
    STATE['lumen_capture_draw'] = STATE['draws']
    unreal.log(f'ARDA_REFERENCE_LUMEN_CAPTURED draw={STATE["draws"]} sha256={info["sha256"]}')
    progress('lumen_captured', draws=STATE['draws'], lumen=info)


def start_path_tracer():
    for command in PT_COMMANDS:
        console(command)
    status = BRIDGE.get_path_tracing_status()
    SESSION['path_tracer']['support_status_before'] = json.loads(status)
    SESSION['path_tracer']['commands'] = PT_COMMANDS
    SESSION['path_tracer']['samples_per_pixel_requested'] = TARGET_SPP
    SESSION['path_tracer']['max_bounces_requested'] = TARGET_BOUNCES
    SESSION['path_tracer']['denoiser_requested'] = 0
    SESSION['path_tracer']['filter_width_requested'] = 3.0
    SESSION['path_tracer']['mis_mode_requested'] = 2
    BRIDGE.set_viewport_mode('pathtracing', '')
    STATE['phase'] = 'pt_warmup'
    STATE['pt_warmup_start_draw'] = STATE['draws']
    unreal.log('ARDA_REFERENCE_PT_START ' + status)
    progress('path_tracing_warmup', draws=STATE['draws'], support=json.loads(status))


def capture_path_tracer():
    image = CAPTURES / 'pathtracer-256spp-8bounces.png'
    if image.exists():
        image.unlink()
    samples_before = BRIDGE.get_path_tracing_samples()
    count = BRIDGE.get_path_tracing_sample_count()
    started = time.monotonic()
    BRIDGE.capture_viewport(str(image))
    STATE['draws'] += 1
    samples_after = BRIDGE.get_path_tracing_samples()
    info = picture_file(image)
    SESSION['images']['path_tracer'] = info
    SESSION['path_tracer'].update({
        'mode': 'path tracing',
        'view_mode': 'VMI_PathTracing',
        'sample_index_before_screenshot': samples_before,
        'sample_index_after_screenshot': samples_after,
        'sample_count_reported': count,
        'samples_per_pixel_at_capture': samples_before,
        'sample_index_authority': 'engine FSceneViewStateInterface::GetPathTracingSampleIndex() (bridge GetPathTracingSamples)',
        'draw_requests_since_reset': STATE['draws'] - STATE['pt_reset_draw'],
        'sample_count_is_not_draw_count': True,
        'max_bounces_at_capture': TARGET_BOUNCES,
        'denoiser_at_capture': 0,
        'capture_draw_number': STATE['draws'],
        'first_accumulation_draw': STATE['pt_first_draw'],
        'first_sample_index': STATE['pt_first_sample'],
        'accumulation_seconds': (STATE['pt_completed_monotonic'] or started) - STATE['pt_started_monotonic'],
        'support_status_at_capture': json.loads(BRIDGE.get_path_tracing_status()),
        'image': info,
    })
    SESSION['status'] = 'ok'
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = time.monotonic() - SESSION['started_monotonic']
    SESSION['draws'] = STATE['draws']
    atomic_json(CAPTURES / 'capture-session.json', SESSION)
    unreal.log(f'ARDA_REFERENCE_PT_CAPTURED samples={samples_before} draw={STATE["draws"]} sha256={info["sha256"]}')
    progress('complete', draws=STATE['draws'], samples=samples_before, path_tracer=info)
    unreal.unregister_slate_post_tick_callback(HANDLE)
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        draw()

        if STATE['phase'] == 'lumen_warmup':
            if STATE['draws'] < LUMEN_WARMUP_DRAWS + LUMEN_SETTLE_DRAWS:
                if STATE['draws'] % 20 == 0:
                    progress('lumen_warmup', draws=STATE['draws'])
                return
            capture_lumen()
            start_path_tracer()
            return

        # Let the path tracer compile its RTPSOs and render a few throw-away
        # frames.  The first PT frame in this engine can log a ray-tracing PSO
        # fallback while compilation finishes; those samples must not enter the
        # reference.  Then force two config resets (SPP 255 -> 256) so the
        # counted accumulation starts from sample zero on a ready PSO.
        if STATE['phase'] == 'pt_warmup':
            if STATE['draws'] - STATE['pt_warmup_start_draw'] < PT_PSO_WARMUP_DRAWS:
                return
            SESSION['path_tracer']['pso_warmup_draws'] = PT_PSO_WARMUP_DRAWS
            SESSION['path_tracer']['pso_warmup_start_draw'] = STATE['pt_warmup_start_draw']
            SESSION['path_tracer']['warmup_status'] = json.loads(BRIDGE.get_path_tracing_status())
            console('r.PathTracing.SamplesPerPixel 255')
            STATE['phase'] = 'pt_reset_low'
            return

        if STATE['phase'] == 'pt_reset_low':
            # The draw at the top of this tick applied SPP 255 and reset the
            # path tracer to sample zero.
            console('r.PathTracing.SamplesPerPixel 256')
            STATE['phase'] = 'pt_reset_high'
            return

        if STATE['phase'] == 'pt_reset_high':
            # The draw at the top of this tick applied SPP 256, reset again,
            # and rendered the first counted sample.
            STATE['pt_reset_draw'] = STATE['draws']
            STATE['pt_started_monotonic'] = time.monotonic()
            SESSION['path_tracer']['forced_reset_after_warmup'] = True
            SESSION['path_tracer']['reset_commands'] = [
                'r.PathTracing.SamplesPerPixel 255', 'r.PathTracing.SamplesPerPixel 256']
            SESSION['path_tracer']['reset_draw'] = STATE['draws']
            SESSION['path_tracer']['sample_index_after_reset'] = BRIDGE.get_path_tracing_samples()
            SESSION['path_tracer']['sample_count_after_reset'] = BRIDGE.get_path_tracing_sample_count()
            STATE['phase'] = 'pt_accumulate'
            progress('path_tracing_reset', draws=STATE['draws'],
                     samples=BRIDGE.get_path_tracing_samples(),
                     sample_count=BRIDGE.get_path_tracing_sample_count())
            return

        if STATE['phase'] == 'pt_accumulate':
            status_obj = json.loads(BRIDGE.get_path_tracing_status())
            if not status_obj.get('is_path_tracing_view_mode', False):
                raise RuntimeError('Viewport did not enter PathTracing view mode: ' + json.dumps(status_obj))
            samples = BRIDGE.get_path_tracing_samples()
            count = BRIDGE.get_path_tracing_sample_count()
            if samples > 0 and STATE['pt_first_sample'] is None:
                STATE['pt_first_sample'] = samples
                STATE['pt_first_draw'] = STATE['draws']
                SESSION['path_tracer']['first_nonzero_sample_draw'] = STATE['draws']
                SESSION['path_tracer']['first_nonzero_sample'] = samples
            if samples != STATE['pt_last_sample']:
                STATE['pt_last_sample'] = samples
                now = time.monotonic()
                if now - STATE['pt_last_progress_write'] >= 2.0 or samples >= TARGET_SPP:
                    STATE['pt_last_progress_write'] = now
                    progress('path_tracing', draws=STATE['draws'], samples=samples,
                             sample_count=count, elapsed_seconds=now - STATE['pt_started_monotonic'])
                    unreal.log(f'ARDA_REFERENCE_PT_SAMPLE {samples}/{count} draw={STATE["draws"]}')
            if count > 0 and count != TARGET_SPP:
                raise RuntimeError(f'Path tracer reports sample count {count}, expected {TARGET_SPP}.')
            if samples >= TARGET_SPP:
                if STATE['pt_completed_monotonic'] is None:
                    STATE['pt_completed_monotonic'] = time.monotonic()
                capture_path_tracer()
                return
            elapsed = time.monotonic() - STATE['pt_started_monotonic']
            if elapsed > 300 and samples == 0 and not status_obj.get('ray_tracing_supported', False):
                raise RuntimeError('Path tracing is not supported by this RHI: ' + json.dumps(status_obj))
            if elapsed > PT_TIMEOUT_SECONDS:
                raise RuntimeError(
                    f'Path tracer did not reach {TARGET_SPP} samples in {PT_TIMEOUT_SECONDS}s '
                    f'(samples={samples}, count={count}, draws={STATE["draws"]}).')
            if STATE['draws'] % 16 == 0 and elapsed > 60:
                unreal.log(f'ARDA_REFERENCE_PT_WAIT samples={samples}/{count} draw={STATE["draws"]} elapsed={elapsed:.1f}s')
    except Exception:
        finish_error(traceback.format_exc())
    finally:
        STATE['busy'] = False


try:
    CAMERA, PP, LIGHT, SCENE = setup()
    SESSION['camera'] = SCENE['camera']
    SESSION['exposure'] = SCENE['exposure']
    SESSION['light'] = SCENE['lighting']['rect_light']
    SESSION['scene_verification'] = SCENE['verification_vs_original_scene_inventory']
    progress('lumen_warmup', draws=0)
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    unreal.log('ARDA_REFERENCE_SETUP_COMPLETE')
except Exception:
    message = traceback.format_exc()
    (ROOT / 'logs' / 'capture-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_REFERENCE_ERROR ' + message)
    progress('error', error=message)
