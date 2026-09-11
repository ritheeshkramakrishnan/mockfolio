import hashlib

import pytest

import app as app_module
from mockfolio.blueprints.auth import hash_pw, verify_password
from mockfolio.db import get_db
from conftest import register_user


def test_new_passwords_use_werkzeug_hash_not_plain_sha256(client):
    register_user(client, email="hash@example.com")
    with app_module.app.app_context():
        db = get_db()
        user = db.execute("SELECT password FROM users WHERE email=?", ("hash@example.com",)).fetchone()
    stored = user["password"]
    assert "$" in stored  # werkzeug format, not a bare 64-char sha256 hex digest
    assert stored != hashlib.sha256(b"hunter2pass").hexdigest()


def test_verify_password_accepts_legacy_sha256_hash():
    legacy_hash = hashlib.sha256(b"old-password").hexdigest()
    assert verify_password(legacy_hash, "old-password") is True
    assert verify_password(legacy_hash, "wrong-password") is False


def test_verify_password_accepts_new_hash():
    new_hash = hash_pw("new-password")
    assert verify_password(new_hash, "new-password") is True
    assert verify_password(new_hash, "wrong-password") is False


def test_login_migrates_legacy_hash_to_new_format(client):
    with app_module.app.app_context():
        db = get_db()
        legacy_hash = hashlib.sha256(b"legacypass1").hexdigest()
        db.execute(
            "INSERT INTO users (username, email, password) VALUES (?,?,?)",
            ("legacyuser", "legacy@example.com", legacy_hash)
        )
        db.commit()

    resp = client.post("/login", json={"email": "legacy@example.com", "password": "legacypass1"})
    assert resp.status_code == 200

    with app_module.app.app_context():
        db = get_db()
        row = db.execute("SELECT password FROM users WHERE email=?", ("legacy@example.com",)).fetchone()
    assert "$" in row["password"]  # upgraded to werkzeug format after login

    # subsequent login still works against the migrated hash
    client.get("/logout")
    resp = client.post("/login", json={"email": "legacy@example.com", "password": "legacypass1"})
    assert resp.status_code == 200


@pytest.mark.parametrize("payload,field", [
    ({"username": "ab", "email": "x@example.com", "password": "longenough1"}, "username too short"),
    ({"username": "valid_user", "email": "not-an-email", "password": "longenough1"}, "bad email"),
    ({"username": "valid_user", "email": "x@example.com", "password": "short"}, "password too short"),
    ({"username": "bad name!", "email": "x@example.com", "password": "longenough1"}, "bad username chars"),
])
def test_register_rejects_invalid_input(client, payload, field):
    resp = client.post("/register", json=payload)
    assert resp.status_code == 400, field


def test_session_cookie_hardening_config():
    assert app_module.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app_module.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_secret_key_required_in_production(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("FLASK_ENV", "production")
    from mockfolio import create_app
    with pytest.raises(RuntimeError):
        create_app()
