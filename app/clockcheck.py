"""记录仪时钟走偏校时核查（纯函数，无框架依赖）。

背景：记录仪时钟走偏会让测温记录与装卸时刻错位。本模块用门磁事件
（记录仪显示的 UTC 时刻）与已确认版本各箱装卸时刻（去重后作锚点）比对，
寻找整秒固定偏移 δ，使得"事件显示时刻 + δ ≈ 真实时刻"。

规则：
- 只在 ±MAX_SHIFT_SECONDS（60 分钟）内寻找整秒固定偏移。
- 偏移后的事件与锚点相差不超过 PAIR_TOLERANCE_SECONDS（30 秒）才可配对；
  各事件和锚点至多使用一次；配对须保持时间先后（不交叉）。
- 按配对数最多、总时差最小选出偏移；至少需要 MIN_PAIRS（2）对。
- 不足两对或最优偏移不唯一时，给出待核原因与并列候选。
- 本模块只读不改写任何正式读数。

实现：对所有 7201 个整秒偏移同时求"最多配对、最小总时差"的非交叉匹配
（LCS 型动态规划，按偏移维度向量化），再对最优偏移回溯出具体配对。
"""
from __future__ import annotations

import numpy as np

MAX_EVENTS = 40            # 单次核查最多门磁事件条数
MAX_ANCHORS = 40           # 单次核查最多锚点个数
MAX_SHIFT_SECONDS = 60 * 60    # 偏移搜索窗：±60 分钟
PAIR_TOLERANCE_SECONDS = 30    # 偏移后事件与锚点相差不超过 30 秒才可配对
MIN_PAIRS = 2              # 得出唯一偏移所需的最少配对数

# (配对数, 总时差) 打包进单个 int64 键：key = pairs*BIG - cost，
# 键越大越优 ⇔ 配对数越多越优，配对数相同时总时差越小越优。
# cost 最大 40*30=1200，远小于 BIG，不会串位。
_BIG = 1 << 32


def build_anchors(rides: list[dict]) -> list[int]:
    """从某次已确认修订的装卸记录提取锚点：各箱装车/卸车时刻去重后排序。"""
    points: set[int] = set()
    for r in rides:
        points.add(int(r["loaded_at"]))
        points.add(int(r["unloaded_at"]))
    return sorted(points)


