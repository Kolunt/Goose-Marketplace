"""Разбор ответов Wildberries API."""

from __future__ import annotations

import json
from typing import Any

from ozon_seller_mcp.parse import normalize_day, optional_number


def money(value: Any) -> float:
    parsed = optional_number(value)
    return float(parsed or 0)


def parse_sales_items(payload: Any) -> tuple[list[dict], str]:
    items = payload if isinstance(payload, list) else []
    rows = []
    last_change = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        srid = item.get("srid") or item.get("saleID")
        if srid is None:
            continue
        sale_date = item.get("date") or item.get("lastChangeDate") or ""
        day = normalize_day(str(sale_date)) if sale_date else None
        barcode = item.get("barcode")
        qty = float(item.get("quantity") or 1)
        price = money(item.get("totalPrice") or item.get("priceWithDisc") or item.get("forPay"))
        is_return = 1 if str(item.get("saleID", "")).startswith("R") or item.get("isCancel") else 0
        if item.get("isCancel") is True:
            is_return = 1
        rows.append(
            {
                "srid": str(srid),
                "sale_date": day,
                "barcode": None if barcode is None else str(barcode),
                "quantity": qty,
                "revenue": price,
                "is_return": is_return,
                "payload": json.dumps(item, ensure_ascii=False),
                "nm_id": str(item.get("nmId")) if item.get("nmId") is not None else None,
                "vendor_code": item.get("supplierArticle"),
                "name": item.get("category") or item.get("subject"),
            }
        )
        lcd = item.get("lastChangeDate")
        if lcd:
            last_change = str(lcd)
    return rows, last_change


def parse_stock_items(payload: Any) -> tuple[list[dict], str]:
    items = payload if isinstance(payload, list) else []
    rows = []
    last_change = ""
    for item in items:
        if not isinstance(item, dict) or item.get("barcode") is None:
            continue
        warehouse = item.get("warehouseName") or item.get("warehouse") or "*"
        rows.append(
            {
                "barcode": str(item["barcode"]),
                "warehouse_id": str(warehouse),
                "warehouse_name": str(warehouse),
                "present": int(float(item.get("quantity") or item.get("quantityFull") or 0)),
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
        lcd = item.get("lastChangeDate")
        if lcd:
            last_change = str(lcd)
    return rows, last_change


def parse_finance_lines(payload: Any) -> tuple[list[dict], str]:
    items = payload if isinstance(payload, list) else []
    rows = []
    last_rrd = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        rrd_id = item.get("rrdId", item.get("rrd_id"))
        if rrd_id is None:
            continue
        report_date = item.get("saleDt") or item.get("rr_dt") or item.get("orderDt") or ""
        day = normalize_day(str(report_date)) if report_date else None
        barcode = item.get("sku") or item.get("barcode")
        rows.append(
            {
                "rrd_id": str(rrd_id),
                "report_date": day,
                "barcode": None if barcode is None else str(barcode),
                "for_pay": money(item.get("forPay") or item.get("ppvz_for_pay")),
                "doc_type": item.get("docTypeName") or item.get("doc_type_name"),
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
        last_rrd = str(rrd_id)
    return rows, last_rrd


INBOUND_SUPPLY_STATES = {
    "CREATED",
    "PACKING",
    "IN_DELIVERY",
    "DELIVERING",
    "NOT_ACCEPTED",
}


def supply_is_inbound(state: str | None) -> bool:
    text = (state or "").strip().upper()
    return text in INBOUND_SUPPLY_STATES or text == ""


def parse_supplies_list(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    supplies = payload.get("supplies")
    if not isinstance(supplies, list):
        return []
    ids = []
    for item in supplies:
        if isinstance(item, dict) and item.get("id") is not None:
            ids.append(str(item["id"]))
    return ids


def parse_supply_detail(payload: Any) -> dict | None:
    if not isinstance(payload, dict):
        return None
    supply_id = payload.get("id")
    if supply_id is None:
        return None
    state = payload.get("status") or payload.get("done")
    state_text = str(state) if state is not None else None
    return {
        "supply_id": str(supply_id),
        "state": state_text,
        "inbound": 1 if supply_is_inbound(state_text) else 0,
        "payload": json.dumps(payload, ensure_ascii=False),
    }


def parse_supply_orders(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    orders = payload.get("orders")
    if not isinstance(orders, list):
        return []
    rows = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        barcode = order.get("barcode") or order.get("skus")
        if isinstance(barcode, list):
            for code in barcode:
                if code:
                    rows.append(
                        {
                            "barcode": str(code),
                            "quantity": 1.0,
                            "payload": json.dumps(order, ensure_ascii=False),
                        }
                    )
            continue
        if barcode is None:
            continue
        rows.append(
            {
                "barcode": str(barcode),
                "quantity": float(order.get("quantity") or 1),
                "payload": json.dumps(order, ensure_ascii=False),
            }
        )
    return rows
