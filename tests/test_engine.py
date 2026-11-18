"""引擎规则单元测试。"""
from datetime import datetime, timezone

import pytest

from app.engine import GAP_LIMIT_SECONDS, analyze_all, analyze_ride, build_timeline


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


READINGS = [
    ("2026-11-18T08:00:00Z", 5.0),
    ("2026-11-18T08:05:00Z", 6.0),
    ("2026-11-18T08:10:00Z", 9.0),   # 超温读数，代表 [08:10, 08:15)
    ("2026-11-18T08:15:00Z", 7.0),
    ("2026-11-18T08:40:00Z", 6.0),   # 与上一点间隔 25 分钟 -> 中间未知
    ("2026-11-18T08:45:00Z", 4.0),
]


@pytest.fixture
def timeline():
    return build_timeline([(ts(t), v) for t, v in READINGS])


def test_gap_within_limit_uses_previous_reading(timeline):
    # 08:00->08:05 间隔 5 分钟，整段由前一读数 5.0 代表
    seg = next(s for s in timeline if s.start == ts("2026-11-18T08:00:00Z"))
    assert seg.end == ts("2026-11-18T08:05:00Z")
    assert seg.kind == "hot" and seg.temperature == 5.0


def test_gap_over_limit_whole_unknown_span(timeline):
    unknown = [s for s in timeline if s.kind == "unknown"]
    assert len(unknown) == 1
    # 前读数只代表其后 10 分钟；10 分钟之后到下一读数整段未知
    assert unknown[0].start == ts("2026-11-18T08:25:00Z")
    assert unknown[0].end == ts("2026-11-18T08:40:00Z")
    assert unknown[0].end - unknown[0].start == 15 * 60
    # 前段读数的代表区被截到 10 分钟
    head = next(s for s in timeline if s.start == ts("2026-11-18T08:15:00Z"))
    assert head.end == ts("2026-11-18T08:25:00Z")


def test_exactly_ten_minutes_is_covered(timeline):
    # 构造恰好 600 秒间隔
    tl = build_timeline([(0, 5.0), (GAP_LIMIT_SECONDS, 6.0)])
    assert all(s.kind != "unknown" for s in tl)
    assert tl[0].end == GAP_LIMIT_SECONDS


def test_ride_hot_and_unknown_durations(timeline):
    res = analyze_ride(
        ts("2026-11-18T08:00:00Z"), ts("2026-11-18T09:00:00Z"), timeline, limit=8.0
    )
    # 超温：08:10-08:15 共 5 分钟
    assert res["hot_seconds"] == 300
    assert res["hot_segments"][0]["temperature"] == 9.0
    # 未知：中间缺口 15 分钟 + 末次读数 08:45 之后到 09:00 共 15 分钟
    assert res["unknown_seconds"] == 30 * 60
    assert res["total_seconds"] == 3600


def test_boundary_without_reading_counts_unknown(timeline):
    # 在车起点早于首读数：07:50-08:00 缺测计未知
    res = analyze_ride(
        ts("2026-11-18T07:50:00Z"), ts("2026-11-18T08:05:00Z"), timeline, limit=8.0
    )
    assert res["unknown_seconds"] == 10 * 60
    assert res["unknown_segments"][0]["start"] == ts("2026-11-18T07:50:00Z")
    assert res["hot_seconds"] == 0


def test_no_readings_at_all_is_fully_unknown():
    res = analyze_ride(0, 600, [], limit=8.0)
    assert res["unknown_seconds"] == 600
    assert res["hot_seconds"] == 0
    assert res["unknown_segments"] == [{"start": 0, "end": 600}]


def test_single_reading_cannot_cover_anything_beyond_itself():
    # 只有一个读数时无法形成任何"前段代表"的区间
    tl = build_timeline([(ts("2026-11-18T08:00:00Z"), 5.0)])
    assert tl == []
    res = analyze_ride(
        ts("2026-11-18T08:00:00Z"), ts("2026-11-18T08:30:00Z"), tl, limit=8.0
    )
    assert res["unknown_seconds"] == 30 * 60


def test_half_open_intervals_do_not_double_count():
    # 两个相邻 5 分钟段拼成 10 分钟在车时段
    tl = build_timeline([(0, 5.0), (300, 6.0), (600, 7.0)])
    res = analyze_ride(0, 600, tl, limit=8.0)
    assert res["total_seconds"] == 600
    assert res["hot_seconds"] == 0 and res["unknown_seconds"] == 0
    # 提高上限判定后，所有覆盖段均为 ok，且没有未知段
    res2 = analyze_ride(0, 600, tl, limit=4.0)
    assert res2["hot_seconds"] == 600 and res2["unknown_seconds"] == 0


def test_equality_at_limit_is_compliant(timeline):
    res = analyze_ride(
        ts("2026-11-18T08:10:00Z"), ts("2026-11-18T08:15:00Z"), timeline, limit=9.0
    )
    assert res["hot_seconds"] == 0


def test_analyze_all_per_box_windows():
    readings = [
        (ts("2026-11-18T08:00:00Z"), 5.0),
        (ts("2026-11-18T08:05:00Z"), 10.0),
        (ts("2026-11-18T08:10:00Z"), 7.0),
    ]
    rides = [
        ("A", ts("2026-11-18T08:00:00Z"), ts("2026-11-18T08:05:00Z")),  # 5.0 代表，合格
        ("B", ts("2026-11-18T08:05:00Z"), ts("2026-11-18T08:20:00Z")),  # 10.0 代表 5 分钟，后 10 分钟未知
    ]
    out = analyze_all(readings, rides, limit=8.0)
    assert out["A"]["hot_seconds"] == 0 and out["A"]["unknown_seconds"] == 0
    assert out["B"]["hot_seconds"] == 300
    assert out["B"]["unknown_seconds"] == 600


def test_unsorted_and_duplicate_timestamps():
    tl = build_timeline([(60, 4.0), (0, 3.0), (0, 7.0)])
    assert tl[0].start == 0 and tl[0].temperature == 7.0  # 同刻取最后一条


def test_invalid_ride_window_rejected():
    with pytest.raises(ValueError):
        analyze_ride(100, 100, [], limit=8.0)
