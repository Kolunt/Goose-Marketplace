"""Очередь и выгрузка Wildberries."""

from __future__ import annotations

import json
from datetime import date, timedelta

from ozon_seller_mcp.db import (
    refresh_wb_sales_daily,
    set_meta,
    upsert_barcode_catalog,
    upsert_wb_finance_line,
    upsert_wb_sale_line,
    upsert_wb_stock_snapshot,
    upsert_wb_supply,
    upsert_wb_supply_item,
)
from ozon_seller_mcp.policy import (
    WB_FINANCE_CHUNK_DAYS,
    WB_FINANCE_CURSOR_TTL_SEC,
    WB_SALES_HISTORY_DAYS,
    WB_STATS_MIN_INTERVAL_SEC,
    chunk_sales_state,
    horizon_start,
    iso_now,
    iter_dates,
    status_after_success,
)
from ozon_seller_mcp.sync import _finish, _insert_chunk, _set_status
from ozon_seller_mcp.wb_client import WbApiError, WbClient
from ozon_seller_mcp.wb_parse import (
    parse_finance_lines,
    parse_sales_items,
    parse_stock_items,
    parse_supply_detail,
    parse_supply_orders,
    parse_supplies_list,
)


def ensure_wb_queue(conn, today: date, horizon_days: int) -> None:
    sales_start = max(
        horizon_start(today, horizon_days),
        today - timedelta(days=WB_SALES_HISTORY_DAYS - 1),
    )
    for day in iter_dates(sales_start, today):
        _insert_chunk(conn, "wb_sales", day, day)
    _insert_chunk(conn, "wb_stocks", today, today)
    finance_start = horizon_start(today, horizon_days)
    cursor = finance_start
    while cursor <= today:
        window_end = min(cursor + timedelta(days=WB_FINANCE_CHUNK_DAYS - 1), today)
        _insert_chunk(conn, "wb_finance", cursor, window_end)
        cursor = window_end + timedelta(days=1)
    _insert_chunk(conn, "wb_supplies", today, today)


def run_wb_chunk(
    conn,
    wb_client: WbClient,
    chunk,
    *,
    today: date,
    now,
    stats_interval: float = WB_STATS_MIN_INTERVAL_SEC,
    sleep=None,
) -> dict:
    waited = False
    try:
        if chunk["dataset"] == "wb_sales":
            waited = _wait_wb_stats(conn, now=now, interval=stats_interval, sleep=sleep)
            day = chunk["period_start"]
            cursor = chunk["cursor"] or day
            payload = wb_client.get(
                "statistics",
                "/api/v1/supplier/sales",
                {"dateFrom": cursor, "flag": 1},
            )
            rows, last_change = parse_sales_items(payload)
            fetched_at = iso_now()
            for row in rows:
                upsert_wb_sale_line(conn, fetched_at=fetched_at, **{k: row[k] for k in (
                    "srid", "sale_date", "barcode", "quantity", "revenue", "is_return", "payload"
                )})
                if row.get("barcode"):
                    upsert_barcode_catalog(
                        conn,
                        barcode=row["barcode"],
                        nm_id=row.get("nm_id"),
                        vendor_code=row.get("vendor_code"),
                        name=row.get("name"),
                        payload=row["payload"],
                        fetched_at=fetched_at,
                    )
            refresh_wb_sales_daily(conn, day=day, fetched_at=fetched_at)
            if rows and last_change and last_change != cursor:
                _set_status(conn, chunk["id"], "pending", cursor=last_change, row_count=len(rows))
            else:
                _finish(conn, chunk, today, len(rows))
            return {"outcome": "ok", "http": True, "waited": waited}
        if chunk["dataset"] == "wb_stocks":
            waited = _wait_wb_stats(conn, now=now, interval=stats_interval, sleep=sleep)
            cursor = chunk["cursor"] or chunk["period_start"]
            payload = wb_client.get(
                "statistics",
                "/api/v1/supplier/stocks",
                {"dateFrom": cursor},
            )
            rows, last_change = parse_stock_items(payload)
            fetched_at = iso_now()
            for row in rows:
                upsert_wb_stock_snapshot(conn, fetched_at=fetched_at, **row)
                upsert_barcode_catalog(
                    conn,
                    barcode=row["barcode"],
                    payload=row["payload"],
                    fetched_at=fetched_at,
                )
            if rows and last_change and last_change != cursor:
                _set_status(conn, chunk["id"], "pending", cursor=last_change, row_count=len(rows))
            else:
                _set_status(conn, chunk["id"], "done", row_count=len(rows), synced_at=fetched_at, error="", cursor="")
            return {"outcome": "ok", "http": True, "waited": waited}
        if chunk["dataset"] == "wb_finance":
            waited = _wait_wb_finance(conn, now=now, interval=stats_interval, sleep=sleep)
            last_rrd = _finance_rrd_id(chunk["cursor"], now)
            body = {
                "dateFrom": chunk["period_start"],
                "dateTo": chunk["period_end"],
                "rrdId": int(last_rrd) if str(last_rrd).isdigit() else 0,
                "period": "daily",
                "limit": 10000,
            }
            payload = wb_client.post("finance", "/api/finance/v1/sales-reports/detailed", body)
            rows, new_rrd = parse_finance_lines(payload)
            fetched_at = iso_now()
            for row in rows:
                upsert_wb_finance_line(conn, fetched_at=fetched_at, **row)
                if row.get("barcode"):
                    upsert_barcode_catalog(
                        conn,
                        barcode=row["barcode"],
                        payload=row["payload"],
                        fetched_at=fetched_at,
                    )
            if rows and new_rrd and new_rrd != last_rrd:
                _set_status(
                    conn,
                    chunk["id"],
                    "pending",
                    cursor=f"{new_rrd}\n{now()}",
                    row_count=len(rows),
                )
            else:
                _finish(conn, chunk, today, len(rows))
            return {"outcome": "ok", "http": True, "waited": waited}
        if chunk["dataset"] == "wb_supplies":
            return _run_wb_supplies(conn, wb_client, chunk, now=now)
    except WbApiError as exc:
        _set_status(conn, chunk["id"], "failed", error=exc.message[:500], synced_at=iso_now())
        return {"outcome": "error", "http": True, "waited": waited}
    _set_status(conn, chunk["id"], "failed", error=f"Неизвестный WB dataset: {chunk['dataset']}")
    return {"outcome": "error", "http": False, "waited": waited}


