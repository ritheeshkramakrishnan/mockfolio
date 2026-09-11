"""Core paper trading: dashboard/trade pages, buy/sell, positions, quotes, charts, Monte Carlo."""
import numpy as np
import pandas as pd
from flask import Blueprint, jsonify, render_template, request

from mockfolio import market
from mockfolio.blueprints.auth import current_user, login_required
from mockfolio.db import PROFIT_TARGET_PCT, STARTING_BALANCE, get_db
from mockfolio.portfolio import check_phase, export_backup, record_snapshot

bp = Blueprint("trading", __name__)


@bp.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@bp.route("/trade")
@login_required
def trade():
    return render_template("trade.html")


@bp.route("/api/me")
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


@bp.route("/api/portfolio/history")
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


@bp.route("/api/quote/<symbol>")
@login_required
def api_quote(symbol):
    try:
        price, source = market._get_price(symbol)
        return jsonify({
            "symbol": symbol.upper(),
            "price": round(price, 4),
            "currency": "USD",
            "source": source,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/chart/<symbol>")
@login_required
def api_chart(symbol):
    try:
        hist = market._fetch_hist(symbol, days=35)
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


@bp.route("/api/trade", methods=["POST"])
@login_required
def api_trade():
    data = request.get_json()
    symbol = data.get("symbol", "").upper()
    try:
        qty = float(data.get("qty", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid symbol or qty"}), 400
    side = data.get("side", "buy")  # buy | sell

    if not symbol or qty <= 0:
        return jsonify({"error": "Invalid symbol or qty"}), 400

    # get live price
    try:
        price, _ = market._get_price(symbol)
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
    record_snapshot(user["id"], db)
    check_phase(user["id"], db)
    db.commit()
    export_backup(db)

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


@bp.route("/api/positions")
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
            current_price, _ = market._get_price(p["symbol"])
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


@bp.route("/api/montecarlo", methods=["POST"])
@login_required
def api_montecarlo():
    data = request.get_json()
    symbol = data.get("symbol", "SPY").upper()
    try:
        days = int(data.get("days", 252))
        simulations = min(int(data.get("simulations", 500)), 1000)  # cap
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400

    try:
        hist = market._fetch_hist(symbol, days=370)
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