def evaluate_offsets(
    event_ts: list[int],
    anchors: list[int],
    max_shift: int = MAX_SHIFT_SECONDS,
    tolerance: int = PAIR_TOLERANCE_SECONDS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """对每个整秒偏移 δ ∈ [-max_shift, max_shift] 求最优配对。

    返回 (offsets, counts, costs) 三个等长 int64 数组：每个偏移下的
    最多配对数，以及该配对数下的最小总时差（秒）。
    """
    offsets = np.arange(-max_shift, max_shift + 1, dtype=np.int64)
    width = offsets.size
    if not event_ts or not anchors:
        return offsets, np.zeros(width, dtype=np.int64), np.zeros(width, dtype=np.int64)

    anchor_arr = np.asarray(anchors, dtype=np.int64)
    m = anchor_arr.size

    # dp[j] = 已处理事件与 anchors[:j] 的最优键（配对数*BIG - 总时差）
    prev = np.zeros((m + 1, width), dtype=np.int64)
    cur = np.zeros((m + 1, width), dtype=np.int64)
    for e in event_ts:
        shifted = offsets + int(e)                       # 各偏移下事件校正后时刻
        diff = np.abs(shifted[None, :] - anchor_arr[:, None])  # (锚点, 偏移)
        match_key = np.where(diff <= tolerance, _BIG - diff, 0)
        cur[0].fill(0)
        for j in range(1, m + 1):
            pair = prev[j - 1] + match_key[j - 1]        # 事件-锚点 j 配对
            np.maximum(prev[j], cur[j - 1], out=cur[j])  # 跳过事件 / 跳过锚点
            np.maximum(cur[j], pair, out=cur[j])
        prev, cur = cur, prev
    keys = prev[m]
    # 解码：key = count*BIG - cost（cost >= 0），count 需向上取整
    counts = (keys + (_BIG - 1)) // _BIG
    costs = counts * _BIG - keys
    return offsets, counts, costs


def match_at_offset(
    event_ts: list[int],
    anchors: list[int],
    offset: int,
    tolerance: int = PAIR_TOLERANCE_SECONDS,
) -> list[dict]:
    """回溯某个整秒偏移下的最优配对（配对数最多，并列时总时差最小）。

    返回按时间先后排列的配对列表，每项含事件/锚点下标与配对偏差
    （diff = 事件校正后时刻 - 锚点时刻，|diff| <= tolerance）。
    """
    n, m = len(event_ts), len(anchors)
    # dp[i][j] = (配对数, 总时差)，比较键为 (配对数, -总时差)
    dp = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        shifted = int(event_ts[i - 1]) + offset
        row, prow = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            best = prow[j] if (prow[j][0], -prow[j][1]) >= (row[j - 1][0], -row[j - 1][1]) else row[j - 1]
            d = shifted - anchors[j - 1]
            if -tolerance <= d <= tolerance:
                cand = (prow[j - 1][0] + 1, prow[j - 1][1] + abs(d))
                if (cand[0], -cand[1]) > (best[0], -best[1]):
                    best = cand
            row[j] = best

    pairs: list[dict] = []
    i, j = n, m
    while i > 0 and j > 0:
        d = int(event_ts[i - 1]) + offset - anchors[j - 1]
        if (-tolerance <= d <= tolerance
                and dp[i][j] == (dp[i - 1][j - 1][0] + 1, dp[i - 1][j - 1][1] + abs(d))):
            pairs.append({"event_index": i - 1, "anchor_index": j - 1, "diff": d})
            i -= 1
            j -= 1
        elif dp[i][j] == dp[i - 1][j]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


def offset_ranges(offsets: list[int]) -> list[tuple[int, int]]:
    """把有序整秒偏移列表压缩为连续区间 [(start, end), ...]，便于展示并列候选。"""
    ranges: list[list[int]] = []
    for o in offsets:
        o = int(o)
        if ranges and o == ranges[-1][1] + 1:
            ranges[-1][1] = o
        else:
            ranges.append([o, o])
    return [(s, e) for s, e in ranges]


def run_check(
    events: list[dict],
    anchors: list[int],
    max_shift: int = MAX_SHIFT_SECONDS,
    tolerance: int = PAIR_TOLERANCE_SECONDS,
) -> dict:
    """执行校时核查，返回报告 dict（不修改任何输入，也不触碰正式数据）。

    events 为解析后的门磁事件（含 ts/event/box/row，已按时刻排序）；
    anchors 为去重排序后的锚点时刻。status ∈ ok / insufficient_pairs / ambiguous。
    """
    event_ts = [int(e["ts"]) for e in events]
    offsets, counts, costs = evaluate_offsets(event_ts, anchors, max_shift, tolerance)
    best_count = int(counts.max())

    report: dict = {
        "status": "insufficient_pairs",
        "reasons": [],
        "event_count": len(events),
        "anchor_count": len(anchors),
        "best_count": best_count,
        "best_cost": None,
        "offset": None,
        "pairs": [],
        "unpaired_events": list(events),
        "unpaired_anchors": list(anchors),
        "candidate_ranges": [],
        "tied_count": 0,
        "max_shift": max_shift,
        "tolerance": tolerance,
        "min_pairs": MIN_PAIRS,
    }

    if best_count == 0:
        report["reasons"].append(
            f"在 ±{max_shift // 60} 分钟搜索窗内没有任何门磁事件能与锚点配对"
            f"（0 对，至少需要 {MIN_PAIRS} 对）；请核对上传的是否为同一趟次的门磁记录"
        )
        return report

    mask = counts == best_count
    best_cost = int(costs[mask].min())
    tied = [int(o) for o in offsets[mask & (costs == best_cost)]]
    report["best_cost"] = best_cost
    report["tied_count"] = len(tied)
    report["candidate_ranges"] = offset_ranges(tied)

    if best_count < MIN_PAIRS:
        report["reasons"].append(
            f"最优偏移下也只能配出 {best_count} 对，少于判定所需的最少 {MIN_PAIRS} 对；"
            "可能是门磁事件缺失或记录仪偏移超出搜索窗"
        )
        return report

    if len(tied) > 1:
        report["status"] = "ambiguous"
        report["reasons"].append(
            f"最优偏移不唯一：{len(tied)} 个整秒偏移并列最优"
            f"（同为 {best_count} 对、总时差 {best_cost} 秒），无法唯一确定记录仪偏移"
        )
        return report

    # 唯一最优偏移：回溯配对证据
    offset = tied[0]
    pairs = match_at_offset(event_ts, anchors, offset, tolerance)
    used_events = {p["event_index"] for p in pairs}
    used_anchors = {p["anchor_index"] for p in pairs}
    report["status"] = "ok"
    report["offset"] = offset
    report["pairs"] = [
        {
            "event": events[p["event_index"]],
            "anchor_ts": anchors[p["anchor_index"]],
            "shifted_ts": int(events[p["event_index"]]["ts"]) + offset,
            "diff": p["diff"],
        }
        for p in pairs
    ]
    report["unpaired_events"] = [e for i, e in enumerate(events) if i not in used_events]
    report["unpaired_anchors"] = [a for i, a in enumerate(anchors) if i not in used_anchors]
    return report
