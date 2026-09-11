"""MockFolio: a paper-trading platform. Flask application factory."""
import os
import secrets

from flask import Flask


def create_app() -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    if os.environ.get("FLASK_ENV") != "production":
        app.config["TEMPLATES_AUTO_RELOAD"] = True

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
