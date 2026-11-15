"""Read-only historical metadata audit; never open or execute old candidates."""
import hashlib
import json
import sqlite3
from pathlib import Path

project = Path('/home/ybgao/2026_cyber_mutiagent')
solver = Path('/home/ybgao/2026_cyber_agents/safegym_solver')
result = {'static_runs': [], 'databases': []}
for path in sorted((project / 'runs').glob('*/report.json')):
    report = json.loads(path.read_text())
    if not isinstance(report.get('rows'), list):
        continue
    result['static_runs'].append({'path': str(path), 'model_mode': report.get('model_mode'),
        'rows': [{k: row.get(k) for k in ('task_id', 'variant', 'review_status',
                 'failure_reason', 'official_status', 'official_success')} for row in report['rows']]})
for path in (solver / 'poc.db', solver / 'server_poc/poc.db'):
    if not path.is_file():
        continue
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        counts = [dict(row) for row in db.execute('SELECT task_id, count(*) AS records, '
            'sum(vul_exit_code IS NOT NULL) AS vul_recorded, '
            'sum(fix_exit_code IS NOT NULL) AS fix_recorded FROM poc_records GROUP BY task_id')]
        rows = [dict(row) for row in db.execute('SELECT * FROM poc_records WHERE task_id IN (?,?)',
                                               ('arvo:3938', 'arvo:65996'))]
        for row in rows:
            for key in ('agent_id', 'poc_id'):
                row[key + '_fingerprint'] = hashlib.sha256(row.pop(key).encode()).hexdigest()[:16]
        result['databases'].append({'path': str(path), 'counts': counts, 'selected_metadata': rows})
print(json.dumps(result, ensure_ascii=False, indent=2))
