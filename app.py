import os
import io
import json
import sqlite3
import hashlib
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from functools import wraps

import requests
from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, session, g
)
import numpy as np
import pandas as pd

# ── optional Groq ─────────────────────────────────────────────────────────────
try:
    from groq import Groq as _Groq
    GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
    _ai_client = _Groq(api_key=GROQ_KEY) if GROQ_KEY else None
    if GROQ_KEY:
        print(f"[groq] client ready (key prefix: {GROQ_KEY[:12]}…)")
    else:
        print("[groq] no API key found in environment")
except Exception as e:
    print(f"[groq] init failed: {e}")
    _ai_client = None

# ── Twelve Data API key ───────────────────────────────────────────────────────
TD_KEY = os.environ.get("TWELVEDATA_API_KEY", "")

# ── price cache (60s TTL) ─────────────────────────────────────────────────────
_price_cache: dict = {}
CACHE_TTL = 60

# ── news + signals cache (15 min TTL) ────────────────────────────────────────
_news_cache: dict = {}
_signals_cache: dict = {}
NEWS_TTL = 900
SIGNALS_TTL = 300   # 5 minutes — matches the background scheduler interval

# ── news RSS feeds ────────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("MarketWatch",  "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Reuters",      "https://feeds.reuters.com/reuters/businessNews"),
    ("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("Yahoo Finance","https://finance.yahoo.com/news/rssindex"),
    ("AP Business",  "https://feeds.apnews.com/rss/apf-business"),
]

# ── seed prices for GBM fallback ──────────────────────────────────────────────
_SEED_PRICES = {
    "AAPL": 213.0, "MSFT": 420.0, "TSLA": 248.0, "NVDA": 134.0,
    "SPY": 548.0, "QQQ": 472.0, "GOOGL": 192.0, "AMZN": 207.0,
    "META": 584.0, "BRK.B": 460.0, "JPM": 245.0, "TLT": 90.0,
}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

DB_PATH = os.path.join(os.path.dirname(__file__), "mockfolio.db")
STARTING_BALANCE = 50_000.0  # paper money
PROFIT_TARGET_PCT = 0.15      # 15% → must reach $57,500 to go funded

# ── database backend (PostgreSQL on Railway, SQLite locally) ─────────────────
_DB_URL = os.environ.get("DATABASE_URL", "")
if _DB_URL.startswith("postgres://"):          # Railway uses legacy prefix
    _DB_URL = _DB_URL.replace("postgres://", "postgresql://", 1)
USE_POSTGRES = bool(_DB_URL)


class _PgConn:
    """Thin psycopg2 wrapper that mirrors the sqlite3 connection API used throughout this app."""
    def __init__(self, url):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        self._conn = psycopg2.connect(url)
        self._RDC  = RealDictCursor

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=self._RDC)
        cur.execute(sql.replace("?", "%s"), params or ())
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        cur.close()

    def commit(self):    self._conn.commit()
    def rollback(self):  self._conn.rollback()
    def close(self):     self._conn.close()


# ── persistent local data directory (~/Documents/MockFolio_Data/) ─────────────
_DATA_DIR = os.path.join(os.path.expanduser("~"), "Documents", "MockFolio_Data")
os.makedirs(_DATA_DIR, exist_ok=True)
BACKUP_PATH  = os.path.join(_DATA_DIR, "backup.json")
TRADES_CSV   = os.path.join(_DATA_DIR, "trades.csv")


# ── database ──────────────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            g.db = _PgConn(_DB_URL)
        else:
            g.db = sqlite3.connect(DB_PATH)
            g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db:
        db.close()


def init_db():
    if USE_POSTGRES:
        db = _PgConn(_DB_URL)
        schema = """
            CREATE TABLE IF NOT EXISTS users (
                id          SERIAL PRIMARY KEY,
                username    TEXT UNIQUE NOT NULL,
                email       TEXT UNIQUE NOT NULL,
                password    TEXT NOT NULL,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                balance     REAL DEFAULT 50000.0,
                phase       TEXT DEFAULT 'evaluation'
            );
            CREATE TABLE IF NOT EXISTS positions (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                qty         REAL NOT NULL,
                avg_price   REAL NOT NULL,
                side        TEXT NOT NULL,
                opened_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS trades (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                qty         REAL NOT NULL,
                price       REAL NOT NULL,
                side        TEXT NOT NULL,
                pnl         REAL DEFAULT 0,
                executed_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS flashcard_progress (
                user_id     INTEGER NOT NULL,
                card_id     TEXT NOT NULL,
                known       INTEGER DEFAULT 0,
                seen        INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, card_id)
            );
            CREATE TABLE IF NOT EXISTS custom_cards (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                term        TEXT NOT NULL,
                definition  TEXT NOT NULL,
                category    TEXT DEFAULT 'My Cards',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                value       REAL NOT NULL,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS autopilot_log (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                action      TEXT NOT NULL,
                symbol      TEXT,
                qty         REAL,
                price       REAL,
                reasoning   TEXT,
                executed_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS options_positions (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                option_type TEXT NOT NULL,
                strike      REAL NOT NULL,
                expiry      TEXT NOT NULL,
                qty         INTEGER NOT NULL,
                premium     REAL NOT NULL,
                side        TEXT NOT NULL,
                opened_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS options_trades (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                option_type TEXT NOT NULL,
                strike      REAL NOT NULL,
                expiry      TEXT NOT NULL,
                qty         INTEGER NOT NULL,
                premium     REAL NOT NULL,
                side        TEXT NOT NULL,
                pnl         REAL DEFAULT 0,
                executed_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                UNIQUE(user_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS market_calls (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER,
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,
                entry_price REAL,
                target_price REAL,
                stop_loss   REAL,
                reasoning   TEXT,
                confidence  INTEGER DEFAULT 50,
                status      TEXT DEFAULT 'active',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """
    else:
        db = sqlite3.connect(DB_PATH)
        db.row_factory = sqlite3.Row
        schema = """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT UNIQUE NOT NULL,
                email       TEXT UNIQUE NOT NULL,
                password    TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                balance     REAL DEFAULT 50000.0,
                phase       TEXT DEFAULT 'evaluation'
            );
            CREATE TABLE IF NOT EXISTS positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                qty         REAL NOT NULL,
                avg_price   REAL NOT NULL,
                side        TEXT NOT NULL,
                opened_at   TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                qty         REAL NOT NULL,
                price       REAL NOT NULL,
                side        TEXT NOT NULL,
                pnl         REAL DEFAULT 0,
                executed_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS flashcard_progress (
                user_id     INTEGER NOT NULL,
                card_id     TEXT NOT NULL,
                known       INTEGER DEFAULT 0,
                seen        INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS custom_cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                term        TEXT NOT NULL,
                definition  TEXT NOT NULL,
                category    TEXT DEFAULT 'My Cards',
                created_at  TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                value       REAL NOT NULL,
                recorded_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS autopilot_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                action      TEXT NOT NULL,
                symbol      TEXT,
                qty         REAL,
                price       REAL,
                reasoning   TEXT,
                executed_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS options_positions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                option_type TEXT NOT NULL,
                strike      REAL NOT NULL,
                expiry      TEXT NOT NULL,
                qty         INTEGER NOT NULL,
                premium     REAL NOT NULL,
                side        TEXT NOT NULL,
                opened_at   TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS options_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                option_type TEXT NOT NULL,
                strike      REAL NOT NULL,
                expiry      TEXT NOT NULL,
                qty         INTEGER NOT NULL,
                premium     REAL NOT NULL,
                side        TEXT NOT NULL,
                pnl         REAL DEFAULT 0,
                executed_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                symbol      TEXT NOT NULL,
                UNIQUE(user_id, symbol),
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS market_calls (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                symbol      TEXT NOT NULL,
                direction   TEXT NOT NULL,
                entry_price REAL,
                target_price REAL,
                stop_loss   REAL,
                reasoning   TEXT,
                confidence  INTEGER DEFAULT 50,
                status      TEXT DEFAULT 'active',
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """
    db.executescript(schema)
    db.commit()
    def _migrate(sql):
        """Run a single ALTER TABLE safely — rollback on failure so next migration can proceed."""
        try:
            db.execute(sql)
            db.commit()
        except Exception:
            try: db.rollback()
            except Exception: pass

    _migrate("ALTER TABLE users ADD COLUMN autopilot INTEGER DEFAULT 0")
    _migrate("ALTER TABLE trades ADD COLUMN source TEXT DEFAULT 'manual'")
    _migrate("ALTER TABLE trades ADD COLUMN ai_reasoning TEXT")
    _migrate("ALTER TABLE users ADD COLUMN autopilot_budget REAL DEFAULT 0")
    _migrate("ALTER TABLE users ADD COLUMN autopilot_max_pct REAL DEFAULT 20")
    _migrate("ALTER TABLE users ADD COLUMN autopilot_daily_loss_limit REAL DEFAULT 500")
    # password reset tokens table
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                token     TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                used      INTEGER DEFAULT 0
            )
        """ if not USE_POSTGRES else """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id        SERIAL PRIMARY KEY,
                user_id   INTEGER NOT NULL,
                token     TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                used      INTEGER DEFAULT 0
            )
        """)
        db.commit()
    except Exception:
        try: db.rollback()
        except: pass
    # persistent key-value cache (survives restarts — used for AI signals, etc.)
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS app_cache (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.commit()
    except Exception:
        try: db.rollback()
        except: pass
    _import_backup(db)
    _cleanup_snapshots(db)
    db.close()


def _autopilot_deployed(user_id: int, db) -> float:
    """Return total capital currently deployed in open autopilot positions (cost basis)."""
    # sum of autopilot buys that haven't been fully sold
    # we approximate by summing open position value for symbols ever bought by autopilot
    ap_symbols = {r["symbol"] for r in db.execute(
        "SELECT DISTINCT symbol FROM trades WHERE user_id=? AND source='autopilot' AND side='buy'",
        (user_id,)
    ).fetchall()}
    if not ap_symbols:
        return 0.0
    deployed = 0.0
    for sym in ap_symbols:
        pos = db.execute(
            "SELECT qty, avg_price FROM positions WHERE user_id=? AND symbol=? AND side='long'",
            (user_id, sym)
        ).fetchone()
        if pos:
            deployed += pos["qty"] * pos["avg_price"]
    return deployed


def _autopilot_today_pnl(user_id: int, db) -> float:
    """Return today's realized P&L from autopilot sell trades."""
    today = datetime.now().date().isoformat()
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(pnl),0) as total FROM trades "
            "WHERE user_id=? AND source='autopilot' AND side='sell' AND DATE(executed_at) = ?",
            (user_id, today)
        ).fetchone()
        return float(row["total"] or 0)
    except Exception:
        return 0.0


