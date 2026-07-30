"""
OAuth Web Server for Kick Authentication
Handles OAuth 2.0 PKCE flow for linking Kick accounts to Discord

Now includes official Kick API integration with:
- User account linking (user:read scope)
- Bot chat messaging (chat:write scope)
- Webhook subscriptions (events:subscribe scope)
- Stream notifications
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

from authlib.integrations.requests_client import OAuth2Session
from flask import Flask, jsonify, redirect, render_template_string, request
from sqlalchemy import create_engine, text

# Configure logging early. This module is the gunicorn entrypoint (core.oauth_server:app)
# in combined_server.py, a separate process from bot.py, so it must install the root
# handler + server-context filter itself or its logs fall to the default lastResort format.
from utils.logging_config import setup_logging  # noqa: E402
from utils.secret_settings import decrypt_secret, encrypt_secret  # noqa: E402

from .oauth_results import redirect_oauth_error, redirect_oauth_success, redirect_service_page

setup_logging("kick_oauth", log_level=os.getenv("LOG_LEVEL", "INFO"), source_tag="BOT")
logger = logging.getLogger(__name__)

# Sign bot_events messages so the bot's redis_subscriber can reject forged chat.
from utils.redis_signing import sign_payload  # noqa: E402

# Import webhook handlers
try:
    from .kick_official_api import OAUTH_SCOPES, WEBHOOK_EVENTS, KickOfficialAPI
    from .kick_webhooks import WebhookEventHandler, register_webhook_routes

    HAS_KICK_OFFICIAL = True
except ImportError:
    HAS_KICK_OFFICIAL = False
    register_webhook_routes = None
    WebhookEventHandler = None

# Import Twitch webhook handlers (independent of Kick availability)
try:
    from .twitch_webhooks import create_twitch_event_handler, register_twitch_webhook_routes

    HAS_TWITCH = True
except ImportError:
    HAS_TWITCH = False
    register_twitch_webhook_routes = None
    create_twitch_event_handler = None

# -------------------------
# 🔒 Security: OAuth Token Signing
# -------------------------
OAUTH_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

# Debug: Log secret key status at startup (don't log the actual key!)
if os.getenv("FLASK_SECRET_KEY"):
    logger.info(f"[OAuth] FLASK_SECRET_KEY configured: YES")
else:
    logger.info(f"[OAuth] WARNING: FLASK_SECRET_KEY not set, using random key!")


def notify_oauth_link_written():
    """Tell the bot an oauth_notifications row is waiting.

    This runs in the OAuth gunicorn process; the bot is a separate process, so
    the two are bridged by Redis. Without this the bot only learned about a link
    by polling the table every 5 seconds forever — a constant query against the
    database for an event that happens a handful of times a day.

    Fire-and-forget: pub/sub has no delivery guarantee, so the bot keeps a slow
    sweep as a backstop for anything published while it was restarting. Failures
    here are logged and ignored — the sweep will still pick the row up.
    """
    try:
        import json

        import redis

        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            return
        if "://" not in redis_url:
            redis_url = f"redis://{redis_url}"

        client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        client.publish("bot_events", json.dumps(sign_payload({"type": "oauth_notification", "data": {}})))
    except Exception as e:
        logger.info(f"[OAuth] ⚠️ Could not notify bot of link (sweep will catch it): {e}")


def sign_discord_id(discord_id: str, timestamp: int, guild_id: str = "0") -> str:
    """
    Create HMAC signature for Discord ID to prevent OAuth initiation spoofing.
    Returns: base64url-encoded signature
    """
    message = f"{discord_id}:{guild_id}:{timestamp}"
    signature = hmac.new(OAUTH_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


def verify_discord_id_signature(discord_id: str, timestamp: int, signature: str, guild_id: str = "0") -> bool:
    """
    Verify HMAC signature to ensure OAuth was initiated by the bot.
    Returns: True if valid, False otherwise
    """
    # Check timestamp is not too old (max 1 hour)
    now = int(datetime.now(timezone.utc).timestamp())
    age_seconds = abs(now - timestamp)
    if age_seconds > 3600:  # 1 hour expiry
        logger.warning(f"⚠️ OAuth signature expired: {age_seconds}s old (max 3600s)")
        return False

    expected_sig = sign_discord_id(discord_id, timestamp, guild_id)
    is_valid = hmac.compare_digest(expected_sig, signature)

    if not is_valid:
        logger.info(f"🔍 Debug - Signature mismatch:")
        logger.info(f"   Discord ID: {discord_id}")
        logger.info(f"   Guild ID: {guild_id}")
        logger.info(f"   Timestamp: {timestamp} (age: {age_seconds}s)")
        logger.info(f"   Received sig: {signature}")
        logger.info(f"   Expected sig: {expected_sig}")
        logger.info(f"   Secret key set: {'Yes' if OAUTH_SECRET_KEY else 'No'}")

    return is_valid


# -------------------------
# 🔒 OPSEC: Data Sanitization
# -------------------------
def sanitize_for_logs(value, field_name=None):
    """
    Sanitize sensitive data for logging.
    Redacts emails, tokens, codes while keeping debug info.
    """
    if value is None:
        return None

    # Sensitive field names to redact
    sensitive_fields = ["email", "token", "access_token", "refresh_token", "code", "code_verifier"]

    # If field name indicates sensitive data, redact
    if field_name and any(s in field_name.lower() for s in sensitive_fields):
        if isinstance(value, str) and len(value) > 8:
            return f"{value[:4]}...{value[-4:]}"
        return "***REDACTED***"

    # If value looks like an email, redact
    if isinstance(value, str) and "@" in value:
        parts = value.split("@")
        if len(parts) == 2:
            return f"{parts[0][:2]}***@{parts[1]}"

    # If dict, sanitize each field
    if isinstance(value, dict):
        return {k: sanitize_for_logs(v, k) for k, v in value.items()}

    # If list, sanitize each item
    if isinstance(value, list):
        return [sanitize_for_logs(item) for item in value]

    return value


# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))

# Register Kick webhook routes if available
if HAS_KICK_OFFICIAL and register_webhook_routes:
    # Create event handler (can be customized later)
    webhook_handler = WebhookEventHandler()

    @webhook_handler.on("chat.message.sent")
    async def handle_chat_message(event_data):
        """Handle chat messages from Kick webhooks - forward to bot via Redis"""
        try:
            sender = event_data.get("sender", {})
            username = sender.get("username", "Unknown")
            content = event_data.get("content", "")
            message_id = event_data.get("message_id", "")
            broadcaster = event_data.get("broadcaster", {})
            channel_slug = broadcaster.get("slug", "")

            # Get server context added by webhook routing
            server_id = event_data.get("_server_id")
            broadcaster_user_id = event_data.get("_broadcaster_user_id")

            logger.info(f"[Webhook] 💬 [{channel_slug}] {username}: {content}")

            # Use Redis to forward the chat message to the bot process
            import json

            import redis

            redis_url = os.getenv("REDIS_URL")
            if not redis_url:
                logger.info("[Webhook] ⚠️ REDIS_URL not set, cannot forward message to bot")
                return

            if "://" not in redis_url:
                redis_url = f"redis://{redis_url}"

            try:
                redis_client = redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                )

                # Publish chat message event to Redis for bot to process
                event = {
                    "type": "kick_chat_message",
                    "data": {
                        "channel_slug": channel_slug,
                        "username": username,
                        "content": content,
                        "message_id": message_id,
                        "sender": sender,
                        "broadcaster": broadcaster,
                        "_server_id": server_id,  # Include server routing info
                        "_broadcaster_user_id": broadcaster_user_id,
                    },
                }

                redis_client.publish("bot_events", json.dumps(sign_payload(event)))
                logger.info(f"[Webhook] ✅ Forwarded chat message to bot via Redis")

            except Exception as e:
                logger.info(f"[Webhook] ❌ Redis error: {e}")
                import traceback

                traceback.print_exc()

        except Exception as e:
            logger.info(f"[Webhook] ❌ Error handling chat message: {e}")
            import traceback

            traceback.print_exc()

    @webhook_handler.on("livestream.status.updated")
    async def handle_livestream_status(event_data):
        """Handle stream going live/offline - send Discord notification"""
        try:
            is_live = event_data.get("is_live", False)
            broadcaster_data = event_data.get("broadcaster", {})
            broadcaster = broadcaster_data.get("slug") or broadcaster_data.get("username", "")
            livestream = event_data.get("livestream", {})
            title = livestream.get("session_title", "")
            category = livestream.get("category", {}).get("name", "Just Chatting")
            discord_server_id = event_data.get("_server_id")
            is_simulated = event_data.get("_simulated", False)

            logger.info(
                f"[Webhook] 📺 Stream status: {broadcaster} is_live={is_live}, server={discord_server_id}, simulated={is_simulated}"
            )

            # Only send notification when going LIVE
            if not is_live:
                logger.info(f"[Webhook] ℹ️ Stream offline, no notification sent")
                return

            if not discord_server_id:
                logger.info(f"[Webhook] ⚠️ No server_id, cannot send notification")
                return

            # Get notification settings from database
            import aiohttp
            from sqlalchemy import text

            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                logger.info(f"[Webhook] ⚠️ DATABASE_URL not set")
                return

            # Kick webhook → read the Kick live-alert settings (legacy fallback).
            from core.stream_notifications import _alert_columns, _build_live_message, alert_setting

            cols = _alert_columns("kick") + ["kick_channel"]
            ph = ", ".join(f":k{i}" for i in range(len(cols)))
            params = {"guild_id": discord_server_id}
            params.update({f"k{i}": c for i, c in enumerate(cols)})
            # Reuse the shared module engine (was create_engine per webhook - leaked a pool).
            with engine.connect() as conn:
                settings_result = conn.execute(
                    text(
                        f"SELECT key, value FROM bot_settings " f"WHERE discord_server_id = :guild_id AND key IN ({ph})"
                    ),
                    params,
                ).fetchall()

                settings = {key: value for key, value in settings_result}

                if alert_setting(settings, "kick", "enabled") != "true":
                    logger.info(f"[Webhook] ℹ️ Kick live alert disabled for server {discord_server_id}")
                    return

                notification_channel_id = alert_setting(settings, "kick", "channel_id")
                if not notification_channel_id:
                    logger.info(f"[Webhook] ⚠️ No Kick live-alert channel configured for server {discord_server_id}")
                    return

                # Use configured kick_channel or broadcaster from webhook
                streamer = settings.get("kick_channel") or broadcaster
                stream_url = f"https://kick.com/{streamer}"

                message_content, footer_text = _build_live_message(settings, "kick", streamer, title, category)

                # Discord button component for "Watch Stream"
                components = [
                    {
                        "type": 1,
                        "components": [
                            {"type": 2, "style": 5, "label": "Watch Stream", "url": stream_url, "emoji": {"name": "🔴"}}
                        ],
                    }
                ]

                # Send to Discord via API
                bot_token = os.getenv("DISCORD_TOKEN")
                if not bot_token:
                    logger.info(f"[Webhook] ⚠️ DISCORD_TOKEN not set")
                    return

                import asyncio

                # Discord's message-create endpoint can be slow during
                # their bad windows; 10s was tight enough that the response
                # read timed out *after* Discord had already created the
                # message, which made us skip the footer follow-up. Bump to
                # 30s and post the footer independently of the main POST's
                # outcome — the two messages are unrelated Discord objects,
                # the footer doesn't need the main message's ID.
                headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
                channel_url = f"https://discord.com/api/v10/channels/{notification_channel_id}/messages"
                timeout = aiohttp.ClientTimeout(total=30)

                async with aiohttp.ClientSession() as session:
                    try:
                        async with session.post(
                            channel_url,
                            headers=headers,
                            json={"content": message_content, "components": components},
                            timeout=timeout,
                        ) as resp:
                            if resp.status in (200, 201):
                                logger.info(
                                    f"[Webhook] ✅ Stream notification sent to channel {notification_channel_id}"
                                )
                            else:
                                error_text = await resp.text()
                                logger.info(f"[Webhook] ❌ Discord API error {resp.status}: {error_text[:200]}")
                    except asyncio.TimeoutError:
                        # Discord likely accepted the POST but the response
                        # read timed out — proceed to the footer anyway so
                        # we don't drop it on the floor when the embed is
                        # actually visible in the channel.
                        logger.info(
                            f"[Webhook] ⚠️ Timed out reading Discord response for main embed (message may have posted)"
                        )

                    if footer_text:
                        await asyncio.sleep(0.5)
                        try:
                            async with session.post(
                                channel_url,
                                headers=headers,
                                json={"content": f"-# {footer_text}"},
                                timeout=timeout,
                            ) as footer_resp:
                                if footer_resp.status in (200, 201):
                                    logger.info(f"[Webhook] ✅ Footer sent")
                                else:
                                    error_text = await footer_resp.text()
                                    logger.info(
                                        f"[Webhook] ⚠️ Failed to send footer: {footer_resp.status} {error_text[:200]}"
                                    )
                        except asyncio.TimeoutError:
                            logger.info(
                                f"[Webhook] ⚠️ Timed out reading Discord response for footer (message may have posted)"
                            )

        except Exception as e:
            logger.info(f"[Webhook] ❌ Error handling livestream status: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()

    @webhook_handler.set_default_handler
    async def default_webhook_handler(event_data):
        """Default handler logs all events for debugging new webhook types"""
        import json

        event_type = event_data.get("_event_type", "unknown")
        logger.info(f"[Webhook] 📩 Unhandled event type: {event_type}")
        logger.info(f"[Webhook] Event data: {json.dumps(event_data, indent=2, default=str)[:500]}")

    # Register webhook routes with Flask app
    register_webhook_routes(app, webhook_handler)
    logger.info("[OAuth] ✅ Webhook routes registered at /webhooks/kick")
else:
    logger.info("[OAuth] ⚠️ Kick webhook support not available")

# Register Twitch EventSub webhook routes if available
if HAS_TWITCH and register_twitch_webhook_routes:
    twitch_webhook_handler = create_twitch_event_handler()
    register_twitch_webhook_routes(app, twitch_webhook_handler)
    logger.info("[OAuth] ✅ Twitch EventSub routes registered at /webhooks/twitch")
else:
    logger.info("[OAuth] ⚠️ Twitch webhook support not available")


# 404 handler - ignore not found errors (bots/scanners)
@app.errorhandler(404)
def handle_404(e):
    # Silently log but don't spam with full traceback
    logger.info(f"ℹ️ 404: {request.method} {request.path}")
    return jsonify({"error": "Not Found"}), 404


# 405 handler - method not allowed (wrong HTTP method)
@app.errorhandler(405)
def handle_405(e):
    # Log method not allowed errors without full traceback
    logger.info(f"ℹ️ 405 Method Not Allowed: {request.method} {request.path}")
    logger.info(f"   Allowed methods: {e.valid_methods}")
    return (
        jsonify(
            {
                "error": "Method Not Allowed",
                "message": f"The method {request.method} is not allowed for this endpoint",
                "allowed_methods": list(e.valid_methods),
            }
        ),
        405,
    )


# Global error handler
@app.errorhandler(Exception)
def handle_error(e):
    logger.info(f"🚨 Unhandled error: {e}")
    import traceback

    traceback.print_exc()
    return jsonify({"error": str(e), "type": type(e).__name__}), 500


from pathlib import Path

# -------------------------
# 🎬 Clip Serving Endpoint
# -------------------------
from flask import make_response, send_from_directory

# Use absolute path for clips directory (relative to project root)
PROJECT_ROOT = Path(__file__).parent.parent  # Go up from core/ to project root
CLIPS_DIR = PROJECT_ROOT / "clips"
CLIPS_DIR.mkdir(exist_ok=True)
logger.info(f"[OAuth] Clips directory: {CLIPS_DIR.absolute()}")


def add_cors_headers(response):
    """Add CORS headers to allow dashboard/overlay access to public clip reads.

    Only GET/OPTIONS are advertised: clip GETs are public (OBS overlays load the
    mp4 cross-origin), but the destructive DELETE is server-to-server only and is
    deliberately NOT offered to browser origins.
    """
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


def _clip_admin_authorized() -> bool:
    """Timing-safe check of the shared bot-admin secret for mutating clip calls.

    Fails closed: if BOT_AUTH_TOKEN is not configured the endpoint is disabled
    rather than left open. Accepts the token via the Authorization: Bearer header
    or an X-Bot-Auth header (never the query string, which lands in access logs).
    """
    expected = os.getenv("BOT_AUTH_TOKEN")
    if not expected:
        logger.warning("BOT_AUTH_TOKEN not configured - clip delete disabled")
        return False
    provided = request.headers.get("X-Bot-Auth", "")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[len("Bearer ") :]
    return bool(provided) and hmac.compare_digest(provided, expected)


@app.route("/clips/<filename>", methods=["GET", "OPTIONS"])
def serve_clip(filename):
    """Serve clip files from the clips directory"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = make_response()
        return add_cors_headers(response)

    # Security: only allow .mp4 files and prevent path traversal
    if not filename.endswith(".mp4") or ".." in filename or "/" in filename:
        return add_cors_headers(jsonify({"error": "Invalid filename"})), 400

    filepath = CLIPS_DIR / filename
    if not filepath.exists():
        return add_cors_headers(jsonify({"error": "Clip not found"})), 404

    response = send_from_directory(CLIPS_DIR, filename, mimetype="video/mp4")
    return add_cors_headers(response)


