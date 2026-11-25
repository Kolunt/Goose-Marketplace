from datetime import date

import pytest

from ozon_seller_mcp.policy import (
    chunk_sales_state,
    finance_month_state,
    horizon_start,
    is_history_limit_error,
    iter_finance_months,
    iter_sales_chunks,
    sales_day_state,
)

TODAY = date(2026, 10, 12)


def test_sales_states():
    assert sales_day_state(date(2026, 10, 12), TODAY) == "open"
    assert sales_day_state(date(2026, 9, 28), TODAY) == "open"
    assert sales_day_state(date(2026, 9, 27), TODAY) == "settling"
    assert sales_day_state(date(2026, 8, 28), TODAY) == "settling"
    assert sales_day_state(date(2026, 8, 27), TODAY) == "frozen"


def test_finance_states():
    assert finance_month_state(date(2026, 10, 1), TODAY) == "open"
    assert finance_month_state(date(2026, 9, 1), TODAY) == "settling"
    assert finance_month_state(date(2026, 9, 1), date(2026, 10, 21)) == "frozen"
    assert finance_month_state(date(2026, 8, 1), TODAY) == "frozen"


def test_year_chunks_cover_every_day_once():
    chunks = iter_sales_chunks(TODAY, 365)
    start = horizon_start(TODAY, 365)
    covered = []
    for chunk_start, chunk_end in chunks:
        days = []
        cursor = chunk_start
        while cursor <= chunk_end:
            days.append(cursor)
            cursor = cursor.fromordinal(cursor.toordinal() + 1)
        assert len({sales_day_state(day, TODAY) for day in days}) == 1
        assert chunk_sales_state(chunk_start, chunk_end, TODAY) == sales_day_state(days[0], TODAY)
        covered.extend(days)
    expected = []
    cursor = start
    while cursor <= TODAY:
        expected.append(cursor)
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    assert covered == expected


def test_finance_months_cover_horizon():
    months = iter_finance_months(TODAY, 365)
    assert months[0][0] == horizon_start(TODAY, 365).replace(day=1)
    assert months[-1][0] == date(2026, 10, 1)
    assert months[-1][1] == date(2026, 10, 31)
    for index in range(1, len(months)):
        assert months[index][0].toordinal() == months[index - 1][1].toordinal() + 1


def test_history_error_detection():
    assert is_history_limit_error(400, "date_from is outside the allowed period")
    assert not is_history_limit_error(500, "date_from is outside the allowed period")
    assert not is_history_limit_error(429, "daily request limit")


def test_horizon_rejects_zero():
    with pytest.raises(ValueError):
        horizon_start(TODAY, 0)
