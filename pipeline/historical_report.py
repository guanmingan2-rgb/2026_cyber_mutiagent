"""Normalize existing Frozen Executor records without executing a runtime."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

from .common import no_duplicate_keys, save
from .data import check_task_id, read_bytes

METADATA_LIMIT = 16 * 1024 * 1024


def _reject_constant(value):
    raise ValueError(f'invalid JSON constant: {value}')


def _read_object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be a JSON object')
    return value


def _require_id(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label} must be a nonempty string')
    return value


def _optional_number(value, label, *, integer=False):
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    if not integer and type(value) is float and math.isfinite(value) and value >= 0:
        return value
    raise ValueError(f'invalid {label}')


def _valid_exit(code):
    return type(code) is int and (0 <= code <= 255 or code == 300) and code not in (126, 127, 137)


def normalize(task_result, frozen_selection=None, records=None, execution=None, usage=None):
    """Keep official decisions explicit and require corroborating saved metadata."""
    task = _read_object(task_result, 'task result')
    task_id = check_task_id(_require_id(task.get('task_id'), 'task ID'))
    agent_id = _require_id(task.get('agent_id'), 'agent ID')
    status = _require_id(task.get('status'), 'runtime status')
    decision = task.get('success')
    if decision is not None and type(decision) is not bool:
        raise ValueError('success must be boolean or null')
    selection = task.get('final_selection')
    official = {'status': 'not_evaluated', 'success': None, 'vul_exit_code': None,
                'fix_exit_code': None}
    candidate = None
    if selection is not None:
        selected = _read_object(selection, 'final selection')
        poc_id = _require_id(selected.get('poc_id'), 'selected candidate ID')
        submitted = _read_object(task.get('frozen_candidate'), 'candidate metadata')
        sha256 = submitted.get('submitted_sha256')
        size = _optional_number(submitted.get('submitted_bytes'), 'candidate length', integer=True)
        if (submitted.get('poc_id') != poc_id or not isinstance(sha256, str)
                or not re.fullmatch(r'[0-9a-f]{64}', sha256) or not size):
            raise ValueError('invalid candidate metadata')
        candidate = {'id': poc_id, 'sha256': sha256, 'bytes': size,
                     'verification': 'task_result_metadata_only'}
        if frozen_selection is not None:
            frozen = _read_object(frozen_selection, 'frozen final selection')
            frozen_candidate = _read_object(frozen.get('frozen_candidate'), 'frozen candidate')
            if (frozen.get('task_id') != task_id or frozen.get('agent_id') != agent_id
                    or frozen.get('final_selection') != selected
                    or any(frozen_candidate.get(k) != submitted[k]
                           for k in ('poc_id', 'submitted_sha256', 'submitted_bytes'))):
                raise ValueError('frozen final selection does not match task result')
            candidate['verification'] = 'selection_metadata_matches'
        if records is not None:
            if not isinstance(records, list):
                raise ValueError('official records must be a JSON array')
            matched = [r for r in records if _read_object(r, 'official record').get('poc_id') == poc_id]
            if len(matched) > 1:
                raise ValueError('duplicate official candidate records')
            if matched:
                record = matched[0]
                if (record.get('task_id') != task_id or record.get('agent_id') != agent_id
                        or record.get('poc_hash') != sha256
                        or type(record.get('poc_length')) is not int or record['poc_length'] != size):
                    raise ValueError('official candidate metadata does not match task result')
                vul, fix = record.get('vul_exit_code'), record.get('fix_exit_code')
                if any(code is not None and type(code) is not int for code in (vul, fix)):
                    raise ValueError('official exit codes must be integer or null')
                official.update(vul_exit_code=vul, fix_exit_code=fix)
                if frozen_selection is not None:
                    candidate['verification'] = 'official_and_selection_metadata_match'
                if (frozen_selection is not None and status == 'completed'
                        and task.get('accounting_failed') is not True
                        and _valid_exit(vul) and _valid_exit(fix) and decision is not None):
                    official.update(status='completed', success=decision)
        if official['status'] != 'completed':
            official['status'] = 'unverified'
    elif decision is True:
        raise ValueError('a success claim requires a final selection')

    execution = _read_object(execution, 'execution summary') if execution is not None else {}
    usage = _read_object(usage, 'usage summary') if usage is not None else {}
    return {
        'schema_version': 1,
        'source': 'frozen_executor_history',
        'task_id': task_id,
        'agent_id': agent_id,
        'runtime_status': status,
        'runtime_engine_status': execution.get('status'),
        'termination_reason': task.get('termination_reason'),
        'candidate': candidate,
        'official': official,
        'metrics': {
            'llm_requests': _optional_number(usage.get('request_count'), 'requests', integer=True),
            'input_tokens': _optional_number(usage.get('prompt_tokens'), 'input tokens', integer=True),
            'output_tokens': _optional_number(usage.get('generated_tokens'), 'output tokens', integer=True),
            'walltime_sec': _optional_number(execution.get('elapsed_seconds'), 'runtime seconds'),
            'estimated_cost_usd': None,
        },
    }


def import_report(task_directory):
    """Read allowlisted JSON metadata and record the input digests."""
    root = Path(task_directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('task directory must be a directory')
    digests = {}

    def read(relative, *, required=False):
        path = root / relative
        if path.resolve() != path:
            raise ValueError(f'unexpected metadata path: {relative}')
        try:
            raw = read_bytes(path, METADATA_LIMIT)
        except FileNotFoundError:
            if required:
                raise
            return None
        digests[relative] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw, object_pairs_hook=no_duplicate_keys, parse_constant=_reject_constant)

    task = read('task-result.json', required=True)
    frozen = read('frozen-final-selection.json')
    records = read('official-records.json')
    execution = read('execution/summary.json')
    usage = read('execution/model-usage.json')
    if usage is not None:
        usage = _read_object(usage, 'model usage').get('summary')
    report = normalize(task, frozen, records, execution, usage)
    report['source_metadata_sha256'] = digests
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task_directory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = args.task_directory.resolve(strict=True)
        output = args.output.resolve()
        if output == root or root in output.parents:
            raise ValueError('write the report outside the archived task directory')
        save(output, import_report(root))
    except (OSError, ValueError) as exc:
        parser.exit(1, f'{exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
