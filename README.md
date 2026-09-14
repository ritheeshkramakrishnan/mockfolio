# MockFolio

A paper-trading platform for learning to invest without risking real money. Users start with a simulated $50,000, trade stocks and options against live/EOD market data, run an AI-assisted "autopilot" strategy, get AI-generated market calls and signals, and study markets through built-in flashcards and an AI tutor chat.

## Features

- **Paper trading** — buy/sell stocks against live (Twelve Data), end-of-day (Stooq), or simulated price fallbacks, with realized/unrealized P&L tracking and portfolio snapshots over time.
- **Options trading** — calls/puts with strike, expiry, and premium tracking, plus basic multi-leg strategy support.
- **Autopilot** — an automated strategy engine that can scan the market and place trades on a schedule within a configurable budget, max position size, and daily loss limit.
- **Signals & market calls** — AI/heuristic-generated trade ideas with entry, target, and stop-loss levels, refreshed periodically and cached.
- **Watchlist** — track symbols outside your open positions.
- **Flashcards** — spaced-repetition-style study cards (built-in + user-created) for learning trading/finance concepts.
- **AI chat tutor** — a chat assistant that explains technical analysis and quant concepts (RSI, MACD, Monte Carlo, Sharpe ratio, Kelly criterion, VaR, etc.), with a rule-based fallback when no AI key is configured.
- **Monte Carlo simulation** — projects a distribution of future portfolio outcomes from historical mean return and volatility.
- **Activity log** — a unified history of trades, autopilot actions, and account events.

## Tech stack

- **Backend**: Flask (Python), SQLite locally / PostgreSQL in production, APScheduler for background jobs (autopilot scans, signal refresh).
- **Frontend**: server-rendered Jinja templates + vanilla JS/CSS (`static/`, `templates/`).
- **Data**: Twelve Data (live quotes), Stooq (EOD fallback), RSS feeds (MarketWatch, Reuters, CNBC, Yahoo Finance, AP) for news-driven signals.
- **AI**: Groq-backed chat for the AI tutor and autopilot reasoning, with graceful fallback when no API key is set.
- **Deployment**: [Render](https://render.com), via `Procfile` (`gunicorn`), `runtime.txt` (Python 3.11), and `render.yaml` (infra-as-code blueprint).

## Architecture

`app.py` is a thin entrypoint (`from mockfolio import create_app; app = create_app()`); the application lives in the `mockfolio/` package, structured as a Flask application factory with one blueprint per feature area:

```
mockfolio/
  __init__.py          create_app(): wires config, DB init, blueprints, scheduler
  db.py                 SQLite/PostgreSQL connection layer + schema/migrations
  market.py              price/history fetching (live → EOD → simulated fallback)
  ai_client.py            Groq client init
  news.py                 news fetching + AI signal generation
  portfolio.py             snapshots, phase promotion, JSON/CSV backup
  autopilot_engine.py      the autopilot trading engine + background scheduler
  options_math.py          Black-Scholes pricing engine
  blueprints/
    auth.py, trading.py, autopilot.py, options.py,
    signals.py, flashcards.py, ai_chat.py, activity.py
```

The database layer supports both SQLite (via the stdlib `sqlite3` module) and PostgreSQL (via a thin `psycopg2` wrapper, `_PgConn`, that mirrors the `sqlite3` connection API) so the same query code runs against either backend depending on whether `DATABASE_URL` is set. Code with no Flask request context (the background scheduler thread) uses `mockfolio.db.raw_connection()` directly instead of the request-scoped `get_db()`.

## Getting started

### Prerequisites
- Python 3.11
- pip

### Setup

```bash
git clone <repo-url>
cd Mockfolio
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in at least `SECRET_KEY`. See [Environment variables](#environment-variables) below for what's required vs optional.

### Run locally

```bash
python app.py
```

The app starts on `http://localhost:5000` using a local SQLite database (`mockfolio.db`, created automatically on first run).

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes (production) | Flask session-signing key. Without it, a random key is generated per process — sessions won't survive a restart or work across multiple workers. Always set this explicitly outside local dev. |
| `DATABASE_URL` | No | PostgreSQL connection string (e.g. from Render). When unset, the app uses local SQLite (`mockfolio.db`). |
| `GROQ_API_KEY` | No | Enables the AI chat tutor and AI-generated autopilot reasoning. Without it, the app falls back to rule-based responses. |
| `TWELVEDATA_API_KEY` | No | Enables live quotes. Without it, prices fall back to end-of-day data (Stooq) or a simulated random walk. |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` / `SMTP_PORT` | No | Enables password-reset emails. |
| `FLASK_ENV` | No | Set to `production` in deployment to disable debug/template auto-reload. |
| `PORT` | No | Port to bind (defaults to `5000`; set automatically by Render). |
| `MOCKFOLIO_DB_PATH` / `MOCKFOLIO_DATA_DIR` / `MOCKFOLIO_DISABLE_SCHEDULER` | No | Test-only overrides used by the pytest suite to isolate the database, JSON/CSV backup directory, and background scheduler from your real local data. Not needed for normal local development or deployment. |

## Security

- **Passwords** are hashed with `werkzeug.security` (scrypt/pbkdf2 with a per-password salt). Accounts created before this change are transparently upgraded to the new hash the next time they log in — no forced reset needed.
- **Rate limiting** (`Flask-Limiter`) throttles `/login`, `/register`, `/forgot-password`, and `/api/reset-password` to slow down credential stuffing and brute-force attempts. Uses in-memory storage, which is fine for this app's single-worker deployment — if you scale to multiple gunicorn workers, switch to a shared backend (Redis) per the [Flask-Limiter docs](https://flask-limiter.readthedocs.io).
- **Session cookies** are `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
- **`SECRET_KEY`** is required when `FLASK_ENV=production` — the app refuses to start without it, rather than silently generating a per-process key that would break sessions on every restart/redeploy.
- Auth endpoints require JSON bodies (no form-encoded fallback), so state-changing requests can't be triggered by a plain cross-site `<form>` POST.

## Testing

```bash
pip install -r requirements-dev.txt
pytest -v
```

Tests run against a temporary SQLite database (never your local `mockfolio.db`) and don't require any API keys — price lookups are mocked, and external calls (AI, live quotes) degrade to their built-in fallbacks. CI runs the same suite on every push/PR via GitHub Actions (`.github/workflows/ci.yml`).

## Deployment

Deployed on [Render](https://render.com). Two options:

**Blueprint (recommended)** — the included `render.yaml` provisions the web service and a free PostgreSQL database together:
1. Push this repo to GitHub, then in the Render dashboard: **New → Blueprint**, pick the repo.
2. Render reads `render.yaml`, creates the web service + database, and wires `DATABASE_URL` automatically.
3. Fill in `GROQ_API_KEY` and `TWELVEDATA_API_KEY` in the service's **Environment** tab (marked `sync: false` in the blueprint, so Render prompts for them rather than committing secrets to the repo). `SECRET_KEY` is generated for you.

**Manual** — New → Web Service, point at the repo. Render auto-detects the `Procfile` (`gunicorn app:app`) as the start command and `runtime.txt` for the Python version. Add a PostgreSQL instance separately and set `DATABASE_URL` plus the environment variables above in the service's dashboard.

Render's free tier spins the service down after inactivity (a request takes a few seconds to wake it back up) and free PostgreSQL instances expire after 90 days — recreate the database and update `DATABASE_URL` when that happens, or upgrade to a paid instance.
