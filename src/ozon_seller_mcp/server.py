"""MCP-сервер Goose: инструменты читают локальную базу и при необходимости догружают короткое окно."""

from __future__ import annotations

import logging
import os
import sys
from datetime import date

from mcp.server.mcpserver import MCPServer

from ozon_seller_mcp.client import MissingCredentials, OzonApiError, client_from_env
from ozon_seller_mcp.env_file import dotenv_candidates, load_project_dotenv, project_root
from ozon_seller_mcp.combined_reports import (
    combined_profit_summary_report,
    combined_sales_volume_report,
    combined_stock_forecast_report,
)
from ozon_seller_mcp.db import connect, data_as_of, init_db
from ozon_seller_mcp.db import set_unit_cost as save_unit_cost
from ozon_seller_mcp.policy import HISTORY_HORIZON_DAYS, iso_now, parse_iso_date
from ozon_seller_mcp.reports import (
    profit_forecast_report,
    profit_summary_report,
    sales_volume_report,
    stock_forecast_report,
)
from ozon_seller_mcp.sync import sync_batch, sync_for_read
from ozon_seller_mcp.sync import unfreeze as reopen_chunks
from ozon_seller_mcp.wb_client import (
    MissingWbCredentials,
    WbApiError,
    wb_category_ready,
    wb_client_from_env,
    wb_env_status,
)
from ozon_seller_mcp.wb_reports import (
    wb_profit_forecast_report,
    wb_profit_summary_report,
    wb_sales_volume_report,
    wb_stock_forecast_report,
)

mcp = MCPServer("ozon-seller")


@mcp.tool()
def sync_ozon(horizon_days: int | None = None) -> dict:
    """Догрузить локальную базу Ozon Seller.

    За один вызов делается не больше 5 запросов к API. Год целиком качается
    командой ozon-seller-sync. horizon_days расширяет очередь в прошлое и не
    удаляет уже скачанные строки. По умолчанию горизонт 365 дней.

    Возвращает квитанцию: сколько кусков готово, сколько осталось, data_as_of
    и упёрся ли прогон в минутный или дневной лимит.
    """
    return _sync_tool(horizon_days, marketplaces=("ozon",))


@mcp.tool()
def sync_wb(horizon_days: int | None = None) -> dict:
    """Догрузить локальную базу Wildberries.

    За один вызов не больше 5 запросов. Нужен WB_API_TOKEN с категориями Статистика,
    Финансы и Маркетплейс (или отдельные WB_*_TOKEN).
    """
    return _sync_tool(horizon_days, marketplaces=("wb",))


@mcp.tool()
def sync_marketplaces(horizon_days: int | None = None) -> dict:
    """Догрузить Ozon и WB в одной пачке до 5 HTTP-вызовов."""
    return _sync_tool(horizon_days, marketplaces=("ozon", "wb"))


@mcp.tool()
def sales_volume(date_from: str, date_to: str, group_by: str = "sku") -> dict:
    """Объём продаж из локальной базы.

    date_from и date_to в формате YYYY-MM-DD. group_by: sku, day, week или month.
    В ответе итог за период и топ-20 групп по выручке, плюс доставленные штуки,
    возвраты и отмены, если аналитика их отдала. Если за эти даты ещё нет
    строк, инструмент сам догружает только этот период, не весь год.
    """
    try:
        start, end = _range(date_from, date_to)
    except ValueError as exc:
        return {"error": str(exc)}
    return _with_conn(
        lambda conn, client: _read_then(
            conn,
            client,
            datasets=("sales",),
            overlap=(start.isoformat(), end.isoformat()),
            build=lambda: sales_volume_report(conn, start, end, group_by),
        )
    )


