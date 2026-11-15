"""仅向固定 Chat Completions 服务转发文本请求的 Unix 网关。"""

import hmac
import http.client
import json
import math
import os
import secrets
import socket
import socketserver
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

MAX_BODY_BYTES = 64 * 1024 * 1024
MAX_WORKERS = 4
MAX_USAGE_EVENT_BYTES = 256 * 1024
MAX_TRANSIENT_ATTEMPTS = 11
TRANSIENT_UPSTREAM_STATUSES = frozenset({502, 503, 504})
RETRY_BACKOFF_SECONDS = tuple(0.1 * (2 ** index) for index in range(MAX_TRANSIENT_ATTEMPTS - 1))
REQUEST_PATH = "/v1/chat/completions"
SAMPLING_FIELDS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty")
ALLOWED_FIELDS = frozenset({
    "model", "messages", "tools", "tool_choice", "parallel_tool_calls",
    "temperature", "top_p", "top_k", "min_p", "repetition_penalty",
    "presence_penalty", "frequency_penalty", "max_tokens", "max_completion_tokens",
    "stop", "stream", "stream_options", "seed", "logit_bias", "logprobs",
    "top_logprobs", "reasoning_effort", "n", "user", "response_format",
    "service_tier", "verbosity", "chat_template_kwargs", "store", "prompt_cache_key", "thinking",
})


class _AttemptFinished(Exception):
    pass


def _strict_json(raw):
    def reject_constant(value):
        raise ValueError("non-JSON constant")
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_fields)


class _UsageObserver:
    """Chat Completions usage 为累计快照；reasoning 是 completion 的子集。"""

    def __init__(self, stream):
        self.stream = stream
        self.buffer = bytearray()
        self.event = bytearray()
        self.usage = None
        self.error = None
        self.done = False
        self.document = None
        self.generated_content = False
        self.usage_dirty = False
        self.upstream_error = False
        self.finish_reason = None
        self.content_bytes = 0
        self.reasoning_bytes = 0

    def _document(self, document):
        if not isinstance(document, dict):
            self.error = self.error or "invalid_response_object"
            return
        self.document = document
        self.upstream_error = self.upstream_error or "error" in document or document.get("object") == "error"
        choices = document.get("choices", [])
        if not isinstance(choices, list):
            self.error = self.error or "invalid_choices"
            return
        for choice in choices:
            if isinstance(choice, dict):
                part = choice.get("delta", choice.get("message", {}))
                reason = choice.get("finish_reason")
                if isinstance(reason, str):
                    self.finish_reason = reason
                if isinstance(part, dict):
                    content = part.get("content")
                    reasoning = part.get("reasoning_content") or part.get("reasoning")
                    if isinstance(content, str):
                        self.content_bytes += len(content.encode("utf-8", errors="replace"))
                    if isinstance(reasoning, str):
                        self.reasoning_bytes += len(reasoning.encode("utf-8", errors="replace"))
                if isinstance(part, dict) and any(part.get(key) for key in ("content", "reasoning_content", "reasoning", "tool_calls")):
                    self.generated_content = True
                    self.usage_dirty = True
        usage = document.get("usage")
        if usage is None:
            return
        fields = ("prompt_tokens", "completion_tokens", "total_tokens")
        if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in fields):
            self.error = self.error or "invalid_usage"
            return
        selected = {key: usage[key] for key in fields}
        if selected["total_tokens"] != selected["prompt_tokens"] + selected["completion_tokens"]:
            self.error = self.error or "inconsistent_usage"
            return
        for detail, key, upper in (("completion_tokens_details", "reasoning_tokens", "completion_tokens"),
                                   ("prompt_tokens_details", "cached_tokens", "prompt_tokens"),
                                   ("prompt_tokens_details", "cache_creation_tokens", "prompt_tokens")):
            values = usage.get(detail)
            if values is None:
                continue
            if not isinstance(values, dict):
                self.error = self.error or "invalid_usage_details"
                return
            value = values.get(key)
            if value is not None:
                if type(value) is not int or not 0 <= value <= selected[upper]:
                    self.error = self.error or "invalid_usage_details"
                    return
                selected[key] = value
        for source, target in (("cached_tokens", "cached_tokens"), ("cache_read_input_tokens", "cached_tokens"),
                               ("cache_creation_tokens", "cache_creation_tokens"),
                               ("cache_creation_input_tokens", "cache_creation_tokens")):
            value = usage.get(source)
            if value is not None:
                if type(value) is not int or not 0 <= value <= selected["prompt_tokens"]:
                    self.error = self.error or "invalid_usage_details"
                    return
                if target in selected and selected[target] != value:
                    self.error = self.error or "inconsistent_usage"
                    return
                selected[target] = value
                selected[source] = value
        for key in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            value = usage.get(key)
            if value is not None:
                if type(value) is not int or not 0 <= value <= selected["prompt_tokens"]:
                    self.error = self.error or "invalid_usage_details"
                    return
                selected[key] = value
        if "prompt_cache_hit_tokens" in selected:
            if "cached_tokens" in selected and selected["cached_tokens"] != selected["prompt_cache_hit_tokens"]:
                self.error = self.error or "inconsistent_usage"
                return
            selected["cached_tokens"] = selected["prompt_cache_hit_tokens"]
            if ("prompt_cache_miss_tokens" in selected
                    and selected["cached_tokens"] + selected["prompt_cache_miss_tokens"] != selected["prompt_tokens"]):
                self.error = self.error or "inconsistent_usage"
                return
        if self.usage is not None and any(selected[key] < self.usage[key] for key in fields):
            self.error = self.error or "usage_decreased"
        self.usage = selected
        self.usage_dirty = False

    def _event(self):
        raw = bytes(self.event).rstrip(b"\n")
        self.event.clear()
        if not raw:
            return
        if self.done:
            self.error = self.error or "event_after_done"
            return
        if raw == b"[DONE]":
            self.done = True
            return
        try:
            self._document(_strict_json(raw))
        except (ValueError, UnicodeError, RecursionError):
            self.error = self.error or "invalid_usage_event"

    def feed(self, data):
        if self.error:
            return
        self.buffer.extend(data)
        if not self.stream:
            if len(self.buffer) > MAX_BODY_BYTES:
                self.error = "response_too_large"
                self.buffer.clear()
            return
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            line = line.rstrip(b"\r")
            if not line:
                self._event()
            elif line.startswith(b"data:"):
                self.event.extend(line[5:].lstrip(b" ") + b"\n")
            if len(self.event) > MAX_USAGE_EVENT_BYTES:
                self.error = "usage_event_too_large"
                self.event.clear()
                break
        if len(self.buffer) > MAX_USAGE_EVENT_BYTES:
            self.error = "usage_event_too_large"
            self.buffer.clear()

    def finish(self):
        if not self.stream and not self.error:
            try:
                self._document(_strict_json(self.buffer))
            except (ValueError, UnicodeError, RecursionError):
                self.error = "invalid_usage_response"
            self.buffer.clear()
        if self.stream and (self.buffer.strip() or self.event or not self.done):
            self.error = self.error or "incomplete_stream"
        if self.usage is not None and (self.usage_dirty or (self.generated_content and self.usage["completion_tokens"] == 0)):
            self.error = self.error or "usage_not_final"


