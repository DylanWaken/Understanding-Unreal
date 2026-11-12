"""Extend the saved Cornell room with real attachment, ISM, and skeletal examples.

Run run.ps1 -Build -Script capture_scene_room.py after capture.py. Copy Epic's
Templates/TemplateResources/Standard/Mannequin/Content to the temporary project's
Content/Mannequin before running. Template assets stay outside this repository.
The baseline map is loaded read-only and the extension is saved under a new name.
"""
import hashlib
import json
import math
import os
import time
import traceback
from pathlib import Path
import unreal

ROOT = Path(os.environ.get('ARDA_UNREAL_LAB', str(Path.home() / 'AppData/Local/Temp/arda-unreal-lab')))
OUT = ROOT / 'scene-room-captures'
OUT.mkdir(parents=True, exist_ok=True)
LEVEL = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
ACTORS = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
LEVEL.load_level('/Game/Labs/LumenRoom')
WORLD = unreal.EditorLevelLibrary.get_editor_world()
BRIDGE = unreal.ArdaLabCapture
CUBE = unreal.load_asset('/Engine/BasicShapes/Cube.Cube')
SPHERE = unreal.load_asset('/Engine/BasicShapes/Sphere.Sphere')
CYLINDER = unreal.load_asset('/Engine/BasicShapes/Cylinder.Cylinder')
PLANE = unreal.load_asset('/Engine/BasicShapes/Plane.Plane')

def vec(value):
    return [float(value.x), float(value.y), float(value.z)]

def rotation(value):
    return {'pitch':float(value.pitch),'yaw':float(value.yaw),'roll':float(value.roll)}

def transform(location=(0,0,0), scale=(1,1,1), angles=(0,0,0)):
    return unreal.Transform(location=unreal.Vector(*location), rotation=unreal.Rotator(pitch=angles[0],yaw=angles[1],roll=angles[2]), scale=unreal.Vector(*scale))

def actor(label, location):
    result = ACTORS.spawn_actor_from_class(unreal.Actor, unreal.Vector(0,0,0))
    result.set_actor_label(label)
    BRIDGE.make_scene_root(result)
    result.set_actor_location(unreal.Vector(*location), False, False)
    return result

def material(name, color, roughness=.6, metallic=0, emission=0, masked=False):
    path='/Game/Labs/RoomMaterials/'+name
    existing=unreal.load_asset(path)
    if existing:
        return existing
    mat=TOOLS.create_asset(name,'/Game/Labs/RoomMaterials',unreal.Material,unreal.MaterialFactoryNew())
    mel=unreal.MaterialEditingLibrary
    color_node=mel.create_material_expression(mat,unreal.MaterialExpressionConstant3Vector)
    color_node.set_editor_property('constant',unreal.LinearColor(*color,1))
    mel.connect_material_property(color_node,'',unreal.MaterialProperty.MP_BASE_COLOR)
    for value, prop in [(roughness,unreal.MaterialProperty.MP_ROUGHNESS),(metallic,unreal.MaterialProperty.MP_METALLIC)]:
        node=mel.create_material_expression(mat,unreal.MaterialExpressionConstant)
        node.set_editor_property('r',value)
        mel.connect_material_property(node,'',prop)
    if emission:
        node=mel.create_material_expression(mat,unreal.MaterialExpressionMultiply)
        node.set_editor_property('const_b',emission)
        mel.connect_material_expressions(color_node,'',node,'A')
        mel.connect_material_property(node,'',unreal.MaterialProperty.MP_EMISSIVE_COLOR)
    if masked:
        mat.set_editor_property('blend_mode',unreal.BlendMode.BLEND_MASKED)
        mat.set_editor_property('two_sided',True)
        mat.set_editor_property('shading_model',unreal.MaterialShadingModel.MSM_TWO_SIDED_FOLIAGE)
        mel.connect_material_property(color_node,'',unreal.MaterialProperty.MP_SUBSURFACE_COLOR)
        uv=mel.create_material_expression(mat,unreal.MaterialExpressionTextureCoordinate)
        center=mel.create_material_expression(mat,unreal.MaterialExpressionConstant2Vector)
        center.set_editor_property('r',.5)
        center.set_editor_property('g',.5)
        mask=mel.create_material_expression(mat,unreal.MaterialExpressionSphereMask)
        mask.set_editor_property('attenuation_radius',.48)
        mask.set_editor_property('hardness_percent',100)
        mel.connect_material_expressions(uv,'',mask,'A')
        mel.connect_material_expressions(center,'',mask,'B')
        mel.connect_material_property(mask,'',unreal.MaterialProperty.MP_OPACITY_MASK)
    mel.recompile_material(mat)
    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat

door_mat=material('DoorBlue',(.035,.15,.32),.38)
lamp_mat=material('IndicatorAmber',(1,.3,.025),.3,emission=3)
pot_mat=material('Terracotta',(.35,.13,.055),.8)
leaf_mat=material('MaskedLeaf',(.06,.32,.045),.65,masked=True)
stem_mat=material('Stem',(.12,.055,.025),.85)

# One actor, a non-rendering root at the hinge, and two independently renderable
# child meshes. The indicator is a real child component; its emissive material
# does not create a ULightComponent or analytically injected point light.
door=actor('Teaching door — hinge root',(-150,200,0))
door_panel=BRIDGE.add_mesh_part(door,'DoorPanel',CUBE,door_mat,transform((50,0,100),(1,.06,2)))
indicator=BRIDGE.add_mesh_part(door,'AttachedIndicator',SPHERE,lamp_mat,transform((60,0,150),(.10,.10,.10)))

# Three pots and 24 masked leaf cards are instances, not duplicated actors.
foliage=actor('Potted foliage — shared instances',(0,0,0))
pot_centers=[(-145,80),(-105,80),(-65,80)]
pots=BRIDGE.add_instance_group(foliage,'Pots',CYLINDER,pot_mat,[transform((x,y,12),(.25,.25,.24)) for x,y in pot_centers])
stems=BRIDGE.add_instance_group(foliage,'Stems',CYLINDER,stem_mat,[transform((x,y,35),(.025,.025,.55)) for x,y in pot_centers])
leaf_transforms=[]
for x,y in pot_centers:
    for i in range(8):
        angle=2*math.pi*i/8
        height=28+(i%4)*8
        leaf_transforms.append(transform((x+9*math.cos(angle),y+9*math.sin(angle),height),(.34,.13,1),(45,math.degrees(angle),0)))
leaves=BRIDGE.add_instance_group(foliage,'MaskedLeaves',PLANE,leaf_mat,leaf_transforms)

mesh=unreal.load_asset('/Game/Mannequin/Character/Mesh/SK_Mannequin.SK_Mannequin')
walk=unreal.load_asset('/Game/Mannequin/Animations/ThirdPersonWalk.ThirdPersonWalk')
assert mesh and walk, 'Copy the Standard Mannequin template Content into Content/Mannequin before running.'
character=ACTORS.spawn_actor_from_class(unreal.SkeletalMeshActor,unreal.Vector(65,110,0),unreal.Rotator(pitch=0,yaw=-90,roll=0))
character.set_actor_label('Visiting mannequin — skeletal skinning')
skel=character.skeletal_mesh_component
skel.set_skeletal_mesh_asset(mesh)
skel.set_mobility(unreal.ComponentMobility.MOVABLE)
BRIDGE.set_frozen_pose(skel,walk,.10)

camera=next(a for a in ACTORS.get_all_level_actors() if isinstance(a,unreal.CameraActor))
pp=next(a for a in ACTORS.get_all_level_actors() if isinstance(a,unreal.PostProcessVolume))
camera.camera_component.set_field_of_view(math.degrees(2*math.atan(math.tan(math.radians(43/2))*16/9)))
settings=pp.get_editor_property('settings')
settings.set_editor_property('auto_exposure_min_brightness',4.)
settings.set_editor_property('auto_exposure_max_brightness',4.)
pp.set_editor_property('settings',settings)
ACTORS.clear_actor_selection_set()
LEVEL.editor_set_game_view(True)
LEVEL.pilot_level_actor(camera)

CAMERAS={
    'overview':((0,-400,165),(0,280,140)),
    'character':((65,-190,115),(65,110,95)),
    'foliage':((-105,-55,100),(-105,80,32)),
    'door':((80,-80,155),(-105,230,110)),
}

def camera_at(key):
    location,target=CAMERAS[key]
    rot=unreal.MathLibrary.find_look_at_rotation(unreal.Vector(*location),unreal.Vector(*target))
    camera.set_actor_location(unreal.Vector(*location),False,False)
    camera.set_actor_rotation(rot,False)
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(camera.get_actor_location(),rot)

def console(command):
    unreal.SystemLibrary.execute_console_command(WORLD,command)

