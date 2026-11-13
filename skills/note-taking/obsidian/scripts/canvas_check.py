#!/usr/bin/env python3
"""Check selected JSON Canvas identifiers and connections without modifying files."""

import argparse
import json
import os
import stat
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024
MAX_REPORTED = 100


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate object key')
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError('non-JSON number')


def inspect(path):
    """Read a bounded, user-selected canvas and check a subset of its structure."""
    try:
        if not stat.S_ISREG(Path(path).stat().st_mode):
            return {'checks_passed': False, 'error': 'not_regular_file'}, 2
        with Path(path).open('rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {'checks_passed': False, 'error': 'not_regular_file'}, 2
            data = stream.read(MAX_BYTES + 1)
    except OSError:
        return {'checks_passed': False, 'error': 'unreadable_file'}, 2
    if len(data) > MAX_BYTES:
        return {'checks_passed': False, 'error': 'file_too_large'}, 2
    try:
        canvas = json.loads(data.decode('utf-8-sig'), object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant)
    except (UnicodeError, ValueError, RecursionError):
        return {'checks_passed': False, 'error': 'invalid_or_ambiguous_json'}, 2

    errors = []
    error_count = 0

    def report(code, collection=None, index=None, field=None):
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_REPORTED:
            errors.append({k: v for k, v in {
                'code': code, 'collection': collection, 'index': index, 'field': field
            }.items() if v is not None})

    if not isinstance(canvas, dict):
        report('root_not_object')
        canvas = {}
    collections = {}
    for name in ('nodes', 'edges'):
        value = canvas.get(name, [])
        if not isinstance(value, list):
            report('expected_array', name)
            value = []
        collections[name] = value

    node_ids = set()
    edge_ids = set()
    for name, seen in (('nodes', node_ids), ('edges', edge_ids)):
        for index, item in enumerate(collections[name]):
            if not isinstance(item, dict):
                report('expected_object', name, index)
                continue
            item_id = item.get('id')
            if not isinstance(item_id, str) or not item_id:
                report('missing_id', name, index, 'id')
            elif item_id in seen:
                report('duplicate_id', name, index, 'id')
            else:
                seen.add(item_id)

    for index, edge in enumerate(collections['edges']):
        if not isinstance(edge, dict):
            continue
        for field in ('fromNode', 'toNode'):
            value = edge.get(field)
            if not isinstance(value, str) or not value:
                report('missing_endpoint', 'edges', index, field)
            elif value not in node_ids:
                report('unknown_endpoint', 'edges', index, field)
    return {
        'checks_passed': error_count == 0,
        'node_count': len(collections['nodes']),
        'edge_count': len(collections['edges']),
        'error_count': error_count,
        'errors': errors,
        'errors_truncated': error_count > len(errors),
        'scope': 'JSON, node and edge IDs, and edge endpoints only; not full schema validation',
    }, 0 if error_count == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('canvas', type=Path, help='user-selected .canvas file')
    args = parser.parse_args()
    report, status = inspect(args.canvas)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