@mcp.tool()
def stock_forecast(lead_time_days: int = 14, safety_days: int = 7) -> dict:
    """Прогноз остатков: дни покрытия, дата нуля и сколько заказать.

    Средние продажи считаются за 28 дней. Формула заказа:
    среднедневные × (срок поставки + страховой запас) − остаток − уже едет.
    При нулевых продажах заказ равен нулю, дни покрытия не считаются.
    В строке есть inbound_qty и число возвратов за 14 дней. Если поставки
    не докачаны, в ответе supplies_pending.
    """
    return _with_conn(lambda conn, client: _stock(conn, client, lead_time_days, safety_days))


@mcp.tool()
def profit_summary(date_from: str, date_to: str) -> dict:
    """Прибыль за период до рекламы.

    Выручка берётся из продаж, прибыль — из суммы total_amount начислений.
    Старые итоги транзакций не используются. Реклама не вычитается.
    Текущая комиссия в ответе — справка по новым продажам, не замена истории.
    Если себестоимость задана не на все SKU, в ответе доля покрытия, а неизвестные
    SKU не подменяются нулём.
    """
    try:
        start, end = _range(date_from, date_to)
    except ValueError as exc:
        return {"error": str(exc)}
    return _with_conn(
        lambda conn, client: _read_then(
            conn,
            client,
            datasets=("sales", "finance_accrual", "finance_accrual_types", "prices"),
            overlap=(start.isoformat(), end.isoformat()),
            build=lambda: profit_summary_report(conn, start, end),
        )
    )


@mcp.tool()
def profit_forecast(horizon_days: int = 30) -> dict:
    """Прогноз прибыли на horizon_days вперёд по последним 28 дням.

    Штуки и прибыль на штуку берутся из локальной базы. Реклама не включена.
    В ответе есть текст допущения.
    """
    if horizon_days <= 0:
        return {"error": "horizon_days должен быть больше нуля"}
    return _with_conn(
        lambda conn, client: _profit_forecast(conn, client, horizon_days)
    )


@mcp.tool()
def wb_sales_volume(date_from: str, date_to: str, group_by: str = "barcode") -> dict:
    """Объём продаж Wildberries по баркоду или дням."""
    try:
        start, end = _range(date_from, date_to)
    except ValueError as exc:
        return {"error": str(exc)}
    return _with_wb_conn(
        lambda conn, ozon_client, wb_client: _read_then(
            conn,
            ozon_client,
            wb_client,
            datasets=("wb_sales",),
            overlap=(start.isoformat(), end.isoformat()),
            build=lambda: wb_sales_volume_report(conn, start, end, group_by),
        )
    )


@mcp.tool()
def wb_stock_forecast(lead_time_days: int = 14, safety_days: int = 7) -> dict:
    """Прогноз остатков Wildberries: FBW + inbound FBS."""
    return _with_wb_conn(
        lambda conn, ozon_client, wb_client: _wb_stock(conn, wb_client, lead_time_days, safety_days)
    )


@mcp.tool()
def wb_profit_summary(date_from: str, date_to: str) -> dict:
    """Прибыль WB до рекламы из finance detailed, не из supplier/sales."""
    try:
        start, end = _range(date_from, date_to)
    except ValueError as exc:
        return {"error": str(exc)}
    return _with_wb_conn(
        lambda conn, ozon_client, wb_client: _read_then(
            conn,
            ozon_client,
            wb_client,
            datasets=("wb_finance", "wb_sales"),
            overlap=(start.isoformat(), end.isoformat()),
            build=lambda: wb_profit_summary_report(conn, start, end),
        )
    )


@mcp.tool()
def wb_profit_forecast(horizon_days: int = 30) -> dict:
    """Прогноз прибыли WB на horizon_days по последним 28 дням."""
    if horizon_days <= 0:
        return {"error": "horizon_days должен быть больше нуля"}
    return _with_wb_conn(
        lambda conn, ozon_client, wb_client: _wb_profit_forecast(conn, wb_client, horizon_days)
    )