def _run_autopilot(user_id: int, db, mode: str = "standard") -> list:
    """
    Claude analyzes the portfolio and market, then executes trades automatically.
    mode: "standard" — normal autopilot
          "cover_losses" — defensive recovery: sell losers, buy safe assets
    Respects autopilot_budget (ring-fenced capital) and autopilot_daily_loss_limit.
    Returns a list of log entries for what was done.
    """
    if not _ai_client:
        return []

    try:
        user_row = db.execute(
            "SELECT balance, phase, autopilot_budget, autopilot_max_pct, autopilot_daily_loss_limit FROM users WHERE id=?",
            (user_id,)
        ).fetchone()
    except Exception:
        try: db.rollback()
        except Exception: pass
        user_row = db.execute("SELECT balance, phase FROM users WHERE id=?", (user_id,)).fetchone()

    if not user_row:
        return []

    balance = user_row["balance"]
    try:
        budget           = float(user_row["autopilot_budget"]           or 0)
        max_pct          = float(user_row["autopilot_max_pct"]           or 20) / 100.0
        daily_loss_limit = float(user_row["autopilot_daily_loss_limit"]  or 500)
    except Exception:
        budget = 0
        max_pct = 0.20
        daily_loss_limit = 500

    # ── daily loss circuit-breaker ────────────────────────────────────────────
    today_pnl = _autopilot_today_pnl(user_id, db)
    if today_pnl <= -abs(daily_loss_limit):
        log_reason = f"Daily loss limit hit (${today_pnl:+.2f} today, limit -${daily_loss_limit:.0f}). Autopilot paused for today."
        db.execute(
            "INSERT INTO autopilot_log (user_id,action,symbol,qty,price,reasoning) VALUES (?,?,?,?,?,?)",
            (user_id, "PAUSED", None, None, None, log_reason)
        )
        db.commit()
        return [{"action": "PAUSED", "symbol": None, "qty": 0, "reasoning": log_reason}]

    # ── determine working capital ─────────────────────────────────────────────
    if budget > 0:
        deployed     = _autopilot_deployed(user_id, db)
        available    = max(0, budget - deployed)
        working_cash = min(available, balance)  # can't spend more than actual balance
    else:
        working_cash = balance  # no budget cap — use full balance (legacy mode)

    positions = db.execute("SELECT * FROM positions WHERE user_id=?", (user_id,)).fetchall()

    # build positions summary with live prices
    pos_summary = []
    prices = {}
    watchlist_rows = db.execute(
        "SELECT symbol FROM watchlist WHERE user_id=?", (user_id,)
    ).fetchall()
    watchlist_syms = [r["symbol"] for r in watchlist_rows] or ["AAPL","TSLA","NVDA","SPY","MSFT"]
    symbols_to_check = list({p["symbol"] for p in positions} | set(watchlist_syms[:6]))

    for sym in symbols_to_check:
        try:
            price, _ = _get_price(sym)
            prices[sym] = price
        except Exception:
            pass

    for p in positions:
        sym = p["symbol"]
        cur = prices.get(sym, p["avg_price"])
        unreal_pnl = (cur - p["avg_price"]) * p["qty"]
        pos_summary.append(f"{sym}: {p['qty']} shares @ avg ${p['avg_price']:.2f}, now ${cur:.2f}, P&L ${unreal_pnl:+.2f}")

    articles = _fetch_news()
    headlines = "\n".join(f"- {a['title']}" for a in articles[:8]) if articles else "No recent news."

    budget_note = (
        f"- Autopilot budget: ${budget:,.2f} total | ${working_cash:,.2f} available to deploy\n"
        f"- Max per trade: {max_pct*100:.0f}% of available (${working_cash*max_pct:,.2f})\n"
        f"- Daily loss limit: ${daily_loss_limit:,.0f} | Today P&L so far: ${today_pnl:+.2f}"
        if budget > 0 else
        f"- Cash available: ${working_cash:,.2f}\n"
        f"- Max per trade: {max_pct*100:.0f}% of cash (${working_cash*max_pct:,.2f})\n"
        f"- Daily loss limit: ${daily_loss_limit:,.0f} | Today P&L so far: ${today_pnl:+.2f}"
    )

    # ── compute unrealized P&L across open positions ──────────────────────────
    total_unrealized = sum(
        (prices.get(p["symbol"], p["avg_price"]) - p["avg_price"]) * p["qty"]
        for p in positions
    )

    if mode == "cover_losses":
        # Sort positions by P&L so Claude sees losers first
        pos_summary_sorted = sorted(
            pos_summary,
            key=lambda s: float(s.split("P&L $")[1].replace(",","")) if "P&L $" in s else 0
        )
        prompt = f"""You are an AI autopilot running in COVER LOSSES mode. Your ONLY goal is to stop losses and recover capital safely.

PORTFOLIO:
- Cash balance: ${balance:,.2f}
- Available capital: ${working_cash:,.2f}
- Unrealized P&L across all positions: ${total_unrealized:+,.2f}
- Today's realized P&L: ${today_pnl:+,.2f}
- Open positions ({len(positions)}) — sorted worst first:
{chr(10).join(pos_summary_sorted) if pos_summary_sorted else '  None'}

MARKET PRICES:
{chr(10).join(f'  {s}: ${p:.2f}' for s,p in prices.items())}

RECENT NEWS:
{headlines}

COVER LOSSES RULES — follow strictly:
1. SELL every position with unrealized loss > 2% — cut the bleeding immediately, do not wait
2. Sell the WORST performing position first if multiple are losing
3. Use freed capital ONLY for safe, defensive assets: SPY, QQQ, VTI, SCHD, BRK-B, or similar low-volatility blue-chip ETFs
4. Zero speculative or high-volatility plays — no single stocks unless they are mega-cap (AAPL, MSFT, GOOGL, JNJ)
5. Max {max_pct*100:.0f}% of available capital per trade — keep positions small to limit further downside
6. If no positions are losing, still deploy available cash into the safest ETF option
7. Market hours do not apply — execute immediately

IMPORTANT: You MUST return at least one BUY or SELL. Do NOT return HOLD.
Respond ONLY with valid JSON — no markdown, no explanation:
[
  {{"action": "SELL", "symbol": "TSLA", "qty": 3, "reasoning": "Cutting loss: down 9.2%, freeing capital for defensive redeployment"}},
  {{"action": "BUY",  "symbol": "SPY",  "qty": 2, "reasoning": "Safe redeployment into broad-market ETF: low volatility, strong liquidity"}}
]"""
    else:
        prompt = f"""You are an AI autopilot managing a paper trading portfolio. Make trading decisions NOW.

PORTFOLIO:
- Cash balance: ${balance:,.2f}
{budget_note}
- Open positions ({len(positions)}):
{chr(10).join(pos_summary) if pos_summary else '  None'}

MARKET PRICES:
{chr(10).join(f'  {s}: ${p:.2f}' for s,p in prices.items())}

RECENT NEWS:
{headlines}

RULES:
- Max {max_pct*100:.0f}% of available capital per single buy trade
- Max 6 open positions total
- You MUST make at least one BUY or SELL trade — do not return only HOLD
- If you have open positions, consider selling the weakest one
- If no open positions, buy the most promising symbol from the watchlist
- Market hours do not apply here — execute regardless

You MUST include at least one BUY or SELL. Respond ONLY with valid JSON:
[
  {{"action": "BUY", "symbol": "AAPL", "qty": 5, "reasoning": "Strong momentum..."}},
  {{"action": "SELL", "symbol": "TSLA", "qty": 2, "reasoning": "Overbought..."}}
]"""

    try:
        response = _ai_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.choices[0].message.content.strip()
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        decisions = json.loads(raw[start:end]) if start != -1 else []
    except Exception as e:
        print(f"[autopilot] AI error: {e}")
        # Propagate so the API endpoint can surface the real message
        raise RuntimeError(f"AI call failed: {e}")

    # ── hard fallback: if Claude returned only HOLDs, force a trade ──────────
    has_action = any(d.get("action","").upper() in ("BUY","SELL") for d in decisions)
    if not has_action:
        # pick the first watchlist symbol we can afford
        forced = None
        for sym in watchlist_syms:
            p = prices.get(sym)
            if not p:
                try: p, _ = _get_price(sym)
                except: continue
            spend = working_cash * max_pct
            qty_forced = max(1, int(spend / p))
            if working_cash >= p * qty_forced:
                forced = {"action": "BUY", "symbol": sym, "qty": qty_forced,
                          "reasoning": f"Forced trade: AI returned no actionable decision. Buying {sym} at ${p:.2f} as default position."}
                break
        if forced:
            decisions = [forced]
        else:
            raise RuntimeError("Insufficient balance to place any trade.")

    log_entries = []
    for decision in decisions:
        action  = decision.get("action", "HOLD").upper()
        symbol  = (decision.get("symbol") or "").upper()
        qty     = float(decision.get("qty") or 0)
        reason  = decision.get("reasoning", "")

        executed_price = None

        if action == "BUY" and symbol and qty > 0:
            price = prices.get(symbol)
            if not price:
                try: price, _ = _get_price(symbol)
                except: continue
            cost = price * qty
            max_spend = working_cash * max_pct
            if cost > max_spend:
                qty = max(1, int(max_spend / price))
                cost = price * qty
            open_count = db.execute(
                "SELECT COUNT(*) as n FROM positions WHERE user_id=?", (user_id,)
            ).fetchone()["n"]
            if open_count >= 6:
                action = "SKIP"
                reason = f"Skipped BUY {symbol}: max 6 positions reached"
            elif balance < cost:
                action = "SKIP"
                reason = f"Skipped BUY {symbol}: insufficient balance (need ${cost:.2f}, have ${balance:.2f})"
            elif working_cash < cost:
                action = "SKIP"
                reason = f"Skipped BUY {symbol}: autopilot budget exhausted (available ${working_cash:.2f})"
            else:
                db.execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user_id))
                pos = db.execute(
                    "SELECT * FROM positions WHERE user_id=? AND symbol=? AND side='long'",
                    (user_id, symbol)
                ).fetchone()
                if pos:
                    new_qty = pos["qty"] + qty
                    new_avg = (pos["avg_price"] * pos["qty"] + price * qty) / new_qty
                    db.execute("UPDATE positions SET qty=?, avg_price=? WHERE id=?", (new_qty, new_avg, pos["id"]))
                else:
                    db.execute(
                        "INSERT INTO positions (user_id,symbol,qty,avg_price,side) VALUES (?,?,?,?,'long')",
                        (user_id, symbol, qty, price)
                    )
                db.execute(
                    "INSERT INTO trades (user_id,symbol,qty,price,side,pnl,source,ai_reasoning) VALUES (?,?,?,?,'buy',0,'autopilot',?)",
                    (user_id, symbol, qty, price, reason)
                )
                balance -= cost
                working_cash -= cost
                executed_price = price

        elif action == "SELL" and symbol and qty > 0:
            pos = db.execute(
                "SELECT * FROM positions WHERE user_id=? AND symbol=? AND side='long'",
                (user_id, symbol)
            ).fetchone()
            if not pos or pos["qty"] < qty:
                qty = pos["qty"] if pos else 0
            if pos and qty > 0:
                price = prices.get(symbol)
                if not price:
                    try: price, _ = _get_price(symbol)
                    except: continue
                pnl = (price - pos["avg_price"]) * qty
                proceeds = price * qty
                db.execute("UPDATE users SET balance=balance+? WHERE id=?", (proceeds, user_id))
                new_qty = pos["qty"] - qty
                if new_qty < 0.0001:
                    db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
                else:
                    db.execute("UPDATE positions SET qty=? WHERE id=?", (new_qty, pos["id"]))
                db.execute(
                    "INSERT INTO trades (user_id,symbol,qty,price,side,pnl,source,ai_reasoning) VALUES (?,?,?,?,'sell',?,'autopilot',?)",
                    (user_id, symbol, qty, price, pnl, reason)
                )
                balance += proceeds
                executed_price = price

        # Cover losses mode: use a distinct log action so the feed can show a shield badge
        log_action = f"COVER_{action}" if (mode == "cover_losses" and action in ("BUY","SELL")) else action
        db.execute(
            "INSERT INTO autopilot_log (user_id,action,symbol,qty,price,reasoning) VALUES (?,?,?,?,?,?)",
            (user_id, log_action, symbol or None, qty or None, executed_price, reason)
        )
        log_entries.append({"action": log_action, "symbol": symbol, "qty": qty, "reasoning": reason})

    if log_entries:
        _record_snapshot(user_id, db)
        _check_phase(user_id, db)
        db.commit()
        _export_backup(db)

    return log_entries


