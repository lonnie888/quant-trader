"""Flask app factory for the Quant Trader Web Dashboard."""
import os
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session


def create_app() -> Flask:
    app = Flask(__name__,
                template_folder="templates",
                static_folder="static",
                static_url_path="/static")
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # Auth
    app.secret_key = os.urandom(32).hex()
    WEB_PASSWORD = os.environ.get("QT_PASSWORD", "quant888")

    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("authenticated"):
                return redirect(url_for("login_page", next=request.path))
            return f(*args, **kwargs)
        return wrapper

    from .api import api_bp
    from .sse import sse_bp

    # Protect API endpoints
    @api_bp.before_request
    def api_auth():
        if not session.get("authenticated"):
            return {"error": "unauthorized"}, 401

    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(sse_bp, url_prefix="/api")

    # ── Auth pages ─────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "POST":
            pwd = request.form.get("password", "")
            if pwd == WEB_PASSWORD:
                session["authenticated"] = True
                return redirect(request.args.get("next", "/"))
            return render_template("login.html", error="密码错误")
        return render_template("login.html", error=None)

    @app.route("/logout")
    def logout():
        session.pop("authenticated", None)
        return redirect("/login")

    # ── Frontend page routes ───────────────────────────────────

    @app.route("/")
    @login_required
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/positions")
    @login_required
    def positions():
        return render_template("positions.html")

    @app.route("/history")
    @login_required
    def history():
        return render_template("history.html")

    @app.route("/chart")
    @login_required
    def chart():
        return render_template("chart.html")

    return app