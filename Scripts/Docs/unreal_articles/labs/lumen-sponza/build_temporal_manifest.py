"""Build the course's discrete observation controls from published evidence.

This is presentation metadata, not a renderer or a new simulation. All images
and coefficient records are produced separately from the native captures.
"""
from pathlib import Path
import json
import re
import argparse

ROOT = Path(__file__).resolve().parents[5]
TEMPORAL = ROOT / 'Docs/Unreal/assets/lumen-sponza/temporal'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def screen_experiment(folder, identity, label, updates, camera_motion=False):
    path = TEMPORAL / folder
    metadata = read(path / 'decode-manifest.json')
    summary = read(path / 'publish/data/summary.json')
    scale = metadata['scales']['lobe_response_scale_luminance']
    states = []
    for entry in metadata['states']:
        name = entry['name']
        state = read(path / f'publish/data/states/{name}.json')
        counts, view = state['counts'], state['view']
        update = updates[name]
        state_label = 'Warm baseline' if update is None else f'Actual update {update}'
        frame = re.search(r'Frame (\d+)', summary['frame_scopes'][name]).group(1)
        record = f'{folder}/publish/data/states/{name}.json'
        facts = {
            'Recorded engine frame': frame,
            'View frame / state index': f"{view['frame_number']} / {view['state_frame_index']}",
            'Change': 'Camera translated 150 cm' if camera_motion and update is not None else
                      ('Sun illuminance 20,000 lux' if update is not None else 'Sun illuminance 80,000 lux'),
            'Exposure': f"EV100 11; captured pre-exposure {view['preexposure']}",
            'Camera world position (cm)': ', '.join(map(str, view['world_camera_origin_cm'])),
            'Active probes': f"{counts['active_total_count']} = {counts['active_uniform_count']} uniform + {counts['active_adaptive_count']} adaptive",
            'Excluded stale adaptive entries': counts['stale_finite_adaptive_count'],
            'Uniform tile jitter (pixels)': ', '.join(map(str, counts['screen_tile_jitter_px'])),
            'Timing': 'Discrete captured updates. Playback spacing does not represent elapsed rendering time.',
        }
        layers = {
            'screen-probes': {
                'image': f'{folder}/publish/figures/active_{name}.png',
                'alt': f'Active screen probes projected onto the actual Sponza scene at {state_label.lower()}; cyan uniform samples and orange adaptive samples.',
                'caption': f"{counts['active_total_count']:,} active probes, reconstructed from their captured translated-world positions. The native screenshot and the captured camera provide the scene context. Finite entries outside the active allocation are excluded.",
                'record': record,
            },
            'screen-sh': {
                'image': f'{folder}/publish/figures/lobes_{name}.png',
                'alt': f'Ten three-dimensional diffuse-response lobes reconstructed from captured screen-probe SH at {state_label.lower()}, anchored to floor and arcade samples in Sponza.',
                'caption': f"Ten captured samples. Radius = 60 cm × clamp(response luminance / {scale}, 0, 1); the same scale applies to every observation in this experiment. Magenta outlines expose depth-occluded parts of these function graphs. Green and pink center marks identify floor and arcade samples.",
                'facts': {
                    'SH data': '27 signed coefficients per active probe: R0…R8, G0…G8, B0…B8',
                    'Evaluated quantity': 'View-pre-exposed unit-albedo diffuse response D(n); AO=1',
                    'Display range': f'0 to {scale} response luminance; negative values clipped only for display',
                    'Correspondence': 'Nearest world-position samples with recorded distance and normal compatibility; atlas index is not physical identity',
                },
                'record': f'{folder}/publish/data/focused_probes.json',
            },
        }
        probe_facts = {key: facts.pop(key) for key in (
            'Active probes', 'Excluded stale adaptive entries', 'Uniform tile jitter (pixels)')}
        layers['screen-probes']['facts'] = probe_facts
        layers['screen-sh']['facts'] = {**probe_facts, **layers['screen-sh']['facts']}
        states.append({'id': name, 'label': state_label, 'facts': facts, 'record': record, 'layers': layers})
    return {'id': identity, 'label': label, 'states': states}


