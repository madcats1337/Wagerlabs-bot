"""
Kick Webhook Handler Module
Handles incoming webhook events from Kick's official API

This module provides Flask routes and handlers for processing Kick webhook events:
- chat.message.sent - Chat messages
- channel.followed - New followers
- channel.subscription.new - New subscriptions
- channel.subscription.renewal - Subscription renewals
- channel.subscription.gifts - Gifted subscriptions
- livestream.status.updated - Stream live/offline events
- moderation.banned - User banned from chat
- kicks.gifted - Tips (Kicks) received

Usage:
    from core.kick_webhooks import register_webhook_routes, WebhookEventHandler

    # Register routes with Flask app
    register_webhook_routes(app, event_handler)
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Dict, Optional

from flask import Blueprint, abort, jsonify, request

from utils.secret_settings import decrypt_secret

logger = logging.getLogger(__name__)


def _test_endpoints_enabled() -> bool:
    """Whether the simulate/test webhook routes are reachable.

    These bypass Kick's signature verification (they inject arbitrary events,
    including gifted-sub -> raffle tickets and fake go-live), so they are disabled
    unless ENABLE_TEST_WEBHOOKS is explicitly set. Must stay off in production.
    """
    return os.getenv("ENABLE_TEST_WEBHOOKS", "").lower() in ("1", "true", "yes")


# RSA verification imports
try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.info("[Webhook] ⚠️ cryptography library not installed - signature verification disabled")

# Import webhook payload classes
try:
    from .kick_official_api import (
        WebhookChatMessage,
        WebhookGiftedSubs,
        WebhookKicksGifted,
        WebhookLivestreamStatus,
        verify_webhook_signature,
    )
except ImportError:
    # Fallback definitions if import fails
    pass

# -------------------------
# Webhook Configuration
# -------------------------

WEBHOOK_SECRET = os.getenv("KICK_WEBHOOK_SECRET", "")

# Kick's Public Key for webhook signature verification
KICK_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAq/+l1WnlRrGSolDMA+A8
6rAhMbQGmQ2SapVcGM3zq8ANXjnhDWocMqfWcTd95btDydITa10kDvHzw9WQOqp2
MZI7ZyrfzJuz5nhTPCiJwTwnEtWft7nV14BYRDHvlfqPUaZ+1KR4OCaO/wWIk/rQ
L/TjY0M70gse8rlBkbo2a8rKhu69RQTRsoaf4DVhDPEeSeI5jVrRDGAMGL3cGuyY
6CLKGdjVEM78g3JfYOvDU/RvfqD7L89TZ3iN94jrmWdGz34JNlEI5hqK8dd7C5EF
BEbZ5jgB8s8ReQV8H+MkuffjdAj3ajDDX3DOJMIut1lBrUVD1AaSrGCKHooWoL2e
twIDAQAB
-----END PUBLIC KEY-----"""

# Cache the loaded public key
_kick_public_key = None


def get_kick_public_key():
    """Load and cache Kick's public key"""
    global _kick_public_key
    if _kick_public_key is None and CRYPTO_AVAILABLE:
        _kick_public_key = serialization.load_pem_public_key(KICK_PUBLIC_KEY.encode("utf-8"), backend=default_backend())
    return _kick_public_key


# Create Flask Blueprint for webhook routes
kick_webhooks_bp = Blueprint("kick_webhooks", __name__)

# Track if tables have been initialized
_tables_initialized = False


