"""Очередь горизонта, заморозка закрытых периодов и короткая пачка запросов к Ozon."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import date, timedelta

from ozon_seller_mcp.client import OzonApiError
from ozon_seller_mcp.db import (
    data_as_of,
    get_meta,
    insert_stock_snapshot,
    remember_listing,
    set_meta,
    upsert_accrual,
    upsert_accrual_type,
    upsert_barcode_catalog,
    upsert_price,
    upsert_product,
    upsert_return,
    upsert_sale,
    upsert_supply_bundle_item,
    upsert_supply_order,
)
from ozon_seller_mcp.parse import (
    BASIC_METRICS,
    EXTENDED_METRICS,
    analytics_request,
    parse_accrual_page,
    parse_accrual_types,
    parse_bundle_items,
    parse_prices,
    parse_product_info,
    parse_product_list,
    parse_returns,
    parse_sales_rows,
    parse_stock_items,
    parse_supply_list,
    parse_supply_orders,
)
from ozon_seller_mcp.policy import (
    ACCRUAL_CURSOR_TTL_SEC,
    ACCRUAL_PAGE_LIMIT,
    ANALYTICS_MIN_INTERVAL_SEC,
    ANALYTICS_PAGE_LIMIT,
    HISTORY_HORIZON_DAYS,
    PRICE_PAGE_LIMIT,
    RETURNS_WINDOW_DAYS,
    STOCK_SNAPSHOT_MAX_AGE_DAYS,
    SUPPLY_GET_LIMIT,
    SUPPLY_LIST_LIMIT,
    SYNC_BATCH_API_CALLS,
    WAREHOUSE_BATCH_SIZE,
    chunk_sales_state,
    classify_api_error,
    horizon_start,
    is_metric_rejected,
    iso_now,
    iter_dates,
    iter_sales_chunks,
    ranges_overlap,
    settling_is_stale,
    status_after_success,
    types_are_stale,
    utc_bounds,
)

logger = logging.getLogger(__name__)

DATASET_ORDER = {
    "products": 0,
    "finance_accrual_types": 1,
    "sales": 2,
    "finance_accrual": 3,
    "prices": 4,
    "returns": 5,
    "supply_orders": 6,
    "stocks_turnover": 7,
    "stocks_warehouse": 8,
    "wb_sales": 9,
    "wb_finance": 10,
    "wb_stocks": 11,
    "wb_supplies": 12,
}
WINDOWED_DATASETS = ("sales", "finance_accrual", "returns", "wb_sales", "wb_finance")
WB_DATASETS = frozenset({"wb_sales", "wb_stocks", "wb_finance", "wb_supplies"})
OZON_DATASETS = frozenset(
    {
        "products",
        "sales",
        "finance_accrual",
        "finance_accrual_types",
        "prices",
        "returns",
        "supply_orders",
        "stocks_turnover",
        "stocks_warehouse",
        "finance_totals",
        "finance_ops",
    }
)


def _chunk_marketplace(dataset: str) -> str:
    return "wb" if dataset in WB_DATASETS else "ozon"
LEGACY_FINANCE = ("finance_totals", "finance_ops")
LEGACY_FINANCE_ERROR = "Метод отключён: начисления читаются из /v1/finance/accrual/by-day"
TYPES_PERIOD = date(1970, 1, 1)


def ensure_queue(conn, today: date, horizon_days: int) -> None:
    set_meta(conn, "history_horizon_days", str(horizon_days))
    for start, end in iter_sales_chunks(today, horizon_days):
        _insert_chunk(conn, "sales", start, end)
    start = horizon_start(today, horizon_days)
    for day in iter_dates(start, today):
        _insert_chunk(conn, "finance_accrual", day, day)
    _ensure_types_chunk(conn)
    _ensure_daily_chunk(conn, "prices", today)
    _insert_chunk(conn, "returns", today - timedelta(days=RETURNS_WINDOW_DAYS - 1), today)
    _ensure_products_chunk(conn, today)
    _ensure_daily_chunk(conn, "supply_orders", today)
    _ensure_turnover_chunk(conn, today)
    _retire_legacy_finance(conn)
    conn.commit()


def _insert_chunk(
    conn,
    dataset: str,
    start: date,
    end: date,
    *,
    offset_value: int = 0,
    page: int = 1,
    cursor: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_chunks (
            dataset, period_start, period_end, offset_value, page, cursor, status
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ON CONFLICT(dataset, period_start, period_end, offset_value, page) DO NOTHING
        """,
        (dataset, start.isoformat(), end.isoformat(), offset_value, page, cursor),
    )


def _retire_legacy_finance(conn) -> None:
    conn.execute(
        """
        UPDATE sync_chunks
        SET status = 'unavailable', error = ?
        WHERE dataset IN ('finance_totals', 'finance_ops')
          AND status != 'unavailable'
        """,
        (LEGACY_FINANCE_ERROR,),
    )


