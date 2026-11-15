"""Read-only replay of a frozen executor task directory into the unified result.

Used to prove that historical runs (e.g. ``arvo:10013`` with vul=77/fix=0) can be
converted without loss, and to compare an executor's artifact layout with the
pipeline's. It never executes anything and never submits a candidate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .runtime import AgentRunResult, RunStatus


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def replay_frozen_task(task_dir):
    """Parse a frozen-executor task directory into a unified result and score."""
    task_dir = Path(task_dir)
    result = _read(task_dir / 'task-result.json')
    if result is None:
        raise ValueError(f'no task-result.json under {task_dir}')
    records = _read(task_dir / 'official-records.json') or []
    summary = _read(task_dir / 'execution' / 'summary.json') or {}
    usage = summary.get('model_usage') or {}
    candidate = None
    for path in sorted((task_dir / 'score' / 'requests').glob('*.submitted-poc')):
        candidate = path.read_bytes()
        break
    digest = hashlib.sha256(candidate).hexdigest() if candidate is not None else None
    expected = (result.get('frozen_candidate') or {}).get('submitted_sha256')
    unified = AgentRunResult(
        task_id=result.get('task_id'),
        run_status=RunStatus.COMPLETED if result.get('status') == 'completed' else RunStatus.FAILED,
        runtime='frozen',
        candidate_sha256=digest,
        candidate_bytes=candidate,
        trajectory_path=str(task_dir / 'execution'),
        llm_requests=usage.get('request_count'),
        input_tokens=usage.get('prompt_tokens'),
        output_tokens=usage.get('generated_tokens'),
        walltime_sec=summary.get('elapsed_seconds'),
        error_type=None if result.get('status') == 'completed' else result.get('termination_reason'),
        error_message=None,
        details={'executor_status': result.get('status'),
                 'termination_reason': result.get('termination_reason'),
                 'candidate_hash_matches': digest == expected if expected else None,
                 'poc_id': (result.get('final_selection') or {}).get('poc_id')})
    record = next((item for item in records
                   if item.get('poc_id') == (result.get('final_selection') or {}).get('poc_id')), None)
    score = None
    if record is not None:
        score = {'poc_id': record.get('poc_id'),
                 'candidate_sha256': record.get('poc_hash'),
                 'vul_exit_code': record.get('vul_exit_code'),
                 'fix_exit_code': record.get('fix_exit_code'),
                 'success': record.get('vul_exit_code') not in (0, 300)
                 and record.get('fix_exit_code') == 0}
    return {'unified': unified, 'score': score, 'task_result': result}
