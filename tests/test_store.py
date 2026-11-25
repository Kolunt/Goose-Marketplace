import json

from ozon_seller_mcp.db import upsert_operation, upsert_sale


def test_sale_upsert_is_idempotent(conn):
    upsert_sale(conn, sku="10", day="2026-01-01", revenue=5, ordered_units=1, fetched_at="t1")
    upsert_sale(conn, sku="10", day="2026-01-01", revenue=9, ordered_units=2, fetched_at="t2")
    conn.commit()
    rows = conn.execute("SELECT revenue, ordered_units, fetched_at FROM sales_daily").fetchall()
    assert len(rows) == 1
    assert rows[0]["revenue"] == 9
    assert rows[0]["ordered_units"] == 2
    assert rows[0]["fetched_at"] == "t2"


def test_operation_upsert_is_idempotent(conn):
    payload = json.dumps({"operation_id": 7, "amount": 3})
    upsert_operation(
        conn,
        operation_id="7",
        operation_date="2026-01-02",
        operation_type="OperationAgentDeliveredToCustomer",
        amount=3,
        payload=payload,
        fetched_at="t1",
    )
    upsert_operation(
        conn,
        operation_id="7",
        operation_date="2026-01-02",
        operation_type="OperationAgentDeliveredToCustomer",
        amount=4,
        payload=payload,
        fetched_at="t2",
    )
    conn.commit()
    rows = conn.execute("SELECT amount, fetched_at FROM finance_operations").fetchall()
    assert len(rows) == 1
    assert rows[0]["amount"] == 4
    assert rows[0]["fetched_at"] == "t2"