def _ensure_types_chunk(conn) -> None:
    row = conn.execute(
        "SELECT 1 FROM sync_chunks WHERE dataset = 'finance_accrual_types'"
    ).fetchone()
    if row is None:
        _insert_chunk(conn, "finance_accrual_types", TYPES_PERIOD, TYPES_PERIOD)


def _ensure_daily_chunk(conn, dataset: str, today: date) -> None:
    period = today.isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM sync_chunks
        WHERE dataset = ? AND period_start = ? AND period_end = ?
        """,
        (dataset, period, period),
    ).fetchone()
    if row is None:
        _insert_chunk(conn, dataset, today, today)


def _ensure_products_chunk(conn, today: date) -> None:
    period = today.isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM sync_chunks
        WHERE dataset = 'products' AND period_start = ? AND period_end = ?
        """,
        (period, period),
    ).fetchone()
    if row is None:
        _insert_chunk(conn, "products", today, today)


def _ensure_turnover_chunk(conn, today: date) -> None:
    if not _snapshot_is_stale(conn, today):
        return
    period = today.isoformat()
    row = conn.execute(
        """
        SELECT 1 FROM sync_chunks
        WHERE dataset = 'stocks_turnover' AND period_start = ? AND offset_value = 0
        """,
        (period,),
    ).fetchone()
    if row is None:
        _insert_chunk(conn, "stocks_turnover", today, today)


def _snapshot_is_stale(conn, today: date) -> bool:
    row = conn.execute("SELECT MAX(fetched_at) AS ts FROM stock_snapshots").fetchone()
    if row is None or not row["ts"]:
        return True
    snapped = date.fromisoformat(str(row["ts"])[:10])
    return (today - snapped).days >= STOCK_SNAPSHOT_MAX_AGE_DAYS


def mark_due(
    conn,
    today: date,
    *,
    refresh_open: bool,
    overlap: tuple[str, str] | None = None,
) -> None:
    rows = conn.execute("SELECT * FROM sync_chunks").fetchall()
    for row in rows:
        if row["status"] == "unavailable":
            continue
        if overlap and row["dataset"] in WINDOWED_DATASETS:
            if not ranges_overlap(row["period_start"], row["period_end"], overlap[0], overlap[1]):
                continue
        if row["dataset"] in ("sales", "finance_accrual", "returns", "wb_sales", "wb_finance"):
            state = chunk_sales_state(
                date.fromisoformat(row["period_start"]),
                date.fromisoformat(row["period_end"]),
                today,
            )
            _apply_due(conn, row, state, today, refresh_open=refresh_open)
        elif row["dataset"] == "finance_accrual_types":
            if row["status"] in ("done", "frozen") and types_are_stale(row["synced_at"], today):
                _reset_period(conn, row)
    conn.commit()


def _apply_due(conn, row, state: str, today: date, *, refresh_open: bool) -> None:
    status = row["status"]
    if state == "frozen":
        if status == "done":
            _set_status(conn, row["id"], "frozen")
        return
    should_refresh = False
    if state == "open" and refresh_open and status in ("done", "frozen", "failed"):
        should_refresh = True
    if state == "settling" and status in ("done", "frozen") and settling_is_stale(row["synced_at"], today):
        should_refresh = True
    if state == "settling" and refresh_open and status == "failed":
        should_refresh = True
    if should_refresh and row["offset_value"] == 0 and row["page"] == 1:
        _reset_period(conn, row)


def _reset_period(conn, row) -> None:
    conn.execute(
        """
        DELETE FROM sync_chunks
        WHERE dataset = ? AND period_start = ? AND period_end = ?
          AND NOT (offset_value = 0 AND page = 1)
        """,
        (row["dataset"], row["period_start"], row["period_end"]),
    )
    conn.execute(
        """
        UPDATE sync_chunks
        SET status = 'pending', error = NULL, row_count = NULL, synced_at = NULL, cursor = NULL
        WHERE id = ?
        """,
        (row["id"],),
    )


def _chunk_status(conn, chunk_id: int) -> str | None:
    row = conn.execute("SELECT status FROM sync_chunks WHERE id = ?", (chunk_id,)).fetchone()
    if row is None:
        return None
    return str(row["status"])


def _set_status(conn, chunk_id: int, status: str, *, error: str | None = None, row_count: int | None = None, synced_at: str | None = None, cursor: str | None = None) -> None:
    conn.execute(
        """
        UPDATE sync_chunks
        SET status = ?,
            error = COALESCE(?, error),
            row_count = COALESCE(?, row_count),
            synced_at = COALESCE(?, synced_at),
            cursor = COALESCE(?, cursor)
        WHERE id = ?
        """,
        (status, error, row_count, synced_at, cursor, chunk_id),
    )


def unfreeze(conn, dataset: str, period_start: str, period_end: str) -> int:
    cursor = conn.execute(
        """
        UPDATE sync_chunks
        SET status = 'pending', error = NULL
        WHERE dataset = ?
          AND period_start <= ?
          AND period_end >= ?
          AND status IN ('frozen', 'done', 'unavailable', 'failed')
        """,
        (dataset, period_end, period_start),
    )
    conn.commit()
    return cursor.rowcount


