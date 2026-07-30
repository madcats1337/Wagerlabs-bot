#!/usr/bin/env python3
"""
Setup Kick Webhooks with Proper Signature Verification

This script:
1. Uses existing OAuth tokens from database
2. Generates ONE webhook secret per streamer (for all events)
3. Deletes old webhooks
4. Registers new webhooks with the secret
5. Stores webhook secrets in database for HMAC verification

IMPORTANT NOTES:
- One webhook secret per streamer (NOT per event)
- OAuth auto-refresh is handled elsewhere in the system
- Webhook secrets are unrelated to OAuth tokens
- Idempotency: Webhooks are deduplicated by message_id
- Gunicorn-safe: Database operations use transactions

Usage:
    # For a specific Discord server
    python setup_webhooks.py --discord-server-id 123456789

    # For all configured servers
    python setup_webhooks.py --all
"""

import argparse
import asyncio
import logging
import os
import secrets
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Load environment variables
load_dotenv()

# Add parent directory to path
sys.path.insert(0, os.path.dirname(__file__))

from utils.secret_settings import decrypt_secret, encrypt_secret  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

try:
    from core.kick_official_api import KickOfficialAPI
except ImportError:
    print("❌ Error: Could not import KickOfficialAPI")
    print("Make sure you're in the Kick-dicord-bot directory")
    sys.exit(1)

# Database connection
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Webhook URL — derived from BOT_PUBLIC_URL so the deployment's public host is the
# single source of truth (falls back to the production host when the env var is unset).
BOT_PUBLIC_URL = os.getenv("BOT_PUBLIC_URL", "https://bot.wagerlabs.app").rstrip("/")
WEBHOOK_URL = f"{BOT_PUBLIC_URL}/webhooks/kick"

# Events to subscribe to
WEBHOOK_EVENTS = [
    "livestream.status.updated",
    "channel.subscription.new",
    "channel.subscription.gifts",
    "channel.subscription.renewal",
    # "chat.message.sent",  # Optional - kickpython already handles this
]


def get_database_engine():
    """Create database engine"""
    if not DATABASE_URL:
        print("❌ Error: DATABASE_URL not set")
        sys.exit(1)
    return create_engine(DATABASE_URL, pool_pre_ping=True)


async def get_oauth_tokens(engine, discord_server_id: str):
    """Get OAuth tokens and broadcaster info from database

    Checks both bot_settings and kick_oauth_tokens tables for OAuth data
    """
    with engine.connect() as conn:
        # Canonical, server-scoped. The previous version tried bot_settings FIRST
        # (the legacy plaintext duplicates) and only fell back to kick_oauth_tokens
        # via an unscoped LOWER(kick_username) match. Both are gone: the tokens no
        # longer live in bot_settings, and a username match could return another
        # workspace's credentials.
        oauth_result = conn.execute(
            text(
                """
            SELECT user_id, kick_username, access_token, refresh_token
            FROM kick_oauth_tokens
            WHERE discord_server_id = :server_id
            ORDER BY updated_at DESC NULLS LAST
            LIMIT 1
        """
            ),
            {"server_id": int(discord_server_id)},
        ).fetchone()

        if not oauth_result:
            logger.error(f"No canonical Kick OAuth row for server {discord_server_id}")
            return None

        # broadcaster_user_id is ordinary (non-secret) config and stays in bot_settings.
        broadcaster = conn.execute(
            text(
                """
            SELECT value FROM bot_settings
            WHERE discord_server_id = :server_id AND key = 'kick_broadcaster_user_id'
        """
            ),
            {"server_id": discord_server_id},
        ).fetchone()

        logger.info(f"Found canonical OAuth data for: {oauth_result[1]}")
        return {
            "broadcaster_user_id": (broadcaster[0] if broadcaster and broadcaster[0] else oauth_result[0]),
            "username": oauth_result[1],
            "access_token": decrypt_secret(
                oauth_result[2], key_name="kick_oauth_tokens.access_token", row_id=oauth_result[0]
            ),
            "refresh_token": decrypt_secret(
                oauth_result[3], key_name="kick_oauth_tokens.refresh_token", row_id=oauth_result[0]
            ),
        }


