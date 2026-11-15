"""Audit historical CyberGym record metadata without assigning experiment scores."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

from review import no_duplicate_keys, save
from task_materials import read_bytes


METADATA_LIMIT = 8 * 1024 * 1024
TASK_ID = re.compile(r'(arvo|oss-fuzz):[0-9]{1,20}')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}')
SHA256 = re.compile(r'[0-9a-fA-F]{64}')


def read_metadata(path):
    raw = read_bytes(path, METADATA_LIMIT)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicate_keys), {
        'path': str(path.absolute()), 'sha256': hashlib.sha256(raw).hexdigest(),
    }


def fingerprint(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def audit_records(records_path, selection_path=None, *, expected_task=None, expected_agent=None):
    records, source = read_metadata(records_path)
    if not isinstance(records, list):
        raise ValueError('historical record file must contain a list')

    selected_id = None
    selection = None
    if selection_path is not None:
        value, selection = read_metadata(selection_path)
        if (not isinstance(value, dict) or set(value) != {'poc_id'}
                or not isinstance(value['poc_id'], str) or not IDENTIFIER.fullmatch(value['poc_id'])):
            raise ValueError('invalid historical final selection')
        selected_id = value['poc_id']

    rows = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('expected a historical record object')
        task, agent, artifact = (record.get(key) for key in ('task_id', 'agent_id', 'poc_id'))
        if (not isinstance(task, str) or not TASK_ID.fullmatch(task)
                or not isinstance(agent, str) or not IDENTIFIER.fullmatch(agent)
                or not isinstance(artifact, str) or not IDENTIFIER.fullmatch(artifact)):
            raise ValueError('invalid task, agent or artifact identifier')
        if expected_task is not None and task != expected_task:
            raise ValueError('historical task mismatch')
        if expected_agent is not None and agent != expected_agent:
            raise ValueError('historical agent mismatch')
        if artifact in seen:
            raise ValueError('duplicate historical artifact ID')
        seen.add(artifact)

        digest = record.get('poc_hash')
        size = record.get('poc_length')
        codes = {key: record.get(key) for key in ('vul_exit_code', 'fix_exit_code')}
        if (not isinstance(digest, str) or not SHA256.fullmatch(digest)
                or type(size) is not int or size < 0
                or any(code is not None and type(code) is not int for code in codes.values())):
            raise ValueError('invalid historical hash, size or exit code')
        rows.append({
            'task_id': task, 'agent_id_fingerprint': fingerprint(agent),
            'artifact_id_fingerprint': fingerprint(artifact),
            'recorded_artifact_bytes': size, **codes,
            'final_selection_matches': None if selected_id is None else artifact == selected_id,
            'official_success': None,
        })

    if selected_id is not None and selected_id not in seen:
        raise ValueError('historical final selection has no matching record')
    return {
        'kind': 'historical_metadata_audit', 'source': source,
        'selection_source': selection, 'records': rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=Path, required=True)
    parser.add_argument('--selection', type=Path)
    parser.add_argument('--expected-task')
    parser.add_argument('--expected-agent')
    parser.add_argument('--output', type=Path, required=True, help='new directory for the audit report')
    args = parser.parse_args()
    try:
        report = audit_records(args.records, args.selection, expected_task=args.expected_task,
                               expected_agent=args.expected_agent)
        args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        save(args.output / 'report.json', report)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
