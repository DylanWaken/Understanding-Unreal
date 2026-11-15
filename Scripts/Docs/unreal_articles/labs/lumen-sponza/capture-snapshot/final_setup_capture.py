"""Save the final low-angle camera/sun/exposure and capture matched previews.

One hidden offscreen editor run.  The final CameraActor is authored and saved in
the copied Sponza level, then piloted on the first active level viewport with
exact camera view enabled.  Lumen and the 64-spp path tracer use the same saved
camera, geometry, lights and pinned EV100; only the renderer changes.
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
from image_stats import Png, compare  # noqa: E402

OUTPUT = WORKSPACE / 'output'
LOGS = OUTPUT / 'logs'
PREVIEW = OUTPUT / 'preview' / 'final-setup'
MAP_PATH = '/Game/Labs/SponzaTeaching'
MAP_FILE = WORKSPACE / 'project' / 'UnrealLab' / 'Content' / 'Labs' / 'SponzaTeaching.umap'

LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
BRIDGE = unreal.ArdaLabCapture
WORLD = None

CAM_LOC = (-980.0, 0.0, 110.0)
CAM_PITCH = 10.0
CAM_YAW = 0.0
CAM_ROLL = 0.0
CAM_HFOV = 60.0
SUN_PITCH = -60.0
SUN_YAW = -45.0
SUN_LUX = 20000.0
SUN_TEMP = 5500.0
SKY_INTENSITY = 18.0
EV100 = 10.0
DIAG_EV100 = 11.0

WARMUP_DRAWS = 170
LUMEN_SETTLE_DRAWS = 50
FOV_SETTLE_DRAWS = 32
DIAG_SETTLE_DRAWS = 24
MAT_SETTLE_DRAWS = 24
PT_PSO_WARMUP_DRAWS = 40
TARGET_SPP = 64
TARGET_BOUNCES = 8

CLEAN_COMMANDS = [
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
    'r.PathTracing.EnableReferenceAtmosphere 0',
]

PT_COMMANDS = [
    'r.PathTracing.SamplesPerPixel 64',
    'r.PathTracing.MaxBounces 8',
    'r.PathTracing.Denoiser 0',
    'r.PathTracing.ProgressDisplay 0',
    'r.PathTracing.FilterWidth 3.0',
    'r.PathTracing.MISMode 2',
    'r.PathTracing.EnableEmissive 1',
    'r.PathTracing.EnableReferenceDOF 0',
    'r.PathTracing.EnableReferenceAtmosphere 0',
    'r.PathTracing.VisibleLights 0',
]

CVAR_NAMES = [
    'r.Lumen.HardwareRayTracing', 'r.LumenScene.FarField',
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.PathTracing.SamplesPerPixel', 'r.PathTracing.MaxBounces',
    'r.PathTracing.Denoiser', 'r.PathTracing.EnableReferenceAtmosphere',
    'r.DistanceFields.Visualize.Monochrome', 'r.SkyLight.RealTimeReflectionCapture',
]

# Explicit ROIs in the 1280x720 capture.  The bright roof/sky opening is at the
# top centre; the geometry ROI is the readable nave below it.
SKY_ROI = (470, 0, 760, 100)
GEOM_ROI = (220, 170, 1080, 700)

SESSION = {
    'schema': 'arda-sponza-final-setup-v1',
    'status': 'running',
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'map': MAP_PATH,
    'map_sha256_before': hashlib.sha256(MAP_FILE.read_bytes()).hexdigest(),
    'hero_camera': {
        'requested': {'location_cm': list(CAM_LOC), 'pitch_deg': CAM_PITCH,
                      'yaw_deg': CAM_YAW, 'roll_deg': CAM_ROLL,
                      'horizontal_fov_deg': CAM_HFOV},
        'lens_convention': ('UE CameraActor horizontal FOV; 16:9 viewport; '
                            '35mm-equivalent focal length from 36 mm sensor width'),
        'vertical_fov_deg_at_16_9': round(math.degrees(2 * math.atan(
            math.tan(math.radians(CAM_HFOV / 2)) * 9 / 16)), 4),
        'equivalent_35mm_focal_length_mm': round(
            36.0 / (2 * math.tan(math.radians(CAM_HFOV / 2))), 3),
        'filmback': 'not physically configured (CameraActor, not CineCamera)',
        'dof': 'off',
    },
    'light': {
        'sun': {'pitch_deg': SUN_PITCH, 'yaw_deg': SUN_YAW,
                'intensity_lux': SUN_LUX, 'temperature_k': SUN_TEMP,
                'atmosphere_sun_light': True, 'light_source_angle_deg': 0.5357},
        'sky_light_intensity': SKY_INTENSITY,
        'exposure_ev100': EV100,
        'note': ('same saved sun/SkyLight/SkyAtmosphere/exposure used by Lumen and '
                 'the path tracer; no per-image normalization'),
    },
    'path_tracer': {'requested_spp': TARGET_SPP, 'max_bounces': TARGET_BOUNCES,
                    'denoiser': 0, 'reference_atmosphere': 0,
                    'commands': PT_COMMANDS},
    'images': {}, 'fov_proof': {'steps': []}, 'roi_metrics': {},
    'notes': [], 'limits': [],
}
STATE = {'draws': 0, 'step': 0, 'step_draws': 0, 'busy': False, 'done': False,
         'pt_status': {}, 'pt_reset_draw': None, 'pt_first_sample': None,
         'pt_last_sample': -1, 'pt_samples_at_capture': None}
CAM = None
CAM_MAT = None
KEY = None
SUN = None
SUN_COMP = None
SKY_COMP = None
PP = None


def log(message):
    unreal.log('ARDA_FINAL_SETUP ' + str(message))


def atomic_json(path, data):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def console(command):
    unreal.log('ARDA_FINAL_SETUP_COMMAND ' + command)
    return unreal.SystemLibrary.execute_console_command(WORLD, command)


def cvar(name):
    try:
        return json.loads(BRIDGE.get_console_variable(name))
    except Exception:
        return {'name': name, 'found': False}


def cvars():
    return {name: cvar(name) for name in CVAR_NAMES}


def vec(v):
    return [round(float(v.x), 3), round(float(v.y), 3), round(float(v.z), 3)]


def rot(v):
    return {'pitch': round(float(v.pitch), 4), 'yaw': round(float(v.yaw), 4),
            'roll': round(float(v.roll), 4)}


def png_metrics(path):
    png = Png(path)
    step = 2
    total = black = white = 0
    lum_sum = lum_sq = 0.0
    colors = {}
    for y in range(0, png.height, step):
        row = png.rows[y]
        for x in range(0, png.width, step):
            i = x * png.channels
            r, g, b = row[i], row[i + 1], row[i + 2]
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            total += 1
            lum_sum += l
            lum_sq += l * l
            if l <= 2:
                black += 1
            if l >= 253:
                white += 1
            colors[(r >> 5, g >> 5, b >> 5)] = 1
    mean = lum_sum / total
    return {'width': png.width, 'height': png.height,
            'mean_luminance': round(mean, 2),
            'std_luminance': round(max(lum_sq / total - mean * mean, 0.0) ** 0.5, 2),
            'near_black_fraction': round(black / total, 4),
            'near_white_fraction': round(white / total, 4),
            'unique_rgb_colors_3bit': len(colors)}


def roi_metrics(path, roi):
    x0, y0, x1, y1 = roi
    png = Png(path)
    n = black = white = 0
    lum_sum = lum_sq = 0.0
    rs = gs = bs = 0
    colors = set()
    for y in range(y0, y1):
        row = png.rows[y]
        for x in range(x0, x1):
            i = x * png.channels
            r, g, b = row[i], row[i + 1], row[i + 2]
            l = 0.2126 * r + 0.7152 * g + 0.0722 * b
            n += 1
            lum_sum += l
            lum_sq += l * l
            rs += r
            gs += g
            bs += b
            if l <= 2:
                black += 1
            if l >= 253:
                white += 1
            colors.add((r >> 4, g >> 4, b >> 4))
    mean = lum_sum / n
    return {'roi_px': [x0, y0, x1, y1], 'pixels': n,
            'mean_luminance': round(mean, 2),
            'std_luminance': round(max(lum_sq / n - mean * mean, 0.0) ** 0.5, 2),
            'mean_rgb': [round(rs / n, 1), round(gs / n, 1), round(bs / n, 1)],
            'near_black_fraction': round(black / n, 4),
            'near_white_fraction': round(white / n, 4),
            'unique_rgb_colors_4bit': len(colors)}


def set_exposure(ev):
    settings = PP.get_editor_property('settings')
    settings.set_editor_property('auto_exposure_min_brightness', float(ev))
    settings.set_editor_property('auto_exposure_max_brightness', float(ev))
    settings.set_editor_property('auto_exposure_bias', 0.0)
    settings.set_editor_property('override_auto_exposure_min_brightness', True)
    settings.set_editor_property('override_auto_exposure_max_brightness', True)
    settings.set_editor_property('override_auto_exposure_bias', True)
    PP.set_editor_property('settings', settings)


def pilot(camera):
    ACTORS.clear_actor_selection_set()
    LEVEL.editor_set_game_view(True, KEY)
    LEVEL.set_allows_cinematic_control(True, KEY)
    LEVEL.set_exact_camera_view(True, KEY)
    LEVEL.pilot_level_actor(camera, KEY)


def view_fov():
    try:
        return float(LEVEL.get_level_viewport_fov(KEY))
    except Exception as exc:
        return {'error': str(exc)}


def pilot_readback(camera=None):
    camera = camera or CAM
    rec = {'requested_config_key': str(KEY),
           'active_config_key': str(LEVEL.get_active_viewport_config_key()),
           'viewport_config_keys': [str(k) for k in LEVEL.get_viewport_config_keys()]}
    try:
        actor = LEVEL.get_pilot_level_actor(KEY)
        rec['pilot_actor'] = actor.get_actor_label() if actor else None
        rec['pilot_matches'] = bool(actor == camera)
    except Exception as exc:
        rec['pilot_error'] = str(exc)
    try:
        rec['exact_camera_view'] = bool(LEVEL.get_exact_camera_view(KEY))
        rec['allows_cinematic_control'] = bool(LEVEL.get_allows_cinematic_control(KEY))
    except Exception as exc:
        rec['lock_readback_error'] = str(exc)
    rec['actual_view_fov'] = view_fov()
    try:
        loc, r = unreal.EditorLevelLibrary.get_level_viewport_camera_info()
        rec['viewport_camera'] = {'location_cm': vec(loc), 'rotation': rot(r)}
    except Exception as exc:
        rec['viewport_camera_error'] = str(exc)
    rec['render_status'] = json.loads(BRIDGE.get_render_status())
    return rec


def capture(name, note, subdir=''):
    path = PREVIEW / subdir / (name + '.png')
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
    else:
        raise RuntimeError('screenshot not written: ' + str(path))
    info = {'file': str(path), 'bytes': path.stat().st_size,
            'sha256': sha256_file(path), 'note': note, 'draw': STATE['draws']}
    info.update(png_metrics(path))
    SESSION['images'][name] = info
    log('captured %s mean=%.2f white=%.4f' % (name, info['mean_luminance'],
                                              info['near_white_fraction']))
    atomic_json(OUTPUT / 'final-setup.json', SESSION)
    return info


def steps():
    return [
        ('warmup', WARMUP_DRAWS, None),
        ('lumen', LUMEN_SETTLE_DRAWS, None),
        ('fov-setup', 1, fov_setup),
        ('fov-60', FOV_SETTLE_DRAWS, None),
        ('fov-70', FOV_SETTLE_DRAWS, lambda: (CAM.camera_component.set_field_of_view(70.0),
                                              pilot(CAM))),
        ('fov-60-restored', FOV_SETTLE_DRAWS, lambda: (
            CAM.camera_component.set_field_of_view(CAM_HFOV), pilot(CAM))),
        ('pt-setup', 1, pt_setup),
        ('pt-warmup', PT_PSO_WARMUP_DRAWS, None),
        ('pt-reset-low', 1, lambda: console('r.PathTracing.SamplesPerPixel 63')),
        ('pt-reset-high', 1, lambda: console('r.PathTracing.SamplesPerPixel 64')),
        ('pt-accumulate', 600, None),
        ('diag-gdf', DIAG_SETTLE_DRAWS, gdf_setup),
        ('diag-mdf', DIAG_SETTLE_DRAWS, mdf_setup),
        ('material', MAT_SETTLE_DRAWS, material_setup),
        ('material-ev11', MAT_SETTLE_DRAWS, lambda: set_exposure(DIAG_EV100)),
        ('restore', 6, restore_setup),
        ('finish', 2, None),
    ]


STEPS = []


def fov_setup():
    CAM.camera_component.set_field_of_view(CAM_HFOV)
    pilot(CAM)
    SESSION['fov_proof'] = {
        'fixed_transform': {'location_cm': list(CAM_LOC), 'pitch_deg': CAM_PITCH,
                            'yaw_deg': CAM_YAW, 'roll_deg': CAM_ROLL},
        'note': ('same saved CameraActor transform; only its horizontal FOV changes; '
                 'ViewFOV is read back from the first active viewport after settle'),
        'steps': []}


def pt_setup():
    for command in PT_COMMANDS:
        console(command)
    BRIDGE.set_viewport_mode('pathtracing', '')
    for command in CLEAN_COMMANDS:
        console(command)
    STATE['pt_status'] = json.loads(BRIDGE.get_path_tracing_status())
    SESSION['path_tracer']['support_status_before'] = STATE['pt_status']
    SESSION['path_tracer']['enable_reference_atmosphere_cvar'] = cvar(
        'r.PathTracing.EnableReferenceAtmosphere')
    SESSION['path_tracer']['camera'] = pilot_readback()


def gdf_setup():
    BRIDGE.set_viewport_mode('lit', '')
    for command in CLEAN_COMMANDS:
        console(command)
    set_exposure(DIAG_EV100)
    console('r.DistanceFields.Visualize.Monochrome 1')
    console('showflag.VisualizeMeshDistanceFields 0')
    console('showflag.VisualizeGlobalDistanceField 1')
    console('r.AOGlobalDistanceField.ForceFullUpdate 1')
    SESSION['diagnostics_display'] = {
        'gdf': {'exposure_ev100': DIAG_EV100, 'monochrome': 1,
                'showflag': 'VisualizeGlobalDistanceField 1'},
    }


def mdf_setup():
    console('showflag.VisualizeGlobalDistanceField 0')
    console('showflag.VisualizeMeshDistanceFields 1')
    SESSION['diagnostics_display']['mdf'] = {
        'exposure_ev100': DIAG_EV100, 'monochrome': 1,
        'showflag': 'VisualizeMeshDistanceFields 1'}


def material_setup():
    console('showflag.VisualizeMeshDistanceFields 0')
    console('showflag.VisualizeGlobalDistanceField 0')
    BRIDGE.set_viewport_mode('lit', '')
    for command in CLEAN_COMMANDS:
        console(command)
    set_exposure(EV100)
    CAM_MAT.camera_component.set_field_of_view(70.0059814453125)
    pilot(CAM_MAT)
    SESSION['material_camera_readback'] = pilot_readback(CAM_MAT)


def restore_setup():
    set_exposure(EV100)
    CAM.camera_component.set_field_of_view(CAM_HFOV)
    pilot(CAM)
    for command in CLEAN_COMMANDS:
        console(command)
    SESSION['restore_readback'] = pilot_readback(CAM)


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
        log('setup error %s: %s' % (name, exc))
    log('step %d %s draws=%d' % (STATE['step'], name, draws))


def capture_fov_step(name, hfov):
    rec = {'requested_hfov_deg': hfov, 'viewport': pilot_readback(CAM)}
    rec['image'] = capture(name, 'fixed transform, CameraActor HFOV %.1f' % hfov)
    SESSION['fov_proof']['steps'].append(rec)
    log('FOV step %s viewFOV=%r actorFOV=%.3f' % (
        name, rec['viewport']['actual_view_fov'],
        float(CAM.camera_component.field_of_view)))


def capture_pt():
    STATE['pt_samples_at_capture'] = BRIDGE.get_path_tracing_samples()
    count = BRIDGE.get_path_tracing_sample_count()
    SESSION['path_tracer'].update({
        'actual_observed_spp_at_capture': STATE['pt_samples_at_capture'],
        'sample_count_reported': count,
        'max_bounces_at_capture': TARGET_BOUNCES,
        'denoiser_at_capture': 0,
        'reference_atmosphere_at_capture': 0,
        'support_status_at_capture': json.loads(BRIDGE.get_path_tracing_status()),
        'sample_index_authority': 'FSceneViewStateInterface::GetPathTracingSampleIndex (bridge)',
    })
    SESSION['path_tracer']['image'] = capture(
        'lumen-vs-pt--pathtracer-64spp',
        'path tracer, %d actual observed spp, %d bounces, denoiser off, same camera/lights/EV100' %
        (STATE['pt_samples_at_capture'], TARGET_BOUNCES))
    STATE['pt_samples_after_capture'] = BRIDGE.get_path_tracing_samples()
    SESSION['path_tracer']['actual_observed_spp_after_screenshot'] = (
        STATE['pt_samples_after_capture'])
    SESSION['path_tracer']['sample_count_after_screenshot'] = (
        BRIDGE.get_path_tracing_sample_count())


def capture_pt_second():
    before = BRIDGE.get_path_tracing_samples()
    SESSION['path_tracer']['second_capture_samples_before'] = before
    SESSION['path_tracer']['second_capture_image'] = capture(
        'lumen-vs-pt--pathtracer-64spp-after-10-draws',
        'second PT screenshot after 10 further Slate draws; accumulation-stability check')
    SESSION['path_tracer']['second_capture_samples_after'] = (
        BRIDGE.get_path_tracing_samples())
    SESSION['path_tracer']['second_capture_sample_count'] = (
        BRIDGE.get_path_tracing_sample_count())


def tick(delta):
    if STATE['busy'] or STATE['done']:
        return
    STATE['busy'] = True
    try:
        BRIDGE.render_viewport()
        STATE['draws'] += 1
        STATE['step_draws'] += 1
        name, draws, setup = STEPS[STATE['step']]
        if name == 'lumen' and STATE['step_draws'] >= draws:
            SESSION['lumen'] = {
                'exposure_ev100': EV100,
                'viewport': pilot_readback(CAM),
                'image': capture('final-lumen-hw',
                                 'HW Lumen, saved hero camera, EV100 %.1f, FarField 0' % EV100)}
        elif name == 'fov-60' and STATE['step_draws'] >= draws:
            capture_fov_step('fov-60', CAM_HFOV)
        elif name == 'fov-70' and STATE['step_draws'] >= draws:
            capture_fov_step('fov-70', 70.0)
        elif name == 'fov-60-restored' and STATE['step_draws'] >= draws:
            capture_fov_step('fov-60-restored', CAM_HFOV)
        elif name == 'pt-warmup' and STATE['step_draws'] >= draws:
            SESSION['path_tracer']['pso_warmup_draws'] = PT_PSO_WARMUP_DRAWS
            SESSION['path_tracer']['warmup_status'] = json.loads(
                BRIDGE.get_path_tracing_status())
            STATE['pt_reset_draw'] = STATE['draws']
        elif name == 'pt-accumulate':
            samples = BRIDGE.get_path_tracing_samples()
            count = BRIDGE.get_path_tracing_sample_count()
            if samples > 0 and STATE['pt_first_sample'] is None:
                STATE['pt_first_sample'] = samples
            if samples != STATE['pt_last_sample']:
                STATE['pt_last_sample'] = samples
                if STATE['draws'] % 16 == 0:
                    log('PT samples %d/%d' % (samples, count))
            if count != TARGET_SPP:
                raise RuntimeError('PT sample count %d != %d' % (count, TARGET_SPP))
            if samples >= TARGET_SPP:
                if not STATE.get('pt_capture_done'):
                    capture_pt()
                    STATE['pt_capture_done'] = True
                    STATE['pt_second_at_draw'] = STATE['draws'] + 10
                    return
                if STATE.get('pt_second_at_draw') and STATE['draws'] >= STATE['pt_second_at_draw']:
                    capture_pt_second()
                    STATE['step'] += 1
                    start_step()
                    return
                return
            if STATE['draws'] - (STATE['pt_reset_draw'] or 0) > 580:
                raise RuntimeError('path tracer did not reach %d samples' % TARGET_SPP)
            return
        elif name == 'diag-gdf' and STATE['step_draws'] >= draws:
            SESSION['diagnostics'] = {
                'gdf': {'image': capture('diagnostic-gdf-monochrome-ev11',
                                         'Global Distance Field, hero camera, monochrome, EV100 %.1f' % DIAG_EV100)},
                'display_note': ('distance-field visualizers show simple shaded hit geometry, '
                                 'not raw distance values or step counts'),
            }
        elif name == 'diag-mdf' and STATE['step_draws'] >= draws:
            SESSION['diagnostics']['mdf'] = {
                'image': capture('diagnostic-mdf-monochrome-ev11',
                                 'Mesh Distance Fields, hero camera, monochrome, EV100 %.1f' % DIAG_EV100)}
        elif name == 'material' and STATE['step_draws'] >= draws:
            SESSION['material'] = {
                'camera': pilot_readback(CAM_MAT),
                'exposure_ev100': EV100,
                'image': capture('material-chart-ev10', 'CAM_Materials at final EV100 10.0')}
        elif name == 'material-ev11' and STATE['step_draws'] >= draws:
            SESSION['material']['image_ev11'] = capture(
                'material-chart-ev11', 'CAM_Materials, diagnostic EV100 11.0')
        if STATE['step_draws'] < draws:
            return
        STATE['step'] += 1
        if STATE['step'] >= len(STEPS):
            finish('ok')
            return
        start_step()
    except Exception:
        message = traceback.format_exc()
        (LOGS / 'final-setup-error.txt').write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_FINAL_SETUP_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


def fov_comparison():
    steps = SESSION['fov_proof']['steps']
    if len(steps) != 3:
        return
    a, b, c = (Png(s['image']['file']) for s in steps)
    SESSION['fov_proof']['comparison_60_vs_70'] = compare(a, b)
    SESSION['fov_proof']['comparison_60_vs_restored'] = compare(a, c)
    SESSION['fov_proof']['fov_changes_image'] = (
        SESSION['fov_proof']['comparison_60_vs_70'][
            'fraction_pixels_rgb_difference_gt_8'] > 0.05)
    SESSION['fov_proof']['restore_mean_abs_diff'] = (
        SESSION['fov_proof']['comparison_60_vs_restored'][
            'mean_absolute_rgb_difference_0_255'])
    SESSION['fov_proof']['restore_is_stable'] = (
        SESSION['fov_proof']['restore_mean_abs_diff'] < 2.0)


def finish(status='ok', error=None):
    SESSION['status'] = status
    if error:
        SESSION['error'] = error
    try:
        fov_comparison()
    except Exception:
        SESSION['fov_proof']['comparison_error'] = traceback.format_exc()
    try:
        hero = SESSION['images']['final-lumen-hw']['file']
        SESSION['roi_metrics'] = {
            'sky_or_roof_opening': roi_metrics(hero, SKY_ROI),
            'geometry': roi_metrics(hero, GEOM_ROI),
            'image': hero,
            'roi_note': ('fixed rectangles in the 1280x720 frame; the sky/roof-opening '
                         'ROI is the top-centre bright aperture, the geometry ROI is the '
                         'readable nave below it'),
        }
        SESSION['quality_checks'] = {
            'sky_has_tonal_variation': (
                SESSION['roi_metrics']['sky_or_roof_opening']['std_luminance'] >= 3.0 and
                SESSION['roi_metrics']['sky_or_roof_opening']['unique_rgb_colors_4bit'] >= 8),
            'sky_not_clipped': (
                SESSION['roi_metrics']['sky_or_roof_opening']['near_white_fraction'] < 0.10),
            'geometry_readable': (
                15.0 <= SESSION['roi_metrics']['geometry']['mean_luminance'] <= 110.0 and
                SESSION['roi_metrics']['geometry']['near_black_fraction'] < 0.30),
        }
    except Exception:
        SESSION['roi_metrics'] = {'error': traceback.format_exc()}
    SESSION['finished_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['elapsed_seconds'] = time.monotonic() - SESSION['started_monotonic']
    SESSION['draws'] = STATE['draws']
    SESSION['map_sha256_after'] = hashlib.sha256(MAP_FILE.read_bytes()).hexdigest()
    SESSION['map_sha256_saved'] = SESSION.get('map_sha256_saved')
    SESSION['map_unchanged_since_save'] = (
        SESSION['map_sha256_saved'] == SESSION['map_sha256_after']
        if SESSION.get('map_sha256_saved') else None)
    SESSION['hero_camera']['readback'] = pilot_readback(CAM)
    SESSION['cvars_at_end'] = cvars()
    SESSION['render_status_at_end'] = json.loads(BRIDGE.get_render_status())
    atomic_json(OUTPUT / 'final-setup.json', SESSION)
    log('final setup ' + status)
    try:
        unreal.unregister_slate_post_tick_callback(HANDLE)
    except Exception:
        pass
    unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    STATE['done'] = True


HANDLE = None
try:
    PREVIEW.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    if not LEVEL.load_level(MAP_PATH):
        raise RuntimeError('failed to load ' + MAP_PATH)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    if WORLD is None:
        raise RuntimeError('no editor world after load')
    KEY = LEVEL.get_active_viewport_config_key()
    actors = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}
    PP = actors['PostProcess_Sponza']
    SUN = actors['Sun_Sponza']
    SUN_COMP = SUN.get_component_by_class(unreal.DirectionalLightComponent)
    SKY_COMP = actors['SkyLight_Sponza'].get_component_by_class(unreal.SkyLightComponent)
    CAM_MAT = actors['CAM_Materials']
    SESSION['actors_found'] = sorted(actors)
    SESSION['sun_before'] = {'rotation': rot(SUN.get_actor_rotation()),
                             'intensity_lux': float(SUN_COMP.intensity)}
    SESSION['sky_light_before'] = float(SKY_COMP.intensity)

    CAM = ACTORS.spawn_actor_from_class(
        unreal.CameraActor, unreal.Vector(*CAM_LOC),
        unreal.Rotator(pitch=CAM_PITCH, yaw=CAM_YAW, roll=CAM_ROLL))
    CAM.set_actor_label('CAM_Hero_Reference')
    CAM.camera_component.set_field_of_view(CAM_HFOV)
    try:
        CAM.camera_component.set_editor_property('constrain_aspect_ratio', False)
    except Exception as exc:
        SESSION['notes'].append('aspect note: ' + str(exc))

    SUN.set_actor_rotation(unreal.Rotator(pitch=SUN_PITCH, yaw=SUN_YAW, roll=0.0), False)
    SUN_COMP.set_intensity(SUN_LUX)
    SUN_COMP.set_editor_property('temperature', SUN_TEMP)
    SUN_COMP.set_editor_property('atmosphere_sun_light', True)
    SKY_COMP.set_intensity(SKY_INTENSITY)
    set_exposure(EV100)

    for command in CLEAN_COMMANDS:
        console(command)
    pilot(CAM)
    SESSION['camera_initial'] = pilot_readback(CAM)
    SESSION['game_view'] = bool(LEVEL.editor_get_game_view(KEY))

    # Save the final camera/light/exposure into the copied level before captures.
    if not unreal.EditorLoadingAndSavingUtils.save_map(WORLD, MAP_PATH):
        raise RuntimeError('save_map failed')
    SESSION['map_sha256_saved'] = hashlib.sha256(MAP_FILE.read_bytes()).hexdigest()
    SESSION['saved_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    SESSION['saved_camera'] = pilot_readback(CAM)
    SESSION['saved_sun'] = {
        'rotation_degrees': rot(SUN.get_actor_rotation()),
        'intensity_lux': float(SUN_COMP.intensity),
        'temperature_k': float(SUN_COMP.get_editor_property('temperature'))}
    SESSION['saved_sky_light_intensity'] = float(SKY_COMP.intensity)
    SESSION['saved_exposure'] = {
        'auto_exposure_min_brightness':
            PP.get_editor_property('settings').get_editor_property(
                'auto_exposure_min_brightness'),
        'auto_exposure_max_brightness':
            PP.get_editor_property('settings').get_editor_property(
                'auto_exposure_max_brightness'),
        'auto_exposure_bias':
            PP.get_editor_property('settings').get_editor_property(
                'auto_exposure_bias')}
    SESSION['cvars_at_start'] = cvars()
    SESSION['render_status_at_start'] = json.loads(BRIDGE.get_render_status())

    STEPS = steps()
    SESSION['steps'] = [s[0] for s in STEPS]
    atomic_json(OUTPUT / 'final-setup.json', SESSION)
    start_step()
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('setup complete steps=' + ','.join(SESSION['steps']))
except Exception:
    message = traceback.format_exc()
    (LOGS / 'final-setup-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_FINAL_SETUP_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass
