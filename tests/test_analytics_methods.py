import json
from datetime import date

from ozon_seller_mcp.client import OzonApiError
from ozon_seller_mcp.db import get_meta
from ozon_seller_mcp.policy import is_history_limit_error, is_metric_rejected
from ozon_seller_mcp.sync import sync_batch

TODAY = date(2026, 10, 12)


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        return self.handler(path, body)


def _insert(conn, dataset, start, end, status="pending"):
    conn.execute(
        """
        INSERT INTO sync_chunks (
            dataset, period_start, period_end, offset_value, page, status
        ) VALUES (?, ?, ?, ?, 1, ?)
        """,
        (dataset, start, end, 0, status),
    )
    conn.commit()


def test_metric_error_is_not_a_history_limit():
    assert is_metric_rejected(400, "unknown metric delivered_units")
    assert not is_history_limit_error(400, "unknown metric delivered_units")
    assert is_history_limit_error(400, "date_from is outside the allowed period")
    assert not is_metric_rejected(400, "date_from is outside the allowed period")


def test_payloads_roundtrip_without_dropping_fields(conn):
    _insert(conn, "finance_accrual", "2026-10-12", "2026-10-12")
    _insert(conn, "prices", "2026-10-12", "2026-10-12")
    _insert(conn, "returns", "2026-09-29", "2026-10-12")
    _insert(conn, "supply_orders", "2026-10-12", "2026-10-12")
    _insert(conn, "sales", "2026-10-12", "2026-10-12")

    def handler(path, body):
        if path.endswith("/v1/finance/accrual/by-day"):
            return {
                "accruals": [
                    {
                        "accrual_id": "a1",
                        "total_amount": {"amount": "15.5", "currency": "RUB"},
                        "fees": [{"type_id": 3, "amount": "1"}],
                        "extra_field": "accrual-extra",
                    }
                ],
                "last_id": "",
            }
        if path.endswith("/v5/product/info/prices"):
            return {
                "items": [
                    {
                        "sku": 9,
                        "price": {"price": "100"},
                        "commissions": {"sales_percent_fbo": 5, "sales_percent_fbs": 7},
                        "extra_field": "price-extra",
                    }
                ],
                "cursor": "",
            }
        if path.endswith("/v1/returns/list"):
            return {
                "returns": [
                    {
                        "id": 3,
                        "product": {"sku": 9, "quantity": 2},
                        "logistic": {"return_date": "2026-10-02T00:00:00Z"},
                        "extra_field": "return-extra",
                    }
                ],
                "has_next": False,
            }
        if path.endswith("/v2/supply-order/list"):
            return {"order_ids": ["55"], "last_id": ""}
        if path.endswith("/v2/supply-order/get"):
            return {
                "orders": [
                    {
                        "order_id": 55,
                        "state": "IN_TRANSIT",
                        "supplies": [{"bundle_id": "b1"}],
                        "extra_field": "order-extra",
                    }
                ]
            }
        if path.endswith("/v1/supply-order/bundle"):
            return {
                "items": [{"sku": 9, "quantity": 4, "extra_field": "line-extra"}],
                "has_next": False,
            }
        if path.endswith("/v1/analytics/data"):
            return {
                "result": {
                    "data": [
                        {
                            "dimensions": [
                                {"id": "9", "name": "Кружка"},
                                {"id": "2026-10-12", "name": ""},
                            ],
                            "metrics": [10, 4, 3, 1, 2],
                            "extra_field": "sale-extra",
                        }
                    ]
                }
            }
        raise AssertionError(path)

    client = FakeClient(handler)
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=8)
    accrual = json.loads(conn.execute("SELECT payload, total_amount FROM finance_accruals").fetchone()["payload"])
    assert accrual["extra_field"] == "accrual-extra"
    assert conn.execute("SELECT total_amount FROM finance_accruals").fetchone()["total_amount"] == 15.5
    price = json.loads(conn.execute("SELECT payload FROM product_prices").fetchone()["payload"])
    assert price["extra_field"] == "price-extra"
    returned = json.loads(conn.execute("SELECT payload FROM returns").fetchone()["payload"])
    assert returned["extra_field"] == "return-extra"
    order = json.loads(conn.execute("SELECT payload, inbound FROM supply_orders").fetchone()["payload"])
    assert order["extra_field"] == "order-extra"
    assert conn.execute("SELECT inbound FROM supply_orders").fetchone()["inbound"] == 1
    line = json.loads(conn.execute("SELECT payload, quantity FROM supply_bundle_items").fetchone()["payload"])
    assert line["extra_field"] == "line-extra"
    assert conn.execute("SELECT quantity FROM supply_bundle_items").fetchone()["quantity"] == 4
    sale = conn.execute(
        "SELECT delivered_units, returns_units, cancellations, payload FROM sales_daily"
    ).fetchone()
    assert sale["delivered_units"] == 3
    assert sale["returns_units"] == 1
    assert sale["cancellations"] == 2
    assert json.loads(sale["payload"])["extra_field"] == "sale-extra"


