"""SQLite 存储层。

并发/一致性设计：
- 每个 trip 有单调递增 revision；确认提交采用条件更新
  UPDATE trip SET revision=? WHERE id=? AND revision=?，配合 BEGIN IMMEDIATE
  写事务，同一 revision 的并发确认只有一个事务能成功（其余提交 0 行 -> 409）。
- 每次确认在单个事务内整趟替换：删除旧 readings/rides/results 后重插，
  任何一步失败全部回滚；查询永远只能看到完整旧版或完整新版。
- WAL 模式 + busy_timeout，兼顾读写并发与重启后的持久化。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trip (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    temp_limit      REAL NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    confirmed_at    TEXT
);
CREATE TABLE IF NOT EXISTS trip_reading (
    trip_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    celsius     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_ride (
    trip_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    box         TEXT NOT NULL,
    loaded_at   INTEGER NOT NULL,
    unloaded_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, revision, box)
);
CREATE TABLE IF NOT EXISTS trip_result (
    trip_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    box             TEXT NOT NULL,
    hot_seconds     INTEGER NOT NULL,
    unknown_seconds INTEGER NOT NULL,
    total_seconds   INTEGER NOT NULL,
    hot_segments    TEXT NOT NULL,
    unknown_segments TEXT NOT NULL,
    PRIMARY KEY (trip_id, revision, box)
);
CREATE INDEX IF NOT EXISTS idx_ride_trip ON trip_ride(trip_id, revision);
CREATE INDEX IF NOT EXISTS idx_result_trip ON trip_result(trip_id, revision);
CREATE TABLE IF NOT EXISTS trip_draft (
    trip_id TEXT PRIMARY KEY, expected_revision INTEGER NOT NULL,
    name TEXT NOT NULL, temp_limit REAL NOT NULL,
    readings TEXT NOT NULL, rides TEXT NOT NULL, results TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS trip_draft_source (
    trip_id TEXT PRIMARY KEY, temp_csv TEXT NOT NULL, handling_csv TEXT NOT NULL
);
"""


class RevisionConflict(RuntimeError):
    """同一 revision 的确认已被其他事务抢先提交。"""


class NotFound(LookupError):
    pass


def get_db_path() -> Path:
    from flask import current_app

    return Path(current_app.config["DATABASE"])


def _connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None：全程手动事务控制，避免 SELECT 隐式开启的事务
    # 让同一连接上的多条语句停留在旧快照，读到跨版本的混合数据
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def db_cursor(commit: bool = False):
    conn = _connect(get_db_path())
    try:
        if commit:
            # 写事务立即拿写锁，配合条件更新保证同修订号并发确认只成功一次
            conn.execute("BEGIN IMMEDIATE")
        else:
            # 显式只读事务：一次查询内的多条 SELECT 共享同一快照，
            # 绝不可能读到"新表头 + 旧明细"之类的跨版本混合状态
            conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 草稿（待确认的修订预览） ----------

def upsert_draft(
    trip_id: str,
    expected_revision: int,
    name: str,
    temp_limit: float,
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    results: dict[str, dict],
) -> None:
    """保存草稿（同一 trip 反复修正只保留最新一份）。

    草稿带 expected_revision：确认时若 trip.revision 已前进则拒绝。
    """
    with db_cursor(commit=True) as conn:
        conn.execute(
            "INSERT INTO trip_draft(trip_id, expected_revision, name, temp_limit, "
            "readings, rides, results) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(trip_id) DO UPDATE SET expected_revision=excluded.expected_revision, "
            "name=excluded.name, temp_limit=excluded.temp_limit, readings=excluded.readings, "
            "rides=excluded.rides, results=excluded.results, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (
                trip_id, expected_revision, name, temp_limit,
                json.dumps(readings, separators=(",", ":")),
                json.dumps(rides, separators=(",", ":")),
                json.dumps(results, separators=(",", ":")),
            ),
        )


def get_draft(trip_id: str) -> dict | None:
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM trip_draft WHERE trip_id=?", (trip_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "trip_id": row["trip_id"],
        "expected_revision": row["expected_revision"],
        "name": row["name"],
        "temp_limit": row["temp_limit"],
        "readings": json.loads(row["readings"]),
        "rides": json.loads(row["rides"]),
        "results": json.loads(row["results"]),
    }


def delete_draft(conn: sqlite3.Connection, trip_id: str) -> None:
    conn.execute("DELETE FROM trip_draft WHERE trip_id=?", (trip_id,))


