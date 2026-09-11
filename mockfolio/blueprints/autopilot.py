"""Autopilot routes: toggle, manual run, config, stats, and the /autopilot page."""
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from mockfolio import ai_client, autopilot_engine, market
from mockfolio.blueprints.auth import current_user, login_required
from mockfolio.db import get_db

bp = Blueprint("autopilot", __name__)


@bp.route("/api/autopilot/status")
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
        "ai_available": bool(ai_client._ai_client),
    })


@bp.route("/api/market/status")
@login_required
def api_market_status():
    return jsonify(market.market_status())


@bp.route("/api/autopilot/pending")
@login_required
def api_autopilot_pending():
    user = current_user()
    db = get_db()
    rows = db.execute(
        "SELECT symbol, qty, reasoning, queued_at FROM pending_orders "
        "WHERE user_id=? AND status='pending' ORDER BY queued_at ASC",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/autopilot/toggle", methods=["POST"])
@login_required
def api_autopilot_toggle():
    user = current_user()
    db = get_db()
    row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
    new_state = 0 if (row and row["autopilot"]) else 1
    db.execute("UPDATE users SET autopilot=? WHERE id=?", (new_state, user["id"]))
    db.commit()
    return jsonify({"enabled": bool(new_state)})


@bp.route("/api/autopilot/run", methods=["POST"])
@login_required
def api_autopilot_run():
    try:
        user = current_user()
        db = get_db()
        row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not row["autopilot"]:
            return jsonify({"ok": False, "message": "Autopilot is off — enable it first using the toggle button"}), 400
        if not ai_client._ai_client:
            return jsonify({"ok": False, "message": "No Groq API key — add GROQ_API_KEY to Railway variables"}), 400
        results = autopilot_engine.run_autopilot(user["id"], db)
        return jsonify({"ok": True, "trades": results})
    except RuntimeError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "message": f"Unexpected error: {e}"}), 500


@bp.route("/api/autopilot/cover-losses", methods=["POST"])
@login_required
def api_autopilot_cover_losses():
    """
    One-shot recovery run: Claude sells losing positions and reinvests defensively.
    Does NOT require autopilot to be enabled — available as a standalone action.
    """
    if not ai_client._ai_client:
        return jsonify({"ok": False, "message": "No Groq API key — add GROQ_API_KEY to Railway variables"}), 400
    try:
        user = current_user()
        db = get_db()
        results = autopilot_engine.run_autopilot(user["id"], db, mode="cover_losses")
        cover_trades = [t for t in results if t.get("action", "").startswith("COVER_")]
        return jsonify({
            "ok": True,
            "trades": results,
            "recovered": len(cover_trades),
            "message": f"Recovery complete — {len(cover_trades)} trade(s) executed to cover losses."
                       if cover_trades else "Portfolio reviewed — no immediate recovery trades needed."
        })
    except RuntimeError as e:
        return jsonify({"ok": False, "message": str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "message": f"Recovery failed: {e}"}), 500


@bp.route("/autopilot")
@login_required
def autopilot_page():
    return render_template("autopilot.html")


@bp.route("/api/autopilot/scan-status")
@login_required
def api_autopilot_scan_status():
    now = datetime.now()
    INTERVAL = 5 * 60  # seconds
    # Load from DB if in-memory value was lost (e.g. after a restart)
    last = autopilot_engine._last_autopilot_scan or autopilot_engine.scan_ts_load()
    if last:
        autopilot_engine._last_autopilot_scan = last   # repopulate in-memory for next call
        secs_since = (now - last).total_seconds()
        secs_until = max(0, INTERVAL - secs_since)
        last_str = last.strftime("%H:%M:%S")
    else:
        secs_until = INTERVAL
        last_str = None
    return jsonify({
        "last_scan": last_str,
        "next_scan_secs": int(secs_until),
        "interval_secs": INTERVAL,
    })


