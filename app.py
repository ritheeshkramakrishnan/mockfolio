"""MockFolio entrypoint. Application code lives in the mockfolio/ package
(see mockfolio/__init__.py for the app factory and mockfolio/blueprints/ for routes)."""
import os

from mockfolio import create_app

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)
