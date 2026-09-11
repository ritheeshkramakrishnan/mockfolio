import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["MOCKFOLIO_DB_PATH"] = _db_path
os.environ["MOCKFOLIO_DATA_DIR"] = tempfile.mkdtemp(prefix="mockfolio_data_")
os.environ["MOCKFOLIO_DISABLE_SCHEDULER"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("FLASK_ENV", "testing")

import app as app_module  # noqa: E402
from mockfolio.db import get_db  # noqa: E402

_TABLES = [
    "trades", "positions", "portfolio_snapshots", "autopilot_log",
    "options_trades", "options_positions", "watchlist", "market_calls",
    "flashcard_progress", "custom_cards", "password_reset_tokens",
    "pending_orders", "users",
]


@pytest.fixture(autouse=True)
def _clean_db():
    """Wipe all tables before each test so tests don't leak state into each other."""
    with app_module.app.app_context():
        db = get_db()
        for table in _TABLES:
            db.execute(f"DELETE FROM {table}")
        db.commit()
    yield


@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture
def app():
    return app_module.app


def register_user(client, username="trader1", email="trader1@example.com", password="hunter2pass"):
    return client.post(
        "/register",
        json={"username": username, "email": email, "password": password},
    )


@pytest.fixture
def auth_client(client):
    register_user(client)
    return client
