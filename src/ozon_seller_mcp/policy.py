"""Правила горизонта, заморозки и классификация ошибок API. Сети здесь нет."""

from __future__ import annotations

from datetime import date, datetime, timedelta

HISTORY_HORIZON_DAYS = 365
SALES_OPEN_DAYS = 14
SALES_FREEZE_AFTER_DAYS = 45
SALES_SETTLING_MAX_AGE_DAYS = 7
FINANCE_SETTLE_UNTIL_DAY = 20
SALES_CHUNK_DAYS = 7
ANALYTICS_PAGE_LIMIT = 1000
SYNC_BATCH_API_CALLS = 5
ANALYTICS_MIN_INTERVAL_SEC = 60
STOCK_SNAPSHOT_MAX_AGE_DAYS = 1
WAREHOUSE_BATCH_SIZE = 100
ACCRUAL_PAGE_LIMIT = 50
ACCRUAL_CURSOR_TTL_SEC = 600
TYPES_REFRESH_DAYS = 7
RETURNS_WINDOW_DAYS = 14
PRICE_PAGE_LIMIT = 100
SUPPLY_LIST_LIMIT = 100
SUPPLY_GET_LIMIT = 50
WB_SALES_HISTORY_DAYS = 90
WB_STATS_MIN_INTERVAL_SEC = 60
WB_FINANCE_CHUNK_DAYS = 7
WB_FINANCE_CURSOR_TTL_SEC = 600

SALES_DATASETS = ("sales",)
FINANCE_DATASETS = ("finance_totals", "finance_ops")
CATALOG_DATASETS = ("products", "stocks_turnover", "stocks_warehouse")

FINANCE_COMPONENT_KEYS = (
    "accruals_for_sale",
    "sale_commission",
    "processing_and_delivery",
    "refunds_and_cancellations",
    "services_amount",
    "compensation_amount",
    "others_amount",
)


def sales_day_state(day: date, today: date) -> str:
    """open — последние 14 дней, settling — 15–45, frozen — старше 45."""
    age = (today - day).days
    if age <= SALES_OPEN_DAYS:
        return "open"
    if age <= SALES_FREEZE_AFTER_DAYS:
        return "settling"
    return "frozen"


def iter_dates(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def chunk_sales_state(period_start: date, period_end: date, today: date) -> str:
    states = {sales_day_state(day, today) for day in iter_dates(period_start, period_end)}
    if "open" in states:
        return "open"
    if "settling" in states:
        return "settling"
    return "frozen"


def finance_month_state(month_start: date, today: date) -> str:
    """Текущий месяц open, прошлый до 20-го числа следующего — settling, остальные frozen."""
    current = today.replace(day=1)
    if month_start >= current:
        return "open"
    previous = (current - timedelta(days=1)).replace(day=1)
    if month_start == previous:
        if today.day <= FINANCE_SETTLE_UNTIL_DAY:
            return "settling"
        return "frozen"
    return "frozen"


def month_end(day: date) -> date:
    if day.month == 12:
        next_month = date(day.year + 1, 1, 1)
    else:
        next_month = date(day.year, day.month + 1, 1)
    return next_month - timedelta(days=1)


def horizon_start(today: date, horizon_days: int) -> date:
    if horizon_days <= 0:
        raise ValueError("horizon_days должен быть больше нуля")
    if horizon_days > 3660:
        raise ValueError("horizon_days не больше 3660")
    return today - timedelta(days=horizon_days)


def iter_sales_chunks(today: date, horizon_days: int) -> list[tuple[date, date]]:
    """Нарезает горизонт на куски до 7 дней, не пересекая границу состояния."""
    start = horizon_start(today, horizon_days)
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= today:
        state = sales_day_state(cursor, today)
        window_end = cursor
        while window_end < today:
            nxt = window_end + timedelta(days=1)
            if sales_day_state(nxt, today) != state:
                break
            if (window_end - cursor).days + 1 >= SALES_CHUNK_DAYS:
                break
            window_end = nxt
        chunks.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return chunks


def iter_finance_months(today: date, horizon_days: int) -> list[tuple[date, date]]:
    start = horizon_start(today, horizon_days).replace(day=1)
    months: list[tuple[date, date]] = []
    cursor = start
    while cursor <= today:
        end = month_end(cursor)
        months.append((cursor, end))
        cursor = end + timedelta(days=1)
    return months


def status_after_success(state: str) -> str:
    if state == "frozen":
        return "frozen"
    return "done"


def settling_is_stale(synced_at: str | None, today: date) -> bool:
    if not synced_at:
        return True
    synced_day = date.fromisoformat(synced_at[:10])
    return (today - synced_day).days >= SALES_SETTLING_MAX_AGE_DAYS


def types_are_stale(synced_at: str | None, today: date) -> bool:
    if not synced_at:
        return True
    synced_day = date.fromisoformat(synced_at[:10])
    return (today - synced_day).days >= TYPES_REFRESH_DAYS


def ranges_overlap(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return start_a <= end_b and start_b <= end_a


def is_history_limit_error(status: int, message: str) -> bool:
    text = (message or "").lower()
    needles = (
        "premium",
        "too old",
        "date_from",
        "максимальн",
        "период",
        "глубин",
        "subscription",
        "подписк",
        "outside",
    )
    return status in (400, 403) and any(needle in text for needle in needles)


def is_metric_rejected(status: int, message: str) -> bool:
    """400 из-за метрики аналитики, а не из-за слишком старой даты."""
    if status != 400 or is_history_limit_error(status, message):
        return False
    text = (message or "").lower()
    needles = ("metric", "метрик", "delivered_units", "cancellations")
    return any(needle in text for needle in needles)


def is_daily_limit_error(status: int, message: str) -> bool:
    text = (message or "").lower()
    needles = ("daily", "per day", "сут", "в день", "лимит запросов", "request limit")
    return status in (400, 403, 429) and any(needle in text for needle in needles)


def classify_api_error(status: int, message: str) -> str:
    if is_history_limit_error(status, message):
        return "history"
    if is_daily_limit_error(status, message):
        return "daily"
    if status == 429:
        return "rate"
    return "error"


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValueError(f"Дата должна быть в формате YYYY-MM-DD, получено: {value}") from exc


def utc_bounds(start: date, end: date) -> tuple[str, str]:
    return (
        f"{start.isoformat()}T00:00:00.000Z",
        f"{end.isoformat()}T23:59:59.999Z",
    )


def iso_now(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    return current.replace(microsecond=0).isoformat()