for command in ['r.ScreenPercentage 100','r.AntiAliasingMethod 2','r.MotionBlurQuality 0','r.Lumen.HardwareRayTracing 0','r.Lumen.ScreenProbeGather.ScreenTraces 1','r.Lumen.Reflections.ScreenTraces 1','r.DynamicGlobalIlluminationMethod 1','r.ReflectionMethod 1','r.Lumen.Visualize 0','showflag.BillboardSprites 0','showflag.Grid 0','showflag.Selection 0','showflag.SelectionOutline 0','showflag.ModeWidgets 0','showflag.Bounds 0']:
    console(command)

def inventory():
    result=[]
    for item in ACTORS.get_all_level_actors():
        entry={'label':item.get_actor_label(),'class':item.get_class().get_name(),'path':item.get_path_name(),'location_cm':vec(item.get_actor_location()),'rotation_degrees':rotation(item.get_actor_rotation()),'components':[]}
        for component in item.get_components_by_class(unreal.SceneComponent):
            parent=component.get_attach_parent()
            c={'class':component.get_class().get_name(),'path':component.get_path_name(),'parent':parent.get_path_name() if parent else None,'location_cm':vec(component.get_world_location()),'scale':vec(component.get_world_scale()),'rotation_degrees':rotation(component.get_world_rotation())}
            if isinstance(component,unreal.PrimitiveComponent):
                origin,extent,radius=unreal.SystemLibrary.get_component_bounds(component)
                c.update({'bounds_origin_cm':vec(origin),'bounds_extent_cm':vec(extent),'bounds_radius_cm':float(radius)})
                c['materials']=[component.get_material(i).get_path_name() if component.get_material(i) else None for i in range(component.get_num_materials())]
            if isinstance(component,unreal.StaticMeshComponent):
                c['mesh']=component.static_mesh.get_path_name() if component.static_mesh else None
            if isinstance(component,unreal.InstancedStaticMeshComponent):
                c['instance_count']=component.get_instance_count()
                c['instance_world_transforms']=[]
                for i in range(component.get_instance_count()):
                    t=component.get_instance_transform(i,world_space=True)
                    c['instance_world_transforms'].append({'location_cm':vec(t.translation),'scale':vec(t.scale3d),'rotation_degrees':rotation(t.rotation.rotator())})
            if isinstance(component,unreal.SkeletalMeshComponent):
                c['skeletal_mesh']=mesh.get_path_name()
                c['bone_count']=component.get_num_bones()
                c['animation']=walk.get_path_name()
            entry['components'].append(c)
        result.append(entry)
    return result

camera_at('overview')
unreal.EditorLoadingAndSavingUtils.save_map(WORLD,'/Game/Labs/SceneRepresentationRoom')
(OUT/'scene-room-inventory.json').write_text(json.dumps({'engine':unreal.SystemLibrary.get_engine_version(),'map':'/Game/Labs/SceneRepresentationRoom','baseline':'/Game/Labs/LumenRoom','actors':inventory()},indent=2),encoding='utf-8')

# A is closed door and a 0.10 s walk pose. B rotates the same hinge by +90°
# and poses the same skeletal component at 0.45 s; it does not translate it.
CAPTURES=[
    ('room-extended-lit','overview','lit','',0,.10),
    ('room-extended-base-color','overview','buffer','BaseColor',0,.10),
    ('room-extended-world-normal','overview','buffer','WorldNormal',0,.10),
    ('room-extended-wireframe','overview','wireframe','',0,.10),
    ('room-extended-bounds','overview','lit','',0,.10),
    ('room-foliage','foliage','lit','',0,.10),
    ('room-foliage-wireframe','foliage','wireframe','',0,.10),
    ('room-character-pose-a','character','lit','',0,.10),
    ('room-character-pose-b','character','lit','',0,.45),
    ('room-door-closed','door','lit','',0,.10),
    ('room-door-open','door','lit','',90,.10),
    ('room-extended-moved','overview','lit','',90,.45),
]
REPORT={'engine':unreal.SystemLibrary.get_engine_version(),'map':'/Game/Labs/SceneRepresentationRoom','baseline_map':'/Game/Labs/LumenRoom','baseline_mesh_transforms':'All seven original cube actors retained unchanged','camera_vertical_fov_degrees':43,'exposure_ev100':4,'image_size':[1280,720],'capture_method':'Actual Unreal editor FSceneViewport; ordinary FScreenshotRequest; no UI or synthetic pixels','warmup':'200 initial viewport draw requests; 100 after each change; not a convergence or performance measurement','lighting':'Original ceiling RectLight 2400 lumens; software Lumen tracing; indicator is emissive geometry without a LightComponent','foliage':'3 pot + 3 stem + 24 masked plane instances across 3 InstancedStaticMeshComponents; authored as teaching foliage, not imported foliage assets; masked Two Sided Foliage material','character':'Epic Standard Mannequin template; real SkeletalMeshActor and skeletal animation evaluated at frozen times; not an animation-controller or cloth simulation demo','template_source':'D:/UnrealEngine/Templates/TemplateResources/Standard/Mannequin/Content','template_publication':'Only rendered screenshots and asset paths are published; template source assets remain in the temporary Unreal project','cameras':{key:{'location_cm':list(value[0]),'target_cm':list(value[1])} for key,value in CAMERAS.items()},'captures':[]}
STATE={'frame':0,'index':-1,'next':200,'requested':False,'started':time.time(),'busy':False}

