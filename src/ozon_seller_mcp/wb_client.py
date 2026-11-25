"""HTTP-клиент Wildberries Seller API."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Literal

import httpx

WbBase = Literal["statistics", "finance", "marketplace"]

BASE_URLS = {
    "statistics": "https://statistics-api.wildberries.ru",
    "finance": "https://finance-api.wildberries.ru",
    "marketplace": "https://marketplace-api.wildberries.ru",
}


WB_ENV_KEYS = (
    "WB_API_TOKEN",
    "WB_TOKEN",
    "WILDBERRIES_API_TOKEN",
    "WB_STATISTICS_TOKEN",
    "WB_FINANCE_TOKEN",
    "WB_MARKETPLACE_TOKEN",
)


class MissingWbCredentials(Exception):
    """В окружении нет токена WB."""


class WbApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"WB API {status}: {message}")


def _common_wb_token() -> str:
    for key in ("WB_API_TOKEN", "WB_TOKEN", "WILDBERRIES_API_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def wb_env_status() -> dict[str, str]:
    """missing | empty | set — без значений токенов."""
    out: dict[str, str] = {}
    for key in WB_ENV_KEYS:
        if key not in os.environ:
            out[key] = "missing"
        elif not os.environ[key].strip():
            out[key] = "empty"
        else:
            out[key] = "set"
    return out


def wb_has_any_credentials() -> bool:
    return any(status == "set" for status in wb_env_status().values())


def wb_category_ready(category: WbBase) -> bool:
    keys = {
        "statistics": "WB_STATISTICS_TOKEN",
        "finance": "WB_FINANCE_TOKEN",
        "marketplace": "WB_MARKETPLACE_TOKEN",
    }
    if os.environ.get(keys[category], "").strip():
        return True
    return bool(_common_wb_token())


def _missing_wb_message() -> str:
    status = wb_env_status()
    empty = [k for k, v in status.items() if v == "empty"]
    hint = ""
    if empty:
        hint = (
            f" В окружении есть пустые ключи ({', '.join(empty)}) — "
            "удалите их в Goose или заполните значением; иначе .env не подставится."
        )
    return (
        "Нужен WB_API_TOKEN (или WB_TOKEN) либо отдельные WB_STATISTICS_TOKEN, "
        "WB_FINANCE_TOKEN, WB_MARKETPLACE_TOKEN. Токен Seller API: ЛК → Профиль → "
        "Настройки → Доступ к API (категории Статистика, Финансы, Маркетплейс). "
        "Токен раздела «Продвижение» сюда не подходит."
        + hint
    )


def token_for(category: WbBase) -> str:
    keys = {
        "statistics": "WB_STATISTICS_TOKEN",
        "finance": "WB_FINANCE_TOKEN",
        "marketplace": "WB_MARKETPLACE_TOKEN",
    }
    specific = os.environ.get(keys[category], "").strip()
    if specific:
        return specific
    common = _common_wb_token()
    if not common:
        raise MissingWbCredentials(_missing_wb_message())
    return common


class WbClient:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        max_retries: int = 2,
        timeout: float = 120.0,
    ):
        self._sleep = sleep or _default_sleep
        self._max_retries = max_retries
        self._timeout = timeout
        self._transport = transport
        self._clients: dict[WbBase, httpx.Client] = {}

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self) -> WbClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _client(self, base: WbBase) -> httpx.Client:
        if base not in self._clients:
            self._clients[base] = httpx.Client(
                base_url=BASE_URLS[base],
                headers={"Authorization": token_for(base), "Content-Type": "application/json"},
                timeout=self._timeout,
                transport=self._transport,
            )
        return self._clients[base]

    def get(self, base: WbBase, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", base, path, params=params)

    def post(self, base: WbBase, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._request("POST", base, path, json=body or {})

    def _request(
        self,
        method: str,
        base: WbBase,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        client = self._client(base)
        last_error: WbApiError | None = None
        for attempt in range(self._max_retries + 1):
            response = client.request(method, path, params=params, json=json)
            if response.status_code == 204:
                return []
            if response.status_code == 429 and attempt < self._max_retries:
                self._sleep(_retry_after_seconds(response))
                continue
            if response.status_code >= 400:
                last_error = WbApiError(response.status_code, _error_message(response))
                raise last_error
            if not response.content:
                return []
            data = response.json()
            return data
        if last_error is not None:
            raise last_error
        raise WbApiError(429, "Превышен лимит запросов WB")


def wb_client_from_env(**kwargs: Any) -> WbClient:
    if not wb_has_any_credentials():
        raise MissingWbCredentials(_missing_wb_message())
    return WbClient(**kwargs)


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _retry_after_seconds(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "1")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except Exception:
        return response.text[:500]
    if isinstance(data, dict):
        message = data.get("message") or data.get("error") or data
        return str(message)[:500]
    return response.text[:500]
