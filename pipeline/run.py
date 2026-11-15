"""CyberGym end-to-end pipeline: read materials → agent runtime → official score.

The pipeline owns task preparation, runtime selection, candidate validation,
official scoring and reporting. Agent behaviour lives behind ``pipeline.runtime``:

``--runtime oneshot``  single DeepSeek session, no tools (original behaviour)
``--runtime frozen``   the frozen multi-turn executor with bash, called as a
                       subprocess (``2026_cyber_agents``), no scoring socket

Stages::

    prepare_task  official make_task workspace
    read_inputs   bounded, hashed materials
    build context + RuntimeConfig
    runner.run    AgentRunResult (candidate + metrics + status)
    score         official /submit-vul + /submit-fix + /query-poc
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from .common import save
from .data import load_task_materials, read_workspace_texts
from .runtime import RuntimeConfig, RunStatus, TaskContext, get_runner
from .runtimes.oneshot import (MAX_POC_BYTES, SMOKE_INSTRUCTION, SOLVE_INSTRUCTION,
                               decode_candidate, validate_smoke, validate_solve)

SOLVER = Path('/home/ybgao/2026_cyber_agents/safegym_solver')
SCORER = Path(__file__).with_name('scoring.py')
DEFAULT_SOLVE_SOURCE_BYTES = 256 * 1024

__all__ = ['SMOKE_INSTRUCTION', 'SOLVE_INSTRUCTION', 'MAX_POC_BYTES',
           'validate_smoke', 'validate_solve', 'decode_candidate',
           'prepare_task', 'read_inputs', 'read_solve_inputs', 'score_candidate',
           'run_pipeline', 'main']


def _scorer_python():
    return str(SOLVER / '.venv/bin/python')


def _scorer(action, task_id, data_root, output, server, timeout):
    command = [_scorer_python(), str(SCORER), action,
               '--task-id', task_id, '--data-root', str(data_root),
               '--output', str(output), '--server', server]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        tail = detail[-1] if detail else f'exit {result.returncode}'
        raise RuntimeError(f'official scorer {action} failed: {tail}')


def prepare_task(task_id, data_root, output, server):
    """Stage 1a: official task generation into ``<output>/task``."""
    _scorer('prepare', task_id, data_root, output, server, timeout=180)
    return Path(output) / 'task'


def read_inputs(task_dir):
    """Stage 1b (smoke): bounded, hashed text handed to the agent."""
    payload, hashes = read_workspace_texts(task_dir)
    for name, text in payload.items():
        if len(text.encode()) > 32768:
            raise ValueError('Task text exceeds input bound')
    return payload, hashes


def read_solve_inputs(task_id, data_root, sources, max_source_bytes, task_dir=None):
    """Stage 1b (solve): description, README and the named vulnerable sources.

    The frozen runtime receives the whole ``repo-vul.tar.gz`` inside its workspace,
    so it does not need a source allowlist; when ``sources`` is empty only the task
    text is materialised.
    """
    if not sources:
        payload, hashes = read_workspace_texts(Path(task_dir))
        return ({"task_id": task_id, "description": payload["description.txt"],
                 "readme": payload["README.md"], "sources": {}}, hashes)
    materials = load_task_materials(data_root, task_id, sources, max_source_bytes=max_source_bytes)
    payload = {'task_id': task_id,
               'description': materials['description']['text'],
               'readme': materials['readme']['text'],
               'sources': {name: record['text'] for name, record in materials['sources'].items()}}
    hashes = {'description.txt': materials['description']['sha256'],
              'README.md': materials['readme']['sha256']}
    hashes.update({name: record['sha256'] for name, record in materials['sources'].items()})
    return payload, hashes


def build_context(task_id, task_dir, data_root, mode, sources, max_source_bytes):
    """Materialise the TaskContext handed to a runtime."""
    if mode == 'solve':
        payload, hashes = read_solve_inputs(task_id, data_root, sources, max_source_bytes,
                                            task_dir=task_dir)
        return TaskContext(task_id=task_id, task_dir=Path(task_dir), data_root=Path(data_root),
                           mode=mode, description=payload['description'], readme=payload['readme'],
                           sources=payload['sources'], input_manifest=hashes)
    payload, hashes = read_inputs(task_dir)
    return TaskContext(task_id=task_id, task_dir=Path(task_dir), data_root=Path(data_root),
                       mode=mode, description=payload['description.txt'],
                       readme=payload['README.md'], input_manifest=hashes)


def score_candidate(task_id, data_root, output, server):
    """Stage 3: official vulnerable/fixed execution of ``candidate.bin``."""
    _scorer('score', task_id, data_root, output, server, timeout=420)
    return json.loads((Path(output) / 'score.json').read_text())


def _runtime_from_args(args, output):
    options = {}
    if args.runtime.startswith('frozen'):
        options = dict(code_root=args.frozen_root, harness_config=args.frozen_config,
                       profile=args.frozen_profile, dry_run=args.frozen_dry_run,
                       score_port=urlsplit(args.server).port or 18666)
        return get_runner(args.runtime, **options)
    return get_runner(args.runtime)


def run_pipeline(task_id, data_root, output, *, server='http://127.0.0.1:18666',
                 agent_timeout=300, python=None, mode='smoke', sources=(),
                 max_source_bytes=None, runtime='oneshot', runner=None, scorer=None,
                 prepare=None):
    """Run all stages and always write ``report.json``, even on failure."""
    output = Path(output).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    started = time.monotonic()
    report = {'task_id': task_id,
              'scope': 'solve' if mode == 'solve' else 'integration_smoke',
              'mode': mode, 'runtime': runtime, 'model_mode': 'live',
              'chain_status': 'failed', 'official_status': 'not_evaluated',
              'agent_status': 'not_started', 'submission_status': 'not_submitted',
              'success': None, 'stage': 'task_prepare', 'sources': list(sources)}
    try:
        # Frozen native protocol: submit.sh must reach the loopback score relay,
        # exactly like the formal runner (127.0.0.1:8667 inside the sandbox).
        prepare_server = 'http://127.0.0.1:8667' if runtime == 'frozen' else server
        task_dir = (prepare or prepare_task)(task_id, data_root, output, prepare_server)
        context = build_context(task_id, task_dir, data_root, mode, sources,
                                max_source_bytes or DEFAULT_SOLVE_SOURCE_BYTES)
        save(output / 'input-manifest.json',
             {'data_root': str(data_root), 'sha256': context.input_manifest})
        config = RuntimeConfig(runtime=runtime, agent_timeout=agent_timeout,
                               output_dir=output, tools=('bash',) if runtime == 'frozen' else ())
        save(output / 'runtime-config.json',
             {'runtime': runtime, 'agent_timeout': agent_timeout, 'mode': mode,
              'sources': list(sources), 'tools': list(config.tools)})

        report['stage'] = 'agent_call'
        if runner is None:
            if runtime == 'frozen':
                # The feedback gateway must forward to the actual scoring service port,
                # which is allocated per Slurm job by run_pipeline.sbatch.
                runner = get_runner(runtime, score_port=urlsplit(server).port or 18666)
            else:
                runner = get_runner(runtime)
        result = runner.run(context, config)
        report.update(runtime_status=result.run_status,
                      agent_requests=result.llm_requests,
                      input_tokens=result.input_tokens,
                      output_tokens=result.output_tokens,
                      agent_walltime_sec=result.walltime_sec,
                      agent_error_type=result.error_type,
                      agent_error=result.error_message)
        save(output / 'agent' / 'runtime-result.json', {
            'task_id': result.task_id, 'runtime': result.runtime,
            'run_status': result.run_status, 'candidate_sha256': result.candidate_sha256,
            'llm_requests': result.llm_requests, 'input_tokens': result.input_tokens,
            'output_tokens': result.output_tokens, 'walltime_sec': result.walltime_sec,
            'error_type': result.error_type, 'error_message': result.error_message,
            'details': result.details})

        if result.run_status != RunStatus.COMPLETED or not result.candidate_bytes:
            raise _RuntimeFailure(result)

        candidate = result.candidate_bytes
        (output / 'candidate.bin').write_bytes(candidate)
        digest = hashlib.sha256(candidate).hexdigest()
        if digest != result.candidate_sha256:
            raise ValueError('candidate hash mismatch between runtime and pipeline')
        save(output / 'final-selection.json', {
            'task_id': task_id, 'runtime': runtime, 'candidate_sha256': digest,
            'candidate_bytes': len(candidate), 'origin': f'{runtime}_runtime'})
        report.update(agent_status='completed', candidate_sha256=digest,
                      candidate_bytes=len(candidate))

        report.update(stage='official_scoring', submission_status='attempted',
                      official_status='pending')
        score = (scorer or score_candidate)(task_id, data_root, output, server)
        selected = score['selected_at_1']
        report.update(chain_status='completed', official_status='completed', stage='completed',
                      submission_status='accepted', selected_at_1=selected,
                      poc_id=score['poc_id'], success=selected.get('success'))
    except _RuntimeFailure as exc:
        result = exc.result
        report.update(error_type=result.error_type or 'RuntimeFailure',
                      error=result.error_message or result.run_status,
                      agent_status=_agent_status(result.run_status),
                      chain_status='failed')
    except Exception as exc:  # noqa: BLE001 — report must survive any stage failure
        report.update(error_type=type(exc).__name__, error=str(exc))
        if report['stage'] == 'agent_call':
            report['agent_status'] = 'failed'
        if report['stage'] == 'official_scoring':
            report['official_status'] = 'infra_error'
        report['success'] = None
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    save(output / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report


class _RuntimeFailure(Exception):
    def __init__(self, result):
        super().__init__(result.error_message or result.run_status)
        self.result = result


def _agent_status(status):
    return {RunStatus.COMPLETED: 'completed', RunStatus.TIMEOUT: 'timeout',
            RunStatus.TRUNCATED: 'truncated', RunStatus.NO_CANDIDATE: 'no_candidate',
            RunStatus.INFRA_ERROR: 'infra_error'}.get(status, 'failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--data-root', type=Path, default=SOLVER / 'cybergym_data/data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--server', default='http://127.0.0.1:18666')
    parser.add_argument('--agent-timeout', type=int, default=300)
    parser.add_argument('--mode', choices=('smoke', 'solve'), default='smoke')
    parser.add_argument('--runtime', choices=('oneshot', 'frozen', 'frozen-nofeedback'),
                        default='oneshot')
    parser.add_argument('--source', action='append', default=[],
                        help='repo-vul.tar.gz member, repeatable (solve mode)')
    parser.add_argument('--max-source-bytes', type=int, default=None)
    parser.add_argument('--frozen-root', default=None)
    parser.add_argument('--frozen-config', default=None)
    parser.add_argument('--frozen-profile', default=None)
    parser.add_argument('--frozen-dry-run', action='store_true',
                        help='only build the frozen executor command; do not run it')
    args = parser.parse_args(argv)
    if args.mode == 'solve' and args.runtime == 'oneshot' and not args.source:
        parser.error('--mode solve --runtime oneshot requires --source')
    report = run_pipeline(args.task_id, args.data_root, args.output,
                          server=args.server, agent_timeout=args.agent_timeout,
                          mode=args.mode, sources=tuple(args.source),
                          max_source_bytes=args.max_source_bytes, runtime=args.runtime,
                          runner=_runtime_from_args(args, args.output) if args.runtime == 'frozen' else None)
    return 0 if report['chain_status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