def valid_request(payload, model):
    if not isinstance(payload, dict) or set(payload) - ALLOWED_FIELDS:
        return False
    if payload.get("model") != model:
        return False
    if "store" in payload and payload["store"] is not False:
        return False
    if "prompt_cache_key" in payload and not isinstance(payload["prompt_cache_key"], str):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    for message in messages:
        if not isinstance(message, dict) or set(message) - {
            "role", "content", "name", "tool_calls", "tool_call_id",
            "refusal", "reasoning_content", "reasoning", "provider_specific_fields",
        }:
            return False
        role = message.get("role")
        if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant", "tool"}:
            return False
        for field in ("name", "tool_call_id", "refusal", "reasoning_content", "reasoning"):
            if message.get(field) is not None and not isinstance(message[field], str):
                return False
        provider = message.get("provider_specific_fields", {})
        if not isinstance(provider, dict) or set(provider) - {"refusal", "reasoning_content", "reasoning"}:
            return False
        if any(value is not None and not isinstance(value, str) for value in provider.values()):
            return False
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            if not isinstance(content, list):
                return False
            for block in content:
                if not isinstance(block, dict) or set(block) != {"type", "text"}:
                    return False
                if block["type"] != "text" or not isinstance(block["text"], str):
                    return False
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            return False
        for call in calls:
            if not isinstance(call, dict) or set(call) - {"id", "type", "function"}:
                return False
            function = call.get("function")
            if call.get("type") != "function" or not isinstance(function, dict):
                return False
            if set(function) != {"name", "arguments"}:
                return False
            if not all(isinstance(function[key], str) for key in ("name", "arguments")):
                return False
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"type", "function"}:
            return False
        function = tool.get("function")
        if tool["type"] != "function" or not isinstance(function, dict):
            return False
        if set(function) - {"name", "description", "parameters", "strict"}:
            return False
        if not isinstance(function.get("name"), str):
            return False
        if "parameters" in function and not isinstance(function["parameters"], dict):
            return False
        if "description" in function and not isinstance(function["description"], str):
            return False
        if "strict" in function and not isinstance(function["strict"], bool):
            return False
    choice = payload.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if set(choice) != {"type", "function"} or choice["type"] != "function":
            return False
        if not isinstance(choice["function"], dict) or set(choice["function"]) != {"name"}:
            return False
        if not isinstance(choice["function"]["name"], str):
            return False
    elif choice not in ("auto", "none", "required"):
        return False
    if "thinking" in payload:
        thinking = payload["thinking"]
        if (not isinstance(thinking, dict) or set(thinking) != {"type"}
                or thinking["type"] not in ("enabled", "disabled")):
            return False
    # 只放行本地模板选项；不把任意后端扩展参数传给模型服务。
    template = payload.get("chat_template_kwargs", {})
    if not isinstance(template, dict) or set(template) - {"enable_thinking"}:
        return False
    return not (template and not isinstance(template["enable_thinking"], bool))


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_WORKERS

    def process_request(self, request, client_address):
        if self.owner._stopping.is_set() or not self.owner._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            self.owner._record("OTHER", "<rejected>", 503, 0, 0, 0)
            return
        with self.owner._lock:
            self.owner._clients.add(request)
            worker = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
            self.owner._workers.add(worker)
        worker.start()

    def process_request_thread(self, request, client_address):
        worker = threading.current_thread()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.owner._lock:
                self.owner._clients.discard(request)
                self.owner._workers.discard(worker)
            self.owner._slots.release()

    def handle_error(self, request, client_address):
        # 客户端中断和上游错误只写脱敏审计，不打印可能包含凭据的异常。
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        self.request.settimeout(self.server.owner.timeout)
        self.deadline = time.monotonic() + self.server.owner.timeout
        self.downstream_stopped = threading.Event()
        self.upstream_socket = None
        self.deadline_timer = threading.Timer(self.server.owner.timeout, self._expire)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()
        self.abort_timer = threading.Timer(self.server.owner.timeout + self.server.owner.drain_timeout, self._abort_upstream)
        self.abort_timer.daemon = True
        self.abort_timer.start()
        super().setup()

    def _expire(self):
        self.downstream_stopped.set()
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _abort_upstream(self):
        if self.upstream_socket is not None:
            try:
                self.upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def finish(self):
        self.deadline_timer.cancel()
        self.abort_timer.cancel()
        super().finish()

    def handle(self):
        started = time.monotonic()
        self.audit_status = 400
        self.audit_request_id = None
        self.request_bytes = self.response_bytes = 0
        try:
            super().handle()
        finally:
            method = getattr(self, "command", "OTHER")
            if method not in {"POST", "GET", "HEAD", "PUT", "DELETE", "OPTIONS", "CONNECT", "PATCH"}:
                method = "OTHER"
            path = REQUEST_PATH if getattr(self, "path", None) == REQUEST_PATH else "<rejected>"
            self.server.owner._record(method, path, self.audit_status,
                                      self.request_bytes, self.response_bytes,
                                      time.monotonic() - started, self.audit_request_id)

    def log_message(self, format, *args):
        pass

    def send_error(self, code, message=None, explain=None):
        self._error(code)

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._reject_method
        raise AttributeError(name)

    def _reject_method(self):
        self._error(405)

    def _error(self, status, reason=None, retryable=False):
        self.close_connection = True
        self.audit_status = status
        error = {"message": "Model gateway rejected the request", "code": status}
        if reason is not None:
            error.update(reason=reason, retryable=retryable)
        body = json.dumps({"error": error}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)
            self.response_bytes = len(body)

    def _start_stream(self, status, content_type):
        if self.downstream_stopped.is_set() or self.server.owner._stopping.is_set():
            return False
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            return True
        except OSError:
            self.downstream_stopped.set()
            return False

    def _relay_response(self, response, observer, stream, initial=b""):
        stopped = threading.Event()
        write_lock = threading.Lock()
        boundary = True
        tail = b""

        def keepalive():
            while not stopped.wait(15):
                with write_lock:
                    if boundary and not self.downstream_stopped.is_set() and not self.server.owner._stopping.is_set():
                        try:
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.flush()
                            self.response_bytes += 14
                        except OSError:
                            self.downstream_stopped.set()
                            return

        heartbeat = threading.Thread(target=keepalive, daemon=True) if stream else None
        if heartbeat is not None:
            heartbeat.start()
        try:
            if initial and not self.downstream_stopped.is_set() and not self.server.owner._stopping.is_set():
                try:
                    self.wfile.write(initial)
                    self.wfile.flush()
                    self.response_bytes += len(initial)
                except OSError:
                    self.downstream_stopped.set()
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    return True
                if observer is not None:
                    observer.feed(chunk)
                with write_lock:
                    tail = (tail + chunk)[-4:]
                    boundary = tail.endswith((b"\n\n", b"\r\n\r\n"))
                    if not self.downstream_stopped.is_set() and not self.server.owner._stopping.is_set():
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            self.response_bytes += len(chunk)
                        except OSError:
                            self.downstream_stopped.set()
        finally:
            stopped.set()
            if heartbeat is not None:
                heartbeat.join()

    def _read_buffered_response(self, response, observer):
        body = bytearray()
        while True:
            chunk = response.read1(65536)
            if not chunk:
                if response.length not in (None, 0):
                    raise http.client.IncompleteRead(bytes(body), response.length)
                return bytes(body)
            if len(body) + len(chunk) > MAX_BODY_BYTES:
                if observer is not None:
                    observer.error = observer.error or "response_too_large"
                return None
            body.extend(chunk)
            if observer is not None:
                observer.feed(chunk)

    def _send_buffered_response(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)
            self.wfile.flush()
            self.response_bytes = len(body)

    def do_POST(self):
        self.close_connection = True
        owner = self.server.owner
        if self.path != REQUEST_PATH or self.requestline.split()[1] != REQUEST_PATH:
            self._error(404)
            return
        auth = self.headers.get_all("Authorization", [])
        if len(auth) != 1 or not hmac.compare_digest(auth[0], "Bearer " + owner.token):
            self._error(401)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1:
            self._error(400)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self._error(400)
            return
        if not 0 < length <= MAX_BODY_BYTES:
            self._error(413)
            return
        if self.headers.get_content_type() != "application/json":
            self._error(415)
            return
        try:
            raw = self.rfile.read(length)
            self.request_bytes = len(raw)
            if len(raw) != length:
                self._error(400)
                return
            payload = _strict_json(raw)
        except (ValueError, UnicodeError, RecursionError):
            self._error(400)
            return
        if not valid_request(payload, owner.model):
            self._error(400)
            return
        try:
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, RecursionError):
            self._error(400)
            return
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or owner._stopping.is_set():
            return
        row, rejection = owner._prepare_usage(payload)
        self.audit_request_id = row["request_id"] if row is not None else None
        if rejection is not None:
            self._error(rejection)
            return
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        observer = None
        sent_headers = False
        complete = False
        upstream_empty = False
        response_status = None
        transport_failure = False
        dispatched = False
        attempts = []
        for attempt_index in range(MAX_TRANSIENT_ATTEMPTS):
            observer = _UsageObserver(bool(payload.get("stream"))) if row is not None else None
            upstream = owner._new_connection(remaining + owner.drain_timeout)
            response = None
            complete = False
            upstream_empty = False
            response_status = None
            attempt = {"attempt": attempt_index + 1, "dispatched": False,
                       "status": None, "complete": False, "error": None}
            retry = False
            try:
                upstream.connect()
                self.upstream_socket = upstream.sock
                with owner._lock:
                    owner._upstreams.add(self.upstream_socket)
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or owner._stopping.is_set():
                    attempt["error"] = "request_deadline"
                    raise _AttemptFinished
                if (attempt_index and owner.cost_guard is not None
                        and not owner.cost_guard.reserve(row["request_id"], row["reserved_output_tokens"],
                                                         1 + sum(item["dispatched"] for item in attempts))):
                    attempt["error"] = "experiment_cost_guard"
                    self._error(429, "experiment_cost_guard")
                    raise _AttemptFinished
                if not dispatched:
                    if not owner._mark_dispatched(row):
                        attempt["error"] = "gateway_stopping"
                        raise _AttemptFinished
                    dispatched = True
                attempt["dispatched"] = True
                upstream.request("POST", owner.upstream_path, body=raw, headers={
                    "Authorization": "Bearer " + owner._api_key,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream" if payload.get("stream") else "application/json",
                    "Connection": "close",
                })
                response = upstream.getresponse()
                response_status = response.status
                attempt["status"] = response.status
                if response.status in TRANSIENT_UPSTREAM_STATUSES:
                    attempt["error"] = "upstream_transport_error"
                    if attempt_index + 1 < MAX_TRANSIENT_ATTEMPTS:
                        retry = True
                    else:
                        transport_failure = True
                        if not self.downstream_stopped.is_set() and not owner._stopping.is_set():
                            self._error(502, "upstream_transport_error", retryable=True)
                    raise _AttemptFinished
                if 300 <= response.status < 400:
                    attempt["error"] = "upstream_redirect"
                    if not self.downstream_stopped.is_set() and not owner._stopping.is_set():
                        self._error(502)
                    raise _AttemptFinished
                self.audit_status = response.status
                content_type = response.getheader("Content-Type", "application/json")
                if observer is not None:
                    observer.stream = content_type.startswith("text/event-stream")
                stream = content_type.startswith("text/event-stream")
                if stream:
                    buffered = bytearray()
                    while not observer.generated_content and not observer.error and not observer.upstream_error:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        if len(buffered) + len(chunk) > MAX_BODY_BYTES:
                            observer.error = "response_too_large"
                            break
                        buffered.extend(chunk)
                        observer.feed(chunk)
                    if observer.generated_content:
                        sent_headers = self._start_stream(response.status, content_type)
                        if sent_headers:
                            complete = self._relay_response(
                                response, observer, True, initial=bytes(buffered))
                        attempt["complete"] = complete
                        if not complete and observer.error:
                            attempt["error"] = observer.error
                        raise _AttemptFinished
                    observer.finish()
                    valid_empty = (
                        observer.done and observer.error is None and not observer.upstream_error
                        and observer.usage is not None
                    )
                    if valid_empty:
                        sent_headers = self._start_stream(response.status, content_type)
                        if sent_headers:
                            try:
                                self.wfile.write(buffered)
                                self.wfile.flush()
                                self.response_bytes += len(buffered)
                                complete = True
                            except OSError:
                                self.downstream_stopped.set()
                        attempt["complete"] = complete
                        raise _AttemptFinished
                    retry = (not self.downstream_stopped.is_set() and not owner._stopping.is_set()
                             and attempt_index + 1 < MAX_TRANSIENT_ATTEMPTS)
                    if not retry:
                        transport_failure = (not self.downstream_stopped.is_set()
                                             and not owner._stopping.is_set())
                        if transport_failure:
                            self._error(502, "upstream_transport_error", retryable=True)
                    attempt["error"] = observer.error or "incomplete_stream"
                    raise _AttemptFinished

                body = self._read_buffered_response(response, observer)
                if body is None:
                    attempt["error"] = observer.error if observer is not None else "upstream_response_unavailable"
                    if not self.downstream_stopped.is_set() and not owner._stopping.is_set():
                        self._error(502)
                    raise _AttemptFinished

                complete = True
                upstream_empty = response.status == 200 and not body
                malformed_no_generation = (
                    response.status == 200 and observer is not None
                    and not observer.generated_content and (observer.error or observer.upstream_error)
                )
                retry = upstream_empty or malformed_no_generation
                attempt["complete"] = True
                if retry:
                    attempt["error"] = "upstream_transport_error"
                    if attempt_index + 1 < MAX_TRANSIENT_ATTEMPTS:
                        retry = True
                    else:
                        retry = False
                        transport_failure = True
                        if not self.downstream_stopped.is_set() and not owner._stopping.is_set():
                            self._error(502, "upstream_transport_error", retryable=True)
                    raise _AttemptFinished

                if not self.downstream_stopped.is_set() and not owner._stopping.is_set():
                    try:
                        self._send_buffered_response(response.status, content_type, body)
                        sent_headers = True
                    except OSError:
                        self.downstream_stopped.set()
                raise _AttemptFinished
            except _AttemptFinished:
                pass
            except (OSError, http.client.HTTPException) as error:
                attempt["error"] = type(error).__name__
                complete = False
                retry = (not sent_headers and observer is not None
                         and not observer.generated_content
                         and attempt_index + 1 < MAX_TRANSIENT_ATTEMPTS)
                if not retry:
                    transport_failure = (not sent_headers and observer is not None
                                         and not observer.generated_content)
                    if not sent_headers and not self.downstream_stopped.is_set() and not owner._stopping.is_set():
                        self._error(502, "upstream_transport_error", retryable=True)
            finally:
                if response is not None:
                    response.close()
                upstream.close()
                with owner._lock:
                    owner._upstreams.discard(self.upstream_socket)

            if retry:
                delay = owner.retry_backoff_seconds[attempt_index]
                attempt["retry_delay_seconds"] = delay
                attempts.append(attempt)
                owner.retry_sleep(delay)
                continue
            attempts.append(attempt)
            break

        if row is not None:
            row["upstream_attempts"] = attempts
            row["attempt_count"] = len(attempts)
            row["retry_count"] = max(0, len(attempts) - 1)
        owner._finalize_usage(row, observer, response_status, complete,
                              upstream_empty=upstream_empty,
                              transport_failure=transport_failure)


