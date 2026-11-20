"""Parsing for the HLS demo playlists and WebVTT subtitle segments.

Mapping rules used throughout the project:

* A video playlist segment's ``EXT-X-PROGRAM-DATE-TIME`` is the UTC instant at
  the *start* of that segment.
* A WebVTT segment's ``X-TIMESTAMP-MAP`` ``LOCAL`` value is the media clock
  that corresponds to the start of that same segment.  Therefore a cue clock
  ``C`` in a segment starting at UTC ``S`` with ``LOCAL = L`` is projected to
  UTC as ``S + (C - L)``.  Because every segment carries its own ``LOCAL``
  anchor, a cue repeated verbatim in two adjacent subtitle files resolves to
  exactly the same UTC range in both.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from .timeutil import parse_clock_ms, parse_iso_ms


@dataclass
class MasterPlaylist:
    video_uri: str
    subtitle_uri: Optional[str]
    raw: str = ""


@dataclass
class PlaylistSegment:
    seq: int
    duration_ms: int
    start_ms: Optional[int]
    uri: str

    @property
    def end_ms(self) -> Optional[int]:
        if self.start_ms is None:
            return None
        return self.start_ms + self.duration_ms


@dataclass
class MediaPlaylist:
    media_sequence: int
    target_duration_ms: int
    segments: list[PlaylistSegment] = field(default_factory=list)
    endlist: bool = False
    base_url: str = ""

    @property
    def window_start(self) -> int:
        return self.media_sequence

    @property
    def window_end(self) -> int:
        return self.media_sequence + len(self.segments)


@dataclass
class VTTCue:
    start_ms: int
    end_ms: int
    text: str


@dataclass
class WebVTT:
    valid: bool
    local_ms: Optional[int]
    mpegts_ms: Optional[int]
    cues: list[VTTCue] = field(default_factory=list)


_ATTRIBUTE_RE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')
_TIMESTAMP_MAP_RE = re.compile(r"X-TIMESTAMP-MAP\s*=\s*(.+)", re.IGNORECASE)
_ARROW = "-->"


def _parse_attributes(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for key, value in _ATTRIBUTE_RE.findall(text):
        attrs[key.strip().upper()] = value.strip().strip('"')
    return attrs


def parse_master(text: str, base_url: str) -> MasterPlaylist:
    """Extract the first variant stream and the default subtitle rendition."""
    video_uri: Optional[str] = None
    subtitle_uri: Optional[str] = None
    lines = _lines(text)
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            attrs = _parse_attributes(line[len("#EXT-X-MEDIA:"):])
            if attrs.get("TYPE", "").upper() == "SUBTITLES" and attrs.get("URI"):
                # Prefer the DEFAULT rendition; otherwise keep the first one.
                if subtitle_uri is None or attrs.get("DEFAULT", "").upper() == "YES":
                    subtitle_uri = urljoin(base_url, attrs["URI"])
        elif line.startswith("#EXT-X-STREAM-INF:") and video_uri is None:
            for follow in lines[index + 1:]:
                if follow and not follow.startswith("#"):
                    video_uri = urljoin(base_url, follow)
                    break
    if video_uri is None:
        video_uri = urljoin(base_url, "video.m3u8")
    return MasterPlaylist(video_uri=video_uri, subtitle_uri=subtitle_uri, raw=text)


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    lines = _lines(text)
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("not an HLS playlist")

    media_sequence = 0
    target_duration_ms = 0
    segments: list[PlaylistSegment] = []
    endlist = False
    pending_duration: Optional[int] = None
    pending_start: Optional[int] = None

    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1].strip())
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target_duration_ms = int(float(line.split(":", 1)[1].strip()) * 1000)
        elif line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0].strip()
            pending_duration = int(round(float(value) * 1000))
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_start = parse_iso_ms(line.split(":", 1)[1].strip())
        elif line.startswith("#EXT-X-ENDLIST"):
            endlist = True
        elif line and not line.startswith("#"):
            if pending_duration is None:
                raise ValueError(f"segment URI without EXTINF: {line!r}")
            seq = media_sequence + len(segments)
            segments.append(
                PlaylistSegment(
                    seq=seq,
                    duration_ms=pending_duration,
                    start_ms=pending_start,
                    uri=urljoin(base_url, line),
                )
            )
            pending_duration = None
            pending_start = None

    _fill_missing_starts(segments)
    return MediaPlaylist(
        media_sequence=media_sequence,
        target_duration_ms=target_duration_ms,
        segments=segments,
        endlist=endlist,
        base_url=base_url,
    )


def _fill_missing_starts(segments: list[PlaylistSegment]) -> None:
    """Derive PDT-less segment starts from neighbours and durations."""
    for index in range(1, len(segments)):
        current = segments[index]
        previous = segments[index - 1]
        if current.start_ms is None and previous.start_ms is not None:
            current.start_ms = previous.start_ms + previous.duration_ms
    for index in range(len(segments) - 2, -1, -1):
        current = segments[index]
        following = segments[index + 1]
        if current.start_ms is None and following.start_ms is not None:
            current.start_ms = following.start_ms - current.duration_ms


def parse_vtt(text: str) -> WebVTT:
    """Parse a WebVTT file, including its ``X-TIMESTAMP-MAP`` header."""
    clean = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = clean.split("\n")
    if not lines or not lines[0].strip().startswith("WEBVTT"):
        return WebVTT(valid=False, local_ms=None, mpegts_ms=None)

    local_ms: Optional[int] = None
    mpegts_ms: Optional[int] = None

    cues: list[VTTCue] = []
    index = 0
    # Header: scan until the first blank line or the first cue.
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        match = _TIMESTAMP_MAP_RE.search(line)
        if match:
            for part in match.group(1).split(","):
                key, _, value = part.partition(":")
                key = key.strip().upper()
                value = value.strip()
                try:
                    if key == "LOCAL":
                        local_ms = parse_clock_ms(value)
                    elif key == "MPEGTS":
                        mpegts_ms = int(value)
                except ValueError:
                    pass
            index += 1
            continue
        if _ARROW in line:
            break
        index += 1

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if _ARROW in line:
            start_clock, _, right = line.partition(_ARROW)
            end_clock = right.strip().split(" ", 1)[0]
            try:
                cue_start = parse_clock_ms(start_clock.strip())
                cue_end = parse_clock_ms(end_clock)
            except ValueError:
                index += 1
                continue
            index += 1
            payload: list[str] = []
            while index < len(lines) and lines[index].strip():
                payload.append(lines[index].rstrip())
                index += 1
            cue_text = "\n".join(payload).strip()
            if cue_text and cue_end > cue_start:
                cues.append(VTTCue(start_ms=cue_start, end_ms=cue_end, text=cue_text))
        else:
            index += 1

    return WebVTT(valid=True, local_ms=local_ms, mpegts_ms=mpegts_ms, cues=cues)


def map_cues_to_utc(
    vtt: WebVTT, segment_start_ms: int, local_ms: Optional[int] = None
) -> list[VTTCue]:
    """Project VTT cue clocks onto UTC using the segment's start instant.

    ``local_ms`` may be supplied explicitly (e.g. from the subtitle playlist's
    PDT); otherwise the file's ``X-TIMESTAMP-MAP`` LOCAL is used.  If neither
    is available cue clocks are treated as offsets from the segment start.
    """
    anchor = vtt.local_ms if local_ms is None else local_ms
    offset = segment_start_ms - (anchor or 0)
    return [
        VTTCue(
            start_ms=cue.start_ms + offset,
            end_ms=cue.end_ms + offset,
            text=cue.text,
        )
        for cue in vtt.cues
    ]


def _lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
