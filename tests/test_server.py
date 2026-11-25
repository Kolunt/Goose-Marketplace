def test_tools_and_prompts_are_registered():
    from ozon_seller_mcp.server import mcp

    tool_names = set(mcp._tool_manager._tools)
    assert {
        "sync_ozon",
        "sync_wb",
        "sync_marketplaces",
        "sales_volume",
        "wb_sales_volume",
        "combined_sales_volume",
        "stock_forecast",
        "profit_summary",
        "profit_forecast",
        "set_unit_cost",
        "unfreeze",
        "wb_unfreeze",
    } <= tool_names
    prompt_names = set(mcp._prompt_manager._prompts)
    assert {"weekly_sales_review", "stockout_risk", "profit_outlook"} <= prompt_names


def test_set_unit_cost_writes_local_row(tmp_path, monkeypatch):
    monkeypatch.setenv("OZON_DB_PATH", str(tmp_path / "costs.sqlite"))
    monkeypatch.delenv("OZON_CLIENT_ID", raising=False)
    monkeypatch.delenv("OZON_API_KEY", raising=False)
    from ozon_seller_mcp.server import set_unit_cost

    result = set_unit_cost("460000111", 12.5)
    assert result["barcode"] == "460000111"
    assert result["cost"] == 12.5
    from ozon_seller_mcp.db import connect

    connection = connect(tmp_path / "costs.sqlite")
    row = connection.execute("SELECT cost FROM unit_costs WHERE barcode = '460000111'").fetchone()
    connection.close()
    assert row["cost"] == 12.5
