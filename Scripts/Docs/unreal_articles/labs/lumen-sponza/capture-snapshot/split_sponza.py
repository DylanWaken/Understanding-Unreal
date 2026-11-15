"""Build a derived Sponza glTF with one mesh per source primitive.

The original Sponza.gltf has a single mesh containing 103 primitives and a root
node with uniform scale 0.008.  Interchange bakes node transforms into static
mesh vertices, so importing that file yields one large static mesh with 103
sections: poor mesh-SDF/card coverage for Lumen.  This script keeps the source
files untouched and writes a derived glTF next to them (same folder, so all
image/buffer URIs resolve unchanged) in which:

  * every primitive becomes its own glTF mesh with a single primitive;
  * positions are recentered on that primitive's source AABB centre, and a new
    POSITION accessor + buffer is written for it;
  * indices, normals, tangents, UVs and material indices are reused verbatim;
  * each new mesh is referenced by one top-level node with identity transform.

Interchange converts glTF (x, y, z) to UE (x, z, y) and multiplies by 100 to
centimetres; the baked node scale 0.008 yields 0.8.  The recentering is undone
at spawn time by placing each actor at 0.8 * (pivot.x, pivot.z, pivot.y) cm.
The derived file therefore reproduces the same world-space geometry, with a
tight pivot per object.

Outputs (under source/Sponza):
  glTF/SponzaSplit.gltf
  glTF/SponzaSplit_positions.bin
  derived/split-manifest.json
"""
import hashlib
import json
import math
import os
import struct
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / 'source' / 'Sponza'
GLTF_DIR = SOURCE / 'glTF'
DERIVED_DIR = SOURCE / 'derived'
ORIGINAL = GLTF_DIR / 'Sponza.gltf'
SPLIT = GLTF_DIR / 'SponzaSplit.gltf'
POSITIONS = GLTF_DIR / 'SponzaSplit_positions.bin'
NODE_SCALE = 0.008
UE_PER_GLTF_UNIT = 100.0 * NODE_SCALE  # Interchange metre->cm times the root scale
COMPONENT_FLOAT = 5126


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def read_positions(document, raw_buffer, accessor_index):
    accessor = document['accessors'][accessor_index]
    if accessor['type'] != 'VEC3' or accessor['componentType'] != COMPONENT_FLOAT:
        raise RuntimeError(f'position accessor {accessor_index} is not float VEC3')
    view = document['bufferViews'][accessor['bufferView']]
    if view.get('buffer') != 0:
        raise RuntimeError(f'position accessor {accessor_index} does not use buffer 0')
    stride = view.get('byteStride', 12)
    base = view.get('byteOffset', 0) + accessor.get('byteOffset', 0)
    count = accessor['count']
    out = []
    for i in range(count):
        offset = base + i * stride
        out.append(struct.unpack_from('<3f', raw_buffer, offset))
    if accessor.get('min') and accessor.get('max'):
        actual_min = [min(p[a] for p in out) for a in range(3)]
        actual_max = [max(p[a] for p in out) for a in range(3)]
        for a in range(3):
            if abs(actual_min[a] - accessor['min'][a]) > 1e-3 or abs(actual_max[a] - accessor['max'][a]) > 1e-3:
                raise RuntimeError(f'accessor {accessor_index} min/max does not match its data')
    return out


