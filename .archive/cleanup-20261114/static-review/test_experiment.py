"""Unit checks for evidence, evaluation boundaries and experiment reporting."""

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from agent_runtime.coding import DEEPSEEK_PROFILE, resolve_profile
from evaluation import EvaluationAdapter, FIXTURES, TestArtifact, artifact_bytes, official_status
from experiment import TaskBudget, configuration, ledger


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.task = FIXTURES['teaching:1'][0]
        raw = artifact_bytes(self.task)
        self.artifact = TestArtifact('test_artifact.json', sha256(raw).hexdigest(), self.task.fixture)
        (self.root / self.artifact.path).write_bytes(raw)

    def test_mock_never_executes_and_has_no_official_score(self):
        with patch('evaluation.subprocess.Popen') as launch:
            result = EvaluationAdapter('mock', 3).evaluate(self.task, self.artifact, self.root)
        launch.assert_not_called()
        self.assertEqual((result.origin, result.status), ('mock', 'simulated'))
        self.assertEqual(official_status(self.task.task_id), {'status': 'not_evaluated', 'success': None})

    def test_local_teaching_result_and_logs(self):
        result = EvaluationAdapter('local', 3).evaluate(self.task, self.artifact, self.root)
        self.assertEqual((result.origin, result.status, result.exit_code), ('local_teaching', 'pass', 0))
        self.assertIn('pass', (self.root / 'evaluation/stdout.log').read_text())
        self.assertTrue((self.root / 'evaluation/stderr.log').is_file())

    def test_modified_artifact_and_external_path_are_rejected(self):
        (self.root / self.artifact.path).write_bytes(b'modified')
        with patch('evaluation.subprocess.Popen') as launch:
            first = EvaluationAdapter('local', 3).evaluate(self.task, self.artifact, self.root)
            other = TestArtifact('../other.json', self.artifact.sha256, self.task.fixture)
            second = EvaluationAdapter('local', 3).evaluate(self.task, other, self.root)
        launch.assert_not_called()
        self.assertEqual((first.status, second.status), ('invalid_artifact', 'invalid_artifact'))

    def test_timeout_kills_and_reaps_process_group(self):
        child = Mock(pid=4321)
        child.wait.side_effect = [subprocess.TimeoutExpired('test fixture', 1), -9]
        with patch('evaluation.subprocess.Popen', return_value=child), patch('evaluation.os.killpg') as kill:
            result = EvaluationAdapter('local', 1).evaluate(self.task, self.artifact, self.root)
        self.assertEqual(result.status, 'timeout')
        kill.assert_called_once()
        self.assertEqual(kill.call_args.args[0], child.pid)
        self.assertEqual(child.wait.call_count, 2)

    def test_official_import_needs_matching_task(self):
        path = self.root / 'record.json'
        path.write_text(json.dumps({'task_id': 'arvo:1', 'selected_at_1': {
            'success': False, 'status': 'completed'}}))
        self.assertEqual(official_status('arvo:1', path)['kind'], 'external_official_record')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            official_status('arvo:2', path)


class ExperimentTests(unittest.TestCase):
    def test_budget_is_shared_across_roles(self):
        profile = resolve_profile(DEEPSEEK_PROFILE)
        budget = TaskBudget({'wall_seconds': 90, 'max_requests': 2, 'max_generated_tokens': 99})
        first = budget.role_profile(profile)
        self.assertEqual(first['budgets']['max_steps'], 2)
        self.assertEqual(first['policy']['max_output_tokens'], 99)
        budget.settle({'model_requests': 1}, {'usage': {'generated_tokens': 40,
                      'accounting_failed': False, 'uncertain_reserved_tokens': 0}})
        second = budget.role_profile(profile)
        self.assertEqual(second['budgets']['max_steps'], 1)
        self.assertEqual(second['policy']['max_total_generated_tokens'], 59)
        budget.settle({'model_requests': 1}, {'usage': {'generated_tokens': 20,
                      'accounting_failed': False, 'uncertain_reserved_tokens': 0}})
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            budget.role_profile(profile)

    def test_cost_is_nullable_and_excludes_fixture_model(self):
        prices = {'input_cache_hit': 1, 'input_cache_miss': 2, 'output': 3}
        with TemporaryDirectory() as temp:
            output = Path(temp)
            role = output / 'worker/execution'
            role.mkdir(parents=True)
            (role / 'execution.json').write_text(json.dumps({'model_requests': 1}))
            (role / 'usage.json').write_text(json.dumps({
                'summary': {'accounting_failed': False, 'uncertain_reserved_tokens': 0},
                'requests': [{'state': 'accounted', 'attempt_count': 1, 'usage': {
                    'prompt_tokens': 100, 'completion_tokens': 20, 'cached_tokens': 40}}]}))
            self.assertAlmostEqual(ledger(output, prices, False)['estimated_cost_usd'], 0.00022)
            self.assertIsNone(ledger(output, prices, True)['estimated_cost_usd'])
            self.assertIsNone(ledger(output, None, False)['estimated_cost_usd'])
            (role / 'usage.json').write_text(json.dumps({
                'summary': {'accounting_failed': True, 'uncertain_reserved_tokens': 8}, 'requests': []}))
            self.assertIsNone(ledger(output, prices, False)['estimated_cost_usd'])

    def test_config_pins_task_variant_budget_and_model(self):
        cfg, _, adapter, _ = configuration(Path(__file__).with_name('experiment.example.json'))
        self.assertEqual(cfg['model'], 'deepseek-flash')
        self.assertEqual(len(cfg['variants']), 4)
        self.assertEqual(adapter.mode, 'local')
