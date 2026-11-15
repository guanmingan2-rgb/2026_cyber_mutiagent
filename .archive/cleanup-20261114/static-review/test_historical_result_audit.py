import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from historical_result_audit import audit_records


class HistoricalResultAuditTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.records = Path(tmp.name) / 'official-records.json'
        self.selection = Path(tmp.name) / 'final_submission.json'
        self.record = {'task_id': 'arvo:1', 'agent_id': 'test-agent', 'poc_id': 'test-record',
                       'poc_hash': 'a' * 64, 'poc_length': 3, 'vul_exit_code': 1, 'fix_exit_code': 0}
        self.records.write_text(json.dumps([self.record]), encoding='utf-8')
        self.selection.write_text(json.dumps({'poc_id': 'test-record'}), encoding='utf-8')

    def test_historical_selection_cannot_create_score(self):
        result = audit_records(self.records, self.selection, expected_task='arvo:1',
                               expected_agent='test-agent')
        self.assertTrue(result['records'][0]['final_selection_matches'])
        self.assertIsNone(result['records'][0]['official_success'])
        self.assertNotIn('poc_id', result['records'][0])

    def test_task_and_agent_mismatch(self):
        for criteria in ({'expected_task': 'arvo:2'}, {'expected_agent': 'wrong-agent'}):
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                audit_records(self.records, **criteria)

    def test_unmatched_final_selection(self):
        self.selection.write_text(json.dumps({'poc_id': 'another-record'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'no matching record'):
            audit_records(self.records, self.selection)

    def test_duplicate_id_and_boolean_exit_rejected(self):
        self.records.write_text(json.dumps([self.record, self.record]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate historical'):
            audit_records(self.records)
        self.records.write_text(json.dumps([{**self.record, 'vul_exit_code': False}]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'exit code'):
            audit_records(self.records)
