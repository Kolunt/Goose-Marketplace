"""HTTP-клиент Seller API. Секреты остаются в заголовках и не попадают в текст ошибок."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import httpx

BASE_URL = "https://api-seller.ozon.ru"


class MissingCredentials(Exception):
    """В окружении нет пары Client-Id и Api-Key."""


class OzonApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"Ozon API {status}: {message}")


def credentials_from_env() -> tuple[str, str]:
    client_id = os.environ.get("OZON_CLIENT_ID", "").strip()
    api_key = os.environ.get("OZON_API_KEY", "").strip()
    if not client_id or not api_key:
        raise MissingCredentials(
            "Нужны переменные OZON_CLIENT_ID и OZON_API_KEY. "
            "Ключ создаётся в кабинете: Настройки → Seller API. Для аналитики достаточно ключа на чтение."
        )
    return client_id, api_key


class OzonClient:
    def __init__(
        self,
        client_id: str,
        api_key: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] | None = None,
        max_retries: int = 2,
        timeout: float = 60.0,
    ):
        self._sleep = sleep or _default_sleep
        self._max_retries = max_retries
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers={
                "Client-Id": client_id,
                "Api-Key": api_key,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OzonClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def post(self, path: str, body: dict[str, Any]) -> dict:
        last_error: OzonApiError | None = None
        for attempt in range(self._max_retries + 1):
            response = self._client.post(path, json=body)
            if response.status_code == 429 and attempt < self._max_retries:
                retry_after = _retry_after_seconds(response)
                self._sleep(retry_after)
                continue
            if response.status_code >= 400:
                last_error = OzonApiError(response.status_code, _error_message(response))
                raise last_error
            data = response.json()
            if not isinstance(data, dict):
                raise OzonApiError(response.status_code, "Ответ Ozon не является JSON-объектом")
            return data
        if last_error is not None:
            raise last_error
        raise OzonApiError(429, "Превышен лимит запросов Ozon")


def client_from_env(**kwargs: Any) -> OzonClient:
    client_id, api_key = credentials_from_env()
    return OzonClient(client_id, api_key, **kwargs)


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