# ---------- 正式版本（已确认结论，只增不改） ----------

def commit_revision(
    trip_id: str,
    expected_revision: int,
    name: str,
    temp_limit: float,
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    results: dict[str, dict],
) -> int:
    """整趟替换提交。返回新 revision；expected_revision 过期则抛 RevisionConflict。

    新 trip（expected_revision=0 且不存在）创建为 revision 1。
    """
    with db_cursor(commit=True) as conn:
        trip = conn.execute("SELECT revision FROM trip WHERE id=?", (trip_id,)).fetchone()
        current = trip["revision"] if trip else 0
        if current != expected_revision:
            raise RevisionConflict(
                f"修订号已过期：页面基于 rev {expected_revision}，当前为 rev {current}"
            )
        new_revision = current + 1

        if trip is None:
            conn.execute(
                "INSERT INTO trip(id, name, revision, temp_limit, confirmed_at) "
                "VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                (trip_id, name, new_revision, temp_limit),
            )
        else:
            updated = conn.execute(
                "UPDATE trip SET revision=?, name=?, temp_limit=?, "
                "confirmed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id=? AND revision=?",
                (new_revision, name, temp_limit, trip_id, current),
            ).rowcount
            if updated != 1:  # 并发下另一事务已推进 revision
                raise RevisionConflict("同一修订号的并发确认只允许一次")
            # 整趟替换：先删旧数据（含已删除箱号的旧结论），再插新版
            for table in ("trip_reading", "trip_ride", "trip_result"):
                conn.execute(f"DELETE FROM {table} WHERE trip_id=?", (trip_id,))

        conn.executemany(
            "INSERT INTO trip_reading(trip_id, revision, ts, celsius) VALUES(?,?,?,?)",
            [(trip_id, new_revision, ts, c) for ts, c in readings],
        )
        conn.executemany(
            "INSERT INTO trip_ride(trip_id, revision, box, loaded_at, unloaded_at) "
            "VALUES(?,?,?,?,?)",
            [(trip_id, new_revision, box, lo, ul) for box, lo, ul in rides],
        )
        conn.executemany(
            "INSERT INTO trip_result(trip_id, revision, box, hot_seconds, "
            "unknown_seconds, total_seconds, hot_segments, unknown_segments) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    trip_id, new_revision, box, r["hot_seconds"], r["unknown_seconds"],
                    r["total_seconds"],
                    json.dumps(r["hot_segments"], separators=(",", ":")),
                    json.dumps(r["unknown_segments"], separators=(",", ":")),
                )
                for box, r in results.items()
            ],
        )
        delete_draft(conn, trip_id)
        return new_revision


def get_trip(trip_id: str) -> dict:
    with db_cursor() as conn:
        trip = conn.execute("SELECT * FROM trip WHERE id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFound(f"运输趟次不存在：{trip_id}")
        revision = trip["revision"]
        rides = conn.execute(
            "SELECT box, loaded_at, unloaded_at FROM trip_ride "
            "WHERE trip_id=? AND revision=? ORDER BY box", (trip_id, revision)
        ).fetchall()
        results = conn.execute(
            "SELECT * FROM trip_result WHERE trip_id=? AND revision=? ORDER BY box",
            (trip_id, revision),
        ).fetchall()
        readings = conn.execute(
            "SELECT ts, celsius FROM trip_reading WHERE trip_id=? AND revision=? ORDER BY ts",
            (trip_id, revision),
        ).fetchall()
    return {
        "id": trip["id"],
        "name": trip["name"],
        "revision": revision,
        "temp_limit": trip["temp_limit"],
        "created_at": trip["created_at"],
        "confirmed_at": trip["confirmed_at"],
        "readings": [(r["ts"], r["celsius"]) for r in readings],
        "rides": [dict(r) for r in rides],
        "results": [
            {
                "box": r["box"],
                "hot_seconds": r["hot_seconds"],
                "unknown_seconds": r["unknown_seconds"],
                "total_seconds": r["total_seconds"],
                "hot_segments": json.loads(r["hot_segments"]),
                "unknown_segments": json.loads(r["unknown_segments"]),
            }
            for r in results
        ],
    }


def list_trips() -> list[dict]:
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, t.revision, t.temp_limit, t.confirmed_at, "
            "(SELECT COUNT(*) FROM trip_ride r WHERE r.trip_id=t.id AND r.revision=t.revision) AS boxes "
            "FROM trip t ORDER BY t.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
