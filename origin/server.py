"""Local HLS demo origin.

The origin is a small, deterministic simulation of a live HLS packager.  It
serves a sliding-window video playlist, a matching WebVTT subtitle playlist and
the individual subtitle files, plus a ``/__control`` API used by the archive
web page (and the tests) to drive the simulation:

* advance the live window,
* add per-segment latency to file delivery,
* make chosen segments unavailable (HTTP 404) and let them arrive later,
* freeze a playlist to imitate a stale CDN cache.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.timeutil import clock_from_ms, iso_from_ms

SEG_MS = 6_000
WINDOW = 8
INITIAL_HEAD = 12
DAY_MS = 86_400_000

#: Fixed broadcast anchors keep the demo (and the tests) reproducible.
CHANNEL_SPECS = (
    {"id": "news", "name": "新闻综合", "label": "新闻", "base": "2026-11-20T00:00:00Z"},
    {"id": "sports", "name": "体育赛事", "label": "体育", "base": "2026-11-20T04:00:00Z"},
)


def _is_empty_segment(seq: int) -> bool:
    """A deliberately caption-less segment: it was delivered, but has no cues."""
    return seq % 7 == 5


@dataclass
class OriginChannel:
    id: str
    name: str
    label: str
    base_ms: int
    head: int = INITIAL_HEAD
    gaps: set[int] = field(default_factory=set)
    frozen_video: Optional[str] = None
    frozen_subs: Optional[str] = None
    #: Serve a playlist this many segments behind the live head, imitating a
    #: stale CDN cache.  ``0`` means a live playlist.
    stale_by: int = 0

    @property
    def served_head(self) -> int:
        return max(0, self.head - self.stale_by)

    @property
    def window_start(self) -> int:
        return max(0, self.served_head - WINDOW)

    @property
    def window(self) -> range:
        return range(self.window_start, self.served_head)

    def start_ms(self, seq: int) -> int:
        return self.base_ms + seq * SEG_MS

    def cues(self, seq: int) -> list[tuple[int, int, str]]:
        """Return cues as (start ms, end ms, text) on the channel clock.

        A "spanning" cue anchored on segment ``seq`` (every fourth segment)
        starts 4 s into it and ends 2 s into the next one.  It is emitted into
        *both* files with identical absolute times, so both map to exactly the
        same UTC interval.  That is what the archive then merges -- but only
        when the two segments are one continuous run.
        """
        cues: list[tuple[int, int, str]] = []
        if _is_empty_segment(seq):
            return cues
        segment_start = self.start_ms(seq)
        cues.append(
            (
                segment_start + 300,
                segment_start + 2_800,
                f"{self.label}｜第{seq}段｜整点快讯",
            )
        )
        cues.append(
            (
                segment_start + 3_300,
                segment_start + 5_800,
                f"{self.label}｜第{seq}段｜现场连线",
            )
        )
        if seq % 4 == 2 and not _is_empty_segment(seq + 1):
            cues.append(
                (
                    segment_start + 4_000,
                    segment_start + 8_000,
                    f"{self.label}｜跨段字幕｜第{seq}段起",
                )
            )
        if seq % 4 == 3 and not _is_empty_segment(seq - 1):
            previous_start = self.start_ms(seq - 1)
            cues.append(
                (
                    previous_start + 4_000,
                    previous_start + 8_000,
                    f"{self.label}｜跨段字幕｜第{seq - 1}段起",
                )
            )
        return cues


@dataclass
class Simulation:
    latency_s: float = 0.0
    channels: dict[str, OriginChannel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for spec in CHANNEL_SPECS:
            self.channels[spec["id"]] = OriginChannel(
                id=spec["id"],
                name=spec["name"],
                label=spec["label"],
                base_ms=_parse_base(spec["base"]),
            )

    def get(self, channel_id: str) -> OriginChannel:
        channel = self.channels.get(channel_id)
        if channel is None:
            raise HTTPException(status_code=404, detail=f"unknown channel {channel_id!r}")
        return channel


def _parse_base(value: str) -> int:
    from app.timeutil import parse_iso_ms

    return parse_iso_ms(value)


SIM = Simulation()


def reset_state() -> None:
    """Reset the simulation to its initial state (used by tests)."""
    global SIM
    SIM = Simulation()


def _clock_of_day(ms: int) -> str:
    return clock_from_ms(ms % DAY_MS)


def _video_playlist(channel: OriginChannel, base_url: str) -> str:
    if channel.frozen_video is not None:
        return channel.frozen_video
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEG_MS // 1000}",
        f"#EXT-X-MEDIA-SEQUENCE:{channel.window_start}",
    ]
    for seq in channel.window:
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso_from_ms(channel.start_ms(seq))}")
        lines.append(f"#EXTINF:{SEG_MS / 1000:.3f},")
        lines.append(f"seg/{seq}.ts")
    return "\n".join(lines) + "\n"


def _subs_playlist(channel: OriginChannel, base_url: str) -> str:
    if channel.frozen_subs is not None:
        return channel.frozen_subs
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{SEG_MS // 1000}",
        f"#EXT-X-MEDIA-SEQUENCE:{channel.window_start}",
    ]
    for seq in channel.window:
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{iso_from_ms(channel.start_ms(seq))}")
        lines.append(f"#EXTINF:{SEG_MS / 1000:.3f},")
        lines.append(f"subs/{seq}.vtt")
    return "\n".join(lines) + "\n"


def _subtitle_file(channel: OriginChannel, seq: int) -> str:
    segment_start = channel.start_ms(seq)
    local = _clock_of_day(segment_start)
    mpegts = ((segment_start // 1000) * 90_000) % (2**33)
    lines = [
        "WEBVTT",
        f"X-TIMESTAMP-MAP=MPEGTS:{mpegts},LOCAL:{local}",
        "",
    ]
    segment_end = segment_start + SEG_MS
    for start, end, text in channel.cues(seq):
        if start < segment_end and end > segment_start:
            lines.append(f"{_clock_of_day(start)} --> {_clock_of_day(end)}")
            lines.append(text)
            lines.append("")
    return "\n".join(lines)


class AdvanceRequest(BaseModel):
    channel: Optional[str] = None
    segments: int = 1


class LatencyRequest(BaseModel):
    seconds: float


class GapRequest(BaseModel):
    channel: str
    seq: int
    missing: bool = True


class FreezeRequest(BaseModel):
    channel: Optional[str] = None
    frozen: bool = True


class StaleRequest(BaseModel):
    channel: Optional[str] = None
    segments: int = 8


def create_app() -> FastAPI:
    app = FastAPI(title="HLS subtitle demo origin")

    async def maybe_delay() -> None:
        if SIM.latency_s > 0:
            await asyncio.sleep(SIM.latency_s)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.get("/channels.json")
    async def channels(request: Request) -> list[dict]:
        base = str(request.base_url).rstrip("/")
        return [
            {
                "id": channel.id,
                "name": channel.name,
                "master_url": f"{base}/channel/{channel.id}/master.m3u8",
                "window_start": channel.window_start,
                "window_end": channel.served_head,
                "latency_s": SIM.latency_s,
                "gaps": sorted(channel.gaps),
            }
            for channel in SIM.channels.values()
        ]

    @app.get("/channel/{channel_id}/master.m3u8", response_class=PlainTextResponse)
    async def master(channel_id: str) -> str:
        SIM.get(channel_id)  # 404 for an unknown channel
        return "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                '#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",'
                'DEFAULT=YES,AUTOSELECT=YES,URI="subs.m3u8"',
                '#EXT-X-STREAM-INF:BANDWIDTH=800000,SUBTITLES="subs"',
                "video.m3u8",
                "",
            ]
        )

    @app.get("/channel/{channel_id}/video.m3u8", response_class=PlainTextResponse)
    async def video_playlist(channel_id: str) -> str:
        channel = SIM.get(channel_id)
        await maybe_delay()
        return _video_playlist(channel, "")

    @app.get("/channel/{channel_id}/subs.m3u8", response_class=PlainTextResponse)
    async def subs_playlist(channel_id: str) -> str:
        channel = SIM.get(channel_id)
        await maybe_delay()
        return _subs_playlist(channel, "")

    @app.get("/channel/{channel_id}/seg/{seq}.ts")
    async def video_segment(channel_id: str, seq: int) -> Response:
        channel = SIM.get(channel_id)
        await maybe_delay()
        if seq in channel.gaps:
            raise HTTPException(status_code=404, detail="segment unavailable")
        return Response(
            content=b"G" + seq.to_bytes(4, "big") + b"\x47" * 184,
            media_type="video/mp2t",
        )

    @app.get("/channel/{channel_id}/subs/{seq}.vtt", response_class=PlainTextResponse)
    async def subtitle_segment(channel_id: str, seq: int) -> str:
        channel = SIM.get(channel_id)
        await maybe_delay()
        if seq in channel.gaps:
            raise HTTPException(status_code=404, detail="subtitle segment unavailable")
        return _subtitle_file(channel, seq)

    # ------------------------------------------------------------------
    # Simulation control
    # ------------------------------------------------------------------
    @app.get("/__control/state")
    async def control_state() -> dict:
        return {
            "latency_s": SIM.latency_s,
            "channels": [
                {
                    "id": channel.id,
                    "name": channel.name,
                    "head": channel.head,
                    "window_start": channel.window_start,
                    "window_end": channel.served_head,
                    "live_head": channel.head,
                    "stale_by": channel.stale_by,
                    "window_start_utc": iso_from_ms(channel.start_ms(channel.window_start)),
                    "window_end_utc": iso_from_ms(channel.start_ms(channel.served_head)),
                    "gaps": sorted(channel.gaps),
                    "frozen": channel.frozen_video is not None,
                    "segments": [
                        {
                            "seq": seq,
                            "start_utc": iso_from_ms(channel.start_ms(seq)),
                            "available": seq not in channel.gaps,
                            "empty_captions": _is_empty_segment(seq),
                        }
                        for seq in channel.window
                    ],
                }
                for channel in SIM.channels.values()
            ],
        }

    @app.post("/__control/advance")
    async def advance(body: AdvanceRequest) -> dict:
        if body.segments < 0:
            raise HTTPException(status_code=400, detail="segments must be >= 0")
        targets = (
            [SIM.get(body.channel)] if body.channel else list(SIM.channels.values())
        )
        for channel in targets:
            channel.head += body.segments
        return await control_state()

    @app.post("/__control/latency")
    async def latency(body: LatencyRequest) -> dict:
        if not 0 <= body.seconds <= 30:
            raise HTTPException(status_code=400, detail="latency must be 0..30 seconds")
        SIM.latency_s = float(body.seconds)
        return await control_state()

    @app.post("/__control/gap")
    async def gap(body: GapRequest) -> dict:
        channel = SIM.get(body.channel)
        if body.missing:
            channel.gaps.add(body.seq)
        else:
            channel.gaps.discard(body.seq)
        return await control_state()

    @app.post("/__control/freeze")
    async def freeze(body: FreezeRequest) -> dict:
        targets = (
            [SIM.get(body.channel)] if body.channel else list(SIM.channels.values())
        )
        for channel in targets:
            if body.frozen:
                channel.frozen_video = _video_playlist(channel, "")
                channel.frozen_subs = _subs_playlist(channel, "")
            else:
                channel.frozen_video = None
                channel.frozen_subs = None
        return await control_state()

    @app.post("/__control/stale")
    async def stale(body: StaleRequest) -> dict:
        if not 0 <= body.segments <= 1000:
            raise HTTPException(status_code=400, detail="segments must be 0..1000")
        targets = (
            [SIM.get(body.channel)] if body.channel else list(SIM.channels.values())
        )
        for channel in targets:
            channel.stale_by = body.segments
            channel.frozen_video = None
            channel.frozen_subs = None
        return await control_state()

    @app.post("/__control/reset")
    async def reset() -> dict:
        reset_state()
        return await control_state()

    return app


app = create_app()
