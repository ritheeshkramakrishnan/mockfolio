"""Options trading routes: chain, positions, single trades, and multi-leg strategies."""
from datetime import date as _date

from flask import Blueprint, jsonify, render_template, request

from mockfolio import market, options_math
from mockfolio.blueprints.auth import current_user, login_required
from mockfolio.db import get_db

bp = Blueprint("options", __name__)


@bp.route("/options")
@login_required
def options_page():
    return render_template("options.html")


@bp.route("/api/options/chain/<symbol>")
@login_required
def api_options_chain(symbol):
    try:
        price, _ = market._get_price(symbol.upper())
        return jsonify(options_math.options_chain(symbol.upper(), price))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/options/positions")
@login_required
def api_options_positions():
    user = current_user()
    db = get_db()
    rows = db.execute(
        "SELECT * FROM options_positions WHERE user_id=? ORDER BY opened_at DESC",
        (user["id"],)
    ).fetchall()
    result = []
    for r in rows:
        pos = dict(r)
        try:
            spot, _ = market._get_price(r["symbol"])
            T = max(0, (_date.fromisoformat(r["expiry"]) - _date.today()).days / 365.0)
            cur_price = options_math.bs_price(spot, r["strike"], T, 0.30, 0.05, r["option_type"])
            multiplier = 100 * r["qty"]
            cost_basis = r["premium"] * multiplier
            cur_value = cur_price * multiplier
            pos["current_premium"] = cur_price
            pos["pnl"] = round((cur_value - cost_basis) if r["side"] == "buy" else (cost_basis - cur_value), 2)
            pos["spot"] = spot
        except Exception:
            pos["current_premium"] = pos["pnl"] = pos["spot"] = None
        result.append(pos)
    return jsonify(result)


@bp.route("/api/options/trade", methods=["POST"])
@login_required
def api_options_trade():
    data = request.get_json()
    symbol = data.get("symbol", "").upper()
    option_type = data.get("option_type", "call").lower()   # call | put
    try:
        strike = float(data.get("strike", 0))
        qty = int(data.get("qty", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    expiry = data.get("expiry", "")
    side = data.get("side", "buy").lower()           # buy | sell

    if not symbol or strike <= 0 or not expiry or qty <= 0:
        return jsonify({"error": "Invalid parameters"}), 400

    user = current_user()
    db = get_db()

    try:
        spot, _ = market._get_price(symbol)
        T = max(0, (_date.fromisoformat(expiry) - _date.today()).days / 365.0)
        premium = options_math.bs_price(spot, strike, T, 0.30, 0.05, option_type)
    except Exception as e:
        return jsonify({"error": f"Pricing failed: {e}"}), 500

    cost = premium * 100 * qty   # 1 contract = 100 shares
    pnl = 0.0

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


@bp.route("/api/options/strategy", methods=["POST"])
@login_required
def api_options_strategy():
    """Execute a multi-leg options strategy."""
    data = request.get_json()
    strategy = data.get("strategy", "")   # straddle | bull_call_spread | bear_put_spread | covered_call
    symbol = data.get("symbol", "").upper()
    expiry = data.get("expiry", "")
    strike1 = float(data.get("strike1", 0))
    strike2 = float(data.get("strike2", 0))
    qty = int(data.get("qty", 1))

    user = current_user()
    db = get_db()

    try:
        spot, _ = market._get_price(symbol)
        T = max(0, (_date.fromisoformat(expiry) - _date.today()).days / 365.0)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    legs = []
    if strategy == "straddle":
        # buy ATM call + buy ATM put
        legs = [
            (symbol, "call", strike1, expiry, qty, "buy"),
            (symbol, "put", strike1, expiry, qty, "buy"),
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
        p = options_math.bs_price(spot, strike, T, 0.30, 0.05, opt_type)
        total_cost += p * 100 * q * (1 if side == "buy" else -1)

    if total_cost > 0 and user["balance"] < total_cost:
        return jsonify({"error": f"Insufficient balance — need ${total_cost:.2f}"}), 400

    for sym, opt_type, strike, exp, q, side in legs:
        p = options_math.bs_price(spot, strike, T, 0.30, 0.05, opt_type)
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
