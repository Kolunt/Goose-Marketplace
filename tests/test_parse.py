from ozon_seller_mcp.parse import analytics_request, parse_sales_rows


def test_metrics_follow_request_order():
    request = analytics_request("2026-01-01", "2026-01-07", 0, 1000)
    assert request["metrics"] == [
        "revenue",
        "ordered_units",
        "delivered_units",
        "returns",
        "cancellations",
    ]
    assert request["dimension"] == ["sku", "day"]
    payload = {
        "result": {
            "data": [
                {
                    "dimensions": [
                        {"id": "55", "name": "Кружка"},
                        {"id": "2026-01-03", "name": "2026-01-03"},
                    ],
                    "metrics": [12.5, 4],
                }
            ],
            "totals": [12.5, 4],
        }
    }
    rows = parse_sales_rows(payload)
    assert rows == [
        {
            "sku": "55",
            "date": "2026-01-03",
            "revenue": 12.5,
            "ordered_units": 4.0,
            "name": "Кружка",
        }
    ]
