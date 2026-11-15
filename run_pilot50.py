#!/usr/bin/env python3
"""Dispatch the frozen 50-task pilot through run_pipeline.sbatch.

One Slurm job per task, capped concurrency, resumable state file, and a summary
that separates infrastructure failures from evaluated tasks. It never scores
anything itself: each job writes its own official result under
``runs/pipeline-<job>/result/report.json``.

Usage::

    python run_pilot50.py --limit 6 --runtime frozen
    python run_pilot50.py --report
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = Path('/home/ybgao/2026_cyber_agents/ops/pilot50_task_manifest.json')
STATE = 'pilot50/state.json'
SUMMARY = 'pilot50/summary.json'
# Nodes known to fail the runtime checks (drain, cgroup bug, full /dev/shm).
EXCLUDE = 'gpu01,gpu02,gpu07,gpu09,gpu10,gpu12,gpu13,gpu15,gpu16'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def save(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def submit(task_id, runtime, timeout):
    command = ['sbatch', '--parsable', f'--exclude={EXCLUDE}',
               '--export=ALL,SAFEGYM_PIPELINE_MODE=binary',
               str(ROOT / 'run_pipeline.sbatch'), task_id,
               '--mode', 'solve', '--runtime', runtime,
               '--agent-timeout', str(timeout)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or 'sbatch failed')
    return result.stdout.strip().split(';')[0]


def running_jobs():
    result = subprocess.run(['squeue', '-u', 'ybgao', '-h', '-o', '%i %T'],
                            capture_output=True, text=True)
    jobs = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            jobs[parts[0]] = parts[1]
    return jobs


def classify(report):
    if not report:
        return 'infra_missing_report'
    if report.get('official_status') == 'completed' and report.get('success') is True:
        return 'solved'
    if report.get('official_status') == 'completed' and report.get('success') is False:
        return 'unsolved'
    status = report.get('runtime_status') or report.get('agent_status')
    return {None: 'infra_unknown', 'no_candidate': 'no_candidate',
            'truncated': 'truncated', 'timeout': 'timeout',
            'infra_error': 'infra_error', 'failed': 'agent_failed'}.get(status, 'infra_other')


def summarize(state):
    rows = []
    for task_id, item in sorted(state['tasks'].items()):
        report_path = Path(item.get('result', '')) / 'report.json'
        report = load(report_path, None) if report_path.is_file() else None
        row = {'task_id': task_id, 'job': item.get('job'), 'phase': item.get('phase'),
               'category': classify(report), 'success': (report or {}).get('success'),
               'requests': (report or {}).get('agent_requests'),
               'input_tokens': (report or {}).get('input_tokens'),
               'output_tokens': (report or {}).get('output_tokens'),
               'walltime_sec': (report or {}).get('elapsed_seconds')}
        rows.append(row)
    evaluated = [r for r in rows if r['category'] in ('solved', 'unsolved')]
    solved = [r for r in evaluated if r['category'] == 'solved']
    summary = {
        'generated_at': now(),
        'tasks_total': len(rows),
        'evaluated': len(evaluated),
        'solved': len(solved),
        'success_rate_valid': round(len(solved) / len(evaluated), 4) if evaluated else None,
        'invalid_or_infra': len(rows) - len(evaluated),
        'categories': {c: sum(1 for r in rows if r['category'] == c)
                       for c in sorted({r['category'] for r in rows})},
        'rows': rows,
    }
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--runtime', default='frozen')
    parser.add_argument('--agent-timeout', type=int, default=14400)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--report', action='store_true')
    args = parser.parse_args()

    state_path = ROOT / STATE
    state = load(state_path, {'tasks': {}, 'created_at': now()})
    if args.report:
        summary = summarize(state)
        save(ROOT / SUMMARY, summary)
        print(json.dumps({k: v for k, v in summary.items() if k != 'rows'}, ensure_ascii=False))
        return
    tasks = [item['task_id'] for item in json.loads(args.manifest.read_text())['tasks']]
    for task_id in tasks:
        state['tasks'].setdefault(task_id, {'phase': 'pending'})
    active = {job for job, text in running_jobs().items()
              if text in ('PENDING', 'RUNNING', 'CONFIGURING', 'COMPLETING')}
    save(state_path, state)
    while True:
        jobs = running_jobs()
        for task_id, item in state['tasks'].items():
            if item.get('phase') != 'submitted':
                continue
            job = item['job']
            if job not in jobs:
                carried = Path(ROOT / 'runs' / f'pipeline-{job}' / 'result') / 'report.json'
                log = Path(ROOT / 'runs' / f'pipeline-{job}.log')
                quick = False
                if not carried.is_file() and log.is_file():
                    tail = log.read_text(errors='ignore')[-2000:]
                    quick = any(marker in tail for marker in
                                ('Slurm 运行时核查失败', '磁盘余量不足', 'cgroup', 'address already in use'))
                attempts = item.get('attempts', 0) + 1
                if quick and attempts <= 3:
                    state['tasks'][task_id] = {'phase': 'pending', 'attempts': attempts}
                    item = state['tasks'][task_id]
                    continue
                item['phase'] = 'finished'
                item['attempts'] = attempts
                item['finished_at'] = now()
                item['result'] = str(ROOT / 'runs' / f'pipeline-{job}' / 'result')
        todo = [t for t in tasks if state['tasks'][t].get('phase') == 'pending']
        submitted = [t for t in tasks if state['tasks'][t].get('phase') == 'submitted']
        while todo and len(submitted) < args.limit:
            task_id = todo.pop(0)
            job = submit(task_id, args.runtime, args.agent_timeout)
            previous = state['tasks'].get(task_id, {})
            state['tasks'][task_id] = {'phase': 'submitted', 'job': job,
                                       'submitted_at': now(),
                                       'attempts': previous.get('attempts', 0)}
            submitted.append(task_id)
            print(f'submitted {task_id} -> {job}', flush=True)
        save(state_path, state)
        if not todo and not submitted:
            break
        time.sleep(args.interval)
    summary = summarize(state)
    save(ROOT / SUMMARY, summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'rows'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