def main():
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    document = json.loads(ORIGINAL.read_text(encoding='utf-8'))
    bin_uri = document['buffers'][0]['uri']
    raw_buffer = (GLTF_DIR / bin_uri).read_bytes()
    if len(raw_buffer) != document['buffers'][0]['byteLength']:
        raise RuntimeError('source buffer length mismatch')
    source_mesh = document['meshes'][0]
    if len(document['meshes']) != 1:
        raise RuntimeError('expected exactly one source mesh')
    if len(source_mesh['primitives']) != 103:
        raise RuntimeError(f'expected 103 primitives, found {len(source_mesh["primitives"])}')

    position_blob = bytearray()
    new_accessors = []
    new_buffer_views = []
    new_meshes = []
    new_nodes = []
    parts = []
    source_offsets = {}

    # Buffer 0 is the new position data; the untouched source binary becomes buffer 1.
    buffer_views = []
    accessors = []
    for index, primitive in enumerate(source_mesh['primitives']):
        attrs = primitive['attributes']
        pos_accessor_index = attrs['POSITION']
        positions = read_positions(document, raw_buffer, pos_accessor_index)
        source_accessor = document['accessors'][pos_accessor_index]
        source_min = list(source_accessor['min'])
        source_max = list(source_accessor['max'])
        pivot = [(source_min[a] + source_max[a]) * 0.5 for a in range(3)]

        blob_start = len(position_blob)
        local_min = [math.inf] * 3
        local_max = [-math.inf] * 3
        for position in positions:
            local = tuple(position[a] - pivot[a] for a in range(3))
            position_blob.extend(struct.pack('<3f', *local))
            for a in range(3):
                local_min[a] = min(local_min[a], local[a])
                local_max[a] = max(local_max[a], local[a])
        # glTF accessor bounds must be float-representable; use the exact recentered AABB.
        if blob_start % 4:
            raise RuntimeError('unaligned position blob')
        buffer_view_index = len(buffer_views)
        buffer_views.append({
            'buffer': 0,
            'byteOffset': blob_start,
            'byteLength': len(positions) * 12,
            'target': 34962,
        })
        accessor_index = len(accessors)
        accessors.append({
            'bufferView': buffer_view_index,
            'componentType': COMPONENT_FLOAT,
            'count': len(positions),
            'type': 'VEC3',
            'min': local_min,
            'max': local_max,
        })

        attributes = dict(attrs)
        attributes['POSITION'] = accessor_index
        for key in list(attributes):
            if key != 'POSITION':
                attributes[key] = attributes[key] + len(source_mesh['primitives'])
        new_primitive = dict(primitive)
        new_primitive['attributes'] = attributes
        new_primitive['indices'] = (primitive['indices'] + len(source_mesh['primitives'])
                                    if primitive.get('indices') is not None else None)
        mesh_index = len(new_meshes)
        new_meshes.append({
            'name': f'Sponza_{index:03d}',
            'primitives': [new_primitive],
        })
        node_index = len(new_nodes)
        new_nodes.append({
            'name': f'Sponza_{index:03d}',
            'mesh': mesh_index,
            # Preserve the source root's 0.008 uniform scale on every node.  The
            # position data was recentered, so the baked local geometry becomes
            # 100 * 0.008 * (source - pivot) cm; the actor re-adds 0.8*pivot.
            'scale': [NODE_SCALE, NODE_SCALE, NODE_SCALE],
        })
        ue_pivot = [pivot[0] * UE_PER_GLTF_UNIT, pivot[2] * UE_PER_GLTF_UNIT, pivot[1] * UE_PER_GLTF_UNIT]
        ue_source_min = [source_min[0] * UE_PER_GLTF_UNIT, source_min[2] * UE_PER_GLTF_UNIT, source_min[1] * UE_PER_GLTF_UNIT]
        ue_source_max = [source_max[0] * UE_PER_GLTF_UNIT, source_max[2] * UE_PER_GLTF_UNIT, source_max[1] * UE_PER_GLTF_UNIT]
        parts.append({
            'part_index': index,
            'mesh_name': f'Sponza_{index:03d}',
            'node_name': f'Sponza_{index:03d}',
            'material_index': primitive.get('material'),
            'attribute_accessors': attributes,
            'primitive_attributes': primitive['attributes'],
            'indices_accessor': primitive.get('indices'),
            'vertex_count': len(positions),
            'triangle_count': (document['accessors'][primitive['indices']]['count'] // 3) if primitive.get('indices') is not None else len(positions) // 3,
            'source_aabb_gltf': {'min': source_min, 'max': source_max, 'pivot': pivot},
            'source_aabb_ue_cm': {'min': ue_source_min, 'max': ue_source_max, 'pivot': ue_pivot,
                                  'mapping': 'UE = (0.8*gltf.x, 0.8*gltf.z, 0.8*gltf.y)'},
            'local_aabb_gltf_after_recenter': {'min': local_min, 'max': local_max},
            'actor_location_ue_cm': ue_pivot,
        })

    # Source buffer becomes buffer 1; accessor/bufferView indices are unchanged,
    # but their buffer index must be remapped from 0 to 1.
    source_buffer_views = []
    for view in document['bufferViews']:
        view = dict(view)
        if view.get('buffer') != 0:
            raise RuntimeError('unexpected source buffer index')
        view['buffer'] = 1
        source_buffer_views.append(view)
    source_accessors = []
    for accessor in document['accessors']:
        accessor = dict(accessor)
        if 'bufferView' in accessor:
            accessor['bufferView'] = accessor['bufferView'] + len(buffer_views)
        source_accessors.append(accessor)
    all_accessors = accessors + source_accessors
    all_buffer_views = buffer_views + source_buffer_views

    for accessor in all_accessors:
        if 'bufferView' in accessor and accessor['bufferView'] >= len(all_buffer_views):
            raise RuntimeError('bad accessor remap')

    # Images, textures, samplers, materials are reused unchanged.
    derived = {
        'asset': {
            'version': '2.0',
            'generator': 'arda-sponza-split/1.0 (derived from Khronos Sponza.gltf)',
            'extras': {
                'source': 'KhronosGroup/glTF-Sample-Assets Models/Sponza/glTF/Sponza.gltf',
                'source_sha256': sha256_file(ORIGINAL),
                'note': 'One mesh per source primitive; positions recentered per primitive; '
                        'indices/normals/tangents/UVs/materials/images reused; identity node transforms. '
                        'Actor placement must add 0.8*(pivot.x,pivot.z,pivot.y) cm.',
            },
        },
        'scene': 0,
        'scenes': [{'name': 'SponzaSplit', 'nodes': list(range(len(new_nodes)))}],
        'nodes': new_nodes,
        'meshes': new_meshes,
        'materials': document['materials'],
        'textures': document['textures'],
        'samplers': document.get('samplers', []),
        'images': document['images'],
        'accessors': all_accessors,
        'bufferViews': all_buffer_views,
        'buffers': [
            {'uri': POSITIONS.name, 'byteLength': len(position_blob)},
            {'uri': bin_uri, 'byteLength': len(raw_buffer)},
        ],
    }
    if derived['samplers'] == [] and 'samplers' not in document:
        del derived['samplers']

    POSITIONS.write_bytes(bytes(position_blob))
    SPLIT.write_text(json.dumps(derived, indent=1), encoding='utf-8')

    ue_min = [min(p['source_aabb_ue_cm']['min'][a] for p in parts) for a in range(3)]
    ue_max = [max(p['source_aabb_ue_cm']['max'][a] for p in parts) for a in range(3)]
    manifest = {
        'schema': 'arda-sponza-split-manifest-v1',
        'generated_utc': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime()),
        'original_gltf': str(ORIGINAL),
        'original_gltf_sha256': sha256_file(ORIGINAL),
        'derived_gltf': str(SPLIT),
        'derived_gltf_sha256': sha256_file(SPLIT),
        'derived_positions_bin': str(POSITIONS),
        'derived_positions_bin_bytes': POSITIONS.stat().st_size,
        'derived_positions_bin_sha256': sha256_file(POSITIONS),
        'source_bin': str(GLTF_DIR / bin_uri),
        'source_bin_sha256': sha256_file(GLTF_DIR / bin_uri),
        'transformation': {
            'source_node_scale': NODE_SCALE,
            'interchange_unit_scale': 100.0,
            'combined_units_per_gltf_unit': UE_PER_GLTF_UNIT,
            'gltf_to_ue_axes': 'UE = (gltf.x, gltf.z, gltf.y)',
            'recenter': 'per-primitive source AABB centre subtracted in glTF space',
            'actor_location_rule': 'UE cm = 0.8 * (pivot.x, pivot.z, pivot.y)',
        },
        'source_scene_bounds_gltf': {'min': [min(p['source_aabb_gltf']['min'][a] for p in parts) for a in range(3)],
                                     'max': [max(p['source_aabb_gltf']['max'][a] for p in parts) for a in range(3)]},
        'source_scene_bounds_ue_cm': {'min': ue_min, 'max': ue_max,
                                      'footprint_cm': [ue_max[0] - ue_min[0], ue_max[1] - ue_min[1]],
                                      'height_cm': ue_max[2] - ue_min[2]},
        'part_count': len(parts),
        'total_triangles': sum(p['triangle_count'] for p in parts),
        'total_vertices': sum(p['vertex_count'] for p in parts),
        'parts': parts,
        'materials': [{'index': i, 'alphaMode': m.get('alphaMode', 'OPAQUE'),
                       'doubleSided': bool(m.get('doubleSided', False)),
                       'name': m.get('name')} for i, m in enumerate(document['materials'])],
    }
    (DERIVED_DIR / 'split-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'wrote {SPLIT} ({SPLIT.stat().st_size} bytes)')
    print(f'wrote {POSITIONS} ({POSITIONS.stat().st_size} bytes)')
    print(f'parts={len(parts)} triangles={manifest["total_triangles"]} vertices={manifest["total_vertices"]}')
    print('UE bounds cm', ue_min, ue_max)
    print('footprint cm', manifest['source_scene_bounds_ue_cm']['footprint_cm'],
          'height cm', manifest['source_scene_bounds_ue_cm']['height_cm'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
