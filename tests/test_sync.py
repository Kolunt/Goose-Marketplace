from datetime import date

from ozon_seller_mcp.client import OzonApiError
from ozon_seller_mcp.sync import ensure_queue, mark_due, sync_batch, sync_until_idle, unfreeze

TODAY = date(2026, 10, 12)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def post(self, path, body):
        self.calls.append((path, body))
        return self.handler(path, body)


def _insert(conn, dataset, start, end, status="pending", offset=0, page=1):
    conn.execute(
        """
        INSERT INTO sync_chunks (
            dataset, period_start, period_end, offset_value, page, status
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dataset, start, end, offset, page, status),
    )
    conn.commit()


def _empty_sales(path, body):
    return {"result": {"data": [], "totals": []}}


def test_batch_stops_at_five_calls(conn):
    for index in range(8):
        day = date(2026, 10, 12 - index).isoformat()
        _insert(conn, "sales", day, day)
    client = FakeClient(lambda path, body: _empty_sales(path, body))
    receipt = sync_batch(
        conn,
        client,
        today=TODAY,
        ensure=False,
        max_calls=5,
        analytics_interval=0,
    )
    assert len(client.calls) == 5
    assert receipt["calls_made"] == 5
    assert receipt["stopped_reason"] == "batch_limit"
    assert receipt["chunks_pending"] == 3


def test_minute_gap_is_waited(conn):
    _insert(conn, "sales", "2026-10-11", "2026-10-11")
    _insert(conn, "sales", "2026-10-12", "2026-10-12")
    clock = Clock()
    client = FakeClient(lambda path, body: _empty_sales(path, body))
    receipt = sync_batch(
        conn,
        client,
        today=TODAY,
        ensure=False,
        max_calls=2,
        analytics_interval=60,
        sleep=clock.sleep,
        now=clock.now,
    )
    assert len(client.calls) == 2
    assert clock.slept == [60]
    assert receipt["hit_minute_limit"] is True


def test_old_success_becomes_frozen_and_is_skipped(conn):
    _insert(conn, "sales", "2026-01-01", "2026-01-07")
    client = FakeClient(lambda path, body: _empty_sales(path, body))
    first = sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=5)
    assert first["chunks_frozen"] == 1
    assert len(client.calls) == 1
    second = sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=5)
    assert len(client.calls) == 1
    assert second["calls_made"] == 0
    assert second["stopped_reason"] == "queue_empty"


def test_history_limit_closes_older_chunks(conn):
    _insert(conn, "sales", "2026-01-01", "2026-01-07")
    _insert(conn, "sales", "2026-02-01", "2026-02-07")
    _insert(conn, "sales", "2026-10-01", "2026-10-07")

    def handler(path, body):
        if body["date_from"] < "2026-06-01":
            raise OzonApiError(400, "date_from is outside the allowed period")
        return _empty_sales(path, body)

    client = FakeClient(handler)
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, max_calls=10)
    rows = {
        row["period_start"]: row["status"]
        for row in conn.execute("SELECT period_start, status FROM sync_chunks")
    }
    assert rows["2026-10-01"] == "done"
    assert rows["2026-02-01"] == "unavailable"
    assert rows["2026-01-01"] == "unavailable"
    assert [body["date_from"] for _path, body in client.calls] == ["2026-10-01", "2026-02-01"]


def test_unfreeze_puts_chunk_back(conn):
    _insert(conn, "sales", "2026-01-01", "2026-01-07", status="frozen")
    client = FakeClient(lambda path, body: _empty_sales(path, body))
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0)
    assert client.calls == []
    updated = unfreeze(conn, "sales", "2026-01-01", "2026-01-07")
    assert updated == 1
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0)
    assert len(client.calls) == 1


def test_queue_survives_second_ensure(conn):
    ensure_queue(conn, TODAY, 365)
    conn.execute(
        """
        UPDATE sync_chunks SET status = 'done', synced_at = '2026-10-01T00:00:00'
        WHERE dataset = 'sales' AND period_start = (
            SELECT MIN(period_start) FROM sync_chunks WHERE dataset = 'sales'
        )
        """
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) AS n FROM sync_chunks").fetchone()["n"]
    done = conn.execute("SELECT COUNT(*) AS n FROM sync_chunks WHERE status = 'done'").fetchone()["n"]
    ensure_queue(conn, TODAY, 365)
    after = conn.execute("SELECT COUNT(*) AS n FROM sync_chunks").fetchone()["n"]
    done_after = conn.execute("SELECT COUNT(*) AS n FROM sync_chunks WHERE status = 'done'").fetchone()["n"]
    assert before == after
    assert done == done_after == 1


def test_refresh_open_once_per_cli_run(conn):
    def handler(path, body):
        if path.endswith("/v3/product/list"):
            return {"result": {"items": [], "last_id": ""}}
        if path.endswith("/v3/product/info/list"):
            return {"items": []}
        if path.endswith("/v1/analytics/data"):
            return _empty_sales(path, body)
        if "/v3/finance/transaction/" in path:
            raise AssertionError(path)
        if path.endswith("/v1/finance/accrual/by-day"):
            return {"accruals": [], "last_id": ""}
        if path.endswith("/v1/finance/accrual/types"):
            return {"types": []}
        if path.endswith("/v5/product/info/prices"):
            return {"items": [], "cursor": ""}
        if path.endswith("/v1/returns/list"):
            return {"returns": [], "has_next": False}
        if path.endswith("/v2/supply-order/list"):
            return {"order_ids": [], "last_id": ""}
        if path.endswith("/v1/analytics/turnover/stocks"):
            return {"items": []}
        if path.endswith("/v1/analytics/stocks"):
            return {"items": []}
        raise AssertionError(path)

    client = FakeClient(handler)
    first = sync_until_idle(conn, client, today=TODAY, horizon_days=7, analytics_interval=0)
    assert first["stopped_reason"] == "queue_empty"
    assert first["calls_made"] < 30
    calls_after_first = len(client.calls)
    second = sync_until_idle(conn, client, today=TODAY, horizon_days=7, analytics_interval=0)
    assert second["stopped_reason"] == "queue_empty"
    assert second["calls_made"] < calls_after_first
    assert second["chunks_pending"] == 0


def test_sales_page_continues_from_offset(conn):
    _insert(conn, "sales", "2026-10-10", "2026-10-10")

    def handler(path, body):
        offset = body["offset"]
        count = 2 if offset == 0 else 1
        data = []
        for index in range(count):
            data.append(
                {
                    "dimensions": [
                        {"id": f"{offset + index}", "name": "Товар"},
                        {"id": "2026-10-10", "name": ""},
                    ],
                    "metrics": [3, 1],
                }
            )
        return {"result": {"data": data, "totals": []}}

    client = FakeClient(handler)
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, page_limit=2, max_calls=5)
    assert [body["offset"] for _path, body in client.calls] == [0, 2]
    assert conn.execute("SELECT COUNT(*) AS n FROM sales_daily").fetchone()["n"] == 3
    second = sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0, page_limit=2, max_calls=5)
    assert second["calls_made"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM sales_daily").fetchone()["n"] == 3


def test_open_chunk_is_refetched_when_asked(conn):
    _insert(conn, "sales", "2026-10-12", "2026-10-12", status="done")
    conn.execute("UPDATE sync_chunks SET synced_at = '2026-10-12T00:00:00'")
    conn.commit()
    client = FakeClient(lambda path, body: _empty_sales(path, body))
    sync_batch(conn, client, today=TODAY, ensure=False, refresh_open=False, analytics_interval=0)
    assert client.calls == []
    mark_due(conn, TODAY, refresh_open=True)
    sync_batch(conn, client, today=TODAY, ensure=False, analytics_interval=0)
    assert len(client.calls) == 1
