#!/usr/bin/env python3
"""Loopback OpenAI-compatible fake backend for dispatcher integration tests."""

import argparse
import json
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class State:
    def __init__(self, port: int, events_path: Path, start_delay: float, sse_chunks: int, sse_delay: float):
        self.port = port
        self.events_path = events_path
        self.start_delay = start_delay
        self.sse_chunks = sse_chunks
        self.sse_delay = sse_delay
        self.lock = threading.Lock()
        self.ready = False
        self.stopping = False
        self.in_flight = 0
        self.cancelled_requests = 0
        self.server = None

    def event(self, name: str, **fields):
        record = {"ts": time.time(), "port": self.port, "event": name, **fields}
        with self.lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")


def wait_for_stop_file(state: State, stop_file: Path):
    while not stop_file.exists():
        if state.stopping:
            return
        time.sleep(0.05)

    with state.lock:
        state.stopping = True
    state.event("stop_file_seen")

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        with state.lock:
            if state.in_flight == 0:
                break
        time.sleep(0.05)

    state.event("exit")
    if state.server:
        threading.Thread(target=state.server.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, fmt, *args):
        return

    def _json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            if self.state.ready and not self.state.stopping:
                self._json(HTTPStatus.OK, {"status": "ok"})
            else:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"status": "starting"})
            return

        if self.path == "/v1/models":
            self._json(HTTPStatus.OK, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})

    def do_POST(self):
        if self.path not in {"/v1/chat/completions", "/v1/responses"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": {"message": f"unknown path {self.path}", "type": "not_found"}})
            return

        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": {"message": "invalid json", "type": "invalid_request_error"}})
            return

        with self.state.lock:
            if self.state.stopping:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": {"message": "backend stopping", "type": "overloaded"}})
                return
            self.state.in_flight += 1
        self.state.event("request_start", model=payload.get("model"), stream=bool(payload.get("stream")))

        try:
            if payload.get("stream"):
                self._stream(payload, responses=self.path == "/v1/responses")
            elif self.path == "/v1/responses":
                self._json(
                    HTTPStatus.OK,
                    {
                        "id": "resp_fake",
                        "object": "response",
                        "model": payload.get("model", "fake-model"),
                        "status": "completed",
                        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"backend:{self.state.port}"}]}],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                )
            else:
                self._json(
                    HTTPStatus.OK,
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": payload.get("model", "fake-model"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": f"backend:{self.state.port}"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )
        except (BrokenPipeError, ConnectionResetError):
            self.state.event("client_disconnected", model=payload.get("model"))
        finally:
            with self.state.lock:
                self.state.in_flight -= 1
            self.state.event("request_end", model=payload.get("model"))

    def _stream(self, payload: dict, *, responses: bool = False):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        for index in range(self.state.sse_chunks):
            with self.state.lock:
                if self.state.stopping:
                    return
            chunk = ({
                "type": "response.output_text.delta",
                "delta": f"{index} ",
                "sequence_number": index,
            } if responses else {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": payload.get("model", "fake-model"),
                "choices": [{"index": 0, "delta": {"content": f"{index} "}, "finish_reason": None}],
            })
            try:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                with self.state.lock:
                    self.state.cancelled_requests += 1
                raise
            time.sleep(self.state.sse_delay)

        done = ({
            "type": "response.completed",
            "response": {
                "id": "resp_fake",
                "object": "response",
                "model": payload.get("model", "fake-model"),
                "status": "completed",
                "usage": {"input_tokens": 1, "output_tokens": self.state.sse_chunks, "total_tokens": self.state.sse_chunks + 1, "input_tokens_details": {"cached_tokens": 0}},
                "timings": {"predicted_per_second": 12.5, "prompt_per_second": 100.0},
            },
        } if responses else {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": payload.get("model", "fake-model"),
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": self.state.sse_chunks,
                "total_tokens": self.state.sse_chunks + 1,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        })
        self.wfile.write(f"data: {json.dumps(done)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--start-delay", type=float, default=0.2)
    parser.add_argument("--sse-chunks", type=int, default=6)
    parser.add_argument("--sse-delay", type=float, default=0.25)
    args = parser.parse_args()

    if args.port < 1024 or args.port > 65535:
        raise SystemExit("port out of range")

    state = State(args.port, args.events, args.start_delay, args.sse_chunks, args.sse_delay)
    Handler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    state.server = server

    def mark_stopping(signum, frame):
        with state.lock:
            state.stopping = True
        state.event("signal", signal=signum)
        if state.server:
            threading.Thread(target=state.server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, mark_stopping)
    signal.signal(signal.SIGINT, mark_stopping)

    threading.Thread(target=wait_for_stop_file, args=(state, args.stop_file), daemon=True).start()
    state.event("process_start")

    def become_ready():
        time.sleep(args.start_delay)
        with state.lock:
            if not state.stopping:
                state.ready = True
        state.event("ready")

    threading.Thread(target=become_ready, daemon=True).start()
    server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
