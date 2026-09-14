"""Database backend: PostgreSQL in production (via DATABASE_URL, e.g. on Render), SQLite locally.

`get_db()`/`close_db()` are for use inside a Flask request (they use `flask.g`).
`raw_connection()` is for code that runs outside a request context (the
background scheduler thread) and must open/close its own connection.
"""
import os
import sqlite3

from flask import g

DB_PATH = os.environ.get("MOCKFOLIO_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mockfolio.db"
)
STARTING_BALANCE = 50_000.0  # paper money
PROFIT_TARGET_PCT = 0.15      # 15% → must reach $57,500 to go funded

# ── database backend (PostgreSQL in production, SQLite locally) ──────────────
_DB_URL = os.environ.get("DATABASE_URL", "")
if _DB_URL.startswith("postgres://"):          # some hosts (Render, Heroku) use the legacy prefix
    _DB_URL = _DB_URL.replace("postgres://", "postgresql://", 1)
USE_POSTGRES = bool(_DB_URL)

# ── persistent local data directory (~/Documents/MockFolio_Data/) ─────────────
_DATA_DIR = os.environ.get("MOCKFOLIO_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), "Documents", "MockFolio_Data"
)
os.makedirs(_DATA_DIR, exist_ok=True)
BACKUP_PATH = os.path.join(_DATA_DIR, "backup.json")
TRADES_CSV = os.path.join(_DATA_DIR, "trades.csv")


class _PgConn:
    """Thin psycopg2 wrapper that mirrors the sqlite3 connection API used throughout this app."""
    def __init__(self, url):
        import psycopg2
        from psycopg2.extras import RealDictCursor
        self._conn = psycopg2.connect(url)
        self._RDC = RealDictCursor

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=self._RDC)
        cur.execute(sql.replace("?", "%s"), params or ())
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
            cur.execute(stmt)
        cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def raw_connection():
    """Open a standalone DB connection for code with no Flask request context
    (the background scheduler thread). Caller is responsible for closing it."""
    if USE_POSTGRES:
        return _PgConn(_DB_URL)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def get_db():
    if "db" not in g:
        g.db = raw_connection()
    return g.db


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
            try:
                db.rollback()
            except Exception:
                pass

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
        try:
            db.rollback()
        except Exception:
            pass

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
        try:
            db.rollback()
        except Exception:
            pass

    # pending orders — BUYs queued outside market hours, executed at next market open
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS pending_orders (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                symbol     TEXT NOT NULL,
                qty        REAL NOT NULL,
                reasoning  TEXT,
                queued_at  TEXT DEFAULT (datetime('now')),
                status     TEXT DEFAULT 'pending'
            )
        """ if not USE_POSTGRES else """
            CREATE TABLE IF NOT EXISTS pending_orders (
                id         SERIAL PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                symbol     TEXT NOT NULL,
                qty        REAL NOT NULL,
                reasoning  TEXT,
                queued_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                status     TEXT DEFAULT 'pending'
            )
        """)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass

    from mockfolio.portfolio import import_backup, cleanup_snapshots
    import_backup(db)
    cleanup_snapshots(db)
    db.close()
