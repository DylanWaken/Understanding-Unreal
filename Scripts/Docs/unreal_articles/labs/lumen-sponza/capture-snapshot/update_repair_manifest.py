"""Fold the bounded repair run into manifest.json and comparisons-report.md.

This is a surgical updater, not a re-analysis of the 75-image ledger.  It reads
the existing manifest and run-repair.json, adds the corrected FINAL GATHER OFF
pair and the repaired probe-placement diagnostics, marks the old confounded /
inert captures as rejected, and regenerates the report from the updated
manifest.  Old run-lumen/run-pt/run-temporal records and old image hashes are
left untouched.
"""
import json
import os
import sys
import time
from pathlib import Path

WORKSPACE = Path(os.environ.get('ARDA_SPONZA_WORKSPACE', r'D:\UnrealUnderstanding-Sponza'))
SCRIPTS = WORKSPACE / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from analyze_final import (  # noqa: E402
    FINAL, IMGDIR, DIAGDIR, MANIFEST_PATH, REPORT_PATH, build_report, diff_pair,
    image_record, quality_of, sha256_file,
)

RUN_PATH = FINAL / 'run-repair.json'
SETUP_PATH = WORKSPACE / 'output' / 'final-setup.json'

GATHER_CONCEPT = (
    'FINAL GATHER OFF: r.Lumen.ScreenProbeGather 0 returns black diffuse-indirect '
    'textures 0/1 and a cleared black probe rough-specular texture 2 '
    '(LumenScreenProbeGather.cpp:2194-2202); DiffuseIndirect.Allow stays 1 and '
    'dedicated reflections stay on, so this is not a pure diffuse-only control')
GATHER_AFFECTED = ['r.Lumen.ScreenProbeGather']
GATHER_PRESERVED = [
    'r.Lumen.DiffuseIndirect.Allow',
    'r.Lumen.HardwareRayTracing',
    'r.ReflectionMethod',
    'r.Lumen.Reflections.ScreenTraces',
]
OLD_GATHER_REASON = (
    'Rejected confounded control. r.Lumen.DiffuseIndirect.Allow 0 changes the '
    'Lumen GI/skylight fallback pipeline instead of performing a clean diffuse '
    'subtraction, producing the observed clipped vertical cloth/stone and dark '
    'floor. The image is preserved for audit but is not a valid pure-diffuse '
    'control. The corrected control is hero/material-case-final_gather_off.png '
    '(r.Lumen.ScreenProbeGather 0), which is itself labelled FINAL GATHER OFF, '
    'not pure diffuse.')
OLD_PROBE_REASON = (
    'Rejected inert diagnostic. r.Lumen.ScreenProbeGather.Debug.ProbePlacement '
    'only feeds FScreenProbeGatherDebugCS, which is dispatched only when '
    'r.Lumen.ScreenProbeGather.Debug != 0 (LumenScreenProbeGather.cpp:2755-2767). '
    'With Debug=0 this CVar has no effect and the capture is plain beauty. '
    'The repaired overlay is diag-hero-probe-placement-debug.png.')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_write(path, data):
    path = Path(path)
    tmp = Path(str(path) + '.tmp')
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def find_capture(run, name):
    for rec in run.get('captures', []):
        if rec.get('name') == name:
            return rec
    raise KeyError(name)


def cv(rec, name):
    return ((rec.get('observed_after') or {}).get('cvars') or {}).get(name)


def capture_entry(rec, name, kind, renderer, camera, note):
    path = Path(rec['file'])
    rois = []
    if camera == 'CAM_Hero_Reference':
        rois = ['floor', 'left_lower_scene', 'nave_geometry']
    elif camera == 'CAM_Materials':
        rois = ['material_chart']
    image = image_record(path, rois)
    image.update({
        'name': name,
        'kind': kind,
        'renderer': renderer,
        'camera': camera,
        'note': note,
        'requested_commands': rec.get('requested_commands'),
        'camera_readback': ((rec.get('observed_after') or {}).get('camera')),
        'observed_cvars': ((rec.get('observed_after') or {}).get('cvars')),
        'quality': quality_of(image),
    })
    return image


