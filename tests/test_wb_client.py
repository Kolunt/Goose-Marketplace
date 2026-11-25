import os

import pytest

from ozon_seller_mcp.wb_client import (
    MissingWbCredentials,
    token_for,
    wb_client_from_env,
    wb_category_ready,
)


@pytest.fixture(autouse=True)
def _clear_wb_env(monkeypatch):
    for key in (
        "WB_API_TOKEN",
        "WB_TOKEN",
        "WILDBERRIES_API_TOKEN",
        "WB_STATISTICS_TOKEN",
        "WB_FINANCE_TOKEN",
        "WB_MARKETPLACE_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_token_for_wb_api_token(monkeypatch):
    monkeypatch.setenv("WB_API_TOKEN", "abc")
    assert token_for("statistics") == "abc"


def test_token_for_wb_token_alias(monkeypatch):
    monkeypatch.setenv("WB_TOKEN", "legacy")
    assert token_for("finance") == "legacy"


def test_token_for_specific_over_common(monkeypatch):
    monkeypatch.setenv("WB_API_TOKEN", "common")
    monkeypatch.setenv("WB_FINANCE_TOKEN", "fin")
    assert token_for("finance") == "fin"


def test_token_for_missing_raises():
    with pytest.raises(MissingWbCredentials):
        token_for("statistics")


def test_wb_client_with_only_finance_token(monkeypatch):
    monkeypatch.setenv("WB_FINANCE_TOKEN", "fin-only")
    wb_client_from_env()
    assert wb_category_ready("finance")
    assert not wb_category_ready("statistics")
