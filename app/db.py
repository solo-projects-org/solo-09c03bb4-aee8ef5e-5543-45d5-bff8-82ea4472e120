"""SQLite archive storage.

Segment status is monotonic.  The numeric rank below is the only thing that
decides whether a write may change a status; this is what makes re-captures and
stale playlists harmless:

    pending (seen in a playlist, not yet fetched)
        -> captured (fetched, including a valid but caption-less file)
        -> missing  (slid out of the playlist without a successful fetch)

``captured`` is terminal, a ``missing`` segment may still be upgraded to
``captured`` by a genuinely successful late fetch, and nothing ever moves a
captured segment back to pending or missing.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Optional

STATUS_PENDING = "pending"
STATUS_CAPTURED = "captured"
STATUS_MISSING = "missing"

_STATUS_RANK = {STATUS_PENDING: 0, STATUS_MISSING: 1, STATUS_CAPTURED: 2}


def transition_allowed(current: str, target: str) -> bool:
    """Whether a segment may move ``current -> target``.

    The rank never decreases, and a pending segment may not jump straight to
    missing twice or back.  The only upward edges are pending -> missing,
    pending -> captured and missing -> captured.
    """
    if current == target:
        return False
    if current not in _STATUS_RANK or target not in _STATUS_RANK:
        return False
    return _STATUS_RANK[target] > _STATUS_RANK[current]

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS channels (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    master_url       TEXT NOT NULL,
    window_start_high INTEGER NOT NULL DEFAULT 0,
    window_end_high  INTEGER NOT NULL DEFAULT -1,
    updated_ms       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    channel_id   TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    sub_uri      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('pending', 'captured', 'missing')),
    captions_empty INTEGER NOT NULL DEFAULT 0,
    first_seen_ms INTEGER NOT NULL,
    captured_ms  INTEGER,
    last_error   TEXT,
    PRIMARY KEY (channel_id, seq)
);

CREATE TABLE IF NOT EXISTS cues (
    channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    text       TEXT NOT NULL,
    PRIMARY KEY (channel_id, seq, start_ms, end_ms, text)
);

CREATE INDEX IF NOT EXISTS ix_cues_channel_time ON cues(channel_id, start_ms);
CREATE INDEX IF NOT EXISTS ix_segments_channel_time ON segments(channel_id, start_ms);
"""


@dataclass
class SegmentRow:
    channel_id: str
    seq: int
    start_ms: int
    end_ms: int
    sub_uri: str
    status: str
    captions_empty: bool
    first_seen_ms: int
    captured_ms: Optional[int]
    last_error: Optional[str]

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass
class CueRow:
    seq: int
    start_ms: int
    end_ms: int
    text: str


@dataclass
class ChannelRow:
    id: str
    name: str
    master_url: str
    window_start_high: int
    window_end_high: int
    updated_ms: int


