"""Capture behaviour when the origin misbehaves."""
from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.capture import CaptureService
from app.db import Database
from app.main import create_app
from tests.conftest import make_settings


class _AlwaysFails(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.ConnectError("connection refused", request=request)


@pytest.fixture()
def broken_client(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings.database_path)
    service = CaptureService(db, timeout_s=1.0, transport=_AlwaysFails())
    app = create_app(
        settings=settings, db=db, capture=service, origin_transport=_AlwaysFails()
    )
    with TestClient(app) as client:
        yield client, db


def test_unreachable_origin_reports_note_and_captures_nothing(broken_client):
    client, db = broken_client
    response = client.post("/api/channels/news/capture")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["captured"] == 0
    assert result["fetched"] == 0
    assert any("unreachable" in note for note in result["notes"])

    # The archive is still queryable and empty, and segments are untouched.
    view = client.get("/api/channels/news/archive").json()
    assert view["counts"]["captured"] == 0
    assert view["bounds"] is None


def test_unreachable_origin_does_not_lose_previous_archive(broken_client):
    client, db = broken_client
    # Seed a captured segment directly, then fail every fetch.
    db.upsert_channel("news", "新闻综合", "http://origin.test/m.m3u8", 0)
    db.observe_playlist(
        "news",
        [{"seq": 1, "start_ms": 0, "end_ms": 6000, "sub_uri": "http://origin.test/1.vtt"}],
        1,
        2,
        0,
    )
    db.mark_captured("news", 1, [], captions_empty=True, now=0)

    response = client.post("/api/channels/news/capture")
    assert response.status_code == 200
    view = client.get("/api/channels/news/archive").json()
    assert view["counts"]["captured"] == 1
    assert view["segments"][0]["seq"] == 1
