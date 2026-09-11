"""News fetching and AI-generated trade signals, with a DB-backed cache that
survives restarts and an in-memory cache on top of that for speed."""
import json
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from mockfolio import ai_client
from mockfolio.db import raw_connection

# ── news + signals cache (15 min TTL) ────────────────────────────────────────
_news_cache: dict = {}
_signals_cache: dict = {}
NEWS_TTL = 900
SIGNALS_TTL = 300   # 5 minutes — matches the background scheduler interval

# ── news RSS feeds ────────────────────────────────────────────────────────────
NEWS_FEEDS = [
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Reuters", "https://feeds.reuters.com/reuters/businessNews"),
    ("CNBC Markets", "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("AP Business", "https://feeds.apnews.com/rss/apf-business"),
]


def fetch_news(limit: int = 25) -> list:
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
                desc = (item.findtext("description") or "").strip()
                link = (item.findtext("link") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    articles.append({
                        "title": title,
                        "description": desc[:250] if desc else "",
                        "link": link,
                        "pub_date": pub,
                        "source": source_name,
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


def fallback_signals() -> list:
    return [
        {"ticker": "SPY", "direction": "BUY", "confidence": 65, "timeframe": "1-5 days",
         "thesis": "Broad market momentum stays positive near all-time highs.",
         "risk": "Fed hawkishness or macro shock could reverse trend.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
        {"ticker": "NVDA", "direction": "BUY", "confidence": 70, "timeframe": "1-3 days",
         "thesis": "AI chip demand structural tailwind continues into next earnings.",
         "risk": "Export restrictions or valuation compression.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
        {"ticker": "TLT", "direction": "SELL", "confidence": 60, "timeframe": "3-7 days",
         "thesis": "Rising yields keep pressure on long-duration bond prices.",
         "risk": "Flight-to-safety rally if risk-off sentiment spikes.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
        {"ticker": "QQQ", "direction": "BUY", "confidence": 68, "timeframe": "2-5 days",
         "thesis": "Tech sector strength driven by AI earnings beats and multiple expansion.",
         "risk": "Rate sensitivity and stretched valuations in mega-caps.",
         "news_trigger": "⚠ Add GROQ_API_KEY + live news for real-time AI signals"},
    ]


def _signals_db_load():
    """Load signals from DB cache. Returns (signals_list, timestamp_float) or (None, 0)."""
    try:
        db = raw_connection()
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
        db = raw_connection()
        val = json.dumps({"signals": signals, "ts": datetime.now().timestamp()})
        now_str = datetime.now().isoformat()
        try:
            db.execute("DELETE FROM app_cache WHERE key=?", ("signals",))
            db.execute("INSERT INTO app_cache(key, value, updated_at) VALUES (?,?,?)",
                       ("signals", val, now_str))
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[signals] DB save error (inner): {e}")
        db.close()
    except Exception as e:
        print(f"[signals] DB save error: {e}")


def generate_signals(articles: list, force: bool = False) -> list:
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
    if not ai_client._ai_client or not articles:
        signals = fallback_signals()
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
        response = ai_client._ai_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=900,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.choices[0].message.content.strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        signals = json.loads(text[start:end])
        if not isinstance(signals, list) or len(signals) == 0:
            raise ValueError("empty signals")
    except Exception:
        signals = fallback_signals()

    _signals_cache["signals"] = signals
    _signals_cache["ts"] = now
    _signals_db_save(signals)
    return signals
