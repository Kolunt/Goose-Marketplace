"""Полная догрузка очереди. Год не укладывается в один вызов инструмента Goose."""

from __future__ import annotations

import argparse
import json
import sys

from ozon_seller_mcp.client import MissingCredentials, client_from_env
from ozon_seller_mcp.env_file import load_project_dotenv
from ozon_seller_mcp.wb_client import MissingWbCredentials, wb_client_from_env
from ozon_seller_mcp.db import connect, init_db
from ozon_seller_mcp.policy import HISTORY_HORIZON_DAYS
from ozon_seller_mcp.sync import sync_until_idle


def main(argv: list[str] | None = None) -> int:
    load_project_dotenv()
    parser = argparse.ArgumentParser(
        description="Догрузить локальную базу Ozon Seller до конца очереди или лимита API."
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=HISTORY_HORIZON_DAYS,
        help="Сколько дней истории поставить в очередь. По умолчанию 365.",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Остановиться после N запросов. По умолчанию очередь идёт, пока не опустеет.",
    )
    parser.add_argument(
        "--marketplace",
        choices=("ozon", "wb", "all"),
        default="all",
        help="Какой маркетплейс догружать: ozon, wb или all (по умолчанию).",
    )
    args = parser.parse_args(argv)
    if args.horizon_days <= 0:
        print("horizon-days должен быть больше нуля", file=sys.stderr)
        return 2
    if args.max_calls is not None and args.max_calls <= 0:
        print("max-calls должен быть больше нуля", file=sys.stderr)
        return 2
    marketplaces = {"ozon": ("ozon",), "wb": ("wb",), "all": ("ozon", "wb")}[args.marketplace]
    ozon_client = None
    wb_client = None
    try:
        if "ozon" in marketplaces:
            ozon_client = client_from_env()
    except MissingCredentials as exc:
        if args.marketplace == "ozon":
            print(str(exc), file=sys.stderr)
            return 1
    try:
        if "wb" in marketplaces:
            wb_client = wb_client_from_env()
    except MissingWbCredentials as exc:
        if args.marketplace == "wb":
            print(str(exc), file=sys.stderr)
            return 1
    if ozon_client is None and wb_client is None:
        print("Нет учётных данных Ozon или WB", file=sys.stderr)
        return 1
    conn = connect()
    try:
        init_db(conn)
        if ozon_client and wb_client:
            with ozon_client, wb_client:
                receipt = sync_until_idle(
                    conn,
                    ozon_client,
                    wb_client=wb_client,
                    horizon_days=args.horizon_days,
                    max_calls=args.max_calls,
                    marketplaces=marketplaces,
                )
        elif ozon_client:
            with ozon_client:
                receipt = sync_until_idle(
                    conn,
                    ozon_client,
                    horizon_days=args.horizon_days,
                    max_calls=args.max_calls,
                    marketplaces=marketplaces,
                )
        else:
            with wb_client:
                receipt = sync_until_idle(
                    conn,
                    None,
                    wb_client=wb_client,
                    horizon_days=args.horizon_days,
                    max_calls=args.max_calls,
                    marketplaces=marketplaces,
                )
    finally:
        conn.close()
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