def tick(delta):
    if STATE['busy']:
        return
    STATE['busy']=True
    try:
        STATE['frame']+=1
        BRIDGE.render_viewport()
        if STATE['frame']<STATE['next']:
            return
        if STATE['requested']:
            name,cam,mode,buffer,yaw,seconds=CAPTURES[STATE['index']]
            BRIDGE.capture_viewport(str(OUT/(name+'.png')))
            REPORT['captures'].append({'name':name,'camera':cam,'mode':mode,'buffer':buffer,'door_yaw_degrees':yaw,'animation_seconds':seconds,'frame':STATE['frame'],'hinge_location_cm':vec(door.get_actor_location()),'indicator_world_location_cm':vec(indicator.get_world_location()),'character_world_location_cm':vec(character.get_actor_location()),'character_bounds_cm':{'origin':vec(unreal.SystemLibrary.get_component_bounds(skel)[0]),'extent':vec(unreal.SystemLibrary.get_component_bounds(skel)[1])}})
            STATE['requested']=False
            STATE['next']=STATE['frame']+20
            unreal.log('ARDA_SCENE_ROOM_CAPTURE '+name)
            return
        STATE['index']+=1
        if STATE['index']>=len(CAPTURES):
            REPORT['elapsed_seconds']=time.time()-STATE['started']
            script_path=os.environ.get('ARDA_CAPTURE_SCRIPT')
            if script_path:
                REPORT['source_script_sha256']=hashlib.sha256(Path(script_path).read_bytes()).hexdigest()
            REPORT['image_sha256']={name+'.png':hashlib.sha256((OUT/(name+'.png')).read_bytes()).hexdigest() for name,*_ in CAPTURES}
            (OUT/'scene-room-capture-report.json').write_text(json.dumps(REPORT,indent=2),encoding='utf-8')
            # Restore baseline extension state before saving; original room untouched.
            door.set_actor_rotation(unreal.Rotator(pitch=0,yaw=0,roll=0),False)
            BRIDGE.set_frozen_pose(skel,walk,.10)
            camera_at('overview')
            BRIDGE.set_viewport_mode('lit','')
            unreal.EditorLoadingAndSavingUtils.save_map(WORLD,'/Game/Labs/SceneRepresentationRoom')
            unreal.unregister_slate_post_tick_callback(HANDLE)
            unreal.EditorPythonScripting.set_keep_python_script_alive(False)
            return
        name,cam,mode,buffer,yaw,seconds=CAPTURES[STATE['index']]
        camera_at(cam)
        door.set_actor_rotation(unreal.Rotator(pitch=0,yaw=yaw,roll=0),False)
        BRIDGE.set_frozen_pose(skel,walk,seconds)
        BRIDGE.set_viewport_mode(mode,buffer)
        console('showflag.Bounds '+('1' if name=='room-extended-bounds' else '0'))
        STATE['requested']=True
        STATE['next']=STATE['frame']+100
    except Exception:
        (OUT/'capture-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
        unreal.log_error(traceback.format_exc())
        unreal.unregister_slate_post_tick_callback(HANDLE)
        unreal.EditorPythonScripting.set_keep_python_script_alive(False)
    finally:
        STATE['busy']=False

unreal.EditorPythonScripting.set_keep_python_script_alive(True)
HANDLE=unreal.register_slate_post_tick_callback(tick)
unreal.log('ARDA_SCENE_ROOM_SETUP_COMPLETE')