@bp.route("/api/autopilot/config", methods=["GET"])
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
            "enabled": bool(row["autopilot"]) if row else False,
            "budget": float(row["autopilot_budget"] or 0) if row else 0,
            "max_pct": float(row["autopilot_max_pct"] or 20) if row else 20,
            "daily_loss_limit": float(row["autopilot_daily_loss_limit"] or 500) if row else 500,
            "ai_available": bool(ai_client._ai_client),
        })
    except Exception:
        try:
            if hasattr(db, 'rollback'):
                db.rollback()
            row = db.execute("SELECT autopilot FROM users WHERE id=?", (user["id"],)).fetchone()
            enabled = bool(row["autopilot"]) if row else False
        except Exception:
            enabled = False
        return jsonify({
            "enabled": enabled, "budget": 0, "max_pct": 20,
            "daily_loss_limit": 500, "ai_available": bool(ai_client._ai_client),
        })


@bp.route("/api/autopilot/config", methods=["POST"])
@login_required
def api_autopilot_config_set():
    data = request.get_json() or {}
    user = current_user()
    db = get_db()

    try:
        budget = float(data.get("budget", 0))
        max_pct = float(data.get("max_pct", 20))
        daily_loss_limit = float(data.get("daily_loss_limit", 500))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400

    # clamp values
    budget = max(0, budget)
    max_pct = max(5, min(100, max_pct))
    daily_loss_limit = max(0, daily_loss_limit)

    db.execute(
        "UPDATE users SET autopilot_budget=?, autopilot_max_pct=?, autopilot_daily_loss_limit=? WHERE id=?",
        (budget, max_pct, daily_loss_limit, user["id"])
    )
    db.commit()
    return jsonify({"ok": True, "budget": budget, "max_pct": max_pct, "daily_loss_limit": daily_loss_limit})


@bp.route("/api/autopilot/stats")
@login_required
def api_autopilot_stats():
    user = current_user()
    db = get_db()

    def _safe_default(err=""):
        # rollback any aborted postgres transaction before retrying
        try:
            if hasattr(db, 'rollback'):
                db.rollback()
            base = db.execute("SELECT autopilot, balance FROM users WHERE id=?", (user["id"],)).fetchone()
            enabled = bool(base["autopilot"]) if base else False
            avail = float(base["balance"]) if base else 0
        except Exception:
            enabled = False
            avail = 0
        return jsonify({
            "enabled": enabled, "budget": 0, "deployed": 0, "available": avail,
            "today_pnl": 0, "total_pnl": 0, "trade_count": 0, "today_trades": 0,
            "paused_today": False, "daily_loss_limit": 500, "max_pct": 20,
            "logs": [], "positions": [], "ai_available": bool(ai_client._ai_client),
            "_error": err,
        })

    try:
        row = db.execute(
            "SELECT autopilot, autopilot_budget, autopilot_max_pct, "
            "autopilot_daily_loss_limit, balance FROM users WHERE id=?",
            (user["id"],)
        ).fetchone()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _safe_default(str(e))

    try:
        budget = float(row["autopilot_budget"] or 0)
        max_pct_val = float(row["autopilot_max_pct"] or 20)
        daily_limit = float(row["autopilot_daily_loss_limit"] or 500)
        balance = float(row["balance"] or 0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return _safe_default(str(e))

    try:
        deployed = autopilot_engine._autopilot_deployed(user["id"], db)
        available = max(0, budget - deployed) if budget > 0 else balance
    except Exception:
        deployed = 0.0
        available = balance

    today_pnl = autopilot_engine._autopilot_today_pnl(user["id"], db)

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
                    cur_price, _ = market._get_price(sym)
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
        "enabled": bool(row["autopilot"]),
        "budget": budget,
        "deployed": round(deployed, 2),
        "available": round(available, 2),
        "today_pnl": round(today_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "trade_count": trade_count,
        "today_trades": today_trades,
        "paused_today": paused_today,
        "daily_loss_limit": daily_limit,
        "max_pct": max_pct_val,
        "logs": logs,
        "positions": ap_positions,
        "ai_available": bool(ai_client._ai_client),
    })