async def setup_webhooks_for_server(discord_server_id: str):
    """Setup webhooks for a specific Discord server"""
    print(f"\n{'='*60}")
    print(f"Setting up webhooks for Discord server: {discord_server_id}")
    print(f"{'='*60}\n")

    engine = get_database_engine()

    # Get OAuth tokens from database
    print("🔍 Looking up OAuth tokens...")
    oauth_data = await get_oauth_tokens(engine, discord_server_id)

    if not oauth_data:
        print(f"❌ No OAuth data found for Discord server {discord_server_id}")
        print("   Run OAuth flow first to connect Kick account")
        return False

    broadcaster_user_id = oauth_data.get("broadcaster_user_id")
    username = oauth_data.get("username", "Unknown")
    access_token = oauth_data.get("access_token")
    refresh_token = oauth_data.get("refresh_token")

    if not broadcaster_user_id:
        print(f"❌ No broadcaster_user_id found for server {discord_server_id}")
        return False

    print(f"✅ Found broadcaster: {username} (ID: {broadcaster_user_id})")

    # Initialize API client
    client_id = os.getenv("KICK_CLIENT_ID")
    client_secret = os.getenv("KICK_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("❌ KICK_CLIENT_ID and KICK_CLIENT_SECRET must be set")
        return False

    api = KickOfficialAPI(client_id=client_id, client_secret=client_secret, access_token=access_token)

    try:
        # NOTE: OAuth auto-refresh is handled elsewhere in the system
        # We use the existing access token from database
        # If it's expired, the API calls will fail and user needs to re-authenticate

        if not access_token:
            print("❌ No access token available")
            print("   User needs to complete OAuth linking first")
            return False

        print(f"✅ Using access token for {username}")

        # List existing webhooks
        print("\n📋 Checking existing webhooks...")
        existing_subs = await api.get_webhook_subscriptions()

        print(f"Found {len(existing_subs)} existing webhook(s)")

        # Delete existing webhooks for this broadcaster
        deleted_count = 0
        for sub in existing_subs:
            sub_dict = sub.__dict__ if hasattr(sub, "__dict__") else sub
            sub_broadcaster = sub_dict.get("broadcaster_user_id")

            # Compare as strings or ints
            if str(sub_broadcaster) == str(broadcaster_user_id):
                sub_id = sub_dict.get("id")
                event = sub_dict.get("event")
                print(f"🗑️  Deleting old webhook: {event} (ID: {sub_id})")
                try:
                    await api.delete_webhook_subscription(sub_id)
                    deleted_count += 1
                except Exception as e:
                    print(f"   ⚠️  Failed to delete: {e}")

        if deleted_count > 0:
            print(f"✅ Deleted {deleted_count} old webhook(s)")
        elif len(existing_subs) == 0:
            print("ℹ️  No existing webhooks found")
        else:
            print(f"ℹ️  No webhooks found for broadcaster {broadcaster_user_id}")

        # Generate ONE webhook secret for this streamer (used for ALL events).
        # The value is never printed — not even a truncated prefix/suffix, which
        # still narrows a brute force and tends to end up in shared terminal logs.
        webhook_secret = secrets.token_hex(32)  # 256-bit secret
        logger.info(f"Generated a new webhook secret for {username} (value not shown)")

        # Register new webhooks - ALL use the SAME secret
        print(f"\n📨 Registering webhooks for events: {', '.join(WEBHOOK_EVENTS)}")

        registered_count = 0
        for event_type in WEBHOOK_EVENTS:
            print(f"\n🔧 Registering: {event_type}")

            try:
                response = await api.subscribe_webhook(
                    event=event_type,
                    callback_url=WEBHOOK_URL,
                    broadcaster_user_id=broadcaster_user_id,
                    secret=webhook_secret,  # Same secret for all events
                )

                print(f"   ✅ Webhook registered (API returned OK)")
                registered_count += 1

            except Exception as e:
                print(f"   ❌ Failed to register: {e}")

        # Query Kick API to get actual subscription IDs
        print(f"\n📋 Fetching subscription IDs from Kick API...")
        try:
            current_subs = await api.get_webhook_subscriptions()

            for sub in current_subs:
                sub_dict = sub if isinstance(sub, dict) else (sub.__dict__ if hasattr(sub, "__dict__") else {})
                sub_broadcaster = str(sub_dict.get("broadcaster_user_id", ""))

                if sub_broadcaster == str(broadcaster_user_id):
                    sub_id = sub_dict.get("id")
                    event = sub_dict.get("event")

                    if sub_id and event in WEBHOOK_EVENTS:
                        print(f"   📥 {event}: {sub_id}")

                        # Store in database with the real subscription ID
                        with engine.begin() as conn:
                            # First, delete any fallback entries for this event
                            fallback_id = f"{broadcaster_user_id}_{event}"
                            conn.execute(
                                text(
                                    """
                                DELETE FROM kick_webhook_subscriptions
                                WHERE subscription_id = :fallback_id
                            """
                                ),
                                {"fallback_id": fallback_id},
                            )

                            # Insert/update with real subscription ID
                            conn.execute(
                                text(
                                    """
                                INSERT INTO kick_webhook_subscriptions
                                (subscription_id, discord_server_id, broadcaster_user_id, event_type, webhook_url, webhook_secret, status)
                                VALUES (:sub_id, :server_id, :broadcaster_id, :event, :url, :secret, 'active')
                                ON CONFLICT (subscription_id)
                                DO UPDATE SET
                                    webhook_secret = EXCLUDED.webhook_secret,
                                    discord_server_id = EXCLUDED.discord_server_id,
                                    status = 'active',
                                    updated_at = NOW()
                            """
                                ),
                                {
                                    "sub_id": sub_id,
                                    "server_id": discord_server_id,
                                    "broadcaster_id": broadcaster_user_id,
                                    "event": event,
                                    "url": WEBHOOK_URL,
                                    "secret": encrypt_secret(webhook_secret),
                                },
                            )
                        print(f"   ✅ Stored in database")
        except Exception as fetch_err:
            print(f"   ⚠️  Could not fetch subscription IDs: {fetch_err}")
            print(f"   ⚠️  Webhooks registered but IDs not stored - may cause issues")

        print(f"\n{'='*60}")
        print(f"✅ Webhook setup complete for {username}!")
        print(f"   Registered {registered_count}/{len(WEBHOOK_EVENTS)} events")
        print(f"   All events use the same webhook secret")
        print(f"{'='*60}\n")

        return True

    except Exception as e:
        print(f"\n❌ Error setting up webhooks: {e}")
        import traceback

        traceback.print_exc()
        return False
    finally:
        await api.close()


async def setup_all_servers():
    """Setup webhooks for all servers with OAuth tokens"""
    engine = get_database_engine()

    print("🔍 Looking for all servers with Kick OAuth tokens...")

    with engine.connect() as conn:
        # Get all Discord servers with broadcaster_user_id
        result = conn.execute(
            text(
                """
            SELECT DISTINCT discord_server_id
            FROM bot_settings
            WHERE key = 'kick_broadcaster_user_id'
            AND value IS NOT NULL
            AND value != ''
        """
            )
        ).fetchall()

        server_ids = [row[0] for row in result]

    if not server_ids:
        print("❌ No servers found with Kick OAuth configured")
        return

    print(f"✅ Found {len(server_ids)} server(s)")

    success_count = 0
    for server_id in server_ids:
        success = await setup_webhooks_for_server(server_id)
        if success:
            success_count += 1

    print(f"\n{'='*60}")
    print(f"Setup complete: {success_count}/{len(server_ids)} servers successful")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Setup Kick webhooks with proper signature verification",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Setup webhooks for specific Discord server
  python setup_webhooks.py --discord-server-id 1234567890

  # Setup webhooks for all configured servers
  python setup_webhooks.py --all
        """,
    )

    parser.add_argument("--discord-server-id", type=str, help="Discord server ID to setup webhooks for")

    parser.add_argument("--all", action="store_true", help="Setup webhooks for all servers with OAuth configured")

    args = parser.parse_args()

    if args.all:
        asyncio.run(setup_all_servers())
    elif args.discord_server_id:
        asyncio.run(setup_webhooks_for_server(args.discord_server_id))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
