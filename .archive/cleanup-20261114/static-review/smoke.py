"""Exercise the review entry with a real SDK and a loopback model."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading

from review import review


ANSWERS = {
    'manager': {'objective': 'Check empty-input behavior.', 'checks': ['Inspect the divisor.']},
    'worker': {'summary': 'Empty sequences cause division by zero.', 'findings': [
        {'line': 2, 'severity': 'medium', 'message': 'The denominator can be zero.',
         'suggestion': 'Define explicit behavior for an empty sequence.'}]},
    'reviewer': {'verdict': 'accept', 'reason': 'The cited line divides by the sequence length.'},
}


def verify(output, expected, requests):
    events = [json.loads(line) for line in (output / 'events.jsonl').read_text().splitlines()]
    lifecycle = [event for event in events if event['status'] != 'structured_output']
    assert [(item['role'], item['status']) for item in lifecycle] == expected, events
    parsing = [(event['role'], event['parse_status'], event['handoff_saved'])
               for event in events if event['status'] == 'structured_output']
    parse_by_outcome = {'completed': ('valid', True), 'failed': ('invalid', False),
                        'timeout': ('not_attempted', False)}
    assert parsing == [(role, *parse_by_outcome[outcome])
                       for role, outcome in expected[1::2]], parsing
    for role, _ in expected[::2]:
        folder = output / role
        execution = json.loads((folder / 'execution/execution.json').read_text())
        try:
            os.killpg(execution['process_group'], 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError(f'{role} process group remains alive')
        assert not list((folder / 'workspace').iterdir())
    assert len(requests) == len(expected) // 2, requests


def main():
    requests = []
    errors = []
    release = threading.Event()
    mode = 'normal'

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert self.path == '/v1/chat/completions' and not data.get('tools')
                messages = [m['content'] for m in data['messages'] if m['role'] == 'user' and m['content'].startswith('Role: ')]
                assert len(messages) == 1, messages
                header, raw = messages[0].split('Input JSON:\n', 1)
                role = header.split('Role: ', 1)[1].split('.', 1)[0]
                payload = json.loads(raw)
                assert role == list(ANSWERS)[len(requests)]
                assert ('source' in payload) == (role != 'manager')
                assert ('worker_report' in payload) == (role == 'reviewer')
                if role != 'manager':
                    assert payload['assignment'] == ANSWERS['manager']
                if role == 'reviewer':
                    assert payload['worker_report'] == ANSWERS['worker']
                requests.append(role)
                if mode == 'timeout':
                    release.wait(30)
                    return
                content = 'invalid JSON' if mode == 'invalid' else json.dumps(ANSWERS[role])
                message = {'role': 'assistant', 'content': content, 'reasoning_content': 'fixture'}
                common = {'id': 'fixture-' + role, 'created': 1, 'model': data['model']}
                usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
                if data.get('stream'):
                    chunks = [
                        {**common, 'object': 'chat.completion.chunk', 'choices': [
                            {'index': 0, 'delta': message, 'finish_reason': None}]},
                        {**common, 'object': 'chat.completion.chunk', 'choices': [
                            {'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage},
                    ]
                    body = (''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
                            + 'data: [DONE]\n\n').encode()
                    content_type = 'text/event-stream'
                else:
                    body = json.dumps({**common, 'object': 'chat.completion', 'choices': [
                        {'index': 0, 'message': message, 'finish_reason': 'stop'}],
                        'usage': usage}).encode()
                    content_type = 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                errors.append(repr(exc))
                self.send_error(500)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory(prefix='dsh-review-') as tmp:
            root = Path(tmp)
            source = root / 'mean.py'
            source.write_text('def mean(values):\n    return sum(values) / len(values)\n')
            url = f'http://127.0.0.1:{server.server_port}/v1'
            normal = root / 'normal'
            assert review(source, normal, sys.executable, 'Check ordinary edge cases.', 30, url) == 0, errors
            verify(normal, [(role, status) for role in ANSWERS for status in ('started', 'completed')], requests)
            report = json.loads((normal / 'report.json').read_text())
            assert report['reviewer'] == ANSWERS['reviewer']
            assert source.read_bytes() == (normal / 'source.txt').read_bytes()
            assert len({json.loads((normal / role / 'execution/spec.json').read_text())['workspace']
                        for role in ANSWERS}) == 3
            print('PASS: handoffs, distinct sessions, no advertised tools, and report')

            mode = 'invalid'
            requests.clear()
            invalid = root / 'invalid'
            assert review(source, invalid, sys.executable, 'Check edge cases.', 30, url) == 1, errors
            verify(invalid, [('manager', 'started'), ('manager', 'failed')], requests)
            assert not (invalid / 'worker').exists() and not (invalid / 'report.json').exists()
            print('PASS: malformed assignment stops handoff')

            mode = 'timeout'
            requests.clear()
            timeout = root / 'timeout'
            assert review(source, timeout, sys.executable, 'Check edge cases.', 10, url) == 124, errors
            verify(timeout, [('manager', 'started'), ('manager', 'timeout')], requests)
            assert not (timeout / 'worker').exists() and not (timeout / 'report.json').exists()
            assert not errors, errors
            print('PASS: timeout stops handoff and reaps the SDK process group')
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