def next_pending(
    conn,
    *,
    datasets: tuple[str, ...] | None = None,
    overlap: tuple[str, str] | None = None,
    marketplaces: tuple[str, ...] | None = None,
):
    rows = conn.execute("SELECT * FROM sync_chunks WHERE status = 'pending'").fetchall()
    matched = []
    for row in rows:
        if datasets and row["dataset"] not in datasets:
            continue
        if marketplaces and _chunk_marketplace(row["dataset"]) not in marketplaces:
            continue
        if overlap and row["dataset"] in WINDOWED_DATASETS:
            if not ranges_overlap(row["period_start"], row["period_end"], overlap[0], overlap[1]):
                continue
        matched.append(row)
    matched.sort(key=lambda row: (row["offset_value"], row["page"]))
    matched.sort(key=lambda row: row["period_start"], reverse=True)
    matched.sort(key=lambda row: DATASET_ORDER.get(row["dataset"], 9))
    matched.sort(key=lambda row: 0 if row["dataset"] in ("finance_accrual", "wb_finance") and row["cursor"] else 1)
    if not matched:
        return None
    return matched[0]


def sync_batch(
    conn,
    client,
    *,
    wb_client=None,
    today: date | None = None,
    horizon_days: int | None = None,
    max_calls: int = SYNC_BATCH_API_CALLS,
    refresh_open: bool = False,
    ensure: bool = True,
    datasets: tuple[str, ...] | None = None,
    overlap: tuple[str, str] | None = None,
    marketplaces: tuple[str, ...] = ("ozon",),
    page_limit: int = ANALYTICS_PAGE_LIMIT,
    analytics_interval: float = ANALYTICS_MIN_INTERVAL_SEC,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> dict:
    current = today or date.today()
    if ensure:
        horizon = horizon_days if horizon_days is not None else _stored_horizon(conn)
        if "ozon" in marketplaces:
            ensure_queue(conn, current, horizon)
        if "wb" in marketplaces:
            from ozon_seller_mcp.wb_sync import ensure_wb_queue

            ensure_wb_queue(conn, current, horizon)
        mark_due(conn, current, refresh_open=refresh_open, overlap=overlap if not refresh_open else None)
    sleeper = sleep or time.sleep
    clock = now or time.time
    calls = 0
    hit_minute = False
    hit_daily = False
    stopped = "queue_empty"
    while calls < max_calls:
        chunk = next_pending(conn, datasets=datasets, overlap=overlap, marketplaces=marketplaces)
        if chunk is None:
            stopped = "queue_empty"
            break
        outcome = _run_chunk(
            conn,
            client,
            chunk,
            wb_client=wb_client,
            today=current,
            page_limit=page_limit,
            analytics_interval=analytics_interval,
            sleep=sleeper,
            now=clock,
        )
        if outcome["waited"]:
            hit_minute = True
        if outcome["http"]:
            calls += 1
        elif outcome["outcome"] == "ok" and _chunk_status(conn, chunk["id"]) == "pending":
            _set_status(conn, chunk["id"], "failed", error="Кусок очереди не продвинулся")
        elif outcome["outcome"] == "error" and not outcome["http"]:
            if _chunk_status(conn, chunk["id"]) == "pending":
                _set_status(conn, chunk["id"], "failed", error="Запрос к API не выполнен")
        if outcome["outcome"] == "rate":
            stopped = "rate_limit"
            break
        if outcome["outcome"] == "daily":
            hit_daily = True
            stopped = "daily_limit"
            break
        if calls >= max_calls and next_pending(
            conn, datasets=datasets, overlap=overlap, marketplaces=marketplaces
        ) is not None:
            stopped = "batch_limit"
            break
    else:
        if next_pending(conn, datasets=datasets, overlap=overlap, marketplaces=marketplaces) is not None:
            stopped = "batch_limit"
    conn.commit()
    return _receipt(conn, calls_made=calls, stopped=stopped, hit_minute=hit_minute, hit_daily=hit_daily)


def sync_until_idle(
    conn,
    client,
    *,
    wb_client=None,
    today: date | None = None,
    horizon_days: int = HISTORY_HORIZON_DAYS,
    max_calls: int | None = None,
    marketplaces: tuple[str, ...] = ("ozon",),
    page_limit: int = ANALYTICS_PAGE_LIMIT,
    analytics_interval: float = ANALYTICS_MIN_INTERVAL_SEC,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> dict:
    """Дожимает очередь. Открытые периоды обновляются один раз за запуск, без бесконечного цикла."""
    current = today or date.today()
    if "ozon" in marketplaces:
        ensure_queue(conn, current, horizon_days)
    if "wb" in marketplaces:
        from ozon_seller_mcp.wb_sync import ensure_wb_queue

        ensure_wb_queue(conn, current, horizon_days)
    mark_due(conn, current, refresh_open=True)
    total_calls = 0
    hit_minute = False
    hit_daily = False
    last = _receipt(conn, calls_made=0, stopped="queue_empty", hit_minute=False, hit_daily=False)
    while True:
        if max_calls is not None and total_calls >= max_calls:
            last["stopped_reason"] = "call_budget"
            last["calls_made"] = total_calls
            break
        room = SYNC_BATCH_API_CALLS if max_calls is None else min(SYNC_BATCH_API_CALLS, max_calls - total_calls)
        last = sync_batch(
            conn,
            client,
            wb_client=wb_client,
            today=current,
            max_calls=room,
            refresh_open=False,
            ensure=False,
            marketplaces=marketplaces,
            page_limit=page_limit,
            analytics_interval=analytics_interval,
            sleep=sleep,
            now=now,
        )
        batch_calls = int(last["calls_made"])
        total_calls += batch_calls
        hit_minute = hit_minute or bool(last["hit_minute_limit"])
        hit_daily = hit_daily or bool(last["hit_daily_limit"])
        last["calls_made"] = total_calls
        last["hit_minute_limit"] = hit_minute
        last["hit_daily_limit"] = hit_daily
        if last["stopped_reason"] != "batch_limit" or batch_calls == 0:
            break
    return last


def _stored_horizon(conn) -> int:
    raw = get_meta(conn, "history_horizon_days")
    if not raw:
        return HISTORY_HORIZON_DAYS
    return int(raw)


def _run_chunk(
    conn,
    client,
    chunk,
    *,
    wb_client=None,
    today: date,
    page_limit: int,
    analytics_interval: float,
    sleep,
    now,
) -> dict:
    waited = False
    if chunk["dataset"] in WB_DATASETS:
        if wb_client is None:
            return {"outcome": "error", "http": False, "waited": False}
        from ozon_seller_mcp.wb_sync import run_wb_chunk

        return run_wb_chunk(
            conn,
            wb_client,
            chunk,
            today=today,
            now=now,
            stats_interval=analytics_interval,
            sleep=sleep,
        )
    try:
        if client is None:
            return {"outcome": "error", "http": False, "waited": False}
        if chunk["dataset"] == "sales":
            return _run_sales(
                conn,
                client,
                chunk,
                today=today,
                page_limit=page_limit,
                analytics_interval=analytics_interval,
                sleep=sleep,
                now=now,
            )
        if chunk["dataset"] == "products":
            return _run_products(conn, client, chunk, page_limit=page_limit)
        if chunk["dataset"] in LEGACY_FINANCE:
            _set_status(
                conn,
                chunk["id"],
                "unavailable",
                error=LEGACY_FINANCE_ERROR,
                synced_at=iso_now(),
            )
            return {"outcome": "ok", "http": False, "waited": False}
        if chunk["dataset"] == "finance_accrual_types":
            return _run_accrual_types(conn, client, chunk)
        if chunk["dataset"] == "finance_accrual":
            return _run_accrual(conn, client, chunk, today=today, now=now)
        if chunk["dataset"] == "prices":
            return _run_prices(conn, client, chunk)
        if chunk["dataset"] == "returns":
            return _run_returns(conn, client, chunk, today=today)
        if chunk["dataset"] == "supply_orders":
            return _run_supply(conn, client, chunk)
        if chunk["dataset"] == "stocks_turnover":
            payload = client.post(
                "/v1/analytics/turnover/stocks",
                {"limit": page_limit, "offset": chunk["offset_value"]},
            )
            _save_stocks(conn, payload, sku_total=True, fetched_at=iso_now())
            row_count = len(parse_stock_items(payload, sku_total=True))
            if row_count >= page_limit:
                _insert_chunk(
                    conn,
                    "stocks_turnover",
                    date.fromisoformat(chunk["period_start"]),
                    date.fromisoformat(chunk["period_end"]),
                    offset_value=chunk["offset_value"] + page_limit,
                )
            else:
                _enqueue_warehouse_batches(conn, date.fromisoformat(chunk["period_start"]))
            _set_status(conn, chunk["id"], "done", row_count=row_count, synced_at=iso_now(), error="")
            return {"outcome": "ok", "http": True, "waited": False}
        if chunk["dataset"] == "stocks_warehouse":
            skus = json.loads(chunk["cursor"] or "[]")
            payload = client.post("/v1/analytics/stocks", {"skus": skus})
            _save_stocks(conn, payload, sku_total=False, fetched_at=iso_now())
            _set_status(
                conn,
                chunk["id"],
                "done",
                row_count=len(parse_stock_items(payload, sku_total=False)),
                synced_at=iso_now(),
                error="",
            )
            return {"outcome": "ok", "http": True, "waited": False}
    except OzonApiError as exc:
        return _fail_chunk(conn, chunk, exc, waited)
    _set_status(conn, chunk["id"], "failed", error=f"Неизвестный набор данных: {chunk['dataset']}")
    return {"outcome": "error", "http": False, "waited": False}


def _fail_chunk(conn, chunk, exc: OzonApiError, waited: bool) -> dict:
    kind = classify_api_error(exc.status, exc.message)
    logger.warning("Ozon %s %s: %s", chunk["dataset"], kind, exc.message)
    if kind == "history" and chunk["dataset"] == "sales":
        _mark_history_unavailable(conn, chunk, exc.message)
        return {"outcome": "history", "http": True, "waited": waited}
    if kind == "daily":
        return {"outcome": "daily", "http": True, "waited": waited}
    if kind == "rate":
        return {"outcome": "rate", "http": True, "waited": waited}
    _set_status(conn, chunk["id"], "failed", error=exc.message[:500], synced_at=iso_now())
    return {"outcome": "error", "http": True, "waited": waited}


def _run_sales(conn, client, chunk, *, today: date, page_limit: int, analytics_interval: float, sleep, now) -> dict:
    waited = _wait_analytics(conn, interval=analytics_interval, sleep=sleep, now=now)
    metrics = BASIC_METRICS if get_meta(conn, "analytics_metrics") == "basic" else EXTENDED_METRICS
    body = analytics_request(chunk["period_start"], chunk["period_end"], chunk["offset_value"], page_limit, metrics)
    try:
        payload = client.post("/v1/analytics/data", body)
    except OzonApiError as exc:
        if metrics != BASIC_METRICS and is_metric_rejected(exc.status, exc.message):
            set_meta(conn, "analytics_metrics", "basic")
            return {"outcome": "ok", "http": True, "waited": waited}
        return _fail_chunk(conn, chunk, exc, waited)
    fetched_at = iso_now()
    _save_sales(conn, payload, metrics=metrics, fetched_at=fetched_at)
    row_count = len(parse_sales_rows(payload, metrics))
    if row_count >= page_limit:
        _insert_chunk(
            conn,
            "sales",
            date.fromisoformat(chunk["period_start"]),
            date.fromisoformat(chunk["period_end"]),
            offset_value=chunk["offset_value"] + page_limit,
        )
    _finish(conn, chunk, today, row_count)
    return {"outcome": "ok", "http": True, "waited": waited}


def _accrual_last_id(cursor: str | None, now) -> str:
    if not cursor or "\n" not in cursor:
        return ""
    last_id, raw_ts = cursor.split("\n", 1)
    try:
        stored = float(raw_ts)
    except ValueError:
        return ""
    if now() - stored > ACCRUAL_CURSOR_TTL_SEC:
        return ""
    return last_id


def _run_accrual_types(conn, client, chunk) -> dict:
    payload = client.post("/v1/finance/accrual/types", {})
    fetched_at = iso_now()
    rows = parse_accrual_types(payload)
    for row in rows:
        upsert_accrual_type(conn, fetched_at=fetched_at, **row)
    _set_status(conn, chunk["id"], "done", row_count=len(rows), synced_at=fetched_at, error="", cursor="")
    return {"outcome": "ok", "http": True, "waited": False}


def _run_accrual(conn, client, chunk, *, today: date, now) -> dict:
    last_id = _accrual_last_id(chunk["cursor"], now)
    payload = client.post(
        "/v1/finance/accrual/by-day",
        {"date": chunk["period_start"], "last_id": last_id, "limit": ACCRUAL_PAGE_LIMIT},
    )
    rows, new_last_id = parse_accrual_page(payload, day=chunk["period_start"])
    fetched_at = iso_now()
    for row in rows:
        upsert_accrual(conn, fetched_at=fetched_at, **row)
    if rows and new_last_id and new_last_id != last_id:
        _set_status(
            conn,
            chunk["id"],
            "pending",
            cursor=f"{new_last_id}\n{now()}",
            row_count=len(rows),
            error="",
        )
        return {"outcome": "ok", "http": True, "waited": False}
    _finish(conn, chunk, today, len(rows))
    return {"outcome": "ok", "http": True, "waited": False}


def _run_prices(conn, client, chunk) -> dict:
    requested = chunk["cursor"] or ""
    payload = client.post(
        "/v5/product/info/prices",
        {"cursor": requested, "filter": {"visibility": "ALL"}, "limit": PRICE_PAGE_LIMIT},
    )
    rows, new_cursor = parse_prices(payload)
    fetched_at = iso_now()
    for row in rows:
        upsert_price(conn, fetched_at=fetched_at, **row)
    if rows and new_cursor and new_cursor != requested:
        _set_status(conn, chunk["id"], "pending", cursor=new_cursor, row_count=len(rows), error="")
    else:
        _set_status(conn, chunk["id"], "done", row_count=len(rows), synced_at=fetched_at, error="", cursor="")
    return {"outcome": "ok", "http": True, "waited": False}


def _run_returns(conn, client, chunk, *, today: date) -> dict:
    requested = chunk["cursor"] or ""
    date_from, date_to = utc_bounds(
        date.fromisoformat(chunk["period_start"]),
        date.fromisoformat(chunk["period_end"]),
    )
    payload = client.post(
        "/v1/returns/list",
        {
            "filter": {"logistic_return_date": {"time_from": date_from, "time_to": date_to}},
            "limit": 100,
            "last_id": int(requested) if requested.isdigit() else (requested or 0),
        },
    )
    rows, new_last_id = parse_returns(payload)
    fetched_at = iso_now()
    for row in rows:
        upsert_return(conn, fetched_at=fetched_at, **row)
    if rows and new_last_id and new_last_id != requested:
        _set_status(conn, chunk["id"], "pending", cursor=new_last_id, row_count=len(rows), error="")
    else:
        _finish(conn, chunk, today, len(rows))
    return {"outcome": "ok", "http": True, "waited": False}


SUPPLY_STATES = [
    "DATA_FILLING",
    "READY_TO_SUPPLY",
    "ACCEPTED_AT_SUPPLY_WAREHOUSE",
    "IN_TRANSIT",
    "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
    "REPORTS_CONFIRMATION_AWAITING",
    "REPORT_REJECTED",
    "COMPLETED",
    "REJECTED_AT_SUPPLY_WAREHOUSE",
    "CANCELLED",
    "OVERDUE",
]


def _supply_cursor(raw: str | None) -> dict:
    if not raw:
        return {"phase": "list", "last_id": "", "order_ids": [], "bundles": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"phase": "list", "last_id": "", "order_ids": [], "bundles": []}
    if not isinstance(data, dict):
        return {"phase": "list", "last_id": "", "order_ids": [], "bundles": []}
    data.setdefault("phase", "list")
    data.setdefault("last_id", "")
    data.setdefault("order_ids", [])
    data.setdefault("bundles", [])
    return data


def _run_supply(conn, client, chunk) -> dict:
    state = _supply_cursor(chunk["cursor"])
    fetched_at = iso_now()
    if state["phase"] == "list":
        payload = client.post(
            "/v2/supply-order/list",
            {
                "filter": {"states": SUPPLY_STATES},
                "last_id": state["last_id"],
                "limit": SUPPLY_LIST_LIMIT,
                "sort_by": "ORDER_CREATION",
                "sort_dir": "DESC",
            },
        )
        order_ids, new_last_id = parse_supply_list(payload)
        for order_id in order_ids:
            if order_id not in state["order_ids"]:
                state["order_ids"].append(order_id)
            existing = conn.execute(
                "SELECT 1 FROM supply_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            if existing is None:
                upsert_supply_order(
                    conn,
                    order_id=order_id,
                    state=None,
                    inbound=0,
                    payload=json.dumps({"order_id": order_id}, ensure_ascii=False),
                    fetched_at=fetched_at,
                )
        if new_last_id and new_last_id != state["last_id"]:
            state["last_id"] = new_last_id
            _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state), error="")
            return {"outcome": "ok", "http": True, "waited": False}
        state["phase"] = "get"
        state["last_id"] = ""
        if not state["order_ids"]:
            _set_status(conn, chunk["id"], "done", row_count=0, synced_at=fetched_at, error="", cursor="")
            return {"outcome": "ok", "http": True, "waited": False}
        _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state), error="")
        return {"outcome": "ok", "http": True, "waited": False}
    if state["phase"] == "get":
        batch = [str(order_id) for order_id in state["order_ids"][:SUPPLY_GET_LIMIT]]
        payload = client.post("/v2/supply-order/get", {"order_ids": batch})
        orders = parse_supply_orders(payload)
        for order in orders:
            upsert_supply_order(
                conn,
                order_id=order["order_id"],
                state=order["state"],
                inbound=order["inbound"],
                payload=order["payload"],
                fetched_at=fetched_at,
            )
            for bundle_id in order["bundle_ids"]:
                state["bundles"].append(
                    {"bundle_id": bundle_id, "order_id": order["order_id"], "last_id": ""}
                )
        state["order_ids"] = [order_id for order_id in state["order_ids"] if str(order_id) not in set(batch)]
        if not state["order_ids"]:
            state["phase"] = "bundle"
        if state["phase"] == "bundle" and not state["bundles"]:
            _set_status(conn, chunk["id"], "done", row_count=len(orders), synced_at=fetched_at, error="", cursor="")
            return {"outcome": "ok", "http": True, "waited": False}
        _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state), error="")
        return {"outcome": "ok", "http": True, "waited": False}
    if not state["bundles"]:
        _set_status(conn, chunk["id"], "done", row_count=0, synced_at=fetched_at, error="", cursor="")
        return {"outcome": "ok", "http": False, "waited": False}
    current = state["bundles"][0]
    payload = client.post(
        "/v1/supply-order/bundle",
        {
            "bundle_ids": [current["bundle_id"]],
            "limit": 100,
            "last_id": current.get("last_id") or "",
        },
    )
    rows, new_last_id = parse_bundle_items(payload)
    for row in rows:
        upsert_supply_bundle_item(
            conn,
            order_id=str(current["order_id"]),
            sku=row["sku"],
            quantity=row["quantity"],
            payload=row["payload"],
            fetched_at=fetched_at,
        )
    if rows and new_last_id and new_last_id != (current.get("last_id") or ""):
        current["last_id"] = new_last_id
    else:
        state["bundles"].pop(0)
    if state["bundles"]:
        _set_status(conn, chunk["id"], "pending", cursor=json.dumps(state), error="")
    else:
        _set_status(conn, chunk["id"], "done", row_count=len(rows), synced_at=fetched_at, error="", cursor="")
    return {"outcome": "ok", "http": True, "waited": False}


