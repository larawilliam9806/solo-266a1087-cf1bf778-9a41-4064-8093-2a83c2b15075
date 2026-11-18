"""记录仪校时核查：偏移搜索/配对逻辑 + 门磁 CSV + 端到端流程。"""
import io
from datetime import datetime, timezone

import pytest

from app import create_app, storage
from app.clockcheck import (
    MAX_ANCHORS, MAX_EVENTS, MAX_SHIFT_SECONDS, PAIR_TOLERANCE_SECONDS,
    build_anchors, evaluate_offsets, match_at_offset, offset_ranges, run_check,
)
from app.parser import CSVError, parse_door_events_csv


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp())


def ev(epoch: int, row: int = 0) -> dict:
    return {"ts": epoch, "event": None, "box": None, "row": row}


def make_events(epochs):
    return [ev(e, i + 2) for i, e in enumerate(epochs)]


# ---------- 纯逻辑：偏移评估与配对 ----------

def test_unique_offset_found():
    events = make_events([100, 200, 300])
    anchors = [400, 500, 600]  # 记录仪偏慢 300 秒
    report = run_check(events, anchors)
    assert report["status"] == "ok"
    assert report["offset"] == 300
    assert report["best_count"] == 3 and report["best_cost"] == 0
    assert [p["diff"] for p in report["pairs"]] == [0, 0, 0]
    assert report["unpaired_events"] == [] and report["unpaired_anchors"] == []


def test_negative_offset_found():
    events = make_events([1000, 2000, 3000])
    anchors = [880, 1880, 2880]  # 记录仪偏快 120 秒
    report = run_check(events, anchors)
    assert report["status"] == "ok"
    assert report["offset"] == -120
    assert report["best_count"] == 3


def test_search_window_edges():
    events = make_events([0, 100, 200])
    for skew in (MAX_SHIFT_SECONDS, -MAX_SHIFT_SECONDS):
        anchors = [e + skew for e in (0, 100, 200)]
        report = run_check(events, anchors)
        assert report["status"] == "ok" and report["offset"] == skew
    # 超出 ±60 分钟搜索窗 + 30 秒容差 + 事件跨度：任何事件-锚点组合都配不上
    anchors = [e + MAX_SHIFT_SECONDS + PAIR_TOLERANCE_SECONDS + 300 for e in (0, 100, 200)]
    report = run_check(events, anchors)
    assert report["status"] == "insufficient_pairs"
    assert report["best_count"] == 0


def test_tolerance_boundary_inclusive():
    events = make_events([0, 100])
    anchors = [30, 70]  # δ=0 时两对偏差都恰好 30 秒
    report = run_check(events, anchors)
    assert report["status"] == "ok" and report["offset"] == 0
    assert report["best_cost"] == 2 * PAIR_TOLERANCE_SECONDS
    # 31 秒则不可配对
    report = run_check(events, [31, 69])
    assert report["status"] == "insufficient_pairs"
    assert report["best_count"] <= 1


def test_each_event_and_anchor_used_once():
    events = make_events([0, 1])
    anchors = [0]  # 一个锚点只能配一个事件
    report = run_check(events, anchors)
    assert report["best_count"] == 1
    assert report["status"] == "insufficient_pairs"


def test_pairing_preserves_time_order():
    # 两个事件都在两个锚点的容差内：配对必须不交叉
    events = make_events([0, 4])
    anchors = [3, 5]
    pairs = match_at_offset([e["ts"] for e in events], anchors, 0)
    assert len(pairs) == 2
    event_idx = [p["event_index"] for p in pairs]
    anchor_idx = [p["anchor_index"] for p in pairs]
    assert event_idx == sorted(event_idx) and anchor_idx == sorted(anchor_idx)
    assert pairs == [
        {"event_index": 0, "anchor_index": 0, "diff": -3},
        {"event_index": 1, "anchor_index": 1, "diff": -1},
    ]


def test_max_pairs_then_min_total_diff():
    # 三个锚点的对齐偏移分别为 3/5/9，总时差在中位数 5 处最小且唯一
    events = make_events([0, 100, 200])
    anchors = [3, 105, 209]
    report = run_check(events, anchors)
    assert report["status"] == "ok"
    assert report["offset"] == 5
    assert report["best_count"] == 3
    assert report["best_cost"] == 2 + 0 + 4


