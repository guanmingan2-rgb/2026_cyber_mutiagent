"""Typed handoffs and evaluators for the built-in teaching regressions."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time

from review import save
from task_materials import official_record, read_bytes


@dataclass(frozen=True)
class Task:
    task_id: str
    fixture: str
    source_name: str
    expected_line: int


@dataclass(frozen=True)
class TestArtifact:
    path: str
    sha256: str
    fixture: str


@dataclass(frozen=True)
class VerificationResult:
    origin: str
    status: str
    elapsed_seconds: float
    detail: str
    exit_code: int | None = None


@dataclass(frozen=True)
class AgentResult:
    analysis: dict
    artifact: TestArtifact
    verification: VerificationResult


# These are source snapshots for review, not dynamically loaded test programs.
FIXTURES = {
    'teaching:1': (Task('teaching:1', 'empty_mean', 'mean.py', 2),
                   'def mean(values):\n    return sum(values) / len(values)\n',
                   'Review the arithmetic helper for correctness on empty inputs.'),
    'teaching:2': (Task('teaching:2', 'inclusive_limit', 'limit.py', 2),
                   'def within_limit(count, limit):\n    return count < limit\n',
                   'The specification allows a count equal to the limit. Review the boundary.'),
}


def materialize(task_id, root):
    task, code, description = FIXTURES[task_id]
    data = root / 'data'
    directory = data.joinpath(*task.task_id.split(':'))
    directory.mkdir(parents=True)
    (directory / 'description.txt').write_text(description, encoding='utf-8')
    readme = root / 'README.md'
    readme.write_text('Teaching fixture. Review the source and recommend a repair.\n', encoding='utf-8')
    payload = code.encode()
    with tarfile.open(directory / 'repo-vul.tar.gz', 'w:gz') as stream:
        member = tarfile.TarInfo(task.source_name)
        member.size = len(payload)
        stream.addfile(member, io.BytesIO(payload))
    return task, data, readme


def artifact_bytes(task):
    return (json.dumps({'schema_version': 1, 'task_id': task.task_id, 'fixture': task.fixture},
                       sort_keys=True) + '\n').encode()


class EvaluationAdapter:
    """Run trusted teaching checks locally or return an explicitly simulated outcome."""

    def __init__(self, mode, timeout):
        if mode not in ('local', 'mock') or type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError('invalid evaluation mode or timeout')
        self.mode = mode
        self.timeout = timeout

    def evaluate(self, task, artifact, root):
        start = time.monotonic()
        origin = 'local_teaching' if self.mode == 'local' else 'mock'

        def result(status, detail, code=None):
            return VerificationResult(origin, status, round(time.monotonic() - start, 4), detail, code)

        try:
            known = FIXTURES.get(task.task_id)
            if known is None or known[0] != task:
                raise ValueError('only the built-in teaching fixtures are executable')
            if artifact.path != 'test_artifact.json' or artifact.fixture != task.fixture:
                raise ValueError('unexpected teaching artifact descriptor')
            raw = read_bytes(root / artifact.path, 4096)
            if raw != artifact_bytes(task) or sha256(raw).hexdigest() != artifact.sha256:
                raise ValueError('teaching artifact content does not match the host manifest')
        except (OSError, ValueError) as exc:
            return result('invalid_artifact', str(exc))

        if self.mode == 'mock':
            return result('simulated', 'Accepted the teaching manifest; no regression executed.')

        work = root / 'evaluation'
        try:
            work.mkdir(mode=0o700)
            runner = Path(__file__).with_name('teaching_checks.py').resolve(strict=True)
            with (work / 'stdout.log').open('xb') as stdout, (work / 'stderr.log').open('xb') as stderr:
                child = subprocess.Popen([sys.executable, '-I', '-B', str(runner), task.fixture],
                                         cwd=work, env={'LC_ALL': 'C'}, stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=stderr, start_new_session=True)
                try:
                    code = child.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    return result('timeout', 'Teaching check exceeded the evaluator timeout.')
                finally:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait()
            if code == 0:
                return result('pass', 'Built-in teaching regression passed.', code)
            return result('fail' if code == 1 else 'error', 'Teaching check returned nonzero.', code)
        except OSError as exc:
            return result('error', f'{type(exc).__name__}: {exc}')


def evaluation_hook(task, adapter, root, analysis):
    raw = artifact_bytes(task)
    artifact = TestArtifact('test_artifact.json', sha256(raw).hexdigest(), task.fixture)
    with (root / artifact.path).open('xb') as stream:
        stream.write(raw)
    verification = adapter.evaluate(task, artifact, root)
    handoff = asdict(AgentResult(analysis, artifact, verification))
    save(root / 'agent_result.json', handoff)
    return {key: handoff[key] for key in ('artifact', 'verification')}


def official_status(task_id, path=None):
    if path is None:
        return {'status': 'not_evaluated', 'success': None}
    return official_record(path, task_id)
