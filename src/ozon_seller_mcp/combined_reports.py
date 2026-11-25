"""Сводки Ozon + WB по баркоду."""

from __future__ import annotations

from datetime import date, timedelta

from ozon_seller_mcp.db import data_as_of
from ozon_seller_mcp.forecast import apply_cogs, cost_coverage, days_of_cover, order_quantity, stockout_on
from ozon_seller_mcp.policy import WB_SALES_HISTORY_DAYS
from ozon_seller_mcp.reports import (
    EMPTY_FINANCE,
    STOCK_ROW_LIMIT,
    _accrual_profit,
    _latest_stock,
    _range_pending,
    _units_by_sku,
)
from ozon_seller_mcp.sync import next_pending
from ozon_seller_mcp.wb_reports import (
    _costs_by_barcode,
    _wb_finance_profit,
    _wb_inbound_by_barcode,
    _wb_latest_stock,
    _wb_units_by_barcode,
)


def combined_sales_volume_report(conn, date_from: date, date_to: date, group_by: str) -> dict:
    if group_by not in ("day", "week", "month", "barcode"):
        raise ValueError("group_by должен быть day, week, month или barcode")
    if group_by == "barcode":
        return _combined_sales_by_barcode(conn, date_from, date_to)
    raise ValueError("Для combined пока поддерживается только group_by=barcode")


def _combined_sales_by_barcode(conn, date_from: date, date_to: date) -> dict:
    params = (date_from.isoformat(), date_to.isoformat())
    ozon_by_sku = {
        str(row["sku"]): {"revenue": float(row["revenue"]), "units": float(row["units"])}
        for row in conn.execute(
            """
            SELECT sku, SUM(revenue) AS revenue, SUM(ordered_units) AS units
            FROM sales_daily
            WHERE date >= ? AND date <= ?
            GROUP BY sku
            """,
            params,
        )
    }
    wb_by_barcode = {
        str(row["barcode"]): {"revenue": float(row["revenue"]), "units": float(row["units"])}
        for row in conn.execute(
            """
            SELECT barcode, SUM(revenue) AS revenue, SUM(ordered_units) AS units
            FROM wb_sales_daily
            WHERE date >= ? AND date <= ?
            GROUP BY barcode
            """,
            params,
        )
    }
    catalog = list(conn.execute("SELECT barcode, ozon_sku, name FROM barcode_catalog"))
    rows_map: dict[str, dict] = {}
    for row in catalog:
        barcode = str(row["barcode"])
        ozon_sku = row["ozon_sku"]
        oz = ozon_by_sku.get(str(ozon_sku), {"revenue": 0.0, "units": 0.0}) if ozon_sku else {"revenue": 0.0, "units": 0.0}
        wb = wb_by_barcode.get(barcode, {"revenue": 0.0, "units": 0.0})
        rows_map[barcode] = {
            "barcode": barcode,
            "name": row["name"],
            "ozon_sku": ozon_sku,
            "ozon_revenue": oz["revenue"],
            "ozon_ordered_units": oz["units"],
            "wb_revenue": wb["revenue"],
            "wb_ordered_units": wb["units"],
            "total_revenue": oz["revenue"] + wb["revenue"],
            "total_ordered_units": oz["units"] + wb["units"],
            "barcode_missing": False,
        }
    for sku, oz in ozon_by_sku.items():
        if any(row["ozon_sku"] == sku for row in catalog):
            continue
        key = f"ozon-only:{sku}"
        rows_map[key] = {
            "barcode": None,
            "name": None,
            "ozon_sku": sku,
            "ozon_revenue": oz["revenue"],
            "ozon_ordered_units": oz["units"],
            "wb_revenue": 0.0,
            "wb_ordered_units": 0.0,
            "total_revenue": oz["revenue"],
            "total_ordered_units": oz["units"],
            "barcode_missing": True,
        }
    for barcode, wb in wb_by_barcode.items():
        if barcode in rows_map:
            continue
        rows_map[barcode] = {
            "barcode": barcode,
            "name": None,
            "ozon_sku": None,
            "ozon_revenue": 0.0,
            "ozon_ordered_units": 0.0,
            "wb_revenue": wb["revenue"],
            "wb_ordered_units": wb["units"],
            "total_revenue": wb["revenue"],
            "total_ordered_units": wb["units"],
            "barcode_missing": False,
        }
    rows = sorted(rows_map.values(), key=lambda row: row["total_revenue"], reverse=True)[:20]
    totals = {
        "ozon_revenue": sum(row["ozon_revenue"] for row in rows_map.values()),
        "wb_revenue": sum(row["wb_revenue"] for row in rows_map.values()),
        "total_revenue": sum(row["total_revenue"] for row in rows_map.values()),
    }
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "group_by": "barcode",
        "totals": totals,
        "rows": rows,
        "wb_sales_limited_to_days": WB_SALES_HISTORY_DAYS,
        "ozon_pending": _range_pending(conn, ("sales",), date_from, date_to),
        "wb_pending": _range_pending(conn, ("wb_sales",), date_from, date_to),
        "data_as_of": data_as_of(conn),
    }


