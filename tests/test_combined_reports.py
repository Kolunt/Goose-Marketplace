from datetime import date

from ozon_seller_mcp.db import set_unit_cost, upsert_accrual, upsert_barcode_catalog, upsert_sale
from ozon_seller_mcp.combined_reports import combined_profit_summary_report, combined_sales_volume_report


def test_combined_sales_joins_by_barcode(conn):
    upsert_barcode_catalog(conn, barcode="111", ozon_sku="oz-1", fetched_at="t")
    upsert_sale(conn, sku="oz-1", day="2026-10-10", revenue=50, ordered_units=5, fetched_at="t")
    conn.execute(
        """
        INSERT INTO wb_sales_daily (barcode, date, ordered_units, return_units, revenue, fetched_at)
        VALUES ('111', '2026-10-10', 3, 0, 30, 't')
        """
    )
    conn.commit()
    report = combined_sales_volume_report(conn, date(2026, 10, 1), date(2026, 10, 31), "barcode")
    row = next(r for r in report["rows"] if r["barcode"] == "111")
    assert row["ozon_ordered_units"] == 5
    assert row["wb_ordered_units"] == 3
    assert row["total_ordered_units"] == 8


def test_combined_profit_uses_barcode_cost(conn):
    upsert_barcode_catalog(conn, barcode="111", ozon_sku="oz-1", fetched_at="t")
    upsert_sale(conn, sku="oz-1", day="2026-10-10", revenue=0, ordered_units=10, fetched_at="t")
    upsert_accrual(
        conn,
        accrual_id="a1",
        accrual_date="2026-10-10",
        type_id="1",
        total_amount=100,
        payload="{}",
        fetched_at="t",
    )
    conn.execute(
        """
        INSERT INTO wb_finance_lines (rrd_id, report_date, barcode, for_pay, doc_type, payload, fetched_at)
        VALUES ('r1', '2026-10-10', '111', 50, 'Продажа', '{}', 't')
        """
    )
    conn.execute(
        """
        INSERT INTO wb_sales_daily (barcode, date, ordered_units, return_units, revenue, fetched_at)
        VALUES ('111', '2026-10-10', 10, 0, 0, 't')
        """
    )
    set_unit_cost(conn, barcode="111", cost=5, currency="RUB", updated_at="t")
    conn.commit()
    report = combined_profit_summary_report(conn, date(2026, 10, 1), date(2026, 10, 31))
    assert report["marketplace_profit"] == 150
    assert report["profit"] == 50
