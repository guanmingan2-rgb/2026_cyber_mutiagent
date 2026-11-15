"""Join a static CyberGym run with explicitly linked, existing score summaries."""

import argparse
from collections import defaultdict
import csv
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from review import no_duplicate_keys, save
from task_materials import official_record, read_bytes


TASK_ID = re.compile(r'(arvo|oss-fuzz):[0-9]{1,20}\Z')
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
JSON_LIMIT = 32 * 1024 * 1024


def read_json(path):
    raw = read_bytes(path, JSON_LIMIT)

    def reject_constant(value):
        raise ValueError(f'invalid JSON number: {value}')

    data = json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicate_keys,
                      parse_constant=reject_constant)
    if not isinstance(data, dict):
        raise ValueError(f'{path.name}: expected a JSON object')
    return data, sha256(raw).hexdigest()


def load_linked_records(path, run_id, report_hash, cases):
    if path is None:
        return {}, None
    links, links_hash = read_json(path)
    if (set(links) != {'schema_version', 'run_id', 'report_sha256', 'records'}
            or type(links['schema_version']) is not int or links['schema_version'] != 1
            or links['run_id'] != run_id or links['report_sha256'] != report_hash
            or not isinstance(links['records'], list)):
        raise ValueError('score links do not match this run and report digest')

    records = {}
    for link in links['records']:
        if (not isinstance(link, dict)
                or set(link) != {'task_id', 'harness_variant', 'path', 'sha256'}
                or not all(isinstance(value, str) for value in link.values())
                or not DIGEST.fullmatch(link['sha256'])):
            raise ValueError('invalid score link')
        key = (link['task_id'], link['harness_variant'])
        if key not in cases or key in records:
            raise ValueError(f'unknown or duplicate score link: {key}')
        relative = Path(link['path'])
        if (not relative.parts or relative.is_absolute() or '..' in relative.parts
                or relative.suffix != '.json'):
            raise ValueError('score paths must be relative JSON paths without parent traversal')
        record_path = path.parent / relative
        if record_path.resolve(strict=True) != record_path.absolute():
            raise ValueError('score path must not traverse symbolic links')
        record = official_record(record_path, key[0], include_exit_codes=True)
        if record['sha256'] != link['sha256']:
            raise ValueError(f'official score digest mismatch: {key}')
        if record['status'] == 'infra_error' and record['success'] is True:
            raise ValueError(f'infrastructure error carries a contradictory official decision: {key}')
        records[key] = record
    return records, links_hash


def case_key(row):
    if not isinstance(row, dict):
        raise ValueError('expected a run row')
    task_id, variant = row.get('task_id'), row.get('variant')
    if (not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id)
            or not isinstance(variant, str) or not RUN_ID.fullmatch(variant)):
        raise ValueError('invalid task ID or Harness variant')
    return task_id, variant


def case_tokens(row, roles):
    completion = row.get('generated_tokens')
    reasoning = None
    if (roles and row.get('usage_complete') is True
            and all(type(role.get('reasoning_tokens')) is int for role in roles)
            and all(type(role.get('completion_tokens')) is int for role in roles)
            and sum(role['completion_tokens'] for role in roles) == completion):
        count = sum(role['reasoning_tokens'] for role in roles)
        if 0 <= count <= completion:
            reasoning = count
    return {
        'input_tokens': row.get('prompt_tokens'),
        'reasoning_tokens': reasoning,
        'output_tokens': completion - reasoning if reasoning is not None else None,
        'completion_tokens': completion,
    }


def result_fields(record):
    success = None
    status = 'not_evaluated'
    error = None
    if record is not None:
        if record['status'] == 'completed' and type(record['success']) is bool:
            success = record['success']
            status = 'verified_successful' if success else 'verified_unsuccessful'
        else:
            status = 'verification_error'
            error = ('official_infra_error' if record['status'] == 'infra_error'
                     else 'official_result_missing_decision')
    return {
        'evaluation_mode': 'official_result_import' if record is not None else 'static_review',
        'evaluation_status': status,
        'verification_completed': record is not None and record['status'] == 'completed',
        'verification_executed_locally': False,
        'official_success': success,
        'vul_exit_code': record.get('vul_exit_code') if record else None,
        'fix_exit_code': record.get('fix_exit_code') if record else None,
        'verification_error_reason': error,
        'official_record_path': record['source'] if record else None,
        'official_record_sha256': record['sha256'] if record else None,
    }