def ensure_webhook_tables(engine):
    """Ensure webhook-related tables exist"""
    global _tables_initialized
    if _tables_initialized:
        return

    try:
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                CREATE TABLE IF NOT EXISTS processed_webhook_messages (
                    id SERIAL PRIMARY KEY,
                    message_id VARCHAR(255) NOT NULL,
                    broadcaster_user_id VARCHAR(50) NOT NULL,
                    event_type VARCHAR(100),
                    processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    CONSTRAINT unique_message_id UNIQUE (message_id)
                )
            """
                )
            )
            conn.execute(
                text(
                    """
                CREATE INDEX IF NOT EXISTS idx_processed_webhook_messages_lookup
                ON processed_webhook_messages(message_id, broadcaster_user_id)
            """
                )
            )
            logger.info("[Webhook] ✅ Webhook tables initialized")
        _tables_initialized = True
    except Exception as e:
        logger.info(f"[Webhook] ⚠️ Could not initialize tables: {e}")


# -------------------------
# Signature Verification
# -------------------------


def verify_kick_signature(
    raw_body: bytes, signature_header: str, message_id: str = None, timestamp: str = None
) -> bool:
    """
    Verify the Kick webhook signature using RSA public key.

    Kick signs webhooks using their private RSA key. We verify using their public key.
    The signature is created from: message_id.timestamp.body

    Args:
        raw_body: Raw request body as bytes
        signature_header: Base64-encoded signature from Kick-Event-Signature header
        message_id: Message ID from Kick-Event-Message-Id header
        timestamp: Timestamp from Kick-Event-Message-Timestamp header

    Returns:
        True if signature is valid, False otherwise
    """
    if not CRYPTO_AVAILABLE:
        logger.info("[Webhook] ❌ CRITICAL: Crypto library not available - rejecting webhook for security")
        return False  # SECURITY: Fail-closed - reject if we can't verify

    if not signature_header:
        logger.info("[Webhook] ❌ No signature header provided")
        return False

    if not message_id or not timestamp:
        logger.info("[Webhook] ❌ Missing message_id or timestamp for signature verification")
        return False

    try:
        # Get Kick's public key
        public_key = get_kick_public_key()
        if not public_key:
            logger.info("[Webhook] ❌ Could not load Kick public key")
            return False

        # Decode the base64 signature
        try:
            signature_bytes = base64.b64decode(signature_header)
        except Exception as e:
            logger.info(f"[Webhook] ❌ Failed to decode signature: {e}")
            return False

        # Create the message that was signed: message_id.timestamp.body
        # Body should be the raw request body as-is (no decoding/encoding manipulation)
        body_str = raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
        message_to_verify = f"{message_id}.{timestamp}.{body_str}".encode("utf-8")

        # Debug output
        if os.getenv("DEBUG_WEBHOOKS") == "true":
            logger.info(f"[Webhook] 🔐 Message to verify (first 100 chars): {message_to_verify[:100]}")
            logger.info(f"[Webhook] 🔐 Signature length: {len(signature_bytes)}")

        # Verify using RSA PKCS1v15 with SHA256
        try:
            public_key.verify(signature_bytes, message_to_verify, padding.PKCS1v15(), hashes.SHA256())
            logger.info(f"[Webhook] ✅ RSA signature verification successful")
            return True
        except Exception as verify_err:
            logger.info(f"[Webhook] ❌ RSA verification failed: {verify_err}")
            # Additional debug info
            if os.getenv("DEBUG_WEBHOOKS") == "true":
                import hashlib

                msg_hash = hashlib.sha256(message_to_verify).hexdigest()
                logger.info(f"[Webhook] 🔐 Message SHA256 hash: {msg_hash}")
            return False

    except Exception as e:
        logger.info(f"[Webhook] ❌ Signature verification error: {e}")
        return False


def require_webhook_signature(f):
    """Decorator to require valid webhook signature"""

    @wraps(f)
    def decorated(*args, **kwargs):
        # Note: Signature verification happens in the route handler
        # after we look up the secret from the database
        return f(*args, **kwargs)

    return decorated


# -------------------------
# Event Handler Class
# -------------------------


class WebhookEventHandler:
    """
    Handler for Kick webhook events.

    Register callbacks for specific event types, then pass events to handle().

    Usage:
        handler = WebhookEventHandler()

        @handler.on("chat.message.sent")
        async def handle_chat(event_data):
            print(f"Chat: {event_data['sender']['username']}: {event_data['content']}")

        @handler.on("channel.subscription.gifts")
        async def handle_gifted_subs(event_data):
            gifter = event_data['gifter']['username']
            count = len(event_data['giftees'])
            print(f"{gifter} gifted {count} subs!")
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._default_handler: Optional[Callable] = None

    def on(self, event_type: str):
        """
        Decorator to register a handler for an event type.

        Args:
            event_type: The webhook event type (e.g., "chat.message.sent")
        """

        def decorator(func):
            self._handlers[event_type] = func
            return func

        return decorator

    def set_default_handler(self, func):
        """Set a default handler for unregistered event types"""
        self._default_handler = func
        return func

    async def handle(self, event_type: str, event_data: Dict[str, Any]) -> bool:
        """
        Handle a webhook event.

        Args:
            event_type: The event type from webhook headers
            event_data: The parsed JSON payload

        Returns:
            True if handler was found and executed
        """
        handler = self._handlers.get(event_type, self._default_handler)

        if handler is None:
            logger.info(f"[Webhook] ⚠️ No handler for event type: {event_type}")
            return False

        try:
            # Check if handler is async
            import asyncio

            if asyncio.iscoroutinefunction(handler):
                await handler(event_data)
            else:
                handler(event_data)
            return True
        except Exception as e:
            logger.info(f"[Webhook] ❌ Error in handler for {event_type}: {e}")
            import traceback

            traceback.print_exc()
            return False


