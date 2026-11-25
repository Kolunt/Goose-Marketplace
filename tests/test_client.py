import pytest
import httpx

from ozon_seller_mcp.client import OzonApiError, OzonClient


def test_retries_429_using_retry_after():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow"})
        return httpx.Response(200, json={"ok": True})

    slept = []
    client = OzonClient("id", "secret-key", transport=httpx.MockTransport(handler), sleep=slept.append)
    try:
        assert client.post("/v1/analytics/data", {}) == {"ok": True}
    finally:
        client.close()
    assert calls["n"] == 2
    assert slept == [0.0]


def test_error_does_not_echo_api_key():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad date"})

    client = OzonClient("id", "secret-key", transport=httpx.MockTransport(handler), sleep=lambda _seconds: None)
    try:
        with pytest.raises(OzonApiError) as caught:
            client.post("/v1/analytics/data", {})
    finally:
        client.close()
    assert caught.value.status == 400
    assert "secret-key" not in str(caught.value)
    assert "bad date" in str(caught.value)