def add_placements(experiment, sequence):
    folder = f'placements/{sequence}'
    for state in experiment['states']:
        name = state['id']
        data = TEMPORAL / folder / 'data' / name
        cards = read(data / 'cards.json')
        cache = read(data / 'worldcache.json')
        cc, wc = cards['counts'], cache['counts']
        for layer, filename, record, caption, facts in [
            ('cards', 'cards-only.png', 'cards.json',
             'Projected bounds of captured cards. Yellow emphasizes a spatially distributed subset; the complete card and page records remain available. Bounds describe a capture domain, not an extra polygonal surface.',
             {'Valid cards': cc['valid_cards'], 'Included / resident cards': cc['page_resident'],
              'Referenced low-resolution pages': cc['total_low_res_pages'],
              'Highlighted cards': cc['representative_subset'],
              'Visibility rule': 'Card-center depth test; this does not clip every outline edge against scene geometry'}),
            ('world-cache', 'worldcache-only.png', 'worldcache.json',
             'Main world-cache probe centers projected into the native scene image. All active cells in this capture belong to the finest clipmap, shown in blue. A point marks a stored sampling location, not a light source.',
             {'Active world probes': wc['active_by_indirection'],
              'Allocator high-water / free slots': f"{wc['allocator_high_water']} / {wc['free_count']}",
              'On screen / passing depth test': f"{wc['on_screen']} / {wc['visible']}",
              'Probe offset': 'Captured sub-cell offsets are zero in these states',
              'Identity': 'Clipmap level + world cell; physical slot is tracked separately'}),
        ]:
            state['layers'][layer] = {
                'image': f'{folder}/figures/{name}/{filename}',
                'alt': f"Captured Sponza {layer} placement, {state['label'].lower()}",
                'caption': caption, 'facts': facts, 'record': f'{folder}/data/{name}/{record}',
            }
        histogram = cache['activity']['0']['traced_age_histogram']
        state['layers']['cards']['downloads'] = [
            {'label': 'Page mappings', 'path': f'{folder}/data/{name}/pages.json'},
            {'label': 'Hidden card bounds', 'path': f'{folder}/figures/{name}/cards-xray.png'}]
        state['layers']['world-cache']['downloads'] = [
            {'label': 'Temporal correspondence', 'path': f'{folder}/data/sequence.json'},
            {'label': 'Hidden probes', 'path': f'{folder}/figures/{name}/worldcache-xray.png'}]
        state['layers']['world-cache']['chart'] = {
            'title': 'Age since last trace, in captured view-state updates',
            'maximum': 1000,
            'unit': 'active probes',
            'values': [{'label': label.replace('_plus', '+').replace('_', '–'), 'value': value}
                       for label, value in histogram.items()],
        }