@mcp.tool()
def wb_unfreeze(dataset: str, period_start: str, period_end: str) -> dict:
    """Снять заморозку WB: wb_sales или wb_finance."""
    if dataset not in ("wb_sales", "wb_finance"):
        return {"error": "dataset должен быть wb_sales или wb_finance"}
    try:
        start, end = _range(period_start, period_end)
    except ValueError as exc:
        return {"error": str(exc)}
    conn = connect()
    try:
        init_db(conn)
        updated = reopen_chunks(conn, dataset, start.isoformat(), end.isoformat())
        return {
            "dataset": dataset,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "chunks_reopened": updated,
            "data_as_of": data_as_of(conn),
        }
    finally:
        conn.close()


@mcp.tool()
def combined_sales_volume(date_from: str, date_to: str, group_by: str = "barcode") -> dict:
    """Сводка продаж Ozon и WB по баркоду."""
    try:
        start, end = _range(date_from, date_to)
    except ValueError as exc:
        return {"error": str(exc)}
    return _with_both_conn(
        lambda conn, ozon_client, wb_client: _read_then(
            conn,
            ozon_client,
            wb_client,
            datasets=("sales", "wb_sales"),
            overlap=(start.isoformat(), end.isoformat()),
            build=lambda: combined_sales_volume_report(conn, start, end, group_by),
        )
    )


@mcp.tool()
def combined_stock_forecast(lead_time_days: int = 14, safety_days: int = 7) -> dict:
    """Прогноз остатков по баркоду: Ozon + WB суммарно."""
    return _with_both_conn(
        lambda conn, ozon_client, wb_client: _combined_stock(conn, ozon_client, wb_client, lead_time_days, safety_days)
    )


@mcp.tool()
def combined_profit_summary(date_from: str, date_to: str) -> dict:
    """Прибыль Ozon (начисления) + WB (finance) минус COGS по баркоду."""
    try:
        start, end = _range(date_from, date_to)
    except ValueError as exc:
        return {"error": str(exc)}
    return _with_both_conn(
        lambda conn, ozon_client, wb_client: _read_then(
            conn,
            ozon_client,
            wb_client,
            datasets=("finance_accrual", "wb_finance"),
            overlap=(start.isoformat(), end.isoformat()),
            build=lambda: combined_profit_summary_report(conn, start, end),
        )
    )


@mcp.tool()
def set_unit_cost(barcode: str, cost: float, currency: str = "RUB") -> dict:
    """Записать себестоимость по баркоду в локальный справочник.

    Маркетплейсы себестоимость не отдают. cost — закупка за штуку, не меньше нуля.
    """
    if not barcode.strip():
        return {"error": "barcode не может быть пустым"}
    if cost < 0:
        return {"error": "Себестоимость не может быть отрицательной"}
    conn = connect()
    try:
        init_db(conn)
        save_unit_cost(
            conn,
            barcode=barcode.strip(),
            cost=cost,
            currency=currency.strip() or "RUB",
            updated_at=iso_now(),
        )
        conn.commit()
        return {
            "barcode": barcode.strip(),
            "cost": cost,
            "currency": currency.strip() or "RUB",
            "data_as_of": data_as_of(conn),
        }
    finally:
        conn.close()


@mcp.tool()
def check_credentials() -> dict:
    """Проверить, видит ли MCP ключи Ozon и WB (значения токенов не показываются)."""
    loaded = load_project_dotenv()

    def _flag(key: str) -> str:
        raw = os.environ.get(key)
        if raw is None:
            return "missing"
        if not raw.strip():
            return "empty"
        return "set"

    root = project_root()
    candidates = [
        {"path": str(p.resolve()), "exists": p.is_file()} for p in dotenv_candidates()
    ]
    hint = None
    if _flag("WB_API_TOKEN") != "set" and wb_env_status().get("WB_TOKEN") == "set":
        hint = (
            "В окружении есть WB_TOKEN, но нет WB_API_TOKEN. Для Seller API задайте "
            f"WB_API_TOKEN в Goose или в {root / '.env'} (не в домашнем C:\\Users\\…\\.env)."
        )

    return {
        "project_root": str(root),
        "process_cwd": str(os.getcwd()),
        "dotenv_candidates": candidates,
        "dotenv_loaded_from": [str(p) for p in loaded],
        "ozon": {"OZON_CLIENT_ID": _flag("OZON_CLIENT_ID"), "OZON_API_KEY": _flag("OZON_API_KEY")},
        "wb": {
            "variables": wb_env_status(),
            "categories_ready": {
                "statistics": wb_category_ready("statistics"),
                "finance": wb_category_ready("finance"),
                "marketplace": wb_category_ready("marketplace"),
            },
            "hint": hint,
        },
    }