def _run_products(conn, client, chunk, *, page_limit: int) -> dict:
    cursor = chunk["cursor"] or "list:"
    if cursor.startswith("list:"):
        last_id = cursor.split(":", 1)[1]
        payload = client.post(
            "/v3/product/list",
            {"filter": {"visibility": "ALL"}, "last_id": last_id, "limit": page_limit},
        )
        items, new_last_id = parse_product_list(payload)
        fetched_at = iso_now()
        for item in items:
            remember_listing(conn, product_id=item["product_id"], offer_id=item["offer_id"], fetched_at=fetched_at)
        if len(items) >= page_limit and new_last_id and new_last_id != last_id:
            _set_status(conn, chunk["id"], "pending", cursor=f"list:{new_last_id}", row_count=len(items))
        else:
            _set_status(conn, chunk["id"], "pending", cursor="info:", row_count=len(items))
        return {"outcome": "ok", "http": True, "waited": False}

    rows = conn.execute(
        """
        SELECT product_id FROM products
        WHERE sku LIKE 'pending:%' AND product_id IS NOT NULL
        ORDER BY product_id
        LIMIT ?
        """,
        (page_limit,),
    ).fetchall()
    if not rows:
        _set_status(conn, chunk["id"], "done", synced_at=iso_now(), row_count=0, error="")
        return {"outcome": "ok", "http": False, "waited": False}
    product_ids = [int(row["product_id"]) for row in rows]
    payload = client.post("/v3/product/info/list", {"product_id": product_ids})
    fetched_at = iso_now()
    parsed = parse_product_info(payload)
    seen: set[int] = set()
    for item in parsed:
        if item["product_id"] is None:
            continue
        seen.add(int(item["product_id"]))
        sku = item["sku"] or f"unknown:{item['product_id']}"
        upsert_product(
            conn,
            sku=sku,
            offer_id=item["offer_id"],
            product_id=item["product_id"],
            name=item["name"],
            fetched_at=fetched_at,
        )
        for barcode in item.get("barcodes") or []:
            upsert_barcode_catalog(
                conn,
                barcode=barcode,
                ozon_sku=sku,
                vendor_code=item.get("offer_id"),
                name=item.get("name"),
                fetched_at=fetched_at,
            )
    for product_id in product_ids:
        if product_id in seen:
            continue
        upsert_product(
            conn,
            sku=f"unknown:{product_id}",
            offer_id=None,
            product_id=product_id,
            name=None,
            fetched_at=fetched_at,
        )
    return {"outcome": "ok", "http": True, "waited": False}


