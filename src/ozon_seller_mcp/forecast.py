"""Расчёт остатков и прибыли по уже собранным числам."""

from __future__ import annotations

import math
from datetime import date, timedelta

from ozon_seller_mcp.policy import FINANCE_COMPONENT_KEYS, month_end

ASSUMPTION = (
    "Прогноз считает, что среднедневные продажи и прибыль на штуку последних 28 дней сохранятся. "
    "Реклама не включена. Дни без продаж внутри окна считаются нулём."
)


def marketplace_profit(totals: dict) -> float:
    """Сумма начислений без money_transfer: выплата Ozon не равна прибыли."""
    return sum(float(totals.get(key) or 0) for key in FINANCE_COMPONENT_KEYS)


def days_of_cover(present: float, avg_daily: float) -> float | None:
    if avg_daily <= 0:
        return None
    return present / avg_daily


def stockout_on(today: date, cover: float | None) -> str | None:
    if cover is None:
        return None
    return (today + timedelta(days=int(cover))).isoformat()


def order_quantity(
    avg_daily: float,
    present: float,
    lead_time_days: int,
    safety_days: int,
    inbound: float = 0,
) -> int:
    """Сколько заказать, чтобы покрыть срок поставки и страховой запас. Дробь округляется вверх."""
    if avg_daily <= 0:
        return 0
    raw = avg_daily * (lead_time_days + safety_days) - present - inbound
    if raw <= 0:
        return 0
    return math.ceil(raw - 1e-9)


def prorate_amount(
    amount: float,
    month_start: date,
    range_start: date,
    range_end: date,
) -> float:
    end = month_end(month_start)
    overlap_start = max(month_start, range_start)
    overlap_end = min(end, range_end)
    if overlap_end < overlap_start:
        return 0.0
    overlap_days = (overlap_end - overlap_start).days + 1
    month_days = (end - month_start).days + 1
    return amount * overlap_days / month_days


def month_is_fully_inside(month_start: date, range_start: date, range_end: date) -> bool:
    end = month_end(month_start)
    return month_start >= range_start and end <= range_end


def cost_coverage(units_by_sku: dict[str, float], costs: dict[str, float]) -> dict:
    total_units = sum(units_by_sku.values())
    known_units = sum(units for sku, units in units_by_sku.items() if sku in costs)
    cogs = sum(units_by_sku[sku] * costs[sku] for sku in costs if sku in units_by_sku)
    if total_units <= 0:
        coverage = None
    else:
        coverage = known_units / total_units
    if not costs:
        note = "Себестоимость не задана. Прибыль посчитана до закупки и до рекламы."
        return {
            "unit_cost_coverage": 0.0 if total_units else None,
            "cogs_known": None,
            "includes_cogs": False,
            "note": note,
            "cogs_applied": 0.0,
        }
    if known_units <= 0:
        return {
            "unit_cost_coverage": 0.0 if total_units else None,
            "cogs_known": 0.0,
            "includes_cogs": False,
            "note": "Себестоимость задана, но не на проданные SKU. Закупка не вычтена. Прибыль до рекламы.",
            "cogs_applied": 0.0,
        }
    if coverage is not None and coverage < 1:
        note = (
            f"Себестоимость покрывает {coverage:.0%} проданных штук. "
            "Неизвестные SKU в закупку не включены. Прибыль до рекламы."
        )
    else:
        note = "Прибыль после себестоимости и до рекламы."
    return {
        "unit_cost_coverage": coverage,
        "cogs_known": cogs,
        "includes_cogs": coverage == 1,
        "note": note,
        "cogs_applied": cogs,
    }


def apply_cogs(net: float, coverage_info: dict) -> float:
    return net - float(coverage_info["cogs_applied"])


def build_profit_forecast(
    *,
    units_28: float,
    net_28: float,
    units_by_sku: dict[str, float],
    costs: dict[str, float],
    horizon_days: int,
    profit_basis: str,
) -> dict:
    coverage_info = cost_coverage(units_by_sku, costs)
    profit_28 = apply_cogs(net_28, coverage_info)
    avg_daily = units_28 / 28 if units_28 else 0.0
    forecast_units = avg_daily * horizon_days
    if units_28 <= 0:
        profit_per_unit = None
        forecast_profit = 0.0
    else:
        profit_per_unit = profit_28 / units_28
        forecast_profit = profit_per_unit * forecast_units
    return {
        "horizon_days": horizon_days,
        "units_28": units_28,
        "avg_daily_units": avg_daily,
        "forecast_units": forecast_units,
        "marketplace_profit_28": net_28,
        "profit_28": profit_28,
        "profit_per_unit": profit_per_unit,
        "forecast_profit": forecast_profit,
        "profit_basis": profit_basis,
        "unit_cost_coverage": coverage_info["unit_cost_coverage"],
        "cogs_known": coverage_info["cogs_known"],
        "includes_cogs": coverage_info["includes_cogs"],
        "before_ads": True,
        "assumption": ASSUMPTION,
        "note": coverage_info["note"],
    }
