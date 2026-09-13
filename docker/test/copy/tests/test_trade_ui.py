import json

import httpx
from fastapi.testclient import TestClient


def test_ui_serves_dashboard_and_only_proxies_allowed_gets(monkeypatch):
    import trade_ui_service.main as ui

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer server-only-token"
        assert "ignored" not in request.url.params
        return httpx.Response(200, json={"items": [], "next_cursor": None})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(ui, "_api_token", lambda: "server-only-token")
    monkeypatch.setattr(
        ui.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, timeout=kwargs.get("timeout")),
    )

    with TestClient(ui.app) as client:
        page = client.get("/")
        history = client.get("/ui-api/history/orders?ignored=value")
        current = client.get("/ui-api/current-positions?exchange_id=binance&symbol=BTC%2FUSDT&ignored=value")
        mutation = client.post("/ui-api/history/orders", json={})

    assert page.status_code == 200
    assert "Trade Ledger" in page.text
    assert "現在値を更新" in page.text
    assert history.status_code == 200
    assert current.status_code == 200
    assert mutation.status_code == 405
    assert "server-only-token" not in page.text
    assert "server-only-token" not in history.text
    assert len(seen) == 2
    assert seen[1].url.path == "/api/v1/current-positions"
    assert seen[1].url.params["exchange_id"] == "binance"
    assert seen[1].url.params["symbol"] == "BTC/USDT"


def test_ui_rejects_invalid_order_id_without_calling_upstream(monkeypatch):
    import trade_ui_service.main as ui

    monkeypatch.setattr(ui, "_proxy_get", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))
    with TestClient(ui.app) as client:
        response = client.get("/ui-api/orders/bad$id/fills")
    assert response.status_code == 422


def test_ui_only_proxies_valid_trading_mutations(monkeypatch):
    import trade_ui_service.main as ui

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer server-only-token"
        if request.url.path.endswith("/cancel"):
            return httpx.Response(202, json={"id": "order-1"})
        if request.url.path == "/api/v1/positions/close":
            return httpx.Response(202, json={"items": []})
        if request.url.path == "/api/v1/close-only":
            return httpx.Response(
                200,
                json={
                    "kill_switch": False,
                    "close_only": True,
                    "reason": "test",
                    "updated_at": "2026-07-28T00:00:00Z",
                },
            )
        return httpx.Response(202, json={"id": "order-1"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(ui, "_api_token", lambda: "server-only-token")
    monkeypatch.setattr(
        ui.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=transport, timeout=kwargs.get("timeout")),
    )

    order = {
        "request_id": "manual-1",
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.01",
        "reduce_only": False,
    }
    with TestClient(ui.app) as client:
        created = client.post("/ui-api/orders", json=order)
        closed = client.post(
            "/ui-api/positions/close",
            json={
                "request_id": "close-1",
                "exchange_id": "binance",
                "symbol": "BTC/USDT",
            },
        )
        controlled = client.post(
            "/ui-api/close-only",
            json={"enabled": True, "reason": "test"},
        )
        canceled = client.post("/ui-api/orders/order-1/cancel")
        invalid_origin = client.post(
            "/ui-api/orders",
            json=order,
            headers={"Origin": "https://example.invalid"},
        )
        invalid_order = client.post("/ui-api/orders/bad$id/cancel")

    assert created.status_code == 202
    assert closed.status_code == 202
    assert controlled.status_code == 200
    assert canceled.status_code == 202
    assert invalid_origin.status_code == 403
    assert invalid_order.status_code == 422
    assert [request.url.path for request in seen] == [
        "/api/v1/orders",
        "/api/v1/positions/close",
        "/api/v1/close-only",
        "/api/v1/orders/order-1/cancel",
    ]
    assert json.loads(seen[0].read())["strategy_id"] == "manual"
    assert json.loads(seen[1].read())["strategy_id"] == "manual"


def test_ui_contains_trading_controls_without_chart():
    import trade_ui_service.main as ui

    with TestClient(ui.app) as client:
        page = client.get("/")

    assert page.status_code == 200
    assert "手動注文" in page.text
    assert "Close-onlyを有効化" in page.text
    assert "表示取引所を全決済" in page.text
    assert "pnl-chart" not in page.text
