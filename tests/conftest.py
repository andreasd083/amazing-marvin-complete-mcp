import json
import os
from pathlib import Path

# Pin the timezone BEFORE any marvin_mcp import (config resolves it at
# import time), so date/offset tests are deterministic on any machine.
os.environ["MARVIN_TIMEZONE"] = "Europe/Stockholm"

import httpx
import pytest

from marvin_mcp import server
from marvin_mcp.config import Settings
from marvin_mcp import ratelimit


@pytest.fixture(autouse=True)
def fast_ratelimit(monkeypatch):
    """Short intervals in tests so they run fast; the logic is the same."""
    monkeypatch.setattr(ratelimit, "CALL_INTERVAL_S", 0.02)
    monkeypatch.setattr(ratelimit, "CREATE_INTERVAL_S", 0.01)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="testapitoken",
        full_access_token="testfulltoken",
        mcp_auth_token="testmcptoken",
        state_dir=tmp_path,
    )


class RecordingTransport(httpx.MockTransport):
    """MockTransport that records all requests for inspection."""

    def __init__(self, handler=None):
        self.requests: list[httpx.Request] = []
        self.responses: dict[str, httpx.Response] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if handler is not None:
                return handler(request)
            key = request.url.path.removeprefix("/api")
            if key in self.responses:
                return self.responses[key]
            return httpx.Response(200, json={"ok": True})

        super().__init__(_handler)

    def last_json(self):
        return json.loads(self.requests[-1].content)


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture
def init_server(settings, transport):
    server.init(settings, transport=transport)
    yield server