def _finish(conn, chunk, today: date, row_count: int) -> None:
    if chunk["dataset"] in ("sales", "finance_accrual", "returns", "wb_sales", "wb_finance"):
        state = chunk_sales_state(
            date.fromisoformat(chunk["period_start"]),
            date.fromisoformat(chunk["period_end"]),
            today,
        )
    else:
        state = "open"
    _set_status(
        conn,
        chunk["id"],
        status_after_success(state),
        row_count=row_count,
        synced_at=iso_now(),
        error="",
        cursor="",
    )


def _save_sales(conn, payload: dict, *, metrics: list[str], fetched_at: str) -> None:
    for row in parse_sales_rows(payload, metrics, with_payload=True):
        upsert_sale(
            conn,
            sku=row["sku"],
            day=row["date"],
            revenue=row["revenue"],
            ordered_units=row["ordered_units"],
            delivered_units=row.get("delivered_units"),
            returns_units=row.get("returns_units"),
            cancellations=row.get("cancellations"),
            payload=row.get("payload"),
            fetched_at=fetched_at,
        )
        if row["name"]:
            upsert_product(
                conn,
                sku=row["sku"],
                offer_id=None,
                product_id=None,
                name=row["name"],
                fetched_at=fetched_at,
            )


def _save_stocks(conn, payload: dict, *, sku_total: bool, fetched_at: str) -> None:
    for row in parse_stock_items(payload, sku_total=sku_total):
        insert_stock_snapshot(conn, fetched_at=fetched_at, **{k: row[k] for k in (
            "sku", "warehouse_id", "warehouse_name", "present", "reserved", "ozon_idc", "turnover_grade"
        )})
        if row.get("name") or row.get("offer_id"):
            upsert_product(
                conn,
                sku=row["sku"],
                offer_id=row.get("offer_id"),
                product_id=None,
                name=row.get("name"),
                fetched_at=fetched_at,
            )


