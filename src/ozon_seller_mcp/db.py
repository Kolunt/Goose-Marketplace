"""SQLite: схема, путь к файлу и идемпотентная запись."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY,
    offer_id TEXT,
    product_id INTEGER,
    name TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_daily (
    sku TEXT NOT NULL,
    date TEXT NOT NULL,
    revenue REAL NOT NULL,
    ordered_units REAL NOT NULL,
    delivered_units REAL,
    returns_units REAL,
    cancellations REAL,
    payload TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (sku, date)
);

CREATE TABLE IF NOT EXISTS stock_snapshots (
    sku TEXT NOT NULL,
    warehouse_id TEXT NOT NULL,
    warehouse_name TEXT,
    present INTEGER NOT NULL,
    reserved INTEGER,
    ozon_idc REAL,
    turnover_grade TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (sku, warehouse_id, fetched_at)
);

CREATE TABLE IF NOT EXISTS finance_totals (
    month TEXT PRIMARY KEY,
    accruals_for_sale REAL NOT NULL DEFAULT 0,
    sale_commission REAL NOT NULL DEFAULT 0,
    processing_and_delivery REAL NOT NULL DEFAULT 0,
    refunds_and_cancellations REAL NOT NULL DEFAULT 0,
    services_amount REAL NOT NULL DEFAULT 0,
    compensation_amount REAL NOT NULL DEFAULT 0,
    money_transfer REAL NOT NULL DEFAULT 0,
    others_amount REAL NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_operations (
    operation_id TEXT PRIMARY KEY,
    operation_date TEXT,
    operation_type TEXT,
    amount REAL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_accruals (
    accrual_id TEXT PRIMARY KEY,
    accrual_date TEXT,
    type_id TEXT,
    total_amount REAL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finance_accrual_types (
    type_id TEXT PRIMARY KEY,
    name TEXT,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_prices (
    sku TEXT PRIMARY KEY,
    price REAL,
    sales_percent_fbo REAL,
    sales_percent_fbs REAL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS returns (
    return_id TEXT PRIMARY KEY,
    sku TEXT,
    quantity REAL,
    return_date TEXT,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_orders (
    order_id TEXT PRIMARY KEY,
    state TEXT,
    inbound INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_bundle_items (
    order_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity REAL NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (order_id, sku)
);

CREATE TABLE IF NOT EXISTS barcode_catalog (
    barcode TEXT PRIMARY KEY,
    ozon_sku TEXT,
    nm_id TEXT,
    vendor_code TEXT,
    name TEXT,
    payload TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_costs (
    barcode TEXT PRIMARY KEY,
    cost REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'RUB',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_sales_lines (
    srid TEXT PRIMARY KEY,
    sale_date TEXT,
    barcode TEXT,
    quantity REAL NOT NULL,
    revenue REAL NOT NULL,
    is_return INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_sales_daily (
    barcode TEXT NOT NULL,
    date TEXT NOT NULL,
    ordered_units REAL NOT NULL,
    return_units REAL NOT NULL DEFAULT 0,
    revenue REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (barcode, date)
);

CREATE TABLE IF NOT EXISTS wb_stock_snapshots (
    barcode TEXT NOT NULL,
    warehouse_id TEXT NOT NULL,
    warehouse_name TEXT,
    present INTEGER NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (barcode, warehouse_id, fetched_at)
);

CREATE TABLE IF NOT EXISTS wb_finance_lines (
    rrd_id TEXT PRIMARY KEY,
    report_date TEXT,
    barcode TEXT,
    for_pay REAL,
    doc_type TEXT,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_supplies (
    supply_id TEXT PRIMARY KEY,
    state TEXT,
    inbound INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_supply_items (
    supply_id TEXT NOT NULL,
    barcode TEXT NOT NULL,
    quantity REAL NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (supply_id, barcode)
);

CREATE TABLE IF NOT EXISTS sync_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    offset_value INTEGER NOT NULL DEFAULT 0,
    page INTEGER NOT NULL DEFAULT 1,
    cursor TEXT,
    status TEXT NOT NULL,
    row_count INTEGER,
    error TEXT,
    synced_at TEXT,
    UNIQUE (dataset, period_start, period_end, offset_value, page)
);

CREATE TABLE IF NOT EXISTS sync_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_daily(date);
CREATE INDEX IF NOT EXISTS idx_chunks_status ON sync_chunks(dataset, status);
CREATE INDEX IF NOT EXISTS idx_accruals_date ON finance_accruals(accrual_date);
CREATE INDEX IF NOT EXISTS idx_returns_date ON returns(return_date);
CREATE INDEX IF NOT EXISTS idx_supply_inbound ON supply_orders(inbound);
CREATE INDEX IF NOT EXISTS idx_barcode_ozon_sku ON barcode_catalog(ozon_sku);
CREATE INDEX IF NOT EXISTS idx_wb_sales_date ON wb_sales_daily(date);
CREATE INDEX IF NOT EXISTS idx_wb_finance_date ON wb_finance_lines(report_date);
CREATE INDEX IF NOT EXISTS idx_wb_supply_inbound ON wb_supplies(inbound);
CREATE UNIQUE INDEX IF NOT EXISTS idx_products_product_id
    ON products(product_id) WHERE product_id IS NOT NULL;
"""


