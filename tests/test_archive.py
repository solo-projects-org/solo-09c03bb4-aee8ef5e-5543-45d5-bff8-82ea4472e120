"""End-to-end behaviour of the archive against the in-process demo origin."""
from __future__ import annotations

from app.timeutil import iso_from_ms, parse_iso_ms


def _seq_range(start: int, end: int) -> list[int]:
    return list(range(start, end))


def test_capture_fills_window_and_maps_utc(harness):
    result = harness.capture_channel("news")
    view = harness.archive("news")

    # Initial demo window: sequences 4..11 (head=12, window=8).
    assert [segment["seq"] for segment in view["segments"]] == _seq_range(4, 12)
    assert result["captured"] == 8
    assert result["fetched"] == 8
    assert view["counts"]["captured"] == 8
    assert view["counts"]["pending"] == 0
    assert view["counts"]["missing"] == 0

    # The very first segment starts at the channel's broadcast epoch.
    first = view["segments"][0]
    assert first["start_utc"] == "2026-11-20T00:00:24.000Z"
    assert first["end_utc"] == "2026-11-20T00:00:30.000Z"

    # Cue times are the file's LOCAL clock projected onto the segment PDT.
    cue = next(cue for cue in view["cues"] if cue["seqs"] == [4])
    assert cue["start_utc"] == "2026-11-20T00:00:24.300Z"
    assert cue["end_utc"] == "2026-11-20T00:00:26.800Z"


def test_empty_caption_segment_counts_as_captured(harness):
    harness.capture_channel("news")
    view = harness.archive("news")
    empty = [segment for segment in view["segments"] if segment["captions_empty"]]
    # The demo leaves sequence 5 without cues.
    assert [segment["seq"] for segment in empty] == [5]
    assert empty[0]["status"] == "captured"
    assert view["counts"]["empty_captions"] == 1
    assert all(
        segment["seq"] != 5 for segment in view["segments"] if segment["status"] != "captured"
    )


def test_switch_channel_keeps_independent_archives(harness):
    harness.capture_channel("news")
    harness.capture_channel("sports")
    news = harness.archive("news")
    sports = harness.archive("sports")

    assert news["channel"]["name"] == "新闻综合"
    assert sports["channel"]["name"] == "体育赛事"
    assert news["segments"][0]["start_utc"] == "2026-11-20T00:00:24.000Z"
    assert sports["segments"][0]["start_utc"] == "2026-11-20T04:00:24.000Z"


def test_pending_then_missing_across_the_sliding_window(harness):
    # Knock out a segment before it is ever fetched, while it is in the window.
    harness.set_gap(9, True)
    harness.capture_channel("news")
    harness.advance(4)  # window 8..15; #9 is still visible
    result = harness.capture_channel("news")
    view = harness.archive("news")
    by_seq = harness.segments_by_seq(view)
    assert by_seq[9]["status"] == "pending"
    assert result["marked_missing"] == []

    # Slide further: #9 leaves the window and becomes missing.
    harness.advance(6)  # window 14..21
    result = harness.capture_channel("news")
    view = harness.archive("news")
    by_seq = harness.segments_by_seq(view)
    assert by_seq[9]["status"] == "missing"
    assert 9 in result["marked_missing"]
    # Segments before it were captured, so they stay captured.
    assert by_seq[8]["status"] == "captured"
    assert all(
        gap["seq"] != 9 or gap["status"] == "missing" for gap in view["gaps"]
    )


def test_missing_segment_excluded_from_coverage_and_splits_run(harness):
    harness.set_gap(9, True)
    harness.capture_channel("news")
    harness.advance(4)
    harness.capture_channel("news")
    harness.advance(6)
    harness.capture_channel("news")

    view = harness.archive("news")
    # Coverage must be two runs split at the #9 hole.
    spans = [(span["seq_start"], span["seq_end"]) for span in view["coverage"]]
    assert any(end == 8 for _, end in spans)
    assert any(start == 10 for start, _ in spans)
    assert not any(start <= 9 <= end for start, end in spans)


