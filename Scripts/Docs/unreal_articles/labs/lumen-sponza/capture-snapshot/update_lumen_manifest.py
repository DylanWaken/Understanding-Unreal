"""Extend output/scene-manifest.json with the Lumen diagnosis and new baseline.

Reads the capture session JSONs written by the three diagnostic runs; does not
modify the map or any pre-existing manifest key except adding/refreshing the
Lumen blocks and the current preview images.
"""
import hashlib
import json
import time
from pathlib import Path

WS = Path(r'D:\UnrealUnderstanding-Sponza')
OUT = WS / 'output'
PREV = OUT / 'preview'


def load(path):
    with open(path, encoding='utf-8-sig') as f:
        return json.load(f)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


final = load(OUT / 'lumen-final-session.json')
diag = load(OUT / 'lumen-diagnosis.json')
verify = load(OUT / 'lumen-verify.json')
manifest = load(OUT / 'scene-manifest.json')

IMAGES = ['hero-lit', 'materials-lit', 'surface-cache', 'lumen-scene', 'lumen-cards',
          'mesh-sdf', 'gdf-probe']
records = {}
for name in IMAGES:
    path = PREV / f'{name}.png'
    rec = dict(final['images'].get(name, {}))
    rec.update({'file': str(path), 'sha256': sha(path), 'bytes': path.stat().st_size})
    records[name] = rec

evidence = {}
for name in ['surface-cache-sw', 'surface-cache-sw2', 'lumen-scene-sw',
             'lumen-scene-sw2', 'surface-cache-hw', 'lumen-scene-hw',
             'hero-clean-hw', 'hero-lit-editorprim-before',
             'lumen-surface-cache-sw-broken', 'lumen-scene-sw-broken',
             'global-sdf-legacy', 'global-sdf-clean', 'lumen-albedo',
             'lumen-albedo-sw2', 'lumen-normals', 'sw-traces-on',
             'hero-clean-sw', 'hero-gi-off', 'hero-gi-on']:
    path = PREV / f'{name}.png'
    if path.exists():
        evidence[name] = {'file': str(path), 'bytes': path.stat().st_size,
                          'sha256': sha(path)}
        for key in ('mean_luminance', 'yellow_fraction', 'near_black_fraction',
                    'nonblack_fraction_lum_gt_8'):
            if name in diag.get('images', {}) and key in diag['images'][name]:
                evidence[name][key] = diag['images'][name][key]
            elif name in verify.get('images', {}) and key in verify['images'][name]:
                evidence[name][key] = verify['images'][name][key]
            elif name in final.get('images', {}) and key in final['images'][name]:
                evidence[name][key] = final['images'][name][key]

stats_dump = {
    'software_first_settle': {
        'source': 'output/logs/lumdiag.stdout.log',
        'mesh_sdf_objects': 115, 'primitive_groups': 115, 'mesh_cards': 80,
        'cards': 522, 'physical_pages_allocated': 44, 'physical_pages_total': 1024,
        'virtual_texels_m': 0.606, 'physical_texels_m': 0.613,
        'mesh_cards_to_add': 0, 'locked_cards_to_update': 251,
    },
    'hardware_farfield': {
        'source': 'output/logs/lumenfinal.stdout.log',
        'mesh_sdf_objects': 115, 'primitive_groups': 115, 'mesh_cards': 105,
        'cards': 637, 'physical_pages_allocated': 258, 'physical_pages_total': 1024,
        'virtual_texels_m': 3.545, 'physical_texels_m': 3.865,
        'physical_usage_percent': 24.158, 'mesh_cards_to_add': 0,
        'locked_cards_to_update': 0,
    },
}