def project_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def default_db_path() -> Path:
    override = os.environ.get("OZON_DB_PATH", "").strip()
    if override:
        return Path(override)
    return project_root() / "data" / "ozon.sqlite"


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or default_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    have = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in have:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_column(conn, "sales_daily", "delivered_units", "REAL")
    _add_column(conn, "sales_daily", "returns_units", "REAL")
    _add_column(conn, "sales_daily", "cancellations", "REAL")
    _add_column(conn, "sales_daily", "payload", "TEXT")
    _migrate_unit_costs_to_barcode(conn)
    conn.commit()


def _migrate_unit_costs_to_barcode(conn: sqlite3.Connection) -> None:
    info = {str(row["name"]) for row in conn.execute("PRAGMA table_info(unit_costs)")}
    if not info:
        return
    if "barcode" in info and "sku" not in info:
        return
    if "sku" in info and "barcode" not in info:
        conn.execute(
            """
            CREATE TABLE unit_costs_barcode (
                barcode TEXT PRIMARY KEY,
                cost REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'RUB',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO unit_costs_barcode (barcode, cost, currency, updated_at)
            SELECT sku, cost, currency, updated_at FROM unit_costs
            """
        )
        conn.execute("DROP TABLE unit_costs")
        conn.execute("ALTER TABLE unit_costs_barcode RENAME TO unit_costs")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM sync_meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    return str(row["value"])


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO sync_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def upsert_sale(
    conn: sqlite3.Connection,
    *,
    sku: str,
    day: str,
    revenue: float,
    ordered_units: float,
    fetched_at: str,
    delivered_units: float | None = None,
    returns_units: float | None = None,
    cancellations: float | None = None,
    payload: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO sales_daily (
            sku, date, revenue, ordered_units, delivered_units, returns_units,
            cancellations, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku, date) DO UPDATE SET
            revenue = excluded.revenue,
            ordered_units = excluded.ordered_units,
            delivered_units = excluded.delivered_units,
            returns_units = excluded.returns_units,
            cancellations = excluded.cancellations,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (
            sku,
            day,
            revenue,
            ordered_units,
            delivered_units,
            returns_units,
            cancellations,
            payload,
            fetched_at,
        ),
    )


def upsert_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    operation_date: str | None,
    operation_type: str | None,
    amount: float | None,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO finance_operations (
            operation_id, operation_date, operation_type, amount, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(operation_id) DO UPDATE SET
            operation_date = excluded.operation_date,
            operation_type = excluded.operation_type,
            amount = excluded.amount,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (operation_id, operation_date, operation_type, amount, payload, fetched_at),
    )


