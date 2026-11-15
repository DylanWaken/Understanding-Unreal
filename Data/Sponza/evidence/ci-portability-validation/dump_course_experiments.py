#!/usr/bin/env python3
"""Emit the lumen-course experiments JSON for a platform comparison."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
COURSE = ROOT / 'Scripts' / 'Docs' / 'unreal_articles' / 'labs' / 'lumen-course'
sys.path.insert(0, str(COURSE))
spec = importlib.util.spec_from_file_location('lumen_course_generate_diag', COURSE / 'generate.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
print(json.dumps(module.experiments(), indent=2))
