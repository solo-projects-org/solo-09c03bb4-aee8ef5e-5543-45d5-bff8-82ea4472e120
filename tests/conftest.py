"""Shared test fixtures: run the demo origin and the web app in-process."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.capture import CaptureService
from app.config import Settings
from app.db import Database
from app.main import create_app
from origin import server as origin_server


@pytest.fixture()
def origin_state():
    origin_server.reset_state()
    yield origin_server.SIM
    origin_server.reset_state()


@pytest.fixture()
def origin_client(origin_state):
    """In-process HTTP client for the demo origin (no sockets needed)."""
    with TestClient(origin_server.app, base_url="http://origin.test") as client:
        yield client


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=str(tmp_path / "archive.sqlite3"),
        origin_base_url="http://origin.test",
        request_timeout_s=5.0,
        channel_config=(
            "news|新闻综合|http://origin.test/channel/news/master.m3u8,"
            "sports|体育赛事|http://origin.test/channel/sports/master.m3u8"
        ),
    )


class Harness:
    """A web app wired to the in-process origin via an ASGI transport."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.settings = make_settings(tmp_path)
        self.db = Database(self.settings.database_path)
        self.transport = httpx.ASGITransport(app=origin_server.app)
        self.capture = CaptureService(self.db, timeout_s=5.0, transport=self.transport)
        self.app = create_app(
            settings=self.settings,
            db=self.db,
            capture=self.capture,
            origin_transport=self.transport,
        )
        self._client_cm = TestClient(self.app, base_url="http://web.test")
        self.client = self._client_cm.__enter__()

    def capture_channel(self, channel_id: str = "news") -> dict:
        response = self.client.post(f"/api/channels/{channel_id}/capture")
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def archive(self, channel_id: str = "news", **params) -> dict:
        response = self.client.get(f"/api/channels/{channel_id}/archive", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def advance(self, segments: int, channel_id: str | None = None) -> None:
        body = {"segments": segments}
        if channel_id:
            body["channel"] = channel_id
        response = self.client.post("/api/demo/advance", json=body)
        assert response.status_code == 200, response.text

    def set_gap(self, seq: int, missing: bool = True, channel_id: str = "news") -> None:
        response = self.client.post(
            "/api/demo/gap",
            json={"channel": channel_id, "seq": seq, "missing": missing},
        )
        assert response.status_code == 200, response.text

    def freeze(self, frozen: bool = True, channel_id: str | None = None) -> None:
        body = {"frozen": frozen}
        if channel_id:
            body["channel"] = channel_id
        response = self.client.post("/api/demo/freeze", json=body)
        assert response.status_code == 200, response.text

    def restart(self) -> None:
        """Re-open the database and rebuild the app, keeping the same file."""
        self._client_cm.__exit__(None, None, None)
        self.db = Database(self.settings.database_path)
        self.capture = CaptureService(self.db, timeout_s=5.0, transport=self.transport)
        self.app = create_app(
            settings=self.settings,
            db=self.db,
            capture=self.capture,
            origin_transport=self.transport,
        )
        self._client_cm = TestClient(self.app, base_url="http://web.test")
        self.client = self._client_cm.__enter__()

    def close(self) -> None:
        self._client_cm.__exit__(None, None, None)

    def segments_by_seq(self, view: dict) -> dict[int, dict]:
        return {segment["seq"]: segment for segment in view["segments"]}


@pytest.fixture()
def harness(tmp_path):
    os.environ["ARCHIVE_DB"] = str(tmp_path / "archive.sqlite3")
    origin_server.reset_state()
    instance = Harness(tmp_path)
    try:
        yield instance
    finally:
        instance.close()
        origin_server.reset_state()
