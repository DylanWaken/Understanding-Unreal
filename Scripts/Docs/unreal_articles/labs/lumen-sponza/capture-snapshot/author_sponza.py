"""Author the saved Sponza teaching map inside UnrealEditor (Python).

Loads the 103 imported static meshes from /Game/Sponza/SponzaSplit/StaticMeshes,
spawns one StaticMeshActor per source glTF primitive at its measured pivot,
then adds the two fixed cameras, the six-sphere material chart, sun/sky and a
fixed-exposure post-process volume.  Saves /Game/Labs/SponzaTeaching.

Run with -ExecutePythonScript while the editor is hidden/offscreen.  Every
measurement and command is written to output/scene-manifest.json.
"""
import hashlib
import json
import math
import os
import time
import traceback
from pathlib import Path

import unreal

WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', r'D:\UnrealUnderstanding-Sponza'))
OUTPUT = WORKSPACE / 'output'
LOGS = OUTPUT / 'logs'
OUTPUT.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)
SPLIT_MANIFEST = WORKSPACE / 'source' / 'Sponza' / 'derived' / 'split-manifest.json'
SOURCE_MANIFEST = WORKSPACE / 'source' / 'Sponza' / 'download-manifest.json'
PROJECT = WORKSPACE / 'project' / 'UnrealLab'
MAP_PATH = '/Game/Labs/SponzaTeaching'
MESH_DIR = '/Game/Sponza/SponzaSplit/StaticMeshes'
MATERIAL_DIR = '/Game/Sponza/SponzaSplit/Materials'
LAB_MATERIAL_DIR = '/Game/Labs/Materials'

# Fixed measurements (cm).  A vertical ray through the courtyard from the fixed
# camera column first hits the tiled walking floor at z=-2; the z=-100 plane in
# the source asset is the exterior ground below the building, not the courtyard.
FLOOR_Z = -2.0
HERO_CAMERA_LOCATION = (-1000.0, 0.0, 168.0)
HERO_CAMERA_TARGET = (600.0, 0.0, 250.0)
MATERIAL_CAMERA_LOCATION = (250.0, 0.0, 168.0)
MATERIAL_CAMERA_TARGET = (850.0, 0.0, 60.0)
# One row of six across the nave at x=850.  A row along the view axis collapsed
# into a single sphere from the oblique camera, so the chart is perpendicular to
# the material camera instead.
CHART_X = 850.0
CHART_YS = (135.0, 81.0, 27.0, -27.0, -81.0, -135.0)
CHART_CENTER = (850.0, 0.0)
SPHERE_RADIUS_CM = 22.0
PLINTH_HEIGHT_CM = 40.0
PLINTH_RADIUS_CM = 28.0
# Pinned exposure: EV100 15 crushed the covered courtyard to black.  EV100 7 was
# selected from an in-engine sweep (13/11/9/7/5) as the brightest value that keeps
# the stone and fabric textures readable without clipping the whole floor.
EV100 = 7.0
SUN_LUX = 80000.0

L = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
A = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
BRIDGE = unreal.ArdaLabCapture
WORLD = None
COMMANDS = []


def log(message):
    unreal.log('ARDA_SPONZA_AUTHOR ' + str(message))


def atomic_json(path, data):
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def load_json(path):
    """Read JSON written by PowerShell/Unreal, tolerating a UTF-8 BOM."""
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def vec(v):
    return [float(v.x), float(v.y), float(v.z)]


def rot(v):
    return {'pitch': float(v.pitch), 'yaw': float(v.yaw), 'roll': float(v.roll)}


def console(command):
    COMMANDS.append(command)
    unreal.log('ARDA_SPONZA_COMMAND ' + command)
    return unreal.SystemLibrary.execute_console_command(WORLD, command)


def cvar(name):
    try:
        return json.loads(BRIDGE.get_console_variable(name))
    except Exception:
        return {'name': name, 'found': False}