class ModelGateway:
    def __init__(self, base_url, api_key, model, timeout, log_path, policy=None, usage_path=None,
                 socket_root=None, drain_timeout=60, max_requests=None, cost_guard=None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(ord(char) < 33 for char in base_url) or "\\" in base_url):
            raise ValueError("模型 base_url 格式无效")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("模型网关 timeout 必须为正有限值")
        if not math.isfinite(drain_timeout) or drain_timeout <= 0:
            raise ValueError("模型网关 drain_timeout 必须为正有限值")
        if max_requests is not None and (type(max_requests) is not int or max_requests < 1):
            raise ValueError("模型网关 max_requests 必须为正整数")
        self.model = model
        self.timeout = timeout
        self.drain_timeout = drain_timeout
        self.log_path = Path(log_path)
        self.socket_root = str(socket_root) if socket_root is not None else "/tmp"
        self.policy = dict(policy) if policy is not None else None
        if self.policy is not None and self.policy.get("protocol") == "openai_chat":
            expected = {"protocol", "temperature", "top_p", "max_output_tokens",
                        "max_total_generated_tokens", "thinking", "reasoning_effort"}
            thinking = self.policy.get("thinking")
            if (set(self.policy) != expected
                    or any(type(self.policy.get(key)) not in (int, float) for key in ("temperature", "top_p"))
                    or not 0 <= self.policy["temperature"] <= 2
                    or not 0 < self.policy["top_p"] <= 1
                    or any(not math.isfinite(self.policy[key]) for key in ("temperature", "top_p"))
                    or any(type(self.policy.get(key)) is not int or self.policy[key] < 1
                           for key in ("max_output_tokens", "max_total_generated_tokens"))
                    or not isinstance(thinking, dict) or set(thinking) != {"type"}
                    or thinking["type"] not in ("enabled", "disabled")
                    or self.policy.get("reasoning_effort") not in ("low", "high", "max")):
                raise ValueError("API 模型网关要求有效的冻结采样、思考参数与预算策略")
            self.policy["thinking"] = dict(thinking)
        elif self.policy is not None:
            temperature = {"qwen35-local": .6, "qwen38-local": 1.0}.get(model)
            expected = dict(zip(SAMPLING_FIELDS, (temperature, .95, 20, 0, 0, 1)))
            if (any(self.policy.get(key) != value for key, value in expected.items())
                    or any(type(self.policy.get(key)) not in (int, float) for key in SAMPLING_FIELDS)
                    or type(self.policy.get("top_k")) is not int
                    or self.policy.get("enable_thinking") is not True
                    or type(self.policy.get("seed")) is not int or not 0 <= self.policy["seed"] < 2**31
                    or type(self.policy.get("max_output_tokens")) is not int or not 1 <= self.policy["max_output_tokens"] <= 32768
                    or type(self.policy.get("max_total_generated_tokens")) is not int or self.policy["max_total_generated_tokens"] < 1):
                raise ValueError("模型网关要求对应模型的冻结采样与预算策略")
        self.usage_path = Path(usage_path) if usage_path is not None else (
            self.log_path.with_suffix(".usage.json") if self.policy is not None else None)
        if self.usage_path == self.log_path:
            raise ValueError("用量文件必须与请求审计日志分开")
        self._budget_lock = threading.Lock()
        self._usage_file = None
        self._started_file = None
        self._usage_requests = []
        self._reservations = {}
        self._generated_tokens = 0
        self._prompt_tokens = 0
        self._uncertain_tokens = 0
        self._accounting_error = None
        self.token = secrets.token_urlsafe(32)
        self.socket_path = None
        self._api_key = api_key
        self._host = parsed.hostname
        self._port = parsed.port
        self._https = parsed.scheme == "https"
        self.upstream_path = parsed.path.rstrip("/") + "/chat/completions"
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_WORKERS)
        self._clients = set()
        self._upstreams = set()
        self._workers = set()
        self._stopping = threading.Event()
        self._drain_lock = threading.Lock()
        self._drained = False
        self._max_requests = max_requests
        self.cost_guard = cost_guard
        self._request_limit_exhausted = False
        self.retry_backoff_seconds = RETRY_BACKOFF_SECONDS
        self.retry_sleep = time.sleep

    def _usage_summary_locked(self):
        limit = self.policy["max_total_generated_tokens"] if self.policy is not None else None
        return {"generated_tokens": self._generated_tokens, "prompt_tokens": self._prompt_tokens,
                "reserved_tokens": sum(self._reservations.values()),
                "uncertain_reserved_tokens": self._uncertain_tokens,
                "max_total_generated_tokens": limit,
                "budget_exhausted": limit is not None and self._generated_tokens >= limit,
                "max_requests": self._max_requests,
                "request_limit_exhausted": self._request_limit_exhausted,
                "gateway_automatic_retries": sum(
                    row.get("retry_count", 0) for row in self._usage_requests),
                "accounting_failed": self._accounting_error is not None,
                "accounting_error": self._accounting_error,
                "request_count": len(self._usage_requests)}

    @property
    def usage_summary(self):
        with self._budget_lock:
            return self._usage_summary_locked()

    def _persist_usage_locked(self):
        if self._usage_file is not None:
            try:
                self._usage_file.seek(0)
                json.dump({"schema_version": 2, "started_ledger": self.usage_path.with_suffix(".started.jsonl").name,
                           "policy": self.policy, "summary": self._usage_summary_locked(),
                           "requests": self._usage_requests}, self._usage_file, ensure_ascii=False, indent=2)
                self._usage_file.write("\n")
                self._usage_file.truncate()
                self._usage_file.flush()
                os.fsync(self._usage_file.fileno())
            except OSError:
                self._accounting_error = "usage_log_write_failed"
                raise

    def _prepare_usage(self, payload):
        if self.policy is None and self.usage_path is None:
            return None, None
        with self._budget_lock:
            if self._stopping.is_set():
                return None, 503
            row = {"request_id": secrets.token_hex(12), "model": self.model,
                   "operation": "chat_completions", "started_time_ns": time.time_ns(),
                   "started_monotonic_ns": time.monotonic_ns(),
                   "started_at": datetime.now(timezone.utc).isoformat(),
                   "backend_dispatched": False, "usage": None, "state": "prepared"}
            self._usage_requests.append(row)
            audit_fields = ("model", "temperature", "top_p", "thinking", "reasoning_effort",
                            "max_tokens", "max_completion_tokens", "stream", "n")
            row["sdk_parameters"] = {key: payload[key] for key in audit_fields if key in payload}
            status = None
            if self._max_requests is not None and len(self._usage_requests) > self._max_requests:
                self._request_limit_exhausted = True
                status, row["reason"] = 429, "model_request_limit_exhausted"
            elif self.policy is not None:
                limits = [payload[key] for key in ("max_tokens", "max_completion_tokens") if key in payload]
                if any(type(value) is not int or value < 1 for value in limits):
                    status, row["reason"] = 400, "invalid_max_tokens"
                elif self._accounting_error:
                    status, row["reason"] = 409, "generated_token_accounting_failed"
                else:
                    remaining = self.policy["max_total_generated_tokens"] - self._generated_tokens - sum(self._reservations.values())
                    if remaining <= 0:
                        exhausted = self._generated_tokens >= self.policy["max_total_generated_tokens"]
                        status, row["reason"] = (409, "generated_token_budget_exhausted") if exhausted else (429, "generated_token_budget_reserved")
                    else:
                        reserved = min([self.policy["max_output_tokens"], remaining, *limits])
                        self._reservations[row["request_id"]] = reserved
                        payload.pop("max_completion_tokens", None)
                        if self.policy.get("protocol") == "openai_chat":
                            for key in (*SAMPLING_FIELDS, "frequency_penalty", "seed", "chat_template_kwargs"):
                                payload.pop(key, None)
                            payload.update(temperature=self.policy["temperature"], top_p=self.policy["top_p"],
                                           thinking=dict(self.policy["thinking"]),
                                           reasoning_effort=self.policy["reasoning_effort"], max_tokens=reserved, n=1)
                            row.update(reserved_output_tokens=reserved, effective_max_tokens=reserved,
                                       effective_sampling={key: payload[key] for key in ("temperature", "top_p")},
                                       thinking=dict(payload["thinking"]), reasoning_effort=payload["reasoning_effort"], n=1)
                        else:
                            payload.pop("reasoning_effort", None)
                            payload.update({key: self.policy[key] for key in SAMPLING_FIELDS})
                            payload.update(max_tokens=reserved, seed=self.policy["seed"], n=1,
                                           chat_template_kwargs={"enable_thinking": True})
                            row.update(reserved_output_tokens=reserved, effective_max_tokens=reserved,
                                       effective_sampling={key: payload[key] for key in SAMPLING_FIELDS},
                                       enable_thinking=True, seed=self.policy["seed"], n=1)
                        if self.cost_guard is not None and not self.cost_guard.reserve(
                                row["request_id"], reserved, 1):
                            self._reservations.pop(row["request_id"])
                            status, row["reason"] = 429, "experiment_cost_guard"
            if payload.get("stream"):
                payload["stream_options"] = {"include_usage": True}
            if status is None:
                row["outbound_parameters"] = {key: payload[key] for key in audit_fields if key in payload}
            if status is not None:
                row.update(state="rejected", http_status=status, complete=False,
                           finished_time_ns=time.time_ns(),
                           duration_seconds=(time.monotonic_ns() - row["started_monotonic_ns"]) / 1e9,
                           finished_at=datetime.now(timezone.utc).isoformat())
            self._persist_usage_locked()
            return row, status

    def _mark_dispatched(self, row):
        with self._lock:
            if self._stopping.is_set():
                return False
            if row is not None:
                with self._budget_lock:
                    row["backend_dispatched"] = True
                    if self._started_file is not None:
                        self._started_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                        self._started_file.flush()
                        os.fsync(self._started_file.fileno())
                    self._persist_usage_locked()
            return True

    def _finalize_usage(self, row, observer, status, complete, upstream_empty=False,
                        transport_failure=False):
        if row is None:
            return
        observer.finish()
        with self._budget_lock:
            if row["state"] != "prepared":
                return
            reserved = self._reservations.pop(row["request_id"], 0)
            usage = observer.usage
            document = observer.document
            context_rejected = (status == 400 and isinstance(document, dict)
                                and document.get("object") == "error"
                                and document.get("type") == "BadRequestError"
                                and document.get("code") == 400
                                and isinstance(document.get("message"), str)
                                and "maximum context length" in document["message"])
            no_generation = not row["backend_dispatched"] or (
                complete and status is not None and 400 <= status < 600
                and not observer.generated_content and observer.error is None
                and isinstance(document, dict) and ("error" in document or context_rejected)
                and (usage is None or usage["completion_tokens"] == 0))
            valid = complete and status == 200 and observer.error is None and not observer.upstream_error and usage is not None
            upstream_empty = upstream_empty and complete and usage is None and not observer.generated_content
            error = None
            if valid and self.policy is not None and usage["completion_tokens"] > reserved:
                valid, error = False, "completion_tokens_exceed_reservation"
            transport_failure = transport_failure and not observer.generated_content
            attempts = row.get("upstream_attempts") or []
            uncertain_attempts = sum(attempt.get("dispatched") is True for attempt in (
                attempts[:-1] if valid else attempts))
            uncertain_tokens = reserved * uncertain_attempts
            if uncertain_tokens:
                self._uncertain_tokens += uncertain_tokens
            row["uncertain_reserved_tokens"] = uncertain_tokens
            if valid:
                self._generated_tokens += usage["completion_tokens"]
                self._prompt_tokens += usage["prompt_tokens"]
                row["state"] = "accounted"
            elif transport_failure:
                row.update(state="upstream_error_no_generation", reason="upstream_transport_error",
                           upstream_http_status=status, http_status=502, gateway_http_status=502)
            elif upstream_empty:
                row.update(state="upstream_error_no_generation", reason="upstream_empty_response",
                           upstream_http_status=status, http_status=502 if status == 200 else status,
                           gateway_http_status=502 if status == 200 else status)
            elif no_generation:
                if usage is not None:
                    self._prompt_tokens += usage["prompt_tokens"]
                row["state"] = "upstream_error_no_generation" if row["backend_dispatched"] else "not_dispatched"
                if context_rejected:
                    row["reason"] = "context_limit"
            else:
                error = error or observer.error or ("upstream_generation_error" if observer.upstream_error else None)
                error = error or ("missing_usage" if complete else "incomplete_response")
                if not uncertain_tokens:
                    self._uncertain_tokens += reserved
                    row["uncertain_reserved_tokens"] = reserved
                self._accounting_error = self._accounting_error or error
                row.update(state="accounting_failed", reason=error)
            row.update(usage=usage, http_status=row.get("http_status", status), complete=complete,
                       finish_reason=observer.finish_reason, content_bytes=observer.content_bytes,
                       reasoning_bytes=observer.reasoning_bytes,
                       finished_time_ns=time.time_ns(),
                       duration_seconds=(time.monotonic_ns() - row["started_monotonic_ns"]) / 1e9,
                       finished_at=datetime.now(timezone.utc).isoformat())
            if isinstance(document, dict) and isinstance(document.get("model"), str):
                row["response_model"] = document["model"]
            if self.cost_guard is not None:
                self.cost_guard.settle(row["request_id"], usage if valid else None,
                                       uncertain_attempts, row["backend_dispatched"])
            self._persist_usage_locked()

    def _new_connection(self, timeout):
        cls = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return cls(self._host, self._port, timeout=timeout)

    def _record(self, method, path, status, request_bytes, response_bytes, elapsed, request_id=None):
        record = {"time": datetime.now(timezone.utc).isoformat(), "method": method,
                  "path": path, "status": status, "model": self.model, "request_id": request_id,
                  "request_bytes": request_bytes, "response_bytes": response_bytes,
                  "elapsed_seconds": round(elapsed, 6)}
        with self._log_lock:
            if self._log.closed:
                return
            self._log.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._log.flush()

    def __enter__(self):
        # 日志可能位于任务输出目录；固定已打开的文件，拒绝符号链接替换。
        fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("网关审计日志必须为普通文件")
        self._log = os.fdopen(fd, "a", encoding="utf-8")
        self._directory = tempfile.TemporaryDirectory(prefix="hgw-", dir=self.socket_root)
        self.socket_path = str(Path(self._directory.name) / "model.sock")
        try:
            if self.usage_path is not None:
                fd = os.open(self.usage_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise ValueError("模型用量日志必须为普通文件")
                self._usage_file = os.fdopen(fd, "r+", encoding="utf-8")
                started_path = self.usage_path.with_suffix(".started.jsonl")
                fd = os.open(started_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                self._started_file = os.fdopen(fd, "w", encoding="utf-8")
                with self._budget_lock:
                    self._persist_usage_locked()
            self._server = _UnixServer(self.socket_path, _Handler)
            self._server.owner = self
            os.chmod(self.socket_path, 0o600)
            self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            self._thread.start()
        except BaseException:
            self._directory.cleanup()
            self._log.close()
            if self._usage_file is not None:
                self._usage_file.close()
            if self._started_file is not None:
                self._started_file.close()
            raise
        return self

    def drain(self, timeout=None):
        """停止下游与新请求，只在限定时间内收集已发请求的最终用量。"""
        if timeout is None:
            timeout = self.drain_timeout
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("排空等待时间必须为非负有限值")
        with self._drain_lock:
            if self._drained:
                return self.usage_summary
            deadline = time.monotonic() + timeout
            with self._lock:
                self._stopping.set()
                clients = list(self._clients)
            self._server.shutdown()
            self._server.server_close()
            for client in clients:
                try:
                    client.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._thread.join(timeout=max(0, deadline - time.monotonic()))
            with self._lock:
                workers = list(self._workers)
            for worker in workers:
                worker.join(timeout=max(0, deadline - time.monotonic()))
            with self._lock:
                upstreams = list(self._upstreams)
            for upstream in upstreams:
                try:
                    upstream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            with self._budget_lock:
                for row in self._usage_requests:
                    if row["state"] == "prepared":
                        reserved = self._reservations.pop(row["request_id"], 0)
                        row.update(state="not_dispatched", complete=False,
                                   finished_time_ns=time.time_ns(),
                                   duration_seconds=(time.monotonic_ns() - row["started_monotonic_ns"]) / 1e9,
                                   finished_at=datetime.now(timezone.utc).isoformat())
                        if row["backend_dispatched"]:
                            self._uncertain_tokens += reserved
                            self._accounting_error = self._accounting_error or "gateway_shutdown_during_generation"
                            row.update(state="accounting_failed", reason="gateway_shutdown_during_generation")
                self._persist_usage_locked()
            self._drained = True
            return self.usage_summary

    def __exit__(self, exc_type, exc, traceback):
        self.drain()
        if self._usage_file is not None:
            with self._budget_lock:
                self._usage_file.close()
        if self._started_file is not None:
            self._started_file.close()
        with self._log_lock:
            self._log.close()
        self._directory.cleanup()