@mcp.tool()
def unfreeze(dataset: str, period_start: str, period_end: str) -> dict:
    """Снять заморозку и поставить период обратно в очередь.

    dataset: sales или finance_accrual. Даты YYYY-MM-DD.
    Нужно, если Ozon задним числом поправил уже закрытый период.
    Следующий sync_ozon или ozon-seller-sync перечитает эти куски.
    """
    if dataset not in ("sales", "finance_accrual"):
        return {"error": "dataset должен быть sales или finance_accrual"}
    try:
        start, end = _range(period_start, period_end)
    except ValueError as exc:
        return {"error": str(exc)}
    conn = connect()
    try:
        init_db(conn)
        updated = reopen_chunks(conn, dataset, start.isoformat(), end.isoformat())
        return {
            "dataset": dataset,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "chunks_reopened": updated,
            "data_as_of": data_as_of(conn),
        }
    finally:
        conn.close()


@mcp.prompt()
def weekly_sales_review() -> str:
    """Разбор продаж за последние 7 дней."""
    return (
        "Посмотри объём продаж Ozon за последние 7 дней по SKU и по дням. "
        "Назови итог, топ товаров и что изменилось по сравнению с предыдущими 7 днями. "
        "Опирайся на data_as_of и не выдумывай строки, которых нет в ответе инструментов."
    )


@mcp.prompt()
def stockout_risk() -> str:
    """Какие товары закончатся раньше поставки."""
    return (
        "Оцени риск обнуления остатков. Используй прогноз остатков со сроком поставки, "
        "который назовёт пользователь, или 14 дней, если срок не назван. "
        "Сначала перечисли товары, у которых дни покрытия меньше срока поставки и страхового запаса."
    )


@mcp.prompt()
def profit_outlook() -> str:
    """Прогноз прибыли на 30 дней."""
    return (
        "Собери прибыль до рекламы за последний закрытый период и прогноз на 30 дней. "
        "Отдельно скажи, учтена ли себестоимость и какая доля SKU ей покрыта. "
        "Если реклама не вычиталась, скажи об этом прямо."
    )


@mcp.prompt()
def weekly_marketplace_review() -> str:
    """Сводка Ozon + WB за неделю по баркоду."""
    return (
        "Сравни продажи Ozon и Wildberries за последние 7 дней по баркоду через combined_sales_volume. "
        "Отметь товары только на одном маркетплейсе и barcode_missing. Опирайся на data_as_of."
    )


def _sync_tool(horizon_days: int | None, *, marketplaces: tuple[str, ...]) -> dict:
    conn = connect()
    try:
        init_db(conn)
        ozon_client = None
        wb_client = None
        try:
            if "ozon" in marketplaces:
                ozon_client = client_from_env()
        except MissingCredentials as exc:
            if marketplaces == ("ozon",):
                return {"error": str(exc)}
        try:
            if "wb" in marketplaces:
                wb_client = wb_client_from_env()
        except MissingWbCredentials as exc:
            if marketplaces == ("wb",):
                return {"error": str(exc)}
        if ozon_client is None and wb_client is None:
            return {"error": "Нет учётных данных Ozon или WB для выбранного sync"}

        def run() -> dict:
            return sync_batch(
                conn,
                ozon_client,
                wb_client=wb_client,
                horizon_days=horizon_days if horizon_days is not None else HISTORY_HORIZON_DAYS,
                refresh_open=True,
                ensure=True,
                marketplaces=marketplaces,
            )

        try:
            if ozon_client and wb_client:
                with ozon_client, wb_client:
                    return run()
            if ozon_client:
                with ozon_client:
                    return run()
            with wb_client:
                return run()
        except (OzonApiError, WbApiError) as exc:
            return {"error": str(exc), "data_as_of": data_as_of(conn)}
    finally:
        conn.close()


