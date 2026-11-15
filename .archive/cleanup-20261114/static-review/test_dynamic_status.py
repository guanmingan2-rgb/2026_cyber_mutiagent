import unittest
from collect_dynamic import classify


class SchedulerEvidenceTests(unittest.TestCase):
    def test_cancelled_job_cannot_be_successful_even_with_complete_artifacts(self):
        report = {'chain_status': 'completed', 'official_status': 'completed', 'poc_id': 'one'}
        score = {'selected_at_1': {'status': 'completed'}, 'poc_id': 'one'}
        self.assertEqual(classify('CANCELLED', report, score), 'incomplete')
        self.assertEqual(classify('COMPLETED', report, score), 'completed')

    def test_scheduler_success_requires_matching_actual_result(self):
        self.assertEqual(classify('COMPLETED', {}, {}), 'result_missing_or_inconsistent')
        report = {'chain_status': 'completed', 'official_status': 'completed', 'poc_id': 'one'}
        score = {'selected_at_1': {'status': 'completed'}, 'poc_id': 'two'}
        self.assertEqual(classify('COMPLETED', report, score), 'result_missing_or_inconsistent')


if __name__ == '__main__':
    unittest.main()
