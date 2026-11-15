"""Tests for the unified Agent Runtime interface and both adapters.

All tests are offline: no DeepSeek API, no official scorer process, no Slurm.
The frozen adapter is exercised with a fake executor that only writes fixture
files, and with its dry-run command builder.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import run as pipeline_run
from pipeline.replay import replay_frozen_task
from pipeline.runtime import AgentRunResult, RuntimeConfig, RunStatus, TaskContext, get_runner
from pipeline.runtimes.frozen import FrozenExecutorAdapter
from pipeline.runtimes.oneshot import OneShotRunner, validate_smoke, validate_solve

FROZEN_HISTORY = Path('/home/ybgao/2026_cyber_agents/safegym_solver/runs/'
                      'unified-migration-20261113T071227Z-95ab6f/results/deepseek/tasks/01')


def make_task_dir(root):
    task = Path(root) / 'task'
    task.mkdir(parents=True)
    (task / 'description.txt').write_text('A bug in the parser.')
    (task / 'README.md').write_text('Submit a raw input.')
    (task / 'repo-vul.tar.gz').write_bytes(b'\x1f\x8b')
    (task / 'submit.sh').write_text('#!/bin/sh\n')
    (task / 'task.json').write_text('{}')
    return task


def fake_prepare(task_id, data_root, output, server):
    return make_task_dir(output)


def fake_scorer(success=True):
    def score(task_id, data_root, output, server):
        value = {'task_id': task_id, 'candidate_sha256': 'x', 'poc_id': 'poc-1',
                 'selected_at_1': {'status': 'completed', 'success': success,
                                   'vul_exit_code': 1 if success else 0,
                                   'fix_exit_code': 0},
                 'responses': {}}
        (Path(output) / 'score.json').write_text(json.dumps(value))
        return value
    return score


class FakeRunner:
    name = 'fake'

    def __init__(self, status=RunStatus.COMPLETED, data=b'candidate'):
        self.status, self.data = status, data

    def run(self, context, config):
        result = AgentRunResult(task_id=context.task_id, run_status=self.status, runtime='fake')
        if self.status == RunStatus.COMPLETED:
            result.attach_candidate(self.data)
        else:
            result.error_type = 'FakeError'
            result.error_message = self.status
        return result


class RuntimeContractTests(unittest.TestCase):
    def test_both_runtimes_satisfy_protocol(self):
        for name in ('oneshot', 'frozen'):
            runner = get_runner(name)
            self.assertTrue(hasattr(runner, 'run'))
            self.assertEqual(runner.name, name)
        with self.assertRaises(ValueError):
            get_runner('nope')

    def test_unknown_runtime_rejected(self):
        with self.assertRaises(ValueError):
            get_runner('missing')


class PipelineRuntimeTests(unittest.TestCase):
    def test_completed_runtime_reaches_official_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'out'
            report = pipeline_run.run_pipeline(
                'arvo:1', Path(tmp), output, runner=FakeRunner(),
                prepare=fake_prepare, scorer=fake_scorer(True))
            self.assertEqual(report['chain_status'], 'completed')
            self.assertTrue(report['success'])
            self.assertEqual(report['runtime'], 'oneshot')
            self.assertTrue((output / 'candidate.bin').is_file())
            self.assertTrue((output / 'agent' / 'runtime-result.json').is_file())
            selection = json.loads((output / 'final-selection.json').read_text())
            self.assertEqual(len(selection['candidate_sha256']), 64)

    def test_official_failure_is_success_false_not_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'out'
            report = pipeline_run.run_pipeline(
                'arvo:1', Path(tmp), output, runner=FakeRunner(),
                prepare=fake_prepare, scorer=fake_scorer(False))
            self.assertEqual(report['chain_status'], 'completed')
            self.assertIs(report['success'], False)

    def test_runtime_failure_keeps_success_null(self):
        for status, expected in ((RunStatus.NO_CANDIDATE, 'no_candidate'),
                                 (RunStatus.TRUNCATED, 'truncated'),
                                 (RunStatus.TIMEOUT, 'timeout'),
                                 (RunStatus.INFRA_ERROR, 'infra_error')):
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'out'
                report = pipeline_run.run_pipeline(
                    'arvo:1', Path(tmp), output, runner=FakeRunner(status),
                    prepare=fake_prepare, scorer=fake_scorer(True))
                self.assertEqual(report['chain_status'], 'failed')
                self.assertIsNone(report['success'])
                self.assertEqual(report['agent_status'], expected)
                self.assertEqual(report['official_status'], 'not_evaluated')

    def test_scorer_rejection_reports_infra_error(self):
        def broken(task_id, data_root, output, server):
            raise RuntimeError('official scorer score failed: target execution failed')

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'out'
            report = pipeline_run.run_pipeline(
                'arvo:1', Path(tmp), output, runner=FakeRunner(),
                prepare=fake_prepare, scorer=broken)
            self.assertEqual(report['stage'], 'official_scoring')
            self.assertEqual(report['official_status'], 'infra_error')
            self.assertIsNone(report['success'])


class OneShotTests(unittest.TestCase):
    def test_validation_contract(self):
        with self.assertRaises(ValueError):
            validate_smoke('{"input_text":"x"}')
        with self.assertRaises(ValueError):
            validate_solve('{"poc_base64":"","reason":"x"}')

    def test_runner_returns_failure_without_session(self):
        # An invalid output directory makes AgentSession fail; the runner must
        # return a structured failure instead of raising.
        with tempfile.TemporaryDirectory() as tmp:
            context = TaskContext(task_id='arvo:1', task_dir=Path(tmp), data_root=Path(tmp),
                                  mode='smoke', description='d', readme='r')
            config = RuntimeConfig(output_dir=Path(tmp) / 'out')
            (Path(tmp) / 'out').mkdir()
            result = OneShotRunner(python=sys.executable).run(context, config)
            self.assertNotEqual(result.run_status, RunStatus.COMPLETED)
            self.assertTrue(result.error_type)


class FrozenAdapterTests(unittest.TestCase):
    def test_dry_run_builds_command_and_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = make_task_dir(tmp)
            output = Path(tmp) / 'agentrun'
            output.mkdir()
            context = TaskContext(task_id='arvo:1', task_dir=task, data_root=Path(tmp),
                                  mode='solve', description='d', readme='r',
                                  sources={'src': 'x'})
            adapter = FrozenExecutorAdapter(dry_run=True)
            result = adapter.run(context, RuntimeConfig(runtime='frozen', output_dir=output))
            self.assertEqual(result.run_status, RunStatus.INFRA_ERROR)
            self.assertEqual(result.error_type, 'DryRun')
            command = result.details['command']
            self.assertIn('agent_harness.py', command[1])
            self.assertIn('--workspace', command)
            self.assertTrue((output / 'workspace' / 'repo-vul.tar.gz').is_file())
            self.assertTrue((output / 'prompt.txt').is_file())

    def test_fake_executor_produces_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'fake_executor.py'
            fake.write_text(
                "import json,sys,pathlib\n"
                "a=sys.argv; ws=pathlib.Path(a[a.index('--workspace')+1]); out=pathlib.Path(a[a.index('--output-dir')+1])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "(ws/'poc.bin').write_bytes(b'POCBYTES')\n"
                "(out/'summary.json').write_text(json.dumps({'status':'completed','elapsed_seconds':1.5,"
                "'model_usage':{'request_count':3,'prompt_tokens':100,'generated_tokens':20}}))\n")
            task = make_task_dir(tmp)
            output = Path(tmp) / 'agentrun'
            output.mkdir()
            context = TaskContext(task_id='arvo:1', task_dir=task, data_root=Path(tmp),
                                  mode='solve', description='d', readme='r',
                                  sources={'src': 'x'})
            adapter = FrozenExecutorAdapter(fake_executor=fake)
            result = adapter.run(context, RuntimeConfig(runtime='frozen', output_dir=output))
            self.assertEqual(result.run_status, RunStatus.COMPLETED)
            self.assertEqual(result.candidate_bytes, b'POCBYTES')
            self.assertEqual(result.llm_requests, 3)
            self.assertEqual(result.input_tokens, 100)
            self.assertEqual(result.output_tokens, 20)

    def test_fake_executor_without_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'fake_executor.py'
            fake.write_text(
                "import json,sys,pathlib\n"
                "a=sys.argv; out=pathlib.Path(a[a.index('--output-dir')+1])\n"
                "out.mkdir(parents=True, exist_ok=True)\n"
                "(out/'summary.json').write_text(json.dumps({'status':'completed'}))\n")
            task = make_task_dir(tmp)
            output = Path(tmp) / 'agentrun'
            output.mkdir()
            context = TaskContext(task_id='arvo:1', task_dir=task, data_root=Path(tmp),
                                  mode='solve', description='d', readme='r', sources={'src': 'x'})
            result = FrozenExecutorAdapter(fake_executor=fake).run(
                context, RuntimeConfig(runtime='frozen', output_dir=output))
            self.assertEqual(result.run_status, RunStatus.NO_CANDIDATE)


class HistoricalReplayTests(unittest.TestCase):
    def test_arvo_10013_replay_matches_official_record(self):
        if not FROZEN_HISTORY.is_dir():
            self.skipTest('historical frozen run not present on this host')
        replay = replay_frozen_task(FROZEN_HISTORY)
        unified, score = replay['unified'], replay['score']
        self.assertEqual(unified.task_id, 'arvo:10013')
        self.assertEqual(unified.run_status, RunStatus.COMPLETED)
        self.assertTrue(unified.details['candidate_hash_matches'])
        self.assertIsNotNone(score)
        self.assertEqual(score['vul_exit_code'], 77)
        self.assertEqual(score['fix_exit_code'], 0)
        self.assertTrue(score['success'])
        self.assertGreater(unified.llm_requests or 0, 0)


if __name__ == '__main__':
    unittest.main()
