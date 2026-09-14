from datetime import timedelta

import pytest

from mockfolio import autopilot_engine, market
from conftest import register_user


@pytest.fixture(autouse=True)
def _reset_scan_state():
    autopilot_engine._last_autopilot_scan = None
    yield
    autopilot_engine._last_autopilot_scan = None


def test_scan_status_reports_market_open_flag(client, monkeypatch):
    register_user(client)
    monkeypatch.setattr(market, "market_status", lambda: {
        "open": False, "status": "pre-market", "label": "Pre-Market",
        "weekday": True, "et_time": "08:57 ET",
    })
    resp = client.get("/api/autopilot/scan-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["market_open"] is False
    assert body["last_scan"] is None


def test_scan_status_last_scan_uses_et_and_stops_counting_down_when_closed(client, monkeypatch):
    register_user(client)
    fixed_now = market._et_now()
    autopilot_engine._last_autopilot_scan = fixed_now - timedelta(minutes=10)
    monkeypatch.setattr(market, "_et_now", lambda: fixed_now)
    monkeypatch.setattr(market, "market_status", lambda: {
        "open": False, "status": "after-hours", "label": "After-Hours",
        "weekday": True, "et_time": "17:00 ET",
    })
    resp = client.get("/api/autopilot/scan-status")
    body = resp.get_json()
    assert body["market_open"] is False
    assert body["last_scan"].endswith("ET")
    # market closed → countdown doesn't tick towards/past zero from a stale timestamp,
    # it just reports the full interval so the UI doesn't show a misleading "0:00"
    assert body["next_scan_secs"] == body["interval_secs"]


def test_scan_ts_load_discards_legacy_naive_timestamp(monkeypatch):
    """A timestamp saved before the ET-timezone fix (naive, server-local time)
    must not be compared against an aware datetime or displayed as-is."""
    naive_row = {"value": '{"ts": "2026-09-14T16:59:40"}'}

    class _FakeDb:
        def execute(self, *a, **kw):
            return self

        def fetchone(self):
            return naive_row

        def close(self):
            pass

    monkeypatch.setattr(autopilot_engine, "raw_connection", lambda: _FakeDb())
    assert autopilot_engine.scan_ts_load() is None
