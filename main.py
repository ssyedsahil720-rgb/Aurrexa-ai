"""AURREXA: authenticated, persistent AI chat built with Flask and SQLite."""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_sqlalchemy import SQLAlchemy
from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, RateLimitError
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("aurrexa")


def _load_or_create_session_secret(instance_path: Path) -> str:
    """Use the configured signing key, or create a private local fallback for Preview."""
    configured = os.getenv("SECRET_KEY", "").strip()
    if configured:
        if len(configured) < 32:
            raise RuntimeError("SECRET_KEY must contain at least 32 characters.")
        return configured

    secret_path = instance_path / ".session-signing-key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(40):
        try:
            descriptor = os.open(str(secret_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            value = secrets.token_urlsafe(48)
            with os.fdopen(descriptor, "w", encoding="utf-8") as key_file:
                key_file.write(value)
                key_file.flush()
                os.fsync(key_file.fileno())
            logger.warning("SECRET_KEY is not configured; using a private instance-local key. Set SECRET_KEY for production.")
            return value
        except FileExistsError:
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
            time.sleep(0.025)
    raise RuntimeError("Could not initialize the Flask session-signing key.")


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


app = Flask(__name__, instance_relative_config=True)
Path(app.instance_path).mkdir(parents=True, exist_ok=True)

sqlite_setting = os.getenv("SQLITE_PATH", "").strip()
if sqlite_setting:
    sqlite_path = Path(sqlite_setting)
    if not sqlite_path.is_absolute():
        sqlite_path = Path(app.root_path) / sqlite_path
else:
    sqlite_path = Path(app.instance_path) / "aurrexa.sqlite3"
sqlite_path.parent.mkdir(parents=True, exist_ok=True)

app.config.update(
    SECRET_KEY=_load_or_create_session_secret(Path(app.instance_path)),
    SQLALCHEMY_DATABASE_URI=f"sqlite:///{sqlite_path.as_posix()}",
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"check_same_thread": False, "timeout": 30}},
    SESSION_COOKIE_NAME="aurrexa_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=_bool_env("AURREXA_COOKIE_SECURE", True),
    SESSION_COOKIE_SAMESITE="None",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    REMEMBER_COOKIE_NAME="aurrexa_remember",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=_bool_env("AURREXA_COOKIE_SECURE", True),
    REMEMBER_COOKIE_SAMESITE="None",
    REMEMBER_COOKIE_DURATION=timedelta(days=30),
    MAX_CONTENT_LENGTH=32 * 1024,
    AURREXA_PUBLIC_URL=os.getenv("AURREXA_PUBLIC_URL", "").strip().rstrip("/"),
    GROQ_MODEL=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.session_protection = "strong"
oauth = OAuth(app)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


@event.listens_for(Engine, "connect")
def _enable_sqlite_safety(dbapi_connection: Any, _connection_record: Any) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(320), unique=True, nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False, default="AURREXA user")
    google_sub = db.Column(db.String(255), unique=True, nullable=False, index=True)
    chats = db.relationship("Chat", back_populates="user", cascade="all, delete-orphan", lazy="dynamic")


class Chat(db.Model):
    __tablename__ = "chats"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user_message = db.Column(db.Text, nullable=False)
    ai_response = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", back_populates="chats")


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@login_manager.unauthorized_handler
def _unauthorized() -> Any:
    if request.path.startswith("/api/"):
        return jsonify(error="Please sign in to continue."), 401
    return redirect(url_for("index"))


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def _inject_csrf_token() -> dict[str, Any]:
    return {"csrf_token": csrf_token()}


def _valid_csrf_request() -> bool:
    stored = session.get("_csrf_token", "")
    supplied = request.headers.get("X-CSRFToken", "")
    return bool(stored and supplied and hmac.compare_digest(str(stored), str(supplied)))


def _require_csrf() -> None:
    if not _valid_csrf_request():
        abort(400, description="Invalid or expired security token. Reload the page and try again.")


