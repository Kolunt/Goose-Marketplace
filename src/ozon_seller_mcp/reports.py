"""Сводки из SQLite. В ответ попадают итоги, а не страницы API."""

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
from ozon_seller_mcp.sync import next_pending

ROW_LIMIT = 20
STOCK_ROW_LIMIT = 50
EMPTY_FINANCE = "Финансовая база пуста: начисления за период ещё не скачаны."


def sales_volume_report(conn, date_from: date, date_to: date, group_by: str) -> dict:
    if group_by not in ("day", "week", "month", "sku"):
        raise ValueError("group_by должен быть day, week, month или sku")
    key_sql = {
        "day": "sales_daily.date",
        "week": "strftime('%Y-%W', sales_daily.date)",
        "month": "substr(sales_daily.date, 1, 7)",
        "sku": "sales_daily.sku",
    }[group_by]
    name_sql = "MAX(products.name)" if group_by == "sku" else "NULL"
    join = "LEFT JOIN products ON products.sku = sales_daily.sku" if group_by == "sku" else ""
    params = (date_from.isoformat(), date_to.isoformat())
    totals = conn.execute(
        """
        SELECT COALESCE(SUM(revenue), 0) AS revenue,
               COALESCE(SUM(ordered_units), 0) AS ordered_units,
               COALESCE(SUM(delivered_units), 0) AS delivered_units,
               COALESCE(SUM(returns_units), 0) AS returns_units,
               COALESCE(SUM(cancellations), 0) AS cancellations
        FROM sales_daily
        WHERE date >= ? AND date <= ?
        """,
        params,
    ).fetchone()
    rows = conn.execute(
        f"""
        SELECT {key_sql} AS key,
               {name_sql} AS name,
               SUM(revenue) AS revenue,
               SUM(ordered_units) AS ordered_units,
               COALESCE(SUM(delivered_units), 0) AS delivered_units,
               COALESCE(SUM(returns_units), 0) AS returns_units,
               COALESCE(SUM(cancellations), 0) AS cancellations
        FROM sales_daily
        {join}
        WHERE sales_daily.date >= ? AND sales_daily.date <= ?
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
        "totals": _sales_metrics(totals),
        "rows": [
            {
                "key": row["key"],
                "name": row["name"],
                **_sales_metrics(row),
            }
            for row in rows
        ],
        "row_limit": ROW_LIMIT,
        "data_as_of": data_as_of(conn),
        "range_pending": _range_pending(conn, ("sales",), date_from, date_to),
    }


def stock_forecast_report(
    conn,
    today: date,
    *,
    lead_time_days: int,
    safety_days: int,
) -> dict:
    if lead_time_days < 0 or safety_days < 0:
        raise ValueError("Срок поставки и страховой запас не могут быть отрицательными")
    start_28 = today - timedelta(days=27)
    start_7 = today - timedelta(days=6)
    units = {
        row["sku"]: {"units_7": float(row["units_7"]), "units_28": float(row["units_28"])}
        for row in conn.execute(
            """
            SELECT sku,
                   SUM(CASE WHEN date >= ? THEN ordered_units ELSE 0 END) AS units_7,
                   SUM(ordered_units) AS units_28
            FROM sales_daily
            WHERE date >= ? AND date <= ?
            GROUP BY sku
            """,
            (start_7.isoformat(), start_28.isoformat(), today.isoformat()),
        )
    }
    names = {
        row["sku"]: {"name": row["name"], "offer_id": row["offer_id"]}
        for row in conn.execute("SELECT sku, name, offer_id FROM products")
    }
    collapsed = _latest_stock(conn)
    inbound = _inbound_by_sku(conn)
    returns = _returns_by_sku(conn, today - timedelta(days=13), today)
    skus = sorted(set(collapsed) | set(units) | set(inbound) | set(returns))
    built = []
    for sku in skus:
        stock = collapsed.get(sku, {})
        present = int(stock.get("present") or 0)
        bundle = units.get(sku, {"units_7": 0.0, "units_28": 0.0})
        avg7 = bundle["units_7"] / 7
        avg28 = bundle["units_28"] / 28
        cover = days_of_cover(present, avg28)
        inbound_qty = float(inbound.get(sku) or 0)
        built.append(
            {
                "sku": sku,
                "name": (names.get(sku) or {}).get("name"),
                "offer_id": (names.get(sku) or {}).get("offer_id"),
                "present": present,
                "inbound_qty": inbound_qty,
                "returns_count": int(returns.get(sku) or 0),
                "units_7": bundle["units_7"],
                "units_28": bundle["units_28"],
                "avg_daily_7": avg7,
                "avg_daily_28": avg28,
                "days_cover": None if cover is None else round(cover, 1),
                "stockout_date": stockout_on(today, cover),
                "order_qty": order_quantity(avg28, present, lead_time_days, safety_days, inbound_qty),
                "ozon_idc": stock.get("ozon_idc"),
                "turnover_grade": stock.get("turnover_grade"),
            }
        )
    built.sort(key=lambda row: (row["days_cover"] is None, row["days_cover"] if row["days_cover"] is not None else 0))
    urgent = [
        row
        for row in built
        if row["days_cover"] is not None and row["days_cover"] <= lead_time_days + safety_days
    ]
    return {
        "as_of": today.isoformat(),
        "lead_time_days": lead_time_days,
        "safety_days": safety_days,
        "formula": "к заказу = среднедневные продажи за 28 дней × (срок поставки + страховой запас) − остаток − уже едет",
        "supplies_pending": next_pending(conn, datasets=("supply_orders",)) is not None,
        "sku_count": len(built),
        "urgent_count": len(urgent),
        "rows": built[:STOCK_ROW_LIMIT],
        "row_limit": STOCK_ROW_LIMIT,
        "data_as_of": data_as_of(conn),
        "sales_window_pending": _range_pending(conn, ("sales",), start_28, today),
        "stocks_pending": next_pending(conn, datasets=("stocks_turnover", "stocks_warehouse")) is not None,
    }


def profit_summary_report(conn, date_from: date, date_to: date) -> dict:
    totals_row = conn.execute(
        """
        SELECT COALESCE(SUM(revenue), 0) AS revenue,
               COALESCE(SUM(ordered_units), 0) AS ordered_units
        FROM sales_daily
        WHERE date >= ? AND date <= ?
        """,
        (date_from.isoformat(), date_to.isoformat()),
    ).fetchone()
    units = _units_by_sku(conn, date_from, date_to)
    costs = _costs(conn)
    components, net, finance_empty = _accrual_profit(conn, date_from, date_to)
    coverage = cost_coverage(units, costs)
    note = coverage["note"]
    if finance_empty:
        note = f"{EMPTY_FINANCE} {note}"
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "revenue": float(totals_row["revenue"]),
        "ordered_units": float(totals_row["ordered_units"]),
        "components": components,
        "finance_empty": finance_empty,
        "finance_source": "accruals",
        "marketplace_profit": net,
        "profit": apply_cogs(net, coverage),
        "unit_cost_coverage": coverage["unit_cost_coverage"],
        "cogs_known": coverage["cogs_known"],
        "includes_cogs": coverage["includes_cogs"],
        "before_ads": True,
        "commission": _commission_reference(conn),
        "commission_note": (
            "Текущая комиссия — справка, сколько кабинет удержит с новой продажи. "
            "Историческая прибыль считается по начислениям."
        ),
        "note": note,
        "data_as_of": data_as_of(conn),
        "range_pending": _range_pending(
            conn,
            ("sales", "finance_accrual", "finance_accrual_types", "prices"),
            date_from,
            date_to,
        ),
    }


def profit_forecast_report(conn, today: date, horizon_days: int) -> dict:
    if horizon_days <= 0:
        raise ValueError("horizon_days должен быть больше нуля")
    start = today - timedelta(days=27)
    units = _units_by_sku(conn, start, today)
    units_28 = sum(units.values())
    _components, net, finance_empty = _accrual_profit(conn, start, today)
    report = build_profit_forecast(
        units_28=units_28,
        net_28=net,
        units_by_sku=units,
        costs=_costs(conn),
        horizon_days=horizon_days,
        profit_basis="accruals_empty" if finance_empty else "accruals",
    )
    if finance_empty:
        report["note"] = f"{EMPTY_FINANCE} {report['note']}"
    report["finance_empty"] = finance_empty
    report["window_from"] = start.isoformat()
    report["window_to"] = today.isoformat()
    report["data_as_of"] = data_as_of(conn)
    report["range_pending"] = _range_pending(
        conn,
        ("sales", "finance_accrual", "finance_accrual_types"),
        start,
        today,
    )
    return report


def _accrual_profit(conn, date_from: date, date_to: date) -> tuple[dict, float, bool]:
    rows = conn.execute(
        """
        SELECT a.total_amount, a.type_id, t.name
        FROM finance_accruals a
        LEFT JOIN finance_accrual_types t ON t.type_id = a.type_id
        WHERE a.accrual_date >= ? AND a.accrual_date <= ?
        """,
        (date_from.isoformat(), date_to.isoformat()),
    ).fetchall()
    if not rows:
        return {}, 0.0, True
    components: dict[str, float] = {}
    total = 0.0
    for row in rows:
        amount = float(row["total_amount"] or 0)
        total += amount
        label = row["name"] or row["type_id"] or "без типа"
        components[str(label)] = components.get(str(label), 0.0) + amount
    return components, total, False


def _units_by_sku(conn, date_from: date, date_to: date) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT sku, SUM(ordered_units) AS units
        FROM sales_daily
        WHERE date >= ? AND date <= ?
        GROUP BY sku
        """,
        (date_from.isoformat(), date_to.isoformat()),
    )
    return {str(row["sku"]): float(row["units"]) for row in rows}


