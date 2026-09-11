"""Unified activity feed: trades, autopilot decisions, and options trades."""
from flask import Blueprint, jsonify, render_template

from mockfolio.blueprints.auth import current_user, login_required
from mockfolio.db import STARTING_BALANCE, get_db

bp = Blueprint("activity", __name__)


@bp.route("/activity")
@login_required
def activity_page():
    return render_template("activity.html")


@bp.route("/api/activity/summary")
@login_required
def api_activity_summary():
    user = current_user()
    db = get_db()

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
        "cash_balance": round(balance, 2),
        "positions_value": round(positions_value, 2),
        "total_portfolio": round(balance + positions_value, 2),
        "ai_total_deployed": round(ai_buy_row["total"] or 0, 2),
        "ai_realized_pnl": round(ai_pnl_row["total"] or 0, 2),
        "ai_trade_count": ai_trade_count["n"] if ai_trade_count else 0,
        "options_spent": round(opt_spent_row["total"] or 0, 2),
        "options_pnl": round(opt_pnl_row["total"] or 0, 2),
        "positions": pos_list,
        "starting_balance": STARTING_BALANCE,
    })


@bp.route("/api/activity")
@login_required
def api_activity():
    user = current_user()
    db = get_db()

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
        src = t.get("source") or "manual"
        reasoning = t.get("ai_reasoning")

        # For old autopilot trades (pre-migration), try matching from ap_logs
        if src == "autopilot" and not reasoning:
            for ap in ap_logs:
                if (ap.get("action", "").upper() == t["side"].upper()
                        and ap.get("symbol") == t["symbol"]):
                    reasoning = ap.get("reasoning")
                    break

        impact = -(t["price"] * t["qty"]) if t["side"] == "buy" else (t["price"] * t["qty"])
        events.append({
            "id": f"trade-{t['id']}",
            "type": "trade",
            "source": src,
            "symbol": t["symbol"],
            "qty": t["qty"],
            "price": t["price"],
            "side": t["side"],
            "pnl": t.get("pnl", 0),
            "balance_impact": round(impact, 2),
            "executed_at": t["executed_at"],
            "reasoning": reasoning,
        })

    # ── autopilot HOLD / SKIP decisions (not reflected in trades table) ───────
    for ap in ap_logs:
        action = (ap.get("action") or "").upper()
        if action == "HOLD" or action.startswith("SKIPPED") or action.startswith("SKIP"):
            events.append({
                "id": f"ap-{ap['id']}",
                "type": "ai_decision",
                "source": "autopilot",
                "action": action,
                "symbol": ap.get("symbol"),
                "qty": ap.get("qty"),
                "price": ap.get("price"),
                "pnl": None,
                "balance_impact": 0,
                "executed_at": ap["executed_at"],
                "reasoning": ap.get("reasoning"),
            })

    # ── options trades ────────────────────────────────────────────────────────
    for ot in opt_trades:
        multiplier = 100 * ot["qty"]
        impact = -(ot["premium"] * multiplier) if ot["side"] == "buy" else (ot["premium"] * multiplier)
        events.append({
            "id": f"opt-{ot['id']}",
            "type": "options_trade",
            "source": "options",
            "symbol": ot["symbol"],
            "option_type": ot.get("option_type"),
            "strike": ot.get("strike"),
            "expiry": ot.get("expiry"),
            "qty": ot["qty"],
            "price": ot.get("premium"),
            "side": ot["side"],
            "pnl": ot.get("pnl", 0),
            "balance_impact": round(impact, 2),
            "executed_at": ot["executed_at"],
            "reasoning": None,
        })

    events.sort(key=lambda x: str(x.get("executed_at") or ""), reverse=True)
    return jsonify(events[:300])
