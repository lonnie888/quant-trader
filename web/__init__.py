"""Flask app factory for the Quant Trader Web Dashboard."""

from flask import Flask, render_template


def create_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder="static",
                static_url_path="/static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    from .api import api_bp
    from .sse import sse_bp

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(sse_bp, url_prefix="/api")

    # ── Frontend page routes ──────────────────────────────────

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/positions")
    def positions():
        return render_template("positions.html")

    @app.route("/history")
    def history():
        return render_template("history.html")

    @app.route("/chart")
    def chart():
        return render_template("chart.html")

    return app