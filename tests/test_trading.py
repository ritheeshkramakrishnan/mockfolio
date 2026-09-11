import pytest

from mockfolio import market
from conftest import register_user


@pytest.fixture(autouse=True)
def _fixed_price(monkeypatch):
    """Trades hit live/EOD/simulated price sources over the network — pin a
    deterministic price so tests are fast and don't depend on connectivity."""
    monkeypatch.setattr(market, "_get_price", lambda symbol: (100.0, "test"))


def test_buy_deducts_balance_and_opens_position(client):
    register_user(client)
    resp = client.post("/api/trade", json={"symbol": "AAPL", "qty": 10, "side": "buy"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["new_balance"] == pytest.approx(50_000.0 - 1_000.0)

    positions = client.get("/api/positions").get_json()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["qty"] == 10


def test_buy_insufficient_balance_rejected(client):
    register_user(client)
    resp = client.post("/api/trade", json={"symbol": "AAPL", "qty": 100_000, "side": "buy"})
    assert resp.status_code == 400
    assert "Insufficient" in resp.get_json()["error"]


def test_invalid_symbol_or_qty_rejected(client):
    register_user(client)
    resp = client.post("/api/trade", json={"symbol": "", "qty": 10, "side": "buy"})
    assert resp.status_code == 400

    resp = client.post("/api/trade", json={"symbol": "AAPL", "qty": 0, "side": "buy"})
    assert resp.status_code == 400


def test_sell_without_position_rejected(client):
    register_user(client)
    resp = client.post("/api/trade", json={"symbol": "AAPL", "qty": 5, "side": "sell"})
    assert resp.status_code == 400
    assert "Not enough shares" in resp.get_json()["error"]


def test_buy_then_sell_realizes_pnl(client, monkeypatch):
    register_user(client)
    client.post("/api/trade", json={"symbol": "AAPL", "qty": 10, "side": "buy"})

    monkeypatch.setattr(market, "_get_price", lambda symbol: (110.0, "test"))
    resp = client.post("/api/trade", json={"symbol": "AAPL", "qty": 10, "side": "sell"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pnl"] == pytest.approx(100.0)  # (110 - 100) * 10

    positions = client.get("/api/positions").get_json()
    assert positions == []


def test_trade_requires_login(client):
    resp = client.post("/api/trade", json={"symbol": "AAPL", "qty": 1, "side": "buy"})
    assert resp.status_code == 302
