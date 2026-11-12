"""Run inside UnrealEditor with -ExecutePythonScript=<this file>.

Builds independent pieces of a Cornell-style room in centimeters, then captures
real engine view modes after rendering warm-up. No screenshot is synthesized.
"""
import unreal
import os
import math
import json
import time
from pathlib import Path

ROOT = Path(os.environ.get('ARDA_UNREAL_LAB', str(Path.home() / 'AppData/Local/Temp/arda-unreal-lab')))
OUT = ROOT / 'captures'
OUT.mkdir(parents=True, exist_ok=True)
LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
unreal.EditorLoadingAndSavingUtils.new_blank_map(False)
WORLD = unreal.EditorLevelLibrary.get_editor_world()
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
CUBE = unreal.load_asset('/Engine/BasicShapes/Cube.Cube')

def console(command):
    unreal.log('ARDA_COMMAND ' + command)
    if command.startswith('viewmode '):
        mode = command.split()[1]
        unreal.ArdaLabCapture.set_viewport_mode('buffer' if mode == 'buffervisualization' else mode,'')
    if command.startswith('r.BufferVisualizationTarget '):
        unreal.ArdaLabCapture.set_viewport_mode('buffer',command.split()[1])
    unreal.SystemLibrary.execute_console_command(WORLD, command)

def material(name, rgb, roughness=.7, metallic=0):
    path = '/Game/Labs/Materials/' + name
    mat = unreal.load_asset(path)
    if mat:
        return mat
    mat = TOOLS.create_asset(name, '/Game/Labs/Materials', unreal.Material, unreal.MaterialFactoryNew())
    color = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant3Vector)
    color.set_editor_property('constant', unreal.LinearColor(*rgb, 1))
    unreal.MaterialEditingLibrary.connect_material_property(color, '', unreal.MaterialProperty.MP_BASE_COLOR)
    for value, prop in [(roughness, unreal.MaterialProperty.MP_ROUGHNESS), (metallic, unreal.MaterialProperty.MP_METALLIC)]:
        expr = unreal.MaterialEditingLibrary.create_material_expression(mat, unreal.MaterialExpressionConstant)
        expr.set_editor_property('r', value)
        unreal.MaterialEditingLibrary.connect_material_property(expr, '', prop)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat

def box(name, lo, hi, mat):
    center = [(a+b)*50 for a,b in zip(lo,hi)]
    # UE looks along +Y with screen-right toward -X. Reflect CPU X to keep
    # the Cornell room's red wall on the left in both renderer outputs.
    center[0] *= -1
    actor = ACTORS.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(*center))
    actor.set_actor_label(name)
    comp = actor.static_mesh_component
    comp.set_static_mesh(CUBE)
    comp.set_material(0, mat)
    actor.set_actor_scale3d(unreal.Vector(*[b-a for a,b in zip(lo,hi)]))
    return actor

neutral = material('Neutral', (.72,.72,.68))
red = material('Red', (.65,.08,.06))
green = material('Green', (.08,.5,.12))
matte = material('Matte', (.72,.65,.5))
glossy = material('Glossy', (.62,.65,.7), .12, .85)
box('Floor', (-2,0,-.1), (2,5,0), neutral)
box('Ceiling', (-2,0,3), (2,5,3.1), neutral)
box('Left red wall', (-2.1,0,0), (-2,5,3), red)
box('Right green wall', (2,0,0), (2.1,5,3), green)
box('Back wall', (-2,5,0), (2,5.1,3), neutral)
box('Matte block', (-1.25,2.3,0), (-.3,3.4,1.05), matte)
box('Glossy block', (.3,3.3,0), (1.2,4.3,1.8), glossy)
light = ACTORS.spawn_actor_from_class(unreal.RectLight, unreal.Vector(0,270,298), unreal.Rotator(pitch=-90,yaw=0,roll=0))
light.set_actor_label('Ceiling area light')
lc = light.get_component_by_class(unreal.RectLightComponent)
lc.set_editor_property('intensity_units', unreal.LightUnits.LUMENS)
lc.set_intensity(2400)
lc.set_source_width(130)
lc.set_source_height(120)
lc.set_attenuation_radius(2000)
lc.set_mobility(unreal.ComponentMobility.MOVABLE)
pp = ACTORS.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0,0,0))
pp.set_editor_property('unbound', True)
settings = pp.get_editor_property('settings')
for k,v in {'override_auto_exposure_min_brightness':True,'override_auto_exposure_max_brightness':True,'auto_exposure_min_brightness':4.0,'auto_exposure_max_brightness':4.0,'override_auto_exposure_bias':True,'auto_exposure_bias':0.0}.items():
    settings.set_editor_property(k,v)