def test_cue_crossing_a_missing_segment_is_not_merged(harness):
    """#7's file repeats #6's spanning cue, but #6 is missing -> no merge."""
    # #6/#7 are the spanning pair in the initial window; hole #6.
    harness.set_gap(6, True)
    harness.capture_channel("news")
    harness.advance(4)
    harness.capture_channel("news")
    harness.advance(6)
    harness.capture_channel("news")

    view = harness.archive("news")
    spanning = [
        cue
        for cue in view["cues"]
        if cue["text"] == "新闻｜跨段字幕｜第6段起" and 7 in cue["seqs"]
    ]
    assert spanning, "expected the spanning cue repeated in #7 to be archived"
    for cue in spanning:
        assert cue["merged"] is False
        assert cue["seqs"] == [7]


def test_cross_segment_cue_merges_within_a_continuous_run(harness):
    harness.capture_channel("news")
    view = harness.archive("news")
    merged = [cue for cue in view["cues"] if cue["merged"]]
    assert merged, "expected at least one cue repeated across two adjacent segments"
    for cue in merged:
        assert len(cue["seqs"]) >= 2
        # Merged cues always come from consecutive sequence numbers.
        assert cue["seqs"] == list(range(cue["seqs"][0], cue["seqs"][0] + len(cue["seqs"])))


def test_late_capture_updates_cues_and_coverage_together(harness):
    harness.set_gap(6, True)
    harness.capture_channel("news")
    harness.advance(4)
    harness.capture_channel("news")
    harness.advance(6)
    harness.capture_channel("news")

    before = harness.archive("news")
    before_seqs = {seq for cue in before["cues"] for seq in cue["seqs"]}
    assert 6 not in before_seqs
    before_span_count = len(before["coverage"])

    # The origin heals; re-capture picks #6 up even though it is off-window.
    harness.set_gap(6, False)
    result = harness.capture_channel("news")
    assert result["captured"] >= 1

    after = harness.archive("news")
    after_seqs = {seq for cue in after["cues"] for seq in cue["seqs"]}
    assert 6 in after_seqs
    assert len(after["coverage"]) == before_span_count - 1
    assert any(span["seq_start"] <= 6 <= span["seq_end"] for span in after["coverage"])
    by_seq = harness.segments_by_seq(after)
    assert by_seq[6]["status"] == "captured"
    assert all(gap["seq"] != 6 for gap in after["gaps"])

    # The healed hole also restores the cross-segment merge for #6/#7.
    spanning = [
        cue
        for cue in after["cues"]
        if cue["text"].startswith("新闻｜跨段字幕") and 6 in cue["seqs"]
    ]
    assert spanning and spanning[0]["seqs"] == [6, 7]


def test_repeated_capture_is_idempotent(harness):
    harness.capture_channel("news")
    first = harness.archive("news")
    for _ in range(3):
        harness.capture_channel("news")
    second = harness.archive("news")
    assert first["cues"] == second["cues"]
    assert first["coverage"] == second["coverage"]
    assert first["counts"] == second["counts"]


def test_stale_playlist_does_not_roll_status_back(harness):
    harness.capture_channel("news")
    harness.advance(8)  # window 12..19; #4..#11 have slid out
    harness.capture_channel("news")

    snapshot = harness.archive("news")
    before = {segment["seq"]: segment["status"] for segment in snapshot["segments"]}

    # Serve the *old* window again, imitating a stale CDN edge, then capture.
    response = harness.client.post("/api/demo/stale", json={"channel": "news", "segments": 8})
    assert response.status_code == 200, response.text
    result = harness.capture_channel("news")
    view = harness.archive("news")
    after = {segment["seq"]: segment["status"] for segment in view["segments"]}

    assert result["stale_playlist"] is True
    # Nothing that was captured may move to pending or missing.
    for seq, status in before.items():
        if status == "captured":
            assert after.get(seq) == "captured"
    assert view["counts"]["captured"] == snapshot["counts"]["captured"]


def test_stale_playlist_does_not_create_new_missing(harness):
    harness.capture_channel("news")
    harness.set_gap(6, True)
    harness.capture_channel("news")  # #6 pending, window 4..11

    harness.advance(8)  # window 12..19, capture makes #6 missing
    harness.capture_channel("news")
    missing_before = harness.archive("news")["counts"]["missing"]

    response = harness.client.post("/api/demo/stale", json={"channel": "news", "segments": 8})
    assert response.status_code == 200, response.text
    harness.capture_channel("news")
    assert harness.archive("news")["counts"]["missing"] == missing_before


