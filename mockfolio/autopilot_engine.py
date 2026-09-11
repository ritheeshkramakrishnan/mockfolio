"""The autopilot trading engine: scheduled AI-driven trade decisions, pending
(after-hours) order queuing, and the background scheduler that drives it all."""
import json
from datetime import datetime

from mockfolio import ai_client, market, news
from mockfolio.db import USE_POSTGRES, raw_connection
from mockfolio.portfolio import check_phase, export_backup, record_snapshot

# tracked for the UI (last scan time + whether we've processed today's market open)
_last_autopilot_scan: datetime = None
_market_just_opened: bool = False


def _autopilot_deployed(user_id: int, db) -> float:
    """Return total capital currently deployed in open autopilot positions (cost basis)."""
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


def process_pending_orders(user_id: int, db) -> list:
    """Execute queued BUY orders at market open. Called once per day when market opens."""
    orders = db.execute(
        "SELECT * FROM pending_orders WHERE user_id=? AND status='pending' ORDER BY queued_at ASC",
        (user_id,)
    ).fetchall()
    if not orders:
        return []

    user_row = db.execute("SELECT balance FROM users WHERE id=?", (user_id,)).fetchone()
    if not user_row:
        return []
    balance = float(user_row["balance"])
    log_entries = []

    for order in orders:
        symbol = order["symbol"].upper()
        qty = float(order["qty"])
        reason = (order["reasoning"] or "") + " [Queued order — executed at market open]"
        try:
            price, _ = market._get_price(symbol)
        except Exception:
            db.execute("UPDATE pending_orders SET status='failed' WHERE id=?", (order["id"],))
            continue
        cost = price * qty
        if balance < cost:
            db.execute("UPDATE pending_orders SET status='skipped' WHERE id=?", (order["id"],))
            log_entries.append({"action": "SKIP", "symbol": symbol, "qty": qty,
                                "reasoning": f"Skipped queued BUY {symbol}: insufficient balance (need ${cost:.2f}, have ${balance:.2f})"})
            continue
        open_count = db.execute(
            "SELECT COUNT(*) as n FROM positions WHERE user_id=?", (user_id,)
        ).fetchone()["n"]
        if open_count >= 6:
            db.execute("UPDATE pending_orders SET status='skipped' WHERE id=?", (order["id"],))
            log_entries.append({"action": "SKIP", "symbol": symbol, "qty": qty,
                                "reasoning": f"Skipped queued BUY {symbol}: max 6 positions reached"})
            continue
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
        db.execute(
            "INSERT INTO autopilot_log (user_id,action,symbol,qty,price,reasoning) VALUES (?,?,?,?,?,?)",
            (user_id, "BUY", symbol, qty, price, reason)
        )
        db.execute("UPDATE pending_orders SET status='executed' WHERE id=?", (order["id"],))
        balance -= cost
        log_entries.append({"action": "BUY", "symbol": symbol, "qty": qty, "reasoning": reason})

    if log_entries:
        record_snapshot(user_id, db)
        check_phase(user_id, db)
        db.commit()
        export_backup(db)
    return log_entries


def run_autopilot(user_id: int, db, mode: str = "standard") -> list:
    """
    AI analyzes the portfolio and market, then executes trades automatically.
    mode: "standard" — normal autopilot
          "cover_losses" — defensive recovery: sell losers, buy safe assets
    Market rules enforced:
      - SELLs only execute during market hours (9:30–16:00 ET, Mon–Fri)
      - BUYs outside market hours are queued as pending orders
      - On weekends, only BUYs are queued; SELLs are blocked entirely
      - Queued BUYs are processed at next market open via process_pending_orders
    Respects autopilot_budget (ring-fenced capital) and autopilot_daily_loss_limit.
    Returns a list of log entries for what was done.
    """
    if not ai_client._ai_client:
        return []

    mkt = market.market_status()
    market_open = mkt["open"]       # True only during 9:30–16:00 ET Mon–Fri
    is_weekday = mkt["weekday"]     # Mon–Fri regardless of time
    queue_buys = not market_open    # outside hours → queue BUYs instead of executing
    allow_sells = market_open       # SELLs only during live market hours

    try:
        user_row = db.execute(
            "SELECT balance, phase, autopilot_budget, autopilot_max_pct, autopilot_daily_loss_limit FROM users WHERE id=?",
            (user_id,)
        ).fetchone()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        user_row = db.execute("SELECT balance, phase FROM users WHERE id=?", (user_id,)).fetchone()

    if not user_row:
        return []

    balance = user_row["balance"]
    try:
        budget = float(user_row["autopilot_budget"] or 0)
        max_pct = float(user_row["autopilot_max_pct"] or 20) / 100.0
        daily_loss_limit = float(user_row["autopilot_daily_loss_limit"] or 500)
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
        deployed = _autopilot_deployed(user_id, db)
        available = max(0, budget - deployed)
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
    watchlist_syms = [r["symbol"] for r in watchlist_rows] or ["AAPL", "TSLA", "NVDA", "SPY", "MSFT"]
    symbols_to_check = list({p["symbol"] for p in positions} | set(watchlist_syms[:6]))

    for sym in symbols_to_check:
        try:
            price, _ = market._get_price(sym)
            prices[sym] = price
        except Exception:
            pass

    for p in positions:
        sym = p["symbol"]
        cur = prices.get(sym, p["avg_price"])
        unreal_pnl = (cur - p["avg_price"]) * p["qty"]
        pos_summary.append(f"{sym}: {p['qty']} shares @ avg ${p['avg_price']:.2f}, now ${cur:.2f}, P&L ${unreal_pnl:+.2f}")

    articles = news.fetch_news()
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
            key=lambda s: float(s.split("P&L $")[1].replace(",", "")) if "P&L $" in s else 0
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
{chr(10).join(f'  {s}: ${p:.2f}' for s, p in prices.items())}

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
{chr(10).join(f'  {s}: ${p:.2f}' for s, p in prices.items())}