def component_materials(component):
    materials = []
    try:
        count = component.get_num_materials()
    except Exception:
        return materials
    for i in range(count):
        try:
            material = component.get_material(i)
            materials.append(material.get_path_name() if material else None)
        except Exception:
            materials.append(None)
    return materials


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
    try:
        origin, extent = actor.get_actor_bounds(False)
        record['world_bounds_origin_cm'] = vec(origin)
        record['world_bounds_extent_cm'] = vec(extent)
        record['world_bounds_min_cm'] = [origin.x - extent.x, origin.y - extent.y, origin.z - extent.z]
        record['world_bounds_max_cm'] = [origin.x + extent.x, origin.y + extent.y, origin.z + extent.z]
    except Exception:
        pass
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
        if isinstance(component, unreal.StaticMeshComponent) and component.static_mesh:
            c['mesh'] = component.static_mesh.get_path_name()
            try:
                c['num_sections_lod0'] = component.static_mesh.get_num_sections(0)
                c['num_triangles_lod0'] = component.static_mesh.get_num_triangles(0)
                c['num_vertices_lod0'] = component.static_mesh.get_num_vertices(0)
            except Exception:
                pass
        record['components'].append(c)
    return record


def mesh_record(mesh, part):
    record = {
        'path': mesh.get_path_name(),
        'part_index': part['part_index'],
        'expected_actor_location_cm': part['actor_location_ue_cm'],
        'expected_source_aabb_ue_cm': part['source_aabb_ue_cm'],
        'expected_triangle_count': part['triangle_count'],
        'sections': [],
        'materials': [],
    }
    try:
        record['nanite_stats'] = json.loads(BRIDGE.nanite_stats(mesh))
    except Exception:
        record['nanite_stats'] = {}
    try:
        record['num_sections_lod0'] = mesh.get_num_sections(0)
        record['num_triangles_lod0'] = mesh.get_num_triangles(0)
        record['num_vertices_lod0'] = mesh.get_num_vertices(0)
        for lod in range(mesh.get_num_lods()):
            record['sections'].append({
                'lod': lod,
                'num_sections': mesh.get_num_sections(lod),
                'num_triangles': mesh.get_num_triangles(lod),
            })
    except Exception as exc:
        record['mesh_stats_error'] = str(exc)
    try:
        for slot in range(record.get('num_sections_lod0', 0) or 0):
            material = mesh.get_material(slot)
            record['materials'].append(material.get_path_name() if material else None)
    except Exception as exc:
        record['material_error'] = str(exc)
    try:
        record['static_materials'] = [
            {'material': str(entry.get('material_interface')), 'slot_name': str(entry.get('material_slot_name'))}
            for entry in mesh.get_editor_property('static_materials')
        ]
    except Exception as exc:
        record['static_materials_error'] = str(exc)
    try:
        bounds = mesh.get_bounds()
        record['local_bounds_origin_cm'] = vec(bounds.origin)
        record['local_bounds_extent_cm'] = vec(bounds.box_extent)
        record['local_bounds_min_cm'] = [bounds.origin.x - bounds.box_extent.x,
                                         bounds.origin.y - bounds.box_extent.y,
                                         bounds.origin.z - bounds.box_extent.z]
        record['local_bounds_max_cm'] = [bounds.origin.x + bounds.box_extent.x,
                                         bounds.origin.y + bounds.box_extent.y,
                                         bounds.origin.z + bounds.box_extent.z]
    except Exception as exc:
        record['bounds_error'] = str(exc)
    try:
        nanite = mesh.get_editor_property('nanite_settings')
        record['nanite_settings'] = {'enabled': bool(nanite.get_editor_property('enabled'))}
    except Exception as exc:
        record['nanite_settings_error'] = str(exc)
    return record


def disable_nanite(mesh):
    """Turn Nanite off so LOD0 is the full imported geometry and mesh SDFs/cards
    are built from the full 262k-triangle mesh rather than Nanite's fallback."""
    try:
        settings = mesh.get_editor_property('nanite_settings')
        if not settings.get_editor_property('enabled'):
            return False
        settings.set_editor_property('enabled', False)
        mesh.set_editor_property('nanite_settings', settings)
        try:
            mesh.post_edit_change()
        except Exception:
            pass
        try:
            mesh.set_editor_property('distance_field_resolution_scale', 1.0)
        except Exception:
            pass
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)
        return True
    except Exception as exc:
        log(f'disable nanite note for {mesh.get_name()}: {exc}')
        return False


