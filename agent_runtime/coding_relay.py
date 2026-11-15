"""独立编程入口的模型请求次数上限；协议策略仍由公共网关维护。"""

import hmac
import http.client
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading


class _UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.host)


class LimitedRelay:
    def __init__(self, socket_path, token, max_requests, timeout):
        self.request_count = 0
        self.exhausted = False
        self.rejected = False
        lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if (self.path != '/v1/chat/completions'
                        or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)):
                    self.send_error(403)
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 64 * 1024 * 1024 or self.headers.get('Transfer-Encoding'):
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                # Review roles are text-only. Fail closed if the SDK advertises tools.
                try:
                    request = json.loads(body)
                    if not isinstance(request, dict) or request.get('tools') or request.get('functions'):
                        raise ValueError('tools are disabled for review roles')
                except (ValueError, TypeError):
                    owner.rejected = True
                    self.send_error(400, 'Invalid tool-free review request')
                    return
                with lock:
                    limited = owner.request_count >= max_requests
                    if limited:
                        owner.exhausted = True
                    else:
                        owner.request_count += 1
                if limited:
                    body = json.dumps({'error': {'message': 'model request limit', 'type': 'step_limit'}}).encode()
                    self.send_response(409)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    with closing(_UnixConnection(str(socket_path), timeout=timeout)) as connection:
                        connection.request('POST', self.path, body, {
                            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
                        response = connection.getresponse()
                        if response.status == 409:
                            owner.rejected = True
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                        self.end_headers()
                        while chunk := response.read1(65536):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (OSError, http.client.HTTPException):
                    # The SDK or gateway can close the stream during timeout cleanup.
                    self.close_connection = True

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}/v1'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