def _row_to_segment(row: sqlite3.Row) -> SegmentRow:
    return SegmentRow(
        channel_id=row["channel_id"],
        seq=row["seq"],
        start_ms=row["start_ms"],
        end_ms=row["end_ms"],
        sub_uri=row["sub_uri"],
        status=row["status"],
        captions_empty=bool(row["captions_empty"]),
        first_seen_ms=row["first_seen_ms"],
        captured_ms=row["captured_ms"],
        last_error=row["last_error"],
    )


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self._write_lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------
    def upsert_channel(self, channel_id: str, name: str, master_url: str, now: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO channels(id, name, master_url, updated_ms)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    master_url=excluded.master_url,
                    updated_ms=excluded.updated_ms
                """,
                (channel_id, name, master_url, now),
            )
            conn.execute("COMMIT")

    def get_channel(self, channel_id: str) -> Optional[ChannelRow]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM channels WHERE id=?", (channel_id,)
            ).fetchone()
        if row is None:
            return None
        return ChannelRow(
            id=row["id"],
            name=row["name"],
            master_url=row["master_url"],
            window_start_high=row["window_start_high"],
            window_end_high=row["window_end_high"],
            updated_ms=row["updated_ms"],
        )

    def list_channels(self) -> list[ChannelRow]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM channels ORDER BY id").fetchall()
        return [
            ChannelRow(
                id=row["id"],
                name=row["name"],
                master_url=row["master_url"],
                window_start_high=row["window_start_high"],
                window_end_high=row["window_end_high"],
                updated_ms=row["updated_ms"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Playlist observation
    # ------------------------------------------------------------------
    def observe_playlist(
        self,
        channel_id: str,
        segments: Iterable[dict],
        window_start: int,
        window_end: int,
        now: int,
    ) -> tuple[bool, list[int]]:
        """Register playlist segments and slide out pending ones.

        ``segments`` are dicts with ``seq``, ``start_ms``, ``end_ms`` and
        ``sub_uri``.  Returns ``(stale, newly_missing)``.  A playlist whose
        window ends before the highest window end ever observed is considered
        stale: it may add newly seen segments but it never marks anything
        missing and never lowers the high-water marks.
        """
        newly_missing: list[int] = []
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            channel = conn.execute(
                "SELECT * FROM channels WHERE id=?", (channel_id,)
            ).fetchone()
            if channel is None:
                raise KeyError(channel_id)
            stale = window_end < channel["window_end_high"]
            start_high = max(channel["window_start_high"], window_start)
            end_high = max(channel["window_end_high"], window_end)

            for segment in segments:
                row = conn.execute(
                    "SELECT status FROM segments WHERE channel_id=? AND seq=?",
                    (channel_id, segment["seq"]),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO segments(
                            channel_id, seq, start_ms, end_ms, sub_uri, status,
                            captions_empty, first_seen_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                        """,
                        (
                            channel_id,
                            segment["seq"],
                            segment["start_ms"],
                            segment["end_ms"],
                            segment["sub_uri"],
                            STATUS_PENDING,
                            now,
                        ),
                    )
                elif row["status"] == STATUS_PENDING:
                    # Refresh transport details but never touch captures.
                    conn.execute(
                        """
                        UPDATE segments
                           SET start_ms=?, end_ms=?, sub_uri=?
                         WHERE channel_id=? AND seq=? AND status=?
                        """,
                        (
                            segment["start_ms"],
                            segment["end_ms"],
                            segment["sub_uri"],
                            channel_id,
                            segment["seq"],
                            STATUS_PENDING,
                        ),
                    )

            if not stale:
                rows = conn.execute(
                    """
                    SELECT seq FROM segments
                     WHERE channel_id=? AND status=? AND seq < ?
                    """,
                    (channel_id, STATUS_PENDING, start_high),
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "UPDATE segments SET status=? WHERE channel_id=? AND seq=? AND status=?",
                        (STATUS_MISSING, channel_id, row["seq"], STATUS_PENDING),
                    )
                    newly_missing.append(row["seq"])

            conn.execute(
                """
                UPDATE channels
                   SET window_start_high=?, window_end_high=?, updated_ms=?
                 WHERE id=?
                """,
                (start_high, end_high, now, channel_id),
            )
            conn.execute("COMMIT")
        return stale, newly_missing

    # ------------------------------------------------------------------
    # Segment outcomes
    # ------------------------------------------------------------------
    def mark_captured(
        self,
        channel_id: str,
        seq: int,
        cues: Iterable[CueRow],
        captions_empty: bool,
        now: int,
    ) -> bool:
        """Atomically store a segment's cues and mark it captured."""
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM segments WHERE channel_id=? AND seq=?",
                (channel_id, seq),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise KeyError((channel_id, seq))
            conn.execute(
                "DELETE FROM cues WHERE channel_id=? AND seq=?", (channel_id, seq)
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO cues(channel_id, seq, start_ms, end_ms, text)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (channel_id, seq, cue.start_ms, cue.end_ms, cue.text)
                    for cue in cues
                ],
            )
            conn.execute(
                """
                UPDATE segments
                   SET status=?, captions_empty=?, captured_ms=?, last_error=NULL
                 WHERE channel_id=? AND seq=?
                """,
                (STATUS_CAPTURED, int(captions_empty), now, channel_id, seq),
            )
            conn.execute("COMMIT")
        return True

    def mark_fetch_failed(self, channel_id: str, seq: int, error: str, now: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE segments
                   SET last_error=?
                 WHERE channel_id=? AND seq=? AND status=?
                """,
                (error[:500], channel_id, seq, STATUS_PENDING),
            )
            conn.execute("COMMIT")

    def mark_missing(self, channel_id: str, seq: int) -> None:
        with self._write_lock, self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE segments SET status=? WHERE channel_id=? AND seq=? AND status=?",
                (STATUS_MISSING, channel_id, seq, STATUS_PENDING),
            )
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_segment(self, channel_id: str, seq: int) -> Optional[SegmentRow]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM segments WHERE channel_id=? AND seq=?",
                (channel_id, seq),
            ).fetchone()
        return _row_to_segment(row) if row else None

    def segments(
        self,
        channel_id: str,
        from_ms: Optional[int] = None,
        to_ms: Optional[int] = None,
    ) -> list[SegmentRow]:
        query = "SELECT * FROM segments WHERE channel_id=?"
        params: list = [channel_id]
        if from_ms is not None:
            query += " AND end_ms > ?"
            params.append(from_ms)
        if to_ms is not None:
            query += " AND start_ms < ?"
            params.append(to_ms)
        query += " ORDER BY seq"
        with self.connect() as conn:
            return [_row_to_segment(row) for row in conn.execute(query, params)]

    def uncaptured_segments(self, channel_id: str) -> list[SegmentRow]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM segments
                 WHERE channel_id=? AND status IN (?, ?)
                 ORDER BY seq
                """,
                (channel_id, STATUS_PENDING, STATUS_MISSING),
            ).fetchall()
        return [_row_to_segment(row) for row in rows]

    def archive_snapshot(
        self, channel_id: str
    ) -> tuple[list[SegmentRow], dict[int, list[CueRow]]]:
        """All segments plus all cues for one channel, read in one transaction.

        Reading both in a single ``BEGIN DEFERRED`` transaction guarantees the
        coverage and the cue list returned to a client describe the same state
        even if a capture commits concurrently: the snapshot is either wholly
        before or wholly after the late capture.
        """
        with self.connect() as conn:
            conn.execute("BEGIN DEFERRED")
            segments = conn.execute(
                "SELECT * FROM segments WHERE channel_id=? ORDER BY seq",
                (channel_id,),
            ).fetchall()
            cue_rows = conn.execute(
                "SELECT seq, start_ms, end_ms, text FROM cues WHERE channel_id=?",
                (channel_id,),
            ).fetchall()
            conn.execute("COMMIT")
        by_seq: dict[int, list[CueRow]] = {}
        for row in cue_rows:
            by_seq.setdefault(row["seq"], []).append(
                CueRow(
                    seq=row["seq"],
                    start_ms=row["start_ms"],
                    end_ms=row["end_ms"],
                    text=row["text"],
                )
            )
        return [_row_to_segment(row) for row in segments], by_seq

    def captured_segments_with_cues(self, channel_id: str) -> list[tuple[SegmentRow, list[CueRow]]]:
        with self.connect() as conn:
            segments = conn.execute(
                """
                SELECT * FROM segments
                 WHERE channel_id=? AND status=?
                 ORDER BY seq
                """,
                (channel_id, STATUS_CAPTURED),
            ).fetchall()
            cue_rows = conn.execute(
                """
                SELECT seq, start_ms, end_ms, text FROM cues
                 WHERE channel_id=?
                 ORDER BY seq, start_ms, end_ms, text
                """,
                (channel_id,),
            ).fetchall()
        by_seq: dict[int, list[CueRow]] = {}
        for row in cue_rows:
            by_seq.setdefault(row["seq"], []).append(
                CueRow(
                    seq=row["seq"],
                    start_ms=row["start_ms"],
                    end_ms=row["end_ms"],
                    text=row["text"],
                )
            )
        return [(_row_to_segment(row), by_seq.get(row["seq"], [])) for row in segments]

    def segment_bounds(self, channel_id: str) -> Optional[tuple[int, int]]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MIN(start_ms) AS lo, MAX(end_ms) AS hi FROM segments WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
        if row is None or row["lo"] is None:
            return None
        return int(row["lo"]), int(row["hi"])
