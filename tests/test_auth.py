from conftest import register_user


def test_register_success(client):
    resp = register_user(client)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True

    with client.session_transaction() as sess:
        assert "user_id" in sess


def test_register_duplicate_username(client):
    register_user(client, email="first@example.com")
    resp = register_user(client, email="second@example.com")
    assert resp.status_code == 409
    assert resp.get_json()["ok"] is False


def test_register_duplicate_email(client):
    register_user(client, username="userA")
    resp = register_user(client, username="userB")
    assert resp.status_code == 409


def test_register_missing_fields(client):
    resp = client.post("/register", json={"username": "", "email": "", "password": ""})
    assert resp.status_code == 400


def test_login_success(client):
    register_user(client)
    client.get("/logout")
    resp = client.post(
        "/login", json={"email": "trader1@example.com", "password": "hunter2pass"}
    )
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_login_wrong_password(client):
    register_user(client)
    client.get("/logout")
    resp = client.post(
        "/login", json={"email": "trader1@example.com", "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert resp.get_json()["ok"] is False


def test_login_nonexistent_user(client):
    resp = client.post(
        "/login", json={"email": "nobody@example.com", "password": "whatever"}
    )
    assert resp.status_code == 401


def test_logout_clears_session(client):
    register_user(client)
    client.get("/logout")
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_login_required_redirects_when_unauthenticated(client):
    resp = client.get("/api/me")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_dashboard_requires_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302