# -------------------------
# Flask Routes
# -------------------------

# Global event handler (set via register_webhook_routes)
_event_handler: Optional[WebhookEventHandler] = None


@kick_webhooks_bp.route("/webhooks/kick", methods=["POST"])
def handle_kick_webhook():
    """
    Main webhook endpoint for Kick events.

    Expected Headers:
    - Kick-Event-Type: Event type
    - Kick-Event-Message-Id: Unique message ID
    - Kick-Event-Subscription-Id: Subscription ID
    - Kick-Signature: HMAC-SHA256 signature

    Returns:
        200 OK on success
        401 Unauthorized if signature invalid
        500 Internal Server Error on handler error
    """
    # 0️⃣ SKIP VERIFICATION FOR NON-POST REQUESTS (health checks, etc.)
    if request.method != "POST":
        return "", 204

    # 0.5️⃣ ENSURE TABLES EXIST
    from sqlalchemy import create_engine

    DATABASE_URL = os.getenv("DATABASE_URL")
    if DATABASE_URL:
        engine = create_engine(DATABASE_URL)
        ensure_webhook_tables(engine)

    # 1️⃣ GET RAW BODY FIRST (CRITICAL: before ANY json parsing)
    raw_body: bytes = request.get_data(cache=True)

    # 2️⃣ GET HEADERS
    event_type = request.headers.get("Kick-Event-Type", "unknown")
    message_id = request.headers.get("Kick-Event-Message-Id", "")
    message_timestamp = request.headers.get("Kick-Event-Message-Timestamp", "")
    subscription_id = request.headers.get("Kick-Event-Subscription-Id", "")

    # 🚫 ARCHITECTURAL DECISION: Ignore chat.message.sent (handled by kickpython WebSockets)
    # EARLY EXIT before verification, database lookup, or ANY processing
    if event_type == "chat.message.sent":
        logger.info(f"[Webhook] ⚠️  Ignoring chat.message.sent — chat handled via WebSockets")
        return jsonify({"status": "ok", "message": "chat handled by websockets"}), 200

    # Look for Kick-Event-Signature header (case-insensitive)
    # Kick officially uses: Kick-Event-Signature
    signature_header = None
    signature_header_name = None
    for header_name in request.headers.keys():
        if header_name.lower() == "kick-event-signature":
            signature_header = request.headers.get(header_name)
            signature_header_name = header_name
            break

    # DEBUG: Only log when signature is missing (temporary diagnostic)
    if not signature_header:
        if os.getenv("DEBUG_WEBHOOKS") == "true":
            logger.info(f"[Webhook] 🔍 DEBUG: method={request.method}, content-length={request.content_length}")
            logger.info(f"[Webhook] 🔍 DEBUG: Available headers: {list(request.headers.keys())}")
        logger.info("[Webhook] ❌ Missing signature header (looking for Kick-Event-Signature)")
        return jsonify({"error": "Missing signature"}), 401

    # DEBUG: Log which header was found (temporary diagnostic)
    if os.getenv("DEBUG_WEBHOOKS") == "true":
        logger.info(f"[Webhook] ✅ Found signature in header: {signature_header_name}")
        logger.info(f"[Webhook] ℹ️  Event: {event_type}, Message ID: {message_id}, Timestamp: {message_timestamp}")

    if not subscription_id:
        logger.info("[Webhook] ❌ Missing subscription ID")
        return jsonify({"error": "Missing subscription ID"}), 400

    try:
        # 3️⃣ RESOLVE SUBSCRIPTION CONTEXT FROM DATABASE
        # Use subscription_id from header to get broadcaster_user_id and discord_server_id
        # This works for ALL event types (chat.message.sent doesn't have broadcaster in payload)
        discord_server_id = None
        broadcaster_user_id = None
        webhook_secret = None

        try:
            from sqlalchemy import create_engine, text

            db_url = os.getenv("DATABASE_URL")
            if db_url:
                engine = create_engine(db_url, pool_pre_ping=True)
                with engine.connect() as conn:
                    result = conn.execute(
                        text(
                            """
                        SELECT discord_server_id, broadcaster_user_id, webhook_secret
                        FROM kick_webhook_subscriptions
                        WHERE subscription_id = :sub_id AND status = 'active'
                    """
                        ),
                        {"sub_id": subscription_id},
                    ).fetchone()

                    if result:
                        discord_server_id = result[0]
                        broadcaster_user_id = result[1]
                        # Decrypt at the signature-verification boundary.
                        webhook_secret = decrypt_secret(
                            result[2],
                            key_name="kick_webhook_subscriptions.webhook_secret",
                            row_id=subscription_id,
                        )

                        if os.getenv("DEBUG_WEBHOOKS") == "true":
                            logger.info(
                                f"[Webhook] ✅ Resolved subscription: server={discord_server_id}, broadcaster={broadcaster_user_id}"
                            )
                    else:
                        # Unknown subscription (legacy/old webhook still firing)
                        # Return 200 OK to prevent Kick retry storms, but do NOT process
                        logger.info(
                            f"[Webhook] ⚠️  Unknown subscription ID: {subscription_id} (legacy webhook, ignoring)"
                        )
                        return jsonify({"status": "ok", "message": "unknown subscription"}), 200
        except Exception as db_err:
            logger.info(f"[Webhook] ❌ Database error: {db_err}")
            return jsonify({"error": "Database error"}), 500

        # 4️⃣ VERIFY SIGNATURE (using Kick's RSA public key)
        logger.info(f"[Webhook] 🔐 Verifying RSA signature...")
        logger.info(f"[Webhook] 🔐 Message ID: {message_id}")
        logger.info(f"[Webhook] 🔐 Timestamp: {message_timestamp}")
        logger.info(f"[Webhook] 🔐 Signature header present: {bool(signature_header)}")
        logger.info(f"[Webhook] 🔐 Raw body length: {len(raw_body)} bytes")

        if not verify_kick_signature(raw_body, signature_header, message_id, message_timestamp):
            logger.info(f"[Webhook] ❌ Invalid signature for subscription {subscription_id}")
            return jsonify({"error": "Invalid signature"}), 401

        logger.info(f"[Webhook] ✅ Signature verified for subscription {subscription_id}")

        # 4.5️⃣ REPLAY PROTECTION: reject deliveries whose signed timestamp is outside a
        # ~10-minute window (mirrors the Twitch path's MAX_MESSAGE_AGE). The signature
        # is already verified, so the timestamp is authentic. Skip only when the header
        # is absent or unparseable (the idempotency table still catches exact replays).
        MAX_WEBHOOK_AGE_SECONDS = 600
        if message_timestamp:
            ts = None
            try:
                ts = datetime.fromisoformat(message_timestamp.replace("Z", "+00:00"))
            except ValueError:
                try:
                    ts = datetime.fromtimestamp(int(message_timestamp), tz=timezone.utc)
                except (ValueError, OverflowError):
                    ts = None
            if ts is not None:
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age = abs((datetime.now(timezone.utc) - ts).total_seconds())
                if age > MAX_WEBHOOK_AGE_SECONDS:
                    logger.warning(
                        f"[Webhook] ❌ Stale webhook (age {age:.0f}s > {MAX_WEBHOOK_AGE_SECONDS}s); rejecting replay"
                    )
                    return jsonify({"error": "Stale webhook"}), 401

        # 5️⃣ PARSE JSON AFTER SIGNATURE VERIFICATION
        try:
            event_data = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON"}), 400

        # Add server context to event data for handlers
        event_data["_server_id"] = discord_server_id
        event_data["_broadcaster_user_id"] = broadcaster_user_id

        logger.info(f"[Webhook] 📥 Event for server {discord_server_id}, broadcaster {broadcaster_user_id}")

        # 6️⃣ IDEMPOTENCY CHECK (Kick retries webhooks on failure)
        # Check if we've already processed this message_id
        if message_id:
            try:
                from sqlalchemy import text as sql_text

                with engine.begin() as conn:
                    # Check if this message was already processed
                    existing = conn.execute(
                        sql_text(
                            """
                        SELECT id FROM processed_webhook_messages
                        WHERE message_id = :msg_id AND broadcaster_user_id = :broadcaster_id
                        LIMIT 1
                    """
                        ),
                        {"msg_id": message_id, "broadcaster_id": broadcaster_user_id},
                    ).fetchone()

                    if existing:
                        logger.info(f"[Webhook] ℹ️ Duplicate message {message_id}, already processed")
                        # Return 200 OK to prevent Kick from retrying
                        return jsonify({"status": "ok", "message": "already processed"}), 200

                    # Mark this message as processed (with expiry for cleanup)
                    conn.execute(
                        sql_text(
                            """
                        INSERT INTO processed_webhook_messages
                        (message_id, broadcaster_user_id, event_type, processed_at)
                        VALUES (:msg_id, :broadcaster_id, :event_type, NOW())
                        ON CONFLICT (message_id) DO NOTHING
                    """
                        ),
                        {"msg_id": message_id, "broadcaster_id": broadcaster_user_id, "event_type": event_type},
                    )
            except Exception as dedup_err:
                # If deduplication fails, continue anyway (better to process twice than not at all)
                logger.info(f"[Webhook] ⚠️ Deduplication check failed: {dedup_err}")

        # 8️⃣ HANDLE EVENT
        if _event_handler:
            import asyncio

            # Run async handler in event loop
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_event_handler.handle(event_type, event_data))
            finally:
                loop.close()
        else:
            _log_event(event_type, event_data)

        return jsonify({"status": "ok", "message_id": message_id}), 200

    except Exception as e:
        logger.info(f"[Webhook] ❌ Error processing webhook: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@kick_webhooks_bp.route("/webhooks/kick/challenge", methods=["GET", "POST"])
def handle_webhook_challenge():
    """
    Handle Kick webhook verification challenge.

    Kick may send a challenge request when registering webhooks.
    We need to echo back the challenge value.
    """
    if request.method == "GET":
        challenge = request.args.get("challenge", "")
    else:
        data = request.get_json() or {}
        challenge = data.get("challenge", "")

    if challenge:
        return challenge, 200, {"Content-Type": "text/plain"}

    return jsonify({"status": "ready"}), 200


@kick_webhooks_bp.route("/webhooks/kick/test", methods=["GET"])
def test_webhook_endpoint():
    """Minimal reachability check. Does not disclose secret/config state."""
    return (
        jsonify(
            {
                "status": "ok",
                "message": "Webhook server is running",
                "endpoint": "/webhooks/kick",
            }
        ),
        200,
    )


@kick_webhooks_bp.route("/webhooks/kick/simulate", methods=["POST"])
def simulate_webhook_event():
    """
    Simulate a webhook event for testing purposes.

    This endpoint bypasses signature verification and directly triggers
    the event handler as if a real webhook was received.

    Requires a secret test token to prevent abuse.

    Expected JSON body:
    {
        "test_token": "your-test-token",
        "event_type": "livestream.status.updated",
        "discord_server_id": "123456789",
        "broadcaster_user_id": "152837",
        "data": { ... event-specific data ... }
    }
    """
    if not _test_endpoints_enabled():
        return jsonify({"error": "Not found"}), 404

    # Verify test token - must be set via WEBHOOK_TEST_TOKEN env var
    test_token = os.getenv("WEBHOOK_TEST_TOKEN", "")

    if not test_token:
        logger.info(f"[Webhook Simulate] ❌ WEBHOOK_TEST_TOKEN not configured")
        return jsonify({"error": "WEBHOOK_TEST_TOKEN not configured on bot"}), 500

    try:
        body = request.get_json() or {}
    except:
        return jsonify({"error": "Invalid JSON"}), 400

    provided_token = body.get("test_token", "")
    if not provided_token or not hmac.compare_digest(provided_token, test_token):
        logger.info(
            f"[Webhook Simulate] ❌ Invalid test token (provided: {provided_token[:8]}... expected: {test_token[:8]}...)"
        )
        return jsonify({"error": "Invalid test token"}), 401

    event_type = body.get("event_type", "livestream.status.updated")
    discord_server_id = body.get("discord_server_id")
    broadcaster_user_id = body.get("broadcaster_user_id")
    event_data = body.get("data", {})

    if not discord_server_id:
        return jsonify({"error": "Missing discord_server_id"}), 400

    # Build simulated event data
    if event_type == "livestream.status.updated":
        # Build a realistic livestream.status.updated payload
        simulated_data = {
            "broadcaster": {
                "is_live": True,
                "user_id": int(broadcaster_user_id) if broadcaster_user_id else 0,
                "slug": event_data.get("slug", "test_streamer"),
            },
            "livestream": {
                "id": f"test-{int(datetime.now().timestamp())}",
                "session_title": event_data.get("title", "🧪 TEST - Simulated Stream Notification"),
                "viewers": event_data.get("viewers", 100),
                "category": {"name": event_data.get("category", "Just Chatting")},
            },
            "is_live": True,
            "_server_id": discord_server_id,
            "_broadcaster_user_id": broadcaster_user_id,
            "_simulated": True,
        }
    else:
        # For other event types, just pass through the data with context
        simulated_data = {
            **event_data,
            "_server_id": discord_server_id,
            "_broadcaster_user_id": broadcaster_user_id,
            "_simulated": True,
        }

    logger.info(f"[Webhook Simulate] 🧪 Simulating {event_type} for server {discord_server_id}")

    # Handle the event
    if _event_handler:
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_event_handler.handle(event_type, simulated_data))
            loop.close()
            return (
                jsonify(
                    {
                        "status": "ok",
                        "message": f"Simulated {event_type} event processed",
                        "event_type": event_type,
                        "discord_server_id": discord_server_id,
                    }
                ),
                200,
            )
        except Exception as e:
            logger.info(f"[Webhook Simulate] ❌ Error: {e}")
            import traceback

            traceback.print_exc()
            return jsonify({"error": str(e)}), 500
    else:
        logger.info(f"[Webhook Simulate] ⚠️ No event handler registered")
        return jsonify({"error": "No event handler registered"}), 500


