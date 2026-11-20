"""Archive query view: coverage spans and merged cues.

The view is always recomputed from the current SQLite state, so a late capture
of a previously missing segment updates both the cues and the coverage in the
same response.

Cross-segment cues are de-duplicated only between *adjacent* captured segments
that form one continuous run (consecutive sequence numbers and touching UTC
ranges).  A hole left by a pending or missing segment breaks the run, so a cue
can never be merged across a gap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .db import (
    STATUS_CAPTURED,
    STATUS_MISSING,
    STATUS_PENDING,
    CueRow,
    Database,
    SegmentRow,
)
from .timeutil import iso_from_ms


@dataclass
class Run:
    segments: list[SegmentRow]

    @property
    def start_ms(self) -> int:
        return self.segments[0].start_ms

    @property
    def end_ms(self) -> int:
        return self.segments[-1].end_ms


def contiguous_runs(segments: Iterable[SegmentRow]) -> list[Run]:
    """Split captured segments into max-length continuous runs."""
    ordered = sorted(
        (segment for segment in segments if segment.status == STATUS_CAPTURED),
        key=lambda segment: segment.seq,
    )
    runs: list[Run] = []
    current: list[SegmentRow] = []
    for segment in ordered:
        if current:
            previous = current[-1]
            joins = (
                segment.seq == previous.seq + 1
                and segment.start_ms == previous.end_ms
            )
            if not joins:
                runs.append(Run(current))
                current = []
        current.append(segment)
    if current:
        runs.append(Run(current))
    return runs


def _merge_run_cues(run: Run, cues_by_seq: dict[int, list[CueRow]]) -> list[dict]:
    merged: dict[tuple[int, int, str], set[int]] = {}
    for segment in run.segments:
        for cue in cues_by_seq.get(segment.seq, []):
            merged.setdefault((cue.start_ms, cue.end_ms, cue.text), set()).add(segment.seq)
    result = [
        {
            "start_ms": start,
            "end_ms": end,
            "text": text,
            "seqs": sorted(seqs),
            "run_start_ms": run.start_ms,
            "run_end_ms": run.end_ms,
        }
        for (start, end, text), seqs in merged.items()
    ]
    result.sort(key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["text"]))
    return result


def _clip(start: int, end: int, lo: int, hi: int) -> Optional[tuple[int, int]]:
    left = max(start, lo)
    right = min(end, hi)
    if right <= left:
        return None
    return left, right


def _status_payload(segment: SegmentRow) -> dict:
    return {
        "seq": segment.seq,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "start_utc": iso_from_ms(segment.start_ms),
        "end_utc": iso_from_ms(segment.end_ms),
        "status": segment.status,
        "captions_empty": segment.captions_empty,
        "error": segment.last_error,
    }


def build_view(
    db: Database,
    channel_id: str,
    name: str,
    from_ms: int,
    to_ms: int,
    explicit_range: bool = True,
) -> dict:
    segments, cues_by_seq = db.archive_snapshot(channel_id)
    runs = contiguous_runs(segments)

    coverage: list[dict] = []
    cues: list[dict] = []
    for run in runs:
        clipped = _clip(run.start_ms, run.end_ms, from_ms, to_ms)
        if clipped is not None:
            # Report the sequence numbers that actually intersect the visible
            # part of the run, so a range-clipped span does not overstate it.
            visible = [
                segment
                for segment in run.segments
                if segment.end_ms > clipped[0] and segment.start_ms < clipped[1]
            ]
            coverage.append(
                {
                    "start_ms": clipped[0],
                    "end_ms": clipped[1],
                    "start_utc": iso_from_ms(clipped[0]),
                    "end_utc": iso_from_ms(clipped[1]),
                    "seq_start": visible[0].seq,
                    "seq_end": visible[-1].seq,
                    "complete": clipped == (run.start_ms, run.end_ms),
                }
            )
        for cue in _merge_run_cues(run, cues_by_seq):
            if cue["start_ms"] < to_ms and cue["end_ms"] > from_ms:
                cues.append(
                    {
                        "start_ms": cue["start_ms"],
                        "end_ms": cue["end_ms"],
                        "start_utc": iso_from_ms(cue["start_ms"]),
                        "end_utc": iso_from_ms(cue["end_ms"]),
                        "text": cue["text"],
                        "seqs": cue["seqs"],
                        "merged": len(cue["seqs"]) > 1,
                    }
                )

    cues.sort(key=lambda cue: (cue["start_ms"], cue["end_ms"], cue["text"]))
    coverage.sort(key=lambda span: span["start_ms"])

    in_range = [
        segment
        for segment in segments
        if segment.end_ms > from_ms and segment.start_ms < to_ms
    ]
    gaps = [
        _status_payload(segment)
        for segment in in_range
        if segment.status != STATUS_CAPTURED
    ]
    gaps.sort(key=lambda gap: gap["start_ms"])

    counts = {
        "captured": sum(1 for s in segments if s.status == STATUS_CAPTURED),
        "pending": sum(1 for s in segments if s.status == STATUS_PENDING),
        "missing": sum(1 for s in segments if s.status == STATUS_MISSING),
        "empty_captions": sum(
            1
            for s in segments
            if s.status == STATUS_CAPTURED and s.captions_empty
        ),
        "cues_in_range": len(cues),
        "gaps_in_range": len(gaps),
    }
    # Derived from the same snapshot as the cues so the two never disagree.
    bounds = (
        (min(segment.start_ms for segment in segments), max(segment.end_ms for segment in segments))
        if segments
        else None
    )
    return {
        "channel": {"id": channel_id, "name": name},
        "range": {
            "from_ms": from_ms,
            "to_ms": to_ms,
            "from_utc": iso_from_ms(from_ms),
            "to_utc": iso_from_ms(to_ms),
        },
        "default_range": not explicit_range,
        "bounds": (
            {
                "start_ms": bounds[0],
                "end_ms": bounds[1],
                "start_utc": iso_from_ms(bounds[0]),
                "end_utc": iso_from_ms(bounds[1]),
            }
            if bounds
            else None
        ),
        "segments": [_status_payload(segment) for segment in in_range],
        "coverage": coverage,
        "gaps": gaps,
        "cues": cues,
        "counts": counts,
    }
