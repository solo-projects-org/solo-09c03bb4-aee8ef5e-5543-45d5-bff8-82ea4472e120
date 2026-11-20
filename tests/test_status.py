"""Unit tests for the monotonic segment-status rule."""
from __future__ import annotations

from app.db import transition_allowed


def test_captured_is_terminal():
    assert transition_allowed("captured", "pending") is False
    assert transition_allowed("captured", "missing") is False
    assert transition_allowed("captured", "captured") is False


def test_pending_can_become_missing_or_captured():
    assert transition_allowed("pending", "missing") is True
    assert transition_allowed("pending", "captured") is True


def test_missing_can_only_be_healed():
    assert transition_allowed("missing", "captured") is True
    assert transition_allowed("missing", "pending") is False
    assert transition_allowed("missing", "missing") is False


def test_unknown_statuses_are_rejected():
    assert transition_allowed("bogus", "captured") is False
    assert transition_allowed("pending", "bogus") is False