def _check_phase(user_id: int, db):
    """Promote user from evaluation → funded if portfolio value hits 15% target."""
    user_row = db.execute("SELECT balance, phase FROM users WHERE id=?", (user_id,)).fetchone()
    if not user_row or user_row["phase"] == "funded":
        return
    positions = db.execute(
        "SELECT qty, avg_price FROM positions WHERE user_id=?", (user_id,)
    ).fetchall()
    portfolio_value = user_row["balance"] + sum(p["qty"] * p["avg_price"] for p in positions)
    target = STARTING_BALANCE * (1 + PROFIT_TARGET_PCT)
    if portfolio_value >= target:
        db.execute("UPDATE users SET phase='funded' WHERE id=?", (user_id,))


def _record_snapshot(user_id: int, db):
    """Save current portfolio value (cash + cost basis of open positions)."""
    user_row = db.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    if not user_row:
        return
    positions = db.execute(
        "SELECT qty, avg_price FROM positions WHERE user_id=?", (user_id,)
    ).fetchall()
    position_value = sum(p["qty"] * p["avg_price"] for p in positions)
    total = user_row["balance"] + position_value
    db.execute(
        "INSERT INTO portfolio_snapshots (user_id, value) VALUES (?,?)",
        (user_id, round(total, 2))
    )


def _export_backup(db):
    """
    Save full state to ~/Documents/MockFolio_Data/backup.json
    and append the latest trade to trades.csv.
    Called after every trade.
    """
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)

        users     = [dict(r) for r in db.execute("SELECT * FROM users").fetchall()]
        trades    = [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY executed_at").fetchall()]
        positions = [dict(r) for r in db.execute("SELECT * FROM positions").fetchall()]
        snapshots = [dict(r) for r in db.execute("SELECT * FROM portfolio_snapshots ORDER BY recorded_at").fetchall()]
        cards     = [dict(r) for r in db.execute("SELECT * FROM custom_cards").fetchall()]

        # ── full JSON backup ──────────────────────────────────────────────────
        with open(BACKUP_PATH, "w") as f:
            json.dump({
                "exported_at": datetime.now().isoformat(),
                "users": users,
                "trades": trades,
                "positions": positions,
                "portfolio_snapshots": snapshots,
                "custom_cards": cards,
            }, f, indent=2)

        # ── trades CSV (append-only, skip rows already written) ───────────────
        import csv
        existing_ids: set = set()
        if os.path.exists(TRADES_CSV):
            with open(TRADES_CSV, newline="") as cf:
                reader = csv.DictReader(cf)
                for row in reader:
                    existing_ids.add(int(row["id"]))

        new_trades = [t for t in trades if t["id"] not in existing_ids]
        if new_trades:
            write_header = not os.path.exists(TRADES_CSV) or os.path.getsize(TRADES_CSV) == 0
            with open(TRADES_CSV, "a", newline="") as cf:
                writer = csv.DictWriter(cf, fieldnames=["id","user_id","symbol","qty","price","side","pnl","executed_at"])
                if write_header:
                    writer.writeheader()
                writer.writerows(new_trades)

        print(f"[backup] saved → {BACKUP_PATH}")

    except Exception as e:
        print(f"[backup] ERROR saving backup: {e}")
        import traceback; traceback.print_exc()


def _import_backup(db):
    """Restore from backup.json if the DB has no users (e.g. after Railway redeploy)."""
    if not os.path.exists(BACKUP_PATH):
        return
    try:
        existing = db.execute("SELECT COUNT(*) as n FROM users").fetchone()["n"]
        if existing > 0:
            return
        with open(BACKUP_PATH) as f:
            data = json.load(f)
        for u in data.get("users", []):
            db.execute(
                "INSERT OR IGNORE INTO users (id,username,email,password,created_at,balance,phase) VALUES (?,?,?,?,?,?,?)",
                (u["id"], u["username"], u["email"], u["password"], u["created_at"], u["balance"], u["phase"])
            )
        for t in data.get("trades", []):
            db.execute(
                "INSERT OR IGNORE INTO trades (id,user_id,symbol,qty,price,side,pnl,executed_at) VALUES (?,?,?,?,?,?,?,?)",
                (t["id"], t["user_id"], t["symbol"], t["qty"], t["price"], t["side"], t["pnl"], t["executed_at"])
            )
        for p in data.get("positions", []):
            db.execute(
                "INSERT OR IGNORE INTO positions (id,user_id,symbol,qty,avg_price,side,opened_at) VALUES (?,?,?,?,?,?,?)",
                (p["id"], p["user_id"], p["symbol"], p["qty"], p["avg_price"], p["side"], p["opened_at"])
            )
        for s in data.get("portfolio_snapshots", []):
            db.execute(
                "INSERT OR IGNORE INTO portfolio_snapshots (id,user_id,value,recorded_at) VALUES (?,?,?,?)",
                (s["id"], s["user_id"], s["value"], s["recorded_at"])
            )
        for c in data.get("custom_cards", []):
            db.execute(
                "INSERT OR IGNORE INTO custom_cards (id,user_id,term,definition,category,created_at) VALUES (?,?,?,?,?,?)",
                (c["id"], c["user_id"], c["term"], c["definition"], c["category"], c["created_at"])
            )
        db.commit()
        print(f"[backup] restored from {BACKUP_PATH}")
    except Exception as e:
        print(f"[backup] restore failed: {e}")


def _cleanup_snapshots(db):
    """
    Prune portfolio snapshots:
    - Remove the opening $50k snapshot once the user has made trades.
    - For snapshots older than 2 days, keep only the last per calendar day.
    """
    try:
        users  = db.execute("SELECT id FROM users").fetchall()
        cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        for user in users:
            uid = user["id"]
            trade_count = db.execute(
                "SELECT COUNT(*) as n FROM trades WHERE user_id=?", (uid,)
            ).fetchone()["n"]
            if trade_count > 0:
                first = db.execute(
                    "SELECT id, value FROM portfolio_snapshots WHERE user_id=? ORDER BY recorded_at ASC LIMIT 1",
                    (uid,)
                ).fetchone()
                if first and abs(first["value"] - STARTING_BALANCE) < 0.01:
                    db.execute("DELETE FROM portfolio_snapshots WHERE id=?", (first["id"],))
            old_rows = db.execute(
                "SELECT id, date(recorded_at) as day FROM portfolio_snapshots "
                "WHERE user_id=? AND date(recorded_at) < ? ORDER BY recorded_at ASC",
                (uid, cutoff)
            ).fetchall()
            by_day: dict = {}
            for row in old_rows:
                by_day.setdefault(row["day"], []).append(row["id"])
            for day, ids in by_day.items():
                to_delete = ids[:-1]
                if to_delete:
                    db.execute(
                        f"DELETE FROM portfolio_snapshots WHERE id IN ({','.join('?'*len(to_delete))})",
                        to_delete
                    )
        db.commit()
    except Exception as e:
        print(f"[cleanup] failed: {e}")


# initialise DB when module is imported (works with gunicorn)
init_db()

# ── autopilot background scheduler ───────────────────────────────────────────
_last_autopilot_scan: datetime = None   # track last scan time for the UI


def _scan_ts_save(dt: datetime):
    """Persist the last autopilot scan timestamp to app_cache."""
    try:
        if USE_POSTGRES:
            db = _PgConn(_DB_URL)
        else:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
        val = json.dumps({"ts": dt.isoformat()})
        now_str = dt.isoformat()
        try:
            db.execute("DELETE FROM app_cache WHERE key=?", ("last_autopilot_scan",))
            db.execute("INSERT INTO app_cache(key, value, updated_at) VALUES (?,?,?)",
                       ("last_autopilot_scan", val, now_str))
            db.commit()
        except Exception as e:
            try: db.rollback()
            except: pass
            print(f"[scheduler] scan ts save error: {e}")
        db.close()
    except Exception as e:
        print(f"[scheduler] scan ts save outer error: {e}")


def _scan_ts_load() -> datetime | None:
    """Load the last autopilot scan timestamp from app_cache."""
    try:
        if USE_POSTGRES:
            db = _PgConn(_DB_URL)
        else:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
        row = db.execute("SELECT value FROM app_cache WHERE key=?", ("last_autopilot_scan",)).fetchone()
        db.close()
        if row:
            data = json.loads(row["value"])
            return datetime.fromisoformat(data["ts"])
    except Exception as e:
        print(f"[scheduler] scan ts load error: {e}")
    return None


def _autopilot_scan_all():
    """Run autopilot for every user who has it enabled. Called every 5 minutes."""
    global _last_autopilot_scan
    if not _ai_client:
        return
    try:
        if USE_POSTGRES:
            db = _PgConn(_DB_URL)
        else:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row

        users = db.execute("SELECT id FROM users WHERE autopilot=1").fetchall()
        print(f"[scheduler] autopilot scan — {len(users)} user(s) enabled")
        for u in users:
            try:
                _run_autopilot(u["id"], db)
            except Exception as e:
                print(f"[scheduler] user {u['id']} error: {e}")
        _last_autopilot_scan = datetime.now()
        _scan_ts_save(_last_autopilot_scan)
        db.close()
    except Exception as e:
        print(f"[scheduler] scan failed: {e}")

    # Refresh AI signals every 5 minutes (server-side, independent of any user's browser)
    try:
        print("[scheduler] refreshing AI signals…")
        articles = _fetch_news(limit=20)
        _signals_cache.clear()
        _generate_signals(articles, force=True)
        print("[scheduler] AI signals refreshed")
    except Exception as e:
        print(f"[scheduler] signals refresh failed: {e}")


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_autopilot_scan_all, 'interval', minutes=5,
                  id='autopilot_scan', max_instances=1, coalesce=True)
    sched.start()
    print("[scheduler] autopilot background scheduler started (5-min interval)")
    return sched


# Start scheduler — guard against Flask debug double-start and gunicorn pre-fork
import os as _os
if not (_os.environ.get("WERKZEUG_RUN_MAIN") == "false"):
    _scheduler = _start_scheduler()

# ── market data helpers ───────────────────────────────────────────────────────
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


# ── news helpers ──────────────────────────────────────────────────────────────
def _fetch_news(limit: int = 25) -> list:
    now = datetime.now().timestamp()
    if _news_cache.get("articles") and now - _news_cache.get("ts", 0) < NEWS_TTL:
        return _news_cache["articles"]

    articles = []
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    for source_name, feed_url in NEWS_FEEDS:
        try:
            r = requests.get(feed_url, headers=headers, timeout=8)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:8]:
                title = (item.findtext("title") or "").strip()
                desc  = (item.findtext("description") or "").strip()
                link  = (item.findtext("link") or "").strip()
                pub   = (item.findtext("pubDate") or "").strip()
                if title:
                    articles.append({
                        "title":       title,
                        "description": desc[:250] if desc else "",
                        "link":        link,
                        "pub_date":    pub,
                        "source":      source_name,
                    })
        except Exception:
            pass

    # deduplicate by title
    seen, unique = set(), []
    for a in articles:
        if a["title"] not in seen:
            seen.add(a["title"])
            unique.append(a)

    _news_cache["articles"] = unique[:limit]
    _news_cache["ts"] = now
    return unique[:limit]


