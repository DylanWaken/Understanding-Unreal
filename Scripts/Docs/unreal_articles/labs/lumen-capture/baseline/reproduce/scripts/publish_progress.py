"""Update buffers/progress.json from the latest dump analysis (no engine needed)."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get('ARDA_BUFFERS_ROOT', str(Path(__file__).resolve().parents[1])))
state = sys.argv[1] if len(sys.argv) > 1 else 'metadata_complete'
proj_path = ROOT / 'metadata' / 'projection.json'
proj = json.loads(proj_path.read_text(encoding='utf-8'))['summary']
free = shutil.disk_usage(str(ROOT.drive) + '\\').free
est = int(proj['estimated_binary_bytes'])
p = json.loads((ROOT / 'progress.json').read_text(encoding='utf-8-sig'))
p.update({
    'state': state,
    'metadata_dump_directory': proj['dump_directory'],
    'metadata_bytes': proj['existing_dump_bytes'],
    'metadata_pass_count': proj['pass_count'],
    'metadata_resource_count': proj['resource_count'],
    'estimated_full_binary_bytes': est,
    'estimated_full_binary_gib': round(est / 1024 ** 3, 3),
    'disk_free_gib': round(free / 1024 ** 3, 2),
    'projected_post_capture_free_gib': round((free - est) / 1024 ** 3, 2),
    'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
})
(ROOT / 'progress.json').write_text(json.dumps(p, indent=2), encoding='utf-8')
print(json.dumps(p, indent=2))
