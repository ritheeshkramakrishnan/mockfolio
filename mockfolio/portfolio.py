"""Portfolio bookkeeping: phase promotion, value snapshots, and the JSON/CSV
local backup used to survive a Railway redeploy wiping the database."""
import csv
import json
import os
from datetime import datetime, timedelta

from mockfolio.db import BACKUP_PATH, PROFIT_TARGET_PCT, STARTING_BALANCE, TRADES_CSV, _DATA_DIR


def check_phase(user_id: int, db):
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


def record_snapshot(user_id: int, db):
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


def export_backup(db):
    """
    Save full state to ~/Documents/MockFolio_Data/backup.json
    and append the latest trade to trades.csv.
    Called after every trade.
    """
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)

        users = [dict(r) for r in db.execute("SELECT * FROM users").fetchall()]
        trades = [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY executed_at").fetchall()]
        positions = [dict(r) for r in db.execute("SELECT * FROM positions").fetchall()]
        snapshots = [dict(r) for r in db.execute("SELECT * FROM portfolio_snapshots ORDER BY recorded_at").fetchall()]
        cards = [dict(r) for r in db.execute("SELECT * FROM custom_cards").fetchall()]

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
                writer = csv.DictWriter(cf, fieldnames=["id", "user_id", "symbol", "qty", "price", "side", "pnl", "executed_at"])
                if write_header:
                    writer.writeheader()
                writer.writerows(new_trades)

        print(f"[backup] saved → {BACKUP_PATH}")

    except Exception as e:
        print(f"[backup] ERROR saving backup: {e}")
        import traceback
        traceback.print_exc()


def import_backup(db):
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


def cleanup_snapshots(db):
    """
    Prune portfolio snapshots:
    - Remove the opening $50k snapshot once the user has made trades.
    - For snapshots older than 2 days, keep only the last per calendar day.
    """
    try:
        users = db.execute("SELECT id FROM users").fetchall()
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
                        f"DELETE FROM portfolio_snapshots WHERE id IN ({','.join('?' * len(to_delete))})",
                        to_delete
                    )
        db.commit()
    except Exception as e:
        print(f"[cleanup] failed: {e}")