def test_restart_resumes_from_sqlite(harness):
    harness.set_gap(6, True)
    harness.capture_channel("news")
    harness.advance(4)
    harness.capture_channel("news")
    before = harness.archive("news")

    harness.restart()

    restored = harness.archive("news")
    assert restored["cues"] == before["cues"]
    assert restored["coverage"] == before["coverage"]
    by_seq_before = harness.segments_by_seq(before)
    by_seq_after = harness.segments_by_seq(restored)
    assert {
        seq: segment["status"] for seq, segment in by_seq_before.items()
    } == {seq: segment["status"] for seq, segment in by_seq_after.items()}

    # And capture continues from where it left off.
    harness.set_gap(6, False)
    result = harness.capture_channel("news")
    assert result["captured"] >= 1
    assert harness.segments_by_seq(harness.archive("news"))[6]["status"] == "captured"


def test_archive_range_filter_uses_utc(harness):
    harness.capture_channel("news")
    full = harness.archive("news")

    from_ms = parse_iso_ms("2026-11-20T00:00:36.000Z")
    to_ms = parse_iso_ms("2026-11-20T00:00:54.000Z")
    window = harness.archive("news", **{"from": iso_from_ms(from_ms), "to": iso_from_ms(to_ms)})

    assert window["range"]["from_ms"] == from_ms
    assert window["range"]["to_ms"] == to_ms
    assert all(cue["end_ms"] > from_ms and cue["start_ms"] < to_ms for cue in window["cues"])
    assert [segment["seq"] for segment in window["segments"]] == [6, 7, 8]
    assert len(window["cues"]) < len(full["cues"])


def test_unknown_channel_is_404(harness):
    response = harness.client.get("/api/channels/nope/archive")
    assert response.status_code == 404


def test_bad_time_filter_is_400(harness):
    response = harness.client.get(
        "/api/channels/news/archive", params={"from": "not-a-time"}
    )
    assert response.status_code == 400


def test_reader_never_sees_a_cue_outside_coverage_during_late_capture(harness):
    """A concurrent reader must always get a self-consistent snapshot.

    Even while a late capture flips #9 from missing to captured, no response may
    contain a cue or coverage span whose segments are not all marked captured.
    """
    import threading
    import time

    harness.set_gap(9, True)
    harness.capture_channel("news")
    harness.advance(4)
    harness.capture_channel("news")
    harness.advance(6)
    harness.capture_channel("news")

    violations: list[tuple] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            view = harness.archive("news")
            captured = {
                segment["seq"]
                for segment in view["segments"]
                if segment["status"] == "captured"
            }
            for span in view["coverage"]:
                span_seqs = set(range(span["seq_start"], span["seq_end"] + 1))
                if not span_seqs <= captured:
                    violations.append(("coverage", sorted(span_seqs - captured)))
            for cue in view["cues"]:
                if not set(cue["seqs"]) <= captured:
                    violations.append(("cue", sorted(set(cue["seqs"]) - captured)))

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    try:
        time.sleep(0.05)
        harness.set_gap(9, False)
        harness.capture_channel("news")
        time.sleep(0.15)
    finally:
        stop.set()
        reader_thread.join()

    assert violations == []


def test_clipped_coverage_reports_only_visible_sequences(harness):
    harness.capture_channel("news")
    from_ms = parse_iso_ms("2026-11-20T00:00:36.000Z")  # mid #6
    view = harness.archive("news", **{"from": iso_from_ms(from_ms)})
    assert len(view["coverage"]) == 1
    span = view["coverage"][0]
    assert span["start_ms"] == from_ms
    assert span["seq_start"] == 6
    assert span["seq_end"] == 11
    assert span["complete"] is False


def test_one_sided_range_extends_to_archive_edge(harness):
    harness.capture_channel("news")
    from_ms = parse_iso_ms("2026-11-20T00:00:36.000Z")
    view = harness.archive("news", **{"from": iso_from_ms(from_ms)})
    assert view["range"]["from_ms"] == from_ms
    # No "to" -> the open side stops at the newest archived segment (#11).
    assert view["range"]["to_ms"] == parse_iso_ms("2026-11-20T00:01:12.000Z")


def test_fetch_error_is_recorded_concisely(harness):
    harness.set_gap(9, True)
    harness.capture_channel("news")
    gaps = harness.archive("news")["gaps"]
    assert gaps and gaps[0]["error"] == "HTTP 404"