def _public_base_url() -> str | None:
    """Use an explicitly configured public origin; never trust the proxy-rewritten Host."""
    origin = app.config["AURREXA_PUBLIC_URL"]
    if not origin:
        return None
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    if parsed.scheme != "https" and not _bool_env("AURREXA_ALLOW_HTTP_ORIGIN", False):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _google_client() -> Any | None:
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        return None
    return oauth.create_client("google")


def _api_error(message: str, status: int) -> Any:
    return jsonify(error=message), status


@app.get("/")
def index() -> str:
    return render_template("index.html", user=current_user)


@app.get("/_app/health")
def health() -> Any:
    return jsonify(status="ok", service="aurrexa"), 200


@app.get("/manus-routes.json")
def route_manifest() -> Any:
    response = jsonify(routes=[{"path": "/", "title": "AURREXA — AI Chat"}])
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/google")
def google_login() -> Any:
    client = _google_client()
    base_url = _public_base_url()
    if client is None:
        flash("Google sign-in is not configured yet. Add the Google OAuth client credentials to the server environment.", "error")
        return redirect(url_for("index"))
    if base_url is None:
        flash("Google sign-in needs AURREXA_PUBLIC_URL set to this site’s public HTTPS origin.", "error")
        return redirect(url_for("index"))

    session.permanent = True
    callback = f"{base_url}{url_for('google_callback')}"
    nonce = secrets.token_urlsafe(24)
    return client.authorize_redirect(callback, nonce=nonce)


@app.get("/auth/google/callback")
def google_callback() -> Any:
    client = _google_client()
    if client is None or _public_base_url() is None:
        flash("Google sign-in is not configured for this site.", "error")
        return redirect(url_for("index"))
    try:
        token = client.authorize_access_token()
        profile = token.get("userinfo")
        if not profile:
            profile = client.userinfo()
        google_sub = str(profile.get("sub", "")).strip()
        email = str(profile.get("email", "")).strip().lower()
        email_verified = profile.get("email_verified") is True
        name = str(profile.get("name", "")).strip()[:160] or "AURREXA user"
        if not google_sub or not email or not email_verified:
            flash("Google did not return a verified account. Please try again with a verified Google account.", "error")
            return redirect(url_for("index"))

        user = User.query.filter_by(google_sub=google_sub).first()
        if user is None:
            user = User.query.filter_by(email=email).first()
            if user is not None and user.google_sub != google_sub:
                flash("This email is already associated with another sign-in identity.", "error")
                return redirect(url_for("index"))
            if user is None:
                user = User(email=email, name=name, google_sub=google_sub)
                db.session.add(user)
            else:
                user.name = name
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                user = User.query.filter_by(google_sub=google_sub).first()
                if user is None:
                    user = User.query.filter_by(email=email).first()
                if user is None:
                    raise
        else:
            user.email = email
            user.name = name
            db.session.commit()

        session.clear()
        session.permanent = True
        login_user(user, remember=True, fresh=True)
        flash("Signed in securely with Google.", "success")
        return redirect(url_for("index"))
    except OAuthError:
        db.session.rollback()
        logger.warning("Google OAuth callback rejected by the provider or state validation.")
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("index"))
    except Exception as exc:
        db.session.rollback()
        logger.warning("Google OAuth callback failed (%s).", type(exc).__name__)
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("index"))


@app.post("/logout")
@login_required
def logout() -> Any:
    _require_csrf()
    logout_user()
    session.clear()
    return redirect(url_for("index"), code=303)


