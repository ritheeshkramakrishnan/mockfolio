from conftest import register_user


def test_api_me_shape(client):
    register_user(client)
    resp = client.get("/api/me")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in (
        "username", "email", "balance", "phase", "total_pnl",
        "positions", "recent_trades", "starting_balance",
        "profit_target", "profit_target_pct",
    ):
        assert key in body
    assert body["username"] == "trader1"
    assert body["balance"] == 50_000.0


def test_portfolio_history_defaults_to_starting_balance(client):
    register_user(client)
    resp = client.get("/api/portfolio/history")
    assert resp.status_code == 200
    body = resp.get_json()
    assert isinstance(body, list)
    assert body[0]["value"] == 50_000.0


def test_positions_empty_by_default(client):
    register_user(client)
    resp = client.get("/api/positions")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_market_status_shape(client):
    register_user(client)
    resp = client.get("/api/market/status")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("open", "status", "label", "weekday", "et_time"):
        assert key in body


def test_autopilot_status_defaults_disabled(client):
    register_user(client)
    resp = client.get("/api/autopilot/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enabled"] is False
    assert body["logs"] == []
