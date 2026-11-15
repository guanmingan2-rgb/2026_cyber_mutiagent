"""Loopback model for testing experiment plumbing with the real SDK."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


@contextmanager
def scripted_model(task, *, static_level1=False):
    assignment = {'objective': 'Review the teaching boundary condition.',
                  'allowed_files': [task.source_name], 'checks': ['Check the edge condition.'],
                  'evidence': [{'file': task.source_name, 'line': task.expected_line}]}
    answers = {
        'mapper': {'locations': [{'file': task.source_name, 'line': task.expected_line,
                                  'note': 'Review this boundary expression.'}]},
        'manager': assignment,
        'critic': {'verdict': 'pass', 'reason': 'Assignment is within the teaching source.'},
        'worker': {'summary': 'A boundary issue is visible.', 'findings': [
            {'file': task.source_name, 'line': task.expected_line, 'severity': 'medium',
             'message': 'The edge condition is not handled as specified.',
             'suggestion': 'Handle the specified boundary explicitly.'}]},
        'verifier': {'reviews': [{'id': 'F1', 'verdict': 'pass',
                                  'reason': 'The fixture demonstrates the boundary issue.'}]},
        'manager_select': {'selected_ids': ['F1']},
    }
    if static_level1:
        assignment.update(objective='Summarize the selected source.', checks=['Review code organization.'])
        answers['worker'] = {'summary': 'Scripted response for handoff validation.', 'findings': []}
        answers['verifier'] = {'reviews': []}
        answers['manager_select'] = {'selected_ids': []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            role_messages = [m['content'] for m in body['messages']
                             if m['role'] == 'user' and m['content'].startswith('Role: ')]
            if self.path != '/v1/chat/completions' or body.get('tools') or len(role_messages) != 1:
                self.send_error(400)
                return
            header, raw = role_messages[0].split('Input JSON:\n', 1)
            role = header.removeprefix('Role: ').split('.', 1)[0]
            payload = json.loads(raw)
            if role == 'verifier':
                if static_level1:
                    valid = (set(payload) == {'task_id', 'worker_status', 'source_metadata', 'worker_report'}
                             and payload['worker_status'] == 'completed')
                else:
                    valid = payload.get('evaluation', {}).get('verification', {}).get(
                        'origin') in ('local_teaching', 'mock')
                if not valid:
                    self.send_error(400)
                    return
            answer = answers.get(role)
            if answer is None:
                self.send_error(400)
                return
            message = {'role': 'assistant', 'content': json.dumps(answer), 'reasoning_content': 'fixture'}
            common = {'id': 'fixture-' + role, 'created': 1, 'model': body['model']}
            usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
                     'prompt_cache_hit_tokens': 0, 'prompt_cache_miss_tokens': 100}
            if body.get('stream'):
                chunks = [
                    {**common, 'object': 'chat.completion.chunk', 'choices': [
                        {'index': 0, 'delta': message, 'finish_reason': None}]},
                    {**common, 'object': 'chat.completion.chunk', 'choices': [
                        {'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage},
                ]
                data = (''.join('data: ' + json.dumps(item) + '\n\n' for item in chunks)
                        + 'data: [DONE]\n\n').encode()
                mime = 'text/event-stream'
            else:
                data = json.dumps({**common, 'object': 'chat.completion', 'choices': [
                    {'index': 0, 'message': message, 'finish_reason': 'stop'}], 'usage': usage}).encode()
                mime = 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
