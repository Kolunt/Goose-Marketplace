import json
from datetime import date

from ozon_seller_mcp.sync import ensure_queue, sync_batch
from ozon_seller_mcp.wb_sync import ensure_wb_queue

TODAY = date(2026, 10, 12)


class FakeWbClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def get(self, base, path, params=None):
        self.calls.append((base, path, params))
        return self.handler("GET", base, path, params or {})

    def post(self, base, path, body=None):
        self.calls.append((base, path, body))
        return self.handler("POST", base, path, body or {})


def test_wb_sales_payload_roundtrip(conn):
    ensure_wb_queue(conn, TODAY, 7)
    conn.execute(
        "UPDATE sync_chunks SET status = 'pending' WHERE dataset = 'wb_sales' AND period_start = '2026-10-12'"
    )
    conn.commit()

    def handler(method, base, path, body):
        if path.endswith("/supplier/sales"):
            return [
                {
                    "srid": "s1",
                    "date": "2026-10-12T10:00:00",
                    "barcode": "4600001",
                    "quantity": 2,
                    "totalPrice": 100,
                    "extra": "keep",
                }
            ]
        raise AssertionError((method, path))

    client = FakeWbClient(handler)
    sync_batch(
        conn,
        None,
        wb_client=client,
        today=TODAY,
        ensure=False,
        marketplaces=("wb",),
        max_calls=1,
        analytics_interval=0,
    )
    row = conn.execute("SELECT payload FROM wb_sales_lines").fetchone()
    assert json.loads(row["payload"])["extra"] == "keep"
    daily = conn.execute("SELECT ordered_units FROM wb_sales_daily WHERE barcode = '4600001'").fetchone()
    assert daily["ordered_units"] == 2


class OzonFake:
    def __init__(self):
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        return {"result": {"data": [], "totals": []}}


def test_ozon_sync_does_not_call_wb(conn):
    ensure_queue(conn, TODAY, 7)
    wb = FakeWbClient(lambda *args: (_ for _ in ()).throw(AssertionError("wb called")))
    ozon = OzonFake()
    receipt = sync_batch(
        conn,
        ozon,
        wb_client=wb,
        today=TODAY,
        ensure=False,
        marketplaces=("ozon",),
        max_calls=1,
        analytics_interval=0,
    )
    assert wb.calls == []
    assert receipt["calls_made"] == 1


def _insert(conn, dataset, start, end, status="pending"):
    conn.execute(
        """
        INSERT INTO sync_chunks (
            dataset, period_start, period_end, offset_value, page, status
        ) VALUES (?, ?, ?, 0, 1, ?)
        """,
        (dataset, start, end, status),
    )
    conn.commit()