def _enqueue_warehouse_batches(conn, today: date) -> None:
    rows = conn.execute(
        """
        SELECT sku FROM products
        WHERE sku NOT LIKE 'pending:%' AND sku NOT LIKE 'unknown:%'
        ORDER BY sku
        """
    ).fetchall()
    skus = [str(row["sku"]) for row in rows]
    if not skus:
        set_meta(conn, "stocks_note", "Нет SKU для разреза по складам. Остался общий срез оборачиваемости.")
        return
    for index in range(0, len(skus), WAREHOUSE_BATCH_SIZE):
        batch = skus[index:index + WAREHOUSE_BATCH_SIZE]
        _insert_chunk(
            conn,
            "stocks_warehouse",
            today,
            today,
            offset_value=index,
            cursor=json.dumps(batch, ensure_ascii=False),
        )


def _mark_history_unavailable(conn, chunk, message: str) -> None:
    note = message[:500]
    conn.execute(
        """
        UPDATE sync_chunks
        SET status = 'unavailable', error = ?, synced_at = ?
        WHERE dataset = 'sales'
          AND (
            id = ?
            OR (period_end < ? AND status IN ('pending', 'failed'))
          )
        """,
        (note, iso_now(), chunk["id"], chunk["period_start"]),
    )
    set_meta(conn, "analytics_earliest_available", chunk["period_start"])