def test_accrual_page_is_idempotent_by_accrual_id(conn):
    _insert(conn, "finance_accrual", "2026-10-12", "2026-10-12")
    versions = {"n": 0}

    def handler(path, body):
        versions["n"] += 1
        return {
            "accruals": [
                {
                    "accrual_id": "a1",
                    "total_amount": versions["n"],
                    "extra_field": f"v{versions['n']}",
                }
            ],
            "last_id": "page-2" if versions["n"] == 1 else "",
        }

    client = FakeClient(handler)
    first = sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=1)
    assert first["chunks_pending"] == 1
    assert client.calls[0][1]["last_id"] == ""
    second = sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=1)
    assert second["calls_made"] == 1
    assert client.calls[1][1]["last_id"] == "page-2"
    row = conn.execute("SELECT COUNT(*) AS n, total_amount, payload FROM finance_accruals").fetchone()
    assert row["n"] == 1
    assert row["total_amount"] == 2
    assert json.loads(row["payload"])["extra_field"] == "v2"


def test_stale_accrual_cursor_restarts_and_legacy_finance_is_not_called(conn):
    _insert(conn, "finance_accrual", "2026-10-12", "2026-10-12")
    conn.execute("UPDATE sync_chunks SET cursor = ? WHERE dataset = 'finance_accrual'", ("stale\n1",))
    _insert(conn, "finance_totals", "2026-10-01", "2026-10-31")
    _insert(conn, "finance_ops", "2026-10-01", "2026-10-31")
    conn.commit()
    seen = []

    def handler(path, body):
        assert "/v3/finance/transaction/" not in path
        seen.append((path, body.get("last_id")))
        return {"accruals": [], "last_id": ""}

    client = FakeClient(handler)
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=3, now=lambda: 10_000)
    assert seen == [("/v1/finance/accrual/by-day", "")]
    statuses = {
        row["dataset"]: row["status"]
        for row in conn.execute("SELECT dataset, status FROM sync_chunks WHERE dataset != 'finance_accrual'")
    }
    assert statuses["finance_totals"] == "unavailable"
    assert statuses["finance_ops"] == "unavailable"

    conn.execute(
        """
        UPDATE sync_chunks
        SET status = 'pending', cursor = ?
        WHERE dataset = 'finance_accrual'
        """,
        ("live\n9700",),
    )
    conn.commit()
    seen.clear()
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=1, now=lambda: 10_000)
    assert seen == [("/v1/finance/accrual/by-day", "live")]


def test_analytics_falls_back_to_basic_metrics_after_400(conn):
    _insert(conn, "sales", "2026-10-12", "2026-10-12")

    def handler(path, body):
        if "delivered_units" in body["metrics"]:
            raise OzonApiError(400, "unknown metric delivered_units")
        assert body["metrics"] == ["revenue", "ordered_units"]
        return {"result": {"data": [], "totals": []}}

    client = FakeClient(handler)
    receipt = sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=2)
    assert receipt["calls_made"] == 2
    assert get_meta(conn, "analytics_metrics") == "basic"
    status = conn.execute("SELECT status FROM sync_chunks").fetchone()["status"]
    assert status == "done"