def _fallback_signals() -> list:
    return [
        {"ticker": "SPY",  "direction": "BUY",  "confidence": 65, "timeframe": "1-5 days",
         "thesis": "Broad market momentum stays positive near all-time highs.",
         "risk": "Fed hawkishness or macro shock could reverse trend.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
        {"ticker": "NVDA", "direction": "BUY",  "confidence": 70, "timeframe": "1-3 days",
         "thesis": "AI chip demand structural tailwind continues into next earnings.",
         "risk": "Export restrictions or valuation compression.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
        {"ticker": "TLT",  "direction": "SELL", "confidence": 60, "timeframe": "3-7 days",
         "thesis": "Rising yields keep pressure on long-duration bond prices.",
         "risk": "Flight-to-safety rally if risk-off sentiment spikes.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
        {"ticker": "QQQ",  "direction": "BUY",  "confidence": 68, "timeframe": "2-5 days",
         "thesis": "Tech sector strength driven by AI earnings beats and multiple expansion.",
         "risk": "Rate sensitivity and stretched valuations in mega-caps.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
    ]


def _signals_db_load():
    """Load signals from DB cache. Returns (signals_list, timestamp_float) or (None, 0)."""
    try:
        if USE_POSTGRES:
            db = _PgConn(_DB_URL)
        else:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
        row = db.execute("SELECT value FROM app_cache WHERE key=?", ("signals",)).fetchone()
        db.close()
        if row:
            data = json.loads(row["value"])
            return data.get("signals", []), float(data.get("ts", 0))
    except Exception as e:
        print(f"[signals] DB load error: {e}")
    return None, 0.0


def _signals_db_save(signals: list):
    """Persist signals to DB so they survive server restarts."""
    try:
        if USE_POSTGRES:
            db = _PgConn(_DB_URL)
        else:
            db = sqlite3.connect(DB_PATH)
            db.row_factory = sqlite3.Row
        val = json.dumps({"signals": signals, "ts": datetime.now().timestamp()})
        now_str = datetime.now().isoformat()
        try:
            db.execute("DELETE FROM app_cache WHERE key=?", ("signals",))
            db.execute("INSERT INTO app_cache(key, value, updated_at) VALUES (?,?,?)",
                       ("signals", val, now_str))
            db.commit()
        except Exception as e:
            try: db.rollback()
            except: pass
            print(f"[signals] DB save error (inner): {e}")
        db.close()
    except Exception as e:
        print(f"[signals] DB save error: {e}")


def _generate_signals(articles: list, force: bool = False) -> list:
    now = datetime.now().timestamp()

    if not force:
        # 1. fastest path — in-memory cache
        if _signals_cache.get("signals") and now - _signals_cache.get("ts", 0) < SIGNALS_TTL:
            return _signals_cache["signals"]

        # 2. survive restarts — check DB cache
        db_signals, db_ts = _signals_db_load()
        if db_signals and now - db_ts < SIGNALS_TTL:
            _signals_cache["signals"] = db_signals
            _signals_cache["ts"] = db_ts
            return db_signals

    # 3. need fresh signals
    if not _ai_client or not articles:
        signals = _fallback_signals()
        _signals_cache["signals"] = signals
        _signals_cache["ts"] = now
        _signals_db_save(signals)
        return signals

    headlines = "\n".join(
        f"- [{a['source']}] {a['title']}: {a['description'][:120]}"
        for a in articles[:18]
    )

    prompt = f"""You are QuantBot, an AI trading analyst for an educational paper trading platform.
Based on the real market news headlines below, generate exactly 4 paper trade signals as JSON.

HEADLINES:
{headlines}

Respond with ONLY a valid JSON array — no markdown, no explanation:
[
  {{
    "ticker": "AAPL",
    "direction": "BUY",
    "confidence": 72,
    "timeframe": "1-3 days",
    "thesis": "One concise sentence explaining the trade rationale based on the news",
    "risk": "One sentence describing the key downside risk",
    "news_trigger": "The specific headline title that triggered this signal"
  }}
]

Rules:
- Use real US stock tickers or ETFs (AAPL, MSFT, SPY, NVDA, TSLA, META, AMZN, QQQ, TLT, etc.)
- direction must be exactly "BUY" or "SELL"
- confidence is integer 0-100
- Include at least 1 bearish (SELL) signal
- Ground every signal in the actual headlines — no generic advice
- This is for educational paper trading only, not real financial advice"""

    try:
        response = _ai_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.choices[0].message.content.strip()
        start = text.find("[")
        end   = text.rfind("]") + 1
        signals = json.loads(text[start:end])
        if not isinstance(signals, list) or len(signals) == 0:
            raise ValueError("empty signals")
    except Exception:
        signals = _fallback_signals()

    _signals_cache["signals"] = signals
    _signals_cache["ts"] = now
    _signals_db_save(signals)
    return signals


# ── auth helpers ──────────────────────────────────────────────────────────────
def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()


# ── pages ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        data = request.get_json() or request.form
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, hash_pw(password))
        ).fetchone()
        if user:
            session["user_id"] = user["id"]
            return jsonify({"ok": True, "redirect": "/dashboard"})
        return jsonify({"ok": False, "error": "Invalid email or password"}), 401
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        data = request.get_json() or request.form
        username = data.get("username", "").strip()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "")
        if not username or not email or not password:
            return jsonify({"ok": False, "error": "All fields required"}), 400
        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password) VALUES (?,?,?)",
                (username, email, hash_pw(password))
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            session["user_id"] = user["id"]
            # record starting portfolio value
            _record_snapshot(user["id"], db)
            db.commit()
            return jsonify({"ok": True, "redirect": "/dashboard"})
        except sqlite3.IntegrityError:
            return jsonify({"ok": False, "error": "Username or email already taken"}), 409
    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/trade")
@login_required
def trade():
    return render_template("trade.html")


@app.route("/learn")
@login_required
def learn():
    return render_template("learn.html")


@app.route("/flashcards")
@login_required
def flashcards():
    return render_template("flashcards.html")


@app.route("/signals")
@login_required
def signals():
    return render_template("signals.html")


# ── API: user info ────────────────────────────────────────────────────────────
@app.route("/api/me")
@login_required
def api_me():
    user = current_user()
    db = get_db()
    positions = db.execute(
        "SELECT * FROM positions WHERE user_id=?", (user["id"],)
    ).fetchall()
    trades = db.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY executed_at DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    total_pnl = sum(t["pnl"] for t in trades)
    return jsonify({
        "username": user["username"],
        "email": user["email"],
        "balance": user["balance"],
        "phase": user["phase"],
        "created_at": user["created_at"],
        "total_pnl": total_pnl,
        "positions": [dict(p) for p in positions],
        "recent_trades": [dict(t) for t in trades],
        "starting_balance": STARTING_BALANCE,
        "profit_target": STARTING_BALANCE * (1 + PROFIT_TARGET_PCT),
        "profit_target_pct": PROFIT_TARGET_PCT * 100,
    })


@app.route("/api/portfolio/history")
@login_required
def api_portfolio_history():
    user = current_user()
    db = get_db()
    rows = db.execute(
        "SELECT value, recorded_at FROM portfolio_snapshots WHERE user_id=? ORDER BY recorded_at ASC",
        (user["id"],)
    ).fetchall()
    # if no snapshots yet, return starting balance as single point
    if not rows:
        return jsonify([{"value": STARTING_BALANCE, "recorded_at": user["created_at"]}])
    return jsonify([{"value": r["value"], "recorded_at": r["recorded_at"]} for r in rows])


# ── API: autopilot ────────────────────────────────────────────────────────────
@app.route("/api/autopilot/status")
@login_required
def api_autopilot_status():
    user = current_user()
    db = get_db()
    row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
    enabled = bool(row["autopilot"]) if row else False
    logs = db.execute(
        "SELECT action, symbol, qty, price, reasoning, executed_at "
        "FROM autopilot_log WHERE user_id=? ORDER BY executed_at DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    return jsonify({
        "enabled": enabled,
        "logs": [dict(l) for l in logs],
        "ai_available": bool(_ai_client),
    })


@app.route("/api/autopilot/toggle", methods=["POST"])
@login_required
def api_autopilot_toggle():
    user = current_user()
    db = get_db()
    row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
    new_state = 0 if (row and row["autopilot"]) else 1
    db.execute("UPDATE users SET autopilot=? WHERE id=?", (new_state, user["id"]))
    db.commit()
    return jsonify({"enabled": bool(new_state)})


@app.route("/api/autopilot/run", methods=["POST"])
@login_required
def api_autopilot_run():
    try:
        user = current_user()
        db = get_db()
        row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not row["autopilot"]:
            return jsonify({"ok": False, "message": "Autopilot is off — enable it first using the toggle button"}), 400
        if not _ai_client:
            return jsonify({"ok": False, "message": "No Groq API key — add GROQ_API_KEY to Railway variables"}), 400
        results = _run_autopilot(user["id"], db)
        return jsonify({"ok": True, "trades": results})
    except RuntimeError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "message": f"Unexpected error: {e}"}), 500


@app.route("/api/autopilot/cover-losses", methods=["POST"])
@login_required
def api_autopilot_cover_losses():
    """
    One-shot recovery run: Claude sells losing positions and reinvests defensively.
    Does NOT require autopilot to be enabled — available as a standalone action.
    """
    if not _ai_client:
        return jsonify({"ok": False, "message": "No Groq API key — add GROQ_API_KEY to Railway variables"}), 400
    try:
        user = current_user()
        db   = get_db()
        results = _run_autopilot(user["id"], db, mode="cover_losses")
        cover_trades = [t for t in results if t.get("action","").startswith("COVER_")]
        return jsonify({
            "ok":     True,
            "trades": results,
            "recovered": len(cover_trades),
            "message": f"Recovery complete — {len(cover_trades)} trade(s) executed to cover losses."
                       if cover_trades else "Portfolio reviewed — no immediate recovery trades needed."
        })
    except RuntimeError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"ok": False, "message": f"Recovery failed: {e}"}), 500


@app.route("/autopilot")
@login_required
def autopilot_page():
    return render_template("autopilot.html")


@app.route("/api/autopilot/scan-status")
@login_required
def api_autopilot_scan_status():
    global _last_autopilot_scan
    now = datetime.now()
    INTERVAL = 5 * 60  # seconds
    # Load from DB if in-memory value was lost (e.g. after a restart)
    last = _last_autopilot_scan or _scan_ts_load()
    if last:
        _last_autopilot_scan = last   # repopulate in-memory for next call
        secs_since = (now - last).total_seconds()
        secs_until  = max(0, INTERVAL - secs_since)
        last_str    = last.strftime("%H:%M:%S")
    else:
        secs_until = INTERVAL
        last_str   = None
    return jsonify({
        "last_scan":       last_str,
        "next_scan_secs":  int(secs_until),
        "interval_secs":   INTERVAL,
    })


