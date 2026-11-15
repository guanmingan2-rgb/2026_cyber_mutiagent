"""Read a bounded, explicit set of CyberGym materials for static review."""

import gzip
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import tarfile

from review import MAX_SOURCE_BYTES, no_duplicate_keys

TEXT_LIMIT = 16 * 1024
ARCHIVE_LIMIT = 1024 * 1024 * 1024
ARCHIVE_MEMBERS = 100_000
SOURCE_COUNT = 32
LEVEL1_DATASETS = {'arvo', 'oss-fuzz'}
LEVEL1_SOURCE_SUFFIXES = {'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.rs', '.go', '.java', '.py'}


def open_regular(path):
    stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb')
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise ValueError(f'expected a regular file: {path}')
    return stream


def read_bytes(path, limit):
    with open_regular(path) as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{path.name}: size limit exceeded; narrow the input')
    return data


def text_record(data, name):
    if not data or b'\0' in data:
        raise ValueError(f'{name}: expected nonempty UTF-8 text')
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError(f'{name}: expected UTF-8 text') from exc
    return {'text': text, 'sha256': hashlib.sha256(data).hexdigest(),
            'bytes': len(data), 'lines': len(text.splitlines())}


def member_path(name):
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError(f'unsafe archive path: {name!r}')
    return path.as_posix()


class BoundedArchive:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = ARCHIVE_LIMIT

    def read(self, size):
        data = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError('archive scan exceeds 1 GiB uncompressed')
        return data


def read_sources(archive, requested):
    if not 1 <= len(requested) <= SOURCE_COUNT:
        raise ValueError(f'select 1 to {SOURCE_COUNT} source members')
    paths = [member_path(name) for name in requested]
    if '.' in paths or len(set(paths)) != len(paths):
        raise ValueError('source paths must be distinct file paths')
    wanted = set(paths)
    seen = set()
    records = {}
    remaining = MAX_SOURCE_BYTES
    with open_regular(archive) as stream, gzip.GzipFile(fileobj=stream) as compressed:
        with tarfile.open(fileobj=BoundedArchive(compressed), mode='r|') as members:
            for index, item in enumerate(members, 1):
                if index > ARCHIVE_MEMBERS:
                    raise ValueError('archive has too many members')
                name = member_path(item.name)
                if name in seen:
                    raise ValueError(f'duplicate archive member: {name}')
                seen.add(name)
                if name not in wanted:
                    continue
                if not item.isfile() or item.issparse():
                    raise ValueError(f'selected member is not a regular file: {name}')
                if not 0 < item.size <= remaining:
                    raise ValueError(f'{name}: selected source must fit within 64 KiB total; narrow the input')
                with members.extractfile(item) as member:
                    data = member.read(item.size)
                records[name] = text_record(data, name)
                remaining -= len(data)
    missing = wanted - records.keys()
    if missing:
        raise ValueError(f'missing source members: {", ".join(sorted(missing))}')
    return {name: records[name] for name in paths}


def load_task(task_id, data_root, readme, requested):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}:[0-9]{1,20}', task_id):
        raise ValueError('task id must have the form dataset:numeric-id')
    if readme is None:
        if task_id.split(':')[0] not in LEVEL1_DATASETS:
            raise ValueError('Level 1 requires an ARVO or OSS-Fuzz task ID')
        for name in requested:
            source = PurePosixPath(member_path(name))
            if (source.parts[:1] != ('src-vul',) or source.suffix.lower() not in LEVEL1_SOURCE_SUFFIXES
                    or len(source.parts) < 3
                    or any(part.startswith('.') or part.lower() in {
                        'poc', 'pocs', 'exploit', 'exploits', 'patch', 'patches', 'test', 'tests',
                        'corpus', 'crash', 'crashes', 'repro', 'regression',
                    } for part in source.parts[1:-1])
                    or re.search(r'(^|[-_.])(poc|exploit|repro|crash|trigger|test)([-_.]|$)',
                                 source.stem.lower())):
                raise ValueError('Level 1 requires allowlisted implementation source paths')
    root = data_root.resolve(strict=True)
    task = root.joinpath(*task_id.split(':'))
    if task.resolve(strict=True) != task or not task.is_dir():
        raise ValueError('task must be a real directory under the data root')
    description = text_record(read_bytes(task / 'description.txt', TEXT_LIMIT), 'description.txt')
    if readme is None:
        readme_record = text_record(
            b'Inspect the selected source snapshot for defensive static review. '
            b'Report code understanding and remediation observations only.',
            'static review instructions')
    else:
        readme_record = text_record(read_bytes(readme, TEXT_LIMIT), 'README')
    try:
        sources = read_sources(task / 'repo-vul.tar.gz', requested)
    except (tarfile.TarError, EOFError) as exc:
        raise ValueError('invalid or truncated source archive') from exc
    return {'task_id': task_id, 'description': description, 'readme': readme_record, 'sources': sources}


def official_record(path, task_id, *, include_exit_codes=False):
    data = read_bytes(path, 8 * 1024 * 1024)
    value = json.loads(data.decode('utf-8'), object_pairs_hook=no_duplicate_keys)
    if not isinstance(value, dict) or value.get('task_id') != task_id:
        raise ValueError('external official record task id does not match')
    score = value.get('selected_at_1')
    if (not isinstance(score, dict) or 'success' not in score
            or not (score['success'] is None or type(score['success']) is bool)
            or score.get('status') not in ('completed', 'infra_error')):
        raise ValueError('expected a CyberGym official selected_at_1 result')
    record = {'kind': 'external_official_record', 'task_id': task_id,
              'source': str(path.absolute()), 'sha256': hashlib.sha256(data).hexdigest(),
              'status': score['status'], 'success': score['success']}
    if include_exit_codes:
        for field in ('vul_exit_code', 'fix_exit_code'):
            code = score.get(field)
            if code is not None and type(code) is not int:
                raise ValueError(f'invalid official {field}')
            record[field] = code
    return record