RECENT NEWS:
{headlines}

MARKET STATUS: {mkt['label']}
{"BUYs will be QUEUED for next market open — do NOT suggest SELLs." if not market_open else "Market is OPEN — BUYs and SELLs execute immediately."}

RULES:
- Max {max_pct*100:.0f}% of available capital per single buy trade
- Max 6 open positions total
- You MUST suggest at least one BUY{"" if not market_open else " or SELL"} — do not return only HOLD
- {"ONLY suggest BUYs — no SELLs while market is closed" if not market_open else "If you have open positions, consider selling the weakest one"}
- If no open positions, buy the most promising symbol from the watchlist

You MUST include at least one BUY{"" if not market_open else " or SELL"}. Respond ONLY with valid JSON:
[
  {{"action": "BUY", "symbol": "AAPL", "qty": 5, "reasoning": "Strong momentum..."}},
  {{"action": "SELL", "symbol": "TSLA", "qty": 2, "reasoning": "Overbought..."}}
]"""

    try:
        response = ai_client._ai_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.choices[0].message.content.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        decisions = json.loads(raw[start:end]) if start != -1 else []
    except Exception as e:
        print(f"[autopilot] AI error: {e}")
        err_str = str(e)
        if "429" in err_str or "rate_limit_exceeded" in err_str:
            import re
            m = re.search(r"try again in ([\d]+m[\d.]+s|[\d.]+s)", err_str)
            wait = f" Try again in {m.group(1)}." if m else " You've used your daily token quota — try again later."
            raise RuntimeError(f"Groq daily token limit reached.{wait}")
        raise RuntimeError(f"AI call failed: {e}")

    # ── hard fallback: if Claude returned only HOLDs, force a trade ──────────
    has_action = any(d.get("action", "").upper() in ("BUY", "SELL") for d in decisions)
    if not has_action:
        # pick the first watchlist symbol we can afford
        forced = None
        for sym in watchlist_syms:
            p = prices.get(sym)
            if not p:
                try:
                    p, _ = market._get_price(sym)
                except Exception:
                    continue
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
        action = decision.get("action", "HOLD").upper()
        symbol = (decision.get("symbol") or "").upper()
        qty = float(decision.get("qty") or 0)
        reason = decision.get("reasoning", "")

        executed_price = None

        if action == "BUY" and symbol and qty > 0:
            price = prices.get(symbol)
            if not price:
                try:
                    price, _ = market._get_price(symbol)
                except Exception:
                    continue
            cost = price * qty
            max_spend = working_cash * max_pct
            if cost > max_spend:
                qty = max(1, int(max_spend / price))
                cost = price * qty

            if queue_buys:
                # Outside market hours — queue the order for next market open
                db.execute(
                    "INSERT INTO pending_orders (user_id,symbol,qty,reasoning) VALUES (?,?,?,?)",
                    (user_id, symbol, qty, reason)
                )
                action = "QUEUED"
                db.execute(
                    "INSERT INTO autopilot_log (user_id,action,symbol,qty,price,reasoning) VALUES (?,?,?,?,?,?)",
                    (user_id, "QUEUED", symbol, qty, None,
                     f"[Queued for market open] {reason}")
                )
                log_entries.append({"action": "QUEUED", "symbol": symbol, "qty": qty,
                                    "reasoning": f"[Queued for market open] {reason}"})
                continue

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
            if not allow_sells:
                # Market is closed — block all sells
                db.execute(
                    "INSERT INTO autopilot_log (user_id,action,symbol,qty,price,reasoning) VALUES (?,?,?,?,?,?)",
                    (user_id, "BLOCKED", symbol, qty, None,
                     f"SELL blocked: market is closed ({mkt['label']}). No sales outside trading hours.")
                )
                log_entries.append({"action": "BLOCKED", "symbol": symbol, "qty": qty,
                                    "reasoning": f"SELL blocked: market is closed ({mkt['label']})"})
                continue

            pos = db.execute(
                "SELECT * FROM positions WHERE user_id=? AND symbol=? AND side='long'",
                (user_id, symbol)
            ).fetchone()
            if not pos or pos["qty"] < qty:
                qty = pos["qty"] if pos else 0
            if pos and qty > 0:
                price = prices.get(symbol)
                if not price:
                    try:
                        price, _ = market._get_price(symbol)
                    except Exception:
                        continue
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

        if action in ("QUEUED", "BLOCKED"):
            continue

        # Cover losses mode: use a distinct log action so the feed can show a shield badge
        log_action = f"COVER_{action}" if (mode == "cover_losses" and action in ("BUY", "SELL")) else action
        db.execute(
            "INSERT INTO autopilot_log (user_id,action,symbol,qty,price,reasoning) VALUES (?,?,?,?,?,?)",
            (user_id, log_action, symbol or None, qty or None, executed_price, reason)
        )
        log_entries.append({"action": log_action, "symbol": symbol, "qty": qty, "reasoning": reason})

    if log_entries:
        record_snapshot(user_id, db)
        check_phase(user_id, db)
        db.commit()
        export_backup(db)

    return log_entries


def _scan_ts_save(dt: datetime):
    """Persist the last autopilot scan timestamp to app_cache."""
    try:
        db = raw_connection()
        val = json.dumps({"ts": dt.isoformat()})
        now_str = dt.isoformat()
        try:
            db.execute("DELETE FROM app_cache WHERE key=?", ("last_autopilot_scan",))
            db.execute("INSERT INTO app_cache(key, value, updated_at) VALUES (?,?,?)",
                       ("last_autopilot_scan", val, now_str))
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[scheduler] scan ts save error: {e}")
        db.close()
    except Exception as e:
        print(f"[scheduler] scan ts save outer error: {e}")


def scan_ts_load():
    """Load the last autopilot scan timestamp from app_cache."""
    try:
        db = raw_connection()
        row = db.execute("SELECT value FROM app_cache WHERE key=?", ("last_autopilot_scan",)).fetchone()
        db.close()
        if row:
            data = json.loads(row["value"])
            return datetime.fromisoformat(data["ts"])
    except Exception as e:
        print(f"[scheduler] scan ts load error: {e}")
    return None


def autopilot_scan_all():
    """
    Run autopilot for every user who has it enabled. Called every 5 minutes.
    Market rules:
      - Only executes live trades during market hours (9:30–16:00 ET, Mon–Fri)
      - Processes queued pending orders once at market open each weekday
      - Skips entirely on weekends (users can still manually queue from the UI)
    """
    global _last_autopilot_scan, _market_just_opened

    mkt = market.market_status()

    if not mkt["weekday"]:
        print(f"[scheduler] weekend — skipping live scan ({mkt['et_time']})")
        return

    if not ai_client._ai_client:
        return

    try:
        db = raw_connection()

        users = db.execute("SELECT id FROM users WHERE autopilot=1").fetchall()

        # ── process pending orders once at market open ────────────────────────
        if mkt["open"] and not _market_just_opened:
            _market_just_opened = True
            print(f"[scheduler] market just opened — processing pending orders for {len(users)} user(s)")
            for u in users:
                try:
                    entries = process_pending_orders(u["id"], db)
                    if entries:
                        print(f"[scheduler] user {u['id']}: executed {len(entries)} pending order(s)")
                except Exception as e:
                    print(f"[scheduler] pending orders user {u['id']} error: {e}")
        elif not mkt["open"]:
            _market_just_opened = False   # reset so we process again next open

        # ── live autopilot scan (market hours only) ───────────────────────────
        if mkt["open"]:
            print(f"[scheduler] autopilot scan — {len(users)} user(s) enabled ({mkt['et_time']})")
            for u in users:
                try:
                    run_autopilot(u["id"], db)
                except Exception as e:
                    print(f"[scheduler] user {u['id']} error: {e}")
            _last_autopilot_scan = datetime.now()
            _scan_ts_save(_last_autopilot_scan)
        else:
            print(f"[scheduler] market closed ({mkt['label']}) — skipping live trades")

        db.close()
    except Exception as e:
        print(f"[scheduler] scan failed: {e}")

    # Refresh AI signals every 5 minutes on weekdays (market hours or pre-market)
    if mkt["weekday"]:
        try:
            print("[scheduler] refreshing AI signals…")
            articles = news.fetch_news(limit=20)
            news._signals_cache.clear()
            news.generate_signals(articles, force=True)
            print("[scheduler] AI signals refreshed")
        except Exception as e:
            print(f"[scheduler] signals refresh failed: {e}")


def start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(autopilot_scan_all, 'interval', minutes=5,
                  id='autopilot_scan', max_instances=1, coalesce=True)
    sched.start()
    print("[scheduler] autopilot background scheduler started (5-min interval)")
    return sched