def _with_conn(fn):
    conn = connect()
    try:
        init_db(conn)
        client = None
        try:
            client = client_from_env()
        except MissingCredentials as exc:
            report = fn(conn, None)
            if isinstance(report, dict) and _still_pending(report):
                report["sync_skipped"] = str(exc)
            return report
        with client:
            try:
                return fn(conn, client)
            except OzonApiError as exc:
                return {"error": str(exc), "data_as_of": data_as_of(conn)}
    except ValueError as exc:
        return {"error": str(exc)}
    finally:
        conn.close()


def _with_wb_conn(fn):
    conn = connect()
    try:
        init_db(conn)
        try:
            wb_client = wb_client_from_env()
        except MissingWbCredentials as exc:
            report = fn(conn, None, None)
            if isinstance(report, dict) and _still_pending(report):
                report["sync_skipped"] = str(exc)
            return report
        with wb_client:
            try:
                return fn(conn, None, wb_client)
            except WbApiError as exc:
                return {"error": str(exc), "data_as_of": data_as_of(conn)}
    finally:
        conn.close()


def _with_both_conn(fn):
    conn = connect()
    try:
        init_db(conn)
        ozon_client = None
        wb_client = None
        skipped = []
        try:
            ozon_client = client_from_env()
        except MissingCredentials as exc:
            skipped.append(str(exc))
        try:
            wb_client = wb_client_from_env()
        except MissingWbCredentials as exc:
            skipped.append(str(exc))
        if ozon_client is None and wb_client is None:
            return {"error": "; ".join(skipped)}
        if ozon_client and wb_client:
            with ozon_client, wb_client:
                report = fn(conn, ozon_client, wb_client)
        elif ozon_client:
            with ozon_client:
                report = fn(conn, ozon_client, None)
        else:
            with wb_client:
                report = fn(conn, None, wb_client)
        if skipped and isinstance(report, dict):
            report["sync_skipped"] = "; ".join(skipped)
        return report
    except (OzonApiError, WbApiError) as exc:
        return {"error": str(exc), "data_as_of": data_as_of(conn)}
    finally:
        conn.close()


def _read_then(conn, ozon_client, wb_client=None, *, datasets, overlap, build):
    today = date.today()
    sync = None
    if ozon_client is not None or wb_client is not None:
        sync = sync_for_read(
            conn,
            ozon_client,
            wb_client=wb_client,
            today=today,
            datasets=datasets,
            overlap=overlap,
        )
    report = build()
    report["sync"] = sync
    return report


def _wb_stock(conn, wb_client, lead_time_days: int, safety_days: int) -> dict:
    today = date.today()
    window_from = date.fromordinal(today.toordinal() - 27).isoformat()
    sync = None
    if wb_client is not None:
        sync = sync_for_read(
            conn,
            None,
            wb_client=wb_client,
            today=today,
            datasets=("wb_sales", "wb_stocks", "wb_supplies"),
            overlap=(window_from, today.isoformat()),
            max_calls=5,
        )
    report = wb_stock_forecast_report(conn, today, lead_time_days=lead_time_days, safety_days=safety_days)
    report["sync"] = sync
    return report