@kick_webhooks_bp.route("/webhooks/kick/simulate-real", methods=["POST"])
def simulate_real_webhook_event():
    """
    Simulate a webhook event using the REAL subscription ID and signature verification.

    This tests the full webhook flow as if Kick actually sent it:
    1. Looks up the real subscription ID from the database
    2. Uses the stored webhook secret to generate a valid signature
    3. Calls the real webhook endpoint with proper headers

    This verifies that subscription IDs are properly stored and signature verification works.

    Expected JSON body:
    {
        "test_token": "your-test-token",
        "event_type": "livestream.status.updated",
        "discord_server_id": "123456789",
        "data": { ... optional event-specific data ... }
    }
    """
    if not _test_endpoints_enabled():
        return jsonify({"error": "Not found"}), 404

    from datetime import datetime

    import requests

    # Verify test token
    test_token = os.getenv("WEBHOOK_TEST_TOKEN", "")
    if not test_token:
        return jsonify({"error": "WEBHOOK_TEST_TOKEN not configured on bot"}), 500

    try:
        body = request.get_json() or {}
    except:
        return jsonify({"error": "Invalid JSON"}), 400

    provided_token = body.get("test_token", "")
    if not provided_token or not hmac.compare_digest(provided_token, test_token):
        return jsonify({"error": "Invalid test token"}), 401

    event_type = body.get("event_type", "livestream.status.updated")
    discord_server_id = body.get("discord_server_id")
    event_data = body.get("data", {})

    if not discord_server_id:
        return jsonify({"error": "Missing discord_server_id"}), 400

    # Look up real subscription ID and secret from database
    try:
        from sqlalchemy import create_engine, text

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            return jsonify({"error": "DATABASE_URL not configured"}), 500

        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT subscription_id, broadcaster_user_id, webhook_secret
                FROM kick_webhook_subscriptions
                WHERE discord_server_id = :server_id
                AND event_type = :event_type
                AND status = 'active'
                LIMIT 1
            """
                ),
                {"server_id": discord_server_id, "event_type": event_type},
            ).fetchone()

            if not result:
                return (
                    jsonify(
                        {
                            "error": f"No active subscription found for server {discord_server_id} and event {event_type}",
                            "hint": "Run setup_webhooks.py to register webhooks",
                        }
                    ),
                    404,
                )

            subscription_id = result[0]
            broadcaster_user_id = result[1]
            webhook_secret = decrypt_secret(
                result[2], key_name="kick_webhook_subscriptions.webhook_secret", row_id=subscription_id
            )

            if not webhook_secret:
                return (
                    jsonify(
                        {"error": "Subscription exists but has no webhook secret", "subscription_id": subscription_id}
                    ),
                    500,
                )

    except Exception as db_err:
        return jsonify({"error": f"Database error: {db_err}"}), 500

    # Build the simulated event payload
    if event_type == "livestream.status.updated":
        payload = {
            "broadcaster": {
                "is_live": True,
                "user_id": int(broadcaster_user_id) if broadcaster_user_id else 0,
                "slug": event_data.get("slug", "test_streamer"),
            },
            "livestream": {
                "id": f"test-real-{int(datetime.now().timestamp())}",
                "session_title": event_data.get("title", "🔬 REAL TEST - Subscription ID Verified"),
                "viewers": event_data.get("viewers", 100),
                "category": {"name": event_data.get("category", "Just Chatting")},
            },
            "is_live": True,
        }
    else:
        payload = event_data

    # Generate valid signature using the stored webhook secret
    # NOTE: This uses HMAC which was the OLD signature method
    # Kick now uses RSA signatures which we can't generate (only Kick has the private key)
    # So we call the /simulate endpoint which bypasses signature verification
    payload_bytes = json.dumps(payload).encode("utf-8")

    message_id = f"test-{int(datetime.now().timestamp())}"
    message_timestamp = datetime.now().isoformat()

    # Call the SIMULATE endpoint (bypasses RSA verification)
    # This tests the event handling logic without requiring Kick's private key
    bot_public_url = os.getenv("BOT_PUBLIC_URL", "https://bot.wagerlabs.app")
    webhook_url = f"{bot_public_url}/webhooks/kick/simulate"

    # Simulate endpoint expects different payload format
    simulate_payload = {
        "test_token": provided_token,
        "event_type": event_type,
        "discord_server_id": discord_server_id,
        "broadcaster_user_id": broadcaster_user_id,
        "data": payload,
    }

    headers = {"Content-Type": "application/json"}

    logger.info(f"[Webhook Real Test] 🔬 Testing webhook event handling:")
    logger.info(f"  Webhook URL: {webhook_url}")
    logger.info(f"  Subscription ID: {subscription_id}")
    logger.info(f"  Event Type: {event_type}")
    logger.info(f"  Server ID: {discord_server_id}")
    logger.info(f"  Broadcaster: {broadcaster_user_id}")
    logger.info(f"  Note: Using /simulate endpoint (RSA signatures can only be generated by Kick)")

    try:
        # Don't follow redirects - POST can become GET on redirect
        resp = requests.post(webhook_url, json=simulate_payload, headers=headers, timeout=10, allow_redirects=False)

        if resp.status_code == 200:
            return (
                jsonify(
                    {
                        "success": True,
                        "message": "Real webhook test PASSED - subscription ID and signature verified!",
                        "subscription_id": subscription_id,
                        "broadcaster_user_id": broadcaster_user_id,
                        "event_type": event_type,
                        "webhook_response": resp.json(),
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": f"Webhook returned {resp.status_code}",
                        "subscription_id": subscription_id,
                        "response": resp.text[:500],
                    }
                ),
                500,
            )

    except Exception as e:
        return (
            jsonify({"success": False, "error": f"Failed to call webhook: {e}", "subscription_id": subscription_id}),
            500,
        )


def _log_event(event_type: str, event_data: Dict[str, Any]):
    """Log webhook event for debugging (used when no handler registered)"""
    logger.info(f"[Webhook] Event Data:")
    logger.info(json.dumps(event_data, indent=2, default=str))


# -------------------------
# Registration Function
# -------------------------


def register_webhook_routes(app, event_handler: WebhookEventHandler = None):
    """
    Register webhook routes with a Flask app.

    Args:
        app: Flask application
        event_handler: Optional WebhookEventHandler for processing events
    """
    global _event_handler
    _event_handler = event_handler

    app.register_blueprint(kick_webhooks_bp)
    logger.info("[Webhook] ✅ Registered Kick webhook routes at /webhooks/kick")


# -------------------------
# Pre-built Event Handlers
# -------------------------


def create_discord_notifier(discord_bot, channel_id: int):
    """
    Create a WebhookEventHandler that sends notifications to Discord.

    Args:
        discord_bot: Discord bot instance
        channel_id: Discord channel ID for notifications

    Returns:
        Configured WebhookEventHandler
    """
    handler = WebhookEventHandler()

    @handler.on("channel.subscription.new")
    async def on_new_sub(data):
        """Handle new subscription"""
        subscriber = data.get("subscriber", {}).get("username", "Unknown")
        duration = data.get("duration", 1)
        broadcaster = data.get("broadcaster", {}).get("username", "")

        channel = discord_bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"🎉 **New Subscriber!**\n"
                f"**{subscriber}** just subscribed to **{broadcaster}** ({duration} month(s))!"
            )

    @handler.on("channel.subscription.gifts")
    async def on_gifted_subs(data):
        """Handle gifted subscriptions"""
        gifter = data.get("gifter", {}).get("username", "Anonymous")
        giftees = data.get("giftees", [])
        count = len(giftees)
        broadcaster = data.get("broadcaster", {}).get("username", "")
        discord_server_id = data.get("_server_id")

        # Track in raffle system. Raffle tickets go to the GIFTER, scaled by
        # the number of subs gifted — so this is ONE award call per event
        # (not one per giftee). track_gifted_sub resolves the active period and
        # per-server reward rate itself and dedups on event_id.
        if discord_server_id and count > 0:
            try:
                from sqlalchemy import create_engine

                from raffle_system.gifted_sub_tracker import track_gifted_sub

                db_url = os.getenv("DATABASE_URL")
                if db_url:
                    engine = create_engine(db_url, pool_pre_ping=True)
                    # Prefer Kick's own event id for dedup; fall back to a
                    # stable composite so re-delivery of the same event is a
                    # duplicate, not a double-award.
                    event_id = data.get("id") or data.get("event_id")
                    if not event_id:
                        giftee_names = "_".join(sorted(g.get("username", "") for g in giftees))
                        event_id = f"webhook_{discord_server_id}_{gifter}_{count}_{giftee_names}"

                    result = await track_gifted_sub(
                        engine,
                        gifter_username=gifter,
                        guild_id=discord_server_id,
                        count=count,
                        event_id=event_id,
                    )
                    logger.info(f"[Webhook] Gifted-sub raffle tracking for {gifter} (x{count}): {result.get('status')}")
            except Exception as e:
                logger.info(f"[Webhook] ⚠️ Failed to track gifted subs in raffle: {e}")

        channel = discord_bot.get_channel(channel_id)
        if channel:
            giftee_names = ", ".join(g.get("username", "?") for g in giftees[:5])
            if count > 5:
                giftee_names += f" and {count - 5} more"

            await channel.send(
                f"🎁 **Gifted Subs!**\n"
                f"**{gifter}** gifted **{count}** sub(s) to **{broadcaster}**!\n"
                f"Recipients: {giftee_names}"
            )

    @handler.on("channel.subscription.renewal")
    async def on_sub_renewal(data):
        """Handle subscription renewal"""
        subscriber = data.get("subscriber", {}).get("username", "Unknown")
        duration = data.get("duration", 1)
        broadcaster = data.get("broadcaster", {}).get("username", "")
        discord_server_id = data.get("_server_id")

        logger.info(f"[Webhook] 🔄 Sub renewal: {subscriber} renewed for {duration} month(s)")

        # Renewals could grant bonus tickets or other rewards in the future
        # For now, just log it

        channel = discord_bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"🔄 **Subscription Renewed!**\n"
                f"**{subscriber}** renewed their subscription to **{broadcaster}** ({duration} month(s))!"
            )

    @handler.on("kicks.gifted")
    async def on_kicks(data):
        """Handle Kicks (tips)"""
        sender = data.get("sender", {}).get("username", "Anonymous")
        amount = data.get("amount", 0)
        kick_count = data.get("kick_count", 0)
        broadcaster = data.get("broadcaster", {}).get("username", "")

        channel = discord_bot.get_channel(channel_id)
        if channel:
            await channel.send(
                f"💚 **Kicks Received!**\n"
                f"**{sender}** sent **{kick_count}** Kicks (${amount:.2f}) to **{broadcaster}**!"
            )

    @handler.on("livestream.status.updated")
    async def on_stream_status(data):
        """Handle stream going live/offline - triggers clip buffer and dashboard notifications.

        Delegates the go-live notification + Redis publish + clip buffer to the
        shared, platform-agnostic sender (core.stream_notifications) so Kick and
        Twitch behave identically. The only Kick-specific extra is the legacy
        message to the handler's hardcoded channel below.
        """
        is_live = data.get("is_live", False)
        broadcaster = data.get("broadcaster", {}).get("username", "")
        title = data.get("livestream", {}).get("session_title", "")
        category = data.get("livestream", {}).get("category", {}).get("name", "Just Chatting")
        discord_server_id = data.get("_server_id")

        logger.info(
            f"[Webhook] 📺 Stream status update: {broadcaster} is_live={is_live}, server_id={discord_server_id}"
        )

        # Shared sender: Discord notification + Redis publish + clip buffer control.
        from core.stream_notifications import send_stream_notification

        await send_stream_notification(
            discord_server_id=discord_server_id,
            streamer=broadcaster,
            is_live=is_live,
            title=title,
            category=category,
            platform="kick",
        )

        # Legacy basic notification to the handler's configured channel (Kick-only).
        channel = discord_bot.get_channel(channel_id)
        if channel:
            if is_live:
                await channel.send(
                    f"🔴 **Stream Live!**\n"
                    f"**{broadcaster}** is now streaming!\n"
                    f"Title: {title}\n"
                    f"https://kick.com/{broadcaster}"
                )
            else:
                await channel.send(f"⚫ **Stream Ended**\n" f"**{broadcaster}** has ended their stream.")

    @handler.on("channel.followed")
    async def on_follow(data):
        """Handle new follower"""
        follower = data.get("follower", {}).get("username", "Unknown")
        broadcaster = data.get("broadcaster", {}).get("username", "")

        channel = discord_bot.get_channel(channel_id)
        if channel:
            await channel.send(f"👋 **New Follower!**\n" f"**{follower}** just followed **{broadcaster}**!")

    return handler


# Export all public interfaces
__all__ = [
    "kick_webhooks_bp",
    "WebhookEventHandler",
    "register_webhook_routes",
    "create_discord_notifier",
    "verify_kick_signature",
    "require_webhook_signature",
]