def test_ambiguous_plateau_lists_tied_candidates():
    # 两对对齐偏移 3 与 7：其间所有整秒偏移同为最优（配对数、总时差都相同）
    events = make_events([0, 1000])
    anchors = [3, 1007]
    report = run_check(events, anchors)
    assert report["status"] == "ambiguous"
    assert report["best_count"] == 2 and report["best_cost"] == 4
    assert report["tied_count"] == 5
    assert report["candidate_ranges"] == [(3, 7)]
    assert any("不唯一" in r for r in report["reasons"])


def test_insufficient_pairs_reason():
    report = run_check(make_events([0]), [10])
    assert report["status"] == "insufficient_pairs"
    assert report["best_count"] == 1
    assert any("最少" in r or "至少需要" in r for r in report["reasons"])
    report = run_check(make_events([0]), [10000])
    assert report["status"] == "insufficient_pairs"
    assert report["best_count"] == 0
    assert any("没有任何" in r for r in report["reasons"])


def test_unpaired_events_and_anchors_reported():
    events = make_events([100, 200, 300, 999999])
    anchors = [400, 500, 600, 123456]
    report = run_check(events, anchors)
    assert report["status"] == "ok" and report["offset"] == 300
    assert [e["ts"] for e in report["unpaired_events"]] == [999999]
    assert report["unpaired_anchors"] == [123456]


def test_evaluate_offsets_matches_brute_force():
    # 与暴力逐偏移 DP 对拍：配对数与最小总时差一致
    events = [10, 50, 90, 130]
    anchors = [40, 80, 120, 200]
    offsets, counts, costs = evaluate_offsets(events, anchors, max_shift=300)
    for i, off in enumerate(range(-300, 301)):
        pairs = match_at_offset(events, anchors, off)
        assert counts[i] == len(pairs)
        assert costs[i] == sum(abs(p["diff"]) for p in pairs)


def test_build_anchors_dedup_and_sort():
    rides = [
        {"box": "A", "loaded_at": 300, "unloaded_at": 900},
        {"box": "B", "loaded_at": 100, "unloaded_at": 900},
        {"box": "C", "loaded_at": 300, "unloaded_at": 500},
    ]
    assert build_anchors(rides) == [100, 300, 500, 900]


def test_offset_ranges_grouping():
    assert offset_ranges([1, 2, 3, 5, 7, 8]) == [(1, 3), (5, 5), (7, 8)]
    assert offset_ranges([]) == []


# ---------- 门磁事件 CSV 解析 ----------

def test_parse_door_events_basic_sorted_and_optional():
    csv_text = (
        "event_time,event,box\n"
        "2026-11-18T08:05:00Z,open,BOX-2\n"
        "2026-11-18T08:00:00Z,close,BOX-1\n"
    )
    out = parse_door_events_csv(csv_text.encode())
    assert [e["ts"] for e in out] == [ts("2026-11-18T08:00:00Z"), ts("2026-11-18T08:05:00Z")]
    assert out[0]["event"] == "close" and out[0]["box"] == "BOX-1"
    assert out[0]["row"] == 3  # 原始行号保留


def test_parse_door_events_aliases_and_naive_utc():
    csv_text = "时间,事件\n2026-11-18 08:00:00,开门\n"
    out = parse_door_events_csv(csv_text.encode("utf-8-sig"))
    assert out[0]["ts"] == ts("2026-11-18T08:00:00Z")
    assert out[0]["event"] == "开门" and out[0]["box"] is None


def test_parse_door_events_errors():
    with pytest.raises(CSVError, match="缺少必需列"):
        parse_door_events_csv(b"foo,bar\n1,2\n")
    with pytest.raises(CSVError, match="第 2 行"):
        parse_door_events_csv(b"event_time\nnot-a-time\n")
    with pytest.raises(CSVError, match="没有任何数据行"):
        parse_door_events_csv(b"event_time\n")


# ---------- 端到端：网页校时核查 ----------