def combined_stock_forecast_report(conn, today: date, *, lead_time_days: int, safety_days: int) -> dict:
    start_28 = today - timedelta(days=27)
    ozon_units = _units_by_sku(conn, start_28, today)
    wb_units = _wb_units_by_barcode(conn, start_28, today)
    ozon_stock = _latest_stock(conn)
    wb_stock = _wb_latest_stock(conn)
    ozon_inbound = _ozon_inbound_by_sku(conn)
    wb_inbound = _wb_inbound_by_barcode(conn)
    catalog = {str(row["barcode"]): str(row["ozon_sku"]) for row in conn.execute(
        "SELECT barcode, ozon_sku FROM barcode_catalog WHERE ozon_sku IS NOT NULL"
    )}
    barcodes = sorted(set(catalog) | set(wb_units) | set(wb_stock) | set(wb_inbound))
    built = []
    for barcode in barcodes:
        sku = catalog.get(barcode)
        oz_present = 0
        if sku and sku in ozon_stock:
            oz_present = int(ozon_stock[sku]["present"])
        wb_present = int(wb_stock.get(barcode, 0))
        present = oz_present + wb_present
        oz_u28 = float(ozon_units.get(sku or "", 0))
        wb_u28 = float(wb_units.get(barcode, 0))
        units_28 = oz_u28 + wb_u28
        avg28 = units_28 / 28
        inbound = float(ozon_inbound.get(sku or "", 0)) + float(wb_inbound.get(barcode, 0))
        cover = days_of_cover(present, avg28)
        built.append(
            {
                "barcode": barcode,
                "ozon_sku": sku,
                "ozon_present": oz_present,
                "wb_present": wb_present,
                "present_total": present,
                "inbound_qty": inbound,
                "units_28_total": units_28,
                "order_qty": order_quantity(avg28, present, lead_time_days, safety_days, inbound),
                "days_cover": None if cover is None else round(cover, 1),
                "stockout_date": stockout_on(today, cover),
            }
        )
    built.sort(key=lambda row: (row["days_cover"] is None, row["days_cover"] if row["days_cover"] is not None else 0))
    return {
        "as_of": today.isoformat(),
        "lead_time_days": lead_time_days,
        "safety_days": safety_days,
        "rows": built[:STOCK_ROW_LIMIT],
        "ozon_pending": _range_pending(conn, ("sales", "supply_orders"), start_28, today),
        "wb_pending": _range_pending(conn, ("wb_sales", "wb_supplies"), start_28, today),
        "data_as_of": data_as_of(conn),
    }


def combined_profit_summary_report(conn, date_from: date, date_to: date) -> dict:
    oz_components, oz_net, oz_empty = _accrual_profit(conn, date_from, date_to)
    wb_components, wb_net, wb_empty = _wb_finance_profit(conn, date_from, date_to)
    units = _combined_units(conn, date_from, date_to)
    costs = _costs_by_barcode(conn)
    coverage = cost_coverage(units, costs)
    net = oz_net + wb_net
    note = coverage["note"]
    if oz_empty or wb_empty:
        note = f"Ozon finance_empty={oz_empty}, WB finance_empty={wb_empty}. {note}"
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "ozon_marketplace_profit": oz_net,
        "wb_marketplace_profit": wb_net,
        "marketplace_profit": net,
        "profit": apply_cogs(net, coverage),
        "ozon_components": oz_components,
        "wb_components": wb_components,
        "unit_cost_coverage": coverage["unit_cost_coverage"],
        "cogs_known": coverage["cogs_known"],
        "includes_cogs": coverage["includes_cogs"],
        "before_ads": True,
        "note": note,
        "ozon_pending": _range_pending(conn, ("finance_accrual",), date_from, date_to),
        "wb_pending": _range_pending(conn, ("wb_finance",), date_from, date_to),
        "data_as_of": data_as_of(conn),
    }


def _combined_units(conn, date_from: date, date_to: date) -> dict[str, float]:
    units = _wb_units_by_barcode(conn, date_from, date_to)
    sku_units = _units_by_sku(conn, date_from, date_to)
    for row in conn.execute("SELECT barcode, ozon_sku FROM barcode_catalog WHERE ozon_sku IS NOT NULL"):
        barcode = str(row["barcode"])
        sku = str(row["ozon_sku"])
        units[barcode] = units.get(barcode, 0.0) + float(sku_units.get(sku, 0.0))
    return units


def _ozon_inbound_by_sku(conn) -> dict[str, float]:
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
