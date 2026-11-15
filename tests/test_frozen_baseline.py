"""Tests for the native-protocol frozen baseline runtime (offline)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.runtime import RuntimeConfig, RunStatus, TaskContext
from pipeline.runtimes.frozen_baseline import FrozenBaselineRunner


def make_task_dir(root):
    task = Path(root) / 'task'
    task.mkdir(parents=True)
    (task / 'description.txt').write_text('bug')
    (task / 'README.md').write_text('readme')
    (task / 'repo-vul.tar.gz').write_bytes(b'\x1f\x8b')
    (task / 'submit.sh').write_text('#!/bin/sh\n')
    (task / 'task.json').write_text(json.dumps(
        {'task_id': 'a5c9210adf28', 'agent_id': 'agent-1', 'checksum': 'deadbeef',
         'server': 'http://127.0.0.1:8667', 'difficulty': 'level1', 'with_flag': False}))
    return task


def fake_executor(script_body):
    def build(root):
        path = Path(root) / 'executor.py'
        path.write_text(script_body)
        return path
    return build


SUBMIT_SCRIPT = """
import json, pathlib, sys
a = sys.argv
ws = pathlib.Path(a[a.index('--workspace') + 1])
out = pathlib.Path(a[a.index('--output-dir') + 1])
out.mkdir(parents=True, exist_ok=True)
score = ws.parent / 'score' / 'requests'
score.mkdir(parents=True, exist_ok=True)
(score / 'r1.submitted-poc').write_bytes(b'POC-BYTES')
rows = [{'request_id': 'r1', 'task_id': 'a5c9210adf28', 'poc_id': 'poc-1',
         'operation': 'score_submit', 'complete': True, 'http_status': 200,
         'exit_code': 1, 'submitted_artifact': 'requests/r1.submitted-poc',
         'submitted_sha256': 'x'}]
(out.parent / 'score' / 'requests.jsonl').write_text('\\n'.join(json.dumps(r) for r in rows) + '\\n')
(ws / 'final_submission.json').write_text(json.dumps({'poc_id': 'poc-1'}))
(out / 'summary.json').write_text(json.dumps({'status': 'completed', 'elapsed_seconds': 2.0,
    'model_usage': {'request_count': 7, 'prompt_tokens': 1000, 'generated_tokens': 50}}))
"""


class FrozenBaselineTests(unittest.TestCase):
    def context(self, task):
        return TaskContext(task_id='arvo:10013', task_dir=task, data_root=task.parent,
                           mode='solve', description='d', readme='r', sources={'s': 'x'})

    def run_adapter(self, tmp, script, **kwargs):
        task = make_task_dir(tmp)
        output = Path(tmp) / 'run'
        output.mkdir()
        executor = fake_executor(script)(tmp)
        adapter = FrozenBaselineRunner(fake_executor=executor, start_gateway=False, **kwargs)
        result = adapter.run(self.context(task), RuntimeConfig(runtime='frozen', output_dir=output))
        return result, output

    def test_native_final_submission_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, output = self.run_adapter(tmp, SUBMIT_SCRIPT)
            self.assertEqual(result.run_status, RunStatus.COMPLETED)
            self.assertEqual(result.candidate_bytes, b'POC-BYTES')
            self.assertEqual(result.details['poc_id'], 'poc-1')
            self.assertEqual(result.details['selection_source'], 'final_submission.json')
            self.assertEqual(result.llm_requests, 7)
            self.assertEqual(result.input_tokens, 1000)

    def test_invalid_selection_is_reported(self):
        broken = SUBMIT_SCRIPT.replace("'exit_code': 1", "'exit_code': 127")
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_adapter(tmp, broken)
            self.assertEqual(result.run_status, RunStatus.FAILED)
            self.assertEqual(result.error_type, 'InvalidSelection')

    def test_missing_final_submission_falls_back_to_poc_bin(self):
        script = """
import json, pathlib, sys
a = sys.argv
ws = pathlib.Path(a[a.index('--workspace') + 1])
out = pathlib.Path(a[a.index('--output-dir') + 1])
out.mkdir(parents=True, exist_ok=True)
(ws / 'poc.bin').write_bytes(b'FALLBACK')
(out / 'summary.json').write_text(json.dumps({'status': 'completed'}))
"""
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self.run_adapter(tmp, script)
            self.assertEqual(result.run_status, RunStatus.COMPLETED)
            self.assertEqual(result.candidate_bytes, b'FALLBACK')
            self.assertIn('fallback', result.details['selection_source'])

    def test_dry_run_exposes_score_socket_when_gateway_present(self):
        class FakeGateway:
            socket_path = Path('/tmp/fake-score.sock')
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            task = make_task_dir(tmp)
            output = Path(tmp) / 'run'
            output.mkdir()
            adapter = FrozenBaselineRunner(dry_run=True, gateway_factory=lambda t, o: FakeGateway())
            result = adapter.run(self.context(task), RuntimeConfig(runtime='frozen', output_dir=output))
            self.assertEqual(result.error_type, 'DryRun')
            self.assertIn('--score-socket', result.details['command'])
            self.assertTrue(result.details['score_channel'])


if __name__ == '__main__':
    unittest.main()
