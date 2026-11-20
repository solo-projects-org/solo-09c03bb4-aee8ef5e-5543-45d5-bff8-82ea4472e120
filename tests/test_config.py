"""Tests for configuration parsing."""
from __future__ import annotations

import pytest

from app.config import Settings


def _settings(config: str) -> Settings:
    return Settings(
        database_path=":memory:",
        origin_base_url="http://origin",
        request_timeout_s=1.0,
        channel_config=config,
    )


def test_channel_config_parses_triplets():
    channels = _settings(
        "a|甲|http://o/a.m3u8, b|乙|http://o/b.m3u8"
    ).channels
    assert channels == [
        {"id": "a", "name": "甲", "master_url": "http://o/a.m3u8"},
        {"id": "b", "name": "乙", "master_url": "http://o/b.m3u8"},
    ]


def test_channel_config_rejects_malformed_entry():
    with pytest.raises(ValueError):
        _settings("a|only-two-parts").channels


def test_channel_config_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        _settings("a|甲|http://o/a.m3u8,a|甲2|http://o/a2.m3u8").channels
