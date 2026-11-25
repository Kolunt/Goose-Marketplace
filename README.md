# Аналитика Ozon и Wildberries для Goose

MCP-расширение для [Goose](https://block.github.io/goose/). Оно забирает данные Ozon Seller и Wildberries, складывает каждый успешный ответ в локальный SQLite целиком (`payload`) и отвечает сводками. В чат не попадают сырые страницы API.

Связь товаров между маркетплейсами — **баркод**. Себестоимость задаётся через `set_unit_cost(barcode, ...)`. Ozon SKU и WB `nmId` попадают в `barcode_catalog` при синхронизации карточек и продаж.

Рекламные API не подключаются. Прибыль — до рекламы.

## Установка

```powershell
uv sync --extra dev
```

```powershell
$env:OZON_CLIENT_ID = "<id кабинета>"
$env:OZON_API_KEY = "<ключ Ozon, Admin read only>"
$env:WB_API_TOKEN = "<токен WB: Статистика + Финансы + Маркетплейс>"
```

Опционально отдельные токены: `WB_STATISTICS_TOKEN`, `WB_FINANCE_TOKEN`, `WB_MARKETPLACE_TOKEN`.

База по умолчанию — `data/ozon.sqlite` (одна база на оба маркетплейса). Путь: `OZON_DB_PATH`.

## Goose STDIO

```text
uv run --directory C:\Users\TBG\Documents\goose-ozon-seller-api ozon-seller-mcp
```

Таймаут **360 с**. В окружение расширения Goose — `OZON_*` и **`WB_API_TOKEN`** (именно так, не `WB_TOKEN` из раздела «Продвижение»).

При старте MCP и CLI читается **только** `.env` в корне этого репозитория (`goose-ozon-seller-api/.env`) или путь из `OZON_SELLER_ENV_FILE`. Файл **`C:\Users\<имя>\.env` не подхватывается** — если токен WB лежит только там под именем `WB_TOKEN`, процесс его не увидит.

**Переменные из Goose не перезаписываются** непустыми значениями из файла. Пустой `WB_API_TOKEN` в Goose можно дополнить из проектного `.env`. Имя для WB Seller API: **`WB_API_TOKEN`** (алиас `WB_TOKEN` только если это тот же seller-токен, не «Продвижение»). После смены настроек — «Сохранить» и новая сессия Goose.

Скопируйте `.env.example` → `.env` в корне репозитория и вставьте токен «Евгений аналитика» как `WB_API_TOKEN=...`. Инструмент **`check_credentials`** покажет `project_root`, какие `.env` искали, `set`/`empty`/`missing` и категории WB.

## Синхронизация

- `sync_ozon` — до 5 запросов Ozon
- `sync_wb` — до 5 запросов WB
- `sync_marketplaces` — общая очередь, до 5 HTTP-вызовов

CLI:

```powershell
uv run ozon-seller-sync --marketplace all
uv run ozon-seller-sync --marketplace ozon --horizon-days 365
uv run ozon-seller-sync --marketplace wb --max-calls 20
```

## Инструменты Ozon (без изменений имён)

`sales_volume`, `stock_forecast`, `profit_summary`, `profit_forecast`, `unfreeze` — только Ozon.

## Инструменты Wildberries

- `wb_sales_volume` — по баркоду/дням (история sales ≤ **90 дней**)
- `wb_stock_forecast` — остатки FBW (`supplier/stocks`) + **inbound FBS** из поставок Marketplace
- `wb_profit_summary` / `wb_profit_forecast` — из `finance/v1/sales-reports/detailed` (`forPay`), не из supplier/sales
- `wb_unfreeze` — `wb_sales` или `wb_finance`

## Сводка по баркоду

- `combined_sales_volume` — `group_by=barcode`, колонки `ozon_*`, `wb_*`, `total_*`; строки без баркода Ozon помечены `barcode_missing`
- `combined_stock_forecast` — суммарный остаток и заказ по баркоду
- `combined_profit_summary` — начисления Ozon + finance WB − COGS по баркоду

`set_unit_cost` принимает **barcode**. Старые записи по SKU мигрируют в barcode при `init_db`.

## Лимиты

- Ozon analytics: 1 req/min
- WB statistics/finance: 1 req/min (пауза в `sync_meta`)
- WB sales в API хранятся около 90 дней — в combined-отчётах поле `wb_sales_limited_to_days`

## Проверка

```powershell
uv run pytest
```

Без вызовов `api-seller.ozon.ru` и хостов WB.

## Методы API

**Ozon:** analytics, accruals, prices, returns, supply-order v2, stocks — как в предыдущей версии README.

**Wildberries:**

- `GET statistics-api.../api/v1/supplier/sales`
- `GET statistics-api.../api/v1/supplier/stocks`
- `POST finance-api.../api/finance/v1/sales-reports/detailed`
- `GET marketplace-api.../api/v3/supplies` (+ детали и заказы поставки)
