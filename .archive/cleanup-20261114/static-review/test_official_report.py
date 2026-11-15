"""Exercise the offline import contract with temporary synthetic result records."""

import csv
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from official_report import report_rows, write_report


def write_json(path, value):
    raw = json.dumps(value).encode('utf-8')
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


class OfflineReportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.report = self.root / 'report.json'
        self.manifest = self.root / 'manifest.json'
        self.links = self.root / 'links.json'
        self.task = 'arvo:100'
        row = {'task_id': self.task, 'variant': 'full', 'source': 'src-vul/sample.c',
               'review_status': 'completed', 'official_success': True, 'prompt_tokens': 100,
               'generated_tokens': 50, 'usage_complete': True}
        role = {'task_id': self.task, 'variant': 'full', 'reasoning_tokens': 35, 'completion_tokens': 50}
        self.report_sha = write_json(self.report, {'rows': [row], 'roles': [role],
                                                   'model_mode': 'deepseek_api'})
        write_json(self.manifest, {'tasks': {self.task: row['source']}, 'variants': ['full'],
                                   'model_mode': 'deepseek_api',
                                   'profile': {'model': {'served_name': 'deepseek-flash'}}})

    def make_links(self, score, *, task=None, run_id=None, report_sha=None):
        score_path = self.root / 'score.json'
        score_sha = write_json(score_path, {'task_id': task or self.task, 'selected_at_1': score})
        write_json(self.links, {
            'schema_version': 1, 'run_id': run_id or self.root.name,
            'report_sha256': report_sha or self.report_sha,
            'records': [{'task_id': self.task, 'harness_variant': 'full',
                         'path': score_path.name, 'sha256': score_sha}],
        })
        return score_path

    def test_static_report_never_asserts_a_score(self):
        merged = report_rows(self.report, self.manifest)
        row = merged['rows'][0]
        self.assertTrue(row['agent_completed'])
        self.assertFalse(row['artifact_produced'])
        self.assertEqual(row['artifact_status'], 'artifact_missing')
        self.assertFalse(row['verification_completed'])
        self.assertFalse(row['verification_executed_locally'])
        self.assertIsNone(row['submission_accepted'])
        self.assertIsNone(row['official_success'])
        self.assertEqual(row['evaluation_status'], 'not_evaluated')
        self.assertEqual((row['input_tokens'], row['reasoning_tokens'], row['output_tokens'],
                          row['completion_tokens']), (100, 35, 15, 50))
        destination = self.root / 'offline'
        write_report(destination, merged)
        with (destination / 'results.csv').open(encoding='utf-8') as stream:
            csv_row, = csv.DictReader(stream)
        self.assertEqual(csv_row['evaluation_status'], 'not_evaluated')
        self.assertEqual(csv_row['official_success'], '')

    def test_explicit_official_decision_and_exit_codes(self):
        self.make_links({'status': 'completed', 'success': False, 'vul_exit_code': 1, 'fix_exit_code': 0})
        row = report_rows(self.report, self.manifest, self.links)['rows'][0]
        self.assertFalse(row['official_success'])
        self.assertEqual(row['evaluation_status'], 'verified_unsuccessful')
        self.assertTrue(row['verification_completed'])
        self.assertFalse(row['verification_executed_locally'])
        self.assertEqual((row['vul_exit_code'], row['fix_exit_code']), (1, 0))
        self.assertIsNotNone(row['official_record_sha256'])
        self.make_links({'status': 'completed', 'success': True})
        row = report_rows(self.report, self.manifest, self.links)['rows'][0]
        self.assertTrue(row['official_success'])
        self.assertEqual(row['evaluation_status'], 'verified_successful')
        self.assertIsNone(row['fix_exit_code'])

    def test_infra_error_keeps_official_success_null(self):
        self.make_links({'status': 'infra_error', 'success': False})
        row = report_rows(self.report, self.manifest, self.links)['rows'][0]
        self.assertEqual(row['evaluation_status'], 'verification_error')
        self.assertIsNone(row['official_success'])
        self.assertFalse(row['verification_completed'])

    def test_rejects_mismatched_run_report_and_score_digests(self):
        score = {'status': 'completed', 'success': False}
        self.make_links(score, run_id='different-run')
        with self.assertRaisesRegex(ValueError, 'run and report digest'):
            report_rows(self.report, self.manifest, self.links)
        self.make_links(score, report_sha='0' * 64)
        with self.assertRaisesRegex(ValueError, 'run and report digest'):
            report_rows(self.report, self.manifest, self.links)
        score_path = self.make_links(score)
        score_path.write_bytes(score_path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'score digest mismatch'):
            report_rows(self.report, self.manifest, self.links)

    def test_rejects_wrong_task_invalid_exit_and_symbolic_link(self):
        score = {'status': 'completed', 'success': False}
        self.make_links(score, task='oss-fuzz:100')
        with self.assertRaisesRegex(ValueError, 'task id does not match'):
            report_rows(self.report, self.manifest, self.links)
        self.make_links({**score, 'vul_exit_code': True})
        with self.assertRaisesRegex(ValueError, 'invalid official vul_exit_code'):
            report_rows(self.report, self.manifest, self.links)
        score_path = self.make_links(score)
        destination = self.root / 'actual.json'
        score_path.rename(destination)
        score_path.symlink_to(destination)
        with self.assertRaisesRegex(ValueError, 'symbolic links'):
            report_rows(self.report, self.manifest, self.links)


if __name__ == '__main__':
    unittest.main()
