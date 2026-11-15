"""Reconcile immutable run evidence with the scheduler; no model calls."""
import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def classify(state, report, score):
    if state.startswith(('CANCELLED', 'FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'NODE_FAIL')):
        return 'incomplete'
    if state == 'COMPLETED':
        if (report.get('chain_status') == 'completed' and report.get('official_status') == 'completed'
                and score.get('selected_at_1', {}).get('status') == 'completed'
                and report.get('poc_id') == score.get('poc_id') and bool(score.get('poc_id'))):
            return 'completed'
        return 'result_missing_or_inconsistent'
    return 'pending' if state == 'PENDING' else 'running_or_unknown'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--job-id', required=True, type=int)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    root = Path(__file__).parent / 'runs' / f'dynamic-{a.job_id}'
    raw = subprocess.check_output(['sacct', '-j', str(a.job_id), '-X', '-n', '-P',
        '--format=JobID,State,ExitCode,NodeList,Elapsed'], text=True)
    row = next(line.split('|') for line in raw.splitlines() if line.split('|')[0] == str(a.job_id))
    state = row[1].split()[0]
    def read(name):
        path = root / name
        return json.loads(path.read_text()) if path.is_file() else {}
    report, score, job = read('result/report.json'), read('result/score.json'), read('job-status.json')
    result = {'job_id': a.job_id, 'scheduler_state': state, 'scheduler_exit_code': row[2],
        'node': row[3], 'elapsed': row[4], 'chain_status': classify(state, report, score),
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'job_status_conflict': job.get('status') == 'completed' and state != 'COMPLETED',
        'raw_sacct': raw, 'task_report': report, 'score_path': str(root / 'result/score.json')}
    with a.output.open('x') as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
