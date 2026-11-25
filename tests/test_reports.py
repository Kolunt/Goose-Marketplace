from datetime import date

from ozon_seller_mcp.db import (
    set_unit_cost,
    upsert_accrual,
    upsert_accrual_type,
    upsert_barcode_catalog,
    upsert_finance_totals,
    upsert_sale,
)
from ozon_seller_mcp.forecast import cost_coverage, order_quantity
from ozon_seller_mcp.reports import profit_summary_report, sales_volume_report, stock_forecast_report


def test_order_quantity_zero_sales_and_rounding():
    assert order_quantity(0, 10, 14, 7) == 0
    assert order_quantity(0, 0, 14, 7, inbound=5) == 0
    assert order_quantity(1.1, 0, 1, 0) == 2
    assert order_quantity(1, 0, 14, 7, inbound=5) == 16


def test_stock_forecast_with_zero_sales(conn):
    conn.execute(
        """
        INSERT INTO stock_snapshots (
            sku, warehouse_id, warehouse_name, present, reserved, ozon_idc, turnover_grade, fetched_at
        ) VALUES ('10', '*', 'Все склады', 8, NULL, 4, 'DEFICIT', '2026-10-12T00:00:00')
        """
    )
    conn.commit()
    report = stock_forecast_report(conn, date(2026, 10, 12), lead_time_days=14, safety_days=7)
    row = report["rows"][0]
    assert row["present"] == 8
    assert row["days_cover"] is None
    assert row["stockout_date"] is None
    assert row["order_qty"] == 0
    assert row["inbound_qty"] == 0
    assert row["returns_count"] == 0
    assert row["ozon_idc"] == 4
    assert report["supplies_pending"] is False


def test_partial_unit_cost_is_not_silent_zero(conn):
    upsert_sale(conn, sku="a", day="2026-09-10", revenue=100, ordered_units=10, fetched_at="t")
    upsert_sale(conn, sku="b", day="2026-09-10", revenue=50, ordered_units=10, fetched_at="t")
    upsert_finance_totals(
        conn,
        "2026-09",
        {
            "accruals_for_sale": 999,
            "sale_commission": 0,
            "processing_and_delivery": 0,
            "refunds_and_cancellations": 0,
            "services_amount": 0,
            "compensation_amount": 0,
            "money_transfer": 100,
            "others_amount": 0,
        },
        "t",
    )
    upsert_accrual_type(conn, type_id="1", name="Продажа", payload='{"id":1}', fetched_at="t")
    upsert_accrual(
        conn,
        accrual_id="a1",
        accrual_date="2026-09-10",
        type_id="1",
        total_amount=120,
        payload='{"accrual_id":"a1","total_amount":120}',
        fetched_at="t",
    )
    upsert_barcode_catalog(conn, barcode="a", ozon_sku="a", fetched_at="t")
    upsert_barcode_catalog(conn, barcode="b", ozon_sku="b", fetched_at="t")
    set_unit_cost(conn, barcode="a", cost=5, currency="RUB", updated_at="t")
    conn.commit()
    report = profit_summary_report(conn, date(2026, 9, 1), date(2026, 9, 30))
    assert report["revenue"] == 150
    assert report["unit_cost_coverage"] == 0.5
    assert report["cogs_known"] == 50
    assert report["includes_cogs"] is False
    assert report["marketplace_profit"] == 120
    assert report["profit"] == 70
    assert report["components"]["Продажа"] == 120
    assert report["finance_empty"] is False
    assert "50%" in report["note"]
    coverage = cost_coverage({"a": 10, "b": 10}, {"a": 5})
    assert coverage["cogs_applied"] == 50


def test_inbound_reduces_order_but_zero_sales_stay_zero(conn):
    upsert_sale(conn, sku="moving", day="2026-10-01", revenue=28, ordered_units=28, fetched_at="t")
    conn.execute(
        """
        INSERT INTO supply_orders (order_id, state, inbound, payload, fetched_at)
        VALUES ('s1', 'IN_TRANSIT', 1, '{}', 't'),
               ('s2', 'COMPLETED', 0, '{}', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO supply_bundle_items (order_id, sku, quantity, payload, fetched_at)
        VALUES ('s1', 'moving', 5, '{}', 't'),
               ('s2', 'moving', 9, '{}', 't'),
               ('s1', 'idle', 4, '{}', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO stock_snapshots (
            sku, warehouse_id, warehouse_name, present, reserved, ozon_idc, turnover_grade, fetched_at
        ) VALUES ('idle', '*', 'Все склады', 8, NULL, NULL, NULL, 't')
        """
    )
    conn.execute(
        """
        INSERT INTO sync_chunks (
            dataset, period_start, period_end, offset_value, page, status
        ) VALUES ('supply_orders', '2026-10-12', '2026-10-12', 0, 1, 'pending')
        """
    )
    conn.commit()
    report = stock_forecast_report(conn, date(2026, 10, 12), lead_time_days=14, safety_days=7)
    rows = {row["sku"]: row for row in report["rows"]}
    assert rows["moving"]["order_qty"] == 16
    assert rows["moving"]["inbound_qty"] == 5
    assert rows["idle"]["order_qty"] == 0
    assert rows["idle"]["inbound_qty"] == 4
    assert report["supplies_pending"] is True


def test_profit_is_empty_without_accruals(conn):
    upsert_finance_totals(
        conn,
        "2026-09",
        {
            "accruals_for_sale": 500,
            "sale_commission": 0,
            "processing_and_delivery": 0,
            "refunds_and_cancellations": 0,
            "services_amount": 0,
            "compensation_amount": 0,
            "money_transfer": 0,
            "others_amount": 0,
        },
        "t",
    )
    conn.commit()
    report = profit_summary_report(conn, date(2026, 9, 1), date(2026, 9, 30))
    assert report["marketplace_profit"] == 0
    assert report["finance_empty"] is True
    assert "Финансовая база пуста" in report["note"]


def test_sales_grouped_by_week(conn):
    upsert_sale(conn, sku="a", day="2026-10-12", revenue=10, ordered_units=1, fetched_at="t")
    upsert_sale(conn, sku="a", day="2026-10-13", revenue=5, ordered_units=1, fetched_at="t")
    upsert_sale(conn, sku="a", day="2026-10-05", revenue=7, ordered_units=1, fetched_at="t")
    conn.commit()
    report = sales_volume_report(conn, date(2026, 10, 1), date(2026, 10, 31), "week")
    assert report["totals"]["revenue"] == 22
    assert len(report["rows"]) == 2
    assert report["data_as_of"] == "t"