def make_material(name, base_color, roughness, metallic):
    path = LAB_MATERIAL_DIR + '/' + name
    existing = unreal.load_asset(path)
    if existing:
        return existing
    material = TOOLS.create_asset(name, LAB_MATERIAL_DIR, unreal.Material, unreal.MaterialFactoryNew())
    if not material:
        raise RuntimeError('could not create material ' + path)
    color = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionVectorParameter)
    color.set_editor_property('parameter_name', 'BaseColor')
    color.set_editor_property('default_value', unreal.LinearColor(*base_color, 1.0))
    if not unreal.MaterialEditingLibrary.connect_material_property(
            color, '', unreal.MaterialProperty.MP_BASE_COLOR):
        raise RuntimeError('connect base color failed: ' + name)
    rough = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionScalarParameter)
    rough.set_editor_property('parameter_name', 'Roughness')
    rough.set_editor_property('default_value', roughness)
    if not unreal.MaterialEditingLibrary.connect_material_property(
            rough, '', unreal.MaterialProperty.MP_ROUGHNESS):
        raise RuntimeError('connect roughness failed: ' + name)
    metal = unreal.MaterialEditingLibrary.create_material_expression(
        material, unreal.MaterialExpressionScalarParameter)
    metal.set_editor_property('parameter_name', 'Metallic')
    metal.set_editor_property('default_value', metallic)
    if not unreal.MaterialEditingLibrary.connect_material_property(
            metal, '', unreal.MaterialProperty.MP_METALLIC):
        raise RuntimeError('connect metallic failed: ' + name)
    unreal.MaterialEditingLibrary.recompile_material(material)
    unreal.EditorAssetLibrary.save_loaded_asset(material)
    return material


def material_instance(name, parent, base_color, roughness, metallic):
    path = LAB_MATERIAL_DIR + '/' + name
    existing = unreal.load_asset(path)
    if existing:
        return existing
    factory = unreal.MaterialInstanceConstantFactoryNew()
    instance = TOOLS.create_asset(name, LAB_MATERIAL_DIR, unreal.MaterialInstanceConstant, factory)
    if not instance:
        raise RuntimeError('could not create material instance ' + path)
    unreal.MaterialEditingLibrary.set_material_instance_parent(instance, parent)
    unreal.MaterialEditingLibrary.set_material_instance_vector_parameter_value(
        instance, 'BaseColor', unreal.LinearColor(*base_color, 1.0))
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
        instance, 'Roughness', roughness)
    unreal.MaterialEditingLibrary.set_material_instance_scalar_parameter_value(
        instance, 'Metallic', metallic)
    unreal.MaterialEditingLibrary.update_material_instance(instance)
    unreal.EditorAssetLibrary.save_loaded_asset(instance)
    return instance


def material_record(asset):
    record = {
        'path': asset.get_path_name(),
        'class': asset.get_class().get_name(),
    }
    try:
        record['parent'] = asset.get_editor_property('parent').get_path_name()
    except Exception:
        pass
    try:
        record['blend_mode'] = asset.get_editor_property('blend_mode').name
        record['two_sided'] = bool(asset.get_editor_property('two_sided'))
        record['shading_model'] = asset.get_editor_property('shading_model').name
    except Exception:
        pass
    try:
        scalars = {}
        for info in asset.get_all_scalar_parameter_info():
            try:
                scalars[info.name] = asset.get_scalar_parameter_value(info)
            except Exception:
                pass
        record['scalar_parameters'] = scalars
    except Exception:
        pass
    try:
        vectors = {}
        for info in asset.get_all_vector_parameter_info():
            try:
                value = asset.get_vector_parameter_value(info)
                vectors[info.name] = [value.r, value.g, value.b, value.a]
            except Exception:
                pass
        record['vector_parameters'] = vectors
    except Exception:
        pass
    try:
        textures = {}
        for info in asset.get_all_texture_parameter_info():
            try:
                textures[info.name] = str(asset.get_texture_parameter_value(info))
            except Exception:
                pass
        record['texture_parameters'] = textures
    except Exception:
        pass
    return record


def place_actor_bottom(actor, mesh, floor_z):
    actor.static_mesh_component.set_static_mesh(mesh)
    origin, extent = actor.get_actor_bounds(False)
    actor.set_actor_location(unreal.Vector(actor.get_actor_location().x, actor.get_actor_location().y,
                                           actor.get_actor_location().z + (floor_z - (origin.z - extent.z))),
                             False, False)


