"""Small JSON helpers shared by the pipeline modules."""

import json
from pathlib import Path


def save(path, value):
    """Write newline-terminated JSON to a path that must not exist yet."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def no_duplicate_keys(pairs):
    """json object hook that rejects duplicate keys in model output."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate response field: {key}')
        result[key] = value
    return result