def _wb_profit_forecast(conn, wb_client, horizon_days: int) -> dict:
    today = date.today()
    start = date.fromordinal(today.toordinal() - 27)
    return _read_then(
        conn,
        None,
        wb_client,
        datasets=("wb_finance", "wb_sales"),
        overlap=(start.isoformat(), today.isoformat()),
        build=lambda: wb_profit_forecast_report(conn, today, horizon_days),
    )


def _combined_stock(conn, ozon_client, wb_client, lead_time_days: int, safety_days: int) -> dict:
    today = date.today()
    window_from = date.fromordinal(today.toordinal() - 27).isoformat()
    sync = None
    if ozon_client is not None:
        sync = sync_for_read(
            conn,
            ozon_client,
            today=today,
            datasets=("sales", "supply_orders", "stocks_turnover", "stocks_warehouse"),
            overlap=(window_from, today.isoformat()),
            max_calls=3,
        )
    if wb_client is not None:
        wb_sync = sync_for_read(
            conn,
            None,
            wb_client=wb_client,
            today=today,
            datasets=("wb_sales", "wb_stocks", "wb_supplies"),
            overlap=(window_from, today.isoformat()),
            max_calls=2 if sync else 5,
        )
        sync = _merge_sync(sync, wb_sync)
    report = combined_stock_forecast_report(
        conn, today, lead_time_days=lead_time_days, safety_days=safety_days
    )
    report["sync"] = sync
    return report


def _stock(conn, client, lead_time_days: int, safety_days: int) -> dict:
    today = date.today()
    start = today.toordinal()
    window_from = date.fromordinal(start - 27).isoformat()
    sync = None
    if client is not None:
        if stock_needs_refresh(conn, today):
            sync = sync_for_read(
                conn,
                client,
                today=today,
                datasets=("stocks_turnover", "stocks_warehouse"),
                overlap=(today.isoformat(), today.isoformat()),
                max_calls=2,
            )
        sales_sync = sync_for_read(
            conn,
            client,
            today=today,
            datasets=("sales", "returns", "supply_orders"),
            overlap=(window_from, today.isoformat()),
            max_calls=3 if sync else 5,
        )
        sync = _merge_sync(sync, sales_sync)
    report = stock_forecast_report(
        conn,
        today,
        lead_time_days=lead_time_days,
        safety_days=safety_days,
    )
    report["sync"] = sync
    return report


def _profit_forecast(conn, client, horizon_days: int) -> dict:
    today = date.today()
    start = date.fromordinal(today.toordinal() - 27)
    return _read_then(
        conn,
        client,
        datasets=("sales", "finance_accrual", "finance_accrual_types", "prices"),
        overlap=(start.isoformat(), today.isoformat()),
        build=lambda: profit_forecast_report(conn, today, horizon_days),
    )


def stock_needs_refresh(conn, today: date) -> bool:
    row = conn.execute("SELECT MAX(fetched_at) AS ts FROM stock_snapshots").fetchone()
    if row is None or not row["ts"]:
        return True
    snapped = date.fromisoformat(str(row["ts"])[:10])
    return (today - snapped).days >= 1


def _still_pending(report: dict) -> bool:
    return bool(
        report.get("range_pending")
        or report.get("sales_window_pending")
        or report.get("stocks_pending")
        or report.get("supplies_pending")
    )


def _merge_sync(first: dict | None, second: dict | None) -> dict | None:
    if first is None:
        return second
    if second is None:
        return first
    return {
        **second,
        "calls_made": int(first["calls_made"]) + int(second["calls_made"]),
        "hit_minute_limit": bool(first["hit_minute_limit"] or second["hit_minute_limit"]),
        "hit_daily_limit": bool(first["hit_daily_limit"] or second["hit_daily_limit"]),
    }


def _range(date_from: str, date_to: str):
    start = parse_iso_date(date_from)
    end = parse_iso_date(date_to)
    if start > end:
        raise ValueError("date_from позже date_to")
    return start, end


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    load_project_dotenv()
    mcp.run()


if __name__ == "__main__":
    main()
