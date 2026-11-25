"""Сводки Wildberries из SQLite."""

from __future__ import annotations

from datetime import date, timedelta

from ozon_seller_mcp.db import data_as_of
from ozon_seller_mcp.forecast import (
    apply_cogs,
    build_profit_forecast,
    cost_coverage,
    days_of_cover,
    order_quantity,
    stockout_on,
)
from ozon_seller_mcp.policy import WB_SALES_HISTORY_DAYS
from ozon_seller_mcp.reports import EMPTY_FINANCE, ROW_LIMIT, STOCK_ROW_LIMIT, _range_pending
from ozon_seller_mcp.sync import next_pending

WB_NOTE = "Скорость продаж WB из supplier/sales — операционные данные, не финансовый отчёт."


def wb_sales_volume_report(conn, date_from: date, date_to: date, group_by: str) -> dict:
    if group_by not in ("day", "week", "month", "barcode"):
        raise ValueError("group_by должен быть day, week, month или barcode")
    key_sql = {
        "day": "date",
        "week": "strftime('%Y-%W', date)",
        "month": "substr(date, 1, 7)",
        "barcode": "barcode",
    }[group_by]
    params = (date_from.isoformat(), date_to.isoformat())
    totals = conn.execute(
        """
        SELECT COALESCE(SUM(revenue), 0) AS revenue,
               COALESCE(SUM(ordered_units), 0) AS ordered_units,
               COALESCE(SUM(return_units), 0) AS return_units
        FROM wb_sales_daily
        WHERE date >= ? AND date <= ?
        """,
        params,
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT {key_sql} AS key,
               SUM(revenue) AS revenue,
               SUM(ordered_units) AS ordered_units,
               SUM(return_units) AS return_units
        FROM wb_sales_daily
        WHERE date >= ? AND date <= ?
        GROUP BY {key_sql}
        ORDER BY revenue DESC
        LIMIT {ROW_LIMIT}
        """,
        params,
    ).fetchall()
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "group_by": group_by,
        "totals": {
            "revenue": float(totals["revenue"]),
            "ordered_units": float(totals["ordered_units"]),
            "return_units": float(totals["return_units"]),
        },
        "rows": [
            {
                "key": row["key"],
                "revenue": float(row["revenue"]),
                "ordered_units": float(row["ordered_units"]),
                "return_units": float(row["return_units"]),
            }
            for row in rows
        ],
        "row_limit": ROW_LIMIT,
        "wb_sales_limited_to_days": WB_SALES_HISTORY_DAYS,
        "note": WB_NOTE,
        "data_as_of": data_as_of(conn),
        "range_pending": _range_pending(conn, ("wb_sales",), date_from, date_to),
    }


def wb_stock_forecast_report(conn, today: date, *, lead_time_days: int, safety_days: int) -> dict:
    start_28 = today - timedelta(days=27)
    start_7 = today - timedelta(days=6)
    units = {
        row["barcode"]: {"units_7": float(row["units_7"]), "units_28": float(row["units_28"])}
        for row in conn.execute(
            """
            SELECT barcode,
                   SUM(CASE WHEN date >= ? THEN ordered_units ELSE 0 END) AS units_7,
                   SUM(ordered_units) AS units_28
            FROM wb_sales_daily
            WHERE date >= ? AND date <= ?
            GROUP BY barcode
            """,
            (start_7.isoformat(), start_28.isoformat(), today.isoformat()),
        )
    }
    inbound = _wb_inbound_by_barcode(conn)
    stocks = _wb_latest_stock(conn)
    barcodes = sorted(set(units) | set(stocks) | set(inbound))
    built = []
    for barcode in barcodes:
        present = int(stocks.get(barcode, 0))
        bundle = units.get(barcode, {"units_7": 0.0, "units_28": 0.0})
        avg28 = bundle["units_28"] / 28
        inbound_qty = float(inbound.get(barcode) or 0)
        cover = days_of_cover(present, avg28)
        built.append(
            {
                "barcode": barcode,
                "present": present,
                "inbound_qty": inbound_qty,
                "units_28": bundle["units_28"],
                "avg_daily_28": avg28,
                "days_cover": None if cover is None else round(cover, 1),
                "stockout_date": stockout_on(today, cover),
                "order_qty": order_quantity(avg28, present, lead_time_days, safety_days, inbound_qty),
            }
        )
    built.sort(key=lambda row: (row["days_cover"] is None, row["days_cover"] if row["days_cover"] is not None else 0))
    return {
        "as_of": today.isoformat(),
        "lead_time_days": lead_time_days,
        "safety_days": safety_days,
        "formula": "к заказу = среднедневные × (срок + запас) − остаток FBW − inbound FBS",
        "supplies_pending": next_pending(conn, datasets=("wb_supplies",)) is not None,
        "rows": built[:STOCK_ROW_LIMIT],
        "note": WB_NOTE,
        "data_as_of": data_as_of(conn),
        "sales_window_pending": _range_pending(conn, ("wb_sales",), start_28, today),
    }


def wb_profit_summary_report(conn, date_from: date, date_to: date) -> dict:
    components, net, finance_empty = _wb_finance_profit(conn, date_from, date_to)
    units = _wb_units_by_barcode(conn, date_from, date_to)
    costs = _costs_by_barcode(conn)
    coverage = cost_coverage(units, costs)
    note = coverage["note"]
    if finance_empty:
        note = f"{EMPTY_FINANCE} {note}"
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "components": components,
        "finance_empty": finance_empty,
        "finance_source": "wb_finance_lines",
        "marketplace_profit": net,
        "profit": apply_cogs(net, coverage),
        "unit_cost_coverage": coverage["unit_cost_coverage"],
        "cogs_known": coverage["cogs_known"],
        "includes_cogs": coverage["includes_cogs"],
        "before_ads": True,
        "note": note,
        "data_as_of": data_as_of(conn),
        "range_pending": _range_pending(conn, ("wb_finance", "wb_sales"), date_from, date_to),
    }


def wb_profit_forecast_report(conn, today: date, horizon_days: int) -> dict:
    start = today - timedelta(days=27)
    units = _wb_units_by_barcode(conn, start, today)
    units_28 = sum(units.values())
    _components, net, finance_empty = _wb_finance_profit(conn, start, today)
    report = build_profit_forecast(
        units_28=units_28,
        net_28=net,
        units_by_sku=units,
        costs=_costs_by_barcode(conn),
        horizon_days=horizon_days,
        profit_basis="wb_finance_empty" if finance_empty else "wb_finance",
    )
    if finance_empty:
        report["note"] = f"{EMPTY_FINANCE} {report['note']}"
    report["finance_empty"] = finance_empty
    report["window_from"] = start.isoformat()
    report["window_to"] = today.isoformat()
    report["data_as_of"] = data_as_of(conn)
    report["range_pending"] = _range_pending(conn, ("wb_finance", "wb_sales"), start, today)
    return report


def _wb_finance_profit(conn, date_from: date, date_to: date) -> tuple[dict, float, bool]:
    rows = conn.execute(
        """
        SELECT for_pay, doc_type FROM wb_finance_lines
        WHERE report_date >= ? AND report_date <= ?
        """,
        (date_from.isoformat(), date_to.isoformat()),
    ).fetchall()
    if not rows:
        return {}, 0.0, True
    components: dict[str, float] = {}
    total = 0.0
    for row in rows:
        amount = float(row["for_pay"] or 0)
        total += amount
        label = row["doc_type"] or "без типа"
        components[str(label)] = components.get(str(label), 0.0) + amount
    return components, total, False


def _wb_units_by_barcode(conn, date_from: date, date_to: date) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT barcode, SUM(ordered_units) AS units
        FROM wb_sales_daily
        WHERE date >= ? AND date <= ?
        GROUP BY barcode
        """,
        (date_from.isoformat(), date_to.isoformat()),
    )
    return {str(row["barcode"]): float(row["units"]) for row in rows}


def _costs_by_barcode(conn) -> dict[str, float]:
    return {
        str(row["barcode"]): float(row["cost"])
        for row in conn.execute("SELECT barcode, cost FROM unit_costs")
    }


def _wb_inbound_by_barcode(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT i.barcode, SUM(i.quantity) AS qty
        FROM wb_supply_items i
        JOIN wb_supplies s ON s.supply_id = i.supply_id
        WHERE s.inbound = 1
        GROUP BY i.barcode
        """
    )
    return {str(row["barcode"]): float(row["qty"] or 0) for row in rows}


def _wb_latest_stock(conn) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT s.barcode, s.present
        FROM wb_stock_snapshots s
        JOIN (
            SELECT barcode, warehouse_id, MAX(fetched_at) AS fetched_at
            FROM wb_stock_snapshots
            GROUP BY barcode, warehouse_id
        ) latest
          ON s.barcode = latest.barcode
         AND s.warehouse_id = latest.warehouse_id
         AND s.fetched_at = latest.fetched_at
        """
    ).fetchall()
    totals: dict[str, int] = {}
    for row in rows:
        totals[str(row["barcode"])] = totals.get(str(row["barcode"]), 0) + int(row["present"])
    return totals