@app.route("/api/autopilot/config", methods=["GET"])
@login_required
def api_autopilot_config_get():
    user = current_user()
    db = get_db()
    try:
        row = db.execute(
            "SELECT autopilot, autopilot_budget, autopilot_max_pct, autopilot_daily_loss_limit FROM users WHERE id=?",
            (user["id"],)
        ).fetchone()
        return jsonify({
            "enabled":          bool(row["autopilot"]) if row else False,
            "budget":           float(row["autopilot_budget"] or 0) if row else 0,
            "max_pct":          float(row["autopilot_max_pct"] or 20) if row else 20,
            "daily_loss_limit": float(row["autopilot_daily_loss_limit"] or 500) if row else 500,
            "ai_available":     bool(_ai_client),
        })
    except Exception:
        try:
            if hasattr(db, 'rollback'): db.rollback()
            row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
            enabled = bool(row["autopilot"]) if row else False
        except Exception:
            enabled = False
        return jsonify({
            "enabled": enabled, "budget": 0, "max_pct": 20,
            "daily_loss_limit": 500, "ai_available": bool(_ai_client),
        })


@app.route("/api/autopilot/config", methods=["POST"])
@login_required
def api_autopilot_config_set():
    data    = request.get_json() or {}
    user    = current_user()
    db      = get_db()

    budget          = float(data.get("budget", 0))
    max_pct         = float(data.get("max_pct", 20))
    daily_loss_limit = float(data.get("daily_loss_limit", 500))

    # clamp values
    budget           = max(0, budget)
    max_pct          = max(5, min(100, max_pct))
    daily_loss_limit = max(0, daily_loss_limit)

    db.execute(
        "UPDATE users SET autopilot_budget=?, autopilot_max_pct=?, autopilot_daily_loss_limit=? WHERE id=?",
        (budget, max_pct, daily_loss_limit, user["id"])
    )
    db.commit()
    return jsonify({"ok": True, "budget": budget, "max_pct": max_pct, "daily_loss_limit": daily_loss_limit})


@app.route("/api/autopilot/stats")
@login_required
def api_autopilot_stats():
    user = current_user()
    db   = get_db()

    def _safe_default(err=""):
        # rollback any aborted postgres transaction before retrying
        try:
            if hasattr(db, 'rollback'):
                db.rollback()
            base = db.execute("SELECT autopilot, balance FROM users WHERE id=?", (user["id"],)).fetchone()
            enabled = bool(base["autopilot"]) if base else False
            avail   = float(base["balance"])  if base else 0
        except Exception:
            enabled = False
            avail   = 0
        return jsonify({
            "enabled": enabled, "budget": 0, "deployed": 0, "available": avail,
            "today_pnl": 0, "total_pnl": 0, "trade_count": 0, "today_trades": 0,
            "paused_today": False, "daily_loss_limit": 500, "max_pct": 20,
            "logs": [], "positions": [], "ai_available": bool(_ai_client),
            "_error": err,
        })

    try:
        row = db.execute(
            "SELECT autopilot, autopilot_budget, autopilot_max_pct, "
            "autopilot_daily_loss_limit, balance FROM users WHERE id=?",
            (user["id"],)
        ).fetchone()
    except Exception as e:
        import traceback; traceback.print_exc()
        return _safe_default(str(e))

    try:
        budget       = float(row["autopilot_budget"] or 0)
        max_pct_val  = float(row["autopilot_max_pct"] or 20)
        daily_limit  = float(row["autopilot_daily_loss_limit"] or 500)
        balance      = float(row["balance"] or 0)
    except Exception as e:
        import traceback; traceback.print_exc()
        return _safe_default(str(e))

    try:
        deployed  = _autopilot_deployed(user["id"], db)
        available = max(0, budget - deployed) if budget > 0 else balance
    except Exception:
        deployed = 0.0
        available = balance

    today_pnl = _autopilot_today_pnl(user["id"], db)

    try:
        total_pnl = float(db.execute(
            "SELECT COALESCE(SUM(pnl),0) as total FROM trades "
            "WHERE user_id=? AND source='autopilot' AND side='sell'",
            (user["id"],)
        ).fetchone()["total"] or 0)
    except Exception:
        total_pnl = 0.0

    try:
        trade_count = db.execute(
            "SELECT COUNT(*) as n FROM trades WHERE user_id=? AND source='autopilot'",
            (user["id"],)
        ).fetchone()["n"]
    except Exception:
        trade_count = 0

    try:
        today = datetime.now().date().isoformat()
        today_trades = db.execute(
            "SELECT COUNT(*) as n FROM trades "
            "WHERE user_id=? AND source='autopilot' AND DATE(executed_at) = ?",
            (user["id"], today)
        ).fetchone()["n"]
    except Exception:
        today_trades = 0

    try:
        logs = [dict(l) for l in db.execute(
            "SELECT action, symbol, qty, price, reasoning, executed_at "
            "FROM autopilot_log WHERE user_id=? ORDER BY executed_at DESC LIMIT 10",
            (user["id"],)
        ).fetchall()]
    except Exception:
        logs = []

    try:
        ap_sym_rows = db.execute(
            "SELECT DISTINCT symbol FROM trades "
            "WHERE user_id=? AND source='autopilot' AND side='buy'",
            (user["id"],)
        ).fetchall()
        ap_symbols = {r["symbol"] for r in ap_sym_rows}
    except Exception:
        ap_symbols = set()

    ap_positions = []
    for sym in ap_symbols:
        try:
            pos = db.execute(
                "SELECT symbol, qty, avg_price FROM positions "
                "WHERE user_id=? AND symbol=? AND side='long'",
                (user["id"], sym)
            ).fetchone()
            if pos:
                try:
                    cur_price, _ = _get_price(sym)
                    unreal = round((cur_price - pos["avg_price"]) * pos["qty"], 2)
                except Exception:
                    cur_price = pos["avg_price"]
                    unreal = 0.0
                ap_positions.append({
                    "symbol": sym, "qty": pos["qty"],
                    "avg_price": pos["avg_price"],
                    "current_price": round(cur_price, 2),
                    "market_value": round(cur_price * pos["qty"], 2),
                    "unrealized_pnl": unreal,
                })
        except Exception:
            pass

    paused_today = today_pnl <= -abs(daily_limit)

    return jsonify({
        "enabled":          bool(row["autopilot"]),
        "budget":           budget,
        "deployed":         round(deployed, 2),
        "available":        round(available, 2),
        "today_pnl":        round(today_pnl, 2),
        "total_pnl":        round(total_pnl, 2),
        "trade_count":      trade_count,
        "today_trades":     today_trades,
        "paused_today":     paused_today,
        "daily_loss_limit": daily_limit,
        "max_pct":          max_pct_val,
        "logs":             logs,
        "positions":        ap_positions,
        "ai_available":     bool(_ai_client),
    })


# ── Black-Scholes engine ──────────────────────────────────────────────────────
import math as _math

def _norm_cdf(x):
    return (1 + _math.erf(x / _math.sqrt(2))) / 2

def _norm_pdf(x):
    return _math.exp(-x**2 / 2) / _math.sqrt(2 * _math.pi)

def _bs_price(S, K, T, sigma, r=0.05, opt='call'):
    """Black-Scholes option price. S=spot, K=strike, T=years, sigma=IV."""
    if T <= 0:
        return round(max(0.0, (S - K) if opt == 'call' else (K - S)), 4)
    d1 = (_math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * _math.sqrt(T))
    d2 = d1 - sigma * _math.sqrt(T)
    if opt == 'call':
        price = S * _norm_cdf(d1) - K * _math.exp(-r * T) * _norm_cdf(d2)
    else:
        price = K * _math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    return round(max(0.0, price), 4)

def _bs_greeks(S, K, T, sigma, r=0.05, opt='call'):
    if T <= 0:
        delta = 1.0 if (opt == 'call' and S > K) else (-1.0 if (opt == 'put' and S < K) else 0.0)
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (_math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * _math.sqrt(T))
    d2 = d1 - sigma * _math.sqrt(T)
    delta = _norm_cdf(d1) if opt == 'call' else _norm_cdf(d1) - 1
    gamma = _norm_pdf(d1) / (S * sigma * _math.sqrt(T))
    theta = (-(S * _norm_pdf(d1) * sigma) / (2 * _math.sqrt(T))
             - r * K * _math.exp(-r * T) * (_norm_cdf(d2) if opt == 'call' else _norm_cdf(-d2))) / 365
    vega  = S * _norm_pdf(d1) * _math.sqrt(T) / 100
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega":  round(vega, 4),
    }

def _options_chain(symbol: str, spot: float):
    """Generate a paper options chain around the current spot price."""
    from datetime import date, timedelta
    today = date.today()
    # next 4 weekly Fridays + 2 monthly expiries
    expiries = []
    d = today + timedelta(days=(4 - today.weekday()) % 7 + 1)
    for _ in range(4):
        expiries.append(d.isoformat())
        d += timedelta(weeks=1)
    # monthly: 3rd Friday of next 2 months
    for month_offset in range(1, 3):
        yr, mo = (today.year + (today.month + month_offset - 1) // 12,
                  (today.month + month_offset - 1) % 12 + 1)
        first = date(yr, mo, 1)
        first_fri = first + timedelta(days=(4 - first.weekday()) % 7)
        third_fri = first_fri + timedelta(weeks=2)
        if third_fri.isoformat() not in expiries:
            expiries.append(third_fri.isoformat())
    expiries.sort()

    # strikes: ±25% of spot in sensible increments
    step = 1 if spot < 20 else (5 if spot < 100 else (10 if spot < 500 else 25))
    low  = round(spot * 0.75 / step) * step
    high = round(spot * 1.25 / step) * step
    strikes = []
    k = low
    while k <= high:
        strikes.append(round(k, 2))
        k += step

    chain = {}
    sigma = 0.30  # baseline IV 30%
    r = 0.05
    for exp in expiries:
        T = max(0.001, (_math.floor((date.fromisoformat(exp) - today).days / 365.0 * 1000)) / 1000)
        chain[exp] = []
        for strike in strikes:
            call_price = _bs_price(spot, strike, T, sigma, r, 'call')
            put_price  = _bs_price(spot, strike, T, sigma, r, 'put')
            call_greeks = _bs_greeks(spot, strike, T, sigma, r, 'call')
            put_greeks  = _bs_greeks(spot, strike, T, sigma, r, 'put')
            spread = round(call_price * 0.04, 2)
            chain[exp].append({
                "strike": strike,
                "atm": abs(strike - spot) < step,
                "call": {**call_greeks, "mid": call_price, "bid": round(call_price - spread, 2), "ask": round(call_price + spread, 2), "iv": sigma},
                "put":  {**put_greeks,  "mid": put_price,  "bid": round(put_price  - spread, 2), "ask": round(put_price  + spread, 2), "iv": sigma},
            })
    return {"symbol": symbol, "spot": spot, "expiries": expiries, "chain": chain}


# ── options routes ────────────────────────────────────────────────────────────
@app.route("/options")
@login_required
def options_page():
    return render_template("options.html")


@app.route("/api/options/chain/<symbol>")
@login_required
def api_options_chain(symbol):
    try:
        price, _ = _get_price(symbol.upper())
        return jsonify(_options_chain(symbol.upper(), price))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/positions")
@login_required
def api_options_positions():
    user = current_user()
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM options_positions WHERE user_id=? ORDER BY opened_at DESC",
        (user["id"],)
    ).fetchall()
    result = []
    for r in rows:
        pos = dict(r)
        try:
            spot, _ = _get_price(r["symbol"])
            from datetime import date as _date
            T = max(0, (_date.fromisoformat(r["expiry"]) - _date.today()).days / 365.0)
            cur_price = _bs_price(spot, r["strike"], T, 0.30, 0.05, r["option_type"])
            multiplier = 100 * r["qty"]
            cost_basis = r["premium"] * multiplier
            cur_value  = cur_price * multiplier
            pos["current_premium"] = cur_price
            pos["pnl"] = round((cur_value - cost_basis) if r["side"] == "buy" else (cost_basis - cur_value), 2)
            pos["spot"] = spot
        except Exception:
            pos["current_premium"] = pos["pnl"] = pos["spot"] = None
        result.append(pos)
    return jsonify(result)


@app.route("/api/options/trade", methods=["POST"])
@login_required
def api_options_trade():
    data        = request.get_json()
    symbol      = data.get("symbol","").upper()
    option_type = data.get("option_type","call").lower()   # call | put
    strike      = float(data.get("strike", 0))
    expiry      = data.get("expiry","")
    qty         = int(data.get("qty", 1))
    side        = data.get("side","buy").lower()           # buy | sell

    if not symbol or strike <= 0 or not expiry or qty <= 0:
        return jsonify({"error": "Invalid parameters"}), 400

    user = current_user()
    db   = get_db()

    try:
        spot, _ = _get_price(symbol)
        from datetime import date as _date
        T = max(0, (_date.fromisoformat(expiry) - _date.today()).days / 365.0)
        premium = _bs_price(spot, strike, T, 0.30, 0.05, option_type)
    except Exception as e:
        return jsonify({"error": f"Pricing failed: {e}"}), 500

    cost = premium * 100 * qty   # 1 contract = 100 shares
    pnl  = 0.0

    if side == "buy":
        if user["balance"] < cost:
            return jsonify({"error": "Insufficient balance"}), 400
        db.execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user["id"]))
        existing = db.execute(
            "SELECT * FROM options_positions WHERE user_id=? AND symbol=? AND option_type=? AND strike=? AND expiry=? AND side='buy'",
            (user["id"], symbol, option_type, strike, expiry)
        ).fetchone()
        if existing:
            new_qty = existing["qty"] + qty
            new_avg = (existing["premium"] * existing["qty"] + premium * qty) / new_qty
            db.execute("UPDATE options_positions SET qty=?, premium=? WHERE id=?", (new_qty, new_avg, existing["id"]))
        else:
            db.execute(
                "INSERT INTO options_positions (user_id,symbol,option_type,strike,expiry,qty,premium,side) VALUES (?,?,?,?,?,?,?,'buy')",
                (user["id"], symbol, option_type, strike, expiry, qty, premium)
            )
    elif side == "sell":
        pos = db.execute(
            "SELECT * FROM options_positions WHERE user_id=? AND symbol=? AND option_type=? AND strike=? AND expiry=? AND side='buy'",
            (user["id"], symbol, option_type, strike, expiry)
        ).fetchone()
        if not pos or pos["qty"] < qty:
            return jsonify({"error": "No matching position to close"}), 400
        pnl = (premium - pos["premium"]) * 100 * qty
        proceeds = premium * 100 * qty
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (proceeds, user["id"]))
        new_qty = pos["qty"] - qty
        if new_qty == 0:
            db.execute("DELETE FROM options_positions WHERE id=?", (pos["id"],))
        else:
            db.execute("UPDATE options_positions SET qty=? WHERE id=?", (new_qty, pos["id"]))

    db.execute(
        "INSERT INTO options_trades (user_id,symbol,option_type,strike,expiry,qty,premium,side,pnl) VALUES (?,?,?,?,?,?,?,?,?)",
        (user["id"], symbol, option_type, strike, expiry, qty, premium, side, pnl)
    )
    db.commit()
    updated = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    return jsonify({"ok": True, "premium": premium, "cost": round(cost, 2), "pnl": round(pnl, 2), "new_balance": round(updated["balance"], 2)})


@app.route("/api/options/strategy", methods=["POST"])
@login_required
def api_options_strategy():
    """Execute a multi-leg options strategy."""
    data     = request.get_json()
    strategy = data.get("strategy","")   # straddle | bull_call_spread | bear_put_spread | covered_call
    symbol   = data.get("symbol","").upper()
    expiry   = data.get("expiry","")
    strike1  = float(data.get("strike1", 0))
    strike2  = float(data.get("strike2", 0))
    qty      = int(data.get("qty", 1))

    user = current_user()
    db   = get_db()

    try:
        spot, _ = _get_price(symbol)
        from datetime import date as _date
        T = max(0, (_date.fromisoformat(expiry) - _date.today()).days / 365.0)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    legs = []
    if strategy == "straddle":
        # buy ATM call + buy ATM put
        legs = [
            (symbol, "call", strike1, expiry, qty, "buy"),
            (symbol, "put",  strike1, expiry, qty, "buy"),
        ]
    elif strategy == "bull_call_spread":
        # buy lower call + sell higher call
        legs = [
            (symbol, "call", strike1, expiry, qty, "buy"),
            (symbol, "call", strike2, expiry, qty, "sell"),
        ]
    elif strategy == "bear_put_spread":
        # buy higher put + sell lower put
        legs = [
            (symbol, "put", strike2, expiry, qty, "buy"),
            (symbol, "put", strike1, expiry, qty, "sell"),
        ]
    elif strategy == "covered_call":
        # sell OTM call (user must own shares)
        legs = [(symbol, "call", strike1, expiry, qty, "sell")]
    else:
        return jsonify({"error": "Unknown strategy"}), 400

    total_cost = 0.0
    for sym, opt_type, strike, exp, q, side in legs:
        p = _bs_price(spot, strike, T, 0.30, 0.05, opt_type)
        total_cost += p * 100 * q * (1 if side == "buy" else -1)

    if total_cost > 0 and user["balance"] < total_cost:
        return jsonify({"error": f"Insufficient balance — need ${total_cost:.2f}"}), 400

    for sym, opt_type, strike, exp, q, side in legs:
        p = _bs_price(spot, strike, T, 0.30, 0.05, opt_type)
        cost = p * 100 * q
        if side == "buy":
            db.execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user["id"]))
            db.execute(
                "INSERT INTO options_positions (user_id,symbol,option_type,strike,expiry,qty,premium,side) VALUES (?,?,?,?,?,?,?,'buy')",
                (user["id"], sym, opt_type, strike, exp, q, p)
            )
        else:
            db.execute("UPDATE users SET balance=balance+? WHERE id=?", (cost, user["id"]))
            db.execute(
                "INSERT INTO options_positions (user_id,symbol,option_type,strike,expiry,qty,premium,side) VALUES (?,?,?,?,?,?,?,'sell')",
                (user["id"], sym, opt_type, strike, exp, q, p)
            )
        db.execute(
            "INSERT INTO options_trades (user_id,symbol,option_type,strike,expiry,qty,premium,side,pnl) VALUES (?,?,?,?,?,?,?,?,0)",
            (user["id"], sym, opt_type, strike, exp, q, p, side)
        )

    db.commit()
    updated = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    return jsonify({"ok": True, "strategy": strategy, "net_cost": round(total_cost, 2), "new_balance": round(updated["balance"], 2)})


# ── watchlist + market calls routes ──────────────────────────────────────────
@app.route("/calls")
@login_required
def calls_page():
    return render_template("calls.html")


@app.route("/api/watchlist", methods=["GET"])
@login_required
def api_watchlist_get():
    user = current_user()
    db   = get_db()
    rows = db.execute("SELECT symbol FROM watchlist WHERE user_id=? ORDER BY symbol", (user["id"],)).fetchall()
    return jsonify([r["symbol"] for r in rows])


@app.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_add():
    symbol = (request.get_json() or {}).get("symbol","").upper().strip()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    user = current_user()
    db   = get_db()
    try:
        db.execute("INSERT INTO watchlist (user_id, symbol) VALUES (?,?)", (user["id"], symbol))
        db.commit()
    except Exception:
        pass  # already exists
    return jsonify({"ok": True, "symbol": symbol})


@app.route("/api/watchlist/<symbol>", methods=["DELETE"])
@login_required
def api_watchlist_remove(symbol):
    user = current_user()
    db   = get_db()
    db.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (user["id"], symbol.upper()))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/calls")
@login_required
def api_calls_get():
    user = current_user()
    db   = get_db()
    rows = db.execute(
        "SELECT * FROM market_calls WHERE (user_id=? OR user_id IS NULL) AND status='active' ORDER BY created_at DESC LIMIT 30",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/calls/generate", methods=["POST"])
@login_required
def api_calls_generate():
    if not _ai_client:
        return jsonify({"error": "No Anthropic API key"}), 400
    user = current_user()
    db   = get_db()
    watchlist = [r["symbol"] for r in db.execute(
        "SELECT symbol FROM watchlist WHERE user_id=?", (user["id"],)
    ).fetchall()]
    if not watchlist:
        return jsonify({"error": "Add symbols to your watchlist first"}), 400

    # bust news cache so every generate gets the freshest headlines
    _news_cache.clear()

    prices = {}
    for sym in watchlist:
        # bust price cache too so we get fresh prices
        _price_cache.pop(sym.upper(), None)
        try:
            p, _ = _get_price(sym)
            prices[sym] = p
        except Exception:
            pass

    articles = _fetch_news()
    headlines = "\n".join(
        f"- [{a['source']}] {a['title']}" for a in articles[:15]
    ) if articles else "No recent news available."

    # fetch previous calls so Claude doesn't just repeat them
    prev_rows = db.execute(
        "SELECT symbol, direction, reasoning FROM market_calls "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
        (user["id"],)
    ).fetchall()
    prev_summary = "\n".join(
        f"  {r['symbol']}: {r['direction']} — {(r['reasoning'] or '')[:80]}"
        for r in prev_rows
    ) if prev_rows else "  None — this is the first analysis."

    now_str = datetime.now().strftime("%A %B %d %Y, %I:%M %p")
    price_lines = "\n".join(f"  {s}: ${p:.2f}" for s,p in prices.items())

    prompt = f"""You are a sharp, independent sell-side analyst refreshing your trade calls.
Current time: {now_str}

WATCHLIST PRICES (live):
{price_lines}

LATEST MARKET HEADLINES (just fetched):
{headlines}

PREVIOUS CALLS YOU ALREADY MADE (do NOT just repeat these — reassess everything fresh):
{prev_summary}

YOUR TASK:
Re-analyze every symbol from scratch using today's headlines and current prices.
- If the thesis has changed, flip the direction or change the target.
- If a previous call hit its target or stop, say so in the reasoning and set a new one.
- Look for new catalysts, sector rotation, earnings plays, macro shifts.
- Vary your timeframes and conviction levels — not everything is a 75% confidence bullish call.
- At least 1 call must be BEARISH (direction: "bearish") if any symbol looks weak.
- Be specific about WHY this is different from last time.

Respond ONLY with a valid JSON array, no explanation outside it:
[
  {{
    "symbol": "AAPL",
    "direction": "bullish",
    "entry_price": 213.50,
    "target_price": 225.00,
    "stop_loss": 205.00,
    "reasoning": "Fresh catalyst: [specific headline]. Previous bullish call at $210 achieved +1.5% — now raising target to $225 on continued momentum. Risk: [specific risk].",
    "confidence": 72
  }}
]
Only include symbols with genuine conviction. Skip truly neutral setups."""

    try:
        resp = _ai_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        start = raw.find("["); end = raw.rfind("]") + 1
        calls = json.loads(raw[start:end])
    except Exception as e:
        return jsonify({"error": f"AI generation failed: {e}"}), 500

    # archive old calls (mark expired) then insert fresh ones
    for sym in watchlist:
        db.execute(
            "UPDATE market_calls SET status='expired' WHERE user_id=? AND symbol=? AND status='active'",
            (user["id"], sym)
        )

    for call in calls:
        db.execute(
            "INSERT INTO market_calls (user_id,symbol,direction,entry_price,target_price,stop_loss,reasoning,confidence) VALUES (?,?,?,?,?,?,?,?)",
            (user["id"], call.get("symbol","").upper(), call.get("direction","bullish"),
             call.get("entry_price"), call.get("target_price"), call.get("stop_loss"),
             call.get("reasoning",""), call.get("confidence", 50))
        )
    db.commit()
    rows = db.execute(
        "SELECT * FROM market_calls WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ── API: market data ──────────────────────────────────────────────────────────
@app.route("/api/quote/<symbol>")
@login_required
def api_quote(symbol):
    try:
        price, source = _get_price(symbol)
        return jsonify({
            "symbol": symbol.upper(),
            "price": round(price, 4),
            "currency": "USD",
            "source": source,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chart/<symbol>")
@login_required
def api_chart(symbol):
    try:
        hist = _fetch_hist(symbol, days=35)
        data = []
        for ts, row in hist.iterrows():
            data.append({
                "t": int(pd.Timestamp(ts).timestamp() * 1000),
                "o": round(float(row["Open"]), 4),
                "h": round(float(row["High"]), 4),
                "l": round(float(row["Low"]), 4),
                "c": round(float(row["Close"]), 4),
                "v": int(row.get("Volume", 0)),
            })
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── API: signals ──────────────────────────────────────────────────────────────
@app.route("/api/signals")
@login_required
def api_signals():
    articles = _fetch_news()
    sigs = _generate_signals(articles)
    now = datetime.now().timestamp()
    cached_ts = _signals_cache.get("ts", 0)
    return jsonify({
        "signals": sigs,
        "news": articles[:20],
        "news_count": len(articles),
        "generated_at": datetime.fromtimestamp(cached_ts).strftime("%H:%M:%S") if cached_ts else "—",
        "next_refresh_secs": max(0, int(SIGNALS_TTL - (now - cached_ts))),
        "live_news": len(articles) > 0,
    })


@app.route("/api/signals/refresh", methods=["POST"])
@login_required
def api_signals_refresh():
    # Force-bust both caches
    _news_cache.clear()
    _signals_cache.clear()
    articles = _fetch_news()
    sigs = _generate_signals(articles, force=True)
    return jsonify({"ok": True, "signals": sigs, "news": articles[:20]})


# ── API: paper trading ────────────────────────────────────────────────────────
@app.route("/api/trade", methods=["POST"])
@login_required
def api_trade():
    data = request.get_json()
    symbol = data.get("symbol", "").upper()
    qty = float(data.get("qty", 0))
    side = data.get("side", "buy")  # buy | sell

    if not symbol or qty <= 0:
        return jsonify({"error": "Invalid symbol or qty"}), 400

    # get live price
    try:
        price, _ = _get_price(symbol)
    except Exception as e:
        return jsonify({"error": f"Price fetch failed: {e}"}), 500

    db = get_db()
    user = current_user()
    cost = price * qty
    pnl = 0.0

    if side == "buy":
        if user["balance"] < cost:
            return jsonify({"error": "Insufficient balance"}), 400
        # deduct balance
        db.execute("UPDATE users SET balance=balance-? WHERE id=?", (cost, user["id"]))
        # update or create position
        pos = db.execute(
            "SELECT * FROM positions WHERE user_id=? AND symbol=? AND side='long'",
            (user["id"], symbol)
        ).fetchone()
        if pos:
            new_qty = pos["qty"] + qty
            new_avg = (pos["avg_price"] * pos["qty"] + price * qty) / new_qty
            db.execute(
                "UPDATE positions SET qty=?, avg_price=? WHERE id=?",
                (new_qty, new_avg, pos["id"])
            )
        else:
            db.execute(
                "INSERT INTO positions (user_id, symbol, qty, avg_price, side) VALUES (?,?,?,?,'long')",
                (user["id"], symbol, qty, price)
            )
    elif side == "sell":
        pos = db.execute(
            "SELECT * FROM positions WHERE user_id=? AND symbol=? AND side='long'",
            (user["id"], symbol)
        ).fetchone()
        if not pos or pos["qty"] < qty:
            return jsonify({"error": "Not enough shares to sell"}), 400
        pnl = (price - pos["avg_price"]) * qty
        proceeds = price * qty
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (proceeds, user["id"]))
        new_qty = pos["qty"] - qty
        if new_qty < 0.0001:
            db.execute("DELETE FROM positions WHERE id=?", (pos["id"],))
        else:
            db.execute("UPDATE positions SET qty=? WHERE id=?", (new_qty, pos["id"]))
    else:
        return jsonify({"error": "side must be buy or sell"}), 400

    db.execute(
        "INSERT INTO trades (user_id, symbol, qty, price, side, pnl) VALUES (?,?,?,?,?,?)",
        (user["id"], symbol, qty, price, side, pnl)
    )
    _record_snapshot(user["id"], db)
    _check_phase(user["id"], db)
    db.commit()
    _export_backup(db)

    updated = db.execute("SELECT balance FROM users WHERE id=?", (user["id"],)).fetchone()
    return jsonify({
        "ok": True,
        "symbol": symbol,
        "qty": qty,
        "price": price,
        "side": side,
        "pnl": round(pnl, 2),
        "new_balance": round(updated["balance"], 2),
    })


@app.route("/api/positions")
@login_required
def api_positions():
    db = get_db()
    user = current_user()
    positions = db.execute(
        "SELECT * FROM positions WHERE user_id=?", (user["id"],)
    ).fetchall()
    result = []
    for p in positions:
        try:
            current_price, _ = _get_price(p["symbol"])
        except Exception:
            current_price = p["avg_price"]
        unrealized_pnl = (current_price - p["avg_price"]) * p["qty"]
        result.append({
            **dict(p),
            "current_price": round(current_price, 4),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "market_value": round(current_price * p["qty"], 2),
        })
    return jsonify(result)


# ── API: Monte Carlo simulation ───────────────────────────────────────────────
@app.route("/api/montecarlo", methods=["POST"])
@login_required
def api_montecarlo():
    data = request.get_json()
    symbol = data.get("symbol", "SPY").upper()
    days = int(data.get("days", 252))
    simulations = int(data.get("simulations", 500))
    simulations = min(simulations, 1000)  # cap

    try:
        hist = _fetch_hist(symbol, days=370)
        returns = hist["Close"].pct_change().dropna()
        mu = float(returns.mean())
        sigma = float(returns.std())
        last_price = float(hist["Close"].iloc[-1])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    np.random.seed(42)
    paths = []
    for _ in range(simulations):
        prices = [last_price]
        for _ in range(days):
            shock = np.random.normal(mu, sigma)
            prices.append(prices[-1] * (1 + shock))
        paths.append(prices)

    paths_arr = np.array(paths)
    final_prices = paths_arr[:, -1]
    percentiles = {
        "p5": round(float(np.percentile(final_prices, 5)), 2),
        "p25": round(float(np.percentile(final_prices, 25)), 2),
        "p50": round(float(np.percentile(final_prices, 50)), 2),
        "p75": round(float(np.percentile(final_prices, 75)), 2),
        "p95": round(float(np.percentile(final_prices, 95)), 2),
    }
    # send 50 sample paths for chart
    sample_idx = np.random.choice(simulations, size=min(50, simulations), replace=False)
    sample_paths = paths_arr[sample_idx, :].tolist()

    return jsonify({
        "symbol": symbol,
        "days": days,
        "simulations": simulations,
        "last_price": round(last_price, 2),
        "mu_daily": round(mu, 6),
        "sigma_daily": round(sigma, 6),
        "percentiles": percentiles,
        "sample_paths": sample_paths,
    })


# ── API: flashcard progress ───────────────────────────────────────────────────
@app.route("/api/flashcard/progress", methods=["GET"])
@login_required
def get_flash_progress():
    db = get_db()
    user = current_user()
    rows = db.execute(
        "SELECT card_id, known, seen FROM flashcard_progress WHERE user_id=?",
        (user["id"],)
    ).fetchall()
    return jsonify({r["card_id"]: {"known": r["known"], "seen": r["seen"]} for r in rows})


@app.route("/api/flashcard/progress", methods=["POST"])
@login_required
def update_flash_progress():
    data = request.get_json()
    card_id = data.get("card_id")
    known = int(data.get("known", 0))
    db = get_db()
    user = current_user()
    db.execute("""
        INSERT INTO flashcard_progress (user_id, card_id, known, seen)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id, card_id) DO UPDATE SET
            known = excluded.known,
            seen = seen + 1
    """, (user["id"], card_id, known))
    db.commit()
    return jsonify({"ok": True})


# ── API: custom flashcards ────────────────────────────────────────────────────
@app.route("/api/flashcard/custom", methods=["GET"])
@login_required
def get_custom_cards():
    db = get_db()
    user = current_user()
    rows = db.execute(
        "SELECT * FROM custom_cards WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/flashcard/custom", methods=["POST"])
@login_required
def create_custom_card():
    data = request.get_json()
    term       = (data.get("term") or "").strip()
    definition = (data.get("definition") or "").strip()
    category   = (data.get("category") or "My Cards").strip()
    if not term or not definition:
        return jsonify({"error": "Term and definition are required"}), 400
    db = get_db()
    user = current_user()
    db.execute(
        "INSERT INTO custom_cards (user_id, term, definition, category) VALUES (?,?,?,?)",
        (user["id"], term, definition, category)
    )
    db.commit()
    row = db.execute(
        "SELECT * FROM custom_cards WHERE user_id=? ORDER BY id DESC LIMIT 1",
        (user["id"],)
    ).fetchone()
    return jsonify({"ok": True, "card": dict(row)})


@app.route("/api/flashcard/custom/<int:card_id>", methods=["DELETE"])
@login_required
def delete_custom_card(card_id):
    db = get_db()
    user = current_user()
    db.execute(
        "DELETE FROM custom_cards WHERE id=? AND user_id=?",
        (card_id, user["id"])
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/flashcard/custom/<int:card_id>", methods=["PUT"])
@login_required
def edit_custom_card(card_id):
    data = request.get_json()
    term       = (data.get("term") or "").strip()
    definition = (data.get("definition") or "").strip()
    category   = (data.get("category") or "My Cards").strip()
    if not term or not definition:
        return jsonify({"error": "Term and definition are required"}), 400
    db = get_db()
    user = current_user()
    db.execute(
        "UPDATE custom_cards SET term=?, definition=?, category=? WHERE id=? AND user_id=?",
        (term, definition, category, card_id, user["id"])
    )
    db.commit()
    return jsonify({"ok": True})


# ── API: AI tutor ─────────────────────────────────────────────────────────────
@app.route("/api/ai/chat", methods=["POST"])
@login_required
def ai_chat():
    data = request.get_json()
    user_msg = data.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Empty message"}), 400

    system_prompt = """You are QuantBot, an expert AI trading tutor for a paper trading platform.
You teach users about:
- Technical analysis patterns (head & shoulders, flags, wedges, RSI, MACD, Bollinger Bands, etc.)
- Quantitative methods: Monte Carlo simulation, Sharpe ratio, Value at Risk (VaR), Kelly criterion, mean reversion, momentum strategies
- Risk management: position sizing, stop-losses, drawdown, diversification
- Market microstructure: bid-ask spread, order flow, liquidity
- Statistical concepts: standard deviation, correlation, beta, alpha, regression

Keep responses concise (under 300 words), use examples, and be encouraging.
When explaining formulas, use plain text notation. Always connect theory to practical trading decisions."""

    if _ai_client:
        try:
            response = _ai_client.messages.create(
                model="claude-opus-4-6",
                max_tokens=512,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}]
            )
            reply = response.content[0].text
        except Exception as e:
            reply = _fallback_ai(user_msg)
    else:
        reply = _fallback_ai(user_msg)

    return jsonify({"reply": reply})


def _fallback_ai(msg: str) -> str:
    msg_l = msg.lower()
    if "monte carlo" in msg_l:
        return ("Monte Carlo simulation runs thousands of random price paths using your asset's historical mean return (μ) "
                "and volatility (σ). Each path follows: P(t+1) = P(t) × (1 + N(μ, σ)). "
                "The distribution of final prices shows you the probability of profit/loss over your holding period. "
                "On this platform you can run it live in the Quant Lab → Monte Carlo tab.")
    if "sharpe" in msg_l:
        return ("The Sharpe Ratio = (Return − Risk-Free Rate) / Std Dev of Returns. "
                "It measures return per unit of risk. A Sharpe above 1.0 is good; above 2.0 is excellent. "
                "In paper trading, track your daily P&L, compute its mean and standard deviation, and annualise: multiply mean × 252 and std × √252.")
    if "rsi" in msg_l:
        return ("RSI (Relative Strength Index) oscillates 0–100. "
                "RSI < 30 = oversold (potential buy), RSI > 70 = overbought (potential sell). "
                "It's calculated from average gains vs average losses over N periods (default 14). "
                "Tip: RSI divergence — price makes a new high but RSI doesn't — often precedes reversals.")
    if "macd" in msg_l:
        return ("MACD = 12-period EMA − 26-period EMA. The Signal Line is a 9-period EMA of MACD. "
                "Buy signal: MACD crosses above signal. Sell signal: MACD crosses below. "
                "The histogram shows the gap between MACD and signal — widening histogram = strengthening trend.")
    if "kelly" in msg_l:
        return ("Kelly Criterion: f* = (bp − q) / b, where b = odds, p = win probability, q = 1−p. "
                "For trading: f* = (Win Rate / Loss Rate) − (Avg Loss / Avg Win). "
                "Most traders use half-Kelly (f*/2) to reduce variance. It tells you what fraction of capital to risk per trade.")
    if "var" in msg_l or "value at risk" in msg_l:
        return ("Value at Risk (VaR) estimates the maximum loss at a confidence level (e.g. 95%) over a time horizon. "
                "Parametric VaR = Portfolio Value × σ × Z-score. For 95% confidence, Z=1.645. "
                "Example: $50k portfolio, daily σ=1.5% → 95% VaR = $50,000 × 0.015 × 1.645 = $1,234 max daily loss.")
    return ("Great question! As your QuantBot tutor, I can explain trading patterns, quant methods (Monte Carlo, Sharpe, VaR, Kelly), "
            "risk management, and more. Try asking about: RSI, MACD, Bollinger Bands, Monte Carlo, Sharpe Ratio, Kelly Criterion, or Value at Risk. "
            "💡 Tip: Add your Anthropic API key in the .env file to unlock full AI responses.")


# ── activity log ─────────────────────────────────────────────────────────────
@app.route("/activity")
@login_required
def activity_page():
    return render_template("activity.html")


@app.route("/api/activity/summary")
@login_required
def api_activity_summary():
    user = current_user()
    db   = get_db()

    balance = user["balance"]
    positions = db.execute(
        "SELECT symbol, qty, avg_price FROM positions WHERE user_id=?", (user["id"],)
    ).fetchall()
    positions_value = sum(p["qty"] * p["avg_price"] for p in positions)

    # AI autopilot stats
    ai_buy_row = db.execute(
        "SELECT COALESCE(SUM(qty*price),0) as total FROM trades WHERE user_id=? AND source='autopilot' AND side='buy'",
        (user["id"],)
    ).fetchone()
    ai_pnl_row = db.execute(
        "SELECT COALESCE(SUM(pnl),0) as total FROM trades WHERE user_id=? AND source='autopilot' AND side='sell'",
        (user["id"],)
    ).fetchone()
    ai_trade_count = db.execute(
        "SELECT COUNT(*) as n FROM trades WHERE user_id=? AND source='autopilot'",
        (user["id"],)
    ).fetchone()

    # Options stats
    opt_spent_row = db.execute(
        "SELECT COALESCE(SUM(premium*qty*100),0) as total FROM options_trades WHERE user_id=? AND side='buy'",
        (user["id"],)
    ).fetchone()
    opt_pnl_row = db.execute(
        "SELECT COALESCE(SUM(pnl),0) as total FROM options_trades WHERE user_id=? AND side='sell'",
        (user["id"],)
    ).fetchone()

    pos_list = [
        {"symbol": p["symbol"], "qty": p["qty"],
         "avg_price": p["avg_price"],
         "value": round(p["qty"] * p["avg_price"], 2)}
        for p in positions
    ]

    return jsonify({
        "cash_balance":     round(balance, 2),
        "positions_value":  round(positions_value, 2),
        "total_portfolio":  round(balance + positions_value, 2),
        "ai_total_deployed": round(ai_buy_row["total"] or 0, 2),
        "ai_realized_pnl":  round(ai_pnl_row["total"] or 0, 2),
        "ai_trade_count":   ai_trade_count["n"] if ai_trade_count else 0,
        "options_spent":    round(opt_spent_row["total"] or 0, 2),
        "options_pnl":      round(opt_pnl_row["total"] or 0, 2),
        "positions":        pos_list,
        "starting_balance": STARTING_BALANCE,
    })


@app.route("/api/activity")
@login_required
def api_activity():
    user = current_user()
    db   = get_db()

    trades = [dict(r) for r in db.execute(
        "SELECT * FROM trades WHERE user_id=? ORDER BY executed_at DESC LIMIT 300",
        (user["id"],)
    ).fetchall()]

    ap_logs = [dict(r) for r in db.execute(
        "SELECT * FROM autopilot_log WHERE user_id=? ORDER BY executed_at DESC LIMIT 300",
        (user["id"],)
    ).fetchall()]

    opt_trades = [dict(r) for r in db.execute(
        "SELECT * FROM options_trades WHERE user_id=? ORDER BY executed_at DESC LIMIT 300",
        (user["id"],)
    ).fetchall()]

    events = []

    # ── stock trades ─────────────────────────────────────────────────────────
    for t in trades:
        src      = t.get("source") or "manual"
        reasoning = t.get("ai_reasoning")

        # For old autopilot trades (pre-migration), try matching from ap_logs
        if src == "autopilot" and not reasoning:
            for ap in ap_logs:
                if (ap.get("action","").upper() == t["side"].upper()
                        and ap.get("symbol") == t["symbol"]):
                    reasoning = ap.get("reasoning")
                    break

        impact = -(t["price"] * t["qty"]) if t["side"] == "buy" else (t["price"] * t["qty"])
        events.append({
            "id":             f"trade-{t['id']}",
            "type":           "trade",
            "source":         src,
            "symbol":         t["symbol"],
            "qty":            t["qty"],
            "price":          t["price"],
            "side":           t["side"],
            "pnl":            t.get("pnl", 0),
            "balance_impact": round(impact, 2),
            "executed_at":    t["executed_at"],
            "reasoning":      reasoning,
        })

    # ── autopilot HOLD / SKIP decisions (not reflected in trades table) ───────
    for ap in ap_logs:
        action = (ap.get("action") or "").upper()
        if action == "HOLD" or action.startswith("SKIPPED") or action.startswith("SKIP"):
            events.append({
                "id":             f"ap-{ap['id']}",
                "type":           "ai_decision",
                "source":         "autopilot",
                "action":         action,
                "symbol":         ap.get("symbol"),
                "qty":            ap.get("qty"),
                "price":          ap.get("price"),
                "pnl":            None,
                "balance_impact": 0,
                "executed_at":    ap["executed_at"],
                "reasoning":      ap.get("reasoning"),
            })

    # ── options trades ────────────────────────────────────────────────────────
    for ot in opt_trades:
        multiplier = 100 * ot["qty"]
        impact = -(ot["premium"] * multiplier) if ot["side"] == "buy" else (ot["premium"] * multiplier)
        events.append({
            "id":             f"opt-{ot['id']}",
            "type":           "options_trade",
            "source":         "options",
            "symbol":         ot["symbol"],
            "option_type":    ot.get("option_type"),
            "strike":         ot.get("strike"),
            "expiry":         ot.get("expiry"),
            "qty":            ot["qty"],
            "price":          ot.get("premium"),
            "side":           ot["side"],
            "pnl":            ot.get("pnl", 0),
            "balance_impact": round(impact, 2),
            "executed_at":    ot["executed_at"],
            "reasoning":      None,
        })

    events.sort(key=lambda x: str(x.get("executed_at") or ""), reverse=True)
    return jsonify(events[:300])


# ── password reset ────────────────────────────────────────────────────────────
def _send_reset_email(to_email: str, reset_url: str):
    """Send reset email via SMTP if env vars are set, otherwise silently skip."""
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    if not (smtp_host and smtp_user and smtp_pass):
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        body = f"""Hi,

You requested a password reset for your MockFolio account.

Click the link below to set a new password (expires in 1 hour):
{reset_url}

If you didn't request this, ignore this email.

— MockFolio
"""
        msg = MIMEText(body)
        msg["Subject"] = "MockFolio — Reset your password"
        msg["From"]    = smtp_user
        msg["To"]      = to_email
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, [to_email], msg.as_string())
        return True
    except Exception as e:
        print(f"[email] send failed: {e}")
        return False


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    data  = request.get_json() or request.form
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"ok": False, "error": "Email is required"}), 400

    db   = get_db()
    user = db.execute("SELECT id, email FROM users WHERE email=?", (email,)).fetchone()

    # Always return ok=True to not leak whether email exists
    if not user:
        return jsonify({"ok": True, "emailed": False, "message": "If that email is registered you'll see the reset link below."})

    token     = secrets.token_urlsafe(32)
    expires   = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    # clear any existing tokens for this user
    db.execute("DELETE FROM password_reset_tokens WHERE user_id=?", (user["id"],))
    db.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)",
        (user["id"], token, expires)
    )
    db.commit()

    base_url  = request.host_url.rstrip("/")
    reset_url = f"{base_url}/reset-password/{token}"
    emailed   = _send_reset_email(email, reset_url)

    return jsonify({
        "ok":       True,
        "emailed":  emailed,
        "reset_url": reset_url,   # shown on-screen when no email configured
        "message":  "Reset link sent to your email." if emailed else "Email not configured — use the link below."
    })


@app.route("/reset-password/<token>", methods=["GET"])
def reset_password_page(token):
    return render_template("reset_password.html", token=token)


@app.route("/api/reset-password", methods=["POST"])
def api_reset_password():
    data     = request.get_json() or {}
    token    = data.get("token", "")
    password = data.get("password", "")

    if not token or not password or len(password) < 6:
        return jsonify({"ok": False, "error": "Invalid request — password must be at least 6 characters"}), 400

    db  = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = db.execute(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0 AND expires_at > ?",
        (token, now)
    ).fetchone()

    if not row:
        return jsonify({"ok": False, "error": "This reset link has expired or already been used. Request a new one."}), 400

    db.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(password), row["user_id"]))
    db.execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (row["id"],))
    db.commit()
    return jsonify({"ok": True})


# ── run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)