def main():
    global WORLD
    log('loading split manifest')
    split = load_json(SPLIT_MANIFEST)
    parts = split['parts']

    log('creating blank map')
    unreal.EditorLoadingAndSavingUtils.new_blank_map(False)
    WORLD = unreal.EditorLevelLibrary.get_editor_world()
    if WORLD is None:
        raise RuntimeError('no editor world after new_blank_map')
    try:
        WORLD.get_world_settings().set_editor_property('force_no_precomputed_lighting', True)
    except Exception as exc:
        log('world settings note: ' + str(exc))

    for command in (
            'r.ScreenPercentage 100', 'r.AntiAliasingMethod 2', 'r.MotionBlurQuality 0',
            'r.BloomQuality 0', 'r.DepthOfFieldQuality 0', 'r.VolumetricFog 0',
            'r.Lumen.Visualize 0', 'r.DynamicGlobalIlluminationMethod 1', 'r.ReflectionMethod 1',
            'r.Lumen.HardwareRayTracing 0', 'r.Shadow.Virtual.Enable 1',
            'r.MegaLights.EnableForProject 0', 'r.MegaLights.Allowed 0'):
        console(command)

    log('spawning 103 Sponza parts')
    mesh_records = []
    part_actors = []
    for part in parts:
        index = part['part_index']
        mesh_path = MESH_DIR + '/' + part['mesh_name']
        mesh = unreal.load_asset(mesh_path)
        if not mesh:
            raise RuntimeError('missing imported mesh: ' + mesh_path)
        disable_nanite(mesh)
        location = unreal.Vector(*part['actor_location_ue_cm'])
        actor = A.spawn_actor_from_class(unreal.StaticMeshActor, location, unreal.Rotator(0, 0, 0))
        if not actor:
            raise RuntimeError('spawn failed for ' + mesh_path)
        actor.set_actor_label(part['mesh_name'])
        actor.static_mesh_component.set_static_mesh(mesh)
        actor.set_actor_location(location, False, False)
        # The static mesh asset carries its imported material in slot 0; set it
        # explicitly as well so the saved map never depends on instance overrides.
        material_path = f'{MATERIAL_DIR}/SponzaSplit_material_{part["material_index"]}'
        material = unreal.load_asset(material_path)
        if material:
            actor.static_mesh_component.set_material(0, material)
        mesh_records.append(mesh_record(mesh, part))
        part_actors.append(actor)

    # Reconstructed world bounds of the whole building.
    world_min = [1e30] * 3
    world_max = [-1e30] * 3
    for actor in part_actors:
        origin, extent = actor.get_actor_bounds(False)
        world_min[0] = min(world_min[0], origin.x - extent.x)
        world_min[1] = min(world_min[1], origin.y - extent.y)
        world_min[2] = min(world_min[2], origin.z - extent.z)
        world_max[0] = max(world_max[0], origin.x + extent.x)
        world_max[1] = max(world_max[1], origin.y + extent.y)
        world_max[2] = max(world_max[2], origin.z + extent.z)
    log('world bounds ' + repr(world_min) + ' ' + repr(world_max))

    # ---- lighting -------------------------------------------------------
    log('spawning sun, sky atmosphere and sky light')
    sun = A.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 800),
                                   unreal.Rotator(pitch=-35.0, yaw=-45.0, roll=0.0))
    sun.set_actor_label('Sun_Sponza')
    sun_comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
    sun_comp.set_mobility(unreal.ComponentMobility.MOVABLE)
    sun_comp.set_intensity(SUN_LUX)
    sun_comp.set_editor_property('use_temperature', True)
    sun_comp.set_editor_property('temperature', 5500.0)
    sun_comp.set_editor_property('atmosphere_sun_light', True)
    sun_comp.set_editor_property('cast_shadows', True)
    sun_comp.set_editor_property('light_source_angle', 0.5357)
    sun_comp.set_editor_property('specular_scale', 1.0)

    sky_atmosphere = A.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(0, 0, 0),
                                              unreal.Rotator(0, 0, 0))
    sky_atmosphere.set_actor_label('SkyAtmosphere_Sponza')

    sky_light = A.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 400),
                                         unreal.Rotator(0, 0, 0))
    sky_light.set_actor_label('SkyLight_Sponza')
    sky_comp = sky_light.get_component_by_class(unreal.SkyLightComponent)
    sky_comp.set_mobility(unreal.ComponentMobility.MOVABLE)
    sky_comp.set_editor_property('source_type', unreal.SkyLightSourceType.SLS_CAPTURED_SCENE)
    sky_comp.set_editor_property('real_time_capture', True)
    sky_comp.set_intensity(1.0)
    try:
        sky_comp.set_editor_property('lower_hemisphere_is_black', True)
    except Exception as exc:
        log('lower_hemisphere note: ' + str(exc))
    try:
        sky_comp.set_editor_property('cubemap_resolution', 256)
    except Exception:
        pass

    # ---- cameras --------------------------------------------------------
    log('spawning fixed cameras')
    def make_camera(label, location, target):
        rotation = unreal.MathLibrary.find_look_at_rotation(unreal.Vector(*location), unreal.Vector(*target))
        camera = A.spawn_actor_from_class(unreal.CameraActor, unreal.Vector(*location), rotation)
        camera.set_actor_label(label)
        camera.camera_component.set_field_of_view(
            math.degrees(2 * math.atan(math.tan(math.radians(43 / 2)) * 16 / 9)))
        try:
            camera.camera_component.set_editor_property('constrain_aspect_ratio', False)
        except Exception as exc:
            log('camera aspect note: ' + str(exc))
        camera.set_actor_rotation(rotation, False)
        return camera, rotation

    hero_camera, hero_rotation = make_camera('CAM_Sponza', HERO_CAMERA_LOCATION, HERO_CAMERA_TARGET)
    material_camera, material_rotation = make_camera('CAM_Materials', MATERIAL_CAMERA_LOCATION,
                                                     MATERIAL_CAMERA_TARGET)

    # ---- material chart -------------------------------------------------
    log('creating test material and six spheres')
    test_material = make_material('M_TestSurface', (0.8, 0.8, 0.8), 0.3, 0.0)
    plinth_material = make_material('M_Plinth', (0.16, 0.15, 0.14), 0.85, 0.0)
    sphere_mesh = unreal.load_asset('/Engine/BasicShapes/Sphere.Sphere')
    cylinder_mesh = unreal.load_asset('/Engine/BasicShapes/Cylinder.Cylinder')
    if not sphere_mesh or not cylinder_mesh:
        raise RuntimeError('engine basic shapes missing')
    gold = (1.0, 0.766, 0.336)
    samples = [
        ('Dielectric_R008', 0.0, 0.08, (0.8, 0.8, 0.8)),
        ('Dielectric_R030', 0.0, 0.30, (0.8, 0.8, 0.8)),
        ('Dielectric_R065', 0.0, 0.65, (0.8, 0.8, 0.8)),
        ('Metal_R008', 1.0, 0.08, gold),
        ('Metal_R030', 1.0, 0.30, gold),
        ('Metal_R065', 1.0, 0.65, gold),
    ]
    sphere_records = []
    chart_actors = []
    for i, (suffix, metallic, roughness, color) in enumerate(samples):
        x = CHART_X
        y = CHART_YS[i]
        name = f'MI_Sponza_Sphere_{suffix}'
        instance = material_instance(name, test_material, color, roughness, metallic)
        plinth = A.spawn_actor_from_class(unreal.StaticMeshActor,
                                          unreal.Vector(x, y, FLOOR_Z + PLINTH_HEIGHT_CM * 0.5),
                                          unreal.Rotator(0, 0, 0))
        plinth.set_actor_label(f'Plinth_{i:02d}_{suffix}')
        plinth.static_mesh_component.set_static_mesh(cylinder_mesh)
        plinth.set_actor_scale3d(unreal.Vector(PLINTH_RADIUS_CM * 2 / 100.0,
                                               PLINTH_RADIUS_CM * 2 / 100.0,
                                               PLINTH_HEIGHT_CM / 100.0))
        plinth.static_mesh_component.set_material(0, plinth_material)
        place_actor_bottom(plinth, cylinder_mesh, FLOOR_Z)

        sphere = A.spawn_actor_from_class(unreal.StaticMeshActor,
                                          unreal.Vector(x, y, FLOOR_Z + PLINTH_HEIGHT_CM + SPHERE_RADIUS_CM),
                                          unreal.Rotator(0, 0, 0))
        sphere.set_actor_label(f'Sphere_{suffix}')
        sphere.static_mesh_component.set_static_mesh(sphere_mesh)
        sphere.set_actor_scale3d(unreal.Vector(SPHERE_RADIUS_CM * 2 / 100.0,
                                               SPHERE_RADIUS_CM * 2 / 100.0,
                                               SPHERE_RADIUS_CM * 2 / 100.0))
        sphere.static_mesh_component.set_material(0, instance)
        origin, extent = sphere.get_actor_bounds(False)
        sphere.set_actor_location(unreal.Vector(x, y, FLOOR_Z + PLINTH_HEIGHT_CM + SPHERE_RADIUS_CM),
                                  False, False)
        sphere_records.append({
            'label': sphere.get_actor_label(),
            'material_instance': instance.get_path_name(),
            'metallic': metallic,
            'roughness': roughness,
            'base_color_linear': list(color),
            'location_cm': vec(sphere.get_actor_location()),
            'scale': vec(sphere.get_actor_scale3d()),
            'world_bounds_min_cm': vec(unreal.Vector(origin.x - extent.x, origin.y - extent.y, origin.z - extent.z)),
            'world_bounds_max_cm': vec(unreal.Vector(origin.x + extent.x, origin.y + extent.y, origin.z + extent.z)),
            'plinth': plinth.get_actor_label(),
            'plinth_top_cm': FLOOR_Z + PLINTH_HEIGHT_CM,
        })
        chart_actors.extend([plinth, sphere])

    # ---- fixed exposure post-process -----------------------------------
    log('authoring fixed-exposure post-process volume')
    pp = A.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    pp.set_actor_label('PostProcess_Sponza')
    pp.set_editor_property('unbound', True)
    settings = pp.get_editor_property('settings')
    fixed = {
        'override_auto_exposure_min_brightness': True,
        'override_auto_exposure_max_brightness': True,
        'auto_exposure_min_brightness': EV100,
        'auto_exposure_max_brightness': EV100,
        'override_auto_exposure_bias': True,
        'auto_exposure_bias': 0.0,
        'override_motion_blur_amount': True,
        'motion_blur_amount': 0.0,
        'override_bloom_intensity': True,
        'bloom_intensity': 0.0,
        'override_vignette_intensity': True,
        'vignette_intensity': 0.0,
        'override_scene_fringe_intensity': True,
        'scene_fringe_intensity': 0.0,
        'override_auto_exposure_method': True,
        'auto_exposure_method': unreal.AutoExposureMethod.AEM_HISTOGRAM,
    }
    for key, value in fixed.items():
        try:
            settings.set_editor_property(key, value)
        except Exception as exc:
            log(f'postprocess property {key}: {exc}')
    pp.set_editor_property('settings', settings)
    settings = pp.get_editor_property('settings')
    pp_values = {}
    for key in ('auto_exposure_min_brightness', 'auto_exposure_max_brightness',
                'auto_exposure_bias', 'auto_exposure_method', 'motion_blur_amount',
                'bloom_intensity', 'vignette_intensity', 'scene_fringe_intensity',
                'override_auto_exposure_min_brightness', 'override_auto_exposure_max_brightness',
                'override_auto_exposure_bias'):
        try:
            value = settings.get_editor_property(key)
            pp_values[key] = value.name if hasattr(value, 'name') else value
        except Exception:
            pass

    # ---- hero camera viewport, save ------------------------------------
    ACTORS = A
    ACTORS.clear_actor_selection_set()
    unreal.EditorLevelLibrary.set_level_viewport_camera_info(hero_camera.get_actor_location(), hero_rotation)
    try:
        L.editor_set_game_view(True)
    except Exception as exc:
        log('game view note: ' + str(exc))
    try:
        L.pilot_level_actor(hero_camera)
    except Exception as exc:
        log('pilot note: ' + str(exc))

    log('saving map ' + MAP_PATH)
    if not unreal.EditorLoadingAndSavingUtils.save_map(WORLD, MAP_PATH):
        raise RuntimeError('save_map failed: ' + MAP_PATH)
    map_file = PROJECT / 'Content' / 'Labs' / 'SponzaTeaching.umap'
    if not map_file.exists():
        raise RuntimeError('saved map file missing: ' + str(map_file))

    # ---- manifest -------------------------------------------------------
    asset_files = sorted((PROJECT / 'Content' / 'Sponza').rglob('*.uasset'))
    asset_manifest = []
    for path in asset_files:
        asset_manifest.append({
            'path': str(path),
            'bytes': path.stat().st_size,
            'sha256': sha256_file(path),
        })
    material_asset_files = sorted((PROJECT / 'Content' / 'Labs' / 'Materials').glob('*.uasset'))
    lab_materials = []
    for path in material_asset_files:
        lab_materials.append({
            'path': str(path),
            'bytes': path.stat().st_size,
            'sha256': sha256_file(path),
        })

    build_version = None
    build_version_path = Path(r'D:\UnrealEngine\Engine\Build\Build.version')
    if build_version_path.exists():
        build_version = load_json(build_version_path)
    modules_path = PROJECT / 'Binaries' / 'Win64' / 'UnrealEditor.modules'
    modules = load_json(modules_path) if modules_path.exists() else None
    source_manifest = load_json(SOURCE_MANIFEST) if SOURCE_MANIFEST.exists() else None
    import_run = None
    import_run_path = LOGS / 'import-run.json'
    if import_run_path.exists():
        import_run = load_json(import_run_path)

    cvar_names = [
        'r.DynamicGlobalIlluminationMethod', 'r.ReflectionMethod', 'r.Lumen.HardwareRayTracing',
        'r.Shadow.Virtual.Enable', 'r.GenerateMeshDistanceFields', 'r.RayTracing',
        'r.Substrate', 'r.MegaLights.EnableForProject', 'r.MegaLights.Allowed',
        'r.AllowStaticLighting', 'r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange',
        'r.MotionBlurQuality', 'r.BloomQuality', 'r.DepthOfFieldQuality', 'r.VolumetricFog',
        'r.ScreenPercentage', 'r.AntiAliasingMethod', 'r.LumenScene.SurfaceCache.AtlasSize',
        'r.Lumen.Visualize', 'r.SkyLight.RealTimeReflectionCapture',
    ]
    cvars = {name: cvar(name) for name in cvar_names}

    def camera_record(camera, target):
        return {
            'actor': camera.get_actor_label(),
            'location_cm': vec(camera.get_actor_location()),
            'rotation_degrees': rot(camera.get_actor_rotation()),
            'target_cm': list(target),
            'vertical_fov_degrees': 43.0,
            'component_horizontal_fov_degrees': float(camera.camera_component.field_of_view),
            'aspect_ratio': '16:9',
            'post_process_blend_weight': float(camera.camera_component.post_process_blend_weight),
        }

    manifest = {
        'schema': 'arda-sponza-scene-manifest-v1',
        'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'engine': {
            'version': unreal.SystemLibrary.get_engine_version(),
            'build_version': build_version,
            'editor_exe': r'D:\UnrealEngine\Engine\Binaries\Win64\UnrealEditor.exe',
            'editor_exe_sha256': sha256_file(Path(r'D:\UnrealEngine\Engine\Binaries\Win64\UnrealEditor.exe')),
            'cmd_exe': r'D:\UnrealEngine\Engine\Binaries\Win64\UnrealEditor-Cmd.exe',
            'cmd_exe_sha256': sha256_file(Path(r'D:\UnrealEngine\Engine\Binaries\Win64\UnrealEditor-Cmd.exe')),
        },
        'project': {
            'root': str(PROJECT),
            'uproject': str(PROJECT / 'UnrealLab.uproject'),
            'name': 'UnrealLab',
            'modules': modules,
            'build_id': modules.get('BuildId') if modules else None,
            'config_sha256': sha256_file(PROJECT / 'Config' / 'DefaultEngine.ini'),
        },
        'source': {
            'download_manifest': str(SOURCE_MANIFEST),
            'upstream_revision': source_manifest.get('upstream_revision') if source_manifest else None,
            'upstream_page': source_manifest.get('upstream_page') if source_manifest else None,
            'gltf_sha256': source_manifest.get('gltf_sha256') if source_manifest else None,
            'license_path': str(WORKSPACE / 'source' / 'Sponza' / 'attribution' / 'LICENSE.md'),
            'license_sha256': sha256_file(WORKSPACE / 'source' / 'Sponza' / 'attribution' / 'LICENSE.md'),
            'readme_path': str(WORKSPACE / 'source' / 'Sponza' / 'attribution' / 'README.md'),
            'metadata_path': str(WORKSPACE / 'source' / 'Sponza' / 'attribution' / 'metadata.json'),
            'derived_gltf': str(WORKSPACE / 'source' / 'Sponza' / 'glTF' / 'SponzaSplit.gltf'),
            'derived_gltf_sha256': sha256_file(WORKSPACE / 'source' / 'Sponza' / 'glTF' / 'SponzaSplit.gltf'),
            'split_manifest': str(SPLIT_MANIFEST),
            'split_manifest_sha256': sha256_file(SPLIT_MANIFEST),
            'import_run': import_run,
        },
        'map': {
            'package': MAP_PATH,
            'file': str(map_file),
            'bytes': map_file.stat().st_size,
            'sha256': sha256_file(map_file),
        },
        'transformation': split['transformation'],
        'measured': {
            'source_scene_bounds_ue_cm': split['source_scene_bounds_ue_cm'],
            'reconstructed_world_bounds_min_cm': world_min,
            'reconstructed_world_bounds_max_cm': world_max,
            'reconstructed_footprint_cm': [world_max[0] - world_min[0], world_max[1] - world_min[1]],
            'reconstructed_height_cm': world_max[2] - world_min[2],
            'floor_z_cm': FLOOR_Z,
            'part_count': len(part_actors),
            'total_triangles_imported': sum(r.get('num_triangles_lod0', 0) for r in mesh_records),
            'mesh_asset_count': len(mesh_records),
            'material_instance_count': len(list((PROJECT / 'Content' / 'Sponza' / 'SponzaSplit' / 'Materials').glob('*.uasset'))),
            'texture_asset_count': len(list((PROJECT / 'Content' / 'Sponza' / 'SponzaSplit' / 'Textures').glob('*.uasset'))),
        },
        'cameras': {
            'hero': camera_record(hero_camera, HERO_CAMERA_TARGET),
            'materials': camera_record(material_camera, MATERIAL_CAMERA_TARGET),
            'camera_clearance_offline': load_json(WORKSPACE / 'source' / 'Sponza' / 'derived' / 'geometry-analysis.json').get('camera_clearance', {})
            if (WORKSPACE / 'source' / 'Sponza' / 'derived' / 'geometry-analysis.json').exists() else {},
        },
        'exposure': {
            'method': 'AEM_Histogram pinned to EV100 with min_brightness == max_brightness (no adaptation)',
            'ev100': EV100,
            'bias': 0.0,
            'postprocess_values': pp_values,
            'note': 'Fixed for every capture; no auto-exposure drift.',
        },
        'lighting': {
            'sun': {
                'actor': sun.get_actor_label(),
                'intensity_lux': SUN_LUX,
                'temperature_k': 5500.0,
                'rotation_degrees': rot(sun.get_actor_rotation()),
                'atmosphere_sun_light': bool(sun_comp.get_editor_property('atmosphere_sun_light')),
                'cast_shadows': bool(sun_comp.get_editor_property('cast_shadows')),
                'light_source_angle_degrees': float(sun_comp.get_editor_property('light_source_angle')),
            },
            'sky_atmosphere': {'actor': sky_atmosphere.get_actor_label()},
            'sky_light': {
                'actor': sky_light.get_actor_label(),
                'real_time_capture': bool(sky_comp.get_editor_property('real_time_capture')),
                'source_type': sky_comp.get_editor_property('source_type').name,
                'intensity': 1.0,
            },
            'other_lights': [a.get_actor_label() for a in A.get_all_level_actors()
                             if isinstance(a, (unreal.PointLight, unreal.SpotLight, unreal.RectLight))],
            'fog_actors': [a.get_actor_label() for a in A.get_all_level_actors()
                           if isinstance(a, unreal.ExponentialHeightFog)],
        },
        'material_chart': {
            'test_material': test_material.get_path_name(),
            'plinth_material': plinth_material.get_path_name(),
            'ys_cm': list(CHART_YS),
            'x_cm': CHART_X,
            'center_cm': list(CHART_CENTER),
            'sphere_radius_cm': SPHERE_RADIUS_CM,
            'plinth_height_cm': PLINTH_HEIGHT_CM,
            'samples': sphere_records,
        },
        'assets': {
            'imported_sponza': asset_manifest,
            'lab_materials': lab_materials,
            'material_records': [material_record(unreal.load_asset(f'/Game/Sponza/SponzaSplit/Materials/SponzaSplit_material_{i}'))
                                 for i in range(25)],
        },
        'meshes': mesh_records,
        'actors': [actor_record(a) for a in A.get_all_level_actors()],
        'cvars': cvars,
        'commands': COMMANDS,
        'notes': [
            'Sponza has no authored lights; the sun, sky atmosphere and sky light are additions for this teaching scene.',
            'Spheres are engine BasicShapes spheres with a test Default Lit material and MaterialInstanceConstant overrides.',
            'The raw Khronos asset is kept only under source/ and is not redistributed; see attribution/LICENSE.md.',
        ],
    }
    atomic_json(OUTPUT / 'scene-manifest.json', manifest)
    log('manifest written')
    return 0


try:
    RESULT = main()
    log('authoring complete')
except Exception:
    message = traceback.format_exc()
    (LOGS / 'author-error.txt').write_text(message, encoding='utf-8')
    unreal.log_error('ARDA_SPONZA_AUTHOR_ERROR ' + message)
    raise