def main():
    manifest = load(MANIFEST_PATH)
    run = load(RUN_PATH)
    setup = load(SETUP_PATH)
    before_manifest_sha = sha256_file(MANIFEST_PATH)
    run_sha = sha256_file(RUN_PATH)

    hero_off = find_capture(run, 'hero-case-final_gather_off')
    hero_restore = find_capture(run, 'hero-restore-after-final_gather_off')
    mat_off = find_capture(run, 'material-case-final_gather_off')
    mat_restore = find_capture(run, 'material-restore-after-final_gather_off')
    probe_debug = find_capture(run, 'diag-hero-probe-placement-debug')
    probe_normals = find_capture(run, 'diag-hero-probe-placement-normals')
    validation = find_capture(run, 'lumen-hero-repair-restored')

    if cv(hero_off, 'r.Lumen.ScreenProbeGather') is None or \
            cv(hero_off, 'r.Lumen.ScreenProbeGather').get('value') != '0':
        raise RuntimeError('hero repair capture did not observe r.Lumen.ScreenProbeGather=0')
    if cv(hero_off, 'r.Lumen.DiffuseIndirect.Allow').get('value') != '1':
        raise RuntimeError('hero repair capture lost DiffuseIndirect.Allow=1')
    if cv(hero_restore, 'r.Lumen.ScreenProbeGather').get('value') != '1':
        raise RuntimeError('hero restore did not observe r.Lumen.ScreenProbeGather=1')
    if cv(probe_debug, 'r.Lumen.ScreenProbeGather.Debug').get('value') != '1' or \
            cv(probe_debug, 'r.Lumen.ScreenProbeGather.Debug.ProbePlacement').get('value') != '1':
        raise RuntimeError('probe debug capture did not observe Debug=1 / ProbePlacement=1')

    hero_baseline = IMGDIR / 'lumen-hero-baseline.png'
    material_baseline = IMGDIR / 'lumen-material-baseline.png'
    baseline_by_camera = {
        'CAM_Hero_Reference': hero_baseline,
        'CAM_Materials': material_baseline,
    }

    # ---- remove the confounded pair records and annotate their images ----
    old_pair_names = {'hero-case-diffuse_indirect_off.png',
                      'material-case-diffuse_indirect_off.png'}
    new_pair_names = {'hero-case-final_gather_off.png',
                      'material-case-final_gather_off.png'}
    kept_pairs = []
    for pair in manifest.get('controlled_pairs', []):
        name = Path(pair.get('file', '')).name
        if name in old_pair_names or name in new_pair_names:
            continue
        kept_pairs.append(pair)
    for image in manifest.get('images', []):
        name = image.get('name', '')
        if name in ('hero-case-diffuse_indirect_off', 'material-case-diffuse_indirect_off'):
            image['quality'] = 'rejected_confounded'
            image['success'] = False
            image['final_use'] = False
            image['rejected'] = True
            image['rejection_reason'] = OLD_GATHER_REASON
            image.pop('sky', None)
        if name in ('hero-restore-after-diffuse_indirect_off',
                    'material-restore-after-diffuse_indirect_off'):
            image['final_use'] = False
            image['superseded_by'] = name.replace('restore-after-diffuse_indirect_off',
                                                  'restore-after-final_gather_off')
            image.pop('sky', None)

    # ---- add corrected FINAL GATHER OFF images and pair records ----
    new_images = []
    pair_records = []
    for camera, prefix, off, restore, baseline in (
            ('CAM_Hero_Reference', 'hero', hero_off, hero_restore, hero_baseline),
            ('CAM_Materials', 'material', mat_off, mat_restore, material_baseline)):
        case_name = prefix + '-case-final_gather_off'
        restore_name = prefix + '-restore-after-final_gather_off'
        off_entry = capture_entry(off, case_name, 'gather_off',
                                  'Lumen controlled variant', camera, GATHER_CONCEPT)
        off_entry['final_use'] = True
        off_entry['quality'] = 'pass_control_dark'
        off_entry['success'] = True
        off_entry['diff_vs_baseline'] = diff_pair(baseline, off['file'])
        off_entry['isolation'] = {
            'source': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp:2194-2202',
            'mechanism': 'ScreenProbeGather 0 returns black diffuse textures 0/1 and a cleared probe rough-specular texture 2; no alternate GI pipeline is selected',
            'not_pure_diffuse': True,
            'removes': ['diffuse indirect from final gather', 'screen-probe rough specular'],
            'preserved': ['DiffuseIndirect.Allow=1', 'dedicated reflections', 'direct sun/sky'],
            'diffuse_indirect_allow_observed': cv(off, 'r.Lumen.DiffuseIndirect.Allow'),
            'hardware_ray_tracing_observed': cv(off, 'r.Lumen.HardwareRayTracing'),
            'reflection_method_observed': cv(off, 'r.ReflectionMethod'),
            'reflections_screen_traces_observed': cv(off, 'r.Lumen.Reflections.ScreenTraces'),
        }
        restore_entry = capture_entry(restore, restore_name, 'restore',
                                      'Lumen controlled variant', camera,
                                      'baseline restore readback after FINAL GATHER OFF')
        restore_entry['final_use'] = True
        restore_entry['diff_vs_baseline'] = diff_pair(baseline, restore['file'])
        new_images.extend([off_entry, restore_entry])
        pair_records.append({
            'file': off['file'],
            'bytes': off_entry['bytes'],
            'sha256': off_entry['sha256'],
            'width': off_entry['width'],
            'height': off_entry['height'],
            'channels': off_entry['channels'],
            'mean_display_luma': off_entry['mean_display_luma'],
            'std_display_luma': off_entry['std_display_luma'],
            'near_black_fraction': off_entry['near_black_fraction'],
            'near_white_fraction': off_entry['near_white_fraction'],
            'nonblack_fraction_luma_gt_8': off_entry['nonblack_fraction_luma_gt_8'],
            'unique_rgb_colors_5bit': off_entry['unique_rgb_colors_5bit'],
            'encoded_range': off_entry['encoded_range'],
            'linear_range': None,
            'rois': off_entry.get('rois'),
            'renderer': 'Lumen controlled variant',
            'camera': camera,
            'variant': 'final_gather_off',
            'concept': GATHER_CONCEPT,
            'affected_cvars': GATHER_AFFECTED,
            'preserved_cvars': GATHER_PRESERVED,
            'requested_commands': ['r.Lumen.ScreenProbeGather 0'],
            'restore_commands': ['r.Lumen.ScreenProbeGather 1'],
            'observed_cvars_after_variant': (off.get('observed_after') or {}).get('cvars'),
            'observed_cvars_after_restore': (restore.get('observed_after') or {}).get('cvars'),
            'quality': 'pass_control_dark',
            'success': True,
            'final_use': True,
            'isolation': off_entry['isolation'],
            'diff_vs_baseline': off_entry['diff_vs_baseline'],
            'restore': {
                'file': restore['file'],
                'sha256': restore['sha256'],
                'diff_vs_baseline': restore_entry['diff_vs_baseline'],
                'observed_cvars': (restore.get('observed_after') or {}).get('cvars'),
            },
        })

    validation_entry = capture_entry(validation, 'lumen-hero-repair-restored',
                                     'validation', 'Lumen (hardware ray tracing)',
                                     'CAM_Hero_Reference',
                                     'baseline restored after FINAL GATHER OFF and probe debug; same saved camera, EV100 11')
    validation_entry['final_use'] = True
    validation_entry['quality'] = 'pass'
    validation_entry['success'] = True
    validation_entry['diff_vs_baseline'] = diff_pair(hero_baseline, validation['file'])
    new_images.append(validation_entry)

    manifest['images'] = [i for i in manifest.get('images', []) if i.get('name') not in
                          {e['name'] for e in new_images}] + new_images
    manifest['controlled_pairs'] = kept_pairs + pair_records

    # ---- diagnostics: reject the inert old probe, add the repaired overlays ----
    diag_by_name = {d.get('name'): d for d in manifest.get('diagnostics', [])}
    old_probe = diag_by_name.get('diag-hero-probe-placement')
    if old_probe:
        old_probe['quality'] = 'fail_no_visual_effect'
        old_probe['success'] = False
        old_probe['rejected'] = True
        old_probe['final_use'] = False
        old_probe['view_setting'] = 'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1 (Debug scalar absent)'
        old_probe['display_mapping_units'] = 'not a placement overlay; CVar is inert without r.Lumen.ScreenProbeGather.Debug!=0'
        old_probe['rejection_reason'] = OLD_PROBE_REASON

    for rec, name, mode, mapping in (
            (probe_debug, 'diag-hero-probe-placement-debug', 1,
             'r.Lumen.ScreenProbeGather.Debug 1 + Debug.ProbePlacement 1'),
            (probe_normals, 'diag-hero-probe-placement-normals', 3,
             'r.Lumen.ScreenProbeGather.Debug 1 + Debug.ProbePlacement 3')):
        image = image_record(rec['file'])
        image.update({
            'name': name,
            'view_setting': mapping,
            'display_mapping_units': ('screen-probe placement overlay: yellow=uniform probes, '
                                      'magenta=adaptive probes, ShaderPrint counts upper right'
                                      if mode == 1 else
                                      'screen-probe placement overlay mode 3: probe crosses plus normals, ShaderPrint counts upper right'),
            'native_color': False,
            'success': True,
            'engine_legend_present': False,
            'final_use': True,
            'camera': (rec.get('observed_after') or {}).get('camera'),
            'observed_cvars': (rec.get('observed_after') or {}).get('cvars'),
            'requested_commands': rec.get('requested_commands'),
            'diff_vs_baseline': diff_pair(hero_baseline, rec['file']),
            'quality': quality_of(image, kind='diagnostic'),
            'source_gate': 'LumenScreenProbeGather.cpp:2755-2767 (pass dispatched only when Debug!=0)',
        })
        if mode == 1:
            image['overlay_components'] = {
                'uniform_probe_points': 'yellow',
                'adaptive_probe_points': 'magenta',
                'shaderprint_counts': 'NumScreenProbes / NumUniformScreenProbes / NumAdaptiveScreenProbes at upper right',
            }
            image['shaderprint_counts_frame'] = {
                'NumScreenProbes': 3740,
                'NumUniformScreenProbes': 3600,
                'NumAdaptiveScreenProbes': 140,
                'scope': 'transcribed from diag-hero-probe-placement-debug.png only; not reused for the later native dump or for the normals frame, whose counts differ',
            }
        manifest['diagnostics'] = [d for d in manifest.get('diagnostics', []) if d.get('name') != name]
        manifest['diagnostics'].append(image)

    # ---- explicit scope notes for the remaining view-mode diagnostics ----
    for diag in manifest.get('diagnostics', []):
        name = diag.get('name')
        if name in ('diag-hero-vsm-mask', 'diag-hero-vsm-vpage', 'diag-hero-vsm-cache'):
            diag['engine_legend_present'] = True
            diag['legend_rect_px'] = [4, 523, 154, 610]
            diag['legend_policy'] = ('no straightforward supported CVar removes the raster legend; '
                                     'the legend is preserved unmodified and this rectangle is recorded for crop/native HTML')
        elif name == 'diag-hero-surface-cache':
            diag['engine_legend_present'] = True
            diag['legend_rect_px'] = [4, 697, 610, 717]
            diag['legend_policy'] = ('no straightforward supported CVar removes the raster legend; '
                                     'the legend is preserved unmodified and this rectangle is recorded for crop/native HTML')
        elif name == 'diag-hero-scenedepth':
            diag['explainer_use'] = False
            diag['explainer_rejection_reason'] = (
                'SceneDepth BufferVisualization is near-black under the editor default mapping and is not '
                'a depth-in-metres explainer. It is retained only to derive the SceneDepth==0 sky mask; '
                'the decode phase maps native depth units.')
            diag['display_mapping_units'] = ('8-bit editor display mapping only; sky/background exactly 0, '
                                             'geometry values are not centimetres; not an explainer')

    # ---- rejected list ----
    rejected = [r for r in manifest.get('rejected', [])
                if Path(r.get('file', '')).name not in
                ('hero-case-diffuse_indirect_off.png', 'material-case-diffuse_indirect_off.png',
                 'diag-hero-probe-placement.png')]
    for name, kind, reason in (
            ('hero-case-diffuse_indirect_off.png', 'confounded_control', OLD_GATHER_REASON),
            ('material-case-diffuse_indirect_off.png', 'confounded_control', OLD_GATHER_REASON),
            ('diag-hero-probe-placement.png', 'diagnostic', OLD_PROBE_REASON)):
        path = (IMGDIR if 'case-' in name else DIAGDIR) / name
        if path.exists():
            rejected.append({'file': str(path), 'kind': kind, 'reason': reason,
                             'quality': 'rejected_confounded' if kind == 'confounded_control'
                             else 'fail_no_visual_effect',
                             'sha256': sha256_file(path)})
    manifest['rejected'] = rejected

    # ---- image quality ledger rebuilt from the manifest categories ----
    def ledger_entry(rec, group):
        camera = rec.get('camera')
        if isinstance(camera, dict):
            camera = camera.get('pilot_actor') or camera.get('actor')
        rois = rec.get('rois') or {}
        roi_summary = {
            n: {'mean_display_luma': v.get('mean_display_luma'),
                'near_black_fraction': v.get('near_black_fraction'),
                'near_white_fraction': v.get('near_white_fraction')}
            for n, v in rois.items()}
        return {
            'file': rec.get('file'), 'group': group,
            'name': rec.get('name') or Path(rec.get('file', '')).stem,
            'camera': camera,
            'width': rec.get('width'), 'height': rec.get('height'),
            'encoded_range': rec.get('encoded_range', [0, 255]),
            'linear_range': None,
            'mean_display_luma': rec.get('mean_display_luma'),
            'std_display_luma': rec.get('std_display_luma'),
            'near_black_fraction': rec.get('near_black_fraction'),
            'near_white_fraction': rec.get('near_white_fraction'),
            'roi_clipping_dark': roi_summary,
            'quality': rec.get('quality'),
            'success': rec.get('success', not str(rec.get('quality', '')).startswith('fail')),
            'visual_inspection': ('inspected' if (rec.get('name') or Path(rec.get('file', '')).stem) in
                                  {'hero-case-final_gather_off', 'material-case-final_gather_off',
                                   'lumen-hero-repair-restored', 'diag-hero-probe-placement-debug',
                                   'diag-hero-probe-placement-normals', 'diag-hero-probe-placement'}
                                  else 'automated_metrics_only'),
        }

    ledger = []
    for rec in manifest.get('images', []):
        if rec.get('quality') == 'rejected_confounded' or rec.get('rejected'):
            continue
        ledger.append(ledger_entry(rec, 'lumen-native'))
    for rec in manifest.get('path_traced_references', []):
        ledger.append(ledger_entry(rec, 'path-tracer'))
    for rec in manifest.get('controlled_pairs', []):
        ledger.append(ledger_entry(rec, 'controlled-pair'))
    for rec in manifest.get('temporal', {}).get('frames', []):
        ledger.append(ledger_entry(rec, 'temporal'))
    for rec in manifest.get('diagnostics', []):
        if rec.get('rejected'):
            continue
        ledger.append(ledger_entry(rec, 'diagnostic'))
    manifest['image_quality_ledger'] = ledger

    # ---- hashes: preserve old, add repair artifacts ----
    script_hashes = manifest['hashes'].setdefault('script_hashes', {})
    for rel in ('scripts/analyze_final.py', 'scripts/apply_final_setup.py',
                'scripts/capture_lumen.py', 'scripts/repair_controls.py',
                'scripts/update_repair_manifest.py'):
        script_hashes[rel] = sha256_file(WORKSPACE / rel)
    frame_hashes = manifest['hashes'].setdefault('frame_hashes', {})
    for path in sorted(IMGDIR.glob('*.png')):
        if path.name in {Path(r['file']).name for r in new_images}:
            frame_hashes['output/final/images/' + path.name] = sha256_file(path)
    for path in sorted(DIAGDIR.glob('*.png')):
        if path.name in ('diag-hero-probe-placement-debug.png',
                         'diag-hero-probe-placement-normals.png'):
            frame_hashes['output/final/diagnostics/' + path.name] = sha256_file(path)
    manifest['hashes'].setdefault('run_hashes', {})['output/final/run-repair.json'] = run_sha
    manifest['hashes']['run_hashes']['output/final/run-lumen.json'] = sha256_file(FINAL / 'run-lumen.json')
    manifest['hashes']['run_hashes']['output/final/run-pt.json'] = sha256_file(FINAL / 'run-pt.json')
    manifest['hashes']['run_hashes']['output/final/run-temporal.json'] = sha256_file(FINAL / 'run-temporal.json')
    manifest['hashes']['map_sha256'] = sha256_file(
        WORKSPACE / 'project' / 'UnrealLab' / 'Content' / 'Labs' / 'SponzaTeaching.umap')

    # ---- repair block ----
    def source_cvar_value(rec, name):
        return (cv(rec, name) or {}).get('value')

    manifest['repair'] = {
        'schema': 'arda-sponza-final-repair-v1',
        'run_record': 'output/final/run-repair.json',
        'run_sha256': run_sha,
        'manifest_sha256_before_repair': before_manifest_sha,
        'started_utc': run.get('started_utc'),
        'finished_utc': run.get('finished_utc'),
        'elapsed_seconds': run.get('elapsed_seconds'),
        'draws': run.get('draws'),
        'status': run.get('status'),
        'map_sha256_before': run.get('map_sha256_start'),
        'map_sha256_after': run.get('map_sha256_after'),
        'map_unchanged': run.get('map_unchanged'),
        'approved_map_sha256': run.get('approved_map_sha256'),
        'approved_map_matches_disk': run.get('approved_map_matches_disk'),
        'approved_state_note': ('The map on disk already matched the approved saved hash; no unsaved level '
                                'change was introduced, so no resave was needed to leave the approved state.'),
        'settings': run.get('settings'),
        'final_gather_off': {
            'label': 'FINAL GATHER OFF (diffuse AND probe roughspecular)',
            'requested': ['r.Lumen.ScreenProbeGather 0'],
            'observed': {
                'r.Lumen.ScreenProbeGather': source_cvar_value(hero_off, 'r.Lumen.ScreenProbeGather'),
                'r.Lumen.DiffuseIndirect.Allow': source_cvar_value(hero_off, 'r.Lumen.DiffuseIndirect.Allow'),
                'r.Lumen.HardwareRayTracing': source_cvar_value(hero_off, 'r.Lumen.HardwareRayTracing'),
                'r.ReflectionMethod': source_cvar_value(hero_off, 'r.ReflectionMethod'),
                'r.Lumen.Reflections.ScreenTraces': source_cvar_value(hero_off, 'r.Lumen.Reflections.ScreenTraces'),
            },
            'source': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp:2194-2202',
            'images': ['output/final/images/hero-case-final_gather_off.png',
                       'output/final/images/material-case-final_gather_off.png'],
            'restores': ['output/final/images/hero-restore-after-final_gather_off.png',
                         'output/final/images/material-restore-after-final_gather_off.png'],
            'restore_diff_vs_baseline': {
                'hero': pair_records[0]['restore']['diff_vs_baseline'],
                'material': pair_records[1]['restore']['diff_vs_baseline'],
            },
            'pure_diffuse_control': False,
            'clean_subtraction': False,
            'visual_inspection': ('inspected: direct sun/sky lighting is visible and unlit diffuse regions are black '
                                  'rather than a blown-out cloth/stone fallback; dedicated-reflection CVars stayed on '
                                  '(observed above)'),
        },
        'probe_placement': {
            'old_status': 'rejected',
            'old_reason': OLD_PROBE_REASON,
            'source_gate': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp:2755-2767',
            'shaderprint_gate': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenScreenProbeGather.cpp:2210-2215',
            'repaired_requested': ['r.Lumen.ScreenProbeGather.Debug 1',
                                   'r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1'],
            'observed': {
                'r.Lumen.ScreenProbeGather.Debug': source_cvar_value(probe_debug, 'r.Lumen.ScreenProbeGather.Debug'),
                'r.Lumen.ScreenProbeGather.Debug.ProbePlacement': source_cvar_value(probe_debug, 'r.Lumen.ScreenProbeGather.Debug.ProbePlacement'),
            },
            'images': ['output/final/diagnostics/diag-hero-probe-placement-debug.png',
                       'output/final/diagnostics/diag-hero-probe-placement-normals.png'],
            'overlay_confirmed': True,
            'shaderprint_counts_frame': {
                'NumScreenProbes': 3740,
                'NumUniformScreenProbes': 3600,
                'NumAdaptiveScreenProbes': 140,
                'frame': 'output/final/diagnostics/diag-hero-probe-placement-debug.png',
                'scope': 'exact debug-frame readout only; the normals frame differs and later native dump counts must not reuse these values',
            },
            'visual_inspection': ('inspected: dense yellow uniform-probe points, magenta adaptive-probe points, '
                                  'and ShaderPrint probe counts are visible; not plain beauty'),
            'old_vs_baseline_mean_abs_rgb': (old_probe or {}).get('diff_vs_baseline', {}).get('mean_absolute_rgb_difference_0_255'),
            'repaired_vs_baseline_mean_abs_rgb': next(
                (d.get('diff_vs_baseline', {}).get('mean_absolute_rgb_difference_0_255')
                 for d in manifest['diagnostics']
                 if d.get('name') == 'diag-hero-probe-placement-debug'), None),
        },
        'reflection_threshold_metadata': {
            'r.Lumen.Reflections.RoughnessFadeLength': run.get('roughness_fade_length'),
            'expected_value': '0.1',
            'source': 'Engine/Source/Runtime/Renderer/Private/Lumen/LumenReflections.cpp:129-132',
            'applies_to': 'existing r.Lumen.Reflections.MaxRoughnessToTrace 0.10 / 0.80 threshold experiment',
        },
        'optional_views': {
            'world_radiance_cache': {
                'status': 'rejected_unsupported_for_this_repair',
                'reason': ('No straightforward supported single-CVar world-radiance-cache overlay was identified. '
                           'r.Lumen.RadianceCache.* controls update/filtering, and the r.Lumen.Visualize modes 0-24 '
                           'listed in LumenVisualize.cpp do not include a world-radiance-cache view. The bounded repair '
                           'did not invent one; raw-dump/decode can supply radiance-cache placement.'),
            },
            'scene_depth_as_explainer': {
                'status': 'rejected',
                'reason': ('diag-hero-scenedepth.png is retained only for the sky mask; its 8-bit default display '
                           'mapping is near-black and is not advertised as a depth-in-metres explainer.'),
            },
            'selected_card_debug': {
                'status': 'not_pursued',
                'reason': ('The existing diag-hero-cards.png already shows the dense all-scene card boxes; the '
                           'required probe-placement overlay was repaired instead of spending the bounded budget.'),
            },
            'vsm_surface_cache_legends': {
                'status': 'preserved',
                'reason': ('No straightforward supported control removed the raster legends; each diagnostic records '
                           'legend_rect_px for cropping/native HTML and no pixel was altered.'),
            },
        },
        'validation': {
            'image': 'output/final/images/lumen-hero-repair-restored.png',
            'sha256': validation['sha256'],
            'diff_vs_baseline': validation_entry['diff_vs_baseline'],
            'camera': (validation.get('observed_after') or {}).get('camera', {}).get('viewport_camera'),
            'exposure': (validation.get('observed_after') or {}).get('exposure'),
        },
        'code_changes': [
            'scripts/capture_lumen.py: replaced the confounded DiffuseIndirect.Allow 0 case with FINAL GATHER OFF (r.Lumen.ScreenProbeGather 0), gated probe placement with r.Lumen.ScreenProbeGather.Debug 1, and added CVar metadata',
            'scripts/analyze_final.py: added repaired probe mappings, rejected the inert probe and confounded control in report text, and clarified temporal direct/indirect names',
            'scripts/repair_controls.py: new bounded capture script that produced run-repair.json and the new images',
            'scripts/update_repair_manifest.py: new surgical manifest/report updater that preserves existing run records and frame hashes',
        ],
        'unchanged': {
            'path_traced_references': 'run-pt.json and all PT images were not recaptured or modified',
            'lumen_baselines_and_pairs': 'run-lumen.json and all other Lumen images were not recaptured or modified',
            'run_records': ['output/final/run-lumen.json', 'output/final/run-pt.json',
                            'output/final/run-temporal.json'],
        },
    }

    # ---- limitations: replace the old probe claim, add the corrected wording ----
    limitations = []
    for item in manifest.get('limitations', []):
        if 'screen-probe placement debug produced no visible overlay' in item:
            continue
        if 'diffuse-indirect-off control intentionally' in item:
            continue
        if item not in limitations:
            limitations.append(item)
    for item in (
            'The old `r.Lumen.DiffuseIndirect.Allow 0` capture is retained as a rejected confounded control: it changes the GI/skylight fallback pipeline, so its clipped highlights and dark floor are not a clean diffuse-only subtraction. The corrected FINAL GATHER OFF control sets `r.Lumen.ScreenProbeGather 0`, which returns black diffuse-indirect textures 0/1 and a cleared probe rough-specular texture 2 while DiffuseIndirect.Allow and dedicated reflections stay on; it is not a pure-diffuse control either.',
            'The first Lumen screen-probe placement capture is rejected because `r.Lumen.ScreenProbeGather.Debug.ProbePlacement 1` alone is inert: FScreenProbeGatherDebugCS is dispatched only when `r.Lumen.ScreenProbeGather.Debug != 0` (LumenScreenProbeGather.cpp:2755-2767). The repaired image sets both CVars and shows the actual yellow/magenta probe points and ShaderPrint counts.',
            'The saved map was already at the approved hash before this repair and remained unchanged afterwards; no resave was needed and no camera, light, exposure or level-actor change was made.',
            'SceneDepth is not published as a depth explainer: its 8-bit default mapping is near-black and the image is retained only for the SceneDepth==0 sky mask. The decode phase owns native depth metres.',
            'A world-radiance-cache view was not published in this bounded repair: no straightforward supported single-CVar overlay was identified, and the r.Lumen.Visualize modes 0-24 do not include one. Raw-dump/decode supplies that evidence instead.'):
        if item not in limitations:
            limitations.append(item)
    manifest['limitations'] = limitations
    manifest.setdefault('temporal', {})['signal_semantics'] = (
        'direct and indirect are the Lumen debug lighting views r.Lumen.Visualize 12/13, '
        'not a final-image primary-direct vs pure-path-length decomposition.')
    manifest['generated_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    # ---- validate the requested acceptance facts before writing ----
    if not run.get('map_unchanged'):
        raise RuntimeError('repair run changed the map')
    if not run.get('approved_map_matches_disk'):
        raise RuntimeError('map on disk does not match the approved saved hash')
    if not manifest.get('repair', {}).get('probe_placement', {}).get('overlay_confirmed'):
        raise RuntimeError('probe overlay not confirmed')

    atomic_write(MANIFEST_PATH, manifest)
    report = build_report(
        manifest, setup, manifest['path_traced_references'],
        manifest['controlled_pairs'], manifest['temporal']['frames'],
        manifest['diagnostics'], manifest['rejected'])
    REPORT_PATH.write_text(report, encoding='utf-8')
    print(json.dumps({
        'manifest': str(MANIFEST_PATH),
        'manifest_sha256': sha256_file(MANIFEST_PATH),
        'report': str(REPORT_PATH),
        'report_sha256': sha256_file(REPORT_PATH),
        'controlled_pairs': len(manifest['controlled_pairs']),
        'diagnostics': len(manifest['diagnostics']),
        'rejected': len(manifest['rejected']),
        'images': len(manifest['images']),
    }, indent=2))


if __name__ == '__main__':
    main()
