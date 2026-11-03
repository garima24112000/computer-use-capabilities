"""Run the target app in a background thread (for tests and dev scripts)."""

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from target_app.app import create_app


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def serve_in_thread(tenant: str = "a", port: int | None = None) -> Iterator[str]:
    """Start Meridian Core on 127.0.0.1 and yield its base URL; stop it on exit."""
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(tenant), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, name=f"target-app-{tenant}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError(f"target app (tenant {tenant}) failed to start on port {port}")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
