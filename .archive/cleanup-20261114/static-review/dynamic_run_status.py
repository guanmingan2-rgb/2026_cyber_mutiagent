"""Persist Slurm stage failures even before task/agent startup."""
import hashlib
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

root, stage, state, code = sys.argv[1:]
root = Path(root)
project = Path(__file__).parent
now = datetime.now(timezone.utc).isoformat()
if state == 'completed':
    report_path = root / 'result/report.json'
    if not report_path.is_file() or json.loads(report_path.read_text()).get('chain_status') != 'completed':
        state, code = 'failed', '1'
value = {'stage': stage, 'status': state, 'exit_code': int(code),
         'updated_at': now, 'job_id': os.environ.get('SLURM_JOB_ID'),
         'node': socket.gethostname(), 'error_log': str(root.with_suffix('.log')),
         'result_report': str(root / 'result/report.json')}
with (root / 'job-events.jsonl').open('a') as f:
    f.write(json.dumps(value) + '\n')
temporary = root / 'job-status.tmp'
temporary.write_text(json.dumps(value, indent=2) + '\n')
temporary.replace(root / 'job-status.json')
manifest = root / 'code-manifest.json'
if not manifest.exists():
    files = [project / name for name in ('cybergym_dynamic.py', 'dynamic_score_backend.py',
             'dynamic_run_status.py', 'run_dynamic.sbatch', 'review.py')]
    files += list((project / 'agent_runtime').glob('*.py'))
    solver = Path('/home/ybgao/2026_cyber_agents/safegym_solver')
    files += [solver / 'scripts' / name for name in ('harness.py', 'server_guard.py',
              'slurm_runtime_check.py', 'rootless_docker.sh')]
    files += list((solver / 'cybergym/src/cybergym/server').glob('*.py'))
    manifest.write_text(json.dumps({'created_at': now, 'sha256': {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}, indent=2) + '\n')