def radiosity_experiment():
    folder = 'radiosity/publish'
    names = [('baseline', 'Warm baseline', 124, '80,000 lux; original view'),
             ('light-001', 'Light change · actual update 1', 128, '20,000 lux; original view'),
             ('light-064', 'Light change · actual update 63', 192, '20,000 lux; original view'),
             ('camera-001', 'Camera change · actual update 1', 262, '80,000 lux restored and warmed; camera translated 150 cm'),
             ('camera-064', 'Camera change · actual update 63', 326, '80,000 lux; translated view')]
    states = []
    for name, label, frame, change in names:
        record = f'{folder}/data/probes-{name}.json'
        data = read(TEMPORAL / record)
        counts, view = data['counts'], data['view']
        states.append({'id': name, 'label': label, 'record': record, 'facts': {
            'Recorded engine frame': frame, 'View state index': view['frame_index_field'],
            'Change': change, 'Camera world position (cm)': ', '.join(map(str, view['camera_world_cm'])),
            'Surface samples retained': counts['probes'], 'Nonzero SH samples': counts['sh_nonzero'],
            'Scene-depth visible / occluded samples': f"{counts['visible']} / {counts['occluded']}",
            'Cache pre-exposure': data['preexposure'],
            'Storage': '12 signed half-float coefficients per sample; all decoded arrays in the NPZ record',
            'Timing': 'Separate capture sequence; these observations are not simultaneous with the screen-probe sequence',
        }, 'layers': {'radiosity': {
            'image': f'{folder}/figures/placement-{name}.png',
            'alt': f'Actual Sponza radiosity probes reconstructed from card surface depth and normals, {label.lower()}; plan and elevation show the complete retained point set.',
            'caption': 'Left: samples projected onto the native scene and tested against primary depth. Right, top: XY plan; bottom: XZ elevation. The two orthographic panels use fixed world bounds; the white marker locates the camera. Colors classify the reconstructed surface normal.',
            'record': record,
            'downloads': [
                {'label': 'Complete numerical arrays (NPZ)', 'path': f'{folder}/data/probes-{name}.npz'},
                {'label': 'X-ray placement', 'path': f'{folder}/figures/placement-xray-{name}.png'},
                {'label': 'SH response functions', 'path': f'{folder}/figures/sh-lobes-{name}.png'},
                {'label': 'Figure scales and interpretation', 'path': f'{folder}/data/figure-legend.json'}],
        }}})
    return {'id': 'radiosity-buffers', 'label': 'Surface radiosity · light and camera changes', 'states': states}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    target = TEMPORAL / 'manifest.json'
    manifest = read(target)
    additions = [
        {'id': 'screen-probes', 'legend': 'Cyan: active uniform screen probes. Orange with white rings: active adaptive probes. Points are projected from captured 3D positions; they are not extra scene lights.'},
        {'id': 'screen-sh', 'legend': 'Blue → cyan → green → yellow → white: increasing response. Filled lobe faces pass the scene-depth test; magenta wire outlines show hidden function geometry. Radius and colors keep one scale within each experiment. Signed coefficients remain in the data.'},
        {'id': 'cards', 'legend': 'Yellow: a representative subset of captured card bounds. The card-center depth test uses a tolerance of max(10 cm, 2% of scene depth); outlines can extend behind foreground geometry. Complete geometry and page arrays are retained separately.'},
        {'id': 'world-cache', 'legend': 'Blue: clipmap 0, cell spacing about 104.17 cm. The other three clipmaps have no active cells in these captures. Marker color and size stay fixed; the age chart shows update activity even when positions do not move.'},
        {'id': 'radiosity', 'legend': 'Green: up-facing normals. Orange: down-facing normals. Blue: X-dominant wall normals. Pink: Y-dominant wall normals. All positions come from captured card depth; color is orientation, not illumination.'},
    ]
    ids = {layer['id'] for layer in additions}
    manifest['layers'] = [x for x in manifest['layers'] if x['id'] not in ids] + additions
    experiments = [
        screen_experiment('screen-sh', 'light-buffers', 'Fixed-camera light step · captured buffers',
                          {'baseline': None, 'lightstep-001': 1, 'lightstep-008': 7, 'lightstep-032': 31, 'lightstep-064': 63}),
        screen_experiment('motion-screen-sh', 'camera-buffers', 'Camera motion · captured buffers',
                          {'baseline': None, 'camera-001': 1, 'camera-064': 63}, True),
    ]
    add_placements(experiments[0], 'light')
    add_placements(experiments[1], 'motion')
    experiments.append(radiosity_experiment())
    old = {experiment['id']: experiment for experiment in manifest['experiments']}
    for experiment in experiments:
        if experiment['id'] in old:
            # Retain independently decoded card/cache layers when rebuilding.
            prior = {state['id']: state for state in old[experiment['id']]['states']}
            for state in experiment['states']:
                if state['id'] in prior:
                    state['layers'] = {**prior[state['id']]['layers'], **state['layers']}
        old[experiment['id']] = experiment
    manifest['experiments'] = list(old.values())
    content = json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'
    if args.check:
        assert target.read_text(encoding='utf-8') == content, 'Stale temporal presentation manifest'
    else:
        target.write_text(content, encoding='utf-8')
    print(f'{"Verified" if args.check else "Built"} {target.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
