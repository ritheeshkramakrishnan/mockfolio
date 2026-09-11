from conftest import register_user


def test_watchlist_empty_by_default(client):
    register_user(client)
    resp = client.get("/api/watchlist")
    assert resp.status_code == 200
    assert resp.get_json() == []


def test_add_and_list_watchlist_symbol(client):
    register_user(client)
    resp = client.post("/api/watchlist", json={"symbol": "tsla"})
    assert resp.status_code == 200
    assert resp.get_json()["symbol"] == "TSLA"

    resp = client.get("/api/watchlist")
    assert resp.get_json() == ["TSLA"]


def test_add_watchlist_requires_symbol(client):
    register_user(client)
    resp = client.post("/api/watchlist", json={"symbol": ""})
    assert resp.status_code == 400


def test_add_duplicate_symbol_is_idempotent(client):
    register_user(client)
    client.post("/api/watchlist", json={"symbol": "NVDA"})
    client.post("/api/watchlist", json={"symbol": "NVDA"})
    resp = client.get("/api/watchlist")
    assert resp.get_json() == ["NVDA"]


def test_remove_watchlist_symbol(client):
    register_user(client)
    client.post("/api/watchlist", json={"symbol": "SPY"})
    resp = client.delete("/api/watchlist/SPY")
    assert resp.status_code == 200
    assert client.get("/api/watchlist").get_json() == []


def test_watchlist_requires_login(client):
    resp = client.get("/api/watchlist")
    assert resp.status_code == 302
