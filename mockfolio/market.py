"""Market status/hours and price/history fetching (live → EOD → simulated fallback chain)."""
import io
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

_ET_TZ = ZoneInfo("America/New_York")

TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

# ── price cache (60s TTL) ─────────────────────────────────────────────────────
_price_cache: dict = {}
CACHE_TTL = 60

# ── seed prices for GBM fallback ──────────────────────────────────────────────
_SEED_PRICES = {
    "AAPL": 213.0, "MSFT": 420.0, "TSLA": 248.0, "NVDA": 134.0,
    "SPY": 548.0, "QQQ": 472.0, "GOOGL": 192.0, "AMZN": 207.0,
    "META": 584.0, "BRK.B": 460.0, "JPM": 245.0, "TLT": 90.0,
}


def _et_now() -> datetime:
    return datetime.now(_ET_TZ)


def _is_market_open() -> bool:
    """True if NYSE is currently open: 9:30–16:00 ET, Mon–Fri (holidays not checked)."""
    now = _et_now()
    if now.weekday() >= 5:          # Sat=5, Sun=6
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now < close_t


def _is_weekday() -> bool:
    return _et_now().weekday() < 5


def market_status() -> dict:
    now = _et_now()
    wd = now.weekday()   # 0=Mon … 6=Sun
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    is_open = (wd < 5) and (open_t <= now < close_t)
    if wd >= 5:
        status = "weekend"
        label = "Market Closed (Weekend)"
    elif now < open_t:
        status = "pre-market"
        label = "Pre-Market — opens at 9:30 AM ET"
    elif now >= close_t:
        status = "after-hours"
        label = "After-Hours — market closed"
    else:
        status = "open"
        label = "Market Open"
    return {"open": is_open, "status": status, "label": label,
            "weekday": wd < 5, "et_time": now.strftime("%H:%M ET")}


def _td_price(symbol: str) -> tuple:
    url = f"https://api.twelvedata.com/price?symbol={symbol.upper()}&apikey={TD_KEY}"
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    data = r.json()
    if "price" not in data:
        raise ValueError(data.get("message", "No price field"))
    return float(data["price"]), "live"


def _stooq_hist(symbol: str, days: int = 35) -> pd.DataFrame:
    sym = symbol.lower().strip()
    if "." not in sym:
        sym += ".us"
    end = datetime.now()
    start = end - timedelta(days=days)
    url = (f"https://stooq.com/q/d/l/?s={sym}"
           f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}&i=d")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.raise_for_status()
    text = r.text.strip()
    if not text or text.startswith("<!") or "No data" in text or len(text) < 40:
        raise ValueError("stooq returned no data")
    df = pd.read_csv(io.StringIO(text), parse_dates=["Date"])
    return df.sort_values("Date").set_index("Date")


def _sim_hist(symbol: str, days: int = 35) -> pd.DataFrame:
    sym = symbol.upper()
    base = _SEED_PRICES.get(sym, 100.0)
    week_seed = int(datetime.now().strftime("%Y%W"))
    rng = np.random.default_rng(abs(hash(sym)) % (2**31) + week_seed)
    mu, vol = 0.0003, 0.012
    returns = rng.normal(mu, vol, size=days)
    prices = base * np.cumprod(1 + returns)
    dates = pd.bdate_range(end=datetime.now().date(), periods=days)
    prices = prices[-len(dates):]
    return pd.DataFrame({"Open": prices * 0.999, "High": prices * 1.005,
                         "Low": prices * 0.995, "Close": prices, "Volume": 1_000_000},
                        index=pd.DatetimeIndex(dates))


def _get_price(symbol: str) -> tuple:
    sym = symbol.upper()
    now = datetime.now().timestamp()
    if sym in _price_cache:
        price, ts, source = _price_cache[sym]
        if now - ts < CACHE_TTL:
            return price, source + " (cached)"
    if TD_KEY:
        try:
            price, source = _td_price(sym)
            _price_cache[sym] = (price, now, "live")
            return price, source
        except Exception:
            pass
    try:
        df = _stooq_hist(sym, days=10)
        price = float(df["Close"].dropna().iloc[-1])
        _price_cache[sym] = (price, now, "EOD")
        return price, "EOD"
    except Exception:
        pass
    df = _sim_hist(sym, days=10)
    price = float(df["Close"].dropna().iloc[-1])
    _price_cache[sym] = (price, now, "simulated")
    return price, "simulated"


def _fetch_hist(symbol: str, days: int = 35) -> pd.DataFrame:
    try:
        return _stooq_hist(symbol, days=days)
    except Exception:
        return _sim_hist(symbol, days=days)
