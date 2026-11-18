"""CSV 解析测试。"""
from datetime import datetime, timezone

import pytest

from app.parser import CSVError, parse_handling_csv, parse_temperature_csv


def _epoch(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp())


def test_parse_temperature_basic():
    csv_text = "timestamp_utc,celsius\n2026-11-18T08:00:00Z,5.2\n2026-11-18T08:10:00+00:00,6\n"
    out = parse_temperature_csv(csv_text.encode())
    assert out == [(_epoch("2026-11-18T08:00:00Z"), 5.2),
                   (_epoch("2026-11-18T08:10:00Z"), 6.0)]


def test_parse_temperature_naive_utc_and_aliases():
    csv_text = "时间,温度\n2026-11-18 08:00:00,5.2\n"
    out = parse_temperature_csv(csv_text.encode("utf-8-sig"))
    assert out == [(_epoch("2026-11-18T08:00:00Z"), 5.2)]


def test_parse_temperature_bad_row():
    with pytest.raises(CSVError, match="第 2 行"):
        parse_temperature_csv(b"timestamp_utc,celsius\nnot-a-time,5\n")
    with pytest.raises(CSVError, match="温度不是数字"):
        parse_temperature_csv(b"timestamp_utc,celsius\n2026-11-18T08:00:00Z,hot\n")


def test_parse_temperature_missing_column():
    with pytest.raises(CSVError, match="缺少必需列"):
        parse_temperature_csv(b"foo,bar\n1,2\n")


def test_parse_handling_basic():
    csv_text = (
        "box,loaded_at,unloaded_at\n"
        "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
        "BOX-2,2026-11-18T08:30:00Z,2026-11-18T10:00:00Z\n"
    )
    out = parse_handling_csv(csv_text.encode())
    assert out[0][0] == "BOX-1"
    assert out[0][2] - out[0][1] == 3600
    assert len(out) == 2


def test_handling_duplicate_box_rejected():
    csv_text = (
        "box,loaded_at,unloaded_at\n"
        "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
        "BOX-1,2026-11-18T09:00:00Z,2026-11-18T10:00:00Z\n"
    )
    with pytest.raises(CSVError, match="重复"):
        parse_handling_csv(csv_text.encode())


def test_handling_unload_before_load_rejected():
    csv_text = "box,loaded_at,unloaded_at\nBOX-1,2026-11-18T09:00:00Z,2026-11-18T08:00:00Z\n"
    with pytest.raises(CSVError, match="卸车时刻必须晚于"):
        parse_handling_csv(csv_text.encode())


def test_empty_files_rejected():
    with pytest.raises(CSVError):
        parse_temperature_csv(b"")
    with pytest.raises(CSVError):
        parse_handling_csv(b"timestamp_utc,celsius\n")
