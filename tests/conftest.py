"""Shared test helpers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from nga_reminder.config import Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_local_env(monkeypatch):
    """别让开发者本地的 ``.env`` 渗进测试。

    ``.env`` 里可能有真凭据，也可能配了通知桥接地址——测试必须保持完全离线。
    """
    monkeypatch.setattr(
        Settings, "model_config", {**Settings.model_config, "env_file": None}
    )


def load_fixture(name: str) -> dict:
    """Load a captured NGA response from ``tests/fixtures``."""
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture missing: {name} (run scripts/capture_fixture.py)")
    return json.loads(path.read_text(encoding="utf-8"))


class RecordingTransport(httpx.MockTransport):
    """Mock transport that records every request it handled."""

    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self._handler = handler

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


@pytest.fixture
def recording_transport():
    return RecordingTransport