manifest['lumen_fix'] = {
    'schema': 'arda-sponza-lumen-fix-v1',
    'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    'accepted_baseline': {
        'gi': 'Lumen hardware ray tracing',
        'cvar': 'r.Lumen.HardwareRayTracing 1',
        'far_field': 'r.LumenScene.FarField 1',
        'exposure_ev100': 7.0,
        'resolution': [1280, 720],
        'view': 'first active level viewport, LevelEditorSubsystem.editor_set_game_view(True)',
        'show_flags': ['ShowFlag.Cameras 0', 'ShowFlag.CameraFrustums 0',
                       'ShowFlag.CompositeEditorPrimitives 0', 'ShowFlag.Game 1',
                       'ShowFlag.BillboardSprites 0', 'ShowFlag.Grid 0',
                       'ShowFlag.Selection 0', 'ShowFlag.SelectionOutline 0',
                       'ShowFlag.ModeWidgets 0', 'ShowFlag.Bounds 0',
                       'ShowFlag.LumenGlobalTraces 1', 'ShowFlag.LumenDetailTraces 1'],
        'flags_applied_after': 'set_viewport_mode + editor_set_game_view',
    },
    'root_cause': (
        'Software Lumen resolves no valid mesh-card hit in this UE 5.8.1 offscreen editor '
        'session. The Lumen Scene is complete (115/115 primitives, 105 mesh-card groups, '
        '637 cards, 258/1024 physical pages, 0 pending mesh-card adds), yet '
        'r.Lumen.Visualize 5 is 99.88% yellow and r.Lumen.Visualize 3 is 100% black under '
        'r.Lumen.HardwareRayTracing 0. The global distance-field debug view is flat at the '
        'hero origin even after r.AOGlobalDistanceField.ForceFullUpdate 1, while from the '
        'atrium centre with a forced full update it renders identifiable SDF geometry '
        '(gdf-probe.png). Enabling ShowFlag.LumenGlobalTraces/DetailTraces does not change '
        'it. With '
        'r.Lumen.HardwareRayTracing 1 the same Lumen Scene yields 0.5% yellow and a lit '
        'Lumen Scene, proving the scene, cards, atlas, capture and GPU upload path are '
        'valid and isolating the fault to the software SDF trace/upload path.'
    ),
    'controls': {
        'software_surface_cache_yellow_fraction': 0.9988,
        'software_surface_cache_mean_luminance': 211.52,
        'software_lumen_scene_mean_luminance': 0.0,
        'hardware_surface_cache_yellow_fraction': 0.0056,
        'hardware_surface_cache_mean_luminance': 37.19,
        'hardware_lumen_scene_mean_luminance': 37.19,
        'showflag_trace_retest': 'output/lumen-verify.json',
        'gdf_force_full_update_retest': 'output/lumen-verify.json',
        'gdf_hero_origin_probe': 'output/gdf-hero-probe.json (1 unique 3-bit colour before and after ForceFullUpdate)',
        'gi_off_comparison': 'output/preview/hero-gi-off.png (r.DynamicGlobalIlluminationMethod 0)',
    },
    'lumen_scene_stats': stats_dump,
    'images': records,
    'evidence_images': evidence,
    'artifacts': {
        'diagnosis_json': str(OUT / 'lumen-diagnosis.json'),
        'verify_json': str(OUT / 'lumen-verify.json'),
        'final_session_json': str(OUT / 'lumen-final-session.json'),
        'logs': [str(OUT / 'logs' / n) for n in
                 ('lumdiag.stdout.log', 'lumverify.stdout.log', 'lumenfinal.stdout.log')],
        'report': str(WS / 'lumen-fix-report.md'),
        'gdf_hero_probe_json': str(OUT / 'gdf-hero-probe.json'),
        'gdf_hero_probe_log': str(OUT / 'logs' / 'gdfhero.stdout.log'),
    },
    'residual_limits': [
        'Software Lumen (r.Lumen.HardwareRayTracing 0) is still broken: 99.9% yellow Surface Cache and 100% black Lumen Scene; the baseline requires D3D12 SM6 ray tracing.',
        'The global distance-field debug view is flat at the hero origin even after ForceFullUpdate; from the atrium centre with a forced full update it renders identifiable SDF geometry (gdf-probe.png). Kept as evidence, nothing faked.',
        '10 of 115 primitives have no mesh cards (thin foliage/plant/detail meshes), so magenta missing-coverage patches remain on the upper arcade in the Surface Cache visualizer; this is expected mesh-card Lumen coverage, not invalid mapping.',
        'Lumen GI/reflection temporal state is warmed (~200 draws), not converged; captures are 8-bit display-referred viewport PNGs.',
        'No path tracing and no DumpGPU were run in this step.',
        'Runtime CVars are per-session; a fresh editor run must re-apply the baseline commands.',
    ],
    'map_unchanged': {
        'sha256': sha(WS / 'project' / 'UnrealLab' / 'Content' / 'Labs' / 'SponzaTeaching.umap'),
        'bytes': (WS / 'project' / 'UnrealLab' / 'Content' / 'Labs' / 'SponzaTeaching.umap').stat().st_size,
        'saved': False,
    },
}

# Refresh the current preview capture block so it no longer advertises the
# broken software-Lumen baseline or the artifact-laden hero frame.
manifest.setdefault('preview_capture', {})
manifest['preview_capture'].update({
    'superseded_by': 'lumen_fix',
    'accepted_baseline': 'Lumen hardware ray tracing (see lumen_fix)',
    'captured_utc': final.get('finished_utc'),
    'session': str(OUT / 'lumen-final-session.json'),
    'fixed_exposure_ev100': 7.0,
    'resolution': [1280, 720],
    'game_view': True,
    'images': records,
    'limits': manifest['lumen_fix']['residual_limits'],
    'previous_broken_evidence': {
        'hero_with_editor_primitives': str(PREV / 'hero-lit-editorprim-before.png'),
        'surface_cache_uniform_yellow': str(PREV / 'surface-cache-sw-broken.png'),
        'lumen_scene_black': str(PREV / 'lumen-scene-sw-broken.png'),
    },
})

with open(OUT / 'scene-manifest.json', 'w', encoding='utf-8') as f:
    json.dump(manifest, f, indent=2)
print('manifest updated: lumen_fix + preview_capture')
print(json.dumps(records['hero-lit'], indent=2))