def upsert_finance_totals(conn: sqlite3.Connection, month: str, totals: dict, fetched_at: str) -> None:
    conn.execute(
        """
        INSERT INTO finance_totals (
            month, accruals_for_sale, sale_commission, processing_and_delivery,
            refunds_and_cancellations, services_amount, compensation_amount,
            money_transfer, others_amount, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(month) DO UPDATE SET
            accruals_for_sale = excluded.accruals_for_sale,
            sale_commission = excluded.sale_commission,
            processing_and_delivery = excluded.processing_and_delivery,
            refunds_and_cancellations = excluded.refunds_and_cancellations,
            services_amount = excluded.services_amount,
            compensation_amount = excluded.compensation_amount,
            money_transfer = excluded.money_transfer,
            others_amount = excluded.others_amount,
            fetched_at = excluded.fetched_at
        """,
        (
            month,
            float(totals.get("accruals_for_sale") or 0),
            float(totals.get("sale_commission") or 0),
            float(totals.get("processing_and_delivery") or 0),
            float(totals.get("refunds_and_cancellations") or 0),
            float(totals.get("services_amount") or 0),
            float(totals.get("compensation_amount") or 0),
            float(totals.get("money_transfer") or 0),
            float(totals.get("others_amount") or 0),
            fetched_at,
        ),
    )


def upsert_product(
    conn: sqlite3.Connection,
    *,
    sku: str,
    offer_id: str | None,
    product_id: int | None,
    name: str | None,
    fetched_at: str,
) -> None:
    if product_id is not None:
        conn.execute("DELETE FROM products WHERE sku = ?", (f"pending:{product_id}",))
    conn.execute(
        """
        INSERT INTO products (sku, offer_id, product_id, name, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(sku) DO UPDATE SET
            offer_id = COALESCE(excluded.offer_id, products.offer_id),
            product_id = COALESCE(excluded.product_id, products.product_id),
            name = COALESCE(excluded.name, products.name),
            fetched_at = excluded.fetched_at
        """,
        (sku, offer_id, product_id, name, fetched_at),
    )


def remember_listing(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    offer_id: str | None,
    fetched_at: str,
) -> None:
    existing = conn.execute(
        "SELECT sku FROM products WHERE product_id = ?",
        (product_id,),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """
            UPDATE products
            SET offer_id = COALESCE(?, offer_id), fetched_at = ?
            WHERE product_id = ?
            """,
            (offer_id, fetched_at, product_id),
        )
        return
    upsert_product(
        conn,
        sku=f"pending:{product_id}",
        offer_id=offer_id,
        product_id=product_id,
        name=None,
        fetched_at=fetched_at,
    )


def insert_stock_snapshot(
    conn: sqlite3.Connection,
    *,
    sku: str,
    warehouse_id: str,
    warehouse_name: str | None,
    present: int,
    reserved: int | None,
    ozon_idc: float | None,
    turnover_grade: str | None,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO stock_snapshots (
            sku, warehouse_id, warehouse_name, present, reserved, ozon_idc,
            turnover_grade, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku, warehouse_id, fetched_at) DO UPDATE SET
            warehouse_name = excluded.warehouse_name,
            present = excluded.present,
            reserved = excluded.reserved,
            ozon_idc = excluded.ozon_idc,
            turnover_grade = excluded.turnover_grade
        """,
        (sku, warehouse_id, warehouse_name, present, reserved, ozon_idc, turnover_grade, fetched_at),
    )


def set_unit_cost(
    conn: sqlite3.Connection,
    *,
    barcode: str,
    cost: float,
    currency: str,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO unit_costs (barcode, cost, currency, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            cost = excluded.cost,
            currency = excluded.currency,
            updated_at = excluded.updated_at
        """,
        (barcode, cost, currency, updated_at),
    )


