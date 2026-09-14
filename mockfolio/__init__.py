"""MockFolio: a paper-trading platform. Flask application factory."""
import os
import secrets

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    is_production = os.environ.get("FLASK_ENV") == "production"

    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        if is_production:
            raise RuntimeError(
                "SECRET_KEY environment variable is required in production — "
                "without it, sessions break on every restart/redeploy and across "
                "gunicorn workers. Set it in your Render (or other host) environment variables."
            )
        secret_key = secrets.token_hex(32)
    app.secret_key = secret_key

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=is_production,
    )
    if not is_production:
        app.config["TEMPLATES_AUTO_RELOAD"] = True

    from mockfolio.limiter import limiter
    # Rate limiting is pointless (and actively breaks) a fast-firing test suite
    # hitting these routes from a single client IP — same test-mode signal used
    # to skip the background scheduler.
    app.config["RATELIMIT_ENABLED"] = not os.environ.get("MOCKFOLIO_DISABLE_SCHEDULER")
    limiter.init_app(app)

    from mockfolio import db as db_module
    app.teardown_appcontext(db_module.close_db)
    db_module.init_db()

    from mockfolio.blueprints import (
        activity, ai_chat, auth, autopilot, flashcards, options, signals, trading,
    )
    for bp_module in (auth, trading, autopilot, options, signals, flashcards, ai_chat, activity):
        app.register_blueprint(bp_module.bp)

    # Start scheduler — guard against Flask debug double-start, gunicorn pre-fork, and tests
    if not (os.environ.get("WERKZEUG_RUN_MAIN") == "false") and not os.environ.get("MOCKFOLIO_DISABLE_SCHEDULER"):
        from mockfolio.autopilot_engine import start_scheduler
        start_scheduler()

    return app