def _wait_analytics(conn, *, interval: float, sleep, now) -> bool:
    raw = get_meta(conn, "analytics_last_request_at")
    waited = False
    current = now()
    if raw and interval > 0:
        elapsed = current - float(raw)
        if elapsed < interval:
            sleep(interval - elapsed)
            waited = True
            current = now()
    set_meta(conn, "analytics_last_request_at", str(current))
    conn.commit()
    return waited


def _receipt(conn, *, calls_made: int, stopped: str, hit_minute: bool, hit_daily: bool) -> dict:
    counts = {name: 0 for name in ("pending", "done", "frozen", "unavailable", "failed")}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM sync_chunks GROUP BY status"):
        counts[row["status"]] = row["n"]
    return {
        "calls_made": calls_made,
        "chunks_pending": counts["pending"],
        "chunks_done": counts["done"],
        "chunks_frozen": counts["frozen"],
        "chunks_unavailable": counts["unavailable"],
        "chunks_failed": counts["failed"],
        "data_as_of": data_as_of(conn),
        "hit_minute_limit": hit_minute,
        "hit_daily_limit": hit_daily,
        "stopped_reason": stopped,
        "horizon_start": _horizon_label(conn),
    }


def _horizon_label(conn) -> str | None:
    raw = get_meta(conn, "history_horizon_days")
    if not raw:
        return None
    start = horizon_start(date.today(), int(raw))
    return start.isoformat()


