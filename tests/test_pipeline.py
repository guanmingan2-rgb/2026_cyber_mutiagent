"""Unit tests for the CyberGym pipeline modules (no model, no scorer)."""

import gzip
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import data, scoring
from pipeline.run import run_pipeline, validate_smoke, validate_solve


def make_archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            payload = content.encode()
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class DataTests(unittest.TestCase):
    def test_task_id_validation(self):
        self.assertEqual(data.check_task_id('arvo:10013'), 'arvo:10013')
        for bad in ('ARVO:1', 'arvo', 'arvo:', 'arvo:abc', '../arvo:1'):
            with self.assertRaises(ValueError):
                data.check_task_id(bad)

    def test_unsafe_member_paths_rejected(self):
        for bad in ('../etc/passwd', '/abs/path', 'src\\vul\\a.c', 'src-vul/\x01.c'):
            with self.assertRaises(ValueError):
                data.member_path(bad)

    def test_level1_source_allowlist(self):
        data.check_level1_source('src-vul/graphicsmagick/magick/version.c')
        for bad in ('src-vul/proj/tests/a.c', 'src-vul/proj/poc.c', 'src-fix/proj/a.c',
                    'src-vul/a.c', 'src-vul/proj/a.patch'):
            with self.assertRaises(ValueError):
                data.check_level1_source(bad)

    def test_load_task_materials_reads_selected_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / 'arvo' / '1'
            task.mkdir(parents=True)
            (task / 'description.txt').write_text('A bug in the parser.')
            (task / 'repo-vul.tar.gz').write_bytes(make_archive({
                'src-vul/proj/a.c': 'int main(void){return 0;}',
                'src-vul/proj/b.c': 'int b;',
            }))
            materials = data.load_task_materials(root, 'arvo:1', ['src-vul/proj/a.c'])
            self.assertEqual(materials['task_id'], 'arvo:1')
            self.assertIn('parser', materials['description']['text'])
            self.assertEqual(materials['sources']['src-vul/proj/a.c']['text'], 'int main(void){return 0;}')
            self.assertEqual(materials['sources']['src-vul/proj/a.c']['lines'], 1)

    def test_read_workspace_texts_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            task = Path(tmp)
            (task / 'description.txt').write_text('desc')
            (task / 'README.md').write_text('readme')
            (task / 'repo-vul.tar.gz').write_bytes(b'\x1f\x8b')
            payload, hashes = data.read_workspace_texts(task)
            self.assertEqual(payload, {'description.txt': 'desc', 'README.md': 'readme'})
            self.assertEqual(set(hashes), {'description.txt', 'README.md', 'repo-vul.tar.gz'})


class ScoringTests(unittest.TestCase):
    def test_official_decision(self):
        self.assertTrue(scoring.decide(1, 0))
        self.assertTrue(scoring.decide(77, 0))
        self.assertFalse(scoring.decide(0, 0))
        self.assertFalse(scoring.decide(300, 0))
        self.assertFalse(scoring.decide(1, 1))


class AgentHandoffTests(unittest.TestCase):
    def test_valid_candidate(self):
        value = validate_smoke(json.dumps({'input_text': 'hello', 'reason': 'plumbing'}))
        self.assertEqual(value['input_text'], 'hello')

    def test_invalid_candidates(self):
        cases = [
            '{"input_text":"x"}',
            '{"input_text":"x","reason":"y","extra":1}',
            '{"input_text":"","reason":"y"}',
            '{"input_text":"x","reason":"  "}',
            '{"input_text":"x","reason":"a","reason":"b"}',
            'not json',
        ]
        for text in cases:
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                validate_smoke(text)


class RunTests(unittest.TestCase):
    def test_failed_prepare_still_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'out'
            report = run_pipeline('arvo:1', Path(tmp) / 'missing-data', output)
            self.assertEqual(report['chain_status'], 'failed')
            self.assertEqual(report['stage'], 'task_prepare')
            self.assertTrue((output / 'report.json').is_file())
            self.assertIn('error_type', report)


if __name__ == '__main__':
    unittest.main()

class SolveHandoffTests(unittest.TestCase):
    def test_valid_solve_payload(self):
        import base64
        raw = b"\x89PNG\r\n\x1a\n"
        value = validate_solve(json.dumps({"poc_base64": base64.b64encode(raw).decode(),
                                           "reason": "header bytes"}))
        self.assertEqual(value["poc_size"], len(raw))

    def test_invalid_solve_payloads(self):
        import base64
        bad = [
            '{"poc_base64":"AAAA"}',
            '{"poc_base64":"not base64!!","reason":"x"}',
            '{"poc_base64":"","reason":"x"}',
            '{"poc_base64":"AAAA","reason":""}',
            json.dumps({"poc_base64": base64.b64encode(b"x" * 70000).decode(), "reason": "big"}),
        ]
        for text in bad:
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                validate_solve(text)

    def test_solve_requires_source(self):
        with self.assertRaises(SystemExit):
            from pipeline.run import main
            main(["--task-id", "arvo:1", "--output", "/tmp/never", "--mode", "solve"])