def report_rows(report_path, manifest_path, links_path=None):
    report, report_hash = read_json(report_path)
    manifest, manifest_hash = read_json(manifest_path)
    run_id = report_path.absolute().parent.name
    if not RUN_ID.fullmatch(run_id):
        raise ValueError('invalid source run directory name')
    rows = report.get('rows')
    roles = report.get('roles', [])
    if not isinstance(rows, list) or not rows or not isinstance(roles, list):
        raise ValueError('expected an existing run report with task rows')
    tasks, variants = manifest.get('tasks'), manifest.get('variants')
    profile = manifest.get('profile')
    model = profile.get('model') if isinstance(profile, dict) else None
    model_id = model.get('served_name') if isinstance(model, dict) else None
    if (not isinstance(tasks, dict) or not isinstance(variants, list)
            or not isinstance(model_id, str) or not model_id
            or report.get('model_mode') not in ('deepseek_api', 'scripted')
            or report.get('model_mode') != manifest.get('model_mode')):
        raise ValueError('manifest is incompatible with the report')

    cases = {}
    for row in rows:
        task_id, variant = key = case_key(row)
        if (key in cases or task_id not in tasks or variant not in variants
                or tasks[task_id] != row.get('source')):
            raise ValueError(f'unknown or duplicate report case: {key}')
        cases[key] = row
    role_groups = defaultdict(list)
    for role in roles:
        key = case_key(role)
        if key not in cases:
            raise ValueError(f'role metrics are not associated with a report case: {key}')
        role_groups[key].append(role)
    records, links_hash = load_linked_records(links_path, run_id, report_hash, cases)

    results = []
    for key, row in cases.items():
        task_id, variant = key
        result = result_fields(records.get(key))
        agent_error = row.get('failure_reason')
        snapshot = report_path.parent / 'tasks' / task_id.replace(':', '-') / variant / 'task_snapshot.json'
        snapshot_hash = sha256(read_bytes(snapshot, 1024 * 1024)).hexdigest() if snapshot.is_file() else None
        results.append({
            'run_id': run_id,
            'task_id': task_id,
            'task_type': task_id.split(':', 1)[0],
            'model_id': model_id,
            'model_mode': report['model_mode'],
            'harness_variant': variant,
            'agent_completed': row.get('review_status') == 'completed',
            'artifact_produced': False,
            'artifact_status': 'artifact_missing',
            'artifact_valid': None,
            'submission_accepted': None,
            **result,
            **case_tokens(row, role_groups[key]),
            'sdk_requests': row.get('sdk_requests'),
            'upstream_attempts': row.get('upstream_attempts'),
            'estimated_cost_usd': row.get('estimated_cost_usd'),
            'elapsed_seconds': row.get('elapsed_seconds'),
            'error_reason': result['verification_error_reason'] or agent_error,
            'agent_error_reason': agent_error,
            'source_snapshot_sha256': snapshot_hash,
        })
    return {
        'schema_version': 1,
        'run_id': run_id,
        'source_report': {'path': str(report_path.absolute()), 'sha256': report_hash},
        'source_manifest': {'path': str(manifest_path.absolute()), 'sha256': manifest_hash},
        'score_links': ({'path': str(links_path.absolute()), 'sha256': links_hash} if links_path else None),
        'rows': results,
    }


def safe_csv_cell(value):
    if isinstance(value, str) and (value[:1] in ('\r', '\n', '\t')
                                  or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


def write_report(output, report):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    json_path, csv_path = output / 'report.json', output / 'results.csv'
    try:
        rows = report['rows']
        with csv_path.open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows({key: safe_csv_cell(value) for key, value in row.items()} for row in rows)
        save(json_path, report)
    except OSError:
        json_path.unlink(missing_ok=True)
        csv_path.unlink(missing_ok=True)
        output.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True, help='existing static run report.json')
    parser.add_argument('--manifest', type=Path, help='defaults to manifest.json beside the report')
    parser.add_argument('--links', type=Path, help='optional SHA-256-bound external result links')
    parser.add_argument('--output', type=Path, required=True, help='new directory for JSON and CSV')
    args = parser.parse_args()
    try:
        manifest = args.manifest or args.report.with_name('manifest.json')
        write_report(args.output, report_rows(args.report, manifest, args.links))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
