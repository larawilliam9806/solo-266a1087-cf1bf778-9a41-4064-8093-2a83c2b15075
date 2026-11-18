"""冷链温度合规计算引擎（纯函数，无框架依赖）。

规则：
- 温度读数按时间排序。相邻两个读数间隔 <= GAP_LIMIT_SECONDS（10 分钟）时，
  前一读数代表该段区间；间隔 > 10 分钟的整段为"未知"。
- 首个读数之前、末次读数之后均无数据覆盖，属于"未知"。
- 对每个箱号，仅统计其在车时段 [装车时刻, 卸车时刻) 与时间线相交的部分：
  读数温度 > 温度上限 -> 超温段；未知区间 -> 未知段。
- 缺少覆盖装车或卸车边界的读数时，边界处落入未知区间，计入未知时长。
"""
from __future__ import annotations

from dataclasses import dataclass

GAP_LIMIT_SECONDS = 10 * 60


@dataclass(frozen=True)
class Segment:
    start: int  # Unix epoch 秒，UTC
    end: int
    kind: str  # "ok" | "hot" | "unknown"
    temperature: float | None = None  # 代表该段的读数温度（未知段为 None）


def build_timeline(readings: list[tuple[int, float]]) -> list[Segment]:
    """把读数序列展开成覆盖全部读数跨度的时间线段（半开区间 [start, end)）。

    readings 为 (epoch_seconds, celsius) 列表，可乱序、可含同刻重复值
    （同刻重复时取最后一条）。
    """
    if not readings:
        return []

    by_ts: dict[int, float] = {}
    for ts, temp in readings:
        by_ts[ts] = temp
    ordered = sorted(by_ts.items())

    segments: list[Segment] = []
    for (ts, temp), (next_ts, _) in zip(ordered, ordered[1:]):
        gap = next_ts - ts
        if gap <= GAP_LIMIT_SECONDS:
            segments.append(Segment(ts, next_ts, "hot", temp))
        else:
            segments.append(Segment(ts, ts + GAP_LIMIT_SECONDS, "hot", temp))
            segments.append(Segment(ts + GAP_LIMIT_SECONDS, next_ts, "unknown"))

    return segments


def _classify(seg: Segment, limit: float) -> str:
    if seg.kind == "unknown":
        return "unknown"
    return "hot" if seg.temperature is not None and seg.temperature > limit else "ok"


def analyze_ride(
    load_ts: int,
    unload_ts: int,
    timeline: list[Segment],
    limit: float,
) -> dict:
    """计算单个箱号在车时段的超温/未知时长与对应时间段。

    返回 {"hot_seconds", "unknown_seconds", "hot_segments", "unknown_segments"}，
    段为合并相邻同类区间后的 [start, end) 列表（dict 形式）。
    """
    if unload_ts <= load_ts:
        raise ValueError("卸车时刻必须晚于装车时刻")

    clipped: list[Segment] = []
    for seg in timeline:
        start = max(seg.start, load_ts)
        end = min(seg.end, unload_ts)
        if start < end:
            clipped.append(Segment(start, end, _classify(seg, limit), seg.temperature))

    # 时间线与在车时段完全不相交（或根本没有读数）：整段未知
    if not clipped:
        return {
            "hot_seconds": 0,
            "unknown_seconds": unload_ts - load_ts,
            "total_seconds": unload_ts - load_ts,
            "hot_segments": [],
            "unknown_segments": [_segment_dict(Segment(load_ts, unload_ts, "unknown"))],
        }

    # 在车起点之前 / 终点之后若无任何读数覆盖，补未知段
    first_start = clipped[0].start
    last_end = clipped[-1].end
    if first_start > load_ts:
        clipped.insert(0, Segment(load_ts, first_start, "unknown"))
    if last_end < unload_ts:
        clipped.append(Segment(last_end, unload_ts, "unknown"))

    merged = _merge(clipped)
    hot = [s for s in merged if s.kind == "hot"]
    unknown = [s for s in merged if s.kind == "unknown"]
    return {
        "hot_seconds": sum(s.end - s.start for s in hot),
        "unknown_seconds": sum(s.end - s.start for s in unknown),
        "total_seconds": unload_ts - load_ts,
        "hot_segments": [_segment_dict(s) for s in hot],
        "unknown_segments": [_segment_dict(s) for s in unknown],
    }


def _merge(segments: list[Segment]) -> list[Segment]:
    out: list[Segment] = []
    for seg in segments:
        if out and out[-1].end == seg.start and out[-1].kind == seg.kind:
            prev = out.pop()
            out.append(Segment(prev.start, seg.end, seg.kind, seg.temperature or prev.temperature))
        else:
            out.append(seg)
    return out


def _segment_dict(seg: Segment) -> dict:
    d = {"start": seg.start, "end": seg.end}
    if seg.temperature is not None:
        d["temperature"] = seg.temperature
    return d


def analyze_all(
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    limit: float,
) -> dict[str, dict]:
    """对一趟运输的所有箱号计算结论。rides 为 (box, load_ts, unload_ts)。"""
    timeline = build_timeline(readings)
    results: dict[str, dict] = {}
    for box, load_ts, unload_ts in rides:
        results[box] = analyze_ride(load_ts, unload_ts, timeline, limit)
    return results
