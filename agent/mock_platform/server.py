"""Serve an ASGI app (the mock platform, or the agent service) over real HTTP, in a
background thread or standalone.

Standalone (seeded demo family): ``python -m mock_platform.server``
"""

from __future__ import annotations

import threading
import time

import uvicorn

from mock_platform.app import MockPlatform


class BackgroundServer:
    def __init__(self, app, host: str = "127.0.0.1"):
        self._server = uvicorn.Server(uvicorn.Config(app, host=host, port=0, log_level="warning"))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.host = host

    def start(self) -> str:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        return f"http://{self.host}:{port}"

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


if __name__ == "__main__":
    from mock_platform.seed import seed_family

    mock = MockPlatform()
    seed_family(mock)
    uvicorn.run(mock.app, host="127.0.0.1", port=8200)