def upsert_barcode_catalog(
    conn: sqlite3.Connection,
    *,
    barcode: str,
    ozon_sku: str | None = None,
    nm_id: str | None = None,
    vendor_code: str | None = None,
    name: str | None = None,
    payload: str | None = None,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO barcode_catalog (
            barcode, ozon_sku, nm_id, vendor_code, name, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            ozon_sku = COALESCE(excluded.ozon_sku, barcode_catalog.ozon_sku),
            nm_id = COALESCE(excluded.nm_id, barcode_catalog.nm_id),
            vendor_code = COALESCE(excluded.vendor_code, barcode_catalog.vendor_code),
            name = COALESCE(excluded.name, barcode_catalog.name),
            payload = COALESCE(excluded.payload, barcode_catalog.payload),
            fetched_at = excluded.fetched_at
        """,
        (barcode, ozon_sku, nm_id, vendor_code, name, payload, fetched_at),
    )


def upsert_wb_sale_line(
    conn: sqlite3.Connection,
    *,
    srid: str,
    sale_date: str | None,
    barcode: str | None,
    quantity: float,
    revenue: float,
    is_return: int,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO wb_sales_lines (
            srid, sale_date, barcode, quantity, revenue, is_return, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(srid) DO UPDATE SET
            sale_date = excluded.sale_date,
            barcode = excluded.barcode,
            quantity = excluded.quantity,
            revenue = excluded.revenue,
            is_return = excluded.is_return,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (srid, sale_date, barcode, quantity, revenue, is_return, payload, fetched_at),
    )


def refresh_wb_sales_daily(conn: sqlite3.Connection, *, day: str, fetched_at: str) -> None:
    rows = conn.execute(
        """
        SELECT barcode,
               SUM(CASE WHEN is_return = 0 THEN quantity ELSE 0 END) AS ordered_units,
               SUM(CASE WHEN is_return = 1 THEN quantity ELSE 0 END) AS return_units,
               SUM(revenue) AS revenue
        FROM wb_sales_lines
        WHERE sale_date = ? AND barcode IS NOT NULL
        GROUP BY barcode
        """,
        (day,),
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO wb_sales_daily (
                barcode, date, ordered_units, return_units, revenue, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(barcode, date) DO UPDATE SET
                ordered_units = excluded.ordered_units,
                return_units = excluded.return_units,
                revenue = excluded.revenue,
                fetched_at = excluded.fetched_at
            """,
            (
                row["barcode"],
                day,
                float(row["ordered_units"] or 0),
                float(row["return_units"] or 0),
                float(row["revenue"] or 0),
                fetched_at,
            ),
        )


def upsert_wb_stock_snapshot(
    conn: sqlite3.Connection,
    *,
    barcode: str,
    warehouse_id: str,
    warehouse_name: str | None,
    present: int,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO wb_stock_snapshots (
            barcode, warehouse_id, warehouse_name, present, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode, warehouse_id, fetched_at) DO UPDATE SET
            warehouse_name = excluded.warehouse_name,
            present = excluded.present,
            payload = excluded.payload
        """,
        (barcode, warehouse_id, warehouse_name, present, payload, fetched_at),
    )


def upsert_wb_finance_line(
    conn: sqlite3.Connection,
    *,
    rrd_id: str,
    report_date: str | None,
    barcode: str | None,
    for_pay: float | None,
    doc_type: str | None,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO wb_finance_lines (
            rrd_id, report_date, barcode, for_pay, doc_type, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rrd_id) DO UPDATE SET
            report_date = excluded.report_date,
            barcode = excluded.barcode,
            for_pay = excluded.for_pay,
            doc_type = excluded.doc_type,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (rrd_id, report_date, barcode, for_pay, doc_type, payload, fetched_at),
    )


def upsert_wb_supply(
    conn: sqlite3.Connection,
    *,
    supply_id: str,
    state: str | None,
    inbound: int,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO wb_supplies (supply_id, state, inbound, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(supply_id) DO UPDATE SET
            state = excluded.state,
            inbound = excluded.inbound,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (supply_id, state, inbound, payload, fetched_at),
    )


def upsert_wb_supply_item(
    conn: sqlite3.Connection,
    *,
    supply_id: str,
    barcode: str,
    quantity: float,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO wb_supply_items (supply_id, barcode, quantity, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(supply_id, barcode) DO UPDATE SET
            quantity = excluded.quantity,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (supply_id, barcode, quantity, payload, fetched_at),
    )


def upsert_accrual(
    conn: sqlite3.Connection,
    *,
    accrual_id: str,
    accrual_date: str | None,
    type_id: str | None,
    total_amount: float | None,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO finance_accruals (
            accrual_id, accrual_date, type_id, total_amount, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(accrual_id) DO UPDATE SET
            accrual_date = excluded.accrual_date,
            type_id = excluded.type_id,
            total_amount = excluded.total_amount,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (accrual_id, accrual_date, type_id, total_amount, payload, fetched_at),
    )


def upsert_accrual_type(
    conn: sqlite3.Connection,
    *,
    type_id: str,
    name: str | None,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO finance_accrual_types (type_id, name, payload, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(type_id) DO UPDATE SET
            name = excluded.name,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (type_id, name, payload, fetched_at),
    )


def upsert_price(
    conn: sqlite3.Connection,
    *,
    sku: str,
    price: float | None,
    sales_percent_fbo: float | None,
    sales_percent_fbs: float | None,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO product_prices (
            sku, price, sales_percent_fbo, sales_percent_fbs, payload, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku) DO UPDATE SET
            price = excluded.price,
            sales_percent_fbo = excluded.sales_percent_fbo,
            sales_percent_fbs = excluded.sales_percent_fbs,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (sku, price, sales_percent_fbo, sales_percent_fbs, payload, fetched_at),
    )


def upsert_return(
    conn: sqlite3.Connection,
    *,
    return_id: str,
    sku: str | None,
    quantity: float | None,
    return_date: str | None,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO returns (return_id, sku, quantity, return_date, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(return_id) DO UPDATE SET
            sku = excluded.sku,
            quantity = excluded.quantity,
            return_date = excluded.return_date,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (return_id, sku, quantity, return_date, payload, fetched_at),
    )


def upsert_supply_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    state: str | None,
    inbound: int,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO supply_orders (order_id, state, inbound, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET
            state = excluded.state,
            inbound = excluded.inbound,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (order_id, state, inbound, payload, fetched_at),
    )


def upsert_supply_bundle_item(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    sku: str,
    quantity: float,
    payload: str,
    fetched_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO supply_bundle_items (order_id, sku, quantity, payload, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(order_id, sku) DO UPDATE SET
            quantity = excluded.quantity,
            payload = excluded.payload,
            fetched_at = excluded.fetched_at
        """,
        (order_id, sku, quantity, payload, fetched_at),
    )


def data_as_of(conn: sqlite3.Connection) -> str | None:
    stamps: list[str] = []
    for table in (
        "products",
        "sales_daily",
        "stock_snapshots",
        "finance_totals",
        "finance_operations",
        "finance_accruals",
        "finance_accrual_types",
        "product_prices",
        "returns",
        "supply_orders",
        "supply_bundle_items",
        "barcode_catalog",
        "wb_sales_lines",
        "wb_sales_daily",
        "wb_stock_snapshots",
        "wb_finance_lines",
        "wb_supplies",
        "wb_supply_items",
    ):
        row = conn.execute(f"SELECT MAX(fetched_at) AS ts FROM {table}").fetchone()
        if row and row["ts"]:
            stamps.append(str(row["ts"]))
    if not stamps:
        return None
    return max(stamps)