pp.set_editor_property('settings',settings)
depth_mat=unreal.load_asset('/Game/Labs/Materials/Depth10Meters')
if not depth_mat:
    depth_mat=TOOLS.create_asset('Depth10Meters','/Game/Labs/Materials',unreal.Material,unreal.MaterialFactoryNew())
    depth_mat.set_editor_property('material_domain',unreal.MaterialDomain.MD_POST_PROCESS)
    depth_mat.set_editor_property('blendable_location',unreal.BlendableLocation.BL_SCENE_COLOR_AFTER_TONEMAPPING)
    depth=unreal.MaterialEditingLibrary.create_material_expression(depth_mat,unreal.MaterialExpressionSceneDepth)
    divide=unreal.MaterialEditingLibrary.create_material_expression(depth_mat,unreal.MaterialExpressionDivide)
    divide.set_editor_property('const_b',1000.0)
    assert unreal.MaterialEditingLibrary.connect_material_expressions(depth,'',divide,'A')
    assert unreal.MaterialEditingLibrary.connect_material_property(divide,'',unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    unreal.MaterialEditingLibrary.recompile_material(depth_mat)
    unreal.EditorAssetLibrary.save_loaded_asset(depth_mat)
camera = ACTORS.spawn_actor_from_class(unreal.CameraActor, unreal.Vector(0,-400,165))
rot = unreal.MathLibrary.find_look_at_rotation(unreal.Vector(0,-400,165), unreal.Vector(0,280,140))
camera.set_actor_rotation(rot, False)
camera.camera_component.set_field_of_view(math.degrees(2*math.atan(math.tan(math.radians(43/2))*16/9)))
unreal.EditorLevelLibrary.set_level_viewport_camera_info(camera.get_actor_location(), rot)
unreal.EditorLoadingAndSavingUtils.save_map(WORLD, '/Game/Labs/LumenRoom')
ACTORS.clear_actor_selection_set()
LEVEL.editor_set_game_view(True)
LEVEL.pilot_level_actor(camera)

def vec(v):
    return [v.x,v.y,v.z]

inventory = {'engine':unreal.SystemLibrary.get_engine_version(),'coordinate_mapping':'UE centimeters = (-100*CPU x, 100*CPU y, 100*CPU z)','actors':[]}
for actor in ACTORS.get_all_level_actors():
    rotation=actor.get_actor_rotation()
    entry = {'label':actor.get_actor_label(),'class':actor.get_class().get_name(),'path':actor.get_path_name(),'location_cm':vec(actor.get_actor_location()),'scale':vec(actor.get_actor_scale3d()),'rotation_degrees':{'pitch':rotation.pitch,'yaw':rotation.yaw,'roll':rotation.roll},'components':[]}
    for comp in actor.get_components_by_class(unreal.PrimitiveComponent):
        c = {'class':comp.get_class().get_name(),'path':comp.get_path_name()}
        component_rotation = comp.get_world_rotation()
        component_origin,component_extent,component_radius = unreal.SystemLibrary.get_component_bounds(comp)
        c.update({'location_cm':vec(comp.get_world_location()),'scale':vec(comp.get_world_scale()),'rotation_degrees':{'pitch':component_rotation.pitch,'yaw':component_rotation.yaw,'roll':component_rotation.roll},'bounds_origin_cm':vec(component_origin),'bounds_extent_cm':vec(component_extent),'bounds_sphere_radius_cm':component_radius})
        if isinstance(comp,unreal.StaticMeshComponent):
            c['mesh'] = comp.static_mesh.get_path_name() if comp.static_mesh else None
            c['materials'] = [comp.get_material(i).get_path_name() if comp.get_material(i) else None for i in range(comp.get_num_materials())]
        entry['components'].append(c)
    origin,extent = actor.get_actor_bounds(False)
    entry['bounds_origin_cm'],entry['bounds_extent_cm'] = vec(origin),vec(extent)
    if isinstance(actor,unreal.RectLight):
        entry['light']={'component_class':'RectLightComponent','lumens':lc.intensity,'source_width_cm':lc.source_width,'source_height_cm':lc.source_height,'attenuation_radius_cm':lc.attenuation_radius}
    inventory['actors'].append(entry)
(OUT/'scene-inventory.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')

for c in ['r.ScreenPercentage 100', 'r.HighResScreenshotDelay 64', 'r.AntiAliasingMethod 2', 'r.MotionBlurQuality 0', 'r.Lumen.HardwareRayTracing 0', 'r.Lumen.ScreenProbeGather.ScreenTraces 1', 'r.Lumen.Reflections.ScreenTraces 1', 'r.LumenScene.SurfaceCache.AtlasSize 4096','showflag.BillboardSprites 0','showflag.Grid 0','showflag.Selection 0','showflag.SelectionOutline 0','showflag.ModeWidgets 0']:
    console(c)

CAPTURES = [
    ('lumen-lit', ['viewmode lit']),
    ('lumen-scene', ['r.Lumen.Visualize 3']),
    ('lumen-surface-cache', ['r.Lumen.Visualize 5']),
    ('lumen-cache-albedo', ['r.Lumen.Visualize 8']),
    ('lumen-cache-normals', ['r.Lumen.Visualize 9']),
    ('lumen-cache-direct', ['r.Lumen.Visualize 12']),
    ('lumen-cache-indirect', ['r.Lumen.Visualize 13']),
    ('lumen-reflection-view', ['r.Lumen.Visualize 4']),
    ('lumen-cards', ['r.Lumen.Visualize 0', 'r.Lumen.Visualize.CardPlacement 1']),
    ('lumen-mesh-sdf', ['r.Lumen.Visualize.CardPlacement 0', 'showflag.VisualizeMeshDistanceFields 1']),
    ('lumen-global-sdf', ['showflag.VisualizeMeshDistanceFields 0', 'showflag.VisualizeGlobalDistanceField 1']),
    ('lumen-screen-probes', ['showflag.VisualizeGlobalDistanceField 0', 'r.ShaderPrint.Enable 1', 'r.Lumen.ScreenProbeGather.Debug 1', 'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1']),
    ('lumen-probe-traces', ['r.Lumen.ScreenProbeGather.Debug 0', 'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 0', 'r.Lumen.ScreenProbeGather.VisualizeTraces 1']),
    ('lumen-radiance-cache', ['r.Lumen.ScreenProbeGather.VisualizeTraces 0', 'r.Lumen.RadianceCache.Visualize 1', 'r.Lumen.RadianceCache.VisualizeRadiusScale 1']),
    ('lumen-radiosity-probes', ['r.Lumen.RadianceCache.Visualize 0', 'r.LumenScene.Radiosity.VisualizeProbes 1', 'r.LumenScene.Radiosity.VisualizeProbeRadius 3']),
    ('lumen-no-screen-traces', ['r.LumenScene.Radiosity.VisualizeProbes 0', 'r.Lumen.ScreenProbeGather.ScreenTraces 0', 'r.Lumen.Reflections.ScreenTraces 0']),
    ('lumen-hardware-tracing', ['r.Lumen.HardwareRayTracing 1']),
    ('lumen-direct-only', ['r.Lumen.HardwareRayTracing 0', 'r.DynamicGlobalIlluminationMethod 0', 'r.ReflectionMethod 0']),
    ('scene-lit', ['r.DynamicGlobalIlluminationMethod 1','r.ReflectionMethod 1','r.Lumen.ScreenProbeGather.ScreenTraces 1','r.Lumen.Reflections.ScreenTraces 1','viewmode lit']),
    ('scene-base-color', ['viewmode buffervisualization','r.BufferVisualizationTarget BaseColor']),
    ('scene-world-normal', ['r.BufferVisualizationTarget WorldNormal']),
    ('scene-roughness', ['r.BufferVisualizationTarget Roughness']),
    ('scene-metallic', ['r.BufferVisualizationTarget Metallic']),
    ('scene-depth', ['viewmode lit']),
    ('scene-wireframe', ['viewmode wireframe']),
    ('scene-bounds', ['viewmode lit','showflag.Bounds 1']),
    ('scene-moved-box', ['showflag.Bounds 0','viewmode lit']),
    ('scene-changed-light', ['viewmode lit']),
]
STATE = {'frame':0,'index':-1,'next':200,'task':None,'requested':0,'started':time.time()}
REPORT = {'engine':unreal.SystemLibrary.get_engine_version(), 'source_scene':'Seven separate cube StaticMeshActors sharing /Engine/BasicShapes/Cube.Cube; UE cm = (-100*CPU x,100*CPU y,100*CPU z)', 'capture_method':'Persistent Unreal editor level viewport, explicit FSceneViewport::Draw calls and ordinary FScreenshotRequest; 1280x720, no screenshot UI', 'warmup':'200 initial draw requests, then 90 draw requests after each state change before capture; not a convergence-rate or performance measurement', 'exposure_ev100':4.0, 'sdf_debug_exposure_ev100':-1.0, 'mesh_sdf_limitation':'Nonuniformly scaled shared cube SDFs approximate these boxes; this is not the analytic teaching lab SDF', 'captures':[]}

def tick(delta):
    if STATE.get('busy'):
        return
    STATE['busy'] = True
    try:
        STATE['frame'] += 1
        unreal.ArdaLabCapture.render_viewport()
        if STATE['frame'] < STATE['next']:
            return
        if STATE['task'] is not None:
            if not STATE['task'].is_task_done():
                return
            STATE['task'] = None
        if STATE['requested']:
            name, commands = CAPTURES[STATE['index']]
            unreal.ArdaLabCapture.capture_viewport(str(OUT/(name+'.png')))
            REPORT['captures'].append({'name':name,'commands':commands,'frame':STATE['frame']})
            STATE['requested'] = 0
            STATE['next'] = STATE['frame'] + 20
            unreal.log('ARDA_CAPTURE '+name)
            return
        STATE['index'] += 1
        if STATE['index'] >= len(CAPTURES):
            REPORT['elapsed_seconds'] = time.time()-STATE['started']
            (OUT/'capture-report.json').write_text(json.dumps(REPORT,indent=2),encoding='utf-8')
            unreal.unregister_slate_post_tick_callback(HANDLE)
            unreal.EditorPythonScripting.set_keep_python_script_alive(False)
            return
        name, commands = CAPTURES[STATE['index']]
        pp.add_or_update_blendable(depth_mat,1.0 if name=='scene-depth' else 0.0)
        if name=='scene-depth':
            REPORT['scene_depth_display']='Native postprocess material: SceneDepth (linear centimeters) / 1000, after tonemapping. White corresponds to 10m; not raw device Z.'
        exposure = -1.0 if name in ('lumen-mesh-sdf','lumen-global-sdf') else 4.0
        exposure_settings=pp.get_editor_property('settings')
        exposure_settings.set_editor_property('auto_exposure_min_brightness',exposure)
        exposure_settings.set_editor_property('auto_exposure_max_brightness',exposure)
        pp.set_editor_property('settings',exposure_settings)
        if name == 'scene-moved-box':
            for actor in ACTORS.get_all_level_actors():
                if actor.get_actor_label() == 'Matte block':
                    actor.static_mesh_component.set_mobility(unreal.ComponentMobility.MOVABLE)
                    before = actor.get_actor_location()
                    actor.set_actor_location(before + unreal.Vector(-100,-80,0),False,False)
                    REPORT['moved_box'] = {'before_cm':vec(before),'after_cm':vec(actor.get_actor_location())}
        if name == 'scene-changed-light':
            lc.set_intensity(7200)
            lc.set_light_color(unreal.LinearColor(1,.55,.25,1))
            REPORT['changed_light'] = {'intensity_lumens':7200,'linear_color':[1,.55,.25]}
        for command in commands:
            console(command)
        STATE['requested'] = 1
        STATE['next'] = STATE['frame'] + 90
    except Exception:
        import traceback
        (OUT/'capture-error.txt').write_text(traceback.format_exc())
        unreal.log_error(traceback.format_exc())
        unreal.unregister_slate_post_tick_callback(HANDLE)
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)

    finally:
        STATE['busy'] = False

unreal.EditorPythonScripting.set_keep_python_script_alive(True)
HANDLE = unreal.register_slate_post_tick_callback(tick)
unreal.log('ARDA_SETUP_COMPLETE')