TEMP_CSV = (
    "timestamp_utc,celsius\n"
    "2026-11-18T08:00:00Z,5.0\n"
    "2026-11-18T08:05:00Z,6.0\n"
    "2026-11-18T08:10:00Z,9.0\n"
    "2026-11-18T08:15:00Z,7.0\n"
    "2026-11-18T08:40:00Z,6.0\n"
    "2026-11-18T08:45:00Z,4.0\n"
).encode()

HANDLING_CSV = (
    "box,loaded_at,unloaded_at\n"
    "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
    "BOX-2,2026-11-18T08:05:00Z,2026-11-18T08:45:00Z\n"
).encode()

# 记录仪偏慢 300 秒：显示时刻 = 真实时刻 - 300
DOOR_EVENTS_CSV = (
    "event_time,event,box\n"
    "2026-11-18T07:55:00Z,open,BOX-1\n"
    "2026-11-18T08:00:00Z,open,BOX-2\n"
    "2026-11-18T08:40:00Z,close,BOX-2\n"
    "2026-11-18T08:55:00Z,close,BOX-1\n"
).encode()


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "test.db")
    app.config.update(TESTING=True)
    return app


@pytest.fixture(autouse=True)
def _app_context(app):
    with app.app_context():
        yield


@pytest.fixture
def client(app):
    return app.test_client()


