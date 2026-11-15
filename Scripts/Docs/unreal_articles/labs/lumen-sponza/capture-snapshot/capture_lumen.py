"""Capture final Lumen baselines, controlled Lumen pairs and explanatory diagnostics.

One hidden offscreen editor run at 1280x720.  The level is loaded from disk, the
saved state is read BEFORE apply_final_setup touches it (persistence proof), then
the authoritative output/final-setup.json is applied without saving.  Camera,
sun, SkyLight and EV100 stay fixed for every Lumen image.

Outputs:
  output/final/run-lumen.json
  output/final/images/*.png
  output/final/diagnostics/*.png
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
                               pixel_metrics, set_exposure, sha256_file,
                               MAP_PATH, MAP_FILE, OUTPUT, LOGS)

FINAL = OUTPUT / 'final'
IMGDIR = FINAL / 'images'
DIAGDIR = FINAL / 'diagnostics'
RUN_PATH = FINAL / 'run-lumen.json'
CHECKPOINT = FINAL / 'checkpoint-lumen.json'
SCRIPT_FILE = SCRIPTS / 'capture_lumen.py'

WARM_DRAWS = 120
BASELINE_SETTLE = 45
PAIR_SETTLE = 55
RESTORE_SETTLE = 45
DIAG_SETTLE = 22
FINAL_SETTLE = 20

CVAR_NAMES = [
    'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod',
    'r.Lumen.HardwareRayTracing', 'r.LumenScene.FarField',
    'r.Lumen.DiffuseIndirect.Allow',
    'r.Lumen.ScreenProbeGather.ScreenTraces', 'r.Lumen.Reflections.ScreenTraces',
    'r.Lumen.ScreenProbeGather.HardwareRayTracing',
    'r.Lumen.Reflections.HardwareRayTracing',
    'r.Lumen.Reflections.MaxRoughnessToTrace',
    'r.Lumen.Reflections.MaxRoughnessToTraceClamp',
    'r.Lumen.Reflections.RoughnessFadeLength',
    'r.Lumen.HardwareRayTracing.LightingMode',
    'r.Lumen.HardwareRayTracing.HitLighting.Allowed',
    'r.Lumen.Reflections.HardwareRayTracing.HitLighting',
    'r.Lumen.ScreenProbeGather', 'r.LumenScene.SurfaceCache.AtlasSize',
    'r.Lumen.ScreenProbeGather.Debug',
    'r.Lumen.ScreenProbeGather.Debug.ProbePlacement',
    'r.ShaderPrint',
    'r.Lumen.Visualize', 'r.Lumen.Visualize.CardPlacement',
    'r.DistanceFields.Visualize.Monochrome',
    'r.BufferVisualizationTarget',
    'r.ScreenPercentage', 'r.AntiAliasingMethod', 'r.PathTracing.SamplesPerPixel',
]

SESSION = {
    'schema': 'arda-sponza-final-lumen-v1',
    'status': 'running',
    'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'started_monotonic': time.monotonic(),
    'resolution': [1280, 720],
    'script_sha256': sha256_file(SCRIPT_FILE),
    'map_sha256_start': sha256_file(MAP_FILE),
    'images': [], 'diagnostics': [], 'pairs': [], 'notes': [], 'limits': [],
}
STATE = {'draws': 0, 'phase': 0, 'phase_draws': 0, 'busy': False, 'done': False}
SETUP = None
HERO = None
MATERIAL = None
PP = None
PHASES = []


def log(message):
    unreal.log('ARDA_FINAL_LUMEN ' + str(message))


def console(command):
    unreal.log('ARDA_FINAL_LUMEN_COMMAND ' + command)
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


def material_roughness():
    out = {}
    for actor in ACTORS.get_all_level_actors():
        label = actor.get_actor_label()
        if not label.startswith('Sphere_'):
            continue
        try:
            comp = actor.static_mesh_component
            material = comp.get_material(0)
            value = unreal.MaterialEditingLibrary.get_material_instance_scalar_parameter_value(
                material, 'Roughness')
            out[label] = float(value)
        except Exception as exc:
            out[label] = {'error': str(exc)}
    return out


def frame_count():
    try:
        return int(unreal.SystemLibrary.get_frame_count())
    except Exception:
        return None


def observed_state(camera):
    return {
        'camera': pilot_readback(camera),
        'frame_counter': frame_count(),
        'render_status': json.loads(BRIDGE.get_render_status()),
        'exposure': {
            'auto_exposure_min_brightness': float(
                PP.get_editor_property('settings').get_editor_property(
                    'auto_exposure_min_brightness')),
            'auto_exposure_max_brightness': float(
                PP.get_editor_property('settings').get_editor_property(
                    'auto_exposure_max_brightness')),
            'auto_exposure_bias': float(
                PP.get_editor_property('settings').get_editor_property(
                    'auto_exposure_bias')),
        },
        'cvars': cvars(CVAR_NAMES),
        'material_roughness': material_roughness(),
    }


def capture_image(path, name, group, camera, note, record_list):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    before_status = json.loads(BRIDGE.get_path_tracing_status())
    before_state = observed_state(camera)
    BRIDGE.capture_viewport(str(path))
    STATE['draws'] += 1
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 2048:
            break
        time.sleep(0.05)
    after_status = json.loads(BRIDGE.get_path_tracing_status())
    after_state = observed_state(camera)
    info = {
        'name': name, 'group': group, 'camera': camera.get_actor_label(),
        'note': note, 'file': str(path), 'capture_draw': STATE['draws'],
        'path_tracing_before': before_status,
        'path_tracing_after': after_status,
        'observed_before': before_state,
        'observed_after': after_state,
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


CASES = [
    {
        'id': 'final_gather_off',
        'concept': 'FINAL GATHER OFF: r.Lumen.ScreenProbeGather 0 returns black diffuse indirect textures 0/1 and a cleared black probe rough-specular texture 2; DiffuseIndirect.Allow stays 1 and dedicated reflections stay on, so this removes final-gather diffuse plus probe rough specular, not one clean subtraction',
        'commands': ['r.Lumen.ScreenProbeGather 0'],
        'restore': ['r.Lumen.ScreenProbeGather 1'],
        'affected': ['r.Lumen.ScreenProbeGather'],
        'preserved': ['r.Lumen.DiffuseIndirect.Allow'],
    },
    {
        'id': 'screen_traces_off',
        'concept': 'both screen-tracing routes disabled (screen-probe gather and dedicated reflections); world tracing remains',
        'commands': ['r.Lumen.ScreenProbeGather.ScreenTraces 0',
                     'r.Lumen.Reflections.ScreenTraces 0'],
        'restore': ['r.Lumen.ScreenProbeGather.ScreenTraces 1',
                    'r.Lumen.Reflections.ScreenTraces 1'],
        'affected': ['r.Lumen.ScreenProbeGather.ScreenTraces',
                     'r.Lumen.Reflections.ScreenTraces'],
    },
    {
        'id': 'software_world_tracing',
        'concept': 'software world tracing (mesh/global SDF) instead of hardware ray tracing; both GI and reflection trace passes switch',
        'commands': ['r.Lumen.HardwareRayTracing 0'],
        'restore': ['r.Lumen.HardwareRayTracing 1'],
        'affected': ['r.Lumen.HardwareRayTracing',
                     'r.Lumen.ScreenProbeGather.HardwareRayTracing',
                     'r.Lumen.Reflections.HardwareRayTracing'],
    },
    {
        'id': 'reflection_cutoff_low',
        'concept': 'dedicated reflection maximum roughness lowered to 0.10; sphere material roughness unchanged',
        'commands': ['r.Lumen.Reflections.MaxRoughnessToTrace 0.10'],
        'restore': ['r.Lumen.Reflections.MaxRoughnessToTrace 0.4'],
        'affected': ['r.Lumen.Reflections.MaxRoughnessToTrace'],
    },
    {
        'id': 'reflection_cutoff_high',
        'concept': 'dedicated reflection maximum roughness raised to 0.80; sphere material roughness unchanged',
        'commands': ['r.Lumen.Reflections.MaxRoughnessToTrace 0.80'],
        'restore': ['r.Lumen.Reflections.MaxRoughnessToTrace 0.4'],
        'affected': ['r.Lumen.Reflections.MaxRoughnessToTrace'],
    },
    {
        'id': 'reflection_hit_lighting',
        'concept': 'hardware reflection hit-lighting mode (LightingMode 2) instead of surface-cache shading (LightingMode 0), master HW tracing stays on',
        'commands': ['r.Lumen.HardwareRayTracing.LightingMode 2',
                     'r.Lumen.HardwareRayTracing.HitLighting.Allowed 1'],
        'restore': ['r.Lumen.HardwareRayTracing.LightingMode 0',
                    'r.Lumen.HardwareRayTracing.HitLighting.Allowed 1'],
        'affected': ['r.Lumen.HardwareRayTracing.LightingMode',
                     'r.Lumen.HardwareRayTracing.HitLighting.Allowed'],
    },
]


def add_phase(name, group, camera, draws, setup=None, capture=None, subdir='', note=''):
    PHASES.append({'name': name, 'group': group, 'camera': camera, 'draws': draws,
                   'setup': setup, 'capture': capture, 'subdir': subdir, 'note': note})


def diag_setup(mode, buffer_name='', commands=()):
    def setup():
        set_lit_clean()
        if mode == 'buffer':
            BRIDGE.set_viewport_mode('buffer', buffer_name)
            console('r.BufferVisualizationTarget ' + buffer_name)
        elif mode == 'vsm':
            BRIDGE.set_viewport_mode('vsm', buffer_name)
        elif mode == 'lit':
            BRIDGE.set_viewport_mode('lit', '')
        for command in commands:
            console(command)
    return setup


def build_phases():
    add_phase('hero-warm', 'hero', HERO, WARM_DRAWS, setup=set_lit_clean)
    add_phase('lumen-hero-baseline', 'hero', HERO, BASELINE_SETTLE,
              capture=lambda: capture_image(IMGDIR / 'lumen-hero-baseline.png',
                                            'lumen-hero-baseline', 'lumen-baseline',
                                            HERO, 'HW Lumen, hero camera, EV100 11, FarField 0',
                                            SESSION['images']))

    diagnostics = [
        ('diag-hero-basecolor', 'BaseColor', 'buffer', 'BaseColor', (),
         'BaseColor buffer visualization, display-encoded 8-bit, EV100 11'),
        ('diag-hero-worldnormal', 'WorldNormal', 'buffer', 'WorldNormal', (),
         'WorldNormal buffer visualization, display-encoded 8-bit'),
        ('diag-hero-roughness', 'Roughness', 'buffer', 'Roughness', (),
         'Roughness buffer visualization, display-encoded 8-bit'),
        ('diag-hero-metallic', 'Metallic', 'buffer', 'Metallic', (),
         'Metallic buffer visualization, display-encoded 8-bit'),
        ('diag-hero-scenedepth', 'SceneDepth', 'buffer', 'SceneDepth', (),
         'SceneDepth buffer visualization; sky/background is exact depth 0; nonzero geometry values are the editor display mapping, not raw centimetres'),
        ('diag-hero-vsm-mask', 'vsm-mask', 'vsm', 'mask', (),
         'Virtual Shadow Map shadow mask view mode'),
        ('diag-hero-vsm-vpage', 'vsm-vpage', 'vsm', 'vpage', (),
         'Virtual Shadow Map virtual page address view mode'),
        ('diag-hero-vsm-cache', 'vsm-cache', 'vsm', 'cache', (),
         'Virtual Shadow Map cached page view mode (green cached, red uncached)'),
        ('diag-hero-lumen-scene', 'lumen-scene', 'lit', '', ('r.Lumen.Visualize 3',),
         'Lumen Scene visualization mode 3'),
        ('diag-hero-surface-cache', 'surface-cache', 'lit', '', ('r.Lumen.Visualize 5',),
         'Lumen Surface Cache Coverage visualization mode 5'),
        ('diag-hero-cards', 'cards', 'lit', '',
         ('r.Lumen.Visualize 0', 'r.Lumen.Visualize.CardPlacement 1'),
         'Lumen card placement overlay'),
        ('diag-hero-gdf', 'gdf', 'lit', '',
         ('showflag.VisualizeMeshDistanceFields 0',
          'showflag.VisualizeGlobalDistanceField 1',
          'r.AOGlobalDistanceField.ForceFullUpdate 1'),
         'Global Distance Field visualizer; shows shaded field-hit geometry, not distance values or step counts'),
        ('diag-hero-mdf', 'mdf', 'lit', '',
         ('showflag.VisualizeGlobalDistanceField 0',
          'showflag.VisualizeMeshDistanceFields 1'),
         'Mesh Distance Field visualizer; shows shaded field-hit geometry, not distance values or step counts'),
        ('diag-hero-probe-placement-debug', 'probe-placement-debug', 'lit', '',
         ('r.Lumen.ScreenProbeGather.Debug 1',
          'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1'),
         'Lumen screen probe placement debug overlay; requires r.Lumen.ScreenProbeGather.Debug!=0 to dispatch FScreenProbeGatherDebugCS, yellow=uniform probes, magenta=adaptive probes'),
        ('diag-hero-probe-placement-normals', 'probe-placement-normals', 'lit', '',
         ('r.Lumen.ScreenProbeGather.Debug 1',
          'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 3'),
         'Lumen screen probe placement debug overlay mode 3 with probe normals; requires r.Lumen.ScreenProbeGather.Debug!=0'),
        ('diag-hero-trace-directions', 'trace-directions', 'lit', '',
         ('r.Lumen.ScreenProbeGather.VisualizeTraces 1',),
         'Lumen screen probe trace-direction visualization for the centre probe'),
    ]
    for name, short, mode, buffer_name, commands, note in diagnostics:
        add_phase(name, 'diagnostics', HERO, DIAG_SETTLE,
                  setup=diag_setup(mode, buffer_name, commands),
                  capture=(lambda n=name, s=short, nn=note: capture_image(
                      DIAGDIR / (n + '.png'), n, 'diagnostics', HERO, nn,
                      SESSION['diagnostics'])))
    add_phase('hero-diagnostics-restored', 'diagnostics', HERO, FINAL_SETTLE,
              setup=set_lit_clean,
              capture=lambda: capture_image(IMGDIR / 'lumen-hero-diagnostics-restored.png',
                                            'lumen-hero-diagnostics-restored',
                                            'lumen-baseline', HERO,
                                            'baseline restored after diagnostics',
                                            SESSION['images']))

    def pair_setup(case, variant):
        def setup():
            set_lit_clean()
            commands = case['commands'] if variant else case['restore']
            for command in commands:
                console(command)
        return setup

    def pair_capture(camera, prefix, case, variant):
        def capture():
            suffix = '-case-' + case['id'] if variant else '-restore-after-' + case['id']
            note = case['concept'] if variant else 'baseline restore readback after ' + case['id']
            rec = capture_image(IMGDIR / (prefix + suffix + '.png'),
                                prefix + suffix, 'controlled-pair', camera, note,
                                SESSION['images'])
            if variant:
                SESSION['pairs'].append({
                    'camera': camera.get_actor_label(),
                    'baseline_image': prefix + '-baseline',
                    'variant': case['id'],
                    'concept': case['concept'],
                    'affected_cvars': case['affected'],
                    'preserved_cvars': case.get('preserved', []),
                    'requested_commands': case['commands'],
                    'restore_commands': case['restore'],
                    'observed_cvars_after_variant': rec.get('observed_after', {}).get('cvars'),
                    'variant_image': rec,
                    'restored': False,
                })
            else:
                for pair in reversed(SESSION['pairs']):
                    if pair['camera'] == camera.get_actor_label() and pair['variant'] == case['id']:
                        pair['restored'] = True
                        pair['restore_image'] = rec
                        pair['observed_cvars_after_restore'] = rec.get('observed_after', {}).get('cvars')
                        break
        return capture

    for case in CASES:
        add_phase('hero-case-' + case['id'], 'hero-pairs', HERO, PAIR_SETTLE,
                  setup=pair_setup(case, True),
                  capture=pair_capture(HERO, 'hero', case, True))
        add_phase('hero-restore-' + case['id'], 'hero-pairs', HERO, RESTORE_SETTLE,
                  setup=pair_setup(case, False),
                  capture=pair_capture(HERO, 'hero', case, False))
    add_phase('hero-pairs-restored', 'hero-pairs', HERO, FINAL_SETTLE, setup=set_lit_clean,
              capture=lambda: capture_image(IMGDIR / 'lumen-hero-pairs-restored.png',
                                            'lumen-hero-pairs-restored', 'lumen-baseline',
                                            HERO, 'baseline restored after all controlled pairs',
                                            SESSION['images']))

    add_phase('material-warm', 'material', MATERIAL, 90, setup=set_lit_clean)
    add_phase('lumen-material-baseline', 'material', MATERIAL, BASELINE_SETTLE,
              capture=lambda: capture_image(IMGDIR / 'lumen-material-baseline.png',
                                            'lumen-material-baseline', 'lumen-baseline',
                                            MATERIAL, 'HW Lumen, material-detail camera, EV100 11',
                                            SESSION['images']))
    for case in CASES:
        add_phase('material-case-' + case['id'], 'material-pairs', MATERIAL, PAIR_SETTLE,
                  setup=pair_setup(case, True),
                  capture=pair_capture(MATERIAL, 'material', case, True))
        add_phase('material-restore-' + case['id'], 'material-pairs', MATERIAL,
                  RESTORE_SETTLE, setup=pair_setup(case, False),
                  capture=pair_capture(MATERIAL, 'material', case, False))
    add_phase('material-pairs-restored', 'material-pairs', MATERIAL, FINAL_SETTLE,
              setup=set_lit_clean,
              capture=lambda: capture_image(IMGDIR / 'lumen-material-pairs-restored.png',
                                            'lumen-material-pairs-restored',
                                            'lumen-baseline', MATERIAL,
                                            'material baseline restored after all controlled pairs',
                                            SESSION['images']))


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
    atomic_json(CHECKPOINT, {'schema': 'arda-sponza-final-lumen-checkpoint-v1',
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
            if STATE['draws'] % 100 == 0:
                SESSION['draws'] = STATE['draws']
                atomic_json(RUN_PATH, SESSION)
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
        (LOGS / 'final-lumen-error.txt').write_text(message, encoding='utf-8')
        unreal.log_error('ARDA_FINAL_LUMEN_ERROR ' + message)
        finish('error', message)
    finally:
        STATE['busy'] = False


def finish(status='ok', error=None):
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
    SESSION['script_sha256_final'] = sha256_file(SCRIPT_FILE)
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
    PRE_APPLY = None
    readbacks, HERO, MATERIAL = apply_config(SETUP, WORLD)
    PRE_APPLY = readbacks.get('pre_apply_saved_state')
    SESSION['pre_apply_saved_state'] = PRE_APPLY
    SESSION['applied_readbacks'] = readbacks
    PP = {a.get_actor_label(): a for a in ACTORS.get_all_level_actors()}['PostProcess_Sponza']
    set_exposure(PP, 11.0, 0.0)
    pilot(HERO)
    set_lit_clean()
    SESSION['hero_readback'] = pilot_readback(HERO)
    SESSION['material_actor_readback'] = {
        'actor': MATERIAL.get_actor_label(),
        'location_cm': [float(MATERIAL.get_actor_location().x),
                        float(MATERIAL.get_actor_location().y),
                        float(MATERIAL.get_actor_location().z)],
        'horizontal_fov_deg': float(MATERIAL.camera_component.field_of_view),
    }
    SESSION['material_roughness'] = material_roughness()
    IMGDIR.mkdir(parents=True, exist_ok=True)
    DIAGDIR.mkdir(parents=True, exist_ok=True)
    build_phases()
    SESSION['phases'] = [p['name'] for p in PHASES]
    SESSION['render_status_start'] = json.loads(BRIDGE.get_render_status())
    atomic_json(RUN_PATH, SESSION)
    start_phase()
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    HANDLE = unreal.register_slate_post_tick_callback(tick)
    log('capture started phases=%d' % len(PHASES))
except Exception:
    message = traceback.format_exc()
    LOGS.mkdir(parents=True, exist_ok=True)
    (LOGS / 'final-lumen-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_FINAL_LUMEN_ERROR ' + message)
    try:
        finish('error', message)
    except Exception:
        pass
