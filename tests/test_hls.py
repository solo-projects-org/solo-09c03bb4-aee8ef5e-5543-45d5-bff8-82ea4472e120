"""Unit tests for playlist parsing, UTC mapping, and time helpers."""
from __future__ import annotations

from app.hls import map_cues_to_utc, parse_master, parse_media_playlist, parse_vtt
from app.timeutil import clock_from_ms, iso_from_ms, parse_clock_ms, parse_iso_ms

VIDEO_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:10
#EXT-X-PROGRAM-DATE-TIME:2026-11-20T00:01:00.000Z
#EXTINF:6.000,
seg/10.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-20T00:01:06.000Z
#EXTINF:6.000,
seg/11.ts
#EXT-X-PROGRAM-DATE-TIME:2026-11-20T00:01:12.000Z
#EXTINF:6.000,
seg/12.ts
"""

VTT_WITH_MAP = """WEBVTT
X-TIMESTAMP-MAP=MPEGTS:900000,LOCAL:00:01:06.000

00:01:06.300 --> 00:01:08.800
新闻｜第11段｜整点快讯

00:01:09.300 --> 00:01:11.800
新闻｜第11段｜现场连线
"""


def test_parse_iso_and_clock_roundtrip():
    ms = parse_iso_ms("2026-11-20T00:01:06.000Z")
    assert iso_from_ms(ms) == "2026-11-20T00:01:06.000Z"
    assert parse_clock_ms("00:01:06.000") == 66_000
    assert parse_clock_ms("01:06.500") == 66_500  # MM:SS.mmm is legal in VTT
    assert clock_from_ms(66_000) == "00:01:06.000"


def test_parse_media_playlist_carries_pdt_per_segment():
    playlist = parse_media_playlist(VIDEO_PLAYLIST, "http://origin/channel/news/video.m3u8")
    assert playlist.media_sequence == 10
    assert [segment.seq for segment in playlist.segments] == [10, 11, 12]
    assert [iso_from_ms(segment.start_ms) for segment in playlist.segments] == [
        "2026-11-20T00:01:00.000Z",
        "2026-11-20T00:01:06.000Z",
        "2026-11-20T00:01:12.000Z",
    ]
    assert playlist.segments[1].uri.endswith("/seg/11.ts")


def test_missing_pdt_is_derived_from_neighbour_durations():
    text = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PROGRAM-DATE-TIME:2026-11-20T00:00:00.000Z
#EXTINF:6.000,
seg/0.ts
#EXTINF:6.000,
seg/1.ts
#EXTINF:6.000,
seg/2.ts
"""
    playlist = parse_media_playlist(text, "http://origin/video.m3u8")
    assert [iso_from_ms(segment.start_ms) for segment in playlist.segments] == [
        "2026-11-20T00:00:00.000Z",
        "2026-11-20T00:00:06.000Z",
        "2026-11-20T00:00:12.000Z",
    ]


def test_parse_master_prefers_subtitle_rendition():
    text = """#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=YES,URI="subs.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,SUBTITLES="subs"
video.m3u8
"""
    master = parse_master(text, "http://origin/channel/news/master.m3u8")
    assert master.video_uri == "http://origin/channel/news/video.m3u8"
    assert master.subtitle_uri == "http://origin/channel/news/subs.m3u8"


def test_parse_vtt_reads_timestamp_map_and_cues():
    vtt = parse_vtt(VTT_WITH_MAP)
    assert vtt.valid
    assert vtt.local_ms == parse_clock_ms("00:01:06.000")
    assert vtt.mpegts_ms == 900_000
    assert len(vtt.cues) == 2
    assert vtt.cues[0].text == "新闻｜第11段｜整点快讯"


def test_invalid_vtt_is_rejected_not_treated_as_empty():
    vtt = parse_vtt("<html>404</html>")
    assert vtt.valid is False


def test_cues_map_through_local_anchor():
    segment_start = parse_iso_ms("2026-11-20T00:01:06.000Z")
    vtt = parse_vtt(VTT_WITH_MAP)
    cues = map_cues_to_utc(vtt, segment_start)
    assert cues[0].start_ms == parse_iso_ms("2026-11-20T00:01:06.300Z")
    assert cues[0].end_ms == parse_iso_ms("2026-11-20T00:01:08.800Z")
    assert cues[1].start_ms == parse_iso_ms("2026-11-20T00:01:09.300Z")


def test_cue_in_two_files_maps_to_identical_utc_range():
    """A cue repeated in adjacent files must land on one UTC interval."""
    first = """WEBVTT
X-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:00.000

00:00:04.000 --> 00:00:08.000
跨段字幕
"""
    second = """WEBVTT
X-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:06.000

00:00:04.000 --> 00:00:08.000
跨段字幕
"""
    start_first = parse_iso_ms("2026-11-20T00:00:00.000Z")
    start_second = parse_iso_ms("2026-11-20T00:00:06.000Z")
    cue_a = map_cues_to_utc(parse_vtt(first), start_first)[0]
    cue_b = map_cues_to_utc(parse_vtt(second), start_second)[0]
    assert (cue_a.start_ms, cue_a.end_ms) == (cue_b.start_ms, cue_b.end_ms)
    assert cue_a.start_ms == parse_iso_ms("2026-11-20T00:00:04.000Z")
    assert cue_a.end_ms == parse_iso_ms("2026-11-20T00:00:08.000Z")
