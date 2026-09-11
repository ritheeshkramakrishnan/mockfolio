"""Watchlist, AI market calls, and trade signals."""
import json
from datetime import datetime

from flask import Blueprint, jsonify, render_template, request

from mockfolio import ai_client, market, news
from mockfolio.blueprints.auth import current_user, login_required
from mockfolio.db import get_db

bp = Blueprint("signals", __name__)


@bp.route("/signals")
@login_required
def signals_page():
    return render_template("signals.html")


@bp.route("/calls")
@login_required
def calls_page():
    return render_template("calls.html")


@bp.route("/api/watchlist", methods=["GET"])
@login_required
def api_watchlist_get():
    user = current_user()
    db = get_db()
    rows = db.execute("SELECT symbol FROM watchlist WHERE user_id=? ORDER BY symbol", (user["id"],)).fetchall()
    return jsonify([r["symbol"] for r in rows])


@bp.route("/api/watchlist", methods=["POST"])
@login_required
def api_watchlist_add():
    symbol = (request.get_json() or {}).get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "Symbol required"}), 400
    user = current_user()
    db = get_db()
    try:
        db.execute("INSERT INTO watchlist (user_id, symbol) VALUES (?,?)", (user["id"], symbol))
        db.commit()
    except Exception:
        pass  # already exists
    return jsonify({"ok": True, "symbol": symbol})


@bp.route("/api/watchlist/<symbol>", methods=["DELETE"])
@login_required
def api_watchlist_remove(symbol):
    user = current_user()
    db = get_db()
    db.execute("DELETE FROM watchlist WHERE user_id=? AND symbol=?", (user["id"], symbol.upper()))
    db.commit()
    return jsonify({"ok": True})


@bp.route("/api/calls")
@login_required
def api_calls_get():
    user = current_user()
    db = get_db()
    rows = db.execute(
        "SELECT * FROM market_calls WHERE (user_id=? OR user_id IS NULL) AND status='active' ORDER BY created_at DESC LIMIT 30",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/calls/generate", methods=["POST"])
@login_required
def api_calls_generate():
    if not ai_client._ai_client:
        return jsonify({"error": "No Anthropic API key"}), 400
    user = current_user()
    db = get_db()
    watchlist = [r["symbol"] for r in db.execute(
        "SELECT symbol FROM watchlist WHERE user_id=?", (user["id"],)
    ).fetchall()]
    if not watchlist:
        return jsonify({"error": "Add symbols to your watchlist first"}), 400

    # bust news cache so every generate gets the freshest headlines
    news._news_cache.clear()

    prices = {}
    for sym in watchlist:
        # bust price cache too so we get fresh prices
        market._price_cache.pop(sym.upper(), None)
        try:
            p, _ = market._get_price(sym)
            prices[sym] = p
        except Exception:
            pass

    articles = news.fetch_news()
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
    price_lines = "\n".join(f"  {s}: ${p:.2f}" for s, p in prices.items())

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
        resp = ai_client._ai_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.choices[0].message.content.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
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
            (user["id"], call.get("symbol", "").upper(), call.get("direction", "bullish"),
             call.get("entry_price"), call.get("target_price"), call.get("stop_loss"),
             call.get("reasoning", ""), call.get("confidence", 50))
        )
    db.commit()
    rows = db.execute(
        "SELECT * FROM market_calls WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
        (user["id"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route("/api/signals")
@login_required
def api_signals():
    articles = news.fetch_news()
    sigs = news.generate_signals(articles)
    now = datetime.now().timestamp()
    cached_ts = news._signals_cache.get("ts", 0)
    return jsonify({
        "signals": sigs,
        "news": articles[:20],
        "news_count": len(articles),
        "generated_at": datetime.fromtimestamp(cached_ts).strftime("%H:%M:%S") if cached_ts else "—",
        "next_refresh_secs": max(0, int(news.SIGNALS_TTL - (now - cached_ts))),
        "live_news": len(articles) > 0,
    })


@bp.route("/api/signals/refresh", methods=["POST"])
@login_required
def api_signals_refresh():
    # Force-bust both caches
    news._news_cache.clear()
    news._signals_cache.clear()
    articles = news.fetch_news()
    sigs = news.generate_signals(articles, force=True)
    return jsonify({"ok": True, "signals": sigs, "news": articles[:20]})