def sync_for_read(
    conn,
    client,
    *,
    wb_client=None,
    today: date,
    datasets: tuple[str, ...],
    overlap: tuple[str, str],
    max_calls: int = SYNC_BATCH_API_CALLS,
    marketplaces: tuple[str, ...] | None = None,
    page_limit: int = ANALYTICS_PAGE_LIMIT,
    analytics_interval: float = ANALYTICS_MIN_INTERVAL_SEC,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> dict | None:
    """Догружает только куски, которые пересекают запрошенный период. Год целиком не качает."""
    mps = marketplaces or _marketplaces_for_datasets(datasets)
    horizon = _stored_horizon(conn)
    if "ozon" in mps:
        ensure_queue(conn, today, horizon)
    if "wb" in mps:
        from ozon_seller_mcp.wb_sync import ensure_wb_queue

        ensure_wb_queue(conn, today, horizon)
    mark_due(conn, today, refresh_open=False, overlap=overlap)
    if next_pending(conn, datasets=datasets, overlap=overlap, marketplaces=mps) is None:
        return None
    return sync_batch(
        conn,
        client,
        wb_client=wb_client,
        today=today,
        max_calls=max_calls,
        refresh_open=False,
        ensure=False,
        datasets=datasets,
        overlap=overlap,
        marketplaces=mps,
        page_limit=page_limit,
        analytics_interval=analytics_interval,
        sleep=sleep,
        now=now,
    )


def _marketplaces_for_datasets(datasets: tuple[str, ...]) -> tuple[str, ...]:
    has_wb = any(name in WB_DATASETS for name in datasets)
    has_ozon = any(name not in WB_DATASETS for name in datasets)
    result: list[str] = []
    if has_ozon:
        result.append("ozon")
    if has_wb:
        result.append("wb")
    return tuple(result) or ("ozon",)


def chunk_counts(conn) -> dict[str, int]:
    return _receipt(conn, calls_made=0, stopped="queue_empty", hit_minute=False, hit_daily=False)