def _finance_rrd_id(cursor: str | None, now) -> str:
    if not cursor or "\n" not in cursor:
        return "0"
    rrd_id, raw_ts = cursor.split("\n", 1)
    try:
        stored = float(raw_ts)
    except ValueError:
        return "0"
    if now() - stored > WB_FINANCE_CURSOR_TTL_SEC:
        return "0"
    return rrd_id


def _supply_cursor(raw: str | None) -> dict:
    if not raw:
        return {"phase": "list", "next": 0, "ids": [], "current": None}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"phase": "list", "next": 0, "ids": [], "current": None}
    data.setdefault("phase", "list")
    data.setdefault("next", 0)
    data.setdefault("ids", [])
    data.setdefault("current", None)
    return data


def _run_wb_supplies(conn, wb_client: WbClient, chunk, *, now) -> dict:
    state = _supply_cursor(chunk["cursor"])
    fetched_at = iso_now()
    if state["phase"] == "list":
        payload = wb_client.get("marketplace", "/api/v3/supplies", {"limit": 100, "next": state["next"]})
        ids = parse_supplies_list(payload)
        for supply_id in ids:
            if supply_id not in state["ids"]:
                state["ids"].append(supply_id)
        next_token = payload.get("next") if isinstance(payload, dict) else None
        if next_token:
            state["next"] = next_token
            _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state))
            return {"outcome": "ok", "http": True, "waited": False}
        state["phase"] = "detail"
        state["index"] = 0
        if not state["ids"]:
            _set_status(conn, chunk["id"], "done", row_count=0, synced_at=fetched_at, cursor="")
            return {"outcome": "ok", "http": True, "waited": False}
        _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state))
        return {"outcome": "ok", "http": True, "waited": False}
    if state["phase"] == "detail":
        index = int(state.get("index", 0))
        if index >= len(state["ids"]):
            state["phase"] = "done"
            _set_status(conn, chunk["id"], "done", row_count=index, synced_at=fetched_at, cursor="")
            return {"outcome": "ok", "http": False, "waited": False}
        supply_id = state["ids"][index]
        detail = wb_client.get("marketplace", f"/api/v3/supplies/{supply_id}")
        parsed = parse_supply_detail(detail)
        if parsed:
            upsert_wb_supply(
                conn,
                supply_id=parsed["supply_id"],
                state=parsed["state"],
                inbound=parsed["inbound"],
                payload=parsed["payload"],
                fetched_at=fetched_at,
            )
            orders = wb_client.get("marketplace", f"/api/v3/supplies/{supply_id}/orders")
            for row in parse_supply_orders(orders):
                upsert_wb_supply_item(
                    conn,
                    supply_id=supply_id,
                    barcode=row["barcode"],
                    quantity=row["quantity"],
                    payload=row["payload"],
                    fetched_at=fetched_at,
                )
        state["index"] = index + 1
        if state["index"] < len(state["ids"]):
            _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state))
        else:
            _set_status(conn, chunk["id"], "done", row_count=state["index"], synced_at=fetched_at, cursor="")
        return {"outcome": "ok", "http": True, "waited": False}
    _set_status(conn, chunk["id"], "done", synced_at=fetched_at, cursor="")
    return {"outcome": "ok", "http": False, "waited": False}


def _wait_wb_stats(conn, *, now, interval: float, sleep) -> bool:
    return _wait_rate(conn, key="wb_stats_last_request_at", interval=interval, now=now, sleep=sleep)


def _wait_wb_finance(conn, *, now, interval: float, sleep) -> bool:
    return _wait_rate(conn, key="wb_finance_last_request_at", interval=interval, now=now, sleep=sleep)


def _wait_rate(conn, *, key: str, interval: float, now, sleep) -> bool:
    import time

    from ozon_seller_mcp.db import get_meta, set_meta

    sleeper = sleep or time.sleep
    raw = get_meta(conn, key)
    waited = False
    current = now()
    if raw and interval > 0:
        elapsed = current - float(raw)
        if elapsed < interval:
            sleeper(interval - elapsed)
            waited = True
            current = now()
    set_meta(conn, key, str(current))
    conn.commit()
    return waited
