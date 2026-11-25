"""Разбор ответов Ozon Seller API. Порядок metrics совпадает с порядком в запросе."""

from __future__ import annotations

import json
from typing import Any


def items_of(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("items", "operations", "data"):
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    for key in ("items", "operations"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def normalize_day(value: str) -> str:
    text = (value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    if len(text) >= 10 and text[2] == "." and text[5] == ".":
        day, month, year = text[:10].split(".")
        return f"{year}-{month}-{day}"
    return text[:10]


BASIC_METRICS = ["revenue", "ordered_units"]
EXTENDED_METRICS = ["revenue", "ordered_units", "delivered_units", "returns", "cancellations"]
METRIC_COLUMNS = {
    "revenue": "revenue",
    "ordered_units": "ordered_units",
    "delivered_units": "delivered_units",
    "returns": "returns_units",
    "cancellations": "cancellations",
}


def parse_sales_rows(payload: dict, metrics: list[str] | None = None, *, with_payload: bool = False) -> list[dict]:
    """metrics[0] — revenue, metrics[1] — ordered_units, дальше — как в запросе."""
    names = list(metrics or EXTENDED_METRICS)
    rows: list[dict] = []
    for item in items_of(payload):
        dims = item.get("dimensions") or []
        values = item.get("metrics") or []
        if len(dims) < 2 or len(values) < 2:
            continue
        sku = str((dims[0] or {}).get("id") or "").strip()
        day = normalize_day(str((dims[1] or {}).get("id") or ""))
        if not sku or not day:
            continue
        name = str((dims[0] or {}).get("name") or "").strip() or None
        row = {
            "sku": sku,
            "date": day,
            "revenue": float(values[0] or 0),
            "ordered_units": float(values[1] or 0),
            "name": name,
        }
        for index, metric_name in enumerate(names):
            if index < 2 or index >= len(values):
                continue
            column = METRIC_COLUMNS.get(metric_name)
            if column:
                row[column] = float(values[index] or 0)
        if with_payload:
            row["payload"] = json.dumps(item, ensure_ascii=False)
        rows.append(row)
    return rows


def analytics_request(
    date_from: str,
    date_to: str,
    offset: int,
    limit: int,
    metrics: list[str] | None = None,
) -> dict:
    return {
        "date_from": date_from,
        "date_to": date_to,
        "metrics": list(metrics or EXTENDED_METRICS),
        "dimension": ["sku", "day"],
        "filters": [],
        "sort": [{"key": "revenue", "order": "DESC"}],
        "limit": limit,
        "offset": offset,
    }


def extract_sku(item: dict) -> str | None:
    sku = item.get("sku")
    if sku is not None and str(sku) not in ("", "0", "None"):
        return str(sku)
    for source in item.get("sources") or []:
        if not isinstance(source, dict):
            continue
        source_sku = source.get("sku")
        if source_sku is not None and str(source_sku) not in ("", "0", "None"):
            return str(source_sku)
    return None


def parse_product_list(payload: dict) -> tuple[list[dict], str]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        items = []
    last_id = ""
    if isinstance(result, dict) and result.get("last_id"):
        last_id = str(result["last_id"])
    parsed = []
    for item in items:
        if not isinstance(item, dict) or item.get("product_id") is None:
            continue
        parsed.append(
            {
                "product_id": int(item["product_id"]),
                "offer_id": item.get("offer_id"),
            }
        )
    return parsed, last_id


def extract_barcodes(item: dict) -> list[str]:
    found: list[str] = []
    raw = item.get("barcodes")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                found.append(entry.strip())
            elif isinstance(entry, dict):
                code = entry.get("barcode") or entry.get("value")
                if code is not None and str(code).strip():
                    found.append(str(code).strip())
    barcode = item.get("barcode")
    if barcode is not None and str(barcode).strip():
        found.append(str(barcode).strip())
    unique: list[str] = []
    for code in found:
        if code not in unique:
            unique.append(code)
    return unique


def parse_product_info(payload: dict) -> list[dict]:
    parsed = []
    for item in items_of(payload):
        sku = extract_sku(item)
        product_id = item.get("id", item.get("product_id"))
        parsed.append(
            {
                "sku": sku,
                "product_id": int(product_id) if product_id is not None else None,
                "offer_id": item.get("offer_id"),
                "name": item.get("name"),
                "barcodes": extract_barcodes(item),
            }
        )
    return parsed


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def parse_stock_items(payload: dict, *, sku_total: bool) -> list[dict]:
    parsed = []
    for item in items_of(payload):
        sku = item.get("sku")
        if sku is None:
            continue
        present_raw = item.get("available_stock_count")
        if present_raw is None:
            present_raw = item.get("valid_stock_count")
        if present_raw is None:
            present_raw = item.get("current_stock", item.get("present", 0))
        warehouse_id = item.get("warehouse_id")
        if sku_total or warehouse_id is None:
            warehouse_key = "*"
            warehouse_name = item.get("warehouse_name") or "Все склады"
        else:
            warehouse_key = str(warehouse_id)
            warehouse_name = item.get("warehouse_name")
        idc = item.get("idc", item.get("days_without_sales"))
        parsed.append(
            {
                "sku": str(sku),
                "warehouse_id": warehouse_key,
                "warehouse_name": warehouse_name,
                "present": int(float(present_raw or 0)),
                "reserved": int(item["reserved"]) if item.get("reserved") is not None else None,
                "ozon_idc": _number(idc),
                "turnover_grade": item.get("turnover_grade") or item.get("idc_grade"),
                "name": item.get("name"),
                "offer_id": item.get("offer_id"),
            }
        )
    return parsed


def parse_finance_totals(payload: dict) -> dict:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    if not isinstance(result, dict):
        result = {}
    keys = (
        "accruals_for_sale",
        "sale_commission",
        "processing_and_delivery",
        "refunds_and_cancellations",
        "services_amount",
        "compensation_amount",
        "money_transfer",
        "others_amount",
    )
    return {key: float(result.get(key) or 0) for key in keys}


def parse_finance_operations(payload: dict) -> tuple[list[dict], int | None]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    operations = []
    if isinstance(result, dict):
        raw_ops = result.get("operations") or []
        page_count = result.get("page_count")
    else:
        raw_ops = []
        page_count = None
    for item in raw_ops:
        if not isinstance(item, dict) or item.get("operation_id") is None:
            continue
        operation_date = item.get("operation_date") or ""
        operations.append(
            {
                "operation_id": str(item["operation_id"]),
                "operation_date": normalize_day(str(operation_date)) if operation_date else None,
                "operation_type": item.get("operation_type"),
                "amount": float(item["amount"]) if item.get("amount") is not None else None,
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
    return operations, int(page_count) if page_count is not None else None


def money(value: Any) -> float:
    if isinstance(value, dict):
        raw = value.get("amount")
        if raw is None or raw == "":
            return 0.0
        return float(raw)
    if value is None or value == "":
        return 0.0
    return float(value)


def optional_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        raw = value.get("amount", value.get("percent"))
        if raw is None or raw == "":
            return None
        return float(raw)
    return float(value)


def _container(payload: dict) -> dict:
    result = payload.get("result")
    if isinstance(result, dict):
        return result
    return payload if isinstance(payload, dict) else {}


def parse_accrual_page(payload: dict, *, day: str) -> tuple[list[dict], str]:
    container = _container(payload)
    raw = container.get("accruals")
    if not isinstance(raw, list):
        raw = container.get("items") if isinstance(container.get("items"), list) else []
    last_id = str(container.get("last_id") or "")
    rows = []
    for item in raw:
        if not isinstance(item, dict) or item.get("accrual_id") is None:
            continue
        type_id = item.get("type_id", item.get("accrual_type_id"))
        if type_id is None:
            fees = item.get("fees") or []
            if fees and isinstance(fees[0], dict) and fees[0].get("type_id") is not None:
                type_id = fees[0]["type_id"]
        accrual_date = item.get("date") or item.get("accrual_date") or day
        rows.append(
            {
                "accrual_id": str(item["accrual_id"]),
                "accrual_date": normalize_day(str(accrual_date)) if accrual_date else day,
                "type_id": None if type_id is None else str(type_id),
                "total_amount": money(item.get("total_amount")),
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
    return rows, last_id


def parse_accrual_types(payload: dict) -> list[dict]:
    container = _container(payload)
    raw = container.get("types")
    if not isinstance(raw, list):
        raw = container.get("items") if isinstance(container.get("items"), list) else items_of(payload)
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        type_id = item.get("type_id", item.get("id"))
        if type_id is None:
            continue
        name = item.get("name") or item.get("title") or item.get("type_name")
        rows.append(
            {
                "type_id": str(type_id),
                "name": None if name is None else str(name),
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
    return rows


def parse_prices(payload: dict) -> tuple[list[dict], str]:
    container = _container(payload)
    raw = container.get("items") if isinstance(container.get("items"), list) else []
    cursor = container.get("cursor", payload.get("cursor") if isinstance(payload, dict) else "")
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sku = extract_sku(item)
        if not sku and item.get("product_id") is not None:
            sku = f"product:{item['product_id']}"
        if not sku:
            continue
        price_obj = item.get("price") if isinstance(item.get("price"), dict) else {}
        commissions = item.get("commissions") if isinstance(item.get("commissions"), dict) else {}
        price_raw = price_obj.get("price") if price_obj else item.get("price")
        rows.append(
            {
                "sku": sku,
                "price": optional_number(price_raw),
                "sales_percent_fbo": optional_number(
                    commissions.get("sales_percent_fbo", item.get("sales_percent_fbo"))
                ),
                "sales_percent_fbs": optional_number(
                    commissions.get("sales_percent_fbs", item.get("sales_percent_fbs"))
                ),
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
    return rows, "" if cursor is None else str(cursor)


def parse_returns(payload: dict) -> tuple[list[dict], str]:
    container = _container(payload)
    raw = payload.get("returns") if isinstance(payload.get("returns"), list) else None
    if raw is None:
        raw = container.get("returns") if isinstance(container.get("returns"), list) else None
    if raw is None:
        raw = container.get("items") if isinstance(container.get("items"), list) else []
    last_id = payload.get("last_id", container.get("last_id"))
    has_next = payload.get("has_next", container.get("has_next"))
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        product = item.get("product") if isinstance(item.get("product"), dict) else {}
        return_id = item.get("id", item.get("return_id"))
        if return_id is None:
            continue
        sku = item.get("sku", product.get("sku"))
        quantity = item.get("quantity", product.get("quantity"))
        logistic = item.get("logistic") if isinstance(item.get("logistic"), dict) else {}
        return_date = (
            item.get("return_date")
            or logistic.get("return_date")
            or item.get("logistic_return_date")
            or ""
        )
        rows.append(
            {
                "return_id": str(return_id),
                "sku": None if sku is None else str(sku),
                "quantity": optional_number(quantity),
                "return_date": normalize_day(str(return_date)) if return_date else None,
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
    if has_next is False:
        return rows, ""
    if (last_id in (None, "", 0, "0")) and has_next and rows:
        last_id = rows[-1]["return_id"]
    if last_id in (None, "", 0, "0"):
        return rows, ""
    return rows, str(last_id)


INBOUND_STATES = {
    "DATA_FILLING",
    "READY_TO_SUPPLY",
    "ACCEPTED_AT_SUPPLY_WAREHOUSE",
    "IN_TRANSIT",
    "ACCEPTANCE_AT_STORAGE_WAREHOUSE",
    "OVERDUE",
}


def normalize_supply_state(state: str | None) -> str:
    text = (state or "").strip().upper()
    if text.startswith("ORDER_STATE_"):
        text = text[len("ORDER_STATE_") :]
    return text


def supply_is_inbound(state: str | None) -> bool:
    return normalize_supply_state(state) in INBOUND_STATES


def _bundle_ids(order: dict) -> list[str]:
    found: list[str] = []
    if order.get("bundle_id"):
        found.append(str(order["bundle_id"]))
    raw_ids = order.get("bundle_ids")
    if isinstance(raw_ids, list):
        found.extend(str(item) for item in raw_ids if item not in (None, ""))
    for key in ("bundles", "supplies"):
        raw = order.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str) and item:
                found.append(item)
            elif isinstance(item, dict) and item.get("bundle_id"):
                found.append(str(item["bundle_id"]))
    unique: list[str] = []
    for bundle_id in found:
        if bundle_id not in unique:
            unique.append(bundle_id)
    return unique


def parse_supply_list(payload: dict) -> tuple[list[str], str]:
    container = _container(payload)
    raw_ids = container.get("order_ids", payload.get("order_ids"))
    order_ids: list[str] = []
    if isinstance(raw_ids, list):
        order_ids.extend(str(item) for item in raw_ids if item not in (None, ""))
    for key in ("orders", "supply_orders"):
        raw = container.get(key)
        if not isinstance(raw, list):
            continue
        for order in raw:
            if not isinstance(order, dict):
                continue
            order_id = order.get("order_id", order.get("supply_order_id"))
            if order_id not in (None, ""):
                order_ids.append(str(order_id))
    unique: list[str] = []
    for order_id in order_ids:
        if order_id not in unique:
            unique.append(order_id)
    last_id = container.get("last_id", payload.get("last_id", payload.get("last_supply_order_id")))
    return unique, "" if last_id in (None, "", 0, "0") else str(last_id)


def parse_supply_orders(payload: dict) -> list[dict]:
    container = _container(payload)
    raw = None
    for key in ("orders", "supply_orders"):
        if isinstance(container.get(key), list):
            raw = container[key]
            break
        if isinstance(payload.get(key), list):
            raw = payload[key]
            break
    if raw is None:
        raw = items_of(payload)
    rows = []
    for order in raw:
        if not isinstance(order, dict):
            continue
        order_id = order.get("order_id", order.get("supply_order_id"))
        if order_id in (None, ""):
            continue
        state = order.get("state") or order.get("status")
        rows.append(
            {
                "order_id": str(order_id),
                "state": None if state is None else str(state),
                "inbound": 1 if supply_is_inbound(None if state is None else str(state)) else 0,
                "bundle_ids": _bundle_ids(order),
                "payload": json.dumps(order, ensure_ascii=False),
            }
        )
    return rows


def parse_bundle_items(payload: dict) -> tuple[list[dict], str]:
    container = _container(payload)
    raw = container.get("items")
    if not isinstance(raw, list):
        raw = container.get("products") if isinstance(container.get("products"), list) else []
    last_id = container.get("last_id", payload.get("last_id"))
    has_next = container.get("has_next", payload.get("has_next"))
    rows = []
    for item in raw:
        if not isinstance(item, dict) or item.get("sku") is None:
            continue
        quantity = item.get("quantity", item.get("quant", 0))
        rows.append(
            {
                "sku": str(item["sku"]),
                "quantity": float(quantity or 0),
                "bundle_id": None if item.get("bundle_id") is None else str(item["bundle_id"]),
                "payload": json.dumps(item, ensure_ascii=False),
            }
        )
    if has_next is False or last_id in (None, "", 0, "0"):
        return rows, ""
    return rows, str(last_id)