def _confirm_trip(client, handling=HANDLING_CSV) -> str:
    resp = client.post(
        "/trips",
        data={
            "name": "校时测试趟次", "temp_limit": "8.0",
            "temp_file": (io.BytesIO(TEMP_CSV), "temp.csv"),
            "handling_file": (io.BytesIO(handling), "handling.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    trip_id = resp.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")
    return trip_id


def _check(client, trip_id, events=DOOR_EVENTS_CSV):
    return client.post(
        f"/trips/{trip_id}/clock-check",
        data={"events_file": (io.BytesIO(events), "events.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_clock_check_form_lists_anchors_and_revision(client):
    trip_id = _confirm_trip(client)
    page = client.get(f"/trips/{trip_id}/clock-check")
    assert page.status_code == 200
    assert "锚点取自 rev 1".encode() in page.data
    assert "08:00:00 UTC".encode() in page.data  # 锚点时刻列出
    assert "开始校时核查".encode() in page.data


def test_clock_check_unique_offset_report(client):
    trip_id = _confirm_trip(client)
    page = _check(client, trip_id)
    assert page.status_code == 200
    assert "唯一最优偏移".encode() in page.data
    assert "+300 秒".encode() in page.data
    assert "偏慢 300 秒".encode() in page.data
    assert "基于 rev 1".encode() in page.data
    assert "配对证据".encode() in page.data
    assert "未改写任何正式读数".encode() in page.data


def test_clock_check_does_not_modify_official_results(client):
    trip_id = _confirm_trip(client)
    before = storage.get_trip(trip_id)
    _check(client, trip_id)
    after = storage.get_trip(trip_id)
    assert before == after  # 正式结论（含修订号）完全未变


def test_clock_check_bad_upload_keeps_official(client):
    trip_id = _confirm_trip(client)
    before = storage.get_trip(trip_id)
    page = _check(client, trip_id, events=b"event_time\nnot-a-time\n")
    assert "时间格式无法解析".encode() in page.data
    assert storage.get_trip(trip_id) == before


def test_clock_check_too_many_events_rejected(client):
    trip_id = _confirm_trip(client)
    rows = ["event_time"]
    base = ts("2026-11-18T07:55:00Z")
    for i in range(MAX_EVENTS + 1):
        rows.append(datetime.fromtimestamp(base + 60 * i, tz=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
    page = _check(client, trip_id, events=("\n".join(rows) + "\n").encode())
    assert f"超过单次 {MAX_EVENTS} 条上限".encode() in page.data


def test_clock_check_too_many_anchors_rejected(client):
    rows = ["box,loaded_at,unloaded_at"]
    base = ts("2026-11-18T08:00:00Z")
    for i in range(21):  # 21 箱 -> 42 个去重锚点
        load = datetime.fromtimestamp(base + 1800 * i, tz=timezone.utc)
        unload = datetime.fromtimestamp(base + 1800 * i + 600, tz=timezone.utc)
        rows.append(f"BOX-{i},{load:%Y-%m-%dT%H:%M:%SZ},{unload:%Y-%m-%dT%H:%M:%SZ}")
    trip_id = _confirm_trip(client, handling=("\n".join(rows) + "\n").encode())
    form = client.get(f"/trips/{trip_id}/clock-check")
    assert f"超过单次 {MAX_ANCHORS} 个上限".encode() in form.data
    page = _check(client, trip_id)
    assert f"超过单次 {MAX_ANCHORS} 个上限".encode() in page.data


def test_clock_check_ambiguous_lists_candidates(client):
    handling = (
        "box,loaded_at,unloaded_at\n"
        "BOX-1,2026-11-18T08:00:00Z,2026-11-18T08:10:00Z\n"
        "BOX-2,2026-11-18T08:10:00Z,2026-11-18T08:20:00Z\n"
    ).encode()
    trip_id = _confirm_trip(client, handling=handling)
    # 对齐偏移 3 秒与 7 秒 -> 3~7 秒五个整秒偏移并列最优
    events = (
        "event_time\n"
        "2026-11-18T07:59:57Z\n"
        "2026-11-18T08:09:53Z\n"
    ).encode()
    page = _check(client, trip_id, events=events)
    assert "最优偏移不唯一".encode() in page.data
    assert "并列候选偏移".encode() in page.data
    assert "+3 秒 ~ +7 秒".encode() in page.data
    assert "未改写任何正式读数".encode() in page.data


def test_clock_check_insufficient_pairs_reported(client):
    trip_id = _confirm_trip(client)
    events = "event_time\n2026-11-18T07:55:00Z\n".encode()  # 只能配 1 对
    page = _check(client, trip_id, events=events)
    assert "待人工核查".encode() in page.data
    assert "至少".encode() in page.data


def test_clock_check_unpaired_events_listed(client):
    trip_id = _confirm_trip(client)
    events = (
        "event_time,event\n"
        "2026-11-18T07:55:00Z,open\n"
        "2026-11-18T08:00:00Z,open\n"
        "2026-11-18T08:40:00Z,close\n"
        "2026-11-18T08:55:00Z,close\n"
        "2026-11-19T00:00:00Z,open\n"  # 远离所有锚点，无法配对
    ).encode()
    page = _check(client, trip_id, events=events)
    assert "唯一最优偏移".encode() in page.data
    assert "未配对事件（1 条）".encode() in page.data
    assert "2026-11-19 00:00:00 UTC".encode() in page.data


def test_clock_check_revision_interleaving_uses_consistent_snapshot(client):
    trip_id = _confirm_trip(client)
    page = _check(client, trip_id)
    assert "基于 rev 1".encode() in page.data and "+300 秒".encode() in page.data

    # 修订：装卸时刻整体后移 600 秒，确认成为 rev 2
    new_handling = (
        "box,loaded_at,unloaded_at\n"
        "BOX-1,2026-11-18T08:10:00Z,2026-11-18T09:10:00Z\n"
        "BOX-2,2026-11-18T08:15:00Z,2026-11-18T08:55:00Z\n"
    ).encode()
    client.post(
        f"/trips/{trip_id}/revise",
        data={"name": "校时测试趟次", "temp_limit": "8.0",
              "handling_file": (io.BytesIO(new_handling), "handling.csv")},
        content_type="multipart/form-data",
    )
    client.post(f"/trips/{trip_id}/confirm")
    assert storage.get_trip(trip_id)["revision"] == 2

    # 同一份门磁事件：锚点已后移 600 秒，偏移应变大 600 秒，且报告标明 rev 2
    page = _check(client, trip_id)
    assert "基于 rev 2".encode() in page.data
    assert "+900 秒".encode() in page.data
    assert "基于 rev 1".encode() not in page.data


def test_clock_check_requires_confirmed_trip(client):
    assert client.get("/trips/nonexistent/clock-check").status_code == 404
    # 只有草稿、尚未确认的趟次同样 404
    resp = client.post(
        "/trips",
        data={
            "name": "未确认", "temp_limit": "8.0",
            "temp_file": (io.BytesIO(TEMP_CSV), "temp.csv"),
            "handling_file": (io.BytesIO(HANDLING_CSV), "handling.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    draft_id = resp.request.path.split("/")[-2]
    assert client.get(f"/trips/{draft_id}/clock-check").status_code == 404