@app.get("/api/history")
@login_required
def history() -> Any:
    try:
        limit = max(1, min(int(request.args.get("limit", "100")), 100))
        before_id_raw = request.args.get("before_id")
        before_id = int(before_id_raw) if before_id_raw else None
        if before_id is not None and before_id < 1:
            raise ValueError
    except ValueError:
        return _api_error("Invalid history cursor.", 400)

    query = Chat.query.filter_by(user_id=current_user.id)
    if before_id is not None:
        query = query.filter(Chat.id < before_id)
    rows = query.order_by(Chat.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    items = [
        {
            "id": row.id,
            "user_message": row.user_message,
            "ai_response": row.ai_response,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]
    return jsonify(items=items, has_more=has_more, next_before_id=(rows[0].id if rows and has_more else None))


@app.post("/api/chat")
@login_required
def send_chat() -> Any:
    _require_csrf()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _api_error("Send a valid JSON message.", 400)
    message = payload.get("message")
    if not isinstance(message, str):
        return _api_error("Your message must be text.", 400)
    message = message.strip()
    if not message:
        return _api_error("Type a message before sending.", 400)
    if len(message) > 8_000:
        return _api_error("Messages are limited to 8,000 characters.", 413)

    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return _api_error("The AI service is not configured. Add GROQ_API_KEY to the server environment.", 503)

    previous = (
        Chat.query.filter_by(user_id=current_user.id)
        .order_by(Chat.id.desc())
        .limit(12)
        .all()
    )
    context_messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": "You are AURREXA, a helpful, accurate, and thoughtful AI assistant. Be clear and concise by default. If you are uncertain, say so. Do not claim to have performed actions you have not performed.",
        }
    ]
    for item in reversed(previous):
        context_messages.append({"role": "user", "content": item.user_message})
        context_messages.append({"role": "assistant", "content": item.ai_response})
    context_messages.append({"role": "user", "content": message})

    try:
        client = Groq(api_key=api_key, timeout=60.0, max_retries=1)
        completion = client.chat.completions.create(
            model=app.config["GROQ_MODEL"],
            messages=context_messages,
            max_completion_tokens=2_048,
            temperature=0.7,
        )
        answer = (completion.choices[0].message.content or "").strip()
        if not answer:
            return _api_error("The AI service returned an empty response. Please try again.", 502)
    except RateLimitError:
        return _api_error("The AI service is busy. Please wait a moment and try again.", 429)
    except APITimeoutError:
        return _api_error("The AI service took too long to respond. Please try again.", 504)
    except APIConnectionError:
        logger.warning("Groq API connection failed.")
        return _api_error("The AI service could not be reached. Please try again shortly.", 502)
    except APIStatusError as exc:
        logger.warning("Groq API returned HTTP %s.", getattr(exc, "status_code", "unknown"))
        return _api_error("The AI service could not complete that response. Please try again.", 502)
    except Exception as exc:
        logger.warning("Groq request failed (%s).", type(exc).__name__)
        return _api_error("The AI service could not complete that response. Please try again.", 502)

    row = Chat(user_id=current_user.id, user_message=message, ai_response=answer)
    try:
        db.session.add(row)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning("Could not save the completed AURREXA exchange (%s).", type(exc).__name__)
        return _api_error("The response arrived, but it could not be saved. Please try again.", 500)

    return jsonify(
        id=row.id,
        user_message=row.user_message,
        ai_response=row.ai_response,
        created_at=row.created_at.isoformat() if row.created_at else None,
    ), 201


@app.errorhandler(400)
def bad_request(error: Any) -> Any:
    if request.path.startswith("/api/") or request.method == "POST":
        description = getattr(error, "description", "The request could not be processed.")
        return _api_error(str(description), 400)
    return error


@app.errorhandler(413)
def request_too_large(_error: Any) -> Any:
    if request.path.startswith("/api/"):
        return _api_error("The request is too large.", 413)
    return "Request too large.", 413


@app.after_request
def add_security_headers(response: Any) -> Any:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    # Do not set X-Frame-Options or restrict CSP frame-ancestors: public Preview is embedded.
    if request.path == "/" or request.path.startswith("/api/") or request.path in {"/logout", "/auth/google", "/auth/google/callback"}:
        response.headers["Cache-Control"] = "private, no-store"
    return response


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, debug=False)
