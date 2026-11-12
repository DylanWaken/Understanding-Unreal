"""Import Stanford bunny OBJ, build real Nanite resources, capture UE modes."""
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
MESHES = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
unreal.EditorLoadingAndSavingUtils.new_blank_map(False)
WORLD = unreal.EditorLevelLibrary.get_editor_world()
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()

def console(command):
    unreal.log('ARDA_COMMAND '+command)
    if command.startswith('viewmode '):
        unreal.ArdaLabCapture.set_viewport_mode(command.split()[1],'')
    unreal.SystemLibrary.execute_console_command(WORLD,command)

console('Interchange.FeatureFlags.Import.OBJ 0')
mesh = unreal.load_asset('/Game/Labs/Bunny/StanfordBunny')
if not mesh:
    task = unreal.AssetImportTask()
    task.set_editor_property('filename',str(ROOT/'shared/bunny.obj'))
    task.set_editor_property('destination_path','/Game/Labs/Bunny')
    task.set_editor_property('destination_name','StanfordBunny')
    task.set_editor_property('automated',True)
    task.set_editor_property('replace_existing',True)
    task.set_editor_property('save',True)
    options = unreal.FbxImportUI()
    options.set_editor_property('import_as_skeletal',False)
    options.set_editor_property('import_materials',False)
    options.set_editor_property('import_textures',False)
    options.set_editor_property('mesh_type_to_import',unreal.FBXImportType.FBXIT_STATIC_MESH)
    options.static_mesh_import_data.set_editor_property('generate_lightmap_u_vs',False)
    options.static_mesh_import_data.set_editor_property('combine_meshes',True)
    options.static_mesh_import_data.set_editor_property('normal_import_method',unreal.FBXNormalImportMethod.FBXNIM_COMPUTE_NORMALS)
    task.set_editor_property('options',options)
    TOOLS.import_asset_tasks([task])
    paths = task.get_editor_property('imported_object_paths')
    unreal.log('ARDA_IMPORTED '+str(paths))
    mesh = unreal.load_asset(paths[0])
settings = MESHES.get_nanite_settings(mesh)
settings.set_editor_property('enabled',True)
MESHES.set_nanite_settings(mesh,settings,True)
unreal.EditorAssetLibrary.save_loaded_asset(mesh)

mat = unreal.load_asset('/Game/Labs/Materials/BunnyClay')
if not mat:
    mat = TOOLS.create_asset('BunnyClay','/Game/Labs/Materials',unreal.Material,unreal.MaterialFactoryNew())
    c = unreal.MaterialEditingLibrary.create_material_expression(mat,unreal.MaterialExpressionConstant3Vector)
    c.set_editor_property('constant',unreal.LinearColor(.56,.36,.19,1))
    unreal.MaterialEditingLibrary.connect_material_property(c,'',unreal.MaterialProperty.MP_BASE_COLOR)
    r = unreal.MaterialEditingLibrary.create_material_expression(mat,unreal.MaterialExpressionConstant)
    r.set_editor_property('r',.55)
    unreal.MaterialEditingLibrary.connect_material_property(r,'',unreal.MaterialProperty.MP_ROUGHNESS)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
tess = unreal.load_asset('/Game/Labs/Materials/BunnyRipplesVerified')
if not tess:
    tess = unreal.EditorAssetLibrary.duplicate_asset('/Game/Labs/Materials/BunnyClay','/Game/Labs/Materials/BunnyRipplesVerified')
    tess.set_editor_property('enable_tessellation',True)
    tess.set_editor_property('displacement_scaling',unreal.DisplacementScaling(magnitude=2.0,center=.5))
    pos = unreal.MaterialEditingLibrary.create_material_expression(tess,unreal.MaterialExpressionWorldPosition)
    pos.set_editor_property('world_position_shader_offset',unreal.WorldPositionIncludedOffsets.WPT_EXCLUDE_ALL_SHADER_OFFSETS)
    mask = unreal.MaterialEditingLibrary.create_material_expression(tess,unreal.MaterialExpressionComponentMask)
    mask.set_editor_property('r',True)
    mask.set_editor_property('g',False)
    mask.set_editor_property('b',False)
    mask.set_editor_property('a',False)
    scale = unreal.MaterialEditingLibrary.create_material_expression(tess,unreal.MaterialExpressionMultiply)
    scale.set_editor_property('const_b',.05)
    wave = unreal.MaterialEditingLibrary.create_material_expression(tess,unreal.MaterialExpressionSine)
    wave.set_editor_property('period',1.0)
    half = unreal.MaterialEditingLibrary.create_material_expression(tess,unreal.MaterialExpressionMultiply)
    half.set_editor_property('const_b',.5)
    bias = unreal.MaterialEditingLibrary.create_material_expression(tess,unreal.MaterialExpressionAdd)
    bias.set_editor_property('const_b',.5)
    for a,b,pin in [(pos,mask,''),(mask,scale,'A'),(scale,wave,''),(wave,half,'A'),(half,bias,'A')]:
        assert unreal.MaterialEditingLibrary.connect_material_expressions(a,'',b,pin)
    assert unreal.ArdaLabCapture.connect_displacement(bias)
    unreal.MaterialEditingLibrary.recompile_material(tess)
    unreal.EditorAssetLibrary.save_loaded_asset(tess)
bunny = ACTORS.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,0))
bunny.set_actor_label('Stanford bunny — real Nanite asset')
bunny.static_mesh_component.set_static_mesh(mesh)
bunny.static_mesh_component.set_material(0,mat)
bunny.set_actor_rotation(unreal.Rotator(pitch=0,yaw=180,roll=0),False)
floor = ACTORS.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,-6))
floor.static_mesh_component.set_static_mesh(unreal.load_asset('/Engine/BasicShapes/Cube.Cube'))
floor.set_actor_scale3d(unreal.Vector(6,6,.1))
neutral = unreal.load_asset('/Game/Labs/Materials/Neutral')
if neutral:
    floor.static_mesh_component.set_material(0,neutral)
light = ACTORS.spawn_actor_from_class(unreal.DirectionalLight,unreal.Vector(0,0,250),unreal.Rotator(pitch=-45,yaw=-35,roll=0))
light.get_component_by_class(unreal.DirectionalLightComponent).set_intensity(4)
fill = ACTORS.spawn_actor_from_class(unreal.RectLight,unreal.Vector(-120,-120,150),unreal.Rotator(pitch=-25,yaw=45,roll=0))
fc = fill.get_component_by_class(unreal.RectLightComponent)
fc.set_editor_property('intensity_units',unreal.LightUnits.LUMENS)
fc.set_intensity(600)
fc.set_source_width(150)
fc.set_source_height(150)
fc.set_attenuation_radius(1200)
pp = ACTORS.spawn_actor_from_class(unreal.PostProcessVolume,unreal.Vector(0,0,0))
pp.set_editor_property('unbound',True)
s = pp.get_editor_property('settings')
for k,v in {'override_auto_exposure_min_brightness':True,'override_auto_exposure_max_brightness':True,'auto_exposure_min_brightness':4.0,'auto_exposure_max_brightness':4.0,'override_auto_exposure_bias':True,'auto_exposure_bias':0.0}.items():
    s.set_editor_property(k,v)
pp.set_editor_property('settings',s)
camera = ACTORS.spawn_actor_from_class(unreal.CameraActor,unreal.Vector(150,-220,100))
camera.camera_component.set_field_of_view(50)

def set_camera(distance):
    direction = unreal.Vector(150,-220,50)
    pos = unreal.Vector(direction.x*distance,direction.y*distance,50+direction.z*distance)
    rot = unreal.MathLibrary.find_look_at_rotation(pos,unreal.Vector(0,0,50))
    camera.set_actor_location(pos,False,False)
    camera.set_actor_rotation(rot,False)
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(pos,rot)

set_camera(1)
unreal.EditorLoadingAndSavingUtils.save_map(WORLD, '/Game/Labs/NaniteBunny')
ACTORS.clear_actor_selection_set()
LEVEL.editor_set_game_view(True)
LEVEL.pilot_level_actor(camera)
for c in ['r.ScreenPercentage 100','r.HighResScreenshotDelay 64','r.AntiAliasingMethod 2','r.MotionBlurQuality 0','r.Nanite 1','r.Nanite.MaxPixelsPerEdge 1','r.Lumen.HardwareRayTracing 0']:
    console(c)
CAPTURES = [
    ('nanite-lit-near','',1),
    ('nanite-triangles-near','Triangles',1),
    ('nanite-clusters-near','Clusters',1),
    ('nanite-groups-near','Groups',1),
    ('nanite-pages','Pages',1),
    ('nanite-hierarchy','Hierarchy',1),
    ('nanite-raster-mode','RasterMode',1),
    ('nanite-raster-bins','RasterBins',1),
    ('nanite-shading-bins','ShadingBins',1),
    ('nanite-overdraw','Overdraw',1),
    ('nanite-triangles-far','Triangles',4),
    ('nanite-clusters-far','Clusters',4),
    ('nanite-groups-far','Groups',4),
    ('nanite-lit-far','',4),
    ('nanite-tessellation-lit','',1),
    ('nanite-tessellation-patches','Patches',1),
    ('nanite-tessellation-triangles','Triangles',1),
    ('nanite-tessellation-mask','Tessellation',1),
]
STATE = {'frame':0,'index':-1,'next':200,'task':None,'requested':False,'started':time.time()}
REPORT = {'engine':unreal.SystemLibrary.get_engine_version(),'asset':mesh.get_path_name(),'nanite_enabled':MESHES.get_nanite_settings(mesh).enabled,'source_vertices':35947,'source_triangles':69451,'captures':[]}
REPORT['built_nanite_resource'] = json.loads(unreal.ArdaLabCapture.nanite_stats(mesh))
REPORT['built_nanite_resource']['streamable_bulk_data_bytes_at_query'] = REPORT['built_nanite_resource'].pop('streamable_data_bytes')
REPORT['resource_notes'] = 'Clusters span all built LODs, not simultaneously selected clusters. Streamable bulk-data size is the queried in-memory bulk field; zero does not mean zero streamable geometry or encoded stream size. Source OBJ vertex count and post-build input vertex count need not match.'
REPORT['capture_method'] = 'Persistent Unreal editor level viewport, explicit FSceneViewport::Draw calls and ordinary FScreenshotRequest; 1280x720, no screenshot UI'
REPORT['warmup'] = '200 initial draw requests, then 90 draw requests after each state change; not a convergence-rate or performance measurement'
REPORT['tessellation'] = {'material':'/Game/Labs/Materials/BunnyRipplesVerified','input':'0.5 + 0.5*sin(2*pi*0.05*world_x_cm), World Position excludes material offsets','magnitude_cm':2.0,'center':0.5,'signed_displacement_range_cm':[-1,1],'period_cm':20}
REPORT['exposure_ev100'] = 4.0

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
            name,mode,distance = CAPTURES[STATE['index']]
            unreal.ArdaLabCapture.capture_viewport(str(OUT/(name+'.png')))
            REPORT['captures'].append({'name':name,'nanite_visualize':mode,'camera_distance_factor':distance,'frame':STATE['frame']})
            STATE['requested'] = False
            STATE['next'] = STATE['frame']+20
            unreal.log('ARDA_CAPTURE '+name)
            return
        STATE['index'] += 1
        if STATE['index'] >= len(CAPTURES):
            REPORT['elapsed_seconds'] = time.time()-STATE['started']
            (OUT/'bunny-report.json').write_text(json.dumps(REPORT,indent=2),encoding='utf-8')
            unreal.unregister_slate_post_tick_callback(HANDLE)
            unreal.EditorPythonScripting.set_keep_python_script_alive(False)
            return
        name,mode,distance = CAPTURES[STATE['index']]
        bunny.static_mesh_component.set_material(0,tess if name.startswith('nanite-tessellation') else mat)
        set_camera(distance)
        console('viewmode lit')
        console('r.Nanite.Visualize '+(mode if mode else 'off'))
        STATE['requested'] = True
        STATE['next'] = STATE['frame']+90
    except Exception:
        import traceback
        (OUT/'bunny-error.txt').write_text(traceback.format_exc())
        unreal.log_error(traceback.format_exc())
        unreal.unregister_slate_post_tick_callback(HANDLE)
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)

    finally:
        STATE['busy'] = False

unreal.EditorPythonScripting.set_keep_python_script_alive(True)
HANDLE = unreal.register_slate_post_tick_callback(tick)
unreal.log('ARDA_SETUP_COMPLETE')