@app.route("/clips", methods=["GET", "OPTIONS"])
def list_clips():
    """List available clips"""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = make_response()
        return add_cors_headers(response)

    clips = []
    for filepath in sorted(CLIPS_DIR.glob("clip_*.mp4"), reverse=True)[:50]:
        stat = filepath.stat()
        clips.append(
            {
                "filename": filepath.name,
                "url": f"/clips/{filepath.name}",
                "size_mb": round(stat.st_size / 1024 / 1024, 2),
                "created": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return add_cors_headers(jsonify(clips))


@app.route("/clips/<filename>", methods=["DELETE", "OPTIONS"])
def delete_clip(filename):
    """Delete a clip file (requires the shared bot-admin secret)."""
    # Handle CORS preflight
    if request.method == "OPTIONS":
        response = make_response()
        return add_cors_headers(response)

    # Auth: destructive endpoint - require the shared bot-admin secret (fail closed).
    if not _clip_admin_authorized():
        return add_cors_headers(jsonify({"error": "Unauthorized"})), 403

    # Security: only allow .mp4 files and prevent path traversal
    if not filename.endswith(".mp4") or ".." in filename or "/" in filename:
        return add_cors_headers(jsonify({"error": "Invalid filename"})), 400

    filepath = CLIPS_DIR / filename
    if not filepath.exists():
        return add_cors_headers(jsonify({"error": "Clip not found"})), 404

    try:
        filepath.unlink()
        return add_cors_headers(jsonify({"success": True, "message": f"Deleted {filename}"}))
    except Exception as e:
        return add_cors_headers(jsonify({"error": str(e)})), 500


# Kick OAuth Configuration
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID")
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET")
OAUTH_BASE_URL = os.getenv("OAUTH_BASE_URL", "")  # e.g., https://your-app.up.railway.app
# Ensure OAUTH_BASE_URL has https:// scheme
if OAUTH_BASE_URL and not OAUTH_BASE_URL.startswith(("http://", "https://")):
    OAUTH_BASE_URL = f"https://{OAUTH_BASE_URL}"

if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
    logger.warning("⚠️ WARNING: KICK_CLIENT_ID and KICK_CLIENT_SECRET not set!")
    logger.warning("⚠️ OAuth linking will not work until these are configured.")

# Kick OAuth endpoints - correct URLs confirmed from lele.gg
KICK_AUTHORIZE_URL = "https://id.kick.com/oauth/authorize"
KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_USER_API_URL = "https://kick.com/api/v2/user"
KICK_OAUTH_USER_INFO_URL = "https://id.kick.com/oauth/userinfo"

# Database configuration (reuse from bot.py)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///watchtime.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Initialize database tables
with engine.begin() as conn:
    # Create oauth_states table for storing OAuth state across workers
    conn.execute(
        text(
            """
        CREATE TABLE IF NOT EXISTS oauth_states (
            state TEXT PRIMARY KEY,
            discord_id BIGINT NOT NULL,
            code_verifier TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            guild_id BIGINT DEFAULT 0
        )
    """
        )
    )

    # Add guild_id column if it doesn't exist (migration)
    try:
        conn.execute(
            text(
                """
            ALTER TABLE oauth_states ADD COLUMN IF NOT EXISTS guild_id BIGINT DEFAULT 0
        """
            )
        )
    except Exception:
        pass  # Column already exists or database doesn't support IF NOT EXISTS

    # kick_oauth_tokens table is created by the dashboard - no need to create bot_tokens here

# Note: OAuth states are stored in database, not in-memory
# This is necessary because Gunicorn workers don't share memory


def generate_pkce_pair():
    """Generate PKCE code_verifier and code_challenge."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")
    )
    return code_verifier, code_challenge


@app.route("/")
def index():
    """Redirect the callback-service root to the branded React surface."""
    return redirect_service_page()


@app.route("/health")
def health():
    """Health check endpoint for Railway."""
    return jsonify({"status": "healthy", "oauth_configured": bool(KICK_CLIENT_ID and KICK_CLIENT_SECRET)}), 200


@app.route("/api/status")
def api_status():
    """Minimal public health check.

    Deliberately does NOT expose which secrets are configured, the available
    OAuth scopes/webhook events, or the endpoint map - that is reconnaissance
    detail an unauthenticated caller does not need.
    """
    return jsonify({"status": "healthy"}), 200


@app.route("/api/webhooks", methods=["GET"])
def list_webhook_subscriptions():
    """
    List all active webhook subscriptions.
    Requires bot access token.
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    access_token = auth_header.replace("Bearer ", "")

    if not HAS_KICK_OFFICIAL:
        return jsonify({"error": "Official Kick API not available"}), 503

    try:
        import asyncio

        async def get_subscriptions():
            api = KickOfficialAPI(
                client_id=KICK_CLIENT_ID,
                client_secret=KICK_CLIENT_SECRET,
                access_token=access_token,
            )
            try:
                subs = await api.get_webhook_subscriptions()
                return [s.__dict__ if hasattr(s, "__dict__") else s for s in subs]
            finally:
                await api.close()

        loop = asyncio.new_event_loop()
        try:
            subscriptions = loop.run_until_complete(get_subscriptions())
        finally:
            loop.close()

        return jsonify({"subscriptions": subscriptions}), 200

    except Exception as e:
        logger.error(f"[API] Error listing webhooks: {e}")
        return jsonify({"error": str(e)}), 500


def _callback_url_allowed(url: str) -> bool:
    """Only permit registering a webhook callback that points back at THIS bot's own
    public endpoint. Otherwise a caller with any accepted Kick token could redirect
    the app's webhook subscriptions to an arbitrary host (webhook hijacking). Fails
    closed if no public URL is configured."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    allowed_hosts = set()
    for base in (os.getenv("BOT_PUBLIC_URL", ""), os.getenv("OAUTH_BASE_URL", "")):
        if base:
            host = urlparse(base if "://" in base else f"https://{base}").netloc
            if host:
                allowed_hosts.add(host.lower())
    return parsed.netloc.lower() in allowed_hosts


@app.route("/api/webhooks", methods=["POST"])
def create_webhook_subscription():
    """
    Create a new webhook subscription.

    Requires:
    - Authorization: Bearer <access_token>
    - JSON body: { "event": "channel.subscription.gifts", "callback_url": "https://..." }
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    access_token = auth_header.replace("Bearer ", "")

    if not HAS_KICK_OFFICIAL:
        return jsonify({"error": "Official Kick API not available"}), 503

    data = request.get_json()
    if not data:
        return jsonify({"error": "JSON body required"}), 400

    event = data.get("event")
    callback_url = data.get("callback_url")
    broadcaster_user_id = data.get("broadcaster_user_id")

    if not event or not callback_url:
        return jsonify({"error": "event and callback_url are required"}), 400

    if not _callback_url_allowed(callback_url):
        logger.warning(f"[API] Rejected webhook callback_url (not this bot's host): {callback_url}")
        return jsonify({"error": "callback_url must point to this bot's public URL"}), 400

    try:
        import asyncio

        async def subscribe():
            api = KickOfficialAPI(
                client_id=KICK_CLIENT_ID,
                client_secret=KICK_CLIENT_SECRET,
                access_token=access_token,
            )
            try:
                sub = await api.subscribe_webhook(
                    event=event,
                    callback_url=callback_url,
                    broadcaster_user_id=broadcaster_user_id,
                )
                return sub.__dict__ if hasattr(sub, "__dict__") else sub
            finally:
                await api.close()

        loop = asyncio.new_event_loop()
        try:
            subscription = loop.run_until_complete(subscribe())
        finally:
            loop.close()

        return jsonify({"subscription": subscription}), 201

    except Exception as e:
        logger.error(f"[API] Error creating webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/webhooks/<subscription_id>", methods=["DELETE"])
def delete_webhook_subscription(subscription_id):
    """
    Delete a webhook subscription.
    Requires Authorization: Bearer <access_token>
    """
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        return jsonify({"error": "Missing or invalid Authorization header"}), 401

    access_token = auth_header.replace("Bearer ", "")

    if not HAS_KICK_OFFICIAL:
        return jsonify({"error": "Official Kick API not available"}), 503

    try:
        import asyncio

        async def delete():
            api = KickOfficialAPI(
                client_id=KICK_CLIENT_ID,
                client_secret=KICK_CLIENT_SECRET,
                access_token=access_token,
            )
            try:
                return await api.delete_webhook_subscription(subscription_id)
            finally:
                await api.close()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(delete())
        finally:
            loop.close()

        return jsonify({"status": "deleted", "id": subscription_id}), 200

    except Exception as e:
        logger.error(f"[API] Error deleting webhook: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/terms")
@app.route("/terms-of-service")
def terms_of_service():
    """Serve Terms of Service page."""
    try:
        with open("TERMS_OF_SERVICE.md", "r", encoding="utf-8") as f:
            content = f.read()

        # Simple markdown to HTML conversion
        lines = content.split("\n")
        html_parts = []

        for line in lines:
            if line.startswith("# "):
                html_parts.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("## "):
                html_parts.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("### "):
                html_parts.append(f"<h3>{line[4:]}</h3>")
            elif line.strip():
                # Bold text
                line = line.replace("**", "<strong>", 1)
                line = line.replace("**", "</strong>", 1)
                html_parts.append(f"<p>{line}</p>")
            else:
                html_parts.append("<br>")

        html_content = "\n".join(html_parts)

        return f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Terms of Service - Kick Watchtime Bot</title>
            <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                :root {{
                    --background: #1a2332;
                    --foreground: #f9fafb;
                    --card: #242d3d;
                    --card-foreground: #f9fafb;
                    --primary: #3b82f6;
                    --primary-foreground: #f9fafb;
                    --muted: #374151;
                    --muted-foreground: #9ca3af;
                    --border: #374151;
                    --radius: 0.625rem;
                }}
                body {{
                    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    line-height: 1.6;
                    color: var(--foreground);
                    background: var(--background);
                    min-height: 100vh;
                    padding: 20px;
                    -webkit-font-smoothing: antialiased;
                    -moz-osx-font-smoothing: grayscale;
                }}
                .container {{
                    max-width: 900px;
                    margin: 0 auto;
                    background: var(--card);
                    padding: 48px;
                    border-radius: var(--radius);
                    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
                    border: 1px solid var(--border);
                }}
                h1 {{
                    color: var(--foreground);
                    font-size: 2.5em;
                    font-weight: 700;
                    margin-bottom: 24px;
                    padding-bottom: 16px;
                    border-bottom: 2px solid var(--primary);
                    letter-spacing: -0.025em;
                }}
                h2 {{
                    color: var(--foreground);
                    font-size: 1.8em;
                    font-weight: 600;
                    margin-top: 36px;
                    margin-bottom: 16px;
                    letter-spacing: -0.015em;
                }}
                h3 {{
                    color: var(--card-foreground);
                    font-size: 1.4em;
                    font-weight: 600;
                    margin-top: 24px;
                    margin-bottom: 12px;
                }}
                p {{
                    margin-bottom: 16px;
                    color: var(--muted-foreground);
                    font-size: 1em;
                }}
                strong {{
                    color: var(--foreground);
                    font-weight: 600;
                }}
                .footer {{
                    margin-top: 48px;
                    padding-top: 24px;
                    border-top: 1px solid var(--border);
                    text-align: center;
                }}
                .footer a {{
                    color: var(--primary);
                    text-decoration: none;
                    margin: 0 16px;
                    font-weight: 500;
                    transition: all 0.2s ease;
                }}
                .footer a:hover {{
                    color: #60a5fa;
                    text-decoration: underline;
                }}
                .footer p {{
                    color: var(--muted-foreground);
                }}
                @media (max-width: 768px) {{
                    .container {{
                        padding: 32px 24px;
                    }}
                    h1 {{
                        font-size: 2em;
                    }}
                    h2 {{
                        font-size: 1.5em;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                {html_content}
                <div class="footer">
                    <p>
                        <a href="/privacy">Privacy Policy</a> |
                        <a href="/terms">Terms of Service</a> |
                        <a href="https://github.com/madcats1337/kick-watchtime-bot">GitHub</a>
                    </p>
                    <p style="font-size: 0.9em; margin-top: 12px;">
                        Kick Watchtime Bot &copy; 2025
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
    except FileNotFoundError:
        return "<h1>Terms of Service</h1><p>Document not found.</p>", 404
    except Exception as e:
        return f"<h1>Error</h1><p>Error loading Terms of Service: {{str(e)}}</p>", 500


@app.route("/privacy")
@app.route("/privacy-policy")
def privacy_policy():
    """Serve Privacy Policy page."""
    try:
        with open("PRIVACY_POLICY.md", "r", encoding="utf-8") as f:
            content = f.read()

        # Simple markdown to HTML conversion
        lines = content.split("\n")
        html_parts = []

        for line in lines:
            if line.startswith("# "):
                html_parts.append(f"<h1>{line[2:]}</h1>")
            elif line.startswith("## "):
                html_parts.append(f"<h2>{line[3:]}</h2>")
            elif line.startswith("### "):
                html_parts.append(f"<h3>{line[4:]}</h3>")
            elif line.strip():
                # Bold text
                line = line.replace("**", "<strong>", 1)
                line = line.replace("**", "</strong>", 1)
                html_parts.append(f"<p>{line}</p>")
            else:
                html_parts.append("<br>")

        html_content = "\n".join(html_parts)

        return f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Privacy Policy - Kick Watchtime Bot</title>
            <link rel="preconnect" href="https://fonts.googleapis.com">
            <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
            <style>
                * {{ margin: 0; padding: 0; box-sizing: border-box; }}
                :root {{
                    --background: #1a2332;
                    --foreground: #f9fafb;
                    --card: #242d3d;
                    --card-foreground: #f9fafb;
                    --primary: #3b82f6;
                    --primary-foreground: #f9fafb;
                    --muted: #374151;
                    --muted-foreground: #9ca3af;
                    --border: #374151;
                    --radius: 0.625rem;
                }}
                body {{
                    font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    line-height: 1.6;
                    color: var(--foreground);
                    background: var(--background);
                    min-height: 100vh;
                    padding: 20px;
                    -webkit-font-smoothing: antialiased;
                    -moz-osx-font-smoothing: grayscale;
                }}
                .container {{
                    max-width: 900px;
                    margin: 0 auto;
                    background: var(--card);
                    padding: 48px;
                    border-radius: var(--radius);
                    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
                    border: 1px solid var(--border);
                }}
                h1 {{
                    color: var(--foreground);
                    font-size: 2.5em;
                    font-weight: 700;
                    margin-bottom: 24px;
                    padding-bottom: 16px;
                    border-bottom: 2px solid var(--primary);
                    letter-spacing: -0.025em;
                }}
                h2 {{
                    color: var(--foreground);
                    font-size: 1.8em;
                    font-weight: 600;
                    margin-top: 36px;
                    margin-bottom: 16px;
                    letter-spacing: -0.015em;
                }}
                h3 {{
                    color: var(--card-foreground);
                    font-size: 1.4em;
                    font-weight: 600;
                    margin-top: 24px;
                    margin-bottom: 12px;
                }}
                p {{
                    margin-bottom: 16px;
                    color: var(--muted-foreground);
                    font-size: 1em;
                }}
                strong {{
                    color: var(--foreground);
                    font-weight: 600;
                }}
                .footer {{
                    margin-top: 48px;
                    padding-top: 24px;
                    border-top: 1px solid var(--border);
                    text-align: center;
                }}
                .footer a {{
                    color: var(--primary);
                    text-decoration: none;
                    margin: 0 16px;
                    font-weight: 500;
                    transition: all 0.2s ease;
                }}
                .footer a:hover {{
                    color: #60a5fa;
                    text-decoration: underline;
                }}
                .footer p {{
                    color: var(--muted-foreground);
                }}
                @media (max-width: 768px) {{
                    .container {{
                        padding: 32px 24px;
                    }}
                    h1 {{
                        font-size: 2em;
                    }}
                    h2 {{
                        font-size: 1.5em;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
                {html_content}
                <div class="footer">
                    <p>
                        <a href="/privacy">Privacy Policy</a> |
                        <a href="/terms">Terms of Service</a> |
                        <a href="https://github.com/madcats1337/kick-watchtime-bot">GitHub</a>
                    </p>
                    <p style="font-size: 0.9em; margin-top: 12px;">
                        Kick Watchtime Bot &copy; 2025
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
    except FileNotFoundError:
        return "<h1>Privacy Policy</h1><p>Document not found.</p>", 404
    except Exception as e:
        return f"<h1>Error</h1><p>Error loading Privacy Policy: {{str(e)}}</p>", 500


@app.route("/auth/kick")
def auth_kick():
    """
    Initiate Kick OAuth flow.
    Query params: discord_id (required), guild_id (optional), timestamp (required), signature (required)

    🔒 Security: Requires cryptographic signature to prevent OAuth initiation spoofing.
    Only URLs generated by the Discord bot will have valid signatures.
    """
    discord_id = request.args.get("discord_id")
    guild_id = request.args.get("guild_id", "0")  # Default to 0 if not provided (for backwards compatibility)
    timestamp_str = request.args.get("timestamp")
    signature = request.args.get("signature")

    if not discord_id or not timestamp_str or not signature:
        return "❌ Missing required parameters (discord_id, timestamp, signature)", 400

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return "❌ Invalid timestamp format", 400

    # 🔒 SECURITY: Verify signature to ensure request came from Discord bot
    if not verify_discord_id_signature(discord_id, timestamp, signature, guild_id):
        logger.info(f"🚨 SECURITY: Invalid OAuth signature for Discord ID {discord_id}, Guild ID {guild_id}")
        return (
            "❌ Invalid or expired authentication token. Please use the !link command in Discord to generate a new link.",
            403,
        )

    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        return "❌ OAuth not configured. Please set KICK_CLIENT_ID and KICK_CLIENT_SECRET.", 500

    logger.info(f"✅ Valid OAuth signature for Discord ID: {discord_id}, Guild ID: {guild_id}")

    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce_pair()

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    logger.info(f"🔑 Generated new state for Discord ID: {discord_id}, Guild ID: {guild_id}")

    # Store state in database (survives across Gunicorn workers)
    with engine.begin() as conn:
        # Clean up old states (older than 30 minutes)
        deleted_count = conn.execute(
            text(
                """
            DELETE FROM oauth_states
            WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '30 minutes'
        """
            )
        ).rowcount
        logger.info(f"🧹 Cleaned up {deleted_count} expired state(s)")

        # Store new state with guild_id
        conn.execute(
            text(
                """
            INSERT INTO oauth_states (state, discord_id, code_verifier, created_at, guild_id)
            VALUES (:state, :discord_id, :code_verifier, CURRENT_TIMESTAMP, :guild_id)
        """
            ),
            {"state": state, "discord_id": int(discord_id), "code_verifier": code_verifier, "guild_id": int(guild_id)},
        )
        logger.info(f"✅ State saved to database with guild_id: {guild_id}")

    # Build authorization URL with required scopes and PKCE
    redirect_uri = f"{OAUTH_BASE_URL}/auth/kick/callback"

    # Request minimal scopes for user account linking
    # (User linking only needs to read their profile and channel info)
    # Bot functionality (chat:write, events:subscribe) is handled separately via streamer dashboard
    scopes = [
        "user:read",  # Read user profile (required for linking)
        "channel:read",  # Read channel information (required for linking)
    ]

    auth_params = {
        "client_id": KICK_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": " ".join(scopes),  # Space-separated scopes
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    logger.info(f"🔗 Authorization URL: {KICK_AUTHORIZE_URL}?{urlencode(auth_params)}")

    auth_url = f"{KICK_AUTHORIZE_URL}?{urlencode(auth_params)}"

    return redirect(auth_url)


@app.route("/auth/kick/callback")
def auth_kick_callback():
    """Handle OAuth callback from Kick (supports both user linking and bot authorization)."""
    logger.info(f"🔔 Callback received!")

    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    logger.info(
        f"📥 Code: {sanitize_for_logs(code, 'code')}, State: {sanitize_for_logs(state, 'state')}, Error: {error}"
    )

    if error:
        logger.error(f"❌ Kick returned error: {error}")
        return redirect_oauth_error("Kick authorization was cancelled or denied. Please start again when ready.")

    if not code or not state:
        logger.error(f"❌ Missing code or state")
        return redirect_oauth_error("The Kick authorization response was incomplete. Please start again.")

    # Verify state from database
    logger.info(f"🔍 Checking state in database...")
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
            SELECT discord_id, code_verifier, created_at, guild_id FROM oauth_states WHERE state = :state
        """
            ),
            {"state": state},
        ).fetchone()

    if not result:
        logger.error(f"❌ State not found or expired")

        # Debug: Check if state exists at all and show recent states
        failed_notification_written = False
        with engine.begin() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM oauth_states")).fetchone()[0]
            recent_states = conn.execute(
                text(
                    """
                SELECT state, discord_id, created_at, guild_id
                FROM oauth_states
                ORDER BY created_at DESC
                LIMIT 5
            """
                )
            ).fetchall()
            logger.info(f"📊 Total states in database: {count}")
            logger.info(f"📋 Recent states:")
            for s in recent_states:
                # 🔒 OPSEC: Sanitize state tokens in debug output
                logger.info(
                    f"   - State: {sanitize_for_logs(s[0], 'state')}, Discord ID: {s[1]}, Created: {s[2]}, Guild ID: {s[3] if len(s) > 3 else 0}"
                )
        return redirect_oauth_error("This Kick connection link is invalid or expired. Please start again.")

    discord_id = result[0]
    code_verifier = result[1]
    created_at = result[2]
    guild_id = result[3] if len(result) > 3 else 0  # Default to 0 for backwards compatibility

    logger.info(f"✅ State valid, Discord ID: {discord_id}, Guild ID: {guild_id}, Created: {created_at}")

    # Check if this is a bot authorization (discord_id == 0) or regular user linking
    if discord_id == 0:
        logger.info(f"🤖 Detected bot authorization flow")
        return handle_bot_authorization_callback(code, code_verifier, state, guild_id)
    else:
        logger.info(f"👤 Detected user linking flow")
        return handle_user_linking_callback(code, code_verifier, state, discord_id, created_at, guild_id)


def handle_bot_authorization_callback(code, code_verifier, state, guild_id=0):
    """Handle bot authorization callback.

    `guild_id` scopes the canonical `kick_oauth_tokens` row. It used to be
    omitted entirely, so the row fell to the column default (0) and the upsert
    named `ON CONFLICT (user_id)` — which does not match the live primary key
    `(user_id, discord_server_id)` and therefore raised at runtime instead of
    updating. Every canonical lookup is scoped by discord_server_id, so the
    write must be too.
    """
    logger.info(f"🤖 [BOT AUTH] Starting bot authorization callback handler")
    logger.info(f"🤖 [BOT AUTH] Code: {sanitize_for_logs(code, 'code')}")
    logger.info(f"🤖 [BOT AUTH] State: {sanitize_for_logs(state, 'state')}")

    try:
        # Ensure bot_tokens table exists with expires_at column
        # kick_oauth_tokens table is created by the dashboard
        logger.info(f"🤖 [BOT AUTH] Using kick_oauth_tokens table...")

        # Exchange code for access token
        logger.info(f"🤖 [BOT AUTH] Exchanging code for token...")
        token_data = exchange_code_for_token(code, code_verifier=code_verifier)
        logger.info(f"🤖 [BOT AUTH] Token exchange complete. Keys: {list(token_data.keys())}")

        access_token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 3600)  # Default to 1 hour if not provided

        if not access_token:
            logger.error(f"❌ [BOT AUTH] No access token in response: {list(token_data.keys())}")
            return redirect_oauth_error("Kick did not complete the bot authorization. Please try again.", flow="bot")

        logger.info(f"✅ [BOT AUTH] Got bot access token (expires in {expires_in} seconds)")
        logger.info(f"✅ [BOT AUTH] Token preview: {sanitize_for_logs(access_token, 'token')}")

        # Get bot user info. Fall back to a neutral placeholder (not a specific
        # bot's name) if Kick doesn't return a username — this is only stored as
        # a display label and is overwritten next time the API call succeeds.
        try:
            kick_user = get_kick_user_info(access_token)
            kick_username = kick_user.get("username") or "bot"
            kick_user_id = kick_user.get("user_id", 0)
            logger.info(f"🤖 Bot username: {kick_username}, user_id: {kick_user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Could not get bot user info: {e}, using default placeholder")
            kick_username = "bot"
            kick_user_id = 0

        # Calculate expiration time
        from datetime import datetime, timedelta

        expires_at = datetime.utcnow() + timedelta(seconds=expires_in)

        # Store token in kick_oauth_tokens table (same table as dashboard)

        logger.info(f"🤖 [BOT AUTH] Storing token in kick_oauth_tokens table...")
        logger.info(f"🤖 [BOT AUTH] Username: {kick_username}, user_id: {kick_user_id}")
        logger.info(f"🤖 [BOT AUTH] Expires at: {expires_at}")

        with engine.begin() as conn:
            # Use UPSERT to kick_oauth_tokens table
            logger.info(f"🤖 [BOT AUTH] Upserting token for {kick_username}...")
            conn.execute(
                text(
                    """
                INSERT INTO kick_oauth_tokens (
                    user_id, discord_server_id, kick_username,
                    access_token, refresh_token, scopes, expires_at
                )
                VALUES (:user_id, :guild_id, :username, :access_token, :refresh_token, :scopes, :expires_at)
                ON CONFLICT (user_id, discord_server_id) DO UPDATE SET
                    access_token = EXCLUDED.access_token,
                    refresh_token = EXCLUDED.refresh_token,
                    scopes = EXCLUDED.scopes,
                    expires_at = EXCLUDED.expires_at,
                    needs_reauth = FALSE,
                    updated_at = CURRENT_TIMESTAMP
            """
                ),
                {
                    "user_id": kick_user_id,
                    "guild_id": int(guild_id or 0),
                    "username": kick_username,
                    "access_token": encrypt_secret(access_token),
                    "refresh_token": encrypt_secret(token_data.get("refresh_token", "")),
                    "scopes": token_data.get("scope", ""),
                    "expires_at": expires_at,
                },
            )

            # Clean up state
            logger.info(f"🤖 [BOT AUTH] Cleaning up state...")
            conn.execute(text("DELETE FROM oauth_states WHERE state = :state"), {"state": state})

            logger.info(f"✅ [BOT AUTH] Bot token stored successfully in kick_oauth_tokens!")

            logger.info(f"✅ Bot token stored securely")

            # 🎯 AUTO-SETUP WEBHOOKS after successful bot authorization
            logger.info(f"🔧 [WEBHOOK AUTO-SETUP] Starting automatic webhook setup for {kick_username}...")
            try:
                import asyncio

                from setup_webhooks import setup_webhooks_for_server

                # Get the Discord server ID for this bot auth (from guild_id in state or bot_settings)
                # For now, try to find it from bot_settings with matching kick_channel
                server_result = conn.execute(
                    text(
                        """
                    SELECT discord_server_id FROM bot_settings
                    WHERE key = 'kick_channel' AND value = :username
                    LIMIT 1
                """
                    ),
                    {"username": kick_username},
                ).fetchone()

                if server_result:
                    discord_server_id = server_result[0]
                    logger.info(f"🔧 [WEBHOOK AUTO-SETUP] Found server ID: {discord_server_id}")

                    # Run webhook setup asynchronously
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    webhook_success = loop.run_until_complete(setup_webhooks_for_server(discord_server_id))
                    loop.close()

                    if webhook_success:
                        logger.info(f"✅ [WEBHOOK AUTO-SETUP] Webhooks configured successfully!")
                    else:
                        logger.warning(f"⚠️  [WEBHOOK AUTO-SETUP] Webhook setup had issues, check logs")
                else:
                    logger.warning(f"⚠️  [WEBHOOK AUTO-SETUP] Could not find Discord server ID for {kick_username}")

            except Exception as webhook_err:
                logger.warning(f"⚠️  [WEBHOOK AUTO-SETUP] Failed to auto-setup webhooks: {webhook_err}")
                import traceback

                traceback.print_exc()
                # Don't fail the OAuth flow if webhook setup fails

        return redirect_oauth_success(kick_username, flow="bot")

    except Exception as e:
        logger.error(f"❌ Bot authorization error: {e}")
        import traceback

        traceback.print_exc()
        return redirect_oauth_error(
            "Kick bot authorization could not be completed. Please try again.",
            flow="bot",
        )


def handle_user_linking_callback(code, code_verifier, state, discord_id, created_at, guild_id=0):
    """Handle regular user linking callback."""

    # Exchange code for access token
    try:
        token_data = exchange_code_for_token(code, code_verifier=code_verifier)
        access_token = token_data.get("access_token")

        if not access_token:
            return redirect_oauth_error("Kick did not complete the account connection. Please try again.")

        logger.info(f"✅ Got access token")

        # Get user info from Kick's public API
        try:
            kick_user = get_kick_user_info(access_token)
            # 🔒 OPSEC: Sanitize user info before logging
            logger.info(f"📊 Got user info: {sanitize_for_logs(kick_user)}")
        except Exception as e:
            logger.error(f"❌ Failed to get user info: {e}")
            return redirect_oauth_error("Wagerlabs could not read your Kick username. Please try again.")

        if not kick_user or not kick_user.get("username"):
            return redirect_oauth_error("Wagerlabs could not read your Kick username. Please try again.")

        kick_username = kick_user["username"]
        logger.info(f"👤 Kick username: {kick_username}")

        # Check if Kick account is already linked to another Discord user on this server
        with engine.connect() as conn:
            existing = conn.execute(
                text("SELECT discord_id FROM links WHERE kick_name = :k AND discord_server_id = :gid"),
                {"k": kick_username.lower(), "gid": guild_id},
            ).fetchone()

            if existing and existing[0] != discord_id:
                # Store failed attempt for logging
                conn.execute(
                    text(
                        """
                    INSERT INTO oauth_notifications (discord_id, kick_username, processed)
                    VALUES (:d, :k, FALSE)
                """
                    ),
                    {"d": discord_id, "k": f"FAILED:{kick_username}:already_linked"},
                )
                failed_notification_written = True

        if failed_notification_written:
            notify_oauth_link_written()
            return redirect_oauth_error(
                f"Kick account '{kick_username}' is already linked to another Discord user on this server."
            )

        # Link accounts in database
        with engine.begin() as conn:
            # First, try the proper multiserver upsert
            try:
                conn.execute(
                    text(
                        """
                    INSERT INTO links (discord_id, kick_name, discord_server_id, platform)
                    VALUES (:d, :k, :gid, 'kick')
                    ON CONFLICT(discord_id, discord_server_id, platform) DO UPDATE
                    SET kick_name = excluded.kick_name, linked_at = CURRENT_TIMESTAMP
                """
                    ),
                    {"d": discord_id, "k": kick_username.lower(), "gid": guild_id},
                )
            except Exception as insert_error:
                # If composite key doesn't exist yet (old schema), try alternative approach
                error_str = str(insert_error).lower()
                if "unique constraint" in error_str or "duplicate key" in error_str:
                    logger.warning(f"⚠️ Falling back to DELETE+INSERT due to old schema: {insert_error}")
                    # Delete existing Kick link for this user on this server
                    conn.execute(
                        text(
                            """
                        DELETE FROM links WHERE discord_id = :d AND discord_server_id = :gid AND platform = 'kick'
                    """
                        ),
                        {"d": discord_id, "gid": guild_id},
                    )
                    # Also try deleting by discord_id only if server_id is 0 (legacy records)
                    if guild_id != 0:
                        conn.execute(
                            text(
                                """
                            DELETE FROM links WHERE discord_id = :d AND (discord_server_id IS NULL OR discord_server_id = 0) AND platform = 'kick'
                        """
                            ),
                            {"d": discord_id},
                        )
                    # Now insert fresh
                    conn.execute(
                        text(
                            """
                        INSERT INTO links (discord_id, kick_name, discord_server_id, platform, linked_at)
                        VALUES (:d, :k, :gid, 'kick', CURRENT_TIMESTAMP)
                    """
                        ),
                        {"d": discord_id, "k": kick_username.lower(), "gid": guild_id},
                    )
                else:
                    raise

            # Clean up pending bio verifications if any
            conn.execute(text("DELETE FROM pending_links WHERE discord_id = :d"), {"d": discord_id})

            # Clean up used OAuth state
            conn.execute(text("DELETE FROM oauth_states WHERE state = :state"), {"state": state})

            # Update existing notification with kick_username (was created when !link command was used)
            # If no existing notification exists, create a new one
            result = conn.execute(
                text(
                    """
                UPDATE oauth_notifications
                SET kick_username = :k, processed = FALSE, discord_server_id = :g
                WHERE discord_id = :d AND kick_username = '' AND (discord_server_id = :g OR discord_server_id IS NULL)
                RETURNING id
            """
                ),
                {"d": discord_id, "k": kick_username, "g": guild_id},
            ).fetchone()

            # If no pending notification found, create new one (with guild_id for proper multi-server support)
            if not result:
                conn.execute(
                    text(
                        """
                    INSERT INTO oauth_notifications (discord_id, kick_username, discord_server_id)
                    VALUES (:d, :k, :g)
                """
                    ),
                    {"d": discord_id, "k": kick_username, "g": guild_id},
                )

        notify_oauth_link_written()
        logger.info(f"✅ OAuth link successful: Discord {discord_id} -> Kick {kick_username}")
        return redirect_oauth_success(kick_username)

    except Exception as e:
        logger.error(f"❌ [OAuth] Error during callback: {e}")
        import traceback

        traceback.print_exc()
        return redirect_oauth_error("The Kick account connection could not be completed. Please try again.")


# =====================================================
# TWITCH VIEWER ACCOUNT LINKING (link panel button flow)
# Mirrors /auth/kick + /auth/kick/callback. Writes a links row with
# platform='twitch' and notifies the bot (oauth_notifications) to grant the
# linked role — reusing the same machinery as Kick.
# =====================================================

TWITCH_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_USERS_URL = "https://api.twitch.tv/helix/users"


@app.route("/auth/twitch/link")
def auth_twitch_link():
    """Initiate Twitch viewer OAuth (signed link from the Discord link panel)."""
    discord_id = request.args.get("discord_id")
    guild_id = request.args.get("guild_id", "0")
    timestamp_str = request.args.get("timestamp")
    signature = request.args.get("signature")

    if not discord_id or not timestamp_str or not signature:
        return "❌ Missing required parameters (discord_id, timestamp, signature)", 400
    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return "❌ Invalid timestamp format", 400

    if not verify_discord_id_signature(discord_id, timestamp, signature, guild_id):
        logger.info(f"🚨 SECURITY: Invalid Twitch OAuth signature for Discord ID {discord_id}")
        return (
            "❌ Invalid or expired authentication token. Please use the link panel in Discord to get a new link.",
            403,
        )

    client_id = os.getenv("TWITCH_CLIENT_ID")
    if not client_id:
        return "❌ Twitch OAuth not configured. Please set TWITCH_CLIENT_ID.", 500

    state = secrets.token_urlsafe(32)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM oauth_states WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '30 minutes'"))
        # code_verifier column tags the provider for the shared callback router.
        conn.execute(
            text(
                """
                INSERT INTO oauth_states (state, discord_id, code_verifier, created_at, guild_id)
                VALUES (:state, :discord_id, 'twitch_link', CURRENT_TIMESTAMP, :guild_id)
                """
            ),
            {"state": state, "discord_id": int(discord_id), "guild_id": int(guild_id)},
        )

    redirect_uri = f"{OAUTH_BASE_URL}/auth/twitch/link/callback"
    # Viewer linking only needs identity; no chat/sub scopes.
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "",  # public identity via /helix/users needs no scope
    }
    return redirect(f"{TWITCH_AUTHORIZE_URL}?{urlencode(auth_params)}")


@app.route("/auth/twitch/link/callback")
def auth_twitch_link_callback():
    """Handle Twitch viewer OAuth callback — create the links row (platform='twitch')."""
    import requests

    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")
    if error:
        return redirect_oauth_error(
            "Twitch authorization was cancelled or denied. Please start again when ready.",
            platform="twitch",
        )
    if not code or not state:
        return redirect_oauth_error(
            "The Twitch authorization response was incomplete. Please start again.",
            platform="twitch",
        )

    # Validate + consume state.
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT discord_id, guild_id FROM oauth_states WHERE state = :s"),
            {"s": state},
        ).fetchone()
    if not row:
        return redirect_oauth_error(
            "This Twitch connection link is invalid or expired. Please start again.",
            platform="twitch",
        )
    discord_id, guild_id = int(row[0]), int(row[1] or 0)

    client_id = os.getenv("TWITCH_CLIENT_ID")
    client_secret = os.getenv("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        return redirect_oauth_error(
            "Twitch account connections are temporarily unavailable.",
            platform="twitch",
        )
    redirect_uri = f"{OAUTH_BASE_URL}/auth/twitch/link/callback"

    try:
        token_resp = requests.post(
            TWITCH_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        if token_resp.status_code != 200:
            return redirect_oauth_error(
                "Twitch did not complete the account connection. Please try again.",
                platform="twitch",
            )
        access_token = token_resp.json().get("access_token")

        user_resp = requests.get(
            TWITCH_USERS_URL,
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": client_id},
            timeout=10,
        )
        twitch_login = None
        if user_resp.status_code == 200:
            udata = (user_resp.json().get("data") or [{}])[0]
            twitch_login = udata.get("login")
        if not twitch_login:
            return redirect_oauth_error(
                "Wagerlabs could not read your Twitch username. Please try again.",
                platform="twitch",
            )

        # Create/update the link (platform='twitch') + notify the bot to grant the role.
        from core.stream_links import upsert_link

        upsert_link(engine, discord_id, twitch_login, guild_id, platform="twitch")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM oauth_states WHERE state = :s"), {"s": state})
            conn.execute(
                text(
                    """
                    INSERT INTO oauth_notifications (discord_id, kick_username, discord_server_id, platform)
                    VALUES (:d, :k, :g, 'twitch')
                    """
                ),
                {"d": discord_id, "k": twitch_login, "g": guild_id},
            )

        notify_oauth_link_written()
        logger.info(f"✅ Twitch link successful: Discord {discord_id} -> Twitch {twitch_login}")
        return redirect_oauth_success(twitch_login, platform="twitch")
    except Exception as e:
        logger.error(f"❌ [Twitch OAuth] Error during callback: {e}")
        import traceback

        traceback.print_exc()
        return redirect_oauth_error(
            "The Twitch account connection could not be completed. Please try again.",
            platform="twitch",
        )


def exchange_code_for_token(code, code_verifier=None, redirect_uri=None):
    """Exchange authorization code for access token."""
    import requests

    # Use provided redirect_uri or default to auth/kick/callback
    if redirect_uri is None:
        redirect_uri = f"{OAUTH_BASE_URL}/auth/kick/callback"

    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": KICK_CLIENT_ID,
        "client_secret": KICK_CLIENT_SECRET,
    }

    # Only include code_verifier if PKCE was used
    if code_verifier:
        token_data["code_verifier"] = code_verifier

    logger.info(f"🔄 Exchanging code for token...")
    response = requests.post(KICK_TOKEN_URL, data=token_data, timeout=10)
    logger.info(f"📊 Token response status: {response.status_code}")
    response.raise_for_status()

    token_json = response.json()
    logger.info(f"✅ Got token response with keys: {list(token_json.keys())}")
    return token_json


def get_kick_user_info(access_token):
    """Get user information from Kick OAuth access token.

    Uses Kick's public API endpoint that works with OAuth Bearer tokens.
    Reference: https://arcticjs.dev/providers/kick
    """
    import requests

    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    try:
        # Use Kick's public API endpoint (documented in Arctic.js OAuth library)
        logger.info(f"🔍 Getting user info from Kick public API...")
        response = requests.get("https://api.kick.com/public/v1/users", headers=headers, timeout=10)

        logger.info(f"📊 Response status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            # 🔒 OPSEC: Sanitize user data before logging
            logger.info(f"✅ Got user data: {sanitize_for_logs(data)}")

            # Kick's API returns: {"data": [{"user_id": ..., "name": "...", "email": "..."}], "message": "OK"}
            if "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                user = data["data"][0]
                return {
                    "username": user.get("name"),
                    "id": user.get("user_id"),
                    "email": user.get("email"),
                    "profile_picture": user.get("profile_picture"),
                }

        # If we get here, the API didn't return expected data
        logger.warning(f"⚠️ Unexpected response format: {response.text[:200]}")
        raise Exception(f"Kick API returned status {response.status_code}")

    except Exception as e:
        logger.error(f"❌ Failed to get user info: {e}")
        raise Exception(f"Could not get user info from Kick API: {str(e)}")


@app.route("/bot/authorize")
def bot_authorize():
    """
    Special route for getting bot access token without Discord account requirement.
    This initiates OAuth flow specifically for the bot account.
    Requires admin authentication via token.
    """
    try:
        # Check for admin authorization token. Prefer the X-Bot-Auth header (which
        # does not land in access logs); fall back to the query param so the legacy
        # browser link keeps working. Compare in constant time.
        auth_token = request.headers.get("X-Bot-Auth") or request.args.get("token")
        expected_token = os.getenv("BOT_AUTH_TOKEN")

        if not expected_token:
            logger.warning(f"⚠️ BOT_AUTH_TOKEN not configured - bot authorization disabled")
            return redirect_oauth_error(
                "Kick bot authorization is not configured. Contact the administrator.",
                flow="bot",
            )

        if not auth_token or not hmac.compare_digest(str(auth_token), expected_token):
            logger.error(f"❌ Invalid or missing bot authorization token")
            return redirect_oauth_error(
                "This Kick bot authorization link is invalid or expired.",
                flow="bot",
            )

        logger.info(f"🤖 Bot authorization initiated with valid token")

        # kick_oauth_tokens table is created by the dashboard - no need to create it here

        # Generate PKCE pair
        code_verifier, code_challenge = generate_pkce_pair()

        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)
        logger.info(f"🔑 Generated state for bot authorization")

        # Store state in database with special discord_id = 0 for bot
        with engine.begin() as conn:
            # Clean up old states
            deleted_count = conn.execute(
                text(
                    """
                DELETE FROM oauth_states
                WHERE created_at < CURRENT_TIMESTAMP - INTERVAL '30 minutes'
            """
                )
            ).rowcount
            logger.info(f"🧹 Cleaned up {deleted_count} expired state(s)")

            # Store new state with discord_id = 0 to indicate bot authorization
            conn.execute(
                text(
                    """
                INSERT INTO oauth_states (state, discord_id, code_verifier, created_at)
                VALUES (:state, :discord_id, :code_verifier, CURRENT_TIMESTAMP)
            """
                ),
                {"state": state, "discord_id": 0, "code_verifier": code_verifier},
            )
            logger.info(f"✅ Bot state saved to database")

        # Build authorization URL with chat:write scope
        # Use same callback as regular OAuth to avoid needing multiple redirect URIs
        redirect_uri = f"{OAUTH_BASE_URL}/auth/kick/callback"

        auth_params = {
            "client_id": KICK_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": "chat:write chat:read channel:read user:read events:subscribe",  # Match Botrix scopes
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        logger.info(f"🔗 Bot authorization URL: {KICK_AUTHORIZE_URL}?{urlencode(auth_params)}")

        auth_url = f"{KICK_AUTHORIZE_URL}?{urlencode(auth_params)}"

        return redirect(auth_url)
    except Exception as e:
        logger.error(f"❌ Error in bot_authorize: {e}")
        import traceback

        traceback.print_exc()
        return redirect_oauth_error(
            "Kick bot authorization could not be started. Please try again.",
            flow="bot",
        )


if __name__ == "__main__":
    # Use OAUTH_PORT if set, otherwise use PORT, otherwise default to 8000
    # This allows Flask to run on a different port than Railway's main PORT
    port = int(os.getenv("OAUTH_PORT", os.getenv("PORT", 8000)))
    logger.info(f"🚀 Starting OAuth server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
