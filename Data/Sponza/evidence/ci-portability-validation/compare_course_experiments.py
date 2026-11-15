#!/usr/bin/env python3
"""Compare the Windows and Linux lumen-course experiments JSON values."""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
a = json.loads((HERE / 'lumen-course-experiments-windows-py314.json').read_text(encoding='utf-8-sig'))
b = json.loads((HERE / 'lumen-course-experiments-linux-py310.json').read_text(encoding='utf-8'))


def walk(left, right, path=''):
    if type(left) is not type(right):
        print(f'{path}: type {type(left).__name__} != {type(right).__name__}')
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                print(f'{path}.{key}: missing on one side')
            else:
                walk(left[key], right[key], f'{path}.{key}' if path else key)
    elif isinstance(left, list):
        if len(left) != len(right):
            print(f'{path}: length {len(left)} != {len(right)}')
            return
        for index, (x, y) in enumerate(zip(left, right)):
            walk(x, y, f'{path}[{index}]')
    elif left != right:
        print(f'{path}: {left!r} != {right!r}')


walk(a, b)
print('equal' if a == b else 'different')
