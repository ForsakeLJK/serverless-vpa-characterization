"""HTTP wrapper around the SeBS function (spec section 3). Standard library only.

One process per request, forked from a parent that has already imported the function
and its dependencies, so every invocation is warm. At most CONCURRENCY_CAP requests run
at once; further connections wait in the parent's queue. The parent is single-threaded,
which keeps fork() safe.

POST / on PORT        body: the SeBS event as JSON; response: the handler's return value.
GET  / on HEALTH_PORT answered by the parent without forking (readiness probe).
"""

from __future__ import annotations

import collections
import importlib
import json
import os
import selectors
import signal
import socket
from typing import Any, Callable

Handler = Callable[[dict[str, Any]], Any]


def _listen(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(4096)
    s.setblocking(False)
    return s


def _read_request(conn: socket.socket) -> bytes:
    """Read one HTTP request and return its body."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            raise ConnectionError("client closed before sending a request")
        data += chunk
    head, body = data.split(b"\r\n\r\n", 1)
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(body) < length:
        chunk = conn.recv(65536)
        if not chunk:
            raise ConnectionError("client closed mid-body")
        body += chunk
    return body


def _respond(conn: socket.socket, status: str, body: bytes) -> None:
    head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    conn.sendall(head.encode() + body)


def _serve_one(conn: socket.socket, handler: Handler) -> None:
    """Runs in the forked child."""
    conn.setblocking(True)
    try:
        event = json.loads(_read_request(conn))
        body = json.dumps(handler(event)).encode()
        _respond(conn, "200 OK", body)
    except ConnectionError:
        pass
    except Exception as e:  # report to the client; the child exits either way
        _respond(conn, "500 Internal Server Error", json.dumps({"error": repr(e)}).encode())
    finally:
        conn.close()


def _answer_health(conn: socket.socket) -> None:
    try:
        conn.settimeout(1.0)
        conn.recv(65536)
        _respond(conn, "200 OK", b'"ok"')
    except OSError:
        pass
    finally:
        conn.close()


def serve(handler: Handler, port: int, health_port: int, cap: int) -> None:
    function_sock, health_sock = _listen(port), _listen(health_port)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_r, False)
    os.set_blocking(wake_w, False)
    signal.signal(signal.SIGCHLD, lambda *_: None)
    signal.set_wakeup_fd(wake_w)

    sel = selectors.DefaultSelector()
    for fileobj in (function_sock, health_sock, wake_r):
        sel.register(fileobj, selectors.EVENT_READ)

    waiting: collections.deque[socket.socket] = collections.deque()
    running = 0
    while True:
        for key, _ in sel.select(timeout=1.0):
            if key.fileobj is wake_r:
                try:
                    os.read(wake_r, 4096)
                except BlockingIOError:
                    pass
                continue
            listener: socket.socket = key.fileobj  # type: ignore[assignment]
            while True:
                try:
                    conn, _ = listener.accept()
                except BlockingIOError:
                    break
                if listener is health_sock:
                    _answer_health(conn)
                else:
                    waiting.append(conn)

        while running:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                running = 0
                break
            if pid == 0:
                break
            running -= 1

        while waiting and running < cap:
            conn = waiting.popleft()
            pid = os.fork()
            if pid == 0:
                signal.set_wakeup_fd(-1)
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                function_sock.close()
                health_sock.close()
                try:
                    _serve_one(conn, handler)
                finally:
                    os._exit(0)
            conn.close()
            running += 1


if __name__ == "__main__":
    module = importlib.import_module(os.environ.get("FUNCTION_MODULE", "function"))
    serve(
        module.handler,
        port=int(os.environ.get("PORT", "8080")),
        health_port=int(os.environ.get("HEALTH_PORT", "8081")),
        cap=int(os.environ.get("CONCURRENCY_CAP", "64")),
    )