def _sales_metrics(row) -> dict:
    return {
        "revenue": float(row["revenue"] or 0),
        "ordered_units": float(row["ordered_units"] or 0),
        "delivered_units": float(row["delivered_units"] or 0),
        "returns_units": float(row["returns_units"] or 0),
        "cancellations": float(row["cancellations"] or 0),
    }


def _inbound_by_sku(conn) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT i.sku, SUM(i.quantity) AS qty
        FROM supply_bundle_items i
        JOIN supply_orders o ON o.order_id = i.order_id
        WHERE o.inbound = 1
        GROUP BY i.sku
        """
    )
    return {str(row["sku"]): float(row["qty"] or 0) for row in rows}


def _returns_by_sku(conn, date_from: date, date_to: date) -> dict[str, int]:
    rows = conn.execute(
        """
        SELECT sku, COUNT(*) AS n
        FROM returns
        WHERE sku IS NOT NULL AND return_date >= ? AND return_date <= ?
        GROUP BY sku
        """,
        (date_from.isoformat(), date_to.isoformat()),
    )
    return {str(row["sku"]): int(row["n"]) for row in rows}


def _commission_reference(conn) -> dict:
    rows = list(conn.execute("SELECT sku, sales_percent_fbo, sales_percent_fbs FROM product_prices"))
    with_commission = [
        row
        for row in rows
        if row["sales_percent_fbo"] is not None or row["sales_percent_fbs"] is not None
    ]
    sku_total = conn.execute(
        """
        SELECT COUNT(*) AS n FROM products
        WHERE sku NOT LIKE 'pending:%' AND sku NOT LIKE 'unknown:%'
        """
    ).fetchone()["n"]
    denominator = int(sku_total) or len(rows)
    coverage = (len(with_commission) / denominator) if denominator else None

    def average(key: str) -> float | None:
        values = [float(row[key]) for row in rows if row[key] is not None]
        if not values:
            return None
        return sum(values) / len(values)

    return {
        "priced_sku": len(rows),
        "sku_with_commission": len(with_commission),
        "commission_coverage": coverage,
        "avg_sales_percent_fbo": average("sales_percent_fbo"),
        "avg_sales_percent_fbs": average("sales_percent_fbs"),
    }


def _costs(conn) -> dict[str, float]:
    costs = {
        str(row["barcode"]): float(row["cost"])
        for row in conn.execute("SELECT barcode, cost FROM unit_costs")
    }
    sku_costs: dict[str, float] = {}
    for row in conn.execute("SELECT ozon_sku, barcode FROM barcode_catalog WHERE ozon_sku IS NOT NULL"):
        barcode = str(row["barcode"])
        if barcode in costs:
            sku_costs[str(row["ozon_sku"])] = costs[barcode]
    return sku_costs


def _latest_stock(conn) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT s.sku, s.warehouse_id, s.present, s.ozon_idc, s.turnover_grade
        FROM stock_snapshots s
        JOIN (
            SELECT sku, warehouse_id, MAX(fetched_at) AS fetched_at
            FROM stock_snapshots
            GROUP BY sku, warehouse_id
        ) latest
          ON s.sku = latest.sku
         AND s.warehouse_id = latest.warehouse_id
         AND s.fetched_at = latest.fetched_at
        """
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(str(row["sku"]), []).append(row)
    collapsed: dict[str, dict] = {}
    for sku, items in grouped.items():
        warehouses = [item for item in items if item["warehouse_id"] != "*"]
        chosen = warehouses or items
        idcs = [item["ozon_idc"] for item in chosen if item["ozon_idc"] is not None]
        grades = [item["turnover_grade"] for item in chosen if item["turnover_grade"]]
        collapsed[sku] = {
            "present": sum(int(item["present"]) for item in chosen),
            "ozon_idc": None if not idcs else float(idcs[0] if len(idcs) == 1 else sum(idcs) / len(idcs)),
            "turnover_grade": grades[0] if grades else None,
        }
    return collapsed


def _range_pending(conn, datasets: tuple[str, ...], date_from: date, date_to: date) -> bool:
    return (
        next_pending(
            conn,
            datasets=datasets,
            overlap=(date_from.isoformat(), date_to.isoformat()),
        )
        is not None
    )
