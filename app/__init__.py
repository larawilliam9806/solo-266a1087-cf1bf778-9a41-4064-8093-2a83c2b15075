"""冷链运输温度合规平台 —— Flask 应用工厂与路由。"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask, abort, flash, redirect, render_template, request, url_for,
)

from . import storage
from .clockcheck import (
    MAX_ANCHORS, MAX_EVENTS, MAX_SHIFT_SECONDS, MIN_PAIRS, PAIR_TOLERANCE_SECONDS,
    build_anchors, run_check,
)
from .engine import analyze_all
from .parser import CSVError, parse_door_events_csv, parse_handling_csv, parse_temperature_csv


def create_app(db_path: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    default_db = Path(__file__).resolve().parent.parent / "data" / "coldchain.db"
    app.config["DATABASE"] = str(db_path or os.environ.get("DATABASE") or default_db)
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
    app.secret_key = "cold-chain-demo-secret-change-in-production"
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    storage.init_db(app.config["DATABASE"])

    # ---------- 辅助 ----------

    def _read_upload(field: str) -> bytes | None:
        f = request.files.get(field)
        if f is None or f.filename == "":
            return None
        return f.read()

    def _build_draft(trip_id: str, expected_revision: int, name: str,
                     temp_limit: float, temp_bytes: bytes | None,
                     handling_bytes: bytes | None,
                     previous: dict | None) -> dict:
        """合并上传与上一版数据，计算预览。previous 为旧草稿或已确认版本。"""
        old_temp_csv = previous["temp_csv"] if previous else ""
        old_handling_csv = previous["handling_csv"] if previous else ""

        if temp_bytes is not None:
            readings = parse_temperature_csv(temp_bytes)
            temp_csv_text = temp_bytes.decode("utf-8-sig")
        else:
            if not previous:
                raise CSVError("请上传温度 CSV（UTC 时刻、摄氏度）")
            readings = previous["readings"]
            temp_csv_text = old_temp_csv

        if handling_bytes is not None:
            rides = parse_handling_csv(handling_bytes)
            handling_csv_text = handling_bytes.decode("utf-8-sig")
        else:
            if not previous:
                raise CSVError("请上传装卸 CSV（箱号、装车时刻、卸车时刻）")
            rides = [tuple(r) for r in previous["rides"]]
            handling_csv_text = old_handling_csv

        results = analyze_all(readings, rides, temp_limit)
        return {
            "trip_id": trip_id,
            "expected_revision": expected_revision,
            "name": name,
            "temp_limit": temp_limit,
            "readings": readings,
            "rides": rides,
            "results": results,
            "temp_csv": temp_csv_text,
            "handling_csv": handling_csv_text,
        }

    def _persist_draft(draft: dict) -> None:
        storage.upsert_draft(
            draft["trip_id"], draft["expected_revision"], draft["name"],
            draft["temp_limit"], draft["readings"], draft["rides"], draft["results"],
        )
        # CSV 原文存在旁路表，供再次修正时回填
        with storage.db_cursor(commit=True) as conn:
            conn.execute(
                "INSERT INTO trip_draft_source(trip_id, temp_csv, handling_csv) VALUES(?,?,?) "
                "ON CONFLICT(trip_id) DO UPDATE SET temp_csv=excluded.temp_csv, "
                "handling_csv=excluded.handling_csv",
                (draft["trip_id"], draft["temp_csv"], draft["handling_csv"]),
            )

    def _load_full_draft(trip_id: str) -> dict | None:
        draft = storage.get_draft(trip_id)
        if draft is None:
            return None
        with storage.db_cursor() as conn:
            row = conn.execute(
                "SELECT temp_csv, handling_csv FROM trip_draft_source WHERE trip_id=?",
                (trip_id,),
            ).fetchone()
        if row:
            draft["temp_csv"] = row["temp_csv"]
            draft["handling_csv"] = row["handling_csv"]
        else:
            draft["temp_csv"], draft["handling_csv"] = "", ""
        return draft

    # ---------- 页面路由 ----------

    @app.route("/")
    def index():
        return render_template("index.html", trips=storage.list_trips())

    @app.route("/trips", methods=["POST"])
    def create_trip():
        trip_id = uuid.uuid4().hex[:12]
        name = request.form.get("name", "").strip() or f"运输趟次 {trip_id[:6]}"
        try:
            temp_limit = float(request.form.get("temp_limit", ""))
        except (TypeError, ValueError):
            flash("请输入有效的温度上限（数字，摄氏度）", "error")
            return redirect(url_for("index"))
        try:
            draft = _build_draft(
                trip_id, expected_revision=0, name=name, temp_limit=temp_limit,
                temp_bytes=_read_upload("temp_file"),
                handling_bytes=_read_upload("handling_file"),
                previous=None,
            )
        except CSVError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        _persist_draft(draft)
        return redirect(url_for("preview", trip_id=trip_id))

    @app.route("/trips/<trip_id>/preview")
    def preview(trip_id: str):
        draft = _load_full_draft(trip_id)
        if draft is None:
            abort(404)
        confirmed = None
        try:
            confirmed = storage.get_trip(trip_id)
        except storage.NotFound:
            pass
        stale = confirmed is not None and confirmed["revision"] != draft["expected_revision"]
        return render_template(
            "preview.html", draft=draft, confirmed=confirmed, stale=stale,
            fmt=fmt_ts, fmt_duration=fmt_duration,
        )

    @app.route("/trips/<trip_id>/revise", methods=["POST"])
    def revise(trip_id: str):
        try:
            confirmed = storage.get_trip(trip_id)
        except storage.NotFound:
            confirmed = None
        current = _load_full_draft(trip_id)
        if current is None and confirmed is None:
            abort(404)
        # 基准修订号以已确认版本为准；草稿始终针对最新确认版
        if confirmed is not None:
            expected = confirmed["revision"]
            previous = {
                "readings": confirmed["readings"],
                "rides": [(r["box"], r["loaded_at"], r["unloaded_at"]) for r in confirmed["rides"]],
                "temp_csv": current["temp_csv"] if current else "",
                "handling_csv": current["handling_csv"] if current else "",
            }
        else:
            expected = 0
            previous = current

        name = request.form.get("name", "").strip() or (
            current["name"] if current else confirmed["name"]
        )
        fallback_limit = current["temp_limit"] if current else confirmed["temp_limit"]
        try:
            temp_limit = float(request.form.get("temp_limit", fallback_limit))
        except (TypeError, ValueError):
            flash("温度上限不是有效数字", "error")
            return redirect(url_for("preview", trip_id=trip_id))
        try:
            draft = _build_draft(
                trip_id, expected_revision=expected, name=name, temp_limit=temp_limit,
                temp_bytes=_read_upload("temp_file"),
                handling_bytes=_read_upload("handling_file"),
                previous=previous,
            )
        except CSVError as exc:
            flash(str(exc), "error")
            return redirect(url_for("preview", trip_id=trip_id))
        _persist_draft(draft)
        flash("已按修正内容重新计算预览，尚未写入正式结论", "info")
        return redirect(url_for("preview", trip_id=trip_id))

    @app.route("/trips/<trip_id>/confirm", methods=["POST"])
    def confirm(trip_id: str):
        draft = storage.get_draft(trip_id)
        if draft is None:
            abort(404)
        try:
            new_rev = storage.commit_revision(
                trip_id, draft["expected_revision"], draft["name"], draft["temp_limit"],
                draft["readings"], [tuple(r) for r in draft["rides"]], draft["results"],
            )
        except storage.RevisionConflict as exc:
            flash(f"确认失败：{exc}。请基于最新版本重新修正。", "error")
            return redirect(url_for("preview", trip_id=trip_id))
        with storage.db_cursor(commit=True) as conn:
            conn.execute("DELETE FROM trip_draft_source WHERE trip_id=?", (trip_id,))
        flash(f"已确认，正式版本号 rev {new_rev}（整趟替换，旧结论已清除）", "success")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trips/<trip_id>")
    def trip_detail(trip_id: str):
        trip = storage.get_trip(trip_id)
        has_draft = storage.get_draft(trip_id) is not None
        return render_template(
            "detail.html", trip=trip, has_draft=has_draft,
            fmt=fmt_ts, fmt_duration=fmt_duration,
        )

    @app.route("/trips/<trip_id>/api/results")
    def api_results(trip_id: str):
        trip = storage.get_trip(trip_id)
        return app.response_class(
            json.dumps(trip, ensure_ascii=False, indent=2),
            mimetype="application/json",
        )

    # ---------- 记录仪校时核查（只读，不改写正式结论） ----------

    def _clock_check_context(trip: dict) -> dict:
        """组装核查页公共上下文。锚点与修订号来自同一次 get_trip 快照，
        核查与修订交错时也不会混用不同版本的锚点和修订号。"""
        anchors = build_anchors(trip["rides"])
        return {
            "trip": trip,
            "anchors": anchors,
            "limits": {
                "max_events": MAX_EVENTS,
                "max_anchors": MAX_ANCHORS,
                "max_shift_minutes": MAX_SHIFT_SECONDS // 60,
                "tolerance": PAIR_TOLERANCE_SECONDS,
                "min_pairs": MIN_PAIRS,
            },
        }

    @app.route("/trips/<trip_id>/clock-check")
    def clock_check(trip_id: str):
        try:
            trip = storage.get_trip(trip_id)
        except storage.NotFound:
            abort(404)
        return render_template("clock_check.html", report=None,
                               **_clock_check_context(trip))

    @app.route("/trips/<trip_id>/clock-check", methods=["POST"])
    def clock_check_run(trip_id: str):
        # 单次只读事务取出修订号与该版装卸记录，二者始终同属一个版本
        try:
            trip = storage.get_trip(trip_id)
        except storage.NotFound:
            abort(404)
        ctx = _clock_check_context(trip)
        anchors = ctx["anchors"]

        def _retry(message: str):
            flash(message, "error")
            return redirect(url_for("clock_check", trip_id=trip_id))

        if not anchors:
            return _retry("该版本没有任何装卸时刻可作锚点，无法进行校时核查")
        if len(anchors) > MAX_ANCHORS:
            return _retry(
                f"该版本装卸时刻去重后有 {len(anchors)} 个锚点，"
                f"超过单次 {MAX_ANCHORS} 个上限，请拆分核查"
            )
        content = _read_upload("events_file")
        if content is None:
            return _retry("请上传门磁事件 CSV（记录仪显示的 UTC 时刻）")
        try:
            events = parse_door_events_csv(content)
        except CSVError as exc:
            # 上传/解析失败仅提示，不触碰正式结论
            return _retry(str(exc))
        if len(events) > MAX_EVENTS:
            return _retry(
                f"门磁事件共 {len(events)} 条，超过单次 {MAX_EVENTS} 条上限，请拆分后重试"
            )
        report = run_check(events, anchors)
        return render_template(
            "clock_check.html", report=report, events=events, **ctx,
        )

    @app.errorhandler(404)
    def not_found(_):
        return render_template("404.html"), 404

    @app.template_filter("fmt")
    def fmt_ts(epoch: int) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    @app.template_filter("duration")
    def fmt_duration(seconds: int) -> str:
        sign = "-" if seconds < 0 else ""
        seconds = abs(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{sign}{h:d}:{m:02d}:{s:02d}"

    @app.template_filter("offset")
    def fmt_offset(seconds: int) -> str:
        """整秒偏移的可读形式：+300 秒 / -45 秒 / 0 秒。"""
        if seconds > 0:
            return f"+{seconds} 秒"
        if seconds < 0:
            return f"-{abs(seconds)} 秒"
        return "0 秒"

    return app
