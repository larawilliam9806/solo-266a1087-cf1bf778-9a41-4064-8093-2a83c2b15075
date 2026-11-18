"""CSV 解析与校验。

温度 CSV：表头含 timestamp_utc, celsius（允许常见别名），时间为 ISO-8601 UTC。
装卸 CSV：表头含 box, loaded_at, unloaded_at。
门磁事件 CSV：表头含事件时刻列（event_time 等别名），event/box 列可选；
  时刻为记录仪显示的 UTC 时刻（可能带时钟偏移，供校时核查使用）。
解析失败抛出 CSVError，消息带行号，直接展示给用户。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

TEMP_ALIASES = {
    "timestamp_utc": ("timestamp_utc", "timestamp", "time", "utc_time", "时刻", "时间"),
    "celsius": ("celsius", "temperature", "temp", "温度", "摄氏度"),
}
RIDE_ALIASES = {
    "box": ("box", "box_id", "container", "箱号", "箱子编号"),
    "loaded_at": ("loaded_at", "load_time", "loaded", "装车时刻", "装车时间"),
    "unloaded_at": ("unloaded_at", "unload_time", "unloaded", "卸车时刻", "卸车时间"),
}
EVENT_ALIASES = {
    "event_time": ("event_time", "timestamp_utc", "timestamp", "time", "occurred_at",
                   "事件时刻", "时刻", "时间"),
}
EVENT_OPTIONAL_ALIASES = {
    "event": ("event", "event_type", "type", "事件", "事件类型"),
    "box": ("box", "box_id", "container", "箱号", "箱子编号"),
}


class CSVError(ValueError):
    pass


def _resolve_columns(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    normalized = {h.strip().lstrip("﻿").lower(): i for i, h in enumerate(header)}
    resolved: dict[str, int] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name.lower() in normalized:
                resolved[canonical] = normalized[name.lower()]
                break
        if canonical not in resolved:
            raise CSVError(f"缺少必需列：{canonical}（实际表头：{', '.join(header) or '空'}）")
    return resolved


def _resolve_optional_columns(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """与 _resolve_columns 相同，但列缺失时不报错（用于可选列）。"""
    normalized = {h.strip().lstrip("﻿").lower(): i for i, h in enumerate(header)}
    resolved: dict[str, int] = {}
    for canonical, names in aliases.items():
        for name in names:
            if name.lower() in normalized:
                resolved[canonical] = normalized[name.lower()]
                break
    return resolved


def parse_timestamp(value: str, row_no: int, field: str) -> int:
    value = value.strip()
    if not value:
        raise CSVError(f"第 {row_no} 行：{field} 为空")
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CSVError(f"第 {row_no} 行：{field} 时间格式无法解析（{value}）") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp())


def parse_temperature_csv(content: bytes) -> list[tuple[int, float]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVError("温度文件不是 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or not any(c.strip() for c in rows[0]):
        raise CSVError("温度文件为空或缺少表头")
    cols = _resolve_columns(rows[0], TEMP_ALIASES)
    readings: list[tuple[int, float]] = []
    for offset, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) <= max(cols.values()):
            raise CSVError(f"第 {offset} 行：列数不足")
        ts = parse_timestamp(row[cols["timestamp_utc"]], offset, "timestamp_utc")
        raw = row[cols["celsius"]].strip()
        try:
            temp = float(raw)
        except ValueError as exc:
            raise CSVError(f"第 {offset} 行：温度不是数字（{raw}）") from exc
        readings.append((ts, temp))
    if not readings:
        raise CSVError("温度文件没有任何数据行")
    return readings


def parse_handling_csv(content: bytes) -> list[tuple[str, int, int]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVError("装卸文件不是 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or not any(c.strip() for c in rows[0]):
        raise CSVError("装卸文件为空或缺少表头")
    cols = _resolve_columns(rows[0], RIDE_ALIASES)
    rides: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for offset, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) <= max(cols.values()):
            raise CSVError(f"第 {offset} 行：列数不足")
        box = row[cols["box"]].strip()
        if not box:
            raise CSVError(f"第 {offset} 行：箱号为空")
        if box in seen:
            raise CSVError(f"第 {offset} 行：箱号 {box} 重复，每个箱号只能有一条装卸记录")
        seen.add(box)
        load_ts = parse_timestamp(row[cols["loaded_at"]], offset, "loaded_at")
        unload_ts = parse_timestamp(row[cols["unloaded_at"]], offset, "unloaded_at")
        if unload_ts <= load_ts:
            raise CSVError(f"第 {offset} 行：箱号 {box} 的卸车时刻必须晚于装车时刻")
        rides.append((box, load_ts, unload_ts))
    if not rides:
        raise CSVError("装卸文件没有任何数据行")
    return rides


def parse_door_events_csv(content: bytes) -> list[dict]:
    """解析门磁事件 CSV，返回按 (时刻, 原始行号) 排序的事件列表。

    每项 {"ts", "event", "box", "row"}：ts 为记录仪显示的 UTC 时刻（epoch 秒），
    event/box 为可选的展示信息（无对应列或留空时为 None），row 为原始行号。
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CSVError("门磁事件文件不是 UTF-8 编码") from exc
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or not any(c.strip() for c in rows[0]):
        raise CSVError("门磁事件文件为空或缺少表头")
    cols = _resolve_columns(rows[0], EVENT_ALIASES)
    optional = _resolve_optional_columns(rows[0], EVENT_OPTIONAL_ALIASES)
    events: list[dict] = []
    for offset, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        if len(row) <= max(cols.values()):
            raise CSVError(f"第 {offset} 行：列数不足")
        ts = parse_timestamp(row[cols["event_time"]], offset, "event_time")

        def _opt(name: str) -> str | None:
            idx = optional.get(name)
            if idx is None or idx >= len(row):
                return None
            value = row[idx].strip()
            return value or None

        events.append({"ts": ts, "event": _opt("event"), "box": _opt("box"), "row": offset})
    if not events:
        raise CSVError("门磁事件文件没有任何数据行")
    events.sort(key=lambda e: (e["ts"], e["row"]))
    return events
