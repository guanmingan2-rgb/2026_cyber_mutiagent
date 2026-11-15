from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from pipeline.historical_report import import_report, normalize


class HistoricalReportTests(unittest.TestCase):
    def setUp(self):
        self.task = {'task_id': 'arvo:1', 'agent_id': 'fixture-agent', 'status': 'completed',
                     'success': True, 'final_selection': {'poc_id': 'example'},
                     'frozen_candidate': {'poc_id': 'example', 'submitted_sha256': 'a' * 64,
                                          'submitted_bytes': 4}}
        self.frozen = {**deepcopy(self.task), 'success': False}
        self.record = {'task_id': 'arvo:1', 'agent_id': 'fixture-agent', 'poc_id': 'example',
                       'poc_hash': 'a' * 64, 'poc_length': 4, 'vul_exit_code': 1,
                       'fix_exit_code': 0}

    def test_uses_explicit_final_decision(self):
        report = normalize(self.task, self.frozen, [self.record])
        self.assertIs(report['official']['success'], True)
        self.assertEqual(report['candidate']['verification'], 'official_and_selection_metadata_match')
        self.task['success'] = False
        self.record['vul_exit_code'] = 0
        self.assertIs(normalize(self.task, self.frozen, [self.record])['official']['success'], False)

    def test_preserves_null_when_unscored(self):
        self.task['success'] = False
        self.assertIsNone(normalize(self.task, self.frozen, [])['official']['success'])
        self.assertIsNone(normalize(self.task, None, [self.record])['official']['success'])
        self.task['status'] = 'infrastructure_error'
        self.assertIsNone(normalize(self.task, self.frozen, [self.record])['official']['success'])

    def test_rejects_mismatched_or_duplicate_evidence(self):
        for rows in ([self.record, self.record], [{**self.record, 'poc_hash': 'b' * 64}],
                     [{**self.record, 'agent_id': 'different'}], [{**self.record, 'poc_length': 3}],
                     [{**self.record, 'fix_exit_code': False}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                normalize(self.task, self.frozen, rows)

    def test_keeps_runtime_metrics_separate(self):
        report = normalize(self.task, self.frozen, [self.record], {'elapsed_seconds': 1.25},
                           {'request_count': 1, 'prompt_tokens': 10, 'generated_tokens': 3})
        self.assertEqual(report['metrics']['output_tokens'], 3)
        self.assertEqual(report['metrics']['walltime_sec'], 1.25)
        self.assertIsNone(report['metrics']['estimated_cost_usd'])
        with self.assertRaises(ValueError):
            normalize(self.task, usage={'request_count': True})

    def test_imports_metadata_without_reading_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'candidate.bin').mkdir()
            for name, value in [('task-result.json', self.task),
                                ('frozen-final-selection.json', self.frozen),
                                ('official-records.json', [self.record])]:
                (root / name).write_text(json.dumps(value))
            report = import_report(root)
            self.assertIs(report['official']['success'], True)
            self.assertEqual(len(report['source_metadata_sha256']), 3)
            (root / 'official-records.json').write_text('[{"poc_id": "a", "poc_id": "b"}]')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                import_report(root)
