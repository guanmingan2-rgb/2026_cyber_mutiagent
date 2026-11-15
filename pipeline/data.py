"""Stage 1 — read CyberGym task materials.

Two entry points, both read-only and bounded:

* :func:`load_task_materials` reads the task description plus an explicit list of
  source members from ``repo-vul.tar.gz``. This is the original static-review
  contract: the caller names the files, the archive is streamed with limits, and
  no path outside the task directory is ever opened.
* :func:`read_workspace_texts` reads the files the official task generator writes
  into a workspace (``description.txt`` and ``README.md``) and hashes them. This
  is what the dynamic pipeline hands to the agent.

Both raise ``ValueError`` on unsafe or oversized input; neither writes anything.
"""

import gzip
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
import tarfile

TEXT_LIMIT = 16 * 1024
ARCHIVE_LIMIT = 1024 * 1024 * 1024
ARCHIVE_MEMBERS = 100_000
SOURCE_COUNT = 32
MAX_SOURCE_BYTES = 64 * 1024
LEVEL1_DATASETS = {'arvo', 'oss-fuzz'}
LEVEL1_SOURCE_SUFFIXES = {'.c', '.cc', '.cpp', '.cxx', '.h', '.hh', '.hpp', '.rs', '.go', '.java', '.py'}
BLOCKED_PART_WORDS = {
    'poc', 'pocs', 'exploit', 'exploits', 'patch', 'patches', 'test', 'tests',
    'corpus', 'crash', 'crashes', 'repro', 'regression',
}


def open_regular(path):
    """Open ``path`` only if it is a regular file and not a symlink."""
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
    """Normalise an archive member name and reject traversal/control characters."""
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError(f'unsafe archive path: {name!r}')
    return path.as_posix()


class BoundedArchive:
    """File-like wrapper that refuses to decompress more than 1 GiB."""

    def __init__(self, stream):
        self.stream = stream
        self.remaining = ARCHIVE_LIMIT

    def read(self, size):
        data = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError('archive scan exceeds 1 GiB uncompressed')
        return data


def read_sources(archive, requested, max_source_bytes=MAX_SOURCE_BYTES):
    """Stream ``repo-vul.tar.gz`` and return only the requested members."""
    if not 1 <= len(requested) <= SOURCE_COUNT:
        raise ValueError(f'select 1 to {SOURCE_COUNT} source members')
    paths = [member_path(name) for name in requested]
    if '.' in paths or len(set(paths)) != len(paths):
        raise ValueError('source paths must be distinct file paths')
    wanted = set(paths)
    seen = set()
    records = {}
    remaining = max_source_bytes
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
                    raise ValueError(f'{name}: selected source exceeds the total source bound')
                with members.extractfile(item) as member:
                    data = member.read(item.size)
                records[name] = text_record(data, name)
                remaining -= len(data)
    missing = wanted - records.keys()
    if missing:
        raise ValueError(f'missing source members: {", ".join(sorted(missing))}')
    return {name: records[name] for name in paths}


def check_task_id(task_id):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}:[0-9]{1,20}', task_id):
        raise ValueError('task id must have the form dataset:numeric-id')
    return task_id


def check_level1_source(name):
    """Reject paths that would leak patches, PoCs or tests into the agent input."""
    source = PurePosixPath(member_path(name))
    if (source.parts[:1] != ('src-vul',) or source.suffix.lower() not in LEVEL1_SOURCE_SUFFIXES
            or len(source.parts) < 3
            or any(part.startswith('.') or part.lower() in BLOCKED_PART_WORDS
                   for part in source.parts[1:-1])
            or re.search(r'(^|[-_.])(poc|exploit|repro|crash|trigger|test)([-_.]|$)', source.stem.lower())):
        raise ValueError('Level 1 requires allowlisted implementation source paths')


def task_directory(data_root, task_id):
    check_task_id(task_id)
    root = data_root.resolve(strict=True)
    task = root.joinpath(*task_id.split(':'))
    if task.resolve(strict=True) != task or not task.is_dir():
        raise ValueError('task must be a real directory under the data root')
    return task


def load_task_materials(data_root, task_id, sources, *, readme=None, max_source_bytes=MAX_SOURCE_BYTES):
    """Read description, optional README and an explicit source list.

    Returns ``{task_id, description, readme, sources}`` where each text entry is
    ``{text, sha256, bytes, lines}``.
    """
    check_task_id(task_id)
    if readme is None:
        if task_id.split(':')[0] not in LEVEL1_DATASETS:
            raise ValueError('Level 1 requires an ARVO or OSS-Fuzz task ID')
        for name in sources:
            check_level1_source(name)
    task = task_directory(data_root, task_id)
    description = text_record(read_bytes(task / 'description.txt', TEXT_LIMIT), 'description.txt')
    if readme is None:
        readme_record = text_record(
            b'Inspect the selected source snapshot for defensive static review. '
            b'Report code understanding and remediation observations only.',
            'static review instructions')
    else:
        readme_record = text_record(read_bytes(readme, TEXT_LIMIT), 'README')
    try:
        members = read_sources(task / 'repo-vul.tar.gz', sources, max_source_bytes)
    except (tarfile.TarError, EOFError) as exc:
        raise ValueError('invalid or truncated source archive') from exc
    return {'task_id': task_id, 'description': description, 'readme': readme_record, 'sources': members}


def read_workspace_texts(task_dir):
    """Read the text files the official task generator wrote into a workspace."""
    task_dir = task_dir.resolve(strict=True)
    payload, hashes = {}, {}
    for name in ('description.txt', 'README.md'):
        path = task_dir / name
        data = read_bytes(path, 32 * 1024)
        payload[name] = data.decode('utf-8')
        hashes[name] = hashlib.sha256(data).hexdigest()
    for name in ('repo-vul.tar.gz',):
        with open_regular(task_dir / name) as stream:
            digest = hashlib.sha256()
            while True:
                block = stream.read(1 << 20)
                if not block:
                    break
                digest.update(block)
        hashes[name] = digest.hexdigest()
    return payload, hashes
