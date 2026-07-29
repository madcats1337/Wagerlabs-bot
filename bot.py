import asyncio
import base64
import contextvars  # noqa: E402
import hashlib
import hmac
import json
import logging
import os
import random
import ssl
import string
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Optional

import aiohttp
import discord
import redis
import requests
import websockets
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from sqlalchemy import create_engine, text  # type: ignore

# Centralized logging FIRST: installs the root handler + per-server context filter so
# every log line (and converted print) is tagged "[BOT] [<server name>]". Must run
# before the core/redis_subscriber imports below, which can log at import time.
from utils.clip_auth import get_clip_api_key  # noqa: E402
from utils.log_context import clear_server, server_context, set_server  # noqa: E402
from utils.logging_config import setup_logging  # noqa: E402
from utils.redis_signing import sign_payload  # noqa: E402
from utils.server_urls import get_server_base_url, get_server_public_page_url  # noqa: E402
from utils.subscription_tier import server_has_feature, upgrade_message  # noqa: E402

# Platform that the chat message currently being handled arrived on ('kick' |
# 'twitch'). Set at the top of _handle_incoming_message so send_kick_message can
# route the REPLY back to the originating platform without touching every call
# site. Defaults to 'kick' (the legacy websocket path).
_reply_platform: "contextvars.ContextVar[str]" = contextvars.ContextVar("reply_platform", default="kick")

# Disable Discord.py's default logging to reduce log spam
logging.getLogger("discord").setLevel(logging.ERROR)
logging.getLogger("discord.http").setLevel(logging.ERROR)
logging.getLogger("discord.gateway").setLevel(logging.ERROR)

setup_logging("kick_bot", log_level=os.getenv("LOG_LEVEL", "INFO"), source_tag="BOT")

from core.kick_api import USER_AGENTS, KickAPI, check_stream_live, fetch_chatroom_id  # Consolidated Kick API module

# Custom commands import
from features.custom_commands import CustomCommandsManager
from features.discord_app_commands import (
    command_invocation_id,
    defer_slash_response,
    register_wagerlabs_slash_commands,
    sync_global_slash_commands,
)
from features.games.gambling import setup_gambling
from features.games.gtb_panel import setup_gtb_panel

# Guess the Balance import
from features.games.guess_the_balance import GuessTheBalanceManager, parse_amount

# Giveaway system import
from features.giveaway.giveaway_manager import GiveawayManager, setup_giveaway_managers

# Link panel import — combined Kick+Twitch panel (per-platform buttons)
from features.linking.combined_link_panel import setup_combined_link_panel_system

# Shuffle verify panel import
from features.linking.howl_panel import setup_howl_panel_system
from features.linking.shuffle_panel import setup_shuffle_panel_system

# Timed messages import
from features.messaging.timed_messages import setup_timed_messages

# Slot call tracker import
from features.slot_requests.slot_calls import setup_slot_call_tracker
from features.slot_requests.slot_request_panel import setup_slot_panel

# Global super-admin panels for the official guild (footer/patch-notes/rules/
# features/subscription roles managed from the dashboard super-admin console).
from features.superadmin.features_panel import setup_features_panel_system
from features.superadmin.patchnotes_panel import setup_extension_patchnotes_panel_system, setup_patchnotes_panel_system
from features.superadmin.rules_panel import setup_rules_panel_system
from features.superadmin.sub_role_panel import setup_sub_role_panel_system
from raffle_system.auto_leaderboard import setup_auto_leaderboard
from raffle_system.commands import setup as setup_raffle_commands

# Raffle system imports
from raffle_system.database import (
    get_current_period,
    migrate_add_created_at_to_shuffle_wagers,
    migrate_add_panel_type_to_link_panels,
    migrate_add_platform_to_wager_tables,
    migrate_make_shuffle_links_kick_name_nullable,
    migrate_one_active_period_per_server,
    setup_raffle_database,
)
from raffle_system.gifted_sub_tracker import setup_gifted_sub_handler
from raffle_system.migrations.add_commit_reveal_to_periods import migrate_add_commit_reveal_to_periods
from raffle_system.migrations.add_platform_to_links import migrate_add_platform_to_links
from raffle_system.migrations.add_provably_fair_to_draws import migrate_add_provably_fair_to_draws
from raffle_system.migrations.platform_scope_raffle_constraints import migrate_platform_scope_raffle_constraints
from raffle_system.scheduler import setup_raffle_scheduler
from raffle_system.shuffle_tracker import setup_shuffle_tracker
from raffle_system.watchtime_converter import setup_watchtime_converter
from redis_subscriber import start_redis_subscriber

# Bot settings manager - loads settings from database with env var fallbacks
from utils.bot_settings import BotSettingsManager

# Clip service moved to Dashboard - bot now calls Dashboard API

logger = logging.getLogger(__name__)


# -------------------------
# Command checks and utils
# -------------------------
def in_guild():
    """Check if command is used in the configured guild."""

    async def predicate(ctx):
        if not DISCORD_GUILD_ID:
            return True
        return ctx.guild and ctx.guild.id == DISCORD_GUILD_ID

    return commands.check(predicate)


def has_manage_roles():
    """Check if user has manage roles permission."""

    async def predicate(ctx):
        if not ctx.guild:
            return False
        return ctx.author.guild_permissions.manage_roles

    return commands.check(predicate)


# -------------------------
# Load config
# -------------------------
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not found in environment variables")

# KICK_CHANNEL can come from env var or database (via BotSettingsManager after engine init)
# We'll set a temporary value here and load from settings later
KICK_CHANNEL = os.getenv("KICK_CHANNEL")  # May be None, will be loaded from DB later

# Optional: Hardcoded chatroom ID to bypass Cloudflare issues
# Can also be configured via dashboard
KICK_CHATROOM_ID = os.getenv("KICK_CHATROOM_ID")  # Set this on Railway to skip fetching

# Multiserver support: Settings are loaded per-guild dynamically
# No need for DISCORD_GUILD_ID - each guild has its own configuration in the database
logger.debug("✅ Multiserver mode: Settings loaded per-guild dynamically")

# Database configuration with cloud PostgreSQL support
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Redis configuration for publishing events
REDIS_URL = os.getenv("REDIS_URL", "")
redis_client = None
if REDIS_URL:
    try:
        _ru = REDIS_URL if "://" in REDIS_URL else f"redis://{REDIS_URL}"
        redis_client = redis.from_url(
            _ru,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        redis_client.ping()
        logger.debug("✅ Redis client connected for event publishing")
    except Exception as e:
        logger.warning(f"⚠️  Redis unavailable: {e}")
        redis_client = None
else:
    logger.warning("⚠️  REDIS_URL not set, events will not be published")


def publish_redis_event(channel: str, action: str, data: dict = None):
    """Publish an event to Redis for dashboard real-time updates"""
    if not redis_client:
        return False
    try:
        payload = sign_payload(
            {"action": action, "data": data or {}, "timestamp": datetime.now(timezone.utc).isoformat()}
        )
        redis_client.publish(channel, json.dumps(payload))
        return True
    except Exception as e:
        logger.info(f"Failed to publish Redis event: {e}")
        return False


if not DATABASE_URL:
    logger.warning("⚠️ WARNING: DATABASE_URL not set! Using in-memory SQLite database.")
    logger.warning("⚠️ This is fine for testing but data will be lost on restart.")
    logger.warning("⚠️ For production, set DATABASE_URL environment variable.")
    DATABASE_URL = "sqlite:///watchtime.db"

# Convert postgres:// to postgresql:// for SQLAlchemy compatibility (Heroku uses postgres://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.debug("📊 Converted database URL to use postgresql:// scheme")

logger.debug(f"📊 Using database: {DATABASE_URL.split('@')[1] if '@' in DATABASE_URL else 'SQLite (local)'}")

WATCH_INTERVAL_SECONDS = int(os.getenv("WATCH_INTERVAL_SECONDS", "60"))
ROLE_UPDATE_INTERVAL_SECONDS = int(os.getenv("ROLE_UPDATE_INTERVAL_SECONDS", "600"))
CODE_EXPIRY_MINUTES = int(os.getenv("CODE_EXPIRY_MINUTES", "10"))

# Slot call tracker configuration
# Can also be configured via dashboard
SLOT_CALLS_CHANNEL_ID = int(os.getenv("SLOT_CALLS_CHANNEL_ID")) if os.getenv("SLOT_CALLS_CHANNEL_ID") else None

# OAuth configuration
OAUTH_BASE_URL = os.getenv("OAUTH_BASE_URL", "")  # e.g., https://your-app.up.railway.app
# Ensure OAUTH_BASE_URL has https:// scheme
if OAUTH_BASE_URL and not OAUTH_BASE_URL.startswith(("http://", "https://")):
    OAUTH_BASE_URL = f"https://{OAUTH_BASE_URL}"
KICK_CLIENT_ID = os.getenv("KICK_CLIENT_ID", "")
KICK_CLIENT_SECRET = os.getenv("KICK_CLIENT_SECRET", "")  # Used for both OAuth and Kick bot
OAUTH_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "")

# Kick bot configuration (for sending chat messages)
# Requires User Access Token from OAuth flow with chat:write scope
KICK_BOT_USER_TOKEN = os.getenv("KICK_BOT_USER_TOKEN", "")  # User access token for Kick bot

# Feature flag: only use kickpython to resolve chatroom_id (no WS listener)
KICK_USE_KICKPYTHON_WS = os.getenv("KICK_USE_KICKPYTHON_WS", "false").lower() == "true"

# CRITICAL: If FLASK_SECRET_KEY is not set, OAuth will not work!
if not OAUTH_SECRET_KEY:
    logger.info("=" * 80)
    logger.warning("⚠️  CRITICAL: FLASK_SECRET_KEY environment variable is NOT SET!")
    logger.warning("⚠️  OAuth linking will NOT WORK without this key!")
    logger.warning("⚠️  Please set FLASK_SECRET_KEY in Railway environment variables.")
    logger.info("=" * 80)
else:
    logger.debug(f"[Bot] FLASK_SECRET_KEY configured: YES")


# -------------------------
# 🔒 Security: OAuth URL Signing
# -------------------------
def generate_signed_oauth_url(discord_id: int, guild_id: int = None) -> str:
    """
    Generate a cryptographically signed OAuth URL to prevent initiation spoofing.
    Only URLs generated by this bot will have valid signatures.

    Args:
        discord_id: Discord user ID
        guild_id: Discord server/guild ID (optional, defaults to 0 if not provided)
    """
    timestamp = int(datetime.now(timezone.utc).timestamp())
    # Include guild_id in signature to prevent tampering
    guild_id = guild_id or 0
    message = f"{discord_id}:{guild_id}:{timestamp}"
    signature = hmac.new(OAUTH_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    sig_encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")

    return f"{OAUTH_BASE_URL}/auth/kick?discord_id={discord_id}&guild_id={guild_id}&timestamp={timestamp}&signature={sig_encoded}"


def generate_signed_twitch_oauth_url(discord_id: int, guild_id: int = None) -> str:
    """
    Signed viewer-OAuth URL for Twitch account linking (mirrors the Kick version).
    Points at the bot OAuth server's /auth/twitch/link route. The signature scheme
    is identical, so verify_discord_id_signature validates both.
    """
    timestamp = int(datetime.now(timezone.utc).timestamp())
    guild_id = guild_id or 0
    message = f"{discord_id}:{guild_id}:{timestamp}"
    signature = hmac.new(OAUTH_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    sig_encoded = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{OAUTH_BASE_URL}/auth/twitch/link?discord_id={discord_id}&guild_id={guild_id}&timestamp={timestamp}&signature={sig_encoded}"


# URLs and Pusher config
KICK_API_BASE = "https://kick.com"
KICK_API_CHANNEL = f"{KICK_API_BASE}/api/v2/channels"
KICK_API_USER = f"{KICK_API_BASE}/api/v2/users"  # Updated to v2 API

# Browser configuration for API requests
CHROME_VERSION = "118.0.0.0"
BROWSER_CONFIG = {
    "headers": {
        "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{CHROME_VERSION} Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Ch-Ua": f'"Google Chrome";v="{CHROME_VERSION}", "Chromium";v="{CHROME_VERSION}", "Not=A?Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Connection": "keep-alive",
    }
}

PUSHER_CONFIG = {
    "key": "32cbd69e4b950bf97679",  # Updated Pusher key
    "cluster": "us2",
    "version": "8.4.0",  # Updated version
    "protocol": 7,
}
KICK_CHAT_WS = (
    f"wss://ws-{PUSHER_CONFIG['cluster']}.pusher.com/app/{PUSHER_CONFIG['key']}"  # Standard Pusher WebSocket endpoint
)

# Role thresholds in minutes
WATCHTIME_ROLES = [
    {"name": "Tier 1", "minutes": 120},
    {"name": "Tier 2", "minutes": 600},
    {"name": "Tier 3", "minutes": 2000},
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Host": "kick.com",
    "Pragma": "no-cache",
    "Referer": "https://kick.com/",
    "Origin": "https://kick.com",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

# -------------------------
# Utility functions
# -------------------------
_kick_api = None


async def get_kick_api():
    """Get or create a KickAPI instance."""
    global _kick_api
    if _kick_api is None:
        from core.kick_api import KickAPI

        _kick_api = KickAPI()
        await _kick_api.setup()
    return _kick_api


# -------------------------
# Kick WebSocket Manager for sending chat messages
# -------------------------
class KickWebSocketManager:
    """Manages persistent WebSocket connections for each guild using kickpython"""

    def __init__(self):
        self.connections = {}  # guild_id -> KickAPI instance
        self.connection_tasks = {}  # guild_id -> asyncio.Task
        self.message_queues = {}  # guild_id -> asyncio.Queue
        self.connected_channels = {}  # guild_id -> kick channel username

    async def ensure_connection(self, guild_id: int, guild_name: str) -> bool:
        """Ensure there's an active websocket connection for this guild"""
        from kickpython import KickAPI

        # Check if already connected
        if guild_id in self.connections:
            return True

        try:
            # Use bot token from environment (shared across all servers)
            bot_token = KICK_BOT_USER_TOKEN

            if not bot_token:
                logger.info(f"⚠️ KICK_BOT_USER_TOKEN not set")
                return False

            # Get channel username for websocket connection
            kick_username = None
            with engine.connect() as conn:
                # Try bot_settings first
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'kick_channel'
                    AND discord_server_id = :guild_id
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchone()

                if result and result[0]:
                    kick_username = result[0]

            if not kick_username:
                logger.info(f"⚠️ Kick channel username not configured in dashboard")
                logger.info(f"💡 Please set the Kick channel name in the dashboard Settings page")
                return False

            # Initialize kickpython API WITHOUT any credentials for chatroom_id fetch
            # kickpython will use public non-OAuth API to fetch chatroom_id (more reliable)
            # We only need auth when SENDING messages (handled in _message_sender)
            api = KickAPI()

            # DON'T set access_token or credentials here - let kickpython use public API
            # (OAuth tokens can expire, causing 401 errors during chatroom_id fetch)
            # We'll fetch fresh tokens in _message_sender when actually sending messages

            # Attach a chat message handler before connecting
            async def _on_message(msg: dict):
                try:
                    await self._handle_incoming_message(guild_id, guild_name, msg)
                except Exception as e:
                    logger.info(f"❌ Error in message handler: {e}")

            api.add_message_handler(_on_message)

            # Create message queue for this guild
            self.message_queues[guild_id] = asyncio.Queue()

            # Connect to chatroom via websocket
            logger.info(f"🔌 Connecting to Kick websocket for channel: {kick_username} with BOT token from env...")

            # Start connection in background task
            task = asyncio.create_task(self._maintain_connection(guild_id, guild_name, api, kick_username))

            self.connections[guild_id] = api
            self.connection_tasks[guild_id] = task
            self.connected_channels[guild_id] = kick_username

            logger.info(f"✅ Kick websocket connection task started")
            return True

        except Exception as e:
            logger.info(f"❌ Failed to connect websocket: {e}")
            import traceback

            traceback.print_exc()
            return False

    async def disconnect(self, guild_id: int):
        """Tear down a guild's websocket connection, tasks and queue.

        Called on guild removal so the per-guild connection + its maintain/sender
        tasks don't keep running (and reconnecting) forever after the bot leaves.
        """
        task = self.connection_tasks.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.info(f"⚠️ Error awaiting cancelled kick task for guild {guild_id}: {e}")
        api = self.connections.pop(guild_id, None)
        if api is not None:
            close_fn = getattr(api, "disconnect", None) or getattr(api, "close", None)
            if close_fn:
                try:
                    result = close_fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.info(f"⚠️ Error closing kick api for guild {guild_id}: {e}")
        self.message_queues.pop(guild_id, None)
        self.connected_channels.pop(guild_id, None)
        logger.info(f"🔌 Tore down Kick websocket for guild {guild_id}")

    async def _maintain_connection(self, guild_id: int, guild_name: str, api, kick_username: str):
        """Maintain websocket connection and process message queue"""
        set_server(guild_id, guild_name)  # tag this per-guild task's logging
        try:
            logger.debug(f"🔌 Starting message sender task in background...")

            # Start the message sender as a separate task
            sender_task = asyncio.create_task(self._message_sender(guild_id, guild_name, api))

            # DON'T set access_token before connecting!
            # kickpython will fetch chatroom_id using PUBLIC API: /api/v2/channels/{channel}/chatroom
            # We only need token for SENDING messages (done in post_chat method)

            # Reconnection loop - keep trying to stay connected
            reconnect_delay = 5
            max_reconnect_delay = 60

            while True:
                try:
                    # Now connect to chatroom (this will block until disconnected)
                    logger.debug(f"🔌 Connecting to websocket...")
                    await api.connect_to_chatroom(kick_username)

                    # Save chatroom_id to database after successful connection
                    if hasattr(api, "chatroom_id") and api.chatroom_id:
                        try:
                            with engine.connect() as conn:
                                conn.execute(
                                    text(
                                        """
                                    INSERT INTO bot_settings (key, value, discord_server_id)
                                    VALUES ('kick_chatroom_id', :chatroom_id, :guild_id)
                                    ON CONFLICT (key, discord_server_id)
                                    DO UPDATE SET value = EXCLUDED.value
                                """
                                    ),
                                    {"chatroom_id": str(api.chatroom_id), "guild_id": guild_id},
                                )
                                conn.commit()
                            logger.info(f"💾 Saved chatroom_id {api.chatroom_id} to database")
                        except Exception as e:
                            logger.info(f"⚠️ Failed to save chatroom_id: {e}")

                    # If we get here, connection was closed - try to reconnect
                    logger.info(f"⚠️ WebSocket disconnected, reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

                except asyncio.CancelledError:
                    logger.info(f"🛑 Connection task cancelled")
                    break
                except Exception as e:
                    logger.info(f"❌ WebSocket error: {e}, reconnecting in {reconnect_delay}s...")
                    await asyncio.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

            sender_task.cancel()

        except Exception as e:
            logger.info(f"❌ Websocket connection lost: {e}")
            import traceback

            traceback.print_exc()
            # Clean up
            if guild_id in self.connections:
                del self.connections[guild_id]
            if guild_id in self.connection_tasks:
                del self.connection_tasks[guild_id]
            if guild_id in self.connected_channels:
                del self.connected_channels[guild_id]

    async def _refresh_oauth_token(self, guild_id: int, guild_name: str) -> Optional[str]:
        """Refresh OAuth token for a guild using canonical kick_oauth_tokens when available."""
        try:
            # Prefer canonical token mapping via kick_channel -> kick_oauth_tokens
            with engine.connect() as conn:
                mapped = conn.execute(
                    text(
                        """
                    SELECT kot.user_id, kot.kick_username, kot.refresh_token
                    FROM bot_settings bs
                    JOIN kick_oauth_tokens kot
                      ON LOWER(kot.kick_username) = LOWER(bs.value)
                    WHERE bs.key = 'kick_channel'
                      AND bs.discord_server_id = :guild_id
                      AND kot.refresh_token IS NOT NULL
                    ORDER BY kot.updated_at DESC
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchone()

                if mapped and mapped[0] and mapped[2]:
                    user_id, kick_username, refresh_token = mapped
                    logger.info(f"🔄 Refreshing via kick_oauth_tokens for {kick_username}...")

                    if await refresh_kick_oauth_token_for_user(user_id, kick_username, refresh_token):
                        latest = conn.execute(
                            text("SELECT access_token FROM kick_oauth_tokens WHERE user_id = :uid LIMIT 1"),
                            {"uid": user_id},
                        ).fetchone()
                        if latest and latest[0]:
                            logger.info(f"✅ OAuth token refreshed successfully")
                            return latest[0]

                    logger.info(f"❌ Token refresh failed via kick_oauth_tokens")
                    return None

            # Fallback: legacy servers that only have bot_settings tokens
            import os

            import aiohttp

            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'kick_refresh_token'
                    AND discord_server_id = :guild_id
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchone()

                if not result or not result[0]:
                    logger.info(f"❌ No refresh_token found for this guild")
                    return None

                refresh_token = result[0]

            # Refresh the token
            client_id = os.getenv("KICK_CLIENT_ID")
            client_secret = os.getenv("KICK_CLIENT_SECRET")

            data = {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            }

            logger.info(f"🔄 Refreshing OAuth 2.1 token...")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://id.kick.com/oauth/token",
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10,
                ) as resp:
                    if resp.status == 200:
                        token_data = await resp.json()
                        new_access_token = token_data["access_token"]
                        new_refresh_token = token_data.get("refresh_token", refresh_token)

                        # Update both tokens in bot_settings
                        with engine.begin() as conn:
                            conn.execute(
                                text(
                                    """
                                INSERT INTO bot_settings (discord_server_id, key, value)
                                VALUES (:guild_id, 'kick_oauth_token', :token)
                                ON CONFLICT (discord_server_id, key) DO UPDATE SET value = EXCLUDED.value
                            """
                                ),
                                {"guild_id": guild_id, "token": new_access_token},
                            )

                            conn.execute(
                                text(
                                    """
                                INSERT INTO bot_settings (discord_server_id, key, value)
                                VALUES (:guild_id, 'kick_refresh_token', :token)
                                ON CONFLICT (discord_server_id, key) DO UPDATE SET value = EXCLUDED.value
                            """
                                ),
                                {"guild_id": guild_id, "token": new_refresh_token},
                            )

                        logger.info(f"✅ OAuth token refreshed successfully")
                        return new_access_token
                    else:
                        error_text = await resp.text()
                        logger.info(f"❌ Token refresh failed: HTTP {resp.status} - {error_text}")
                        return None
        except Exception as e:
            logger.info(f"❌ Error refreshing token: {e}")
            import traceback

            traceback.print_exc()
            return None

    async def _message_sender(self, guild_id: int, guild_name: str, api):
        """Process message queue and send via kickpython (multiserver support)"""
        set_server(guild_id, guild_name)  # tag this per-guild task's logging
        logger.debug(f"📨 Message sender task started!")

        try:
            # Load OAuth 2.1 token ONCE at startup for performance
            oauth_token = None
            try:
                logger.debug(f"🔍 Loading kick_oauth_token from database...")
                with engine.connect() as conn:
                    result = conn.execute(
                        text(
                            """
                        SELECT kot.access_token
                        FROM bot_settings bs
                        JOIN kick_oauth_tokens kot
                          ON LOWER(kot.kick_username) = LOWER(bs.value)
                        WHERE bs.key = 'kick_channel'
                          AND bs.discord_server_id = :guild_id
                        ORDER BY kot.updated_at DESC
                        LIMIT 1
                    """
                        ),
                        {"guild_id": guild_id},
                    ).fetchone()

                    if not result or not result[0]:
                        # Legacy fallback if guild has not been mapped into kick_oauth_tokens yet
                        result = conn.execute(
                            text(
                                """
                            SELECT value FROM bot_settings
                            WHERE key = 'kick_oauth_token'
                              AND discord_server_id = :guild_id
                            LIMIT 1
                        """
                            ),
                            {"guild_id": guild_id},
                        ).fetchone()

                    if result and result[0]:
                        oauth_token = result[0]
                        api.access_token = oauth_token
                        logger.debug(f"✅ OAuth token loaded successfully")
                    else:
                        logger.warning(f"⚠️ No kick_oauth_token found - messages will fail!")
                        # Debug: Check what keys exist
                        all_keys = conn.execute(
                            text(
                                """
                            SELECT key FROM bot_settings WHERE discord_server_id = :guild_id
                        """
                            ),
                            {"guild_id": guild_id},
                        ).fetchall()
                        logger.debug(f"🔍 Available keys: {[row[0] for row in all_keys]}")
            except Exception as e:
                logger.info(f"❌ Error loading OAuth token: {e}")
                import traceback

                traceback.print_exc()

            logger.debug(f"🔄 Starting message processing loop...")

            while True:
                try:
                    # Wait for message from queue (timeout allows checking for cancellation)
                    try:
                        message, channel_id = await asyncio.wait_for(self.message_queues[guild_id].get(), timeout=5.0)
                    except asyncio.TimeoutError:
                        # No message in queue, keep waiting
                        continue

                    # Reload token from database before each send (in case proactive refresh updated it)
                    try:
                        with engine.connect() as conn:
                            result = conn.execute(
                                text(
                                    """
                                SELECT kot.access_token
                                FROM bot_settings bs
                                JOIN kick_oauth_tokens kot
                                  ON LOWER(kot.kick_username) = LOWER(bs.value)
                                WHERE bs.key = 'kick_channel'
                                  AND bs.discord_server_id = :guild_id
                                ORDER BY kot.updated_at DESC
                                LIMIT 1
                            """
                                ),
                                {"guild_id": guild_id},
                            ).fetchone()

                            if not result or not result[0]:
                                result = conn.execute(
                                    text(
                                        """
                                    SELECT value FROM bot_settings
                                    WHERE key = 'kick_oauth_token'
                                      AND discord_server_id = :guild_id
                                    LIMIT 1
                                """
                                    ),
                                    {"guild_id": guild_id},
                                ).fetchone()

                            if result and result[0]:
                                api.access_token = result[0]
                    except Exception as e:
                        logger.info(f"⚠️ Error reloading token: {e}")

                    # Debug: Log what we're about to send
                    logger.info(f"📤 Preparing to send message:")
                    logger.info(f"   Message: {message[:100]}...")
                    logger.info(f"   Channel ID: {channel_id}")
                    logger.info(f"   Token loaded: {'YES' if api.access_token else 'NO'}")

                    # Send via direct HTTP request to Kick API
                    import aiohttp

                    headers = {
                        "Authorization": f"Bearer {api.access_token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "LeleBot/1.0",
                    }

                    # Use bot mode - broadcaster_user_id not needed, message sent to channel attached to token
                    payload = {"content": message, "type": "bot"}  # bot mode instead of 'message'

                    url = "https://api.kick.com/public/v1/chat"

                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
                            response_text = await resp.text()
                            logger.info(f"📡 Kick API Response: HTTP {resp.status}")
                            logger.info(f"📡 Response body: {response_text[:500]}")

                            if resp.status in [200, 201]:
                                logger.info(f"✅ Message sent successfully!")
                            elif resp.status == 401:
                                raise Exception(f"HTTP 401 Unauthorized - token may have expired")
                            else:
                                raise Exception(f"HTTP {resp.status}: {response_text}")

                except asyncio.CancelledError:
                    logger.info(f"🛑 Message sender cancelled")
                    break
                except Exception as send_error:
                    error_str = str(send_error)
                    logger.info(f"⚠️ Send failed: {error_str}")

                    # If sending fails with 401, try refreshing token
                    if "401" in error_str or "Unauthorized" in error_str:
                        logger.info(f"🔄 Token expired, attempting refresh...")
                        refreshed = await self._refresh_oauth_token(guild_id, guild_name)
                        if refreshed:
                            api.access_token = refreshed
                            logger.info(f"🔄 Retrying with refreshed token...")

                            # Retry with refreshed token
                            import aiohttp

                            headers = {
                                "Authorization": f"Bearer {api.access_token}",
                                "Accept": "application/json",
                                "Content-Type": "application/json",
                                "User-Agent": "LeleBot/1.0",
                            }
                            payload = {"content": message, "type": "bot"}
                            url = "https://api.kick.com/public/v1/chat"

                            async with aiohttp.ClientSession() as session:
                                async with session.post(url, headers=headers, json=payload, timeout=10) as resp:
                                    response_text = await resp.text()
                                    logger.info(f"📡 Retry response: HTTP {resp.status} - {response_text[:200]}")
                                    if resp.status not in [200, 201]:
                                        # Try streamer OAuth as final fallback
                                        logger.info(f"⚠️ Trying streamer OAuth as fallback...")
                                        from utils.kick_oauth import get_kick_token_for_server

                                        token_data = get_kick_token_for_server(engine, guild_id)
                                        if token_data and token_data.get("access_token"):
                                            api.access_token = token_data["access_token"]
                                            headers["Authorization"] = f"Bearer {api.access_token}"
                                            async with session.post(
                                                url, headers=headers, json=payload, timeout=10
                                            ) as fallback_resp:
                                                fallback_text = await fallback_resp.text()
                                                logger.info(
                                                    f"📡 Streamer OAuth: HTTP {fallback_resp.status} - {fallback_text[:200]}"
                                                )
                        else:
                            logger.info(f"❌ Token refresh failed")
                    else:
                        import traceback

                        traceback.print_exc()

        except asyncio.CancelledError:
            logger.info(f"🛑 Message sender task cancelled")
        except Exception as e:
            logger.info(f"❌ FATAL: Message sender crashed: {e}")
            import traceback

            traceback.print_exc()

    async def send_message(self, message: str, guild_id: int, guild_name: str) -> bool:
        """Queue a message to be sent via websocket"""
        try:
            # Ensure connection exists
            if not await self.ensure_connection(guild_id, guild_name):
                return False

            # Get API instance
            api = self.connections.get(guild_id)
            if not api:
                logger.info(f"⚠️ No API instance found")
                return False

            # Get chatroom ID - prioritize in-memory value from kickpython
            chatroom_id = None

            # PRIORITY 1: Get from kickpython's in-memory chatroom_id (most reliable)
            if hasattr(api, "chatroom_id") and api.chatroom_id:
                chatroom_id = int(api.chatroom_id)
                logger.info(f"📋 Using chatroom_id from kickpython: {chatroom_id}")

            # PRIORITY 2: Fall back to database (for reconnections)
            if not chatroom_id:
                with engine.connect() as conn:
                    result = conn.execute(
                        text(
                            """
                        SELECT value FROM bot_settings
                        WHERE key = 'kick_chatroom_id'
                        AND discord_server_id = :guild_id
                        LIMIT 1
                    """
                        ),
                        {"guild_id": guild_id},
                    ).fetchone()

                    if result and result[0]:
                        try:
                            chatroom_id = int(result[0])
                            logger.info(f"📋 Using chatroom_id from database: {chatroom_id}")

                            # HOTFIX: 152837 is broadcaster_user_id, not chatroom_id!
                            if chatroom_id == 152837:
                                logger.info(f"⚠️ Invalid chatroom_id (152837 is broadcaster_user_id, not chatroom_id)")
                                chatroom_id = None
                        except (ValueError, TypeError):
                            logger.info(f"⚠️ Invalid chatroom_id format")
                            chatroom_id = None

            if not chatroom_id:
                logger.info(f"⚠️ Chatroom ID not configured")
                return False

            # Add to queue
            await self.message_queues[guild_id].put((message, chatroom_id))
            logger.info(f"📤 Queued message for websocket (chatroom: {chatroom_id})")
            return True

        except Exception as e:
            logger.info(f"❌ Failed to queue message: {e}")
            return False

    async def _handle_incoming_message(self, guild_id: int, guild_name: str, msg: dict):
        """Handle inbound chat messages from kickpython websocket."""
        # Tag every chat message's logging with the server. This callback may run in
        # a different asyncio Task than the connection loop, so set context here too.
        set_server(guild_id, guild_name)
        try:
            username = msg.get("sender_username") or msg.get("username") or "unknown"
            # The chatter's REAL name on their platform, for @-mentions in replies.
            # On Twitch dual-link, `username` is canonicalized for identity/crediting,
            # but replies must address the actual chatter (e.g. @MadcatsTV). Falls
            # back to `username` on the Kick path (no remapping there).
            display_username = msg.get("display_username") or username
            content = msg.get("content") or ""
            chat_id = msg.get("chat_id") or msg.get("id")

            # Route any reply this handler produces back to the platform the message
            # arrived on (Twitch chat → Twitch, Kick chat → Kick). send_kick_message
            # reads this contextvar; defaults to 'kick'.
            platform = (msg.get("_platform") or "kick").lower()
            _reply_platform.set(platform)

            # Ignore the bot's OWN messages so its replies don't loop back in as new
            # input. On Twitch the chatter id equals our bot account id; on Kick the
            # username matches the bot account login.
            twitch_bot_id = os.getenv("TWITCH_BOT_USER_ID")
            if platform == "twitch" and twitch_bot_id:
                chatter_id = str((msg.get("user") or {}).get("id") or "")
                if chatter_id and chatter_id == str(twitch_bot_id):
                    return

            # Debug: Log every incoming message
            logger.info(f"💬 Received {platform} message: {username}: {content[:100]}")

            # Update watchtime tracking (per-guild)
            now = datetime.now(timezone.utc)
            username_lower = username.lower()

            # Initialize dicts if missing
            if "active_viewers_by_guild" not in globals():
                globals()["active_viewers_by_guild"] = {}
            if "recent_chatters_by_guild" not in globals():
                globals()["recent_chatters_by_guild"] = {}
            if "last_chat_activity_by_guild" not in globals():
                globals()["last_chat_activity_by_guild"] = {}

            # Update last chat activity
            last_chat_activity_by_guild[guild_id] = now

            # Update active viewers (for watchtime tracking)
            guild_active_viewers = active_viewers_by_guild.get(guild_id, {})
            guild_active_viewers[username_lower] = now
            active_viewers_by_guild[guild_id] = guild_active_viewers

            # Update recent chatters (for stream-live detection)
            guild_chatters = recent_chatters_by_guild.get(guild_id, {})
            guild_chatters[username_lower] = now
            recent_chatters_by_guild[guild_id] = guild_chatters

            # Publish to Redis for dashboard (optional)
            publish_redis_event(
                channel="kick_chat",
                action="message",
                data={
                    "guild_id": guild_id,
                    "channel": self.connected_channels.get(guild_id),
                    "username": username,
                    "content": content,
                    "chat_id": chat_id,
                    "timestamp": now.isoformat(),
                },
            )

            # 🎁 GIVEAWAY: Track messages for keyword and active chatter detection
            if guild_id in giveaway_managers:
                giveaway_manager = giveaway_managers[guild_id]
                if giveaway_manager.active_giveaway:
                    try:
                        entry_method = giveaway_manager.active_giveaway["entry_method"]
                        giveaway_id = giveaway_manager.active_giveaway["id"]
                        entry_added = False

                        # Keyword detection - exact match only (no extra text)
                        if entry_method == "keyword":
                            keyword = giveaway_manager.active_giveaway.get("keyword", "").lower()
                            if keyword and content.strip().lower() == keyword:
                                entry_added = await giveaway_manager.add_entry(
                                    username, entry_method="keyword", platform=platform, display_name=display_username
                                )
                                if entry_added:
                                    logger.info(f"🎁 Giveaway entry added: {username} (keyword: {keyword})")

                        # Active chatter tracking
                        elif entry_method == "active_chatter":
                            entry_added = await giveaway_manager.track_message(
                                username, content, platform=platform, display_name=display_username
                            )

                        # Notify the dashboard admin page so its entries list updates
                        # live (session-scoped SSE forwards this by discord_server_id).
                        if entry_added:
                            publish_redis_event(
                                channel="dashboard:giveaway",
                                action="giveaway_entry",
                                data={
                                    "discord_server_id": guild_id,
                                    "giveaway_id": giveaway_id,
                                    "kick_username": username,
                                },
                            )
                    except Exception as e:
                        logger.info(f"⚠️ Giveaway tracking error: {e}")

            # Process commands from Kick chat
            content_stripped = content.strip()
            logger.info(f"🔍 Processing message: '{content_stripped[:50]}...'")

            # Custom commands (from dashboard) — Tier 2+ feature.
            if (
                hasattr(bot, "custom_commands_managers")
                and guild_id in bot.custom_commands_managers
                and server_has_feature(engine, guild_id, "commands")
            ):
                try:
                    commands_manager = bot.custom_commands_managers[guild_id]
                    original_guild_id = getattr(commands_manager, "discord_server_id", None)
                    try:
                        commands_manager.discord_server_id = guild_id
                        commands_manager.send_message_callback = lambda msg: send_kick_message(msg, guild_id=guild_id)
                        handled = await commands_manager.handle_message(
                            content,
                            username,
                            platform=platform,
                            display_name=display_username,
                        )
                    finally:
                        commands_manager.discord_server_id = original_guild_id
                    if handled:
                        logger.info(f"✅ Custom command handled")
                        return  # Command was handled, don't process further
                except Exception as e:
                    logger.info(f"⚠️ Custom command error: {e}")

            # !points command
            if content_stripped.lower() == "!points":
                logger.info(f"💰 Processing !points command from {username}")
                # Point shop is a Tier 2+ feature.
                if not server_has_feature(engine, guild_id, "point_shop"):
                    logger.info(f"🔒 !points blocked: server tier lacks point_shop")
                    await send_kick_message(upgrade_message(username, "point_shop"), guild_id=guild_id)
                    return
                try:
                    with engine.connect() as conn:
                        # Points are shared across a person's linked platforms and
                        # accrue under a single canonical username. Resolve the
                        # chatter's canonical account (via their discord_id) so a
                        # request from their non-canonical platform still reads the
                        # right balance; fall back to their own handle if unlinked.
                        canonical = username.lower()
                        did_row = conn.execute(
                            text(
                                """
                            SELECT discord_id FROM links
                            WHERE LOWER(kick_name) = :u AND discord_server_id = :guild_id
                              AND discord_id IS NOT NULL
                            LIMIT 1
                        """
                            ),
                            {"u": username.lower(), "guild_id": guild_id},
                        ).fetchone()
                        if did_row:
                            names = conn.execute(
                                text(
                                    """
                                SELECT LOWER(kick_name) FROM links
                                WHERE discord_id = :d AND discord_server_id = :guild_id
                                  AND kick_name IS NOT NULL
                            """
                                ),
                                {"d": did_row[0], "guild_id": guild_id},
                            ).fetchall()
                            handles = [r[0] for r in names if r[0]]
                            if handles:
                                canonical = min(handles)

                        result = conn.execute(
                            text(
                                """
                            SELECT points FROM user_points
                            WHERE LOWER(kick_username) = :username AND discord_server_id = :guild_id
                        """
                            ),
                            {"username": canonical, "guild_id": guild_id},
                        ).fetchone()

                        if result and result[0] is not None:
                            points_balance = int(result[0])
                            await send_kick_message(
                                f"@{display_username}, You currently have {points_balance:,} points.",
                                guild_id=guild_id,
                            )
                        else:
                            await send_kick_message(
                                f"@{display_username}, You currently have 0 points. Start watching to earn points!",
                                guild_id=guild_id,
                            )
                except Exception as e:
                    logger.info(f"❌ Error fetching points for {username}: {e}")
                    await send_kick_message(
                        f"@{display_username}, Unable to retrieve points balance at this time.", guild_id=guild_id
                    )

            # !current / !slot — what slot is the streamer playing right now?
            # Reads the latest current_slot_history row (written by the browser
            # extension's dashboard push on every game-page change). Ungated:
            # the write path is already tier-gated, so servers without the
            # extension feature simply have no rows.
            elif content_stripped.lower() in ("!current", "!slot"):
                logger.info(f"🎰 Processing !current command from {username}")
                try:
                    with engine.connect() as conn:
                        result = conn.execute(
                            text(
                                """
                            SELECT slot_name, provider FROM current_slot_history
                            WHERE discord_server_id = :guild_id
                            ORDER BY started_at DESC, id DESC
                            LIMIT 1
                        """
                            ),
                            {"guild_id": guild_id},
                        ).fetchone()

                    if result and result[0]:
                        slot_name, provider = result[0], result[1]
                        suffix = f" ({provider})" if provider else ""
                        await send_kick_message(
                            f"@{display_username} Currently playing: {slot_name}{suffix}",
                            guild_id=guild_id,
                        )
                    else:
                        await send_kick_message(
                            f"@{display_username} No slot tracked yet.",
                            guild_id=guild_id,
                        )
                except Exception as e:
                    logger.info(f"❌ Error fetching current slot: {e}")

            # !call / !sr commands (slot requests)
            elif content_stripped.startswith(("!call", "!sr")):
                logger.info(f"🎰 Detected slot command: {content_stripped[:50]}")
                # Slot requests are a Tier 1 (free) feature — gated for
                # uniformity / future suspended-server handling.
                if not server_has_feature(engine, guild_id, "slot_requests"):
                    logger.info(f"🔒 !sr blocked: server tier lacks slot_requests")
                    await send_kick_message(upgrade_message(username, "slot_requests"), guild_id=guild_id)
                    return
                # Anti-multi-account: require a link on the chatter's own platform when enabled.
                if not require_link_ok(guild_id, username, platform):
                    await send_kick_message(
                        f"@{display_username} Link your {platform.capitalize()} account with the Link Account panel in Discord to request slots.",
                        guild_id=guild_id,
                    )
                    return
                logger.info(f"🔍 Has slot_trackers: {hasattr(bot, 'slot_trackers')}")

                # Use guild-specific tracker (check both naming conventions).
                # Look up tolerant of int/str key type so a guild_id arriving as a
                # different type (e.g. from the Twitch redis path) still matches.
                def _tracker_for(registry):
                    if not isinstance(registry, dict):
                        return None
                    if guild_id in registry:
                        return registry[guild_id]
                    return registry.get(int(guild_id), registry.get(str(guild_id)))

                tracker = None
                _byg = getattr(bot, "slot_call_trackers_by_guild", None)
                _alt = getattr(bot, "slot_trackers", None)
                if _tracker_for(_byg) is not None:
                    tracker = _tracker_for(_byg)
                    logger.info(f"✅ Found guild-specific tracker (slot_call_trackers_by_guild)")
                elif _tracker_for(_alt) is not None:
                    tracker = _tracker_for(_alt)
                    logger.info(f"✅ Found guild-specific tracker (slot_trackers)")
                elif hasattr(bot, "slot_call_tracker"):
                    tracker = bot.slot_call_tracker
                    logger.info(f"⚠️ Using fallback global tracker")

                if tracker:
                    logger.info(f"🔍 Slot tracker enabled: {tracker.enabled}")
                    logger.info(f"🔍 Slot tracker guild_id: {getattr(tracker, 'discord_server_id', 'NOT SET')}")
                    # Check if slot requests are enabled first
                    if not tracker.enabled:
                        logger.info(f"❌ Slot requests disabled, sending rejection message")
                        await send_kick_message(
                            f"@{display_username} Slot requests are not open at the moment.", guild_id=guild_id
                        )
                    else:
                        logger.info(f"✅ Slot requests enabled, processing command")
                        slot_call = (
                            content_stripped[5:].strip()[:200]
                            if content_stripped.startswith("!call")
                            else content_stripped[3:].strip()[:200]
                        )
                        if slot_call:
                            # Fetch avatar from Kick API (same method as giveaway entries - proven to work!)
                            avatar_url = None
                            try:
                                from core.kick_api import get_channel_info

                                channel_data = await get_channel_info(username)
                                if channel_data and "user" in channel_data:
                                    avatar_url = channel_data["user"].get("profile_pic")
                                    if avatar_url:
                                        logger.info(f"✅ Fetched avatar from Kick API for {username}: {avatar_url}")
                                    else:
                                        logger.warning(f"⚠️ No profile_pic in Kick API response for {username}")
                                else:
                                    logger.warning(f"⚠️ No user data in Kick API response for {username}")
                            except Exception as e:
                                logger.error(f"❌ Failed to fetch avatar for {username}: {e}")

                            original_guild_id = getattr(tracker, "discord_server_id", None)
                            tracker.discord_server_id = guild_id
                            try:
                                await tracker.handle_slot_call(
                                    username,
                                    slot_call,
                                    avatar_url=avatar_url,
                                    platform=platform,
                                    display_username=display_username,
                                )
                                logger.info(f"✅ Slot call processed: {username} - {slot_call}")
                            finally:
                                if original_guild_id:
                                    tracker.discord_server_id = original_guild_id
                        else:
                            await send_kick_message(f"@{display_username} Please specify a slot!", guild_id=guild_id)
                else:
                    logger.info(f"❌ No slot tracker found for this guild")

            # !gtb command
            elif content_stripped.lower().startswith("!gtb"):
                logger.info(f"🎲 Processing !gtb command from {username}")
                # Guess the Balance is a Tier 1 (free) feature — gated for
                # uniformity / future suspended-server handling.
                if not server_has_feature(engine, guild_id, "gtb"):
                    logger.info(f"🔒 !gtb blocked: server tier lacks gtb")
                    await send_kick_message(upgrade_message(username, "gtb"), guild_id=guild_id)
                    return
                # Anti-multi-account: require a link on the chatter's own platform when enabled.
                if not require_link_ok(guild_id, username, platform):
                    await send_kick_message(
                        f"@{display_username} Link your {platform.capitalize()} account with the Link Account panel in Discord to guess.",
                        guild_id=guild_id,
                    )
                    return
                parts = content_stripped.split(maxsplit=1)
                if len(parts) == 2:
                    amount = parse_amount(parts[1])
                    if amount is not None:
                        # Use per-guild GTB manager
                        gtb_mgr = (
                            bot.gtb_managers_by_guild.get(guild_id) if hasattr(bot, "gtb_managers_by_guild") else None
                        )
                        if gtb_mgr:
                            success, message = gtb_mgr.add_guess(username, amount, display_name=display_username)
                            response = f"@{display_username} {message}" + (" Good luck! 🎰" if success else "")
                            await send_kick_message(response, guild_id=guild_id)
                            logger.info(f"✅ GTB guess processed: {username} - ${amount}")
                        else:
                            await send_kick_message(
                                f"@{display_username} GTB system not initialized", guild_id=guild_id
                            )
                            logger.info(f"❌ GTB system not initialized")
                    else:
                        await send_kick_message(
                            f"@{display_username} Invalid amount. Use: !gtb <amount>", guild_id=guild_id
                        )
                else:
                    await send_kick_message(
                        f"@{display_username} Usage: !gtb <amount> (e.g., !gtb 1234.56)", guild_id=guild_id
                    )

            # !clip command
            elif content_stripped.lower().startswith("!clip"):
                clip_title = content_stripped[5:].strip()  # Remove "!clip" and trim
                if not clip_title:
                    # No title provided - generate default
                    timestamp = datetime.now().strftime("%b %d, %Y %H:%M")
                    clip_title = f"Clip by {username} - {timestamp}"

                logger.info(f"🎬 {username} requested clip: {clip_title}")

                # Create clip via Dashboard API (background task)
                async def create_clip_background(user: str, title: str):
                    try:
                        # Get kick channel
                        kick_channel = self.connected_channels.get(guild_id)
                        if not kick_channel:
                            logger.info(f"[Clip] ❌ No channel configured")
                            return

                        # Check if stream is live FIRST (async function returning bool)
                        try:
                            is_live = await check_stream_live(kick_channel)
                            if not is_live:
                                logger.info(f"[Clip] ❌ Stream is offline")
                                await send_kick_message(
                                    f"@{user} Stream is not live! Can't create clip when offline.", guild_id=guild_id
                                )
                                return
                        except Exception as live_check_err:
                            logger.info(f"[Clip] ⚠️ Could not verify stream status: {live_check_err}")
                            # Continue anyway - dashboard will verify

                        # Get settings from bot_settings
                        dashboard_url = None
                        api_key = None
                        clip_duration = 30

                        with engine.connect() as conn:
                            settings_result = conn.execute(
                                text(
                                    """
                                SELECT key, value FROM bot_settings
                                WHERE discord_server_id = :guild_id
                                AND key IN ('dashboard_url', 'bot_api_key', 'clip_duration')
                            """
                                ),
                                {"guild_id": guild_id},
                            ).fetchall()

                            for key, value in settings_result:
                                if key == "dashboard_url":
                                    dashboard_url = value
                                elif key == "bot_api_key":
                                    api_key = value
                                elif key == "clip_duration":
                                    clip_duration = int(value) if value else 30

                        # Prefer the derived per-server base (servers.subdomain +
                        # public domain); the stored dashboard_url is only a
                        # fallback and may be stale (pre-rebrand host).
                        dashboard_url = get_server_base_url(engine, guild_id) or dashboard_url

                        # Env-controlled system secret takes precedence over the legacy
                        # per-server bot_settings value.
                        api_key = get_clip_api_key(api_key)

                        if not dashboard_url or not api_key:
                            logger.info(f"[Clip] ❌ Dashboard URL or API key not configured")
                            await send_kick_message(f"@{user} Clip service not configured!", guild_id=guild_id)
                            return

                        # Call Dashboard API
                        import aiohttp

                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                f"{dashboard_url}/api/clips/create",
                                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                                json={
                                    "channel": kick_channel,
                                    "duration": clip_duration,
                                    "username": user,
                                    "title": title,
                                    "discord_server_id": guild_id,
                                },
                                timeout=10,
                            ) as resp:
                                if resp.status == 200:
                                    result = await resp.json()
                                    logger.info(f"[Clip] ✅ Clip created for {user}")
                                    clip_url = result.get("clip_url", "")
                                    await send_kick_message(f"@{user} Clip created! 🎬 {clip_url}", guild_id=guild_id)
                                else:
                                    error_data = await resp.json()
                                    error_msg = error_data.get("message", "Unknown error")
                                    logger.info(f"[Clip] ❌ Failed: HTTP {resp.status} - {error_msg}")

                                    # Provide helpful error messages
                                    if "no_buffer" in error_msg or "not_recording" in error_msg:
                                        await send_kick_message(
                                            f"@{user} Clip buffer is starting up, try again in 30 seconds!",
                                            guild_id=guild_id,
                                        )
                                    elif "no_segments" in error_msg:
                                        await send_kick_message(
                                            f"@{user} Buffer still filling, try again in a few seconds!",
                                            guild_id=guild_id,
                                        )
                                    else:
                                        await send_kick_message(f"@{user} Clip failed: {error_msg}", guild_id=guild_id)
                    except Exception as e:
                        logger.info(f"[Clip] ❌ Error: {e}")
                        await send_kick_message(f"@{user} Clip error: {str(e)}", guild_id=guild_id)

                asyncio.create_task(create_clip_background(username, clip_title))

            # !fair / !pf — how to verify a provably-fair draw. Links the
            # server's public /provably-fair page (subdomain host, or the apex
            # with ?server=<slug> for subdomain-less workspaces).
            elif content_stripped.lower() in ("!fair", "!pf"):
                logger.info(f"🎲 Processing !fair command from {username}")
                try:
                    fair_url = get_server_public_page_url(engine, guild_id, "/provably-fair")
                    await send_kick_message(
                        f"@{username} All draws are provably fair (commit-reveal + SHA-256). "
                        f"Verify any result yourself: {fair_url}",
                        guild_id=guild_id,
                    )
                except Exception as e:
                    logger.error(f"Error processing !fair command: {e}")

            # !tickets command
            elif content_stripped.lower() == "!tickets":
                logger.info(f"🎟️ Processing !tickets command from {username}")
                try:
                    with engine.connect() as conn:
                        # Check if user is linked
                        link_result = conn.execute(
                            text(
                                """
                            SELECT discord_id FROM links
                            WHERE LOWER(kick_name) = :username AND discord_server_id = :guild_id
                        """
                            ),
                            {"username": username.lower(), "guild_id": guild_id},
                        ).fetchone()

                        if not link_result:
                            await send_kick_message(
                                f"@{username}, You need to link your account first! Use the Link Account panel in Discord.",
                                guild_id=guild_id,
                            )
                        else:
                            discord_id = link_result[0]

                            # Get current raffle period
                            period_result = conn.execute(
                                text(
                                    """
                                SELECT id FROM raffle_periods
                                WHERE status = 'active' AND discord_server_id = :guild_id
                                LIMIT 1
                            """
                                ),
                                {"guild_id": guild_id},
                            ).fetchone()

                            if not period_result:
                                await send_kick_message(
                                    f"@{username}, No active raffle period right now.", guild_id=guild_id
                                )
                            else:
                                period_id = period_result[0]

                                # Get ticket balance
                                ticket_result = conn.execute(
                                    text(
                                        """
                                    SELECT
                                        total_tickets,
                                        watchtime_tickets,
                                        gifted_sub_tickets,
                                        shuffle_wager_tickets,
                                        bonus_tickets
                                    FROM raffle_tickets
                                    WHERE period_id = :period_id
                                    AND discord_id = :discord_id
                                    AND discord_server_id = :guild_id
                                """
                                    ),
                                    {"period_id": period_id, "discord_id": discord_id, "guild_id": guild_id},
                                ).fetchone()

                                if not ticket_result or ticket_result[0] == 0:
                                    await send_kick_message(
                                        f"@{username}, You currently have 0 raffle tickets. Watch streams, gift subs, or wager to earn tickets!",
                                        guild_id=guild_id,
                                    )
                                else:
                                    total_tickets = ticket_result[0] or 0
                                    watchtime = ticket_result[1] or 0
                                    gifted_subs = ticket_result[2] or 0
                                    wager = ticket_result[3] or 0
                                    bonus = ticket_result[4] or 0

                                    # Calculate win probability
                                    total_pool = conn.execute(
                                        text(
                                            """
                                        SELECT COALESCE(SUM(total_tickets), 0)
                                        FROM raffle_tickets
                                        WHERE period_id = :period_id AND discord_server_id = :guild_id
                                    """
                                        ),
                                        {"period_id": period_id, "guild_id": guild_id},
                                    ).scalar()

                                    win_prob = (total_tickets / total_pool * 100) if total_pool > 0 else 0

                                    await send_kick_message(
                                        f"@{username}, You have {int(total_tickets):,} tickets "
                                        f"(Watch: {int(watchtime)} | Subs: {int(gifted_subs)} | Wager: {int(wager)} | Bonus: {int(bonus)}) "
                                        f"Win chance: {win_prob:.2f}% 🎟️",
                                        guild_id=guild_id,
                                    )
                                    logger.info(f"✅ Sent ticket balance to {username}")
                except Exception as e:
                    logger.info(f"❌ Error fetching tickets for {username}: {e}")
                    await send_kick_message(
                        f"@{username}, Unable to retrieve ticket balance at this time.", guild_id=guild_id
                    )

        except Exception as e:
            logger.info(f"❌ Error handling incoming message: {e}")


# Global websocket manager instance
kick_ws_manager = KickWebSocketManager()


async def send_kick_message(message: str, guild_id: int = None) -> bool:
    """
    Send a message to the originating platform's chat (for COMMAND REPLIES).

    Despite the name (kept for back-compat with ~30 call sites in the chat
    handler), this routes the reply to whichever platform the message being
    handled arrived on, via the _reply_platform contextvar. Twitch → Helix send;
    Kick → kickpython queue (the original behavior, and the default).

    NOTE: announcements (slot picks, GTB, winners, timed messages) must NOT use
    this — they run outside the chat handler where the contextvar may carry a
    stale value. They use send_stream_message, which calls the raw Kick sender
    directly so it always reaches Kick.

    Returns True if message queued/sent successfully, False otherwise.
    """
    # Reply on the platform the current message came from.
    if _reply_platform.get() == "twitch":
        return await send_twitch_message(message, guild_id=guild_id)
    return await _send_kick_message_raw(message, guild_id=guild_id)


async def _send_kick_message_raw(message: str, guild_id: int = None) -> bool:
    """Send a message to KICK chat unconditionally (no platform contextvar).
    The actual kickpython-queue delivery used by both command replies (via
    send_kick_message) and announcements (via send_stream_message)."""
    # Get guild name for logging
    guild_name = "Unknown"
    if guild_id:
        guild = bot.get_guild(guild_id)
        guild_name = guild.name if guild else str(guild_id)

    try:
        # Check if kickpython connection exists for this guild, auto-connect if not
        if guild_id not in kick_ws_manager.message_queues:
            logger.info(f"🔌 No kickpython connection - attempting auto-connect...")
            logger.info(f"🔍 guild_id={guild_id}")
            success = await kick_ws_manager.ensure_connection(guild_id, guild_name)
            if not success:
                logger.info(f"❌ Failed to auto-connect kickpython")
                return False
            logger.info(f"✅ Auto-connected kickpython successfully")

        # Get broadcaster_user_id (channel_id for kickpython)
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT value FROM bot_settings
                WHERE key = 'kick_broadcaster_user_id'
                AND discord_server_id = :guild_id
                LIMIT 1
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()

            if not result or not result[0]:
                logger.info(f"⚠️ broadcaster_user_id not configured - use Sync button in dashboard")
                return False

            channel_id = int(result[0])

        # Queue message for kickpython to send
        logger.info(f"📨 Queueing message via kickpython: {message[:50]}...")
        await kick_ws_manager.message_queues[guild_id].put((message, channel_id))
        logger.info(f"✅ Message queued")
        return True

    except Exception as e:
        logger.info(f"❌ Failed to queue message: {e}")
        import traceback

        traceback.print_exc()
        return False


async def send_twitch_message(message: str, guild_id: int = None) -> bool:
    """
    Send a message to Twitch chat via the Helix Send Chat Message API.

    IMPORTANT — chatbot badge: to receive the Twitch "chatbot" badge, the send MUST
    use an APP ACCESS TOKEN (not the bot's user token), per
    https://dev.twitch.tv/docs/chat/#chatbot-badge-and-chat-identity :
      - must use the Send Chat Message API
      - must use an App Access Token
      - the broadcaster must have granted channel:bot (or the bot is a mod)
      - the bot account must NOT be the broadcaster
    The bot account must still have authorized the app once (user:write:chat +
    user:bot) — that authorization is a prerequisite; the SEND itself is app-token.

    Resolves the broadcaster id from bot_settings(twitch_broadcaster_user_id) and
    the bot sender id from TWITCH_BOT_USER_ID. Returns True on success.
    """
    try:
        import os as _os

        from sqlalchemy import text as _text

        bot_user_id = _os.getenv("TWITCH_BOT_USER_ID")
        if not bot_user_id:
            logger.info("[Twitch Send] TWITCH_BOT_USER_ID not set")
            return False

        bot_uid = int(bot_user_id) if str(bot_user_id).isdigit() else 0
        with engine.connect() as conn:
            row = conn.execute(
                _text(
                    "SELECT value FROM bot_settings WHERE key = 'twitch_broadcaster_user_id' AND discord_server_id = :g"
                ),
                {"g": guild_id},
            ).fetchone()
            broadcaster_id = row[0] if row else None
            # The bot account must have authorized once (prerequisite for the badge
            # + chat grants). We don't SEND with this token, but its presence tells
            # us the bot is set up. Global row → discord_server_id = 0.
            authorized = conn.execute(
                _text("SELECT 1 FROM twitch_oauth_tokens WHERE user_id = :u AND discord_server_id = 0 LIMIT 1"),
                {"u": bot_uid},
            ).fetchone()

        if not broadcaster_id:
            logger.info("[Twitch Send] No twitch_broadcaster_user_id for this server — cannot send")
            return False
        if not authorized:
            logger.info("[Twitch Send] Bot account not authorized (no token row) — authorize it in SuperAdmin")
            return False

        from core.twitch_api import TwitchAPI

        # Send with an APP ACCESS TOKEN so the message earns the chatbot badge.
        api = TwitchAPI()
        try:
            tokens = await api.get_app_token()
            api.access_token = tokens.access_token
            logger.info(
                f"[Twitch Send] POST broadcaster_id={broadcaster_id} sender_id={bot_user_id} msg={message[:40]!r}"
            )
            result = await api.send_chat_message(broadcaster_id, bot_user_id, message)
            logger.info(f"[Twitch Send] Helix result: {result}")
            if result.get("is_sent"):
                return True
            logger.info(f"[Twitch Send] Not sent: drop_reason={result.get('drop_reason')}")
            return False
        finally:
            await api.close()
    except Exception as e:
        logger.info(f"[Twitch Send] Failed: {e}")
        return False


async def send_stream_message(message: str, guild_id: int = None) -> bool:
    """
    Send a chat message to whichever stream platform(s) the server runs (per the
    `stream_platforms` setting). Sends to each active platform; returns True if any
    send succeeded. Use this for cross-platform announcements (raffle winners, etc.)
    instead of send_kick_message directly.
    """
    from utils.bot_settings import BotSettingsManager

    platforms_raw = BotSettingsManager(engine, guild_id).get("stream_platforms", "kick") or "kick"
    active = [p.strip().lower() for p in str(platforms_raw).split(",") if p.strip()] or ["kick"]
    logger.info(f"[StreamSend] guild={guild_id} active_platforms={active} msg={message[:40]!r}")
    kick_ok = twitch_ok = None
    if "kick" in active:
        # Raw Kick sender (NOT send_kick_message) so announcements never get
        # mis-routed by a stale _reply_platform contextvar.
        kick_ok = await _send_kick_message_raw(message, guild_id=guild_id)
    if "twitch" in active:
        twitch_ok = await send_twitch_message(message, guild_id=guild_id)
    logger.info(f"[StreamSend] result kick={kick_ok} twitch={twitch_ok}")
    return bool(kick_ok) or bool(twitch_ok)


def require_link_ok(guild_id: int, username: str, platform: str) -> bool:
    """
    Gate for chat-activity commands (slot requests, GTB) when the server has the
    'require_link_for_chat' toggle ON.

    Returns True if the command is ALLOWED to proceed:
      - toggle OFF  → always True (open participation, unchanged behavior)
      - toggle ON   → True only if the chatter is linked ON THE PLATFORM they're
                      chatting from (Twitch chatter needs a twitch link, Kick
                      chatter needs a kick link). This yields a discord_id, which
                      makes cross-platform dedup possible.

    Fails OPEN (returns True) on any error so a DB hiccup never blocks chat.
    """
    try:
        from utils.bot_settings import BotSettingsManager

        if not BotSettingsManager(engine, guild_id).get_bool("require_link_for_chat", default=False):
            return True
        from core.stream_links import resolve_discord_id

        return resolve_discord_id(engine, username, guild_id, platform) is not None
    except Exception as e:
        logger.warning(f"[require_link] check failed (allowing): {e}")
        return True


# -------------------------
# -------------------------
# Database setup and utilities
# -------------------------


class DBConnection:
    """Context manager for safe database connections."""

    def __init__(self):
        self.conn = None

    async def __aenter__(self):
        retries = 3
        while retries > 0:
            try:
                self.conn = engine.connect()
                return self.conn
            except Exception as e:
                retries -= 1
                if retries == 0:
                    raise
                await asyncio.sleep(1)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.conn.close()


# -------------------------
# Database setup
# -------------------------
engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,  # Detect disconnections
    pool_recycle=1800,  # Recycle connections after 30 minutes (Railway timeout)
    pool_size=10,  # 🔒 SECURITY: Increased from 3 to 10 to prevent connection exhaustion
    max_overflow=10,  # 🔒 SECURITY: Increased from 5 to 10 for better availability
    pool_timeout=30,  # Wait up to 30 seconds for a connection
    echo=False,  # Don't log all SQL
    echo_pool=False,  # Disable pool logging to reduce noise
    pool_use_lifo=True,  # Last In First Out for better performance
    connect_args=(
        {
            "connect_timeout": 10,  # Connection timeout
            "keepalives": 1,  # Enable TCP keepalives
            "keepalives_idle": 30,  # Start keepalives after 30s idle
            "keepalives_interval": 10,  # Send keepalive every 10s
            "keepalives_count": 5,  # Drop connection after 5 failed keepalives
        }
        if DATABASE_URL.startswith("postgresql")
        else {}
    ),
)

try:
    with engine.begin() as conn:
        # Create watchtime table (multiserver-aware)
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS watchtime (
            username TEXT,
            minutes INTEGER DEFAULT 0,
            last_active TIMESTAMP,
            discord_server_id BIGINT,
            PRIMARY KEY (username, discord_server_id)
        );
        """
            )
        )

        # Create links table (multi-server aware)
        # New columns:
        # - discord_server_id: isolates links per guild
        # - linked_at: timestamp for link creation
        # Backwards compatibility: migrate existing schema if needed
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS links (
            discord_id BIGINT,
            kick_name TEXT,
            discord_server_id BIGINT,
            linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (discord_id, discord_server_id),
            UNIQUE (kick_name, discord_server_id)
        );
        """
            )
        )

        # Cache the viewer's Discord display name on the link row so the
        # dashboard can show it without a live Discord API call per participant.
        # Populated on link writes when a member is resolvable and refreshed by
        # the periodic backfill loop (update_discord_usernames_task).
        conn.execute(text("ALTER TABLE links ADD COLUMN IF NOT EXISTS discord_username TEXT"))

        # Create pending_links table
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS pending_links (
            discord_id BIGINT PRIMARY KEY,
            kick_name TEXT,
            code TEXT,
            timestamp TEXT
        );
        """
            )
        )

        # Create oauth_notifications table
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS oauth_notifications (
            id SERIAL PRIMARY KEY,
            discord_id BIGINT NOT NULL,
            kick_username TEXT NOT NULL,
            channel_id BIGINT,
            message_id BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            processed BOOLEAN DEFAULT FALSE
        );
        """
            )
        )

        # Platform tag so the role-grant picks the right linked-role per platform
        # (kick_linked_role_id vs twitch_linked_role_id). Backfills to 'kick'.
        conn.execute(
            text("ALTER TABLE oauth_notifications ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'kick'")
        )

        # Create link_panels table for reaction-based OAuth linking
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS link_panels (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            emoji TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, channel_id, message_id)
        );
        """
            )
        )

        # Create timer_panels table for reaction-based timer management
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS timer_panels (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(guild_id, channel_id, message_id)
        );
        """
            )
        )

        # Create bot_settings table for persistent configuration
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            discord_server_id BIGINT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (key, discord_server_id)
        );
        """
            )
        )

        # Create link_logs_config table for Discord logging configuration
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS link_logs_config (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT NOT NULL,
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Create watchtime_roles table for configurable role thresholds
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS watchtime_roles (
            id SERIAL PRIMARY KEY,
            role_name TEXT NOT NULL,
            minutes_required INTEGER NOT NULL,
            display_order INTEGER NOT NULL,
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Insert default roles if table is empty
        role_count = conn.execute(text("SELECT COUNT(*) FROM watchtime_roles")).fetchone()[0]
        if role_count == 0:
            logger.info("📝 Initializing default watchtime roles...")
            for idx, role in enumerate(WATCHTIME_ROLES, 1):
                conn.execute(
                    text(
                        """
                    INSERT INTO watchtime_roles (role_name, minutes_required, display_order, enabled)
                    VALUES (:name, :minutes, :order, TRUE)
                """
                    ),
                    {"name": role["name"], "minutes": role["minutes"], "order": idx},
                )
            logger.info("✅ Default watchtime roles created")

        # Create Guess the Balance tables
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS gtb_sessions (
            id SERIAL PRIMARY KEY,
            opened_by TEXT NOT NULL,
            opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            closed_at TIMESTAMP,
            result_amount NUMERIC(12, 2),
            status TEXT DEFAULT 'open' CHECK (status IN ('open', 'closed', 'completed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            winner_count INTEGER NOT NULL DEFAULT 3
        );
        """
            )
        )

        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS gtb_guesses (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES gtb_sessions(id) ON DELETE CASCADE,
            kick_username TEXT NOT NULL,
            guess_amount NUMERIC(12, 2) NOT NULL,
            guessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            discord_server_id BIGINT,
            UNIQUE(session_id, kick_username)
        );
        """
            )
        )

        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS gtb_winners (
            id SERIAL PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES gtb_sessions(id) ON DELETE CASCADE,
            kick_username TEXT NOT NULL,
            rank INTEGER NOT NULL CHECK (rank BETWEEN 1 AND 10),
            guess_amount NUMERIC(12, 2) NOT NULL,
            result_amount NUMERIC(12, 2) NOT NULL,
            difference NUMERIC(12, 2) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # GTB configurable winner count. The column + the relaxed rank check are
        # owned by the dashboard's run_migrations(), but the bot must not depend
        # on the dashboard having deployed first (it READS winner_count and
        # WRITES ranks > 3) — add them defensively so the bot's GTB scoring never
        # hits "column does not exist" or the old rank IN (1,2,3) check on a DB
        # where this table predates the feature. Mirrors the token-table pattern
        # below. Each statement is IF-EXISTS-guarded so it never aborts the
        # surrounding transaction on the common (already-applied) path.
        try:
            conn.execute(
                text("ALTER TABLE gtb_sessions ADD COLUMN IF NOT EXISTS winner_count INTEGER NOT NULL DEFAULT 3;")
            )
        except Exception:
            pass  # Column may already exist
        try:
            conn.execute(text("ALTER TABLE gtb_winners DROP CONSTRAINT IF EXISTS gtb_winners_rank_check;"))
            conn.execute(
                text("ALTER TABLE gtb_winners ADD CONSTRAINT gtb_winners_rank_check CHECK (rank BETWEEN 1 AND 10);")
            )
        except Exception:
            pass  # Constraint may already be in the relaxed form

        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS clips (
            id SERIAL PRIMARY KEY,
            kick_username TEXT NOT NULL,
            clip_title TEXT,
            clip_duration INTEGER NOT NULL,
            clip_url TEXT,
            discord_server_id BIGINT,
            kick_channel TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Add multiserver columns to clips table if they don't exist
        try:
            conn.execute(
                text(
                    """
                ALTER TABLE clips
                ADD COLUMN IF NOT EXISTS discord_server_id BIGINT,
                ADD COLUMN IF NOT EXISTS kick_channel TEXT,
                ADD COLUMN IF NOT EXISTS clip_title TEXT;
            """
                )
            )
        except Exception:
            pass  # Columns may already exist

        # Clip hosting columns. Mirrors the dashboard's run_migrations() — the
        # two Railway services deploy independently and either may boot first.
        try:
            conn.execute(
                text(
                    """
                ALTER TABLE clips
                ADD COLUMN IF NOT EXISTS filename TEXT,
                ADD COLUMN IF NOT EXISTS file_size BIGINT,
                ADD COLUMN IF NOT EXISTS thumbnail_url TEXT,
                ADD COLUMN IF NOT EXISTS video_width INTEGER,
                ADD COLUMN IF NOT EXISTS video_height INTEGER;
            """
                )
            )
        except Exception:
            pass  # Columns may already exist

        # Ensure the per-server column exists on the OAuth token tables the BOT
        # READS (twitch_oauth_tokens / kick_oauth_tokens). These tables are created
        # by the dashboard's run_migrations(), but the bot must not depend on the
        # dashboard having deployed first — add the column defensively so the bot's
        # token-maintenance + send queries (which filter by discord_server_id)
        # never hit "column does not exist". The PK rescope stays the dashboard's
        # job; only the column is needed here.
        for _tok_table in ("twitch_oauth_tokens", "kick_oauth_tokens"):
            # SAVEPOINT-isolate: on a fresh/partial DB this ALTER fails because
            # the table doesn't exist yet. Without a nested transaction that
            # failure poisons the whole init transaction (Postgres:
            # "current transaction is aborted"), silently skipping every later
            # CREATE TABLE. begin_nested() rolls back only this statement.
            try:
                with conn.begin_nested():
                    conn.execute(
                        text(
                            f"ALTER TABLE {_tok_table} ADD COLUMN IF NOT EXISTS discord_server_id BIGINT NOT NULL DEFAULT 0;"
                        )
                    )
            except Exception:
                pass  # Table may not exist yet (dashboard creates it) or column present

        # needs_reauth on kick_oauth_tokens: set when Kick rejects a refresh with
        # invalid_grant (dead token). The bot's refresh scheduler reads/writes it,
        # so add it defensively here too rather than depend on the dashboard
        # deploying first (same reasoning as the discord_server_id backfill above).
        try:
            with conn.begin_nested():
                conn.execute(
                    text(
                        "ALTER TABLE kick_oauth_tokens ADD COLUMN IF NOT EXISTS needs_reauth BOOLEAN NOT NULL DEFAULT FALSE;"
                    )
                )
        except Exception:
            pass  # Table may not exist yet (dashboard creates it) or column present

        # Native display name for chat-activity entries (Twitch name on Twitch, Kick
        # name on Kick). The existing username column stays the canonical credit/dedup
        # key; display_name is what's SHOWN in embeds/panels/announcements. Nullable —
        # old rows fall back to the username column at render time.
        for _disp_table in ("slot_requests", "gtb_guesses", "gtb_winners", "giveaway_entries"):
            # SAVEPOINT-isolate (see note above): these tables may not exist yet
            # on a fresh DB; a bare failure would abort the init transaction.
            try:
                with conn.begin_nested():
                    conn.execute(text(f"ALTER TABLE {_disp_table} ADD COLUMN IF NOT EXISTS display_name TEXT;"))
            except Exception:
                pass  # Table may not exist yet or column already present

        # -------------------------
        # Point Reward System Tables
        # -------------------------

        # Points balance for each user
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS user_points (
            id SERIAL PRIMARY KEY,
            kick_username TEXT NOT NULL,
            discord_id BIGINT,
            points INTEGER DEFAULT 0,
            total_earned INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(kick_username)
        );
        """
            )
        )

        # Track watchtime already converted to points (similar to raffle system)
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS points_watchtime_converted (
            id SERIAL PRIMARY KEY,
            kick_username TEXT NOT NULL,
            minutes_converted INTEGER NOT NULL,
            points_awarded INTEGER NOT NULL,
            converted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Point shop items
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS point_shop_items (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            price INTEGER NOT NULL,
            stock INTEGER DEFAULT -1,
            image_url TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Per-user purchase cap (-1 = unlimited, same sentinel as `stock`), and the
        # rolling window that cap is measured over (0 = all-time, never resets).
        # Also created by the dashboard's migration; declared here too so the columns
        # exist no matter which service deploys first.
        conn.execute(text("ALTER TABLE point_shop_items ADD COLUMN IF NOT EXISTS per_user_limit INTEGER DEFAULT -1;"))
        conn.execute(
            text("ALTER TABLE point_shop_items ADD COLUMN IF NOT EXISTS limit_window_seconds INTEGER DEFAULT 0;")
        )

        # Point shop sales/purchases
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS point_sales (
            id SERIAL PRIMARY KEY,
            item_id INTEGER REFERENCES point_shop_items(id),
            kick_username TEXT NOT NULL,
            discord_id BIGINT,
            item_name TEXT NOT NULL,
            price_paid INTEGER NOT NULL,
            quantity INTEGER DEFAULT 1,
            status TEXT DEFAULT 'pending',
            notes TEXT,
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Dashboard notification inbox + browser-push outbox. These shared
        # tables are also migrated by the dashboard so either Railway service
        # can boot first.
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            discord_server_id BIGINT NOT NULL,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT,
            data JSONB,
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )
        conn.execute(
            text(
                """
        CREATE INDEX IF NOT EXISTS idx_notifications_server_read
        ON notifications(discord_server_id, is_read, created_at DESC);
        """
            )
        )
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS web_push_subscriptions (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            discord_server_id BIGINT NOT NULL,
            endpoint TEXT NOT NULL,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (user_id, discord_server_id, endpoint)
        );
        """
            )
        )
        conn.execute(
            text(
                """
        CREATE INDEX IF NOT EXISTS idx_web_push_subscriptions_server
        ON web_push_subscriptions (discord_server_id);
        """
            )
        )
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS web_push_deliveries (
            id BIGSERIAL PRIMARY KEY,
            notification_id INTEGER NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
            subscription_id BIGINT NOT NULL REFERENCES web_push_subscriptions(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claimed_at TIMESTAMPTZ,
            sent_at TIMESTAMPTZ,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (notification_id, subscription_id)
        );
        """
            )
        )
        conn.execute(
            text(
                """
        CREATE INDEX IF NOT EXISTS idx_web_push_deliveries_pending
        ON web_push_deliveries (status, next_attempt_at);
        """
            )
        )
        conn.execute(text("DELETE FROM notifications WHERE type IN ('stream_live', 'stream_offline')"))

        # Point system settings
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS point_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # -------------------------
        # Gambling History Table
        # -------------------------
        conn.execute(
            text(
                """
        CREATE TABLE IF NOT EXISTS gambling_history (
            id SERIAL PRIMARY KEY,
            discord_server_id BIGINT NOT NULL,
            discord_id BIGINT NOT NULL,
            kick_username TEXT NOT NULL,
            game_type TEXT NOT NULL,
            bet_amount BIGINT NOT NULL,
            payout_amount BIGINT NOT NULL,
            net_result BIGINT NOT NULL,
            game_data JSONB,
            server_seed TEXT NOT NULL,
            client_seed TEXT NOT NULL,
            nonce TEXT NOT NULL,
            proof_hash TEXT NOT NULL,
            random_value NUMERIC(5,2),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
            )
        )

        # Index for efficient lookups
        conn.execute(
            text(
                """
        CREATE INDEX IF NOT EXISTS idx_gambling_history_server_user
        ON gambling_history (discord_server_id, discord_id, created_at DESC);
        """
            )
        )

    logger.debug("✅ Database tables initialized successfully")
except Exception as e:
    logger.warning(f"⚠️ Database initialization error: {e}")
    raise


def _merge_cross_platform_points():
    """One-time-per-deploy merge of split point balances into one row per person.

    Before cross-platform points, a viewer active on both Kick and Twitch accrued
    separate user_points rows (one per platform username). Now points are shared:
    a person accrues under a single CANONICAL username (the alphabetically-first of
    their linked usernames — the same rule the watchtime tracker + shop use). This
    consolidates any pre-existing split rows into the canonical row so historical
    balances are preserved and visible.

    Idempotent: after the first run each person has a single row, so re-running on
    later deploys is a no-op. The points_watchtime_converted ledger is re-pointed to
    the canonical username too, so future watchtime→points conversion doesn't
    re-award minutes already converted under a now-merged username.
    """
    try:
        with engine.begin() as conn:
            # People (per server) with 2+ linked usernames — only these can have splits.
            groups = conn.execute(
                text(
                    """
                    SELECT discord_server_id, discord_id,
                           ARRAY_AGG(DISTINCT LOWER(kick_name)) AS unames
                    FROM links
                    WHERE discord_id IS NOT NULL AND kick_name IS NOT NULL
                    GROUP BY discord_server_id, discord_id
                    HAVING COUNT(DISTINCT LOWER(kick_name)) > 1
                    """
                )
            ).fetchall()

            merged_people = 0
            for server_id, _discord_id, unames in groups:
                unames = [u for u in (unames or []) if u]
                if len(unames) < 2:
                    continue
                canonical = min(unames)
                others = [u for u in unames if u != canonical]

                # Sum the non-canonical rows into the canonical row, then delete them.
                agg = conn.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(points),0), COALESCE(SUM(total_earned),0),
                               COALESCE(SUM(total_spent),0)
                        FROM user_points
                        WHERE discord_server_id = :sid AND LOWER(kick_username) = ANY(:others)
                        """
                    ),
                    {"sid": server_id, "others": others},
                ).fetchone()
                add_points, add_earned, add_spent = (agg[0], agg[1], agg[2]) if agg else (0, 0, 0)

                if add_points or add_earned or add_spent:
                    # Ensure a canonical row exists, then fold the others in.
                    conn.execute(
                        text(
                            """
                            INSERT INTO user_points (kick_username, discord_id, points, total_earned, total_spent, discord_server_id)
                            VALUES (:c, :did, 0, 0, 0, :sid)
                            ON CONFLICT (kick_username, discord_server_id) DO NOTHING
                            """
                        ),
                        {"c": canonical, "did": _discord_id, "sid": server_id},
                    )
                    conn.execute(
                        text(
                            """
                            UPDATE user_points
                            SET points = points + :p, total_earned = total_earned + :e,
                                total_spent = total_spent + :s, last_updated = CURRENT_TIMESTAMP
                            WHERE discord_server_id = :sid AND LOWER(kick_username) = :c
                            """
                        ),
                        {"p": add_points, "e": add_earned, "s": add_spent, "sid": server_id, "c": canonical},
                    )
                    conn.execute(
                        text(
                            "DELETE FROM user_points WHERE discord_server_id = :sid AND LOWER(kick_username) = ANY(:others)"
                        ),
                        {"sid": server_id, "others": others},
                    )

                # Drop the non-canonical converted-watchtime ledger rows. Those
                # usernames no longer accrue watchtime (accrual consolidates to the
                # canonical username now), so their ledger is dead; the canonical
                # username keeps its own ledger. Deleting (rather than re-pointing)
                # avoids the (kick_username, discord_server_id) unique-constraint
                # conflict when the canonical already has ledger rows — the same
                # approach the username-change handler uses.
                conn.execute(
                    text(
                        "DELETE FROM points_watchtime_converted WHERE discord_server_id = :sid AND LOWER(kick_username) = ANY(:others)"
                    ),
                    {"sid": server_id, "others": others},
                )
                merged_people += 1

            if merged_people:
                logger.info(f"[Points] ✅ Merged cross-platform point balances for {merged_people} member(s)")
    except Exception as e:
        # Non-fatal: a failed merge must not stop the bot from starting.
        logger.warning(f"[Points] ⚠️ Cross-platform points merge skipped: {e}")


_merge_cross_platform_points()

# -------------------------
# Bot Settings Manager
# -------------------------
# Multi-server support: Per-guild settings managers
# MULTISERVER: Initialize settings manager without guild-specific configuration
# Guild settings are loaded dynamically via get_guild_settings(guild_id)
bot_settings = BotSettingsManager(engine)
logger.debug("✅ Multiserver bot initialized")
logger.debug("   Each Discord server configures via Dashboard → Profile Settings")

# Dictionary to store per-guild settings managers
guild_settings_managers = {}


def get_guild_settings(guild_id: int) -> BotSettingsManager:
    """
    Get settings manager for a specific guild.
    REQUIRED for all guild-specific operations.

    Args:
        guild_id: Discord guild/server ID

    Returns:
        BotSettingsManager instance for the guild
    """
    # Get or create guild-specific settings
    if guild_id not in guild_settings_managers:
        guild_settings_managers[guild_id] = BotSettingsManager(engine, guild_id=guild_id)
        logger.debug(f"✅ Loaded settings for guild {guild_id}")

    return guild_settings_managers[guild_id]


# Default !raffle promo (used when a guild hasn't configured its own in the dashboard).
DEFAULT_RAFFLE_PROMO_MESSAGE = (
    "Do you want to win a $100 super buy on Sweet Bonanza 1000? "
    "All you gotta do is join my discord, verify with Wagerlabs and follow the instructions"
)
DEFAULT_DISCORD_INVITE_URL = "https://discord.gg/k7CXJtfrPY"


def build_raffle_promo(settings) -> str:
    """Build the !raffle chat promo from per-guild dashboard settings.

    Reads `raffle_promo_message` and `discord_invite_url` from the guild's
    BotSettingsManager, falling back to the original hardcoded text so servers
    that haven't configured anything see no change. The invite is appended as
    "-> <url>" only when present.
    """
    message = DEFAULT_RAFFLE_PROMO_MESSAGE
    invite = DEFAULT_DISCORD_INVITE_URL
    if settings is not None:
        try:
            message = settings.get("raffle_promo_message", default=DEFAULT_RAFFLE_PROMO_MESSAGE)
            invite = settings.get("discord_invite_url", default=DEFAULT_DISCORD_INVITE_URL)
        except Exception:
            pass
    invite = (invite or "").strip()
    if invite:
        return f"{message.rstrip()} -> {invite}"
    return message.rstrip()


# Multiserver: No global KICK_CHANNEL or SLOT_CALLS_CHANNEL_ID
# All features must use get_guild_settings(guild.id) to access per-guild configuration
logger.debug("✅ Multiserver bot ready - configure each guild in Dashboard → Profile Settings")


async def ensure_chatroom_id(guild_id: int, guild_name: str) -> None:
    """Ensure chatroom_id exists in DB for this guild; resolve via kickpython-backed core API if missing."""
    try:
        # Check existing setting
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                SELECT value FROM bot_settings
                WHERE key = 'kick_chatroom_id' AND discord_server_id = :gid
                LIMIT 1
            """
                ),
                {"gid": guild_id},
            ).fetchone()
            if row and row[0]:
                logger.info(f"📦 chatroom_id already set: {row[0]}")
                return

        # Need to resolve: get channel username
        kick_username = None
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                SELECT value FROM bot_settings
                WHERE key = 'kick_channel' AND discord_server_id = :gid
                LIMIT 1
            """
                ),
                {"gid": guild_id},
            ).fetchone()
            if row and row[0]:
                kick_username = str(row[0]).strip()

        if not kick_username:
            logger.info(f"⚠️ Cannot resolve chatroom_id: no kick_channel configured")
            return

        logger.info(f"🔎 Resolving chatroom_id for {kick_username} via core.fetch_chatroom_id()...")
        from core.kick_api import fetch_chatroom_id as core_fetch_chatroom_id

        chatroom_id = await core_fetch_chatroom_id(kick_username)
        if not chatroom_id:
            logger.info(f"❌ Failed to resolve chatroom_id for {kick_username}")
            return

        # Save to DB
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                INSERT INTO bot_settings (key, value, discord_server_id)
                VALUES ('kick_chatroom_id', :val, :gid)
                ON CONFLICT (key, discord_server_id)
                DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
            """
                ),
                {"val": str(chatroom_id), "gid": guild_id},
            )
        logger.info(f"✅ Stored chatroom_id: {chatroom_id}")
    except Exception as e:
        logger.info(f"⚠️ ensure_chatroom_id error: {e}")


# -------------------------
# Pusher WebSocket for direct chat message reading
# -------------------------


async def kick_chat_loop(channel_slug: str, guild_id: int):
    """
    Connect to Pusher WebSocket and read ALL chat messages directly (no webhooks).
    Handles commands, watchtime tracking, and all chat interactions.
    """
    guild = bot.get_guild(guild_id)
    guild_name = guild.name if guild else str(guild_id)

    # Tag all logging from this per-guild task with the server (own asyncio Task).
    set_server(guild_id, guild.name if guild else None)

    # Get chatroom_id from database or fetch it
    chatroom_id = None
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT value FROM bot_settings
                WHERE key = 'kick_chatroom_id' AND discord_server_id = :guild_id
                LIMIT 1
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()
            if result and result[0]:
                chatroom_id = int(result[0])
                logger.info(f"📦 Loaded chatroom_id from database: {chatroom_id}")

                # HOTFIX: 152837 is broadcaster_user_id, not chatroom_id!
                # Clear it and refetch the correct one
                if chatroom_id == 152837:
                    logger.info(f"⚠️ Invalid chatroom_id detected (152837 is broadcaster_user_id)")
                    logger.info(f"🔄 Clearing and refetching correct chatroom_id...")
                    conn.execute(
                        text(
                            """
                        DELETE FROM bot_settings
                        WHERE key = 'kick_chatroom_id' AND discord_server_id = :guild_id
                    """
                        ),
                        {"guild_id": guild_id},
                    )
                    conn.commit()
                    chatroom_id = None
    except Exception as e:
        logger.info(f"⚠️ Error loading chatroom_id from database: {e}")
        import traceback

        traceback.print_exc()

    # Fetch chatroom_id if not in database using kickpython
    if not chatroom_id:
        logger.info(f"🔍 No chatroom_id in database, fetching for channel: {channel_slug}")
        try:
            # Use kickpython to fetch chatroom_id
            from kickpython import KickAPI as KickPyAPI

            access_token = os.getenv("KICK_BOT_USER_TOKEN") or os.getenv("KICK_USER_TOKEN")
            client_id = os.getenv("KICK_CLIENT_ID")
            client_secret = os.getenv("KICK_CLIENT_SECRET")
            redirect_uri = os.getenv("OAUTH_BASE_URL")

            if redirect_uri and not redirect_uri.startswith(("http://", "https://")):
                redirect_uri = f"https://{redirect_uri}"

            api = KickPyAPI(
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=f"{redirect_uri}/auth/kick/callback" if redirect_uri else None,
            )
            if access_token:
                api.access_token = access_token

            logger.info(f"🔌 Resolving chatroom_id via kickpython...")

            # Create task to connect
            connect_task = asyncio.create_task(api.connect_to_chatroom(channel_slug))

            # Wait for chatroom_id to be discovered (kickpython sets this automatically)
            timeout = 15
            start_time = asyncio.get_event_loop().time()

            while asyncio.get_event_loop().time() - start_time < timeout:
                await asyncio.sleep(0.3)

                # Check if chatroom_id was discovered
                if hasattr(api, "chatroom_id") and api.chatroom_id:
                    chatroom_id = int(api.chatroom_id)
                    logger.info(f"✅ kickpython found chatroom_id: {chatroom_id}")

                    # Cancel the connection task since we only needed the ID
                    connect_task.cancel()
                    try:
                        await connect_task
                    except asyncio.CancelledError:
                        pass

                    # Save to database
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                            INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                            VALUES ('kick_chatroom_id', :cid, :guild_id, CURRENT_TIMESTAMP)
                            ON CONFLICT (key, discord_server_id)
                            DO UPDATE SET value = :cid, updated_at = CURRENT_TIMESTAMP
                        """
                            ),
                            {"cid": str(chatroom_id), "guild_id": guild_id},
                        )
                    logger.info(f"💾 Saved chatroom_id to database")
                    break

                # Log progress every 3 seconds
                elapsed = asyncio.get_event_loop().time() - start_time
                if int(elapsed) % 3 == 0 and int(elapsed) > 0:
                    logger.info(f"⏳ Waiting for chatroom_id... ({int(elapsed)}s)")

            # Cleanup
            if not chatroom_id:
                connect_task.cancel()
                try:
                    await connect_task
                except asyncio.CancelledError:
                    pass

                logger.info(f"❌ Timeout after {timeout}s - kickpython couldn't resolve chatroom_id")
                logger.info(f"💡 Verify channel name: '{channel_slug}'")
                return

        except ImportError as e:
            logger.info(f"❌ kickpython not installed: {e}")
            logger.info(f"💡 Install with: pip install kickpython")
            return
        except Exception as e:
            logger.info(f"❌ Error fetching chatroom_id: {type(e).__name__}: {e}")
            import traceback

            traceback.print_exc()
            return

    if not chatroom_id:
        logger.info(f"❌ No chatroom_id available - cannot start WebSocket")
        return

    channel_name = f"chatrooms.{chatroom_id}.v2"
    logger.info(f"🔌 Connecting to Pusher WebSocket...")
    logger.info(f"   Kick Channel: {channel_slug}")
    logger.info(f"   Chatroom ID: {chatroom_id}")
    logger.info(f"   Pusher Channel: {channel_name}")

    while True:
        try:
            async with websockets.connect(KICK_CHAT_WS) as ws:
                # Subscribe to chatroom
                subscribe_msg = json.dumps({"event": "pusher:subscribe", "data": {"channel": channel_name}})
                await ws.send(subscribe_msg)
                logger.info(f"✅ Connected to Pusher WebSocket")

                async for message in ws:
                    try:
                        data = json.dumps(message)
                        event_type = data.get("event")

                        # Skip chat messages if kickpython is handling them
                        # Only process subscription events from Pusher when kickpython is enabled
                        if KICK_USE_KICKPYTHON_WS and event_type == "App\\Events\\ChatMessageEvent":
                            continue  # kickpython handles chat messages

                        # Handle chat messages (only when kickpython is disabled)
                        if event_type == "App\\Events\\ChatMessageEvent":
                            payload = json.loads(data.get("data", "{}"))
                            username = payload.get("sender", {}).get("username", "Unknown")
                            content = payload.get("content", "")

                            logger.info(f"💬 {username}: {content}")

                            # Update watchtime tracking
                            now = datetime.now(timezone.utc)
                            username_lower = username.lower()

                            last_chat_activity_by_guild[guild_id] = now
                            guild_active_viewers = active_viewers_by_guild.get(guild_id, {})
                            guild_active_viewers[username_lower] = now
                            active_viewers_by_guild[guild_id] = guild_active_viewers

                            guild_recent_chatters = recent_chatters_by_guild.get(guild_id, {})
                            guild_recent_chatters[username_lower] = now
                            recent_chatters_by_guild[guild_id] = guild_recent_chatters

                            # 🎁 GIVEAWAY: Track messages for keyword and active chatter detection
                            if guild_id in giveaway_managers:
                                giveaway_manager = giveaway_managers[guild_id]
                                if giveaway_manager.active_giveaway:
                                    try:
                                        entry_method = giveaway_manager.active_giveaway["entry_method"]

                                        # Keyword detection - exact match only (no extra text)
                                        if entry_method == "keyword":
                                            keyword = giveaway_manager.active_giveaway.get("keyword", "").lower()
                                            if keyword and content_stripped.lower() == keyword:
                                                await giveaway_manager.add_entry(username, entry_method="keyword")
                                                logger.info(f"🎁 Giveaway entry added: {username} (keyword: {keyword})")

                                        # Active chatter tracking
                                        elif entry_method == "active_chatter":
                                            await giveaway_manager.track_message(username, content)
                                    except Exception as e:
                                        logger.info(f"⚠️ Giveaway tracking error: {e}")

                            # Process commands
                            content_stripped = content.strip()

                            # Custom commands
                            commands_manager = getattr(bot, "custom_commands_managers", {}).get(guild_id) or getattr(
                                bot, "custom_commands_manager", None
                            )
                            if commands_manager:
                                try:
                                    original_guild_id = getattr(commands_manager, "discord_server_id", None)
                                    try:
                                        commands_manager.discord_server_id = guild_id
                                        commands_manager.send_message_callback = lambda msg: send_kick_message(
                                            msg, guild_id=guild_id
                                        )
                                        handled = await commands_manager.handle_message(
                                            content,
                                            username,
                                            platform="kick",
                                            display_name=username,
                                        )
                                    finally:
                                        commands_manager.discord_server_id = original_guild_id
                                    if handled:
                                        continue
                                except Exception as e:
                                    logger.info(f"⚠️ Custom command error: {e}")

                            # !raffle command
                            if content_stripped.lower() == "!raffle":
                                await send_kick_message(
                                    build_raffle_promo(get_guild_settings(guild_id)),
                                    guild_id=guild_id,
                                )

                            # !call / !sr commands (slot requests)
                            elif content_stripped.startswith(("!call", "!sr")):
                                # Use per-guild slot call tracker
                                tracker = (
                                    bot.slot_call_trackers_by_guild.get(guild_id)
                                    if hasattr(bot, "slot_call_trackers_by_guild")
                                    else None
                                )
                                if not tracker and hasattr(bot, "slot_call_tracker"):
                                    # Fallback to global tracker for backwards compatibility
                                    tracker = bot.slot_call_tracker

                                if tracker and require_link_ok(guild_id, username, "kick"):
                                    slot_call = (
                                        content_stripped[5:].strip()[:200]
                                        if content_stripped.startswith("!call")
                                        else content_stripped[3:].strip()[:200]
                                    )
                                    if slot_call:
                                        await tracker.handle_slot_call(username, slot_call, platform="kick")

                            # !gtb command
                            elif content_stripped.lower().startswith("!gtb"):
                                # Use per-guild GTB manager
                                gtb_mgr = (
                                    bot.gtb_managers_by_guild.get(guild_id)
                                    if hasattr(bot, "gtb_managers_by_guild")
                                    else None
                                )
                                if gtb_mgr and require_link_ok(guild_id, username, "kick"):
                                    parts = content_stripped.split(maxsplit=1)
                                    if len(parts) == 2:
                                        amount = parse_amount(parts[1])
                                        if amount is not None:
                                            success, message = gtb_mgr.add_guess(username, amount)
                                            response = f"@{username} {message}" + (" Good luck! 🎰" if success else "")
                                            # Legacy Kick-only loop → reply on Kick directly.
                                            await _send_kick_message_raw(response, guild_id=guild_id)

                        # Connection health
                        elif event_type == "pusher:connection_established":
                            logger.info(f"✅ Pusher connection established")
                        elif event_type == "pusher_internal:subscription_succeeded":
                            logger.info(f"✅ Subscribed to channel: {channel_name}")

                    except json.JSONDecodeError:
                        pass  # Ignore non-JSON messages
                    except Exception as e:
                        logger.info(f"⚠️ Error processing message: {e}")

        except Exception as e:
            logger.info(f"❌ WebSocket error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


# -------------------------
# Discord bot setup
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True  # Enable reaction events

# Safe default mentions: never let user/chat-derived text ping @everyone/@here or
# roles. Individual user mentions are still allowed (needed for reply pings); pass an
# explicit allowed_mentions on a specific send only when that send needs different rules.
# max_messages=None disables the default 1000-entry message cache: only
# on_raw_reaction_add / on_command_error are used (no message edit/delete or non-raw
# reaction handlers), so nothing reads the cache - it is pure resident memory.
bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
    max_messages=None,
)

# App Discovery requires native Discord application commands. These handlers are
# registered on the local tree at import time and published globally after the
# bot connects, making them available in every server that installs Wagerlabs.
register_wagerlabs_slash_commands(bot, engine)


@bot.before_invoke
async def deduplicate_command(ctx):
    """
    Prevent duplicate command responses during Railway zero-downtime deployments.
    Railway briefly runs TWO containers simultaneously when deploying — both receive
    the same Discord gateway message and would both respond. This hook claims the
    Discord message ID in Redis with a 10-second TTL; the first instance to claim it
    proceeds, the second raises CheckFailure and is silently ignored.
    """
    # Tag all logging for this command with its guild. discord.py allows only one
    # global before_invoke, so this single hook covers every hybrid command and cog
    # command. It runs inside the command's asyncio Task, so the context is scoped
    # to this invocation and discarded when the Task ends (no manual clear needed).
    if ctx.guild:
        set_server(ctx.guild.id, ctx.guild.name)

    if not redis_client:
        return  # Redis unavailable — allow command through
    key = f"cmd_dedup:{command_invocation_id(ctx)}"
    if not redis_client.set(key, "1", nx=True, ex=10):
        # Another instance already claimed this message ID — suppress this invocation
        raise commands.CheckFailure("duplicate_suppressed")


# Store settings manager on bot for access throughout
bot.settings_manager = bot_settings
# Expose the per-guild settings accessor so cogs can read guild-scoped config
# off the shared cache (e.g. raffle commands resolving campaign code / platform).
bot.get_guild_settings = get_guild_settings

# -------------------------
# Bot lifecycle: start Kick chat listeners
# -------------------------
# NOTE: on_ready() is defined later in the file (around line 4950)
# DO NOT add another @bot.event on_ready here - it will override the main one!

# Multiserver tracking - per-guild dictionaries
# active_viewers_by_guild[guild_id][username] = last_seen_time
active_viewers_by_guild = {}
# recent_chatters_by_guild[guild_id][username] = last_chat_time
recent_chatters_by_guild = {}
# last_chat_activity_by_guild[guild_id] = last_activity_time
last_chat_activity_by_guild = {}
# kick_chatroom_ids[guild_id] = chatroom_id
kick_chatroom_ids = {}

# Legacy global variables (for backward compatibility during transition)
active_viewers = {}
kick_ws = None
kick_chatroom_id_global = None

# Stream status tracking
stream_tracking_enabled = True  # Admin can toggle this
# When true, admins can force watchtime updates to run even if the live-detection
# checks (unique chatters, recent activity) would normally block updates.
tracking_force_override = False  # Default off, loaded per-guild when needed
# When true, enables detailed debug logging for watchtime tracking
watchtime_debug_enabled = True  # Admin can toggle this

# Note: tracking_force_override is saved per-guild in bot_settings table.
# The global variable is used as a cache. Each guild loads its own value.

last_chat_activity = None  # Track last time we saw any chat activity

# 🔒 SECURITY: Track unique chatters in recent window for stream-live detection
recent_chatters = {}  # {username: timestamp} - rolling window of recent chat activity

# Clip buffer tracking - per guild
clip_buffer_active_by_guild = {}  # guild_id -> bool: Track if clip buffer is running on Dashboard
last_stream_live_state_by_guild = {}  # guild_id -> bool: Track last known stream live state (for detecting transitions)

# Raffle system global trackers - now per-guild dictionaries
gifted_sub_trackers = {}  # guild_id -> GiftedSubTracker
shuffle_trackers = {}  # guild_id -> ShuffleTracker
slot_call_trackers = {}  # guild_id -> SlotCallTracker
gtb_managers = {}  # guild_id -> GTBManager
giveaway_managers = {}  # guild_id -> GiveawayManager

# Legacy global variables (for backward compatibility)
gifted_sub_tracker = None
shuffle_tracker = None
slot_call_tracker = None
gtb_manager = None
MIN_UNIQUE_CHATTERS = 2  # Require at least 2 different people chatting to consider stream "live"
CHAT_ACTIVITY_WINDOW_MINUTES = 5  # Look back 5 minutes for unique chatters


# -------------------------
# Helper functions
# -------------------------
def get_active_chatters_count(guild_id: Optional[int] = None):
    """Get the number of active chatters in the recent window for a specific guild"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    chat_cutoff = now - timedelta(minutes=CHAT_ACTIVITY_WINDOW_MINUTES)

    if guild_id is None:
        # Legacy: use global recent_chatters
        active_chatters = {
            username: timestamp for username, timestamp in recent_chatters.items() if timestamp >= chat_cutoff
        }
    else:
        # Multiserver: use per-guild tracking
        guild_chatters = recent_chatters_by_guild.get(guild_id, {})
        active_chatters = {
            username: timestamp for username, timestamp in guild_chatters.items() if timestamp >= chat_cutoff
        }

    return len(active_chatters)


# -------------------------
# Role configuration helper
# -------------------------
def load_watchtime_roles():
    """Load watchtime role configuration from database."""
    try:
        with engine.connect() as conn:
            roles = conn.execute(
                text(
                    """
                SELECT role_name, minutes_required
                FROM watchtime_roles
                WHERE enabled = TRUE
                ORDER BY display_order ASC
            """
                )
            ).fetchall()

            if roles:
                role_list = [{"name": r[0], "minutes": r[1]} for r in roles]
                logger.debug(f"🎯 Loaded {len(role_list)} watchtime roles from database")
                return role_list
            else:
                logger.warning("⚠️ No roles found in database, using hardcoded defaults")
                return WATCHTIME_ROLES
    except Exception as e:
        logger.warning(f"⚠️ Could not load roles from database: {e}")
        return WATCHTIME_ROLES


# -------------------------
# Link logging helper
# -------------------------
async def log_link_attempt(
    discord_user,
    kick_username: str,
    success: bool,
    error_message: str = None,
    guild_id: int = None,
    platform: str = "kick",
):
    """Log account linking attempts to configured Discord channel.

    `platform` ('kick' | 'twitch') labels the username field, since both
    platforms' link flows share this logger."""
    if not guild_id:
        logger.warning("⚠️ log_link_attempt: No guild_id provided, skipping log")
        return

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT channel_id, enabled FROM link_logs_config
                WHERE guild_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()

            if not result or not result[1]:  # Not configured or disabled
                return

            channel_id = result[0]

        # Get the channel
        channel = bot.get_channel(int(channel_id))
        if not channel:
            logger.warning(f"⚠️ Link logs channel {channel_id} not found")
            return

        # Create embed
        embed = discord.Embed(
            title="🔗 Account Link Attempt" if success else "❌ Account Link Failed",
            color=0x53FC18 if success else 0xFF0000,
            timestamp=datetime.now(timezone.utc),
        )

        platform_label = "Twitch" if str(platform).lower() == "twitch" else "Kick"
        embed.add_field(name="Discord User", value=f"{discord_user.mention} ({discord_user})", inline=False)
        embed.add_field(name=f"{platform_label} Username", value=kick_username, inline=True)
        embed.add_field(name="Status", value="✅ Success" if success else "❌ Failed", inline=True)

        if not success and error_message:
            embed.add_field(name="Error", value=error_message, inline=False)

        embed.set_footer(text=f"Discord ID: {discord_user.id}")

        await channel.send(embed=embed)
        logger.info(f"✅ Logged link attempt to channel {channel_id}")

    except Exception as e:
        logger.warning(f"⚠️ Failed to log link attempt: {e}")


# -------------------------
# Kick chat message sending
# -------------------------
async def get_kick_bot_token() -> Optional[str]:
    """
    Get an OAuth access token for the Kick bot using Client Credentials flow.

    Returns:
        str: Access token if successful, None otherwise
    """
    global KICK_BOT_TOKEN

    if not KICK_CLIENT_ID or not KICK_CLIENT_SECRET:
        logger.info("[Kick Bot] ⚠️ Client credentials not configured")
        return None

    try:
        # Official Kick OAuth token endpoint
        token_url = "https://id.kick.com/oauth/token"

        # Must be form-encoded, not JSON
        payload = {"grant_type": "client_credentials", "client_id": KICK_CLIENT_ID, "client_secret": KICK_CLIENT_SECRET}

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=payload, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    KICK_BOT_TOKEN = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    logger.info(f"[Kick Bot] ✅ Got access token (expires in {expires_in}s)")
                    return KICK_BOT_TOKEN
                else:
                    error_text = await response.text()
                    logger.info(f"[Kick Bot] ❌ Failed to get token (HTTP {response.status}): {error_text}")
                    return None

    except Exception as e:
        logger.info(f"[Kick Bot] ❌ Error getting token: {e}")
        return None


# -------------------------
# Kick Chat Messaging
# -------------------------


async def refresh_kick_oauth_token() -> bool:
    """
    Refresh the Kick OAuth token using the refresh_token.

    Returns:
        True if token was refreshed successfully, False otherwise
    """
    if not engine:
        logger.info("[Kick] ❌ No database connection for token refresh")
        return False

    try:
        # Get refresh token from kick_oauth_tokens table
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT refresh_token, kick_username FROM kick_oauth_tokens
                ORDER BY updated_at DESC LIMIT 1
            """
                )
            ).fetchone()

            if not result or not result[0]:
                logger.info("[Kick] ❌ No refresh token available in kick_oauth_tokens table")
                return False

            refresh_token = result[0]
            kick_username = result[1]

        logger.info(f"[Kick] 🔄 Attempting to refresh OAuth token for {kick_username}...")

        # Call Kick's token endpoint to refresh
        token_url = "https://id.kick.com/oauth/token"
        token_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": KICK_CLIENT_ID,
            "client_secret": KICK_CLIENT_SECRET,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=token_data, timeout=10) as response:
                if response.status == 200:
                    token_response = await response.json()
                    new_access_token = token_response.get("access_token")
                    new_refresh_token = token_response.get("refresh_token", refresh_token)
                    expires_in = token_response.get("expires_in", 3600)  # Default to 1 hour

                    if not new_access_token:
                        logger.info("[Kick] ❌ No access_token in refresh response")
                        return False

                    logger.info(f"[Kick] ✅ Access token refreshed (expires in {expires_in//3600}h)")
                    logger.info(f"[Kick] ℹ️  Refresh token is permanent per Kick API - will not be refreshed")

                    # Calculate when this ACCESS TOKEN will expire
                    # (Refresh token is permanent and never expires)
                    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

                    # Update tokens in kick_oauth_tokens table
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                            UPDATE kick_oauth_tokens
                            SET access_token = :access_token,
                                refresh_token = :refresh_token,
                                expires_at = :expires_at,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE kick_username = :kick_username
                        """
                            ),
                            {
                                "access_token": new_access_token,
                                "refresh_token": new_refresh_token,
                                "expires_at": expires_at,
                                "kick_username": kick_username,
                            },
                        )

                    logger.info(f"[Kick] ✅ OAuth token refreshed successfully for {kick_username}!")
                    return True
                else:
                    error_text = await response.text()
                    logger.info(f"[Kick] ❌ Token refresh failed (HTTP {response.status}): {error_text}")
                    return False

    except Exception as e:
        logger.info(f"[Kick] ❌ Error refreshing token: {e}")
        import traceback

        traceback.print_exc()
        return False


# -------------------------
# Kick listener functions
# -------------------------
# Browser-like headers for requests
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
    "Host": "kick.com",
    "Origin": "https://kick.com",
}


async def kick_chat_loop(channel_name: str, guild_id: int):
    """Connect to Kick's Pusher WebSocket to read ALL chat messages and events (no webhooks)."""
    global kick_chatroom_id_global

    # Get guild name for logging
    guild = bot.get_guild(guild_id)
    guild_name = guild.name if guild else str(guild_id)

    # Tag ALL logging from this long-running per-guild task with the server. This task
    # runs in its own asyncio Task (created at startup), so the context is scoped to it.
    set_server(guild_id, guild.name if guild else None)

    # Initialize per-guild tracking dictionaries if they don't exist
    if guild_id not in active_viewers_by_guild:
        active_viewers_by_guild[guild_id] = {}
    if guild_id not in recent_chatters_by_guild:
        recent_chatters_by_guild[guild_id] = {}
    if guild_id not in last_chat_activity_by_guild:
        last_chat_activity_by_guild[guild_id] = None

    # Track current channel and chatroom to detect changes
    current_channel = channel_name
    current_chatroom_id = None

    # Get guild settings
    guild_settings = get_guild_settings(guild_id)

    while True:
        try:
            # Refresh settings to detect changes
            guild_settings.refresh()

            # Check if channel has been updated in settings (allow hot-reload on reconnect)
            new_channel = guild_settings.kick_channel
            if new_channel and new_channel != current_channel:
                logger.info(f"🔄 Pusher: Channel changed from '{current_channel}' to '{new_channel}'")
                current_channel = new_channel

            # Use current_channel instead of the original channel_name
            channel_to_use = current_channel

            # Fetch channel data (chatroom ID and channel ID for subscriptions)
            chatroom_id = None
            channel_id = None

            # Check if chatroom ID is hardcoded in environment (bypass for Cloudflare issues)
            # Or configured in guild settings (per-guild configuration)
            chatroom_id_from_settings = guild_settings.kick_chatroom_id

            if KICK_CHATROOM_ID or chatroom_id_from_settings:
                chatroom_id = KICK_CHATROOM_ID or chatroom_id_from_settings
                kick_chatroom_ids[guild_id] = chatroom_id  # Store per-guild
                logger.info(f"Pusher: Using configured chatroom ID: {chatroom_id}")

                # Still try to fetch channel_id for subscription events
                try:
                    # Try official API first with OAuth token (if available)
                    access_token = None
                    try:
                        from utils.kick_oauth import get_kick_token_for_server

                        token_data = get_kick_token_for_server(engine, guild_id)
                        if token_data:
                            access_token = token_data.get("access_token")
                            logger.info(f"[Kick] Using OAuth token for API request")
                    except Exception as e:
                        logger.info(f"[Kick] Could not get OAuth token: {e}")

                    async with aiohttp.ClientSession() as session:
                        headers = {
                            "User-Agent": random.choice(USER_AGENTS),
                            "Accept": "application/json",
                        }

                        if access_token:
                            # Use official API with authentication
                            headers["Authorization"] = f"Bearer {access_token}"
                            url = f"https://api.kick.com/public/v1/channels"
                            params = {"slug": channel_to_use.lower()}
                            async with session.get(
                                url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)
                            ) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    channels = data.get("data", [])
                                    if channels:
                                        channel_data = channels[0]
                                        channel_id = str(channel_data.get("id", ""))
                                        # Try to get chatroom from official API
                                        if "chatroom" in channel_data:
                                            chatroom_id = str(channel_data["chatroom"].get("id", ""))
                                            logger.info(
                                                f"[Kick] ✅ Official API - chatroom ID: {chatroom_id}, channel ID: {channel_id}"
                                            )
                                        else:
                                            logger.info(
                                                f"[Kick] ✅ Official API - channel ID: {channel_id} (no chatroom in response)"
                                            )
                                else:
                                    logger.info(f"[Kick] ⚠️ Official API error: HTTP {response.status}")
                        else:
                            # Fallback to unofficial API without auth (may be blocked)
                            headers["Referer"] = "https://kick.com/"
                            url = f"https://kick.com/api/v2/channels/{channel_to_use}"
                            async with session.get(
                                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                            ) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    channel_id = str(data.get("id"))
                                    logger.info(f"[Kick] ✅ Fetched channel ID: {channel_id}")
                                else:
                                    logger.info(f"[Kick] ⚠️ Could not fetch channel ID (HTTP {response.status})")
                except Exception as e:
                    logger.info(f"[Kick] ⚠️ Error fetching channel ID: {e}")
            else:
                # Fetch full channel data to get both chatroom_id and channel_id
                chatroom_id = None
                channel_id = None

                try:
                    # Try Client Credentials OAuth first for channel_id (needed for subscription events on Pusher)
                    # Subscription events require authenticated Pusher channels
                    access_token = None

                    # Try Client Credentials flow (KICK_CLIENT_ID + KICK_CLIENT_SECRET)
                    if KICK_CLIENT_ID and KICK_CLIENT_SECRET:
                        try:
                            logger.info(f"[Kick] 🔐 Fetching token via Client Credentials for channel lookup...")
                            async with aiohttp.ClientSession() as session:
                                payload = {
                                    "grant_type": "client_credentials",
                                    "client_id": KICK_CLIENT_ID,
                                    "client_secret": KICK_CLIENT_SECRET,
                                }
                                async with session.post(
                                    "https://id.kick.com/oauth/token",
                                    data=payload,
                                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                                    timeout=aiohttp.ClientTimeout(total=10),
                                ) as resp:
                                    if resp.status == 200:
                                        token_data = await resp.json()
                                        access_token = token_data.get("access_token")
                                        logger.info(f"[Kick] ✅ Got Client Credentials token for channel lookup")
                                    else:
                                        logger.info(f"[Kick] ⚠️ Client Credentials failed: HTTP {resp.status}")
                        except Exception as e:
                            logger.info(f"[Kick] ⚠️ Could not get Client Credentials token: {e}")

                    # Fallback: Try getting streamer's OAuth token from database
                    if not access_token:
                        try:
                            from utils.kick_oauth import get_kick_token_for_server

                            token_data = get_kick_token_for_server(engine, guild_id)
                            if token_data:
                                access_token = token_data.get("access_token")
                                logger.info(f"[Kick] Using streamer OAuth token for channel lookup")
                        except Exception as e:
                            logger.info(f"[Kick] Could not get streamer OAuth token: {e}")

                    async with aiohttp.ClientSession() as session:
                        headers = {
                            "User-Agent": random.choice(USER_AGENTS),
                            "Accept": "application/json",
                        }

                        if access_token:
                            # Use official authenticated API (required for subscription event channels)
                            headers["Authorization"] = f"Bearer {access_token}"
                            url = f"https://api.kick.com/public/v1/channels"
                            params = {"slug": channel_to_use.lower()}
                            async with session.get(
                                url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=10)
                            ) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    channels = data.get("data", [])
                                    if channels:
                                        channel_data = channels[0]
                                        channel_id = str(channel_data.get("id", ""))
                                        # Get chatroom ID from authenticated API
                                        if "chatroom" in channel_data and channel_data["chatroom"]:
                                            chatroom_id = str(channel_data["chatroom"].get("id", ""))

                                        if chatroom_id:
                                            kick_chatroom_ids[guild_id] = chatroom_id
                                            logger.info(
                                                f"[Kick] ✅ Authenticated API - chatroom: {chatroom_id}, channel: {channel_id}"
                                            )
                                        else:
                                            logger.info(f"[Kick] ⚠️ Got channel ID {channel_id} but no chatroom ID")
                                    else:
                                        logger.info(f"[Kick] ⚠️ No channels found in API response")
                                elif response.status == 401:
                                    logger.info(f"[Kick] ⚠️ OAuth token expired (401), falling back to public API")
                                    # Fall through to public API below
                                    access_token = None
                                else:
                                    logger.info(f"[Kick] ⚠️ Authenticated API failed: HTTP {response.status}")

                        # Fallback to public API if no auth or auth failed
                        if not access_token or not chatroom_id:
                            logger.info(f"[Kick] 🔍 Falling back to public API (subscription events may not work)")
                            headers = {
                                "User-Agent": random.choice(USER_AGENTS),
                                "Accept": "application/json",
                                "Referer": "https://kick.com/",
                            }
                            url = f"https://kick.com/api/v2/channels/{channel_to_use}"
                            async with session.get(
                                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                            ) as response:
                                if response.status == 200:
                                    data = await response.json()
                                    chatroom_id = str(data.get("chatroom", {}).get("id"))
                                    channel_id = str(data.get("id"))
                                    kick_chatroom_ids[guild_id] = chatroom_id
                                    logger.info(
                                        f"[Kick] ✅ Public API - chatroom: {chatroom_id}, channel: {channel_id}"
                                    )
                                    logger.info(
                                        f"[Kick] ⚠️ Note: Subscription events may not work without authentication"
                                    )
                                elif response.status == 403:
                                    logger.info(f"[Kick] ❌ Cloudflare blocked request (403). SOLUTION:")
                                    logger.info(f"[Kick]    1. Link Kick account in dashboard (provides OAuth token)")
                                    logger.info(f"[Kick]    2. Or set KICK_CHATROOM_ID environment variable")
                                else:
                                    logger.info(f"[Kick] Failed to fetch channel data: HTTP {response.status}")
                except Exception as e:
                    logger.error(f"[Kick] Error fetching channel data: {e}")
                    import traceback

                    traceback.print_exc()

                if not chatroom_id:
                    logger.info(f"[Kick] Could not obtain chatroom id for {channel_to_use}. Retrying in 30s.")
                    await asyncio.sleep(30)
                    continue

            # Check if chatroom_id has changed (hot-reload support)
            if current_chatroom_id and chatroom_id != current_chatroom_id:
                logger.info(
                    f"🔄 Pusher: Chatroom ID changed from {current_chatroom_id} to {chatroom_id} - reconnecting..."
                )

            # Store current chatroom_id for change detection
            current_chatroom_id = chatroom_id

            logger.info(f"Pusher: Connecting to chatroom {chatroom_id} for channel {channel_to_use}...")

            # Build WebSocket URL matching successful connection pattern
            ws_url = (
                f"{KICK_CHAT_WS}"
                f"?protocol={PUSHER_CONFIG['protocol']}"
                f"&client=js"
                f"&version={PUSHER_CONFIG['version']}"
                f"&flash=false"
                f"&cluster={PUSHER_CONFIG['cluster']}"
            )
            logger.info(f"[Kick] Connecting to WebSocket: {ws_url}")

            # Verify TLS certificates. Pusher/Kick present valid public certs, and this
            # chat feed drives points/commands/clips, so a MITM here would mean forged
            # economy actions. create_default_context() enables verification + hostname
            # checking; do not disable it (fix cert issues via the trust store, not here).
            ssl_context = ssl.create_default_context()

            # Connect to Pusher WebSocket with headers
            async with websockets.connect(
                ws_url,
                max_size=None,
                ssl=ssl_context,
                additional_headers={
                    "User-Agent": BROWSER_HEADERS["User-Agent"],
                    "Origin": "https://kick.com",
                    "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
                },
            ) as ws:
                logger.info("[Kick] WebSocket connected, waiting for server response...")

                # Wait for connection established
                response = await ws.recv()
                logger.info(f"[Kick] Initial response: {response}")
                response_data = json.loads(response)

                # Handle connection errors
                if response_data.get("event") == "pusher:error":
                    error_data = response_data.get("data", {})
                    error_code = error_data.get("code")
                    error_message = error_data.get("message")
                    raise Exception(f"WebSocket error code {error_code}: {error_message}")

                # Handle successful connection
                if response_data.get("event") == "pusher:connection_established":
                    socket_details = json.loads(response_data["data"])
                    socket_id = socket_details["socket_id"]
                    logger.info(f"[Kick] Got socket_id: {socket_id}")

                    # Subscribe to the chatroom channel (for chat messages)
                    subscribe_msg = json.dumps(
                        {"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{chatroom_id}.v2"}}
                    )
                await ws.send(subscribe_msg)
                logger.info(f"[Kick] ✅ Subscribed to chatrooms.{chatroom_id}.v2")

                # Subscribe to channel events (for subscriptions, raids, follows, etc.)
                if channel_id:
                    channel_events_msg = json.dumps(
                        {"event": "pusher:subscribe", "data": {"auth": "", "channel": f"channel.{channel_id}"}}
                    )
                    await ws.send(channel_events_msg)
                    logger.info(f"[Kick] ✅ Subscribed to channel.{channel_id} (for subscription events)")
                else:
                    logger.info(f"[Kick] ⚠️ Channel ID not available - subscription events may not be received")

                    # Initialize last_chat_activity to assume stream is live when we connect
                last_chat_activity_by_guild[guild_id] = datetime.now(timezone.utc)
                logger.info(f"Pusher: Initialized chat activity tracking")

                # Listen for messages
                last_settings_check = datetime.now(timezone.utc)
                settings_check_interval = 30  # Check for settings changes every 30 seconds

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)

                        # Periodically check if chatroom_id has changed in settings
                        now = datetime.now(timezone.utc)
                        if (now - last_settings_check).total_seconds() >= settings_check_interval:
                            guild_settings.refresh()
                            new_chatroom_id = guild_settings.kick_chatroom_id or KICK_CHATROOM_ID
                            if new_chatroom_id and new_chatroom_id != current_chatroom_id:
                                logger.info(
                                    f"[Kick][Guild {guild_id}] 🔄 Detected chatroom ID change for THIS GUILD: {current_chatroom_id} → {new_chatroom_id}"
                                )
                                logger.info(
                                    f"[Kick][Guild {guild_id}] Breaking connection to reconnect with new chatroom (other guilds unaffected)..."
                                )
                                break  # Break inner loop to reconnect with new chatroom_id
                            last_settings_check = now

                        if not msg:
                            continue

                        # Parse Pusher message
                        try:
                            data = json.loads(msg)
                            event_type = data.get("event")

                            # DEBUG: Log ALL events to see what's coming through
                            if event_type not in ["pusher:ping", "pusher:pong"]:
                                logger.info(
                                    f"[KICK EVENT DEBUG] Type: {event_type}, Channel: {data.get('channel', 'N/A')}"
                                )
                                if data.get("data"):
                                    try:
                                        event_data = json.loads(data.get("data", "{}"))
                                        logger.info(
                                            f"[KICK EVENT DEBUG] Data keys: {list(event_data.keys())[:5]}"
                                        )  # Show first 5 keys
                                    except:
                                        logger.info(
                                            f"[KICK EVENT DEBUG] Raw data (first 100 chars): {str(data.get('data'))[:100]}"
                                        )

                            # DEBUG: Log ALL event types (except ping/pong and chat) to catch subscription events
                            if event_type and event_type not in [
                                "pusher:ping",
                                "pusher:pong",
                                "App\\Events\\ChatMessageEvent",
                            ]:
                                logger.info(f"[KICK EVENT] Type: {event_type}")
                                logger.info(
                                    f"[KICK EVENT] Data keys: {list(json.loads(data.get('data', '{}')).keys()) if data.get('data') else 'No data'}"
                                )

                            # Respond to ping
                            if event_type == "pusher:ping":
                                await ws.send(json.dumps({"event": "pusher:pong"}))
                                continue

                            # Skip chat messages if kickpython is handling them
                            # Only process subscription events from Pusher when kickpython is enabled
                            if KICK_USE_KICKPYTHON_WS and event_type == "App\\Events\\ChatMessageEvent":
                                continue  # kickpython handles chat messages

                            # Handle chat message (only when kickpython is disabled)
                            # Process ALL chat messages directly from Pusher (no webhooks)
                            if event_type == "App\\Events\\ChatMessageEvent":
                                event_data = json.loads(data.get("data", "{}"))
                                message_type = event_data.get("type")

                                # Check if this is a subscription-related message
                                is_sub_message = message_type and (
                                    "gift" in str(message_type).lower()
                                    or "subscription" in str(message_type).lower()
                                    or "sub" in str(message_type).lower()
                                )

                                # Process regular chat messages (not just subscriptions)
                                if not is_sub_message:
                                    # Regular chat message - process it!
                                    content_text = event_data.get("content", "")
                                    username = event_data.get("sender", {}).get("username", "")
                                    username_lower = username.lower() if username else ""

                                    if not username or not content_text:
                                        continue

                                    # Update watchtime tracking
                                    now = datetime.now(timezone.utc)
                                    last_chat_activity_by_guild[guild_id] = now

                                    guild_active_viewers = active_viewers_by_guild.get(guild_id, {})
                                    guild_active_viewers[username_lower] = now
                                    active_viewers_by_guild[guild_id] = guild_active_viewers

                                    guild_recent_chatters = recent_chatters_by_guild.get(guild_id, {})
                                    guild_recent_chatters[username_lower] = now
                                    recent_chatters_by_guild[guild_id] = guild_recent_chatters

                                    logger.info(f"💬 {username}: {content_text}")

                                    # Check for custom commands first (they get priority)
                                    commands_manager = getattr(bot, "custom_commands_managers", {}).get(
                                        guild_id
                                    ) or getattr(bot, "custom_commands_manager", None)
                                    if commands_manager:
                                        try:
                                            # Create a wrapper that includes guild_id
                                            async def send_message_with_guild(msg):
                                                return await send_kick_message(msg, guild_id=guild_id)

                                            original_guild_id = getattr(commands_manager, "discord_server_id", None)
                                            try:
                                                commands_manager.discord_server_id = guild_id
                                                commands_manager.send_message_callback = send_message_with_guild
                                                handled = await commands_manager.handle_message(
                                                    content_text,
                                                    username,
                                                    platform="kick",
                                                    display_name=username,
                                                )
                                            finally:
                                                commands_manager.discord_server_id = original_guild_id
                                            if handled:
                                                continue  # Command was handled, skip other processing
                                        except Exception as e:
                                            logger.error(f"⚠️ Error handling custom command: {e}")

                                    # Handle slot call commands (!call or !sr)
                                    content_stripped = content_text.strip()
                                    # Use per-guild slot call tracker
                                    tracker = (
                                        bot.slot_call_trackers_by_guild.get(guild_id)
                                        if hasattr(bot, "slot_call_trackers_by_guild")
                                        else None
                                    )
                                    if not tracker and hasattr(bot, "slot_call_tracker"):
                                        tracker = bot.slot_call_tracker  # Fallback for backwards compatibility

                                    if tracker and (
                                        content_stripped.startswith("!call") or content_stripped.startswith("!sr")
                                    ):
                                        # Check if slot requests are enabled first
                                        if not tracker.enabled:
                                            await send_kick_message(
                                                f"@{username} Slot requests are not open at the moment.",
                                                guild_id=guild_id,
                                            )
                                            continue

                                        # 🔒 SECURITY: Check if user is blacklisted (per-guild)
                                        is_blacklisted = False
                                        try:
                                            tracker_guild_id = (
                                                tracker.discord_server_id
                                                if hasattr(tracker, "discord_server_id")
                                                else guild_id
                                            )
                                            if tracker_guild_id:
                                                with engine.begin() as check_conn:
                                                    blacklist_check = check_conn.execute(
                                                        text(
                                                            """
                                                        SELECT 1 FROM slot_call_blacklist
                                                        WHERE kick_username = :username AND discord_server_id = :guild_id
                                                    """
                                                        ),
                                                        {"username": username_lower, "guild_id": tracker_guild_id},
                                                    ).fetchone()
                                                    is_blacklisted = blacklist_check is not None
                                        except Exception as e:
                                            logger.error(f"Error checking slot call blacklist: {e}")

                                        if is_blacklisted:
                                            logger.info(f"[Slot Call] Blocked blacklisted user: {username}")
                                            # No response sent to blacklisted users
                                        else:
                                            # Extract the slot call (everything after "!call " or "!sr ")
                                            # 🔒 SECURITY: Limit length to prevent abuse (200 chars max)
                                            if content_stripped.startswith("!call"):
                                                slot_call = content_stripped[5:].strip()[:200]  # Remove "!call"
                                            else:  # !sr
                                                slot_call = content_stripped[3:].strip()[:200]  # Remove "!sr"

                                            if slot_call and not require_link_ok(guild_id, username, "kick"):
                                                await send_kick_message(
                                                    f"@{username} Link your Kick account with the Link Account panel in Discord to request slots.",
                                                    guild_id=guild_id,
                                                )
                                            elif slot_call:  # Only process if there's actually a call
                                                await tracker.handle_slot_call(username, slot_call, platform="kick")
                                            else:
                                                # Send usage message when no content is provided (only to non-blacklisted users)
                                                try:
                                                    await send_kick_message(
                                                        f"@{username} Please specify a slot!", guild_id=guild_id
                                                    )
                                                    logger.info(f"[Slot Call] Sent usage instructions to {username}")
                                                except Exception as e:
                                                    logger.info(f"Failed to send usage message to {username}: {e}")

                                    # Handle !raffle command
                                    if content_stripped.lower() == "!raffle":
                                        raffle_message = build_raffle_promo(get_guild_settings(guild_id))
                                        # Send message to Kick chat using the API
                                        await send_kick_message(raffle_message, guild_id=guild_id)

                                    # Handle !gtb (Guess the Balance) command
                                    elif content_stripped.lower().startswith("!gtb"):
                                        logger.info(f"[GTB] {username} issued !gtb command")
                                        if not require_link_ok(guild_id, username, "kick"):
                                            await _send_kick_message_raw(
                                                f"@{username} Link your Kick account with the Link Account panel in Discord to guess.",
                                                guild_id=guild_id,
                                            )
                                            gtb_mgr = None
                                        else:
                                            # Use per-guild GTB manager
                                            gtb_mgr = (
                                                bot.gtb_managers_by_guild.get(guild_id)
                                                if hasattr(bot, "gtb_managers_by_guild")
                                                else None
                                            )
                                        if gtb_mgr:
                                            gtb_parts = content_stripped.split(maxsplit=1)
                                            if len(gtb_parts) == 2:
                                                amount_str = gtb_parts[1]
                                                amount = parse_amount(amount_str)

                                                if amount is not None:
                                                    success, message = gtb_mgr.add_guess(username, amount)
                                                    if success:
                                                        # Legacy Kick-only loop → reply on Kick directly.
                                                        response = f"@{username} {message} Good luck! 🎰"
                                                        await _send_kick_message_raw(response, guild_id=guild_id)
                                                        logger.info(f"[GTB] {username} guessed ${amount:,.2f}")
                                                    else:
                                                        await _send_kick_message_raw(
                                                            f"@{username} {message}", guild_id=guild_id
                                                        )
                                                        logger.info(f"[GTB] Failed guess from {username}: {message}")
                                                else:
                                                    await _send_kick_message_raw(
                                                        f"@{username} Invalid amount. Use: !gtb <amount> (e.g., !gtb 1234.56)",
                                                        guild_id=guild_id,
                                                    )
                                            else:
                                                await _send_kick_message_raw(
                                                    f"@{username} Usage: !gtb <amount> (e.g., !gtb 1234.56)",
                                                    guild_id=guild_id,
                                                )
                                        else:
                                            logger.info(f"[GTB] GTB manager not found for guild {guild_id}")
                                            await _send_kick_message_raw(
                                                f"@{username} GTB system not initialized", guild_id=guild_id
                                            )

                                    # !current / !slot — what slot is the streamer playing right
                                    # now? Same read as the primary chain's branch: latest
                                    # current_slot_history row (extension-pushed). Duplicated here
                                    # because this Pusher-loop path dispatches its own commands.
                                    if content_stripped.lower() in ("!current", "!slot"):
                                        try:
                                            with engine.connect() as conn:
                                                result = conn.execute(
                                                    text(
                                                        """
                                                    SELECT slot_name, provider FROM current_slot_history
                                                    WHERE discord_server_id = :guild_id
                                                    ORDER BY started_at DESC, id DESC
                                                    LIMIT 1
                                                """
                                                    ),
                                                    {"guild_id": guild_id},
                                                ).fetchone()
                                            if result and result[0]:
                                                _cs_suffix = f" ({result[1]})" if result[1] else ""
                                                await send_kick_message(
                                                    f"@{username} Currently playing: {result[0]}{_cs_suffix}",
                                                    guild_id=guild_id,
                                                )
                                            else:
                                                await send_kick_message(
                                                    f"@{username} No slot tracked yet.",
                                                    guild_id=guild_id,
                                                )
                                        except Exception as e:
                                            logger.info(f"❌ Error fetching current slot: {e}")

                                    # Handle !clip command - Create a clip of the livestream
                                    if content_stripped.lower().startswith("!clip"):
                                        # Get clip duration from bot_settings (default 30 seconds)
                                        clip_duration = 30
                                        try:
                                            with engine.connect() as conn:
                                                result = conn.execute(
                                                    text(
                                                        """
                                                    SELECT value FROM bot_settings WHERE key = 'clip_duration' AND discord_server_id = :guild_id
                                                """
                                                    ),
                                                    {"guild_id": guild_id},
                                                ).fetchone()
                                                if result:
                                                    clip_duration = int(result[0])
                                        except Exception as e:
                                            logger.info(f"[Clip] Using default duration, couldn't load from DB: {e}")

                                        # Everything after !clip is the title
                                        clip_title = content_stripped[5:].strip()  # Remove "!clip" and trim

                                        if not clip_title:
                                            # No title provided - generate default
                                            timestamp = datetime.now().strftime("%b %d, %Y %H:%M")
                                            clip_title = f"Clip by {username} - {timestamp}"

                                        # Use background task for clip creation (non-blocking)
                                        logger.info(
                                            f"[Clip] {username} requested a clip ({clip_duration}s) - Title: {clip_title}"
                                        )

                                        async def create_clip_background(user: str, duration: int, title: str):
                                            """Background task to create clip via Dashboard API"""
                                            try:
                                                # Refresh settings from database to get latest values
                                                bot_settings.refresh()

                                                # Derived per-server base first; the stored
                                                # dashboard_url setting is a stale-prone fallback.
                                                dashboard_url = (
                                                    get_server_base_url(engine, guild_id) or bot_settings.dashboard_url
                                                )
                                                # Env-controlled system secret first;
                                                # DB value is a legacy fallback.
                                                api_key = get_clip_api_key(bot_settings.bot_api_key)

                                                logger.info(f"[Clip] DEBUG - dashboard_url resolved: '{dashboard_url}'")
                                                logger.info(f"[Clip] DEBUG - bot_api_key exists: {bool(api_key)}")

                                                if not dashboard_url:
                                                    logger.info(
                                                        f"[Clip] ❌ Dashboard URL not configured in bot_settings table"
                                                    )
                                                    await send_kick_message(
                                                        f"@{user} Clip service not configured - contact admin!"
                                                    )
                                                    return

                                                # Create clip via Dashboard API
                                                async with aiohttp.ClientSession() as http_session:
                                                    headers = {"Content-Type": "application/json", "X-API-Key": api_key}
                                                    payload = {
                                                        "channel": KICK_CHANNEL,
                                                        "duration": duration,
                                                        "username": user,
                                                        "title": title,
                                                    }

                                                    async with http_session.post(
                                                        f"{dashboard_url}/api/clips/create",
                                                        json=payload,
                                                        headers=headers,
                                                        timeout=aiohttp.ClientTimeout(total=60),
                                                    ) as response:
                                                        clip_result = await response.json()

                                                        logger.info(f"[Clip] Dashboard API response: {clip_result}")

                                                        if response.status == 200 and clip_result.get("success"):
                                                            # Success - extract clip URL
                                                            clip_url = clip_result.get("clip_url", "")
                                                            clip_filename = clip_result.get("filename", "")
                                                            file_size_mb = clip_result.get(
                                                                "file_size_mb",
                                                                clip_result.get("file_size", 0) / 1024 / 1024,
                                                            )
                                                            actual_duration = clip_result.get("duration", duration)

                                                            await send_kick_message(
                                                                f"@{user} Your clip is ready! ({actual_duration}s, {file_size_mb:.1f}MB) {clip_url}"
                                                            )
                                                            logger.info(
                                                                f"[Clip] ✅ Clip created for {user}: {clip_filename}"
                                                            )

                                                            # Save clip data to database
                                                            try:
                                                                with engine.connect() as conn:
                                                                    conn.execute(
                                                                        text(
                                                                            """
                                                                        INSERT INTO clips (kick_username, clip_duration, clip_url, filename, file_size)
                                                                        VALUES (:username, :duration, :url, :filename, :file_size)
                                                                    """
                                                                        ),
                                                                        {
                                                                            "username": user,
                                                                            "duration": actual_duration,
                                                                            "url": clip_url,
                                                                            "filename": clip_filename,
                                                                            "file_size": clip_result.get(
                                                                                "file_size", 0
                                                                            ),
                                                                        },
                                                                    )
                                                                    conn.commit()
                                                                logger.info(f"[Clip] 💾 Saved clip data for {user}")
                                                            except Exception as db_err:
                                                                logger.info(
                                                                    f"[Clip] ⚠️ Failed to save clip to DB: {db_err}"
                                                                )

                                                            # Post clip embed to Discord channel if configured
                                                            try:
                                                                clip_channel_id = None
                                                                with engine.connect() as conn:
                                                                    result = conn.execute(
                                                                        text(
                                                                            """
                                                                        SELECT value FROM bot_settings WHERE key = 'clip_channel_id' AND discord_server_id = :guild_id
                                                                    """
                                                                        ),
                                                                        {"guild_id": guild_id},
                                                                    ).fetchone()
                                                                    if result and result[0]:
                                                                        clip_channel_id = int(result[0])

                                                                if clip_channel_id:
                                                                    discord_channel = bot.get_channel(clip_channel_id)
                                                                    if discord_channel:
                                                                        clip_embed = discord.Embed(
                                                                            title=f"🎬 {title}",
                                                                            description=f"New clip created by **{user}** in Kick chat!",
                                                                            color=0x53FC18,
                                                                            url=(
                                                                                clip_url
                                                                                if clip_url.startswith("http")
                                                                                else None
                                                                            ),
                                                                            timestamp=datetime.now(timezone.utc),
                                                                        )
                                                                        clip_embed.add_field(
                                                                            name="Duration",
                                                                            value=f"{actual_duration} seconds",
                                                                            inline=True,
                                                                        )
                                                                        clip_embed.add_field(
                                                                            name="Created by", value=user, inline=True
                                                                        )
                                                                        clip_embed.add_field(
                                                                            name="Size",
                                                                            value=f"{file_size_mb:.1f} MB",
                                                                            inline=True,
                                                                        )
                                                                        if clip_url:
                                                                            clip_embed.add_field(
                                                                                name="🔗 Watch Clip",
                                                                                value=(
                                                                                    f"[Click here]({clip_url})"
                                                                                    if clip_url.startswith("http")
                                                                                    else clip_url
                                                                                ),
                                                                                inline=False,
                                                                            )
                                                                        clip_embed.set_footer(
                                                                            text=f"Kick Channel: {KICK_CHANNEL}"
                                                                        )
                                                                        await discord_channel.send(embed=clip_embed)
                                                                        logger.info(
                                                                            f"[Clip] 📢 Posted clip to Discord channel {clip_channel_id}"
                                                                        )
                                                            except Exception as discord_err:
                                                                logger.info(
                                                                    f"[Clip] ⚠️ Failed to post clip to Discord: {discord_err}"
                                                                )
                                                        else:
                                                            # Handle error from Dashboard API
                                                            error_type = clip_result.get("error", "unknown")
                                                            error_msg = clip_result.get("message", "Unknown error")

                                                            if (
                                                                error_type == "not_live"
                                                                or error_type == "not_recording"
                                                            ):
                                                                await send_kick_message(
                                                                    f"@{user} Stream must be live to create clips!"
                                                                )
                                                            elif (
                                                                error_type == "no_segments" or error_type == "no_buffer"
                                                            ):
                                                                await send_kick_message(
                                                                    f"@{user} Buffer still loading - try again in 30 seconds!"
                                                                )
                                                            else:
                                                                await send_kick_message(
                                                                    f"@{user} Couldn't create clip - {error_msg}"
                                                                )

                                                            logger.info(
                                                                f"[Clip] ❌ Clip creation failed for {user}: {error_type}"
                                                            )

                                            except Exception as e:
                                                logger.info(f"[Clip] ❌ Background clip error: {e}")
                                                await send_kick_message(f"@{user} Clip failed - try again later!")

                                        # Start background task (non-blocking)
                                        asyncio.create_task(create_clip_background(username, clip_duration, clip_title))

                            # Handle subscription events (both regular and gifted)
                            # Kick may use different event types for subs
                            subscription_event_types = [
                                "App\\Events\\GiftedSubscriptionsEvent",
                                "App\\Events\\LuckyUsersWhoGotGiftSubscriptionsEvent",
                                "App\\Events\\SubscriptionEvent",
                                "App\\Events\\ChatMessageEvent",  # Sometimes subs come as special chat messages
                            ]

                            # DEBUG: Check if event type should match
                            if event_type and "Subscription" in event_type:
                                logger.info(f"[SUB CHECK] Event type: {event_type}")
                                logger.info(f"[SUB CHECK] Event type repr: {repr(event_type)}")
                                logger.info(f"[SUB CHECK] In list: {event_type in subscription_event_types}")
                                logger.info(f"[SUB CHECK] Expected: {subscription_event_types[1]}")
                                logger.info(f"[SUB CHECK] Expected repr: {repr(subscription_event_types[1])}")
                                logger.info(f"[SUB CHECK] Match: {event_type == subscription_event_types[1]}")

                            if event_type in subscription_event_types:
                                logger.info(f"[SUB HANDLER] Entering subscription handler for event type: {event_type}")
                                logger.info(
                                    f"[SUB HANDLER] gifted_sub_tracker initialized: {gifted_sub_tracker is not None}"
                                )

                                event_data = json.loads(data.get("data", "{}"))

                                # DEBUG: Log subscription events
                                if event_type == "App\\Events\\LuckyUsersWhoGotGiftSubscriptionsEvent":
                                    logger.info(f"[SUB DEBUG] LuckyUsersWhoGotGiftSubscriptionsEvent detected!")
                                    logger.info(f"[SUB DEBUG] Gifter: {event_data.get('gifter_username')}")
                                    logger.info(f"[SUB DEBUG] Recipients: {event_data.get('usernames')}")
                                    logger.info(f"[SUB DEBUG] Full event data: {json.dumps(event_data, indent=2)}")

                                # DEBUG: Check what ChatMessageEvent contains
                                if event_type == "App\\Events\\ChatMessageEvent":
                                    msg_type = event_data.get("type")
                                    if msg_type and msg_type != "message":
                                        logger.info(f"[SUB DEBUG] ChatMessageEvent type: {msg_type}")
                                        logger.info(f"[SUB DEBUG] Event data: {json.dumps(event_data, indent=2)[:500]}")

                                # Check if this is any type of subscription
                                message_type = event_data.get("type")
                                is_subscription = (
                                    event_type == "App\\Events\\LuckyUsersWhoGotGiftSubscriptionsEvent"
                                    or event_type == "App\\Events\\GiftedSubscriptionsEvent"
                                    or event_type == "App\\Events\\SubscriptionEvent"
                                    or "gift" in str(message_type).lower()
                                    or "subscription" in str(message_type).lower()
                                    or "sub" in str(message_type).lower()
                                    or event_data.get("gifted_usernames") is not None
                                    or event_data.get("usernames") is not None  # LuckyUsersWhoGotGiftSubscriptionsEvent
                                    or event_data.get("gifter_username")
                                    is not None  # LuckyUsersWhoGotGiftSubscriptionsEvent
                                    or event_data.get("gift_count") is not None
                                    or event_data.get("months") is not None  # Regular subs often have months field
                                )

                                if is_subscription:
                                    logger.info(f"🎁 Pusher: Subscription detected! Type: {message_type}")
                                    logger.info(f"Event data: {json.dumps(event_data, indent=2)[:800]}")
                                else:
                                    logger.info(f"Pusher: NOT a subscription. message_type: {message_type}")

                                if is_subscription and gifted_sub_tracker:
                                    logger.info(f"🎁 Processing subscription with raffle tracker")
                                    # Handle any subscription event (gifted or regular)
                                    result = await gifted_sub_tracker.handle_gifted_sub_event(event_data)
                                    logger.info(f"Raffle result: {result}")

                                    if result["status"] == "success":
                                        sub_type = "gifted" if result.get("gift_count", 1) > 1 else "subscribed"
                                        logger.info(
                                            f"🎁 {result['gifter']} {sub_type} → +{result['tickets_awarded']} tickets"
                                        )
                                    elif result["status"] == "not_linked":
                                        logger.info(f"🎁 {result['kick_name']} subscribed but account not linked")
                                    elif result["status"] == "duplicate":
                                        # Already processed, silent skip
                                        pass
                                    else:
                                        logger.info(f"⚠️ Failed to process gifted sub: {result}")
                                elif is_subscription and not gifted_sub_tracker:
                                    logger.info(f"⚠️ Subscription detected but raffle tracker is None!")

                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            logger.error(f"[Kick] Error parsing message: {e}")

                    except asyncio.TimeoutError:
                        # Check for settings changes on timeout
                        guild_settings.refresh()
                        new_chatroom_id = guild_settings.kick_chatroom_id or KICK_CHATROOM_ID
                        if new_chatroom_id and new_chatroom_id != current_chatroom_id:
                            logger.info(
                                f"[Kick][Guild {guild_id}] 🔄 Detected chatroom ID change during timeout for THIS GUILD: {current_chatroom_id} → {new_chatroom_id}"
                            )
                            logger.info(
                                f"[Kick][Guild {guild_id}] Breaking connection to reconnect with new chatroom (other guilds unaffected)..."
                            )
                            break  # Break inner loop to reconnect

                        # Send ping to keep connection alive
                        try:
                            await ws.send(json.dumps({"event": "pusher:ping"}))
                        except:
                            break

        except websockets.exceptions.WebSocketException as e:
            logger.info(f"[Kick] WebSocket error: {e}. Reconnecting in 10s.")
            await asyncio.sleep(10)
        except Exception as e:
            logger.info(f"[Kick] Connection error: {e}. Reconnecting in 10s.")
            await asyncio.sleep(10)


# -------------------------
# Point Reward System
# -------------------------
def dedupe_active_users_by_person(conn, active_users: dict, guild_id) -> dict:
    """Collapse the active-viewer set to one entry per linked person so a viewer
    watching a simultaneous Kick+Twitch stream can't earn watchtime/points twice.

    A person's platform accounts share one discord_id in `links`. For each active
    username we find its discord_id, then for each linked person we accrue under a
    single CANONICAL username: the alphabetically-first of ALL that person's linked
    usernames (not just the ones active this interval), so the same person always
    accrues to the same row every interval regardless of which platform they chatted
    on. Usernames with no discord_id (unlinked viewers) are kept as-is — they can't
    be deduped cross-platform and continue to earn per-platform.

    We also carry the canonical username's last_seen forward even when the person was
    only active under a different platform account this interval, so their accrual
    doesn't stall. Returns {username_lower: last_seen}, deduped.
    """
    if not active_users:
        return active_users

    names = list(active_users.keys())
    # Map active usernames -> discord_id.
    active_rows = conn.execute(
        text(
            """
            SELECT LOWER(kick_name) AS uname, discord_id
            FROM links
            WHERE discord_server_id = :sid AND LOWER(kick_name) = ANY(:names)
            """
        ),
        {"sid": guild_id, "names": names},
    ).fetchall()
    username_to_did = {row[0]: row[1] for row in active_rows if row[1] is not None}

    # For the people who are active, fetch ALL their linked usernames so the
    # canonical pick is stable across intervals (independent of which account
    # was active now).
    dids = list({did for did in username_to_did.values()})
    canonical_for_did = {}
    if dids:
        all_rows = conn.execute(
            text(
                """
                SELECT discord_id, LOWER(kick_name) AS uname
                FROM links
                WHERE discord_server_id = :sid AND discord_id = ANY(:dids)
                """
            ),
            {"sid": guild_id, "dids": dids},
        ).fetchall()
        per_person = {}
        for did, uname in all_rows:
            if uname:
                per_person.setdefault(did, []).append(uname)
        canonical_for_did = {did: min(unames) for did, unames in per_person.items()}

    kept = {}
    handled_person = set()
    for user in sorted(active_users.keys()):
        did = username_to_did.get(user)
        if did is None:
            kept[user] = active_users[user]  # unlinked: keep, earns per-platform
            continue
        if did in handled_person:
            continue  # another platform account of the same person this interval — drop
        handled_person.add(did)
        canonical = canonical_for_did.get(did, user)
        # Accrue under the canonical username, using this person's most-recent
        # last_seen across whichever account(s) they were active on this interval.
        person_last_seen = max(ls for u, ls in active_users.items() if username_to_did.get(u) == did)
        kept[canonical] = person_last_seen
    return kept


async def award_points_for_watchtime(active_usernames: list, guild_id: Optional[int] = None):
    """
    Award points to users based on their new watchtime.
    Similar to raffle ticket system but tracks points separately.
    Only awards points for NEW watchtime since last conversion.

    Args:
        active_usernames: List of usernames to award points to
        guild_id: Discord guild/server ID for multi-server support
    """
    try:
        # Multiserver: Require guild_id parameter
        if not guild_id:
            logger.info("[Points] ⚠️ No guild_id provided for award_points")
            return
        server_id = guild_id

        # Get point settings from database
        points_per_5min = 1  # Default: 1 point per 5 minutes
        sub_points_per_5min = 2  # Default: 2 points per 5 minutes for subs

        with engine.connect() as conn:
            # Load settings (multiserver: filter by server_id)
            result = conn.execute(
                text(
                    """
                SELECT key, value FROM point_settings
                WHERE key IN ('points_per_5min', 'sub_points_per_5min')
                AND (discord_server_id = :sid OR discord_server_id IS NULL)
                ORDER BY discord_server_id NULLS FIRST
            """
                ),
                {"sid": server_id},
            ).fetchall()

            for key, value in result:
                if key == "points_per_5min":
                    points_per_5min = int(value)
                elif key == "sub_points_per_5min":
                    sub_points_per_5min = int(value)

        if points_per_5min == 0 and sub_points_per_5min == 0:
            return  # Points system disabled

        with engine.begin() as conn:
            for username in active_usernames:
                try:
                    # Get current total watchtime for user (multiserver: filter by server_id)
                    result = conn.execute(
                        text(
                            """
                        SELECT minutes FROM watchtime
                        WHERE username = :u AND discord_server_id = :sid
                    """
                        ),
                        {"u": username, "sid": server_id},
                    ).fetchone()

                    if not result:
                        continue

                    total_minutes = result[0]

                    # Get how many minutes have already been converted to points (multiserver)
                    converted_result = conn.execute(
                        text(
                            """
                        SELECT COALESCE(SUM(minutes_converted), 0)
                        FROM points_watchtime_converted
                        WHERE kick_username = :u AND discord_server_id = :sid
                    """
                        ),
                        {"u": username, "sid": server_id},
                    ).fetchone()

                    minutes_already_converted = converted_result[0] if converted_result else 0

                    # Get discord_id to write to permanent log later
                    link_result = conn.execute(
                        text(
                            """
                        SELECT discord_id FROM links
                        WHERE LOWER(kick_name) = LOWER(:u) AND discord_server_id = :sid
                    """
                        ),
                        {"u": username, "sid": server_id},
                    ).fetchone()
                    discord_id = link_result[0] if link_result else None

                    # Calculate new minutes since last conversion
                    new_minutes = total_minutes - minutes_already_converted

                    # Only award if we have at least 5 new minutes
                    if new_minutes >= 5:
                        # Calculate points (5 minutes = 1 interval)
                        intervals = int(new_minutes // 5)
                        minutes_to_convert = intervals * 5

                        # TODO: Check if user is subscriber for bonus points
                        # For now, use regular points rate
                        points_to_award = intervals * points_per_5min

                        if points_to_award > 0:
                            # discord_id already retrieved above for verification

                            # Update or insert user points (multiserver: composite PK with discord_server_id)
                            conn.execute(
                                text(
                                    """
                                INSERT INTO user_points (kick_username, discord_id, points, total_earned, discord_server_id, last_updated)
                                VALUES (:u, :d, :p, :p, :sid, CURRENT_TIMESTAMP)
                                ON CONFLICT(kick_username, discord_server_id) DO UPDATE SET
                                    points = user_points.points + :p,
                                    total_earned = user_points.total_earned + :p,
                                    discord_id = COALESCE(:d, user_points.discord_id),
                                    last_updated = CURRENT_TIMESTAMP
                            """
                                ),
                                {"u": username, "d": discord_id, "p": points_to_award, "sid": server_id},
                            )

                            # Log the conversion (multiserver: add discord_server_id)
                            conn.execute(
                                text(
                                    """
                                INSERT INTO points_watchtime_converted
                                (kick_username, minutes_converted, points_awarded, discord_server_id)
                                VALUES (:u, :m, :p, :sid)
                            """
                                ),
                                {"u": username, "m": minutes_to_convert, "p": points_to_award, "sid": server_id},
                            )

                            # ALSO log to permanent watchtime_conversion_logs (NEVER deleted)
                            if discord_id:
                                conn.execute(
                                    text(
                                        """
                                    INSERT INTO watchtime_conversion_logs
                                        (discord_id, kick_name, discord_server_id, raffle_minutes, points_minutes, converted_at)
                                    VALUES
                                        (:discord_id, :kick_name, :server_id, 0, :minutes, NOW())
                                """
                                    ),
                                    {
                                        "discord_id": discord_id,
                                        "kick_name": username,
                                        "server_id": server_id,
                                        "minutes": minutes_to_convert,
                                    },
                                )

                            if watchtime_debug_enabled:
                                logger.info(
                                    f"[Points] ✅ {username}: +{points_to_award} points ({minutes_to_convert} min converted)"
                                )

                except Exception as e:
                    logger.info(f"[Points] ⚠️ Error awarding points to {username}: {e}")
                    continue

    except Exception as e:
        logger.info(f"[Points] ⚠️ Error in points award task: {e}")


# -------------------------
# Watchtime updater task
# -------------------------
@tasks.loop(seconds=WATCH_INTERVAL_SECONDS)
async def update_watchtime_task():
    """Update watchtime for active viewers (only when tracking is enabled and stream has real activity)."""
    global stream_tracking_enabled, last_chat_activity, recent_chatters

    try:
        # Check if tracking is enabled by admin
        if not stream_tracking_enabled:
            return

        now = datetime.now(timezone.utc)
        minutes_to_add = WATCH_INTERVAL_SECONDS / 60

        # Multiserver: Process each guild independently
        for guild in bot.guilds:
            server_id = guild.id

            # Tag this guild's iteration's logging with the server name.
            set_server(server_id, guild.name)

            # Memory: evict stale keys so these accumulators don't grow unbounded with
            # every unique username that ever chats. They are only ever read as a
            # recent-window snapshot (active viewers within WATCH_INTERVAL_SECONDS+60,
            # chatters within CHAT_ACTIVITY_WINDOW_MINUTES), so older entries are dead
            # weight. Runs before the early-continue live checks so offline guilds -
            # whose accumulators would otherwise never shrink - get pruned too.
            _prune_now = datetime.now(timezone.utc)
            _viewers = active_viewers_by_guild.get(server_id)
            if _viewers:
                for _u in [
                    u for u, t in _viewers.items() if (_prune_now - t).total_seconds() >= (WATCH_INTERVAL_SECONDS + 60)
                ]:
                    del _viewers[_u]
                if not _viewers:
                    active_viewers_by_guild.pop(server_id, None)
            _chatters = recent_chatters_by_guild.get(server_id)
            if _chatters:
                _chat_max_age = timedelta(minutes=CHAT_ACTIVITY_WINDOW_MINUTES)
                for _u in [u for u, t in _chatters.items() if (_prune_now - t) >= _chat_max_age]:
                    del _chatters[_u]
                if not _chatters:
                    recent_chatters_by_guild.pop(server_id, None)

            # 🔒 SECURITY: Per-guild multi-factor stream-live detection
            if not tracking_force_override:
                # Check last chat activity for THIS GUILD
                guild_last_activity = last_chat_activity_by_guild.get(server_id)
                if guild_last_activity is None:
                    if watchtime_debug_enabled:
                        logger.debug(f"[Security] No chat activity detected yet - skipping")
                    continue

                # Check 1: Recent chat activity (within last 10 minutes)
                time_since_last_chat = (now - guild_last_activity).total_seconds() / 60
                if time_since_last_chat > CHAT_ACTIVITY_WINDOW_MINUTES:
                    if watchtime_debug_enabled:
                        logger.info(
                            f"[Security] No chat activity for {time_since_last_chat:.1f} minutes - stream likely offline"
                        )
                    continue

                # Check 2: Count unique chatters in the recent window FOR THIS GUILD
                chat_cutoff = now - timedelta(minutes=CHAT_ACTIVITY_WINDOW_MINUTES)
                guild_recent_chatters = recent_chatters_by_guild.get(server_id, {})
                active_chatters = {
                    username: timestamp
                    for username, timestamp in guild_recent_chatters.items()
                    if timestamp >= chat_cutoff
                }

                unique_chatter_count = len(active_chatters)

                if unique_chatter_count < MIN_UNIQUE_CHATTERS:
                    if watchtime_debug_enabled:
                        logger.info(
                            f"[Security] Only {unique_chatter_count} unique chatter(s) in last {CHAT_ACTIVITY_WINDOW_MINUTES} min (need {MIN_UNIQUE_CHATTERS})"
                        )
                        logger.info(f"[Security] Stream might be offline or being farmed - skipping watchtime update")
                        logger.info(
                            f"[Security] Tip: Use '!tracking force on' to override if stream has low chat activity"
                        )
                    continue

                if watchtime_debug_enabled:
                    logger.info(
                        f"[Security] ✅ Stream appears live: {unique_chatter_count} unique chatters in last {CHAT_ACTIVITY_WINDOW_MINUTES} min"
                    )
            else:
                if watchtime_debug_enabled:
                    logger.info(f"[Security] Force override enabled - skipping multi-factor live detection")

            # Get active viewers for this guild
            guild_active_viewers = active_viewers_by_guild.get(server_id, {})

            # Filter to active users (seen in last 5 minutes)
            active_users = {
                user: last_seen
                for user, last_seen in list(guild_active_viewers.items())
                if (datetime.now(timezone.utc) - last_seen).total_seconds() < (WATCH_INTERVAL_SECONDS + 60)
            }

            if not active_users:
                continue

            # Update all active users for this guild
            with engine.begin() as conn:
                # Collapse dual-platform viewers (Kick + Twitch) to one entry per
                # linked person so simultaneous cross-platform watching can't
                # double-count watchtime (and therefore points). Unlinked viewers
                # pass through and keep earning per-platform.
                active_users = dedupe_active_users_by_person(conn, active_users, server_id)

                for user, last_seen in active_users.items():
                    try:
                        conn.execute(
                            text(
                                """
                            INSERT INTO watchtime (username, minutes, last_active, discord_server_id)
                            VALUES (:u, :m, :t, :sid)
                            ON CONFLICT(username, discord_server_id) DO UPDATE SET
                                minutes = watchtime.minutes + :m,
                                last_active = :t
                        """
                            ),
                            {"u": user, "m": minutes_to_add, "t": last_seen.isoformat(), "sid": server_id},
                        )
                    except Exception as e:
                        logger.error(f"⚠️ Error updating watchtime for {user}: {e}")
                        continue  # Skip this user but continue with others

            # Award points for new watchtime (runs after watchtime update).
            # active_users is already deduped to one canonical username per person.
            await award_points_for_watchtime(list(active_users.keys()), guild_id=server_id)

    except Exception as e:
        logger.error(f"⚠️ Error in watchtime update task: {e}")
        import traceback

        traceback.print_exc()


@update_watchtime_task.before_loop
async def before_watchtime_task():
    """Wait for bot to be ready before starting watchtime updates."""
    await bot.wait_until_ready()


@update_watchtime_task.error
async def update_watchtime_task_error(error):
    """Handle errors in the watchtime task loop."""
    logger.error(f"❌ Watchtime task encountered an error: {error}")
    import traceback

    traceback.print_exc()


# -------------------------
# Role updater task
# -------------------------
@tasks.loop(seconds=ROLE_UPDATE_INTERVAL_SECONDS)
async def update_roles_task():
    """Update Discord roles based on watchtime."""
    try:
        # Multiserver: Update roles for all guilds
        for guild in bot.guilds:
            set_server(guild.id, guild.name)  # tag this guild's iteration logging
            try:
                # Validate bot permissions
                if not guild.me.guild_permissions.manage_roles:
                    logger.warning(f"⚠️ Bot lacks manage_roles permission!")
                    continue

                # Load current role configuration from database
                current_roles = load_watchtime_roles()

                # Cache role objects and validate they exist
                role_cache = {}
                for role_info in current_roles:
                    role = discord.utils.get(guild.roles, name=role_info["name"])
                    if not role:
                        logger.warning(f"⚠️ Role {role_info['name']} not found in server!")
                        continue
                    role_cache[role_info["name"]] = role

                # Get linked users with watchtime for this guild
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            """
                        SELECT l.discord_id, w.minutes, l.kick_name
                        FROM links l
                        JOIN watchtime w ON l.kick_name = w.username AND l.discord_server_id = w.discord_server_id
                        WHERE l.discord_server_id = :sid
                    """
                        ),
                        {"sid": guild.id},
                    ).fetchall()

                # Update roles for each user
                for discord_id, minutes, kick_name in rows:
                    member = guild.get_member(int(discord_id))
                    if not member:
                        continue

                    # Assign all eligible roles
                    for role_info in current_roles:
                        role = role_cache.get(role_info["name"])
                        if role and minutes >= role_info["minutes"] and role not in member.roles:
                            try:
                                await member.add_roles(role, reason=f"Reached {role_info['minutes']} min watchtime")
                                logger.info(f"Assigned {role.name} to {member.display_name} ({kick_name})")

                                # Send DM notification to user
                                try:
                                    hours = minutes / 60
                                    embed = discord.Embed(
                                        title="🎉 New Role Unlocked!",
                                        description=f"Congratulations! You've earned the **{role.name}** role!",
                                        color=0x53FC18,
                                    )
                                    embed.add_field(
                                        name="Your Watchtime",
                                        value=f"{minutes:.0f} minutes ({hours:.1f} hours)",
                                        inline=False,
                                    )
                                    embed.add_field(
                                        name="Keep Watching",
                                        value="Continue watching to unlock more exclusive roles!",
                                        inline=False,
                                    )
                                    embed.set_footer(text=f"Kick: {kick_name}")

                                    await member.send(embed=embed)
                                    logger.info(f"Sent role notification DM to {member.display_name}")
                                except discord.Forbidden:
                                    pass  # User has DMs disabled
                                except Exception as dm_error:
                                    logger.debug(f"Error sending DM: {dm_error}")

                            except discord.Forbidden:
                                logger.warning(f"Missing permission to assign {role.name}")
                            except Exception as e:
                                logger.warning(f"Error assigning role: {e}")

            except Exception as guild_error:
                logger.warning(f"⚠️ Role update error: {guild_error}")

    except Exception as e:
        logger.error(f"⚠️ Error in role update task: {e}")
        import traceback

        traceback.print_exc()


# -------------------------
# Dashboard access-users materialization
# -------------------------
def _refresh_access_users(guild):
    """Recompute + persist who currently has dashboard access for one guild.

    Mirrors the dashboard's own access rule (utils/auth.py):
      - 'admin' access: the guild OWNER, or any member with the Administrator or
        Manage Server permission — full, ungated dashboard access.
      - 'role' access: a member who holds one of the server's allowed roles
        (dashboard_role_access) — narrowed per dashboard_role_feature_access.
    A member who qualifies as BOTH is recorded as 'admin' (admin wins, matching
    resolve_server_access).

    Writes one row per (server, member) into dashboard_access_users and prunes
    rows for members who no longer qualify. Requires the members intent (on).
    """
    if not engine:
        return

    # Allowed role IDs for this server (empty if role access disabled / none set).
    with engine.connect() as conn:
        allowed_rows = conn.execute(
            text(
                """
                SELECT s.allow_role_access, r.discord_role_id
                FROM dashboard_access_settings s
                LEFT JOIN dashboard_role_access r
                  ON r.discord_server_id = s.discord_server_id
                WHERE s.discord_server_id = :sid
                """
            ),
            {"sid": guild.id},
        ).fetchall()

    allow_role_access = bool(allowed_rows and allowed_rows[0][0])
    allowed_role_ids = {int(row[1]) for row in allowed_rows if row[1] is not None} if allow_role_access else set()

    rows = []
    for member in guild.members:
        if member.bot:
            continue
        perms = member.guild_permissions
        is_owner = member.id == getattr(guild, "owner_id", None)
        name = getattr(member, "global_name", None) or member.name

        if is_owner or perms.administrator or perms.manage_guild:
            via = "Owner" if is_owner else ("Administrator" if perms.administrator else "Manage Server")
            rows.append({"d": member.id, "n": name, "t": "admin", "v": via})
            continue

        if allowed_role_ids:
            member_role_ids = {r.id for r in member.roles}
            matched = member_role_ids & allowed_role_ids
            if matched:
                # Name the granting role(s) for the "via" column.
                matched_names = [r.name for r in member.roles if r.id in matched]
                rows.append(
                    {
                        "d": member.id,
                        "n": name,
                        "t": "role",
                        "v": "Role: " + ", ".join(matched_names),
                    }
                )

    keep_ids = [r["d"] for r in rows]
    with engine.begin() as conn:
        if rows:
            conn.execute(
                text(
                    """
                    INSERT INTO dashboard_access_users
                    (discord_server_id, discord_id, username, access_type, via, updated_at)
                    VALUES (:sid, :d, :n, :t, :v, NOW())
                    ON CONFLICT (discord_server_id, discord_id)
                    DO UPDATE SET
                        username = EXCLUDED.username,
                        access_type = EXCLUDED.access_type,
                        via = EXCLUDED.via,
                        updated_at = NOW()
                    """
                ),
                [{"sid": guild.id, **r} for r in rows],
            )
        # Prune members who no longer qualify (lost a role/permission, left).
        if keep_ids:
            conn.execute(
                text(
                    """
                    DELETE FROM dashboard_access_users
                    WHERE discord_server_id = :sid
                      AND discord_id <> ALL(:keep)
                    """
                ),
                {"sid": guild.id, "keep": keep_ids},
            )
        else:
            conn.execute(
                text("DELETE FROM dashboard_access_users WHERE discord_server_id = :sid"),
                {"sid": guild.id},
            )
    logger.debug(f"[AccessUsers] {len(rows)} member(s) with dashboard access for guild {guild.id}")


# -------------------------
# Discord username backfill
# -------------------------
@tasks.loop(minutes=30)
async def update_discord_usernames_task():
    """Cache each linked viewer's Discord display name onto their links row,
    and keep servers.owner_discord_id in sync with the guild owner.

    The dashboard's Top Participants list shows the Discord name, which only the
    Discord API knows. Rather than have the dashboard call Discord per
    participant, we resolve it here (where discord.py already has the member
    objects) and persist it to links.discord_username. Runs periodically so
    name changes are picked up; only writes when the value actually changed.

    Piggybacks the same per-guild loop to persist the guild owner so the
    dashboard can gate owner-only extension writes (current-slot push).
    """
    if not engine:
        return
    try:
        for guild in bot.guilds:
            set_server(guild.id, guild.name)
            try:
                # Keep servers.owner_discord_id in sync with the Discord guild
                # owner. The dashboard gates owner-only extension writes (e.g. the
                # current-slot push) on this, so a mod/role-access user running the
                # extension can't write to the streamer's history. Only write when
                # it actually changed (ownership transfer is rare).
                owner_id = getattr(guild, "owner_id", None)
                if owner_id:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                                UPDATE servers SET owner_discord_id = :oid
                                WHERE discord_server_id = :sid
                                  AND (owner_discord_id IS DISTINCT FROM :oid)
                                """
                            ),
                            {"oid": int(owner_id), "sid": guild.id},
                        )

                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            """
                            SELECT DISTINCT discord_id, discord_username
                            FROM links
                            WHERE discord_server_id = :sid AND discord_id IS NOT NULL
                            """
                        ),
                        {"sid": guild.id},
                    ).fetchall()

                updates = []
                for discord_id, stored_name in rows:
                    member = guild.get_member(int(discord_id))
                    if not member:
                        continue  # not cached / left the server; leave stored value
                    # Display name = global_name, else the account username.
                    name = getattr(member, "global_name", None) or member.name
                    if name and name != stored_name:
                        updates.append({"d": int(discord_id), "n": name, "sid": guild.id})

                if updates:
                    with engine.begin() as conn:
                        # Update ALL platform rows for this (discord_id, server).
                        conn.execute(
                            text(
                                """
                                UPDATE links SET discord_username = :n
                                WHERE discord_id = :d AND discord_server_id = :sid
                                """
                            ),
                            updates,
                        )
                    logger.debug(f"[DiscordNames] Refreshed {len(updates)} name(s) for guild {guild.id}")

                # Materialize the list of members who currently have dashboard
                # access, so the Access Control page can show it without live
                # Discord calls. Reuses the members already in cache (members
                # intent is on). See _compute_access_users.
                try:
                    _refresh_access_users(guild)
                except Exception as au_err:
                    logger.warning(f"⚠️ access-users refresh error for {guild.id}: {au_err}")
            except Exception as guild_error:
                logger.warning(f"⚠️ Discord username backfill error for {guild.id}: {guild_error}")
    except Exception as e:
        logger.error(f"⚠️ Error in Discord username backfill task: {e}")
        import traceback

        traceback.print_exc()


@update_discord_usernames_task.before_loop
async def before_update_discord_usernames():
    await bot.wait_until_ready()


# -------------------------
# Cleanup expired verification codes and old chat data
# -------------------------
@tasks.loop(seconds=5)  # Check every 5 seconds for fast response
async def check_oauth_notifications_task():
    """Check for OAuth link success notifications and send Discord messages."""
    try:
        with engine.begin() as conn:
            # Get unprocessed notifications
            notifications = conn.execute(
                text(
                    """
                SELECT id, discord_id, kick_username, channel_id, message_id, discord_server_id, platform
                FROM oauth_notifications
                WHERE processed = FALSE AND kick_username != ''
                ORDER BY created_at ASC
                LIMIT 10
            """
                )
            ).fetchall()

            for (
                notification_id,
                discord_id,
                kick_username,
                channel_id,
                message_id,
                guild_id,
                link_platform,
            ) in notifications:
                # Tag this notification's logging with its server.
                _g = bot.get_guild(int(guild_id)) if guild_id else None
                set_server(guild_id, _g.name if _g else None)
                try:
                    # Check if this is a failed attempt (kick_username starts with "FAILED:")
                    is_failed = kick_username.startswith("FAILED:")
                    actual_kick_username = None
                    error_message = None

                    if is_failed:
                        # Parse failed attempt: "FAILED:<username>:<error>"
                        parts = kick_username.split(":", 2)
                        actual_kick_username = parts[1] if len(parts) > 1 else "unknown"
                        error_type = parts[2] if len(parts) > 2 else "unknown_error"

                        if error_type == "already_linked":
                            error_message = "This Kick account is already linked to another Discord user"
                        else:
                            error_message = f"Error: {error_type}"
                    else:
                        actual_kick_username = kick_username

                    # Delete the original "Link with Kick OAuth" message if we have the IDs
                    if channel_id and message_id:
                        try:
                            channel = bot.get_channel(int(channel_id))
                            if channel:
                                try:
                                    original_message = await channel.fetch_message(int(message_id))
                                    await original_message.delete()
                                    logger.info(f"🗑️ Deleted original OAuth message")
                                except (discord.NotFound, discord.Forbidden):
                                    pass
                        except Exception as e:
                            logger.warning(f"⚠️ Could not delete original message: {e}")

                    # Get the user
                    user = await bot.fetch_user(int(discord_id))
                    if user:
                        # Platform-aware label (Twitch links share this path).
                        _plat_label = "Twitch" if link_platform == "twitch" else "Kick"
                        if is_failed:
                            # Send failure message via DM
                            try:
                                await user.send(
                                    f"❌ **Link Failed**\n\n{error_message}\n\n{_plat_label} account: **{actual_kick_username}**"
                                )
                            except discord.Forbidden:
                                pass  # User has DMs disabled

                            # Log the failed attempt
                            await log_link_attempt(
                                user,
                                actual_kick_username,
                                success=False,
                                error_message=error_message,
                                guild_id=guild_id,
                                platform=link_platform,
                            )
                        else:
                            # Immediately cache the Discord display name on the
                            # new link row(s) so the dashboard has it without
                            # waiting for the periodic backfill. Best-effort.
                            try:
                                _dname = getattr(user, "global_name", None) or user.name
                                if _dname and guild_id:
                                    with engine.begin() as _conn:
                                        _conn.execute(
                                            text(
                                                """
                                                UPDATE links SET discord_username = :n
                                                WHERE discord_id = :d AND discord_server_id = :sid
                                                """
                                            ),
                                            {"n": _dname, "d": int(discord_id), "sid": int(guild_id)},
                                        )
                            except Exception as _name_err:
                                logger.debug(f"[DiscordNames] immediate cache skipped: {_name_err}")

                            # Send success message via DM
                            try:
                                await user.send(
                                    f"✅ **Verification Successful!**\n\nYour Discord account has been linked to {_plat_label} account **{actual_kick_username}**."
                                )
                            except discord.Forbidden:
                                # If DM fails, try to find a guild channel
                                # Multiserver: Try all guilds where the user is a member
                                for guild in bot.guilds:
                                    if not guild:
                                        continue
                                    member = guild.get_member(int(discord_id))
                                    if member:
                                        # Try to send in the same channel as original message, or system channel
                                        target_channel = bot.get_channel(int(channel_id)) if channel_id else None
                                        if (
                                            not target_channel
                                            or not target_channel.permissions_for(guild.me).send_messages
                                        ):
                                            target_channel = guild.system_channel or next(
                                                (
                                                    ch
                                                    for ch in guild.text_channels
                                                    if ch.permissions_for(guild.me).send_messages
                                                ),
                                                None,
                                            )

                                        if target_channel:
                                            await target_channel.send(
                                                f"{member.mention} ✅ **Verification Successful!** Your account has been linked to {_plat_label} **{actual_kick_username}**."
                                            )

                            # Log the successful link attempt
                            await log_link_attempt(
                                user, actual_kick_username, success=True, guild_id=guild_id, platform=link_platform
                            )

                            # Grant linked role if configured
                            if guild_id:
                                try:
                                    logger.info(
                                        f"🔍 Attempting to grant linked role for guild_id={guild_id}, discord_id={discord_id}"
                                    )
                                    guild = bot.get_guild(int(guild_id))
                                    if not guild:
                                        logger.warning(f"⚠️ Guild {guild_id} not found in bot")
                                    else:
                                        member = guild.get_member(int(discord_id))
                                        if not member:
                                            logger.warning(f"⚠️ Member {discord_id} not found in guild {guild.name}")
                                        else:
                                            # Get linked role ID from bot_settings — platform-aware
                                            # (twitch_linked_role_id for Twitch links, else kick_linked_role_id).
                                            role_setting_key = (
                                                "twitch_linked_role_id"
                                                if link_platform == "twitch"
                                                else "kick_linked_role_id"
                                            )
                                            try:
                                                linked_role_id = conn.execute(
                                                    text(
                                                        """
                                                        SELECT value FROM bot_settings
                                                        WHERE key = :role_key AND discord_server_id = :guild_id
                                                    """
                                                    ),
                                                    {"role_key": role_setting_key, "guild_id": guild_id},
                                                ).scalar()
                                                logger.info(
                                                    f"📋 Linked role ID ({role_setting_key}) from DB: {linked_role_id!r}"
                                                )
                                            except Exception as query_err:
                                                logger.error(f"❌ Failed to query linked role ID: {query_err}")
                                                linked_role_id = None

                                            if linked_role_id and linked_role_id.strip():
                                                try:
                                                    role = guild.get_role(int(linked_role_id))
                                                    if not role:
                                                        logger.warning(
                                                            f"⚠️ Linked role ID {linked_role_id} not found in guild {guild.name}"
                                                        )
                                                    elif role in member.roles:
                                                        logger.info(
                                                            f"ℹ️ Member {member.display_name} already has role '{role.name}'"
                                                        )
                                                    else:
                                                        _plat_label = "Twitch" if link_platform == "twitch" else "Kick"
                                                        await member.add_roles(
                                                            role,
                                                            reason=f"Linked {_plat_label} account: {actual_kick_username}",
                                                        )
                                                        logger.info(
                                                            f"✅ Granted role '{role.name}' to {member.display_name} for {_plat_label} link"
                                                        )
                                                except ValueError as val_err:
                                                    logger.error(f"❌ Invalid role ID {linked_role_id}: {val_err}")
                                            else:
                                                logger.warning(f"⚠️ No linked role configured for guild {guild_id}")
                                except Exception as role_error:
                                    import traceback

                                    logger.error(f"❌ Error granting linked role: {role_error}")
                                    traceback.print_exc()

                    # Mark as processed
                    conn.execute(
                        text(
                            """
                        UPDATE oauth_notifications
                        SET processed = TRUE
                        WHERE id = :id
                    """
                        ),
                        {"id": notification_id},
                    )

                    logger.info(f"✅ Sent OAuth notification to Discord {discord_id}")

                except Exception as e:
                    logger.error(f"⚠️ Error sending OAuth notification to {discord_id}: {e}")
                    # Mark as processed anyway to avoid retry loops
                    conn.execute(
                        text(
                            """
                        UPDATE oauth_notifications
                        SET processed = TRUE
                        WHERE id = :id
                    """
                        ),
                        {"id": notification_id},
                    )

    except Exception as e:
        logger.error(f"⚠️ Error in OAuth notifications task: {e}")


@tasks.loop(minutes=60)  # Twitch requires hourly validation; also refresh proactively
async def proactive_twitch_token_refresh_task():
    """
    Keep Twitch user tokens (the shared bot account + any broadcaster tokens) alive
    so chat sending never goes stale.

    Twitch rules (https://dev.twitch.tv/docs/authentication/validate-tokens/ +
    .../refresh-tokens/):
      - Apps MUST validate tokens hourly (compliance; Twitch audits this).
      - User access tokens expire (~4h); refresh tokens for a confidential client
        (we have a client secret) do NOT expire — they only break on password
        change / app disconnect / client-secret rotation.
      - Refresh ROTATES the refresh token — persist the new one every time.

    For each stored token: validate it; if expiring within 60 min (or validation
    401s), refresh and persist the rotated tokens.
    """
    if not engine:
        return

    try:
        from core.twitch_api import TwitchAPI

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT user_id, discord_server_id, twitch_username, access_token, refresh_token, expires_at
                    FROM twitch_oauth_tokens
                    WHERE refresh_token IS NOT NULL
                    ORDER BY expires_at ASC NULLS FIRST
                    """
                )
            ).fetchall()

        if not rows:
            logger.debug("[Twitch] ℹ️ No Twitch OAuth tokens to maintain")
            return

        refreshed = failed = validated = 0
        for user_id, row_server_id, twitch_username, access_token, refresh_token, expires_at in rows:
            # Tag this token's refresh/validation logs with its owning server so a
            # "[Twitch] Refreshed token…" line shows "[<server name>]" not "[-]".
            _g = bot.get_guild(row_server_id) if row_server_id else None
            set_server(row_server_id, _g.name if _g else None)
            api = TwitchAPI(access_token=access_token, refresh_token=refresh_token)
            try:
                needs_refresh = False
                # 1) Hourly validation (compliance) — also tells us real expiry.
                try:
                    info = await api.validate_token(access_token)
                    validated += 1
                    if int(info.get("expires_in", 0)) < 3600:  # <60 min left
                        needs_refresh = True
                except Exception:
                    # Validation failed (likely 401 / expired) — refresh.
                    needs_refresh = True

                if needs_refresh:
                    tokens = await api.refresh_user_token()
                    from datetime import timedelta as _td

                    new_expires = datetime.now(timezone.utc) + _td(seconds=tokens.expires_in or 14400)
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                                UPDATE twitch_oauth_tokens
                                SET access_token = :at, refresh_token = :rt,
                                    expires_at = :exp, updated_at = CURRENT_TIMESTAMP
                                WHERE user_id = :u AND discord_server_id = :sid
                                """
                            ),
                            {
                                "at": tokens.access_token,
                                "rt": tokens.refresh_token or refresh_token,
                                "exp": new_expires,
                                "u": user_id,
                                "sid": row_server_id,
                            },
                        )
                    refreshed += 1
                    logger.info(f"[Twitch] ✅ Refreshed token for {twitch_username or user_id}")
            except Exception as e:
                failed += 1
                # A 400 from id.twitch.tv/oauth2/token on a refresh grant means the
                # refresh token is PERMANENTLY invalid (authorization revoked,
                # client secret rotated, or a stale row for a retired account —
                # e.g. the old lelebotxyz bot account). It will never succeed, so
                # clear it to stop retrying every hour; the maintenance SELECT
                # skips NULL refresh tokens. A fresh OAuth link rewrites the row.
                if getattr(e, "status", None) == 400:
                    try:
                        with engine.begin() as conn:
                            conn.execute(
                                text(
                                    """
                                    UPDATE twitch_oauth_tokens
                                    SET refresh_token = NULL, updated_at = CURRENT_TIMESTAMP
                                    WHERE user_id = :u AND discord_server_id = :sid
                                    """
                                ),
                                {"u": user_id, "sid": row_server_id},
                            )
                        logger.warning(
                            f"[Twitch] 🧹 Refresh token for {twitch_username or user_id} is permanently "
                            f"invalid (400) — cleared it; re-link the account if it is still needed"
                        )
                    except Exception as db_err:
                        logger.info(
                            f"[Twitch] Could not clear dead refresh token for {twitch_username or user_id}: {db_err}"
                        )
                else:
                    logger.info(f"[Twitch] ❌ Failed to maintain token for {twitch_username or user_id}: {e}")
            finally:
                await api.close()

        # Per-token loop done; the summary below is a global (all-servers) line, so
        # clear the last token's server tag before emitting it.
        clear_server()
        if refreshed or failed:
            logger.info(f"[Twitch] Token maintenance: {validated} validated, {refreshed} refreshed, {failed} failed")
        else:
            logger.debug(f"[Twitch] Token maintenance: {validated} validated, all healthy")
    except Exception as e:
        logger.info(f"[Twitch] ❌ Error in token maintenance task: {e}")
        import traceback

        traceback.print_exc()


@tasks.loop(minutes=30)  # Check every 30 minutes
async def proactive_token_refresh_task():
    """
    Proactive access token refresh task.

    NOTE: Refresh tokens are PERMANENT per Kick API (as of Nov 2025).
    This task ONLY refreshes ACCESS TOKENS when they are about to expire.

    - expires_at tracks ACCESS TOKEN expiration (they expire ~1 hour after issued)
    - Refresh tokens are permanent and never expire
    - When access token expires, we use refresh_token to get a new one
    - Refresh token stays the same
    """
    if not engine:
        return

    try:
        logger.debug("[Kick] 🔄 Checking for access tokens that need refresh...")

        with engine.connect() as conn:
            results = conn.execute(
                text(
                    """
                SELECT user_id, kick_username, refresh_token, expires_at
                FROM kick_oauth_tokens
                WHERE refresh_token IS NOT NULL AND needs_reauth = FALSE
                ORDER BY expires_at ASC
            """
                )
            ).fetchall()

            if not results:
                logger.info("[Kick] ℹ️  No OAuth tokens in database")
                return

            now = datetime.now(timezone.utc)
            refreshed = 0
            failed = 0
            checked = 0

            for user_id, kick_username, refresh_token, expires_at in results:
                checked += 1

                if expires_at is None:
                    # Legacy token without expiration info - treat as expired to trigger refresh
                    logger.info(
                        f"[Kick] ⏱️  Token for {kick_username} has no expiration (legacy) - treating as expired, will refresh"
                    )
                    from datetime import timedelta

                    expires_at = now - timedelta(minutes=1)

                # Make timezone-aware if needed
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)

                time_until_expiry = expires_at - now
                minutes_until_expiry = time_until_expiry.total_seconds() / 60

                if minutes_until_expiry < 30:
                    # Token expired or expiring soon - refresh it now
                    if minutes_until_expiry < 0:
                        logger.info(
                            f"[Kick] ⏱️  Token for {kick_username} has EXPIRED - refreshing using refresh_token..."
                        )
                    else:
                        logger.info(
                            f"[Kick] ⏱️  Token for {kick_username} expires in {minutes_until_expiry:.0f}m - refreshing now..."
                        )
                    if await refresh_kick_oauth_token_for_user(user_id, kick_username, refresh_token):
                        refreshed += 1
                        logger.info(f"[Kick] ✅ Token refreshed for {kick_username}")
                    else:
                        failed += 1
                        logger.info(f"[Kick] ❌ Failed to refresh token for {kick_username}")
                else:
                    # Plenty of time left
                    hours = minutes_until_expiry / 60
                    logger.debug(f"[Kick] ✓ {kick_username} token valid for {hours:.1f} more hours")

            # Only announce at INFO when work was done or there were failures.
            if refreshed or failed:
                logger.info(
                    f"[Kick] Token refresh check complete: {refreshed} refreshed, {failed} failed, {checked} checked"
                )
            else:
                logger.debug(
                    f"[Kick] Token refresh check complete: {refreshed} refreshed, {failed} failed, {checked} checked"
                )

    except Exception as e:
        logger.info(f"[Kick] ❌ Error in token refresh task: {e}")
        import traceback

        traceback.print_exc()


async def refresh_kick_oauth_token_for_user(user_id: int, kick_username: str, refresh_token: str) -> bool:
    """Refresh OAuth ACCESS TOKEN for a specific user using their REFRESH TOKEN.

    NOTE: Only refreshes the ACCESS TOKEN (short-lived ~1hr).
    The REFRESH TOKEN itself is permanent and never needs refresh.
    Uses PostgreSQL advisory lock to prevent race conditions with the dashboard's
    token refresh scheduler (both may try to refresh the same token).
    """
    try:
        # Acquire advisory lock to prevent race with dashboard refresh
        lock_key = abs(user_id) % (2**31)
        with engine.connect() as conn:
            got_lock = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}).scalar()
            if not got_lock:
                logger.info(f"[Kick] ⏳ Token for {kick_username} is being refreshed by another process, skipping")
                return True  # Not a failure — someone else is handling it

            # Re-check: token may have been refreshed by another process already
            current = conn.execute(
                text("SELECT refresh_token, expires_at FROM kick_oauth_tokens WHERE user_id = :uid"),
                {"uid": user_id},
            ).fetchone()
            if current and current[0] != refresh_token:
                logger.info(f"[Kick] ✅ Token for {kick_username} was already refreshed by another process")
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                conn.commit()
                return True

        token_url = "https://id.kick.com/oauth/token"
        token_data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": KICK_CLIENT_ID,
            "client_secret": KICK_CLIENT_SECRET,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=token_data, timeout=10) as response:
                if response.status == 200:
                    token_response = await response.json()
                    new_access_token = token_response.get("access_token")
                    new_refresh_token = token_response.get("refresh_token", refresh_token)
                    expires_in = token_response.get("expires_in", 3600)

                    if not new_access_token:
                        # Release lock
                        with engine.connect() as conn:
                            conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                            conn.commit()
                        return False

                    # Calculate access token expiration
                    # Kick access tokens expire ~1 hour after issued (expires_in from response)
                    # Refresh tokens are permanent and never expire
                    from datetime import datetime, timedelta, timezone

                    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

                    with engine.begin() as conn:
                        # Update kick_oauth_tokens table
                        conn.execute(
                            text(
                                """
                            UPDATE kick_oauth_tokens
                            SET access_token = :access_token,
                                refresh_token = :refresh_token,
                                expires_at = :expires_at,
                                needs_reauth = FALSE,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = :user_id
                        """
                            ),
                            {
                                "access_token": new_access_token,
                                "refresh_token": new_refresh_token,
                                "expires_at": expires_at,
                                "user_id": user_id,
                            },
                        )

                        # Also update bot_settings table if tokens exist there (legacy support)
                        # This ensures both tables stay in sync
                        conn.execute(
                            text(
                                """
                            UPDATE bot_settings
                            SET value = :access_token, updated_at = CURRENT_TIMESTAMP
                            WHERE key IN ('kick_oauth_token', 'kick_access_token')
                            AND discord_server_id IN (
                                SELECT DISTINCT discord_server_id FROM bot_settings
                                WHERE key = 'kick_channel' AND LOWER(value) = LOWER(:kick_username)
                            )
                        """
                            ),
                            {"access_token": new_access_token, "kick_username": kick_username},
                        )

                        conn.execute(
                            text(
                                """
                            UPDATE bot_settings
                            SET value = :refresh_token, updated_at = CURRENT_TIMESTAMP
                            WHERE key = 'kick_refresh_token'
                            AND discord_server_id IN (
                                SELECT DISTINCT discord_server_id FROM bot_settings
                                WHERE key = 'kick_channel' AND LOWER(value) = LOWER(:kick_username)
                            )
                        """
                            ),
                            {"refresh_token": new_refresh_token, "kick_username": kick_username},
                        )

                    # Release lock after successful update
                    with engine.connect() as conn:
                        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                        conn.commit()

                    return True
                else:
                    error_text = await response.text()
                    logger.info(
                        f"[Kick] ❌ Refresh failed for {kick_username} (HTTP {response.status}): {error_text[:100]}"
                    )

                    # invalid_grant = the refresh token is permanently dead (revoked
                    # / issued to another client). Flag needs_reauth (by user_id, so
                    # both the per-server and legacy discord_server_id=0 rows are
                    # covered) so the scheduler stops looping and the dashboard
                    # prompts the streamer to reconnect. Other errors stay
                    # retryable — the refresh token itself may still be valid.
                    error_code = ""
                    try:
                        error_code = (json.loads(error_text) or {}).get("error", "")
                    except Exception:
                        error_code = "invalid_grant" if "invalid_grant" in (error_text or "") else ""

                    with engine.begin() as conn:
                        if error_code == "invalid_grant":
                            conn.execute(
                                text(
                                    "UPDATE kick_oauth_tokens SET needs_reauth = TRUE, updated_at = CURRENT_TIMESTAMP WHERE user_id = :uid"
                                ),
                                {"uid": user_id},
                            )
                            logger.warning(
                                f"[Kick] 🔐 Refresh token for {kick_username} is invalid_grant (revoked) - flagged needs_reauth; streamer must reconnect Kick"
                            )
                        conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                    return False

    except Exception as e:
        logger.info(f"[Kick] ❌ Error refreshing token for {kick_username}: {e}")
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": abs(user_id) % (2**31)})
                conn.commit()
        except Exception:
            pass
        return False


@tasks.loop(minutes=5)
async def cleanup_pending_links_task():
    """Remove expired verification codes and old chat activity data."""
    global recent_chatters

    now = datetime.now(timezone.utc)

    # 🔒 SECURITY: Clean up old chatter data to prevent memory leak
    chat_cutoff = now - timedelta(minutes=CHAT_ACTIVITY_WINDOW_MINUTES * 2)  # Keep 2x window for safety
    recent_chatters = {
        username: timestamp for username, timestamp in recent_chatters.items() if timestamp >= chat_cutoff
    }

    expiry_cutoff = now - timedelta(minutes=CODE_EXPIRY_MINUTES)

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT discord_id FROM pending_links WHERE timestamp < :t"), {"t": expiry_cutoff.isoformat()}
        ).fetchall()

        expired_ids = [r[0] for r in rows]

        conn.execute(text("DELETE FROM pending_links WHERE timestamp < :t"), {"t": expiry_cutoff.isoformat()})

    # Notify users their codes expired
    for discord_id in expired_ids:
        try:
            user = await bot.fetch_user(int(discord_id))
            if user:
                try:
                    await user.send(
                        "⏰ Your Kick verification code expired. " "Use the Link Account panel to generate a new one."
                    )
                except discord.Forbidden:
                    pass
        except Exception:
            pass


@tasks.loop(minutes=5)  # Fallback check every 5 minutes (PRIMARY: webhooks)
async def clip_buffer_management_task():
    """
    FALLBACK: Monitor stream status per guild and manage clip buffers on Dashboard.

    PRIMARY METHOD: Kick webhooks (livestream.status.updated) in kick_webhooks.py
    - Instant response when stream goes live/offline
    - Triggers clip buffer start/stop automatically

    FALLBACK: This polling task runs every 5 minutes in case:
    - Webhooks aren't registered for a server
    - Webhook delivery fails
    - Dashboard restarts and loses buffer state

    - When stream goes LIVE: Start clip buffer on Dashboard
    - When stream goes OFFLINE: Stop clip buffer on Dashboard
    """
    global clip_buffer_active_by_guild, last_stream_live_state_by_guild

    # Multiserver: Process each guild independently
    for guild in bot.guilds:
        guild_id = guild.id
        guild_name = guild.name

        # Tag every log line in this guild's iteration with the server name. The
        # loop is sequential and each iteration re-sets the context, so the next
        # guild overwrites it; no per-iteration clear is needed.
        set_server(guild_id, guild_name)

        try:
            # Get per-guild settings from database
            kick_channel = None
            dashboard_url = None
            bot_api_key = None
            # Default True: only the explicit string 'false' disables auto-start.
            auto_start_buffer = True

            with engine.connect() as conn:
                settings = conn.execute(
                    text(
                        """
                    SELECT key, value FROM bot_settings
                    WHERE discord_server_id = :guild_id
                    AND key IN ('kick_channel', 'dashboard_url', 'bot_api_key', 'clips_auto_start_on_live')
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchall()

                for key, value in settings:
                    if key == "kick_channel":
                        kick_channel = value
                    elif key == "dashboard_url":
                        dashboard_url = value
                    elif key == "bot_api_key":
                        bot_api_key = value
                    elif key == "clips_auto_start_on_live":
                        auto_start_buffer = str(value).lower() != "false"

            # Prefer the derived per-server base (servers.subdomain + public
            # domain); the stored dashboard_url is only a stale-prone fallback.
            dashboard_url = get_server_base_url(engine, guild_id) or dashboard_url

            # Env-controlled system secret takes precedence over the legacy per-server
            # DB value (covers both the live-start and offline-stop calls below).
            bot_api_key = get_clip_api_key(bot_api_key)

            # Skip guilds without required configuration. dashboard_url and
            # bot_api_key are no longer needed: the buffer lives in this process,
            # so this loop drives it directly instead of calling the dashboard.
            if not kick_channel:
                continue

            from utils.clip_service import get_clip_manager

            clip_manager = get_clip_manager()

            # Get per-guild state
            clip_buffer_active = clip_buffer_active_by_guild.get(guild_id, False)
            last_stream_live_state = last_stream_live_state_by_guild.get(guild_id)

            # Check if stream is currently live
            try:
                is_live = await check_stream_live(kick_channel)
                if last_stream_live_state != is_live:  # Only log state changes
                    logger.info(
                        f"[Clip Buffer] Stream live check for '{kick_channel}': {is_live} | Last state: {last_stream_live_state} | Buffer active: {clip_buffer_active}"
                    )
            except Exception as e:
                # Cloudflare block or other error - skip this guild
                if "403" not in str(e) and "Cloudflare" not in str(e):
                    logger.info(f"[Clip Buffer] ⚠️ Error checking stream status: {e}")
                continue

            # Detect state transitions
            should_start_buffer = False
            if last_stream_live_state is None:
                # First run - initialize state
                last_stream_live_state_by_guild[guild_id] = is_live
                last_stream_live_state = is_live
                if is_live:
                    logger.info(f"[Clip Buffer] 🎬 Stream is already live on startup, starting buffer...")
                    should_start_buffer = True
                else:
                    logger.info(f"[Clip Buffer] 📴 Stream is offline on startup")

            # If stream is live but buffer is not active, check if we need to start it.
            # The buffer object is right here, so this is a direct read rather
            # than an HTTP round-trip through the dashboard.
            if is_live and not clip_buffer_active and not should_start_buffer:
                buf = clip_manager.get_buffer(kick_channel)
                if not buf:
                    logger.info(f"[Clip Buffer] 🔄 Stream is live but no buffer running, starting...")
                    should_start_buffer = True
                elif buf.is_recording:
                    clip_buffer_active_by_guild[guild_id] = True
                    logger.info(f"[Clip Buffer] ℹ️ Buffer already running")
                else:
                    logger.info(f"[Clip Buffer] ⚠️ Buffer exists but not recording, restarting...")
                    should_start_buffer = True

            # Periodic verification: ffmpeg can die without the buffer being removed.
            elif is_live and clip_buffer_active:
                buf = clip_manager.get_buffer(kick_channel)
                if not buf:
                    logger.info(f"[Clip Buffer] ⚠️ Buffer disappeared! Restarting...")
                    clip_buffer_active_by_guild[guild_id] = False
                    should_start_buffer = True
                elif not buf.is_recording:
                    logger.info(f"[Clip Buffer] ⚠️ Buffer stopped recording! Restarting...")
                    clip_buffer_active_by_guild[guild_id] = False
                    should_start_buffer = True

            # Handle transition: OFFLINE -> LIVE (or first run while live)
            if ((is_live and not last_stream_live_state) or should_start_buffer) and not auto_start_buffer:
                # Auto-start disabled for this guild — note the live state below
                # but don't start the buffer. Manual Start from the dashboard
                # still works.
                if not should_start_buffer:
                    logger.info(
                        f"[Clip Buffer] ⏸️ Auto-start disabled (clips_auto_start_on_live=false) — skipping buffer start"
                    )
            elif (is_live and not last_stream_live_state) or should_start_buffer:
                if not should_start_buffer:
                    logger.info(f"[Clip Buffer] 🟢 Stream went LIVE! Starting clip buffer...")

                # Use the robust playback URL fetcher with caching and validation
                playback_url: Optional[str] = None
                try:
                    from core.kick_api import get_playback_url

                    # Force refresh if this is a new live transition (not a retry)
                    force_refresh = not should_start_buffer
                    playback_url = await get_playback_url(kick_channel, force_refresh=force_refresh)

                    if playback_url:
                        logger.info(f"[Clip Buffer] 📺 Obtained playback URL: {playback_url[:80]}...")
                    else:
                        logger.info(f"[Clip Buffer] ⚠️ No playback URL available")
                except Exception as e:
                    logger.info(f"[Clip Buffer] ⚠️ Error fetching playback URL: {e}")

                try:
                    buffer = clip_manager.create_buffer(kick_channel, buffer_minutes=4)
                    if await buffer.start(playback_url):
                        clip_buffer_active_by_guild[guild_id] = True
                        logger.info(f"[Clip Buffer] ✅ Buffer started for {kick_channel}")
                    else:
                        clip_buffer_active_by_guild[guild_id] = False
                        logger.info(f"[Clip Buffer] ❌ Failed to start buffer for {kick_channel}")
                except Exception as e:
                    logger.info(f"[Clip Buffer] ❌ Error starting buffer: {e}")

            # Handle transition: LIVE -> OFFLINE
            elif not is_live and last_stream_live_state:
                logger.info(f"[Clip Buffer] 🔴 Stream went OFFLINE! Stopping clip buffer...")
                try:
                    buffer = clip_manager.get_buffer(kick_channel)
                    if buffer:
                        await buffer.stop()
                        clip_manager.remove_buffer(kick_channel)
                        logger.info(f"[Clip Buffer] ✅ Buffer stopped for {kick_channel}")
                    else:
                        # Auto-start off, or the start had failed. The desired end
                        # state — stopped — already holds.
                        logger.info(f"[Clip Buffer] ℹ️ No buffer was running — nothing to stop")
                    clip_buffer_active_by_guild[guild_id] = False
                except Exception as e:
                    logger.info(f"[Clip Buffer] ⚠️ Error stopping buffer: {e}")

            # Update last known state
            last_stream_live_state_by_guild[guild_id] = is_live

        except Exception as e:
            logger.info(f"[Clip Buffer] ❌ Error in buffer management task: {e}")


@clip_buffer_management_task.before_loop
async def before_clip_buffer_task():
    """Wait for bot to be ready before starting clip buffer management."""
    await bot.wait_until_ready()
    # Initial delay to allow settings to load
    await asyncio.sleep(10)
    logger.debug("[Clip Buffer] 🎬 Starting clip buffer management task...")
    logger.debug(
        f"[Clip Buffer] Settings manager initialized: {hasattr(bot, 'settings_manager') and bot.settings_manager is not None}"
    )
    if hasattr(bot, "settings_manager") and bot.settings_manager:
        bot.settings_manager.refresh()
        logger.debug(f"[Clip Buffer] dashboard_url: {bool(bot.settings_manager.dashboard_url)}")
        logger.debug(f"[Clip Buffer] bot_api_key: {bool(bot.settings_manager.bot_api_key)}")
        logger.debug(f"[Clip Buffer] kick_channel: {bot.settings_manager.kick_channel}")


# ---------------------------------------------------------------------------
# Leaderboard period auto-renewal (Tier 4: "leaderboard_generator")
#
# When an active wager_leaderboard_periods row's end_date passes, freeze its
# top-N snapshot (the SAME SUM(wager_delta) math as the dashboard's
# get_wager_leaderboard) and, if auto_renew is on, immediately start the next
# back-to-back period (new.start = old.end, new.end = old.end + duration),
# copying the prizes/site/settings.
#
# These tables are wager-leaderboard-specific — this task NEVER touches
# raffle_periods, points, or watchtime. It mirrors the raffle period-rollover
# fail-safe: any error for a guild skips that guild and is retried next tick;
# we never end/create from a partial read.
# ---------------------------------------------------------------------------
def _lb_bot_settings(conn, server_id, keys):
    """Read per-server bot_settings for the given keys. Returns {key: value}."""
    rows = conn.execute(
        text("SELECT key, value FROM bot_settings WHERE discord_server_id = :sid AND key = ANY(:keys)"),
        {"keys": list(keys), "sid": server_id},
    ).fetchall()
    out = {}
    for k, v in rows:
        val = (v or "").strip()
        if val:
            out[k] = val
    return out


def _fetch_howl_freeze_rows(conn, server_id, start_dt, end_dt, winner_count):
    """Fetch Howl's per-window leaderboard for a freeze (sync, via requests).

    Howl's affiliate API returns the exact per-window wagered total, so a Howl
    period is frozen from the API directly — NOT from shuffle_wager_history.
    Returns a list of (shuffle_username, kick_name, discord_id, is_linked,
    wagered_usd) tuples matching the SQL path's row shape, or [] on any error.
    """
    s = _lb_bot_settings(conn, server_id, ("howl_affiliate_url", "howl_api_key"))
    url = s.get("howl_affiliate_url") or "https://howl.gg/api/user/affiliate/lb"
    api_key = s.get("howl_api_key")
    if not api_key:
        logger.warning(f"[Leaderboard] howl freeze: no api_key for server {server_id}")
        return []

    limit = max(1, min(int(winner_count or 10), 1000))
    params = {
        "from": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "limit": str(limit),
    }
    try:
        # Accept/UA match the tracker's: howl.gg's Cloudflare scores bare
        # python-client UAs worst, so identify honestly but not as one.
        headers = {
            "Authorization": api_key,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; WagerlabsBot/1.0; +https://wagerlabs.app)",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        body = resp.json() if resp.status_code == 200 else None
    except Exception as e:
        logger.warning(f"[Leaderboard] howl freeze fetch failed for server {server_id}: {e}")
        return []
    if not isinstance(body, dict) or not body.get("success"):
        logger.warning(f"[Leaderboard] howl freeze API error: {body.get('error') if isinstance(body, dict) else body}")
        return []

    out = []
    for r in body.get("data") or []:
        name = r.get("name")
        if not name:
            continue
        try:
            wagered = round(float(r.get("wageredUSD") or 0), 2)
        except (TypeError, ValueError):
            wagered = 0.0
        # (username, kick_name, discord_id, is_linked, wagered) — Howl has no link info.
        out.append((name, None, None, False, wagered))
    out.sort(key=lambda t: t[4], reverse=True)
    return out[:limit]


def _shuffle_lifetime_totals(conn, server_id):
    """Lifetime totals per shuffle user, from the bot's tracked rows (DB read).

    The shuffle tracker already polls the affiliate URL continuously and stores
    total_wager_usd in raffle_shuffle_wagers, so the leaderboard reads THOSE rows
    instead of fetching shuffle's API again (shuffle 400s with TOO_MANY_REQUEST
    when many surfaces hit it). Pure DB read — no external request.

    One row per username (most recent), INDEPENDENT of any raffle period_id.
    Returns {uname_key: (username, total)}.
    """
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT ON (LOWER(shuffle_username))
                LOWER(shuffle_username) AS uname_key,
                shuffle_username,
                CAST(total_wager_usd AS FLOAT) AS total
            FROM raffle_shuffle_wagers
            WHERE discord_server_id = :sid AND platform = 'shuffle'
            ORDER BY LOWER(shuffle_username), last_updated DESC
            """
        ),
        {"sid": server_id},
    ).fetchall()
    return {r[0]: (r[1], float(r[2] or 0)) for r in rows}


def _shuffle_identity(conn, server_id):
    """Map shuffle username -> (kick_name, discord_id) for link badges (DB)."""
    rows = conn.execute(
        text(
            """
            SELECT DISTINCT ON (LOWER(shuffle_username))
                LOWER(shuffle_username) AS uname_key, kick_name, discord_id
            FROM raffle_shuffle_wagers
            WHERE discord_server_id = :sid AND platform = 'shuffle'
            ORDER BY LOWER(shuffle_username), last_updated DESC
            """
        ),
        {"sid": server_id},
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _ensure_leaderboard_baselines(conn, period_id, totals, identity):
    """Snapshot a baseline for any user not yet baselined for this period.

    `totals` is {uname_key: (username, total)} from the affiliate URL. First
    sighting during a period fixes that user's baseline at their current lifetime
    total, so only wagering from that point counts. Existing baselines are never
    overwritten.
    """
    for key, (username, total) in totals.items():
        kick, did = identity.get(key, (None, None))
        conn.execute(
            text(
                """
                INSERT INTO wager_leaderboard_baselines
                    (period_id, shuffle_username, baseline_amount, kick_name, discord_id)
                VALUES (:pid, :u, :b, :k, :d)
                ON CONFLICT (period_id, shuffle_username) DO NOTHING
                """
            ),
            {"pid": period_id, "u": username, "b": total, "k": kick, "d": did},
        )


def _shuffle_baseline_rows(conn, period_id, server_id, winner_count):
    """Top-N shuffle rows for a period via the baseline model (SQLAlchemy).

    leaderboard_value = max(0, current_lifetime_total - period_baseline), where
    the current total comes from the affiliate URL. Baselines are seeded by the
    live sync, so freezing just reads them.
    """
    totals = _shuffle_lifetime_totals(conn, server_id)
    identity = _shuffle_identity(conn, server_id)
    baselines = {
        str(u).lower(): float(b)
        for u, b in conn.execute(
            text("SELECT shuffle_username, baseline_amount FROM wager_leaderboard_baselines WHERE period_id = :pid"),
            {"pid": period_id},
        ).fetchall()
    }
    rows = []
    for key, (username, total) in totals.items():
        baseline = baselines.get(key, total)
        period_wagered = round(max(0.0, total - baseline), 2)
        if period_wagered > 0:
            kick, did = identity.get(key, (None, None))
            rows.append((username, kick, did, did is not None, period_wagered))
    rows.sort(key=lambda t: t[4], reverse=True)
    return rows[: winner_count or 10]


def _freeze_leaderboard_snapshot(conn, period_id, server_id, start_dt, end_dt, winner_count, site="shuffle"):
    """Write the frozen top-N winners for a period (SQLAlchemy connection).

    - Howl: query the Howl affiliate API directly for [start, end).
    - Shuffle: the baseline model (current lifetime total - period baseline).
    """
    conn.execute(
        text("DELETE FROM wager_leaderboard_winners WHERE period_id = :pid"),
        {"pid": period_id},
    )

    if (site or "shuffle").lower() == "howl":
        rows = _fetch_howl_freeze_rows(conn, server_id, start_dt, end_dt, winner_count)
    else:
        rows = _shuffle_baseline_rows(conn, period_id, server_id, winner_count)

    prizes = {
        r[0]: r[1]
        for r in conn.execute(
            text("SELECT rank, amount FROM wager_leaderboard_prizes WHERE period_id = :pid"),
            {"pid": period_id},
        ).fetchall()
    }

    for i, row in enumerate(rows):
        rank = i + 1
        conn.execute(
            text(
                """
                INSERT INTO wager_leaderboard_winners
                    (period_id, rank, shuffle_username, kick_name, discord_id,
                     wagered_usd, prize_amount, is_linked)
                VALUES (:pid, :rank, :uname, :kick, :did, :wag, :prize, :linked)
                """
            ),
            {
                "pid": period_id,
                "rank": rank,
                "uname": row[0],
                "kick": row[1],
                "did": int(row[2]) if row[2] is not None else None,
                "wag": float(row[4]) if row[4] is not None else 0,
                "prize": float(prizes.get(rank, 0) or 0),
                "linked": bool(row[3]),
            },
        )
    return len(rows)


def _sync_active_leaderboard_baselines(conn, guild_id, now):
    """Snapshot baselines for active, started SHUFFLE periods (decoupled from raffle).

    Ensures the shuffle baseline exists for every tracked user once a period has
    started, so the leaderboard (current total - baseline) is populated even
    before anyone views the public page. Howl periods need no baselines (their
    API returns per-window totals). Independent of raffle periods entirely.
    """
    active = conn.execute(
        text(
            """
            SELECT id, site, winner_count
            FROM wager_leaderboard_periods
            WHERE discord_server_id = :sid
              AND status = 'active'
              AND start_date <= :now
            """
        ),
        {"sid": guild_id, "now": now},
    ).fetchall()
    if not active:
        return
    # Fetch once per guild (not per period) — all active periods share the same
    # affiliate totals + identity.
    totals = None
    identity = None
    for pid, site, _wc in active:
        if (site or "shuffle").lower() == "howl":
            continue
        if totals is None:
            totals = _shuffle_lifetime_totals(conn, guild_id)
            identity = _shuffle_identity(conn, guild_id)
        if totals:
            _ensure_leaderboard_baselines(conn, pid, totals, identity)


@tasks.loop(minutes=2)
async def leaderboard_sync_task():
    """Keep leaderboard periods current: snapshot shuffle baselines for active
    periods, freeze ended periods, and roll over auto-renewing ones.

    Fully independent of the raffle period system.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC (column type)

    for guild in bot.guilds:
        guild_id = guild.id
        set_server(guild_id, guild.name)
        try:
            with engine.begin() as conn:
                # 0) Keep active shuffle periods' baselines seeded.
                _sync_active_leaderboard_baselines(conn, guild_id, now)

                period = conn.execute(
                    text(
                        """
                        SELECT id, start_date, end_date, winner_count, auto_renew,
                               site, title, prize_pool, stats_url, join_code, cta_url
                        FROM wager_leaderboard_periods
                        WHERE discord_server_id = :sid
                          AND status = 'active'
                          AND end_date <= :now
                        ORDER BY end_date ASC
                        LIMIT 1
                        """
                    ),
                    {"sid": guild_id, "now": now},
                ).fetchone()

                if not period:
                    continue

                (
                    pid,
                    start_dt,
                    end_dt,
                    winner_count,
                    auto_renew,
                    site,
                    title,
                    prize_pool,
                    stats_url,
                    join_code,
                    cta_url,
                ) = period

                # 1) Freeze the finished period (platform-correct source).
                count = _freeze_leaderboard_snapshot(conn, pid, guild_id, start_dt, end_dt, winner_count, site=site)
                conn.execute(
                    text("UPDATE wager_leaderboard_periods " "SET status = 'ended', frozen_at = NOW() WHERE id = :pid"),
                    {"pid": pid},
                )
                logger.info(f"[Leaderboard] Froze period {pid} ({count} winners)")

                # 2) Roll over if auto-renew is on. Back-to-back, same duration.
                if auto_renew:
                    duration = end_dt - start_dt
                    new_start = end_dt
                    new_end = end_dt + duration
                    new_id = conn.execute(
                        text(
                            """
                            INSERT INTO wager_leaderboard_periods
                                (discord_server_id, site, title, prize_pool, winner_count,
                                 start_date, end_date, stats_url, join_code, cta_url,
                                 auto_renew, status)
                            VALUES (:sid, :site, :title, :pool, :wc, :start, :end,
                                    :stats, :join, :cta, TRUE, 'active')
                            RETURNING id
                            """
                        ),
                        {
                            "sid": guild_id,
                            "site": site,
                            "title": title,
                            "pool": prize_pool,
                            "wc": winner_count,
                            "start": new_start,
                            "end": new_end,
                            "stats": stats_url,
                            "join": join_code,
                            "cta": cta_url,
                        },
                    ).scalar()
                    # Copy the prize grid into the new period.
                    conn.execute(
                        text(
                            """
                            INSERT INTO wager_leaderboard_prizes (period_id, rank, amount)
                            SELECT :new_id, rank, amount
                            FROM wager_leaderboard_prizes WHERE period_id = :old_id
                            """
                        ),
                        {"new_id": new_id, "old_id": pid},
                    )
                    logger.info(f"[Leaderboard] Auto-renewed period {pid} -> {new_id}")
        except Exception as e:
            # Fail safe: skip this guild and retry next tick. Never end/create
            # from a partial read (the raffle period-rollover lesson).
            logger.warning(f"[Leaderboard] auto-renew skipped for guild {guild_id}: {e}")
            continue


@leaderboard_sync_task.before_loop
async def before_leaderboard_sync_task():
    """Wait for the bot to be ready before syncing leaderboard periods."""
    await bot.wait_until_ready()
    await asyncio.sleep(15)
    logger.debug("[Leaderboard] 🏆 Starting leaderboard sync task...")


# -------------------------
# Command cooldowns and checks
# -------------------------
# Progressive cooldown tracking: {user_id: {command: attempt_count}}
progressive_cooldown_attempts = {}
_cooldown_last_sweep = None
# Entries unused for this long are dead for cooldown purposes (>> any real cooldown),
# so they can be evicted. Without this the dict grows with every distinct user forever.
_COOLDOWN_ENTRY_TTL_SECONDS = 3600


def _sweep_cooldown_attempts(now):
    """Throttled eviction of stale progressive-cooldown entries (runs at most every
    10 minutes) so progressive_cooldown_attempts stays bounded by recently-active
    users instead of every user who ever ran a cooldowned command."""
    global _cooldown_last_sweep
    if _cooldown_last_sweep and (now - _cooldown_last_sweep).total_seconds() < 600:
        return
    _cooldown_last_sweep = now
    for _uid in list(progressive_cooldown_attempts.keys()):
        _cmds = progressive_cooldown_attempts[_uid]
        for _cmd in list(_cmds.keys()):
            _last = _cmds[_cmd].get("last_use")
            if _last and (now - _last).total_seconds() > _COOLDOWN_ENTRY_TTL_SECONDS:
                del _cmds[_cmd]
        if not _cmds:
            del progressive_cooldown_attempts[_uid]


class CommandCooldowns:
    # Cooldown settings (base values, will increase progressively)
    LINK_COOLDOWN = commands.CooldownMapping.from_cooldown(
        1, 10, commands.BucketType.user
    )  # Start at 10s, +10s per attempt
    VERIFY_COOLDOWN = commands.CooldownMapping.from_cooldown(
        3, 300, commands.BucketType.user
    )  # 🔒 SECURITY: Max 3 attempts per 5 minutes
    LEADERBOARD_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 30, commands.BucketType.channel)
    WATCHTIME_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 15, commands.BucketType.user)
    UNLINK_COOLDOWN = commands.CooldownMapping.from_cooldown(1, 300, commands.BucketType.user)


def progressive_cooldown(base_seconds: int, increment_seconds: int, max_seconds: int):
    """
    Progressive cooldown that increases with each use.

    Args:
        base_seconds: Starting cooldown (e.g., 10)
        increment_seconds: How much to add per attempt (e.g., 10)
        max_seconds: Maximum cooldown cap (e.g., 60)

    Example: 10s -> 20s -> 30s -> 40s -> 50s -> 60s (capped)
    """

    async def predicate(ctx):
        # 🔒 ADMIN BYPASS: Admins skip cooldowns for testing
        if ctx.guild and ctx.author.guild_permissions.administrator:
            return True

        user_id = ctx.author.id
        command_name = ctx.command.name

        # Initialize tracking
        if user_id not in progressive_cooldown_attempts:
            progressive_cooldown_attempts[user_id] = {}

        if command_name not in progressive_cooldown_attempts[user_id]:
            progressive_cooldown_attempts[user_id][command_name] = {"count": 0, "last_use": None}

        tracking = progressive_cooldown_attempts[user_id][command_name]
        now = datetime.now(timezone.utc)

        # Memory: opportunistically evict stale entries (throttled internally). Only
        # touches entries with a set-but-old last_use, so the current fresh entry is safe.
        _sweep_cooldown_attempts(now)

        # Reset count if enough time has passed (2x max cooldown = full reset)
        if tracking["last_use"]:
            time_since_last = (now - tracking["last_use"]).total_seconds()
            if time_since_last > (max_seconds * 2):
                tracking["count"] = 0

        # Calculate progressive cooldown
        current_cooldown = min(base_seconds + (tracking["count"] * increment_seconds), max_seconds)

        # Check if user is still on cooldown
        if tracking["last_use"]:
            time_since_last = (now - tracking["last_use"]).total_seconds()
            if time_since_last < current_cooldown:
                retry_after = current_cooldown - time_since_last
                raise commands.CommandOnCooldown(None, retry_after, commands.BucketType.user)

        # Update tracking
        tracking["count"] += 1
        tracking["last_use"] = now

        return True

    return commands.check(predicate)


def dynamic_cooldown(cooldown_mapping):
    async def predicate(ctx):
        # 🔒 SECURITY: Admins bypass cooldowns for testing
        if ctx.guild and ctx.author.guild_permissions.administrator:
            return True

        bucket = cooldown_mapping.get_bucket(ctx.message)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            raise commands.CommandOnCooldown(bucket, retry_after, cooldown_mapping._type)
        return True

    return commands.check(predicate)


# -------------------------
# Commands
# -------------------------
# Note: Commands now work in all guilds (multiserver support)
# Each guild has its own settings loaded dynamically


@bot.hybrid_command(name="unlink")
@app_commands.default_permissions(manage_guild=True)
@commands.has_permissions(manage_guild=True)
async def cmd_unlink(ctx, member: discord.Member = None):
    """Admin command to unlink a user's Kick account from Discord.

    Usage:
    /unlink @user - Unlink another user's account (admin only)
    """
    # Reads the database before its first reply - acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    # Admin must specify a user
    if member is None:
        await ctx.send("❌ Usage: `/unlink @user`\n\nAdmins must specify which user to unlink.")
        return

    discord_id = member.id
    guild_id = ctx.guild.id if ctx.guild else None

    # Check if user has a linked account (Kick platform)
    with engine.connect() as conn:
        existing = conn.execute(
            text(
                "SELECT kick_name FROM links WHERE discord_id = :d AND discord_server_id = :guild_id AND platform = 'kick'"
            ),
            {"d": discord_id, "guild_id": guild_id},
        ).fetchone()

    if not existing:
        await ctx.send(f"❌ {member.mention} doesn't have a linked Kick account.")
        return

    kick_name = existing[0]

    # Unlink without confirmation (admin action) — Kick platform only
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM links WHERE discord_id = :d AND discord_server_id = :guild_id AND platform = 'kick'"),
            {"d": discord_id, "guild_id": guild_id},
        )

        # Also clean up any pending OAuth notifications
        conn.execute(
            text("DELETE FROM oauth_notifications WHERE discord_id = :d AND discord_server_id = :guild_id"),
            {"d": discord_id, "guild_id": guild_id},
        )

        # Clean up pending verifications
        conn.execute(
            text("DELETE FROM pending_links WHERE discord_id = :d AND discord_server_id = :guild_id"),
            {"d": discord_id, "guild_id": guild_id},
        )

    await ctx.send(
        f"🔓 Admin action: {member.mention}'s Kick account **{kick_name}** has been unlinked.\n"
        f"Their watchtime has been preserved."
    )


@cmd_unlink.error
async def unlink_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ This command is admin-only. Regular users cannot unlink accounts to prevent abuse.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid user. Usage: `/unlink @user`")


@bot.hybrid_command(name="updatekick")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def cmd_update_kick(ctx, member: discord.Member, new_kick_username: str):
    """[ADMIN] Update a user's linked Kick username.

    This also migrates all associated data (watchtime, points, raffle tickets, etc.)
    to the new username to prevent data loss.

    Usage:
    /updatekick @user newkickname - Update the Kick username for a linked user
    """
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    discord_id = member.id
    guild_id = ctx.guild.id if ctx.guild else None
    new_kick_username = new_kick_username.lower().strip()

    # Check if user has a linked account
    with engine.connect() as conn:
        existing = conn.execute(
            text("SELECT kick_name FROM links WHERE discord_id = :d AND discord_server_id = :guild_id"),
            {"d": discord_id, "guild_id": guild_id},
        ).fetchone()

    if not existing:
        await ctx.send(
            f"❌ {member.mention} doesn't have a linked Kick account. They need to link first via the link panel."
        )
        return

    old_kick_name = existing[0]

    if old_kick_name.lower() == new_kick_username.lower():
        await ctx.send(f"❌ The new username is the same as the current one: **{old_kick_name}**")
        return

    # Check if new username is already linked by someone else
    with engine.connect() as conn:
        existing_new = conn.execute(
            text("SELECT discord_id FROM links WHERE kick_name = :new_name AND discord_server_id = :guild_id"),
            {"new_name": new_kick_username, "guild_id": guild_id},
        ).fetchone()

    if existing_new and existing_new[0] != discord_id:
        await ctx.send(f"❌ The username **{new_kick_username}** is already linked to another Discord user.")
        return

    status_msg = await ctx.send(
        f"🔄 Updating **{old_kick_name}** → **{new_kick_username}**...\nMigrating all user data..."
    )

    # Update all tables that reference the kick username
    # Use MERGE logic: if new username already has data, combine it then delete old
    tables_updated = []

    with engine.begin() as conn:
        # 1. Update links table (Kick platform)
        conn.execute(
            text(
                """
                UPDATE links
                SET kick_name = :new_name, linked_at = CURRENT_TIMESTAMP
                WHERE discord_id = :d AND discord_server_id = :guild_id AND platform = 'kick'
            """
            ),
            {"new_name": new_kick_username, "d": discord_id, "guild_id": guild_id},
        )
        tables_updated.append("links")

        # 2. MERGE watchtime table (combine minutes if new username exists)
        # First check if new username already has watchtime
        existing_new_wt = conn.execute(
            text("SELECT minutes FROM watchtime WHERE username = :new_name AND discord_server_id = :guild_id"),
            {"new_name": new_kick_username, "guild_id": guild_id},
        ).fetchone()

        old_wt = conn.execute(
            text("SELECT minutes FROM watchtime WHERE username = :old_name AND discord_server_id = :guild_id"),
            {"old_name": old_kick_name, "guild_id": guild_id},
        ).fetchone()

        if old_wt:
            old_minutes = old_wt[0] or 0
            if existing_new_wt:
                # Merge: add old minutes to new, then delete old record
                new_minutes = existing_new_wt[0] or 0
                conn.execute(
                    text(
                        """
                        UPDATE watchtime
                        SET minutes = :total, last_active = CURRENT_TIMESTAMP
                        WHERE username = :new_name AND discord_server_id = :guild_id
                    """
                    ),
                    {"total": new_minutes + old_minutes, "new_name": new_kick_username, "guild_id": guild_id},
                )
                conn.execute(
                    text("DELETE FROM watchtime WHERE username = :old_name AND discord_server_id = :guild_id"),
                    {"old_name": old_kick_name, "guild_id": guild_id},
                )
                tables_updated.append(f"watchtime (merged {old_minutes}+{new_minutes}={old_minutes+new_minutes} min)")
            else:
                # Simple rename
                conn.execute(
                    text(
                        """
                        UPDATE watchtime
                        SET username = :new_name
                        WHERE username = :old_name AND discord_server_id = :guild_id
                    """
                    ),
                    {"new_name": new_kick_username, "old_name": old_kick_name, "guild_id": guild_id},
                )
                tables_updated.append(f"watchtime ({old_minutes} min)")

        # 3. Update raffle_tickets table - ONLY for the ACTIVE period
        # Unique constraint is (period_id, discord_id), so we update kick_name for this user
        # Only carry over tickets from the active period, not historical periods
        result = conn.execute(
            text(
                """
                UPDATE raffle_tickets rt
                SET kick_name = :new_name
                WHERE rt.discord_id = :d
                  AND rt.discord_server_id = :guild_id
                  AND rt.period_id = (
                      SELECT id FROM raffle_periods
                      WHERE discord_server_id = :guild_id AND status = 'active'
                      LIMIT 1
                  )
            """
            ),
            {"new_name": new_kick_username, "d": discord_id, "guild_id": guild_id},
        )
        if result.rowcount > 0:
            # Get current ticket count to show in the message
            ticket_info = conn.execute(
                text(
                    """
                    SELECT total_tickets FROM raffle_tickets rt
                    WHERE rt.discord_id = :d AND rt.discord_server_id = :guild_id
                      AND rt.period_id = (
                          SELECT id FROM raffle_periods
                          WHERE discord_server_id = :guild_id AND status = 'active'
                          LIMIT 1
                      )
                """
                ),
                {"d": discord_id, "guild_id": guild_id},
            ).fetchone()
            ticket_count = ticket_info[0] if ticket_info else 0
            tables_updated.append(f"raffle_tickets (active period, {ticket_count} tickets)")

        # 4. Handle raffle_watchtime_converted table (no discord_server_id - uses period_id)
        # Check if new username has records - if so, delete old to avoid conflicts
        existing_new_rwc = conn.execute(
            text("SELECT COUNT(*) FROM raffle_watchtime_converted WHERE kick_name = :new_name"),
            {"new_name": new_kick_username},
        ).scalar()

        if existing_new_rwc and existing_new_rwc > 0:
            # New username has conversion records - delete old username's records
            result = conn.execute(
                text("DELETE FROM raffle_watchtime_converted WHERE kick_name = :old_name"),
                {"old_name": old_kick_name},
            )
            if result.rowcount > 0:
                tables_updated.append(f"raffle_watchtime_converted (cleaned {result.rowcount} old)")
        else:
            # Safe to rename
            result = conn.execute(
                text(
                    """
                    UPDATE raffle_watchtime_converted
                    SET kick_name = :new_name
                    WHERE kick_name = :old_name
                """
                ),
                {"new_name": new_kick_username, "old_name": old_kick_name},
            )
            if result.rowcount > 0:
                tables_updated.append(f"raffle_watchtime_converted ({result.rowcount})")

        # 5. Update raffle_shuffle_links table (global table - scoped by discord_id)
        # Only update records belonging to this specific user
        result = conn.execute(
            text(
                """
                UPDATE raffle_shuffle_links
                SET kick_name = :new_name
                WHERE discord_id = :d AND kick_name = :old_name
            """
            ),
            {"new_name": new_kick_username, "old_name": old_kick_name, "d": discord_id},
        )
        if result.rowcount > 0:
            tables_updated.append(f"raffle_shuffle_links ({result.rowcount})")

        # 6. MERGE user_points table
        existing_new_pts = conn.execute(
            text(
                "SELECT points, total_earned, total_spent FROM user_points WHERE kick_username = :new_name AND discord_server_id = :guild_id"
            ),
            {"new_name": new_kick_username, "guild_id": guild_id},
        ).fetchone()

        old_pts = conn.execute(
            text(
                "SELECT points, total_earned, total_spent FROM user_points WHERE kick_username = :old_name AND discord_server_id = :guild_id"
            ),
            {"old_name": old_kick_name, "guild_id": guild_id},
        ).fetchone()

        if old_pts:
            if existing_new_pts:
                # Merge points
                conn.execute(
                    text(
                        """
                        UPDATE user_points
                        SET points = points + :old_pts,
                            total_earned = total_earned + :old_earned,
                            total_spent = total_spent + :old_spent,
                            last_updated = CURRENT_TIMESTAMP
                        WHERE kick_username = :new_name AND discord_server_id = :guild_id
                    """
                    ),
                    {
                        "old_pts": old_pts[0] or 0,
                        "old_earned": old_pts[1] or 0,
                        "old_spent": old_pts[2] or 0,
                        "new_name": new_kick_username,
                        "guild_id": guild_id,
                    },
                )
                conn.execute(
                    text("DELETE FROM user_points WHERE kick_username = :old_name AND discord_server_id = :guild_id"),
                    {"old_name": old_kick_name, "guild_id": guild_id},
                )
                tables_updated.append(f"user_points (merged +{old_pts[0] or 0} pts)")
            else:
                conn.execute(
                    text(
                        """
                        UPDATE user_points
                        SET kick_username = :new_name
                        WHERE kick_username = :old_name AND discord_server_id = :guild_id
                    """
                    ),
                    {"new_name": new_kick_username, "old_name": old_kick_name, "guild_id": guild_id},
                )
                tables_updated.append(f"user_points ({old_pts[0] or 0} pts)")

        # 7. Handle points_watchtime_converted table (may have unique constraint)
        existing_new_pwc = conn.execute(
            text(
                "SELECT COUNT(*) FROM points_watchtime_converted WHERE kick_username = :new_name AND discord_server_id = :guild_id"
            ),
            {"new_name": new_kick_username, "guild_id": guild_id},
        ).scalar()

        if existing_new_pwc and existing_new_pwc > 0:
            # New username has conversion records - delete old to avoid conflicts
            result = conn.execute(
                text(
                    "DELETE FROM points_watchtime_converted WHERE kick_username = :old_name AND discord_server_id = :guild_id"
                ),
                {"old_name": old_kick_name, "guild_id": guild_id},
            )
            if result.rowcount > 0:
                tables_updated.append(f"points_watchtime_converted (cleaned {result.rowcount} old)")
        else:
            result = conn.execute(
                text(
                    """
                    UPDATE points_watchtime_converted
                    SET kick_username = :new_name
                    WHERE kick_username = :old_name AND discord_server_id = :guild_id
                """
                ),
                {"new_name": new_kick_username, "old_name": old_kick_name, "guild_id": guild_id},
            )
            if result.rowcount > 0:
                tables_updated.append(f"points_watchtime_converted ({result.rowcount})")

        # 8. Update slot_requests table (pending requests)
        result = conn.execute(
            text(
                """
                UPDATE slot_requests
                SET kick_username = :new_name
                WHERE kick_username = :old_name AND discord_server_id = :guild_id
            """
            ),
            {"new_name": new_kick_username, "old_name": old_kick_name, "guild_id": guild_id},
        )
        if result.rowcount > 0:
            tables_updated.append(f"slot_requests ({result.rowcount})")

        # 9. Update raffle_gifted_subs table (uses period_id + gifter_discord_id)
        result = conn.execute(
            text(
                """
                UPDATE raffle_gifted_subs
                SET gifter_kick_name = :new_name
                WHERE gifter_kick_name = :old_name AND gifter_discord_id = :d
            """
            ),
            {"new_name": new_kick_username, "old_name": old_kick_name, "d": discord_id},
        )
        if result.rowcount > 0:
            tables_updated.append(f"raffle_gifted_subs ({result.rowcount})")

        # 10. Update raffle_shuffle_wagers table (uses period_id + discord_id)
        result = conn.execute(
            text(
                """
                UPDATE raffle_shuffle_wagers
                SET kick_name = :new_name
                WHERE kick_name = :old_name AND discord_id = :d
            """
            ),
            {"new_name": new_kick_username, "old_name": old_kick_name, "d": discord_id},
        )
        if result.rowcount > 0:
            tables_updated.append(f"raffle_shuffle_wagers ({result.rowcount})")

    await status_msg.edit(
        content=f"✅ Updated {member.mention}'s Kick username:\n"
        f"**{old_kick_name}** → **{new_kick_username}**\n\n"
        f"📦 Data migrated: {', '.join(tables_updated)}"
    )
    logger.info(
        f"[Admin] {ctx.author} updated {member}'s Kick: {old_kick_name} → {new_kick_username} | Tables: {tables_updated}"
    )


@cmd_update_kick.error
async def update_kick_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ This command requires Administrator permission.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Usage: `/updatekick @user newkickname`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid user. Usage: `/updatekick @user newkickname`")


@bot.hybrid_command(name="grantlinkrole", aliases=["synclinkedroles"])
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def cmd_grant_link_role(ctx, member: discord.Member = None):
    """[ADMIN] Grant the linked role to users who are already linked.

    Usage:
    /grantlinkrole - Grant role to ALL linked users in this server
    /grantlinkrole @user - Grant role to a specific linked user
    """
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    guild_id = ctx.guild.id if ctx.guild else None

    # Get the configured linked role ID
    with engine.connect() as conn:
        linked_role_id = conn.execute(
            text(
                """
                SELECT value FROM bot_settings
                WHERE key = 'kick_linked_role_id' AND discord_server_id = :guild_id
            """
            ),
            {"guild_id": guild_id},
        ).scalar()

    if not linked_role_id or not linked_role_id.strip():
        await ctx.send("❌ No linked role configured. Set it in Dashboard → Profile Settings → Kick Link Settings.")
        return

    role = ctx.guild.get_role(int(linked_role_id))
    if not role:
        await ctx.send(f"❌ Configured role ID `{linked_role_id}` not found in this server. Please update the setting.")
        return

    # Check bot permissions
    if not ctx.guild.me.guild_permissions.manage_roles:
        await ctx.send("❌ I don't have permission to manage roles.")
        return

    if role >= ctx.guild.me.top_role:
        await ctx.send(f"❌ The role **{role.name}** is higher than my highest role. I cannot assign it.")
        return

    if member:
        # Single user mode
        with engine.connect() as conn:
            linked = conn.execute(
                text("SELECT kick_name FROM links WHERE discord_id = :d AND discord_server_id = :guild_id"),
                {"d": member.id, "guild_id": guild_id},
            ).fetchone()

        if not linked:
            await ctx.send(f"❌ {member.mention} doesn't have a linked Kick account.")
            return

        if role in member.roles:
            await ctx.send(f"ℹ️ {member.mention} already has the **{role.name}** role.")
            return

        await member.add_roles(role, reason=f"Admin granted linked role (Kick: {linked[0]})")
        await ctx.send(f"✅ Granted **{role.name}** to {member.mention} (Kick: **{linked[0]}**)")
    else:
        # Bulk mode - grant to all linked users
        status_msg = await ctx.send("🔄 Scanning linked users and granting roles...")

        with engine.connect() as conn:
            linked_users = conn.execute(
                text("SELECT discord_id, kick_name FROM links WHERE discord_server_id = :guild_id"),
                {"guild_id": guild_id},
            ).fetchall()

        if not linked_users:
            await status_msg.edit(content="❌ No linked users found in this server.")
            return

        granted = 0
        already_had = 0
        not_in_server = 0
        errors = 0

        for discord_id, kick_name in linked_users:
            try:
                # Try cache first, then fetch from API if not cached
                member = ctx.guild.get_member(int(discord_id))
                if not member:
                    try:
                        member = await ctx.guild.fetch_member(int(discord_id))
                    except discord.NotFound:
                        not_in_server += 1
                        continue
                    except discord.HTTPException:
                        not_in_server += 1
                        continue

                if role in member.roles:
                    already_had += 1
                    continue

                await member.add_roles(role, reason=f"Bulk sync: Linked Kick account ({kick_name})")
                granted += 1
            except Exception as e:
                errors += 1
                logger.error(f"⚠️ Error granting role to {discord_id}: {e}")

        await status_msg.edit(
            content=f"✅ **Link Role Sync Complete**\n\n"
            f"**{role.name}** role:\n"
            f"• ✅ Granted to: **{granted}** users\n"
            f"• ℹ️ Already had role: **{already_had}** users\n"
            f"• ⚠️ Not in server: **{not_in_server}** users\n"
            f"• ❌ Errors: **{errors}**"
        )
        logger.info(
            f"[Admin] {ctx.author} synced linked roles: {granted} granted, {already_had} already had, {not_in_server} not in server"
        )


@cmd_grant_link_role.error
async def grant_link_role_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ This command requires Administrator permission.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid user. Usage: `/grantlinkrole` or `/grantlinkrole @user`")


@bot.hybrid_command(name="leaderboard")
async def cmd_leaderboard(ctx, top: int = 10):
    """Show top viewers by watchtime."""
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    if top > 25:
        top = 25

    guild_id = ctx.guild.id if ctx.guild else None
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT username, minutes FROM watchtime WHERE discord_server_id = :guild_id ORDER BY minutes DESC LIMIT :n"
            ),
            {"n": top, "guild_id": guild_id},
        ).fetchall()

    if not rows:
        await ctx.send("📊 No watchtime data yet. Start watching to appear on the leaderboard!")
        return

    embed = discord.Embed(title="🏆 Kick Watchtime Leaderboard", description=f"Top {len(rows)} viewers", color=0x53FC18)

    medals = ["🥇", "🥈", "🥉"]
    for i, (username, minutes) in enumerate(rows, start=1):
        medal = medals[i - 1] if i <= 3 else f"#{i}"
        hours = minutes / 60
        embed.add_field(name=f"{medal} {username}", value=f"⏱️ {minutes:.0f} min ({hours:.1f} hrs)", inline=False)

    await ctx.send(embed=embed)


@bot.hybrid_command(name="watchtime")
@dynamic_cooldown(CommandCooldowns.WATCHTIME_COOLDOWN)
async def cmd_watchtime(ctx, target: str = None):
    """
    Check watchtime for yourself or another user.
    Usage: /watchtime (check your own)
           /watchtime @user (admins - lookup by Discord mention)
           /watchtime <kick_username> (admins - lookup by Kick username)
    """
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    discord_id = ctx.author.id
    is_admin = ctx.guild and ctx.author.guild_permissions.administrator

    guild_id = ctx.guild.id if ctx.guild else None
    with engine.connect() as conn:
        # If target provided, check if admin
        if target:
            if not is_admin:
                await ctx.send("❌ Only administrators can check other users' watchtime.")
                return

            # Check if it's a Discord mention
            if target.startswith("<@") and target.endswith(">"):
                # Extract Discord ID from mention
                mention_id = target.replace("<@", "").replace(">", "").replace("!", "")
                try:
                    target_discord_id = int(mention_id)
                    # Look up their linked Kick name
                    link = conn.execute(
                        text("SELECT kick_name FROM links WHERE discord_id = :d AND discord_server_id = :guild_id"),
                        {"d": target_discord_id, "guild_id": guild_id},
                    ).fetchone()

                    if not link:
                        await ctx.send(f"❌ <@{target_discord_id}> doesn't have a linked Kick account.")
                        return

                    kick_name = link[0]
                except ValueError:
                    await ctx.send("❌ Invalid Discord mention.")
                    return
            else:
                # Admin lookup by Kick username
                kick_name = target.lower()

            watchtime = conn.execute(
                text("SELECT minutes FROM watchtime WHERE username = :u AND discord_server_id = :guild_id"),
                {"u": kick_name, "guild_id": guild_id},
            ).fetchone()

            if not watchtime or watchtime[0] == 0:
                await ctx.send(f"⏱️ No watchtime recorded for **{kick_name}**.")
                return
        else:
            # Regular user checking their own watchtime
            link = conn.execute(
                text("SELECT kick_name FROM links WHERE discord_id = :d AND discord_server_id = :guild_id"),
                {"d": discord_id, "guild_id": guild_id},
            ).fetchone()

            if not link:
                await ctx.send("❌ You haven't linked your Kick account yet.")
                return

            kick_name = link[0]

            # Get watchtime
            watchtime = conn.execute(
                text("SELECT minutes FROM watchtime WHERE username = :u AND discord_server_id = :guild_id"),
                {"u": kick_name, "guild_id": guild_id},
            ).fetchone()

            if not watchtime or watchtime[0] == 0:
                await ctx.send(f"⏱️ No watchtime recorded yet for **{kick_name}**. Start watching to earn time!")
                return

    minutes = watchtime[0]
    hours = minutes / 60

    # Load current role configuration
    current_roles = load_watchtime_roles()

    # Check which roles they've earned
    earned_roles = []
    for role_info in current_roles:
        if minutes >= role_info["minutes"]:
            earned_roles.append(role_info["name"])

    embed = discord.Embed(title=f"⏱️ Watchtime for {kick_name}", color=0x53FC18)
    embed.add_field(name="Total Time", value=f"{minutes:.0f} minutes ({hours:.1f} hours)", inline=False)

    if earned_roles:
        embed.add_field(name="Earned Roles", value="\n".join(earned_roles), inline=False)
    else:
        # Show next role milestone
        for role_info in current_roles:
            if minutes < role_info["minutes"]:
                remaining = role_info["minutes"] - minutes
                embed.add_field(
                    name="Next Role", value=f"**{role_info['name']}** in {remaining:.0f} more minutes", inline=False
                )
                break

    await ctx.send(embed=embed)


# -------------------------
# Admin Commands
# -------------------------
@bot.hybrid_command(name="tracking")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
# 🔒 SECURITY: Ensure command only works in the configured guild
async def toggle_tracking(ctx, action: str = None, subaction: str = None):
    """
    Admin command to control watchtime tracking.
    Usage: /tracking on|off|status
           /tracking force on|off|status
           /tracking debug on|off|status
    """
    # Reads the database before its first reply - acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    global stream_tracking_enabled, tracking_force_override, watchtime_debug_enabled

    # Support a force subcommand: /tracking force on|off|status
    if action is None or action.lower() == "status":
        status = "🟢 ENABLED" if stream_tracking_enabled else "🔴 DISABLED"
        force_status = "🟢 FORCE ON" if tracking_force_override else "🔴 FORCE OFF"
        debug_status = "🟢 DEBUG ON" if watchtime_debug_enabled else "🔴 DEBUG OFF"
        await ctx.send(
            f"**Watchtime Tracking Status:** {status}\n**Force override:** {force_status}\n**Debug logging:** {debug_status}"
        )
        return

    if action.lower() == "on":
        stream_tracking_enabled = True
        await ctx.send("✅ **Watchtime tracking ENABLED**\nUsers will now earn watchtime from chat activity.")
    elif action.lower() == "off":
        stream_tracking_enabled = False
        await ctx.send("⏸️ **Watchtime tracking DISABLED**\nUsers will NOT earn watchtime until re-enabled.")
    elif action.lower() == "force":
        if subaction is None or subaction.lower() == "status":
            force_status = "🟢 FORCE ON" if tracking_force_override else "🔴 FORCE OFF"
            await ctx.send(f"**Force override:** {force_status}")
            return

        if subaction.lower() == "on":
            tracking_force_override = True
            # Save to database
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                    INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                    VALUES ('tracking_force_override', 'true', :guild_id, CURRENT_TIMESTAMP)
                    ON CONFLICT (key, discord_server_id) DO UPDATE SET
                        value = 'true',
                        updated_at = CURRENT_TIMESTAMP
                """
                    ),
                    {"guild_id": ctx.guild.id},
                )
            await ctx.send(
                "🔒 **Watchtime FORCE override ENABLED**\nWatchtime updates will run regardless of live-detection checks."
            )
        elif subaction.lower() == "off":
            tracking_force_override = False
            # Save to database
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                    INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                    VALUES ('tracking_force_override', 'false', :guild_id, CURRENT_TIMESTAMP)
                    ON CONFLICT (key, discord_server_id) DO UPDATE SET
                        value = 'false',
                        updated_at = CURRENT_TIMESTAMP
                """
                    ),
                    {"guild_id": ctx.guild.id},
                )
            await ctx.send("🔓 **Watchtime FORCE override DISABLED**\nLive-detection checks will be enforced again.")
        else:
            await ctx.send(
                "❌ Invalid force option. Use: `/tracking force on` or `/tracking force off` or `/tracking force status`"
            )
    elif action.lower() == "debug":
        if subaction is None or subaction.lower() == "status":
            debug_status = "🟢 DEBUG ON" if watchtime_debug_enabled else "🔴 DEBUG OFF"
            await ctx.send(f"**Debug logging:** {debug_status}")
            return

        if subaction.lower() == "on":
            watchtime_debug_enabled = True
            await ctx.send("🐛 **Watchtime DEBUG logging ENABLED**\nDetailed debug messages will appear in logs.")
        elif subaction.lower() == "off":
            watchtime_debug_enabled = False
            await ctx.send("🔇 **Watchtime DEBUG logging DISABLED**\nDebug messages will be suppressed.")
        else:
            await ctx.send(
                "❌ Invalid debug option. Use: `/tracking debug on` or `/tracking debug off` or `/tracking debug status`"
            )
    else:
        await ctx.send(
            "❌ Invalid option. Use: `/tracking on`, `/tracking off`, `/tracking status`, or `/tracking force/debug ...`"
        )


@toggle_tracking.error
async def tracking_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="linklogs")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def link_logs_toggle(ctx, action: str = None):
    """
    Admin command to configure link attempt logging to Discord channel.
    Usage: /linklogs on|off|status

    When enabled, all account linking attempts (successful and failed) will be logged
    to the channel where this command is run.
    """
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    guild_id = ctx.guild.id
    channel_id = ctx.channel.id

    if action is None or action.lower() == "status":
        # Check current status
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT channel_id, enabled FROM link_logs_config
                WHERE guild_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()

            if not result:
                await ctx.send(
                    "📊 **Link Logging Status:** 🔴 NOT CONFIGURED\nUse `/linklogs on` to enable logging in this channel."
                )
                return

            log_channel = bot.get_channel(result[0])
            status = "🟢 ENABLED" if result[1] else "🔴 DISABLED"
            channel_mention = log_channel.mention if log_channel else f"<#{result[0]}>"
            await ctx.send(f"📊 **Link Logging Status:** {status}\n**Log Channel:** {channel_mention}")
            return

    if action.lower() == "on":
        # Enable logging in current channel
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                INSERT INTO link_logs_config (guild_id, channel_id, enabled, updated_at)
                VALUES (:guild_id, :channel_id, TRUE, CURRENT_TIMESTAMP)
                ON CONFLICT (guild_id) DO UPDATE SET
                    channel_id = :channel_id,
                    enabled = TRUE,
                    updated_at = CURRENT_TIMESTAMP
            """
                ),
                {"guild_id": guild_id, "channel_id": channel_id},
            )

        await ctx.send(
            f"✅ **Link logging ENABLED** in {ctx.channel.mention}\n\n"
            f"All account linking attempts will be logged here with:\n"
            f"• Discord user attempting to link\n"
            f"• Success/failure status\n"
            f"• Kick username\n"
            f"• Timestamp\n\n"
            f"Use `/linklogs off` to disable logging."
        )

    elif action.lower() == "off":
        # Disable logging
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                UPDATE link_logs_config
                SET enabled = FALSE, updated_at = CURRENT_TIMESTAMP
                WHERE guild_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            )

        await ctx.send("⏸️ **Link logging DISABLED**\nAccount linking attempts will no longer be logged.")

    else:
        await ctx.send("❌ Invalid option. Use: `/linklogs on`, `/linklogs off`, or `/linklogs status`")


@link_logs_toggle.error
async def link_logs_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="callblacklist", aliases=["srblacklist", "blockslotcall"])
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def slot_call_blacklist(ctx, action: str = None, kick_username: str = None, *, reason: str = None):
    """
    Admin command to blacklist Kick users from using !call/!sr commands.
    Usage:
      /callblacklist add <kick_username> [reason]
      /callblacklist remove <kick_username>
      /callblacklist list
      /callblacklist check <kick_username>
    """
    # Reads the database before its first reply - acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    if action is None or action.lower() not in ["add", "remove", "list", "check"]:
        await ctx.send(
            "**Slot Call Blacklist Management**\n\n"
            "**Usage:**\n"
            "• `/callblacklist add <kick_username> [reason]` - Block a user from using !call/!sr\n"
            "• `/callblacklist remove <kick_username>` - Unblock a user\n"
            "• `/callblacklist list` - Show all blacklisted users\n"
            "• `/callblacklist check <kick_username>` - Check if a user is blacklisted"
        )
        return

    if action.lower() == "add":
        if not kick_username:
            await ctx.send("❌ Please provide a Kick username: `/callblacklist add <kick_username> [reason]`")
            return

        kick_username_lower = kick_username.lower()

        with engine.begin() as conn:
            # Check if already blacklisted
            existing = conn.execute(
                text(
                    """
                SELECT 1 FROM slot_call_blacklist WHERE kick_username = :username AND discord_server_id = :guild_id
            """
                ),
                {"username": kick_username_lower, "guild_id": ctx.guild.id},
            ).fetchone()

            if existing:
                await ctx.send(f"⚠️ User `{kick_username}` is already blacklisted.")
                return

            # Add to blacklist
            conn.execute(
                text(
                    """
                INSERT INTO slot_call_blacklist (kick_username, reason, blacklisted_by, discord_server_id)
                VALUES (:username, :reason, :admin_id, :guild_id)
            """
                ),
                {
                    "username": kick_username_lower,
                    "reason": reason or "No reason provided",
                    "admin_id": ctx.author.id,
                    "guild_id": ctx.guild.id,
                },
            )

        await ctx.send(
            f"✅ **User blacklisted from !call/!sr**\n"
            f"**Kick Username:** `{kick_username}`\n"
            f"**Reason:** {reason or 'No reason provided'}\n"
            f"**By:** {ctx.author.mention}"
        )

    elif action.lower() == "remove":
        if not kick_username:
            await ctx.send("❌ Please provide a Kick username: `/callblacklist remove <kick_username>`")
            return

        kick_username_lower = kick_username.lower()

        with engine.begin() as conn:
            result = conn.execute(
                text(
                    """
                DELETE FROM slot_call_blacklist WHERE kick_username = :username AND discord_server_id = :guild_id
            """
                ),
                {"username": kick_username_lower, "guild_id": ctx.guild.id},
            )

            if result.rowcount == 0:
                await ctx.send(f"⚠️ User `{kick_username}` is not blacklisted.")
                return

        await ctx.send(f"✅ User `{kick_username}` removed from !call/!sr blacklist.")

    elif action.lower() == "list":
        with engine.connect() as conn:
            results = conn.execute(
                text(
                    """
                SELECT kick_username, reason, created_at
                FROM slot_call_blacklist
                WHERE discord_server_id = :guild_id
                ORDER BY created_at DESC
            """
                ),
                {"guild_id": ctx.guild.id},
            ).fetchall()

            if not results:
                await ctx.send("📋 No users are currently blacklisted from !call/!sr commands.")
                return

            embed = discord.Embed(
                title="🚫 Slot Call Blacklist",
                description=f"**{len(results)}** blacklisted user(s)",
                color=discord.Color.red(),
            )

            for username, reason, created_at in results[:25]:  # Limit to 25 for embed
                embed.add_field(
                    name=f"👤 {username}",
                    value=f"**Reason:** {reason}\n**Added:** {created_at.strftime('%Y-%m-%d')}",
                    inline=False,
                )

            if len(results) > 25:
                embed.set_footer(text=f"Showing first 25 of {len(results)} blacklisted users")

            await ctx.send(embed=embed)

    elif action.lower() == "check":
        if not kick_username:
            await ctx.send("❌ Please provide a Kick username: `/callblacklist check <kick_username>`")
            return

        kick_username_lower = kick_username.lower()

        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT reason, blacklisted_by, created_at
                FROM slot_call_blacklist
                WHERE kick_username = :username AND discord_server_id = :guild_id
            """
                ),
                {"username": kick_username_lower, "guild_id": ctx.guild.id},
            ).fetchone()

            if not result:
                await ctx.send(f"✅ User `{kick_username}` is **NOT** blacklisted.")
                return

            reason, admin_id, created_at = result
            admin = await bot.fetch_user(admin_id) if admin_id else None

            await ctx.send(
                f"🚫 User `{kick_username}` is **BLACKLISTED**\n"
                f"**Reason:** {reason}\n"
                f"**Added by:** {admin.mention if admin else 'Unknown'}\n"
                f"**Date:** {created_at.strftime('%Y-%m-%d %H:%M UTC')}"
            )


@slot_call_blacklist.error
async def slot_call_blacklist_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="roles")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def manage_roles(ctx, action: str = None, role_name: str = None, minutes: int = None):
    """
    Admin command to manage watchtime role thresholds.
    Usage:
      /roles list - Show current role configuration
      /roles add <role_name> <minutes> - Add a new role threshold
      /roles update <role_name> <minutes> - Update existing role threshold
      /roles remove <role_name> - Remove a role threshold
      /roles enable <role_name> - Enable a role
      /roles disable <role_name> - Disable a role

    Example: /roles add "Tier 4" 5000
    """
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    if action is None or action.lower() == "list":
        # Show current configuration
        guild_id = ctx.guild.id if ctx.guild else None
        with engine.connect() as conn:
            roles = conn.execute(
                text(
                    """
                SELECT role_name, minutes_required, enabled, display_order
                FROM watchtime_roles
                WHERE discord_server_id = :guild_id
                ORDER BY display_order ASC
            """
                ),
                {"guild_id": guild_id},
            ).fetchall()

        if not roles:
            await ctx.send("📋 No roles configured.")
            return

        embed = discord.Embed(
            title="🎯 Watchtime Role Configuration",
            description="Current role thresholds for automatic role assignment",
            color=0x53FC18,
        )

        for role_name, minutes, enabled, order in roles:
            hours = minutes / 60
            status = "✅ Enabled" if enabled else "❌ Disabled"
            embed.add_field(
                name=f"{order}. {role_name}",
                value=f"**{minutes:,} minutes** ({hours:.1f} hours)\n{status}",
                inline=False,
            )

        embed.set_footer(text="Use /roles add/update/remove to modify • Changes take effect immediately")
        await ctx.send(embed=embed)
        return

    if action.lower() == "add":
        if not role_name or minutes is None:
            await ctx.send('❌ Usage: `/roles add <role_name> <minutes>`\nExample: `/roles add "Tier 4" 5000`')
            return

        try:
            guild_id = ctx.guild.id if ctx.guild else None
            with engine.begin() as conn:
                # Get highest display order for this guild
                max_order = conn.execute(
                    text(
                        "SELECT COALESCE(MAX(display_order), 0) FROM watchtime_roles WHERE discord_server_id = :guild_id"
                    ),
                    {"guild_id": guild_id},
                ).fetchone()[0]

                # Insert new role
                conn.execute(
                    text(
                        """
                    INSERT INTO watchtime_roles (role_name, minutes_required, display_order, enabled, discord_server_id)
                    VALUES (:name, :minutes, :order, TRUE, :guild_id)
                """
                    ),
                    {"name": role_name, "minutes": minutes, "order": max_order + 1, "guild_id": guild_id},
                )

            await ctx.send(f"✅ Added role **{role_name}** at **{minutes:,} minutes** ({minutes/60:.1f} hours)")
        except Exception as e:
            await ctx.send(f"❌ Error adding role: {e}")

    elif action.lower() == "update":
        if not role_name or minutes is None:
            await ctx.send('❌ Usage: `/roles update <role_name> <minutes>`\nExample: `/roles update "Tier 1" 180`')
            return

        try:
            guild_id = ctx.guild.id if ctx.guild else None
            with engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    UPDATE watchtime_roles
                    SET minutes_required = :minutes, updated_at = CURRENT_TIMESTAMP
                    WHERE role_name = :name AND discord_server_id = :guild_id
                    RETURNING id
                """
                    ),
                    {"name": role_name, "minutes": minutes, "guild_id": guild_id},
                ).fetchone()

                if not result:
                    await ctx.send(f"❌ Role **{role_name}** not found.")
                    return

            await ctx.send(f"✅ Updated **{role_name}** to **{minutes:,} minutes** ({minutes/60:.1f} hours)")
        except Exception as e:
            await ctx.send(f"❌ Error updating role: {e}")

    elif action.lower() == "remove":
        if not role_name:
            await ctx.send('❌ Usage: `/roles remove <role_name>`\nExample: `/roles remove "Tier 4"`')
            return

        try:
            guild_id = ctx.guild.id if ctx.guild else None
            with engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    DELETE FROM watchtime_roles
                    WHERE role_name = :name AND discord_server_id = :guild_id
                    RETURNING id
                """
                    ),
                    {"name": role_name, "guild_id": guild_id},
                ).fetchone()

                if not result:
                    await ctx.send(f"❌ Role **{role_name}** not found.")
                    return

            await ctx.send(f"✅ Removed role **{role_name}** from configuration")
        except Exception as e:
            await ctx.send(f"❌ Error removing role: {e}")

    elif action.lower() in ["enable", "disable"]:
        if not role_name:
            await ctx.send(f"❌ Usage: `/roles {action} <role_name>`")
            return

        enabled = action.lower() == "enable"
        try:
            guild_id = ctx.guild.id if ctx.guild else None
            with engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    UPDATE watchtime_roles
                    SET enabled = :enabled, updated_at = CURRENT_TIMESTAMP
                    WHERE role_name = :name AND discord_server_id = :guild_id
                    RETURNING id
                """
                    ),
                    {"name": role_name, "enabled": enabled, "guild_id": guild_id},
                ).fetchone()

                if not result:
                    await ctx.send(f"❌ Role **{role_name}** not found.")
                    return

            status = "enabled" if enabled else "disabled"
            await ctx.send(f"✅ Role **{role_name}** {status}")
        except Exception as e:
            await ctx.send(f"❌ Error updating role: {e}")

    elif action.lower() == "members":
        if not role_name:
            await ctx.send('❌ Usage: `/roles members <role_name>`\nExample: `/roles members "Tier 1"`')
            return

        # Find the Discord role
        role = discord.utils.get(ctx.guild.roles, name=role_name)
        if not role:
            await ctx.send(f"❌ Discord role **{role_name}** not found in this server.")
            return

        # Get members with this role
        members = role.members

        if not members:
            await ctx.send(f"📋 No members have the **{role_name}** role yet.")
            return

        # Get watchtime for linked members only
        member_list = []
        linked_count = 0
        with engine.connect() as conn:
            for member in members[:50]:  # Check up to 50, but only show linked ones
                # Try to get their watchtime
                link = conn.execute(
                    text("SELECT kick_name FROM links WHERE discord_id = :d"), {"d": member.id}
                ).fetchone()

                if link:  # Only add linked members
                    linked_count += 1
                    kick_name = link[0]
                    watchtime = conn.execute(
                        text("SELECT minutes FROM watchtime WHERE username = :u"), {"u": kick_name}
                    ).fetchone()

                    if watchtime:
                        minutes = watchtime[0]
                        hours = minutes / 60
                        member_list.append(f"• {member.mention} - **{kick_name}** ({minutes:,} min / {hours:.1f}h)")
                    else:
                        member_list.append(f"• {member.mention} - **{kick_name}** (0 min)")

                    # Stop if we have 25 linked members
                    if linked_count >= 25:
                        break

        if not member_list:
            await ctx.send(f"📋 No linked members have the **{role_name}** role yet.")
            return

        # Create embed
        embed = discord.Embed(
            title=f"👥 Linked Members with {role_name}",
            description=f"Showing **{len(member_list)}** linked member{'s' if len(member_list) != 1 else ''} (Total with role: {len(members)})",
            color=role.color if role.color != discord.Color.default() else 0x53FC18,
        )

        # Split into chunks if too many
        chunk_size = 20
        for i in range(0, len(member_list), chunk_size):
            chunk = member_list[i : i + chunk_size]
            embed.add_field(
                name=(
                    f"Members {i+1}-{min(i+chunk_size, len(member_list))}"
                    if len(member_list) > chunk_size
                    else "Members"
                ),
                value="\n".join(chunk),
                inline=False,
            )

        if linked_count >= 25 and len(members) > linked_count:
            embed.set_footer(text=f"Showing first 25 linked members • {len(members) - linked_count} unlinked not shown")

        await ctx.send(embed=embed)

    else:
        await ctx.send(
            "❌ Invalid action. Available actions:\n"
            "• `/roles list` - Show current roles\n"
            "• `/roles add <name> <minutes>` - Add new role\n"
            "• `/roles update <name> <minutes>` - Update role threshold\n"
            "• `/roles remove <name>` - Remove role\n"
            "• `/roles enable/disable <name>` - Enable/disable role\n"
            "• `/roles members <name>` - List members with a role"
        )


@manage_roles.error
async def manage_roles_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="testsub")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def test_subscription(ctx, kick_username: str = None, sub_count: int = 1):
    """
    [ADMIN/DEBUG] Simulate a subscription event to test raffle ticket awarding
    Usage: /testsub <kick_username> [sub_count]
    Example: /testsub testuser123 5  (simulates 5 gifted subs)
    """
    guild_id = ctx.guild.id if ctx.guild else None
    if not guild_id:
        await ctx.send("❌ This command must be used in a server.")
        return

    gifted_sub_tracker = gifted_sub_trackers.get(guild_id)

    if not gifted_sub_tracker:
        await ctx.send(
            "❌ **Gifted sub tracker not initialized for this server!**\n\n"
            "**Possible reasons:**\n"
            "• Bot is still starting up (wait a few seconds)\n"
            "• Database connection failed\n"
            "• Raffle system initialization error\n\n"
            "Check the bot console for error messages."
        )
        return

    if not kick_username:
        await ctx.send("❌ Usage: `/testsub <kick_username> [sub_count]`\nExample: `/testsub testuser123 5`")
        return

    # Create a fake subscription event that matches Kick's structure
    fake_event = {
        "id": f"test_{int(datetime.now().timestamp())}",
        "sender": {"username": kick_username, "id": 99999},
        "gift_count": sub_count if sub_count > 1 else None,
        "months": 1 if sub_count == 1 else None,
    }

    await ctx.send(
        f"🧪 **Testing subscription event...**\n"
        f"User: `{kick_username}`\n"
        f"Type: {'Gifted ' + str(sub_count) + ' subs' if sub_count > 1 else 'Regular subscription'}"
    )

    # Process the fake event
    result = await gifted_sub_tracker.handle_gifted_sub_event(fake_event)

    # Show results
    embed = discord.Embed(
        title="🧪 Test Subscription Result",
        color=discord.Color.green() if result["status"] == "success" else discord.Color.red(),
        timestamp=datetime.utcnow(),
    )

    embed.add_field(name="Status", value=result["status"], inline=True)
    embed.add_field(name="Kick Username", value=kick_username, inline=True)
    embed.add_field(name="Sub Count", value=str(sub_count), inline=True)

    if result["status"] == "success":
        embed.add_field(name="✅ Tickets Awarded", value=str(result["tickets_awarded"]), inline=True)
        embed.add_field(name="Discord ID", value=str(result["discord_id"]), inline=True)
        embed.description = f"Successfully awarded **{result['tickets_awarded']} tickets** to {result['gifter']}"
    elif result["status"] == "not_linked":
        embed.description = (
            f"❌ User `{kick_username}` is not linked to a Discord account.\n"
            "They need to use this server's Link Account panel."
        )
        embed.add_field(name="Note", value="Sub was logged but no tickets awarded", inline=False)
    elif result["status"] == "duplicate":
        embed.description = f"⚠️ This event was already processed (duplicate)"
    elif result["status"] == "no_active_period":
        embed.description = f"❌ No active raffle period found.\nUse raffle commands to create a new period."
    else:
        embed.description = f"❌ Error: {result.get('error', 'Unknown error')}"

    embed.set_footer(text=f"Test performed by {ctx.author.name}")
    await ctx.send(embed=embed)


@test_subscription.error
async def test_subscription_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="convertwatchtime")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def convert_watchtime_manual(ctx):
    """
    [ADMIN/DEBUG] Manually trigger watchtime to tickets conversion
    Usage: /convertwatchtime
    """
    if not engine:
        await ctx.send("❌ Database not initialized!")
        return

    await ctx.send("🔄 Converting watchtime to raffle tickets...")

    try:
        from raffle_system.watchtime_converter import WatchtimeConverter

        converter = WatchtimeConverter(engine)
        result = await converter.convert_watchtime_to_tickets()

        embed = discord.Embed(
            title="🎟️ Watchtime Conversion Result",
            color=discord.Color.blue() if result["status"] == "success" else discord.Color.red(),
            timestamp=datetime.utcnow(),
        )

        embed.add_field(name="Status", value=result["status"], inline=True)
        embed.add_field(name="Users Converted", value=str(result.get("conversions", 0)), inline=True)

        if result["status"] == "success" and result.get("details"):
            details_text = "\n".join(
                [
                    f"• **{d['kick_name']}**: {d['hours_converted']}h → {d['tickets_awarded']} tickets"
                    for d in result["details"][:10]
                ]
            )
            embed.add_field(name="Conversions", value=details_text or "None", inline=False)
        elif result["status"] == "no_active_period":
            embed.description = "❌ No active raffle period found"
        elif result["status"] == "no_users":
            embed.description = "ℹ️ No linked users with convertible watchtime (need 60+ minutes)"
        elif result["status"] == "error":
            embed.description = f"❌ Error: {result.get('error', 'Unknown')}"

        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@convert_watchtime_manual.error
async def convert_watchtime_manual_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="fixwatchtime")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def fix_watchtime_for_current_period(ctx):
    """
    [ADMIN] Fix watchtime tracking for current period by snapshotting existing totals
    This prevents re-awarding tickets for old watchtime
    Usage: /fixwatchtime
    """
    if not engine:
        await ctx.send("❌ Database not initialized!")
        return

    await ctx.send("🔄 Fixing watchtime tracking for current period...")

    try:
        from raffle_system.database import get_current_period

        # Get current period
        period = get_current_period(engine)
        if not period:
            await ctx.send("❌ No active raffle period found!")
            return

        period_id = period["id"]

        with engine.begin() as conn:
            # First, get list of users who don't have a snapshot yet
            users_to_snapshot = conn.execute(
                text(
                    """
                SELECT w.username, w.minutes
                FROM watchtime w
                WHERE w.minutes > 0
                AND NOT EXISTS (
                    SELECT 1 FROM raffle_watchtime_converted c
                    WHERE c.period_id = :period_id
                    AND LOWER(c.kick_name) = LOWER(w.username)
                )
            """
                ),
                {"period_id": period_id},
            ).fetchall()

            # Insert snapshots for users without one
            affected = 0
            for username, minutes in users_to_snapshot:
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_watchtime_converted (period_id, kick_name, minutes_converted, tickets_awarded)
                    VALUES (:period_id, :username, :minutes, 0)
                """
                    ),
                    {"period_id": period_id, "username": username, "minutes": minutes},
                )
                affected += 1

        embed = discord.Embed(
            title="✅ Watchtime Tracking Fixed",
            description=f"Snapshotted current watchtime for period #{period_id}",
            color=discord.Color.green(),
            timestamp=datetime.utcnow(),
        )

        embed.add_field(name="Users Updated", value=str(affected), inline=True)
        embed.add_field(name="Effect", value="Only NEW watchtime will be converted from now on", inline=False)

        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@fix_watchtime_for_current_period.error
async def fix_watchtime_for_current_period_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="checkwatchtime")
async def check_watchtime_conversion(ctx, kick_username: str = None):
    """
    Check watchtime conversion status for a user
    Usage: /checkwatchtime [kick_username]
    """
    # Reads the database before its first reply - acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    if not engine:
        await ctx.send("❌ Database not initialized!")
        return

    # If no username provided, try to get from links table
    if not kick_username:
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT kick_name FROM links
                    WHERE discord_id = :discord_id AND discord_server_id = :guild_id
                """
                    ),
                    {"discord_id": ctx.author.id, "guild_id": ctx.guild.id},
                )
                row = result.fetchone()
                if row:
                    kick_username = row[0]
                else:
                    await ctx.send("❌ You need to link your account first using this server's Link Account panel.")
                    return
        except Exception as e:
            await ctx.send(f"❌ Error looking up your linked account: {e}")
            return

    try:
        from raffle_system.watchtime_converter import WatchtimeConverter

        converter = WatchtimeConverter(engine)
        info = converter.get_unconverted_watchtime(kick_username)

        if not info:
            await ctx.send(f"❌ No watchtime data found for **{kick_username}**")
            return

        embed = discord.Embed(
            title=f"⏱️ Watchtime Status: {kick_username}", color=discord.Color.blue(), timestamp=datetime.utcnow()
        )

        total_hours = info["total_minutes"] / 60
        converted_hours = info["converted_minutes"] / 60
        unconverted_hours = info["unconverted_minutes"] / 60

        embed.add_field(name="Total Watchtime", value=f"{total_hours:.1f}h ({info['total_minutes']} min)", inline=False)
        embed.add_field(
            name="Already Converted", value=f"{converted_hours:.1f}h ({info['converted_minutes']} min)", inline=True
        )
        embed.add_field(
            name="Not Yet Converted", value=f"{unconverted_hours:.1f}h ({info['unconverted_minutes']} min)", inline=True
        )
        embed.add_field(
            name="Convertible Now",
            value=f"{info['convertible_hours']}h → {info['potential_tickets']} tickets",
            inline=False,
        )

        if info["unconverted_minutes"] < 60:
            embed.set_footer(text=f"Need {60 - info['unconverted_minutes']} more minutes to convert next hour")
        else:
            embed.set_footer(text="Watchtime converts automatically every 10 minutes")

        await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")


@bot.hybrid_command(name="commandlist", aliases=["commands"])
async def command_list(ctx):
    """
    Show available bot commands for regular users
    Usage: /commandlist
    """
    embed = discord.Embed(
        title="📋 User Commands",
        description="Commands available to everyone",
        color=discord.Color.blue(),
        timestamp=datetime.utcnow(),
    )

    # Account Linking
    embed.add_field(
        name="🔗 Account Linking",
        value="Use this server's Link Account panel to connect Kick or Twitch.",
        inline=False,
    )

    # Watchtime & Stats
    embed.add_field(
        name="⏱️ Watchtime & Stats",
        value=(
            "`/watchtime [user]` - Check your or someone's watchtime\n" "`/leaderboard` - View watchtime leaderboard"
        ),
        inline=False,
    )

    # Points & Shop
    embed.add_field(
        name="💰 Points",
        value=("`/points` - Check your points balance\n" "`/pointslb` - View points leaderboard"),
        inline=False,
    )

    # Raffle System
    embed.add_field(
        name="🎰 Monthly Raffle",
        value=(
            "`/tickets` - View your raffle tickets\n"
            "`/raffleboard` - View ticket leaderboard\n"
            "`/raffleinfo` - View raffle period information\n"
            "`/fair` - How to verify a provably-fair draw"
        ),
        inline=False,
    )

    # Gambling
    embed.add_field(
        name="🎲 Gambling (Provably Fair)",
        value=(
            "`/bj <amount>` - Play blackjack\n"
            "`/roll <amount>` - Roll 1-100 for multiplied payout\n"
            "`/double <amount>` - Double or nothing"
        ),
        inline=False,
    )

    embed.set_footer(text="Admins: Use /admincommands to see administrator commands")

    await ctx.send(embed=embed)


@bot.hybrid_command(name="admincommands", aliases=["adminhelp"])
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def admin_command_list(ctx):
    """
    Show all available administrator commands
    Usage: /admincommands
    """
    embed = discord.Embed(
        title="🔧 Administrator Commands",
        description="Commands available to server administrators",
        color=discord.Color.red(),
        timestamp=datetime.utcnow(),
    )

    # Tracking Control
    embed.add_field(
        name="📡 Tracking Control",
        value=(
            "`/tracking [on|off|status]` - Control watchtime tracking\n"
            "`/tracking force [on|off]` - Force tracking override\n"
            "`/tracking debug [on|off]` - Toggle debug mode\n"
            "`/linklogs` - View recent account links"
        ),
        inline=False,
    )

    # Account Management
    embed.add_field(
        name="🔗 Account Management",
        value=(
            "`/unlink @user` - Unlink a user's Kick account\n"
            "`/updatekick @user <newname>` - Update linked Kick username\n"
            "`/grantlinkrole [@user]` - Grant linked role (or sync all)"
        ),
        inline=False,
    )

    # Role Management
    embed.add_field(
        name="👥 Role Management",
        value=(
            "`/roles list` - Show role configuration\n"
            "`/roles add <name> <minutes>` - Add role threshold\n"
            "`/roles update <name> <minutes>` - Update threshold\n"
            "`/roles remove <name>` - Remove role\n"
            "`/roles enable/disable <name>` - Toggle role\n"
            "`/roles members <name>` - List members with role"
        ),
        inline=False,
    )

    # Raffle Management
    embed.add_field(
        name="🎲 Raffle Management",
        value=(
            "`/rafflestart [start_day] [end_day]` - Start new raffle period\n"
            "`/raffleend` - End current raffle period\n"
            "`/raffledraw [prize]` - Draw raffle winner\n"
            "`/rafflestats [@user]` - View raffle statistics\n"
            "`/rafflegive <@user> <amount> [reason]` - Award tickets\n"
            "`/raffleremove <@user> <amount> [reason]` - Remove tickets\n"
            "`/convertwatchtime` - Manually convert watchtime\n"
            "`/fixwatchtime` - Fix watchtime tracking"
        ),
        inline=False,
    )

    # Slot Requests
    embed.add_field(
        name="🎰 Slot Requests",
        value=(
            "`/slotpanel` - Create/update slot request panel\n"
            "`/callblacklist add <username> [reason]` - Block user from requests\n"
            "`/callblacklist remove <username>` - Unblock user\n"
            "`/callblacklist list` - View blacklisted users"
        ),
        inline=False,
    )

    # Gambling Admin
    embed.add_field(
        name="🎲 Gambling",
        value="`/setgamblechannel #channel` - Restrict gambling to a channel",
        inline=False,
    )

    # Testing & Debug
    embed.add_field(
        name="🧪 Testing & Debug",
        value=(
            "`/testsub <kick_username> [count]` - Test subscription event\n"
            "`/systemstatus` - Check system initialization\n"
            "`/health` - Bot health check"
        ),
        inline=False,
    )

    # Setup Commands
    embed.add_field(
        name="⚙️ Setup",
        value=(
            "`/setup_link_panel` - Create button-based linking panel\n"
            "`/post_link_info` - Post linking instructions\n"
            "`/creategtbpanel` - Create GTB control panel\n"
            "`/postshop` - Post/update point shop embed"
        ),
        inline=False,
    )

    embed.set_footer(text="Regular users: Use /commandlist to see user commands")

    await ctx.send(embed=embed)


@bot.hybrid_command(name="systemstatus")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def raffle_system_info(ctx):
    """
    [ADMIN/DEBUG] Check raffle system initialization status
    Usage: /systemstatus
    """
    guild_id = ctx.guild.id if ctx.guild else None
    if not guild_id:
        await ctx.send("❌ This command must be used in a server.")
        return

    # Get guild-specific trackers
    gifted_sub_tracker = gifted_sub_trackers.get(guild_id)
    shuffle_tracker = shuffle_trackers.get(guild_id)
    slot_call_tracker = slot_call_trackers.get(guild_id)

    embed = discord.Embed(title="🎰 Raffle System Status", color=discord.Color.blue(), timestamp=datetime.utcnow())

    # Check each component
    sub_status = "✅ Initialized" if gifted_sub_tracker else "❌ Not initialized"
    shuffle_status = "✅ Initialized" if shuffle_tracker else "❌ Not initialized"
    slot_status = "✅ Initialized" if slot_call_tracker else "❌ Not initialized"

    embed.add_field(name="Gifted Sub Tracker", value=sub_status, inline=False)
    embed.add_field(name="Shuffle Tracker", value=shuffle_status, inline=False)
    embed.add_field(name="Slot Call Tracker", value=slot_status, inline=False)

    # Check database
    db_status = "✅ Connected" if engine else "❌ Not connected"
    embed.add_field(name="Database", value=db_status, inline=False)

    # Check active raffle period
    if engine:
        try:
            period = get_current_period(engine)
            if period:
                period_info = f"✅ Active Period #{period.get('period_number', period['id'])}\n"
                period_info += f"Started: {period['start_date'].strftime('%Y-%m-%d')}\n"
                period_info += f"Status: {period['status']}"
            else:
                period_info = "❌ No active period"
            embed.add_field(name="Current Raffle Period", value=period_info, inline=False)
        except Exception as e:
            embed.add_field(name="Current Raffle Period", value=f"❌ Error: {str(e)[:100]}", inline=False)

    embed.set_footer(
        text=f"Bot uptime: {datetime.now() - bot.uptime_start if hasattr(bot, 'uptime_start') else 'Unknown'}"
    )

    await ctx.send(embed=embed)


@raffle_system_info.error
async def raffle_system_info_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to use this command.")


@bot.hybrid_command(name="setup_link_panel")
@app_commands.default_permissions(manage_guild=True)
@commands.has_permissions(manage_guild=True)
async def setup_link_panel(ctx, emoji: str = "🔗"):
    """
    Admin command to create a pinned message for reaction-based OAuth linking.
    Usage: /setup_link_panel [emoji]
    Default emoji: 🔗

    Users can react to the pinned message to link their Kick account via OAuth.
    """

    if not OAUTH_BASE_URL or not KICK_CLIENT_ID:
        await ctx.send(
            "❌ OAuth linking is not configured on this bot.\n"
            "Please set OAUTH_BASE_URL and KICK_CLIENT_ID environment variables."
        )
        return

    # Create the embed for the pinned message
    embed = discord.Embed(
        title="🎮 Link Your Kick Account",
        description=f"React with {emoji} below to link your Discord account with your Kick account!",
        color=0x53FC18,
    )
    embed.set_footer(text="Contact an admin if you need to unlink your account")

    # Send the message
    message = await ctx.send(embed=embed)

    # Add the reaction
    await message.add_reaction(emoji)

    # Pin the message
    try:
        await message.pin()
    except discord.Forbidden:
        await ctx.send("⚠️ I don't have permission to pin messages. Please pin the message manually.")
    except discord.HTTPException as e:
        await ctx.send(f"⚠️ Failed to pin message: {e}")

    # Store in database
    with engine.begin() as conn:
        # Remove any existing link panel for this channel (only one per channel)
        conn.execute(
            text(
                """
            DELETE FROM link_panels
            WHERE guild_id = :g AND channel_id = :c
        """
            ),
            {"g": ctx.guild.id, "c": ctx.channel.id},
        )

        # Insert new link panel
        conn.execute(
            text(
                """
            INSERT INTO link_panels (guild_id, channel_id, message_id, emoji)
            VALUES (:g, :c, :m, :e)
        """
            ),
            {"g": ctx.guild.id, "c": ctx.channel.id, "m": message.id, "e": emoji},
        )

    await ctx.send(f"✅ Link panel created! Users can now react with {emoji} to start the OAuth linking process.")


@setup_link_panel.error
async def setup_link_panel_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need 'Manage Server' permission to use this command.")


@bot.hybrid_command(name="post_link_info")
@app_commands.default_permissions(manage_guild=True)
@commands.has_permissions(manage_guild=True)
async def post_link_info(ctx):
    """
    Admin command to post an informational embed explaining why users should link their accounts.
    Usage: /post_link_info

    This creates a detailed explanation of the benefits of linking Kick accounts.
    """

    # Create the detailed embed
    embed = discord.Embed(
        title="🔗 Link Your Kick Account to Discord!",
        description="Connect your Kick account to unlock exclusive benefits in Maikelele's community!",
        color=0x53FC18,
    )

    embed.add_field(name="📝 Why Link Your Account?", value="** **", inline=False)  # Spacer

    embed.add_field(
        name="🎁 Enter Giveaways",
        value="• Only linked accounts can participate\n• Ensures fair distribution to real viewers\n• No alt accounts allowed",
        inline=False,
    )

    embed.add_field(
        name="🏆 Automatic Role Rewards",
        value="• Earn roles based on Kick chat activity\n• Get recognized for your watch time",
        inline=False,
    )

    embed.add_field(
        name="🛡️ Verified Member Status",
        value="• Prove you're a real supporter\n• Stand out in the community\n• Help keep the server authentic",
        inline=False,
    )

    embed.add_field(
        name="⚡ How to Link (Easy!)",
        value="React with 🔗 on the pinned message above!\n\n1. Click the 🔗 reaction\n2. Check your DMs for a secure link\n3. Authorize with your Kick account\n4. Done! Your accounts are now linked\n\n*Note: Contact an admin if you need to unlink*",
        inline=False,
    )

    embed.set_footer(text="🔒 Your data is secure • Takes less than 30 seconds")

    # Send the embed
    await ctx.send(embed=embed)

    # Prefix invocations leave a command message; slash interactions do not.
    if ctx.interaction is None:
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass  # If we can't delete, no big deal
        except discord.HTTPException:
            pass


@post_link_info.error
async def post_link_info_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need 'Manage Server' permission to use this command.")


@bot.hybrid_command(name="health")
@app_commands.default_permissions(manage_guild=True)
@commands.has_permissions(manage_guild=True)
async def health_check(ctx):
    """
    Admin command to check if all bot systems are functioning correctly.
    Usage: /health

    Checks:
    - Discord connection
    - Database connection
    - Kick API accessibility
    - OAuth configuration
    - Background tasks status
    - WebSocket connection
    """

    embed = discord.Embed(title="🏥 System Health Check", description="Checking all bot systems...", color=0x3498DB)

    status_msg = await ctx.send(embed=embed)

    checks = []
    overall_status = "✅ All Systems Operational"
    has_warnings = False
    has_errors = False

    # 1. Discord Connection
    try:
        latency_ms = round(bot.latency * 1000, 2)
        if latency_ms < 200:
            checks.append(f"✅ **Discord Connection**: {latency_ms}ms")
        elif latency_ms < 500:
            checks.append(f"⚠️ **Discord Connection**: {latency_ms}ms (Slow)")
            has_warnings = True
        else:
            checks.append(f"❌ **Discord Connection**: {latency_ms}ms (Very Slow)")
            has_errors = True
    except Exception as e:
        checks.append(f"❌ **Discord Connection**: Error - {str(e)}")
        has_errors = True

    # 2. Database Connection
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            result.fetchone()
        db_type = "PostgreSQL" if "postgresql" in DATABASE_URL else "SQLite"
        checks.append(f"✅ **Database Connection**: {db_type} OK")
    except Exception as e:
        checks.append(f"❌ **Database Connection**: {str(e)[:50]}")
        has_errors = True

    # 3. Kick API Check
    try:
        guild_id = ctx.guild.id if ctx.guild else None
        guild_settings = get_guild_settings(guild_id) if guild_id else None

        if guild_settings:
            kick_channel = guild_settings.kick_channel
            kick_chatroom_id = guild_settings.kick_chatroom_id

            if kick_chatroom_id:
                checks.append(f"✅ **Kick Chatroom ID**: Configured ({kick_chatroom_id})")
            elif kick_channel:
                chatroom_id = await fetch_chatroom_id(kick_channel)
                if chatroom_id:
                    checks.append(f"✅ **Kick API**: Accessible (ID: {chatroom_id})")
                else:
                    checks.append(f"⚠️ **Kick API**: Could not fetch chatroom ID")
                    has_warnings = True
            else:
                checks.append(f"⚠️ **Kick Channel**: Not configured for this server")
                has_warnings = True
        else:
            checks.append(f"⚠️ **Settings**: Not loaded for this server")
            has_warnings = True
    except Exception as e:
        checks.append(f"❌ **Kick API**: {str(e)[:50]}")
        has_errors = True

    # 4. OAuth Configuration
    oauth_checks = []
    if OAUTH_BASE_URL:
        oauth_checks.append("✅ Base URL configured")
    else:
        oauth_checks.append("❌ Base URL missing")
        has_errors = True

    if KICK_CLIENT_ID:
        oauth_checks.append("✅ Client ID configured")
    else:
        oauth_checks.append("❌ Client ID missing")
        has_errors = True

    if OAUTH_SECRET_KEY:
        oauth_checks.append("✅ Secret key configured")
    else:
        oauth_checks.append("❌ Secret key missing")
        has_errors = True

    oauth_status = " | ".join(oauth_checks)
    checks.append(f"**OAuth Config**: {oauth_status}")

    # 5. Background Tasks
    task_statuses = []

    if update_watchtime_task.is_running():
        task_statuses.append("✅ Watchtime tracker")
    else:
        task_statuses.append("❌ Watchtime tracker")
        has_errors = True

    if update_roles_task.is_running():
        task_statuses.append("✅ Role updater")
    else:
        task_statuses.append("❌ Role updater")
        has_errors = True

    if check_oauth_notifications_task.is_running():
        task_statuses.append("✅ OAuth checker")
    else:
        task_statuses.append("❌ OAuth checker")
        has_errors = True

    if cleanup_pending_links_task.is_running():
        task_statuses.append("✅ Cleanup task")
    else:
        task_statuses.append("❌ Cleanup task")
        has_errors = True

    checks.append(f"**Background Tasks**: {' | '.join(task_statuses)}")

    # 6. Database Tables Check
    try:
        guild_id = ctx.guild.id if ctx.guild else None
        with engine.connect() as conn:
            tables_check = []

            # Check watchtime table for this guild
            result = conn.execute(
                text("SELECT COUNT(*) FROM watchtime WHERE discord_server_id = :guild_id"), {"guild_id": guild_id}
            )
            watchtime_count = result.fetchone()[0]
            tables_check.append(f"{watchtime_count} viewers")

            # Check links table for this guild
            result = conn.execute(
                text("SELECT COUNT(*) FROM links WHERE discord_server_id = :guild_id"), {"guild_id": guild_id}
            )
            links_count = result.fetchone()[0]
            tables_check.append(f"{links_count} linked accounts")

            # Check watchtime_roles table for this guild
            result = conn.execute(
                text("SELECT COUNT(*) FROM watchtime_roles WHERE enabled = true AND discord_server_id = :guild_id"),
                {"guild_id": guild_id},
            )
            roles_count = result.fetchone()[0]
            tables_check.append(f"{roles_count} active roles")

            checks.append(f"✅ **Database Stats** (This Server): {' | '.join(tables_check)}")
    except Exception as e:
        checks.append(f"⚠️ **Database Stats**: {str(e)[:50]}")
        has_warnings = True

    # 7. WebSocket Status
    if hasattr(bot, "ws") and bot.ws:
        checks.append(f"✅ **WebSocket**: Connected")
    else:
        checks.append(f"⚠️ **WebSocket**: Not connected")
        has_warnings = True

    # 8. Uptime
    if hasattr(bot, "uptime_start"):
        uptime = datetime.now() - bot.uptime_start
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        checks.append(f"⏱️ **Uptime**: {hours}h {minutes}m {seconds}s")

    # Determine overall status
    if has_errors:
        overall_status = "❌ System Issues Detected"
        color = 0xE74C3C  # Red
    elif has_warnings:
        overall_status = "⚠️ System Operational with Warnings"
        color = 0xF39C12  # Orange
    else:
        overall_status = "✅ All Systems Operational"
        color = 0x2ECC71  # Green

    # Update embed
    embed = discord.Embed(
        title="🏥 System Health Check", description=overall_status, color=color, timestamp=datetime.now()
    )

    for check in checks:
        embed.add_field(name="\u200b", value=check, inline=False)

    embed.set_footer(text=f"Requested by {ctx.author.display_name}")

    await status_msg.edit(embed=embed)


@health_check.error
async def health_check_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need 'Manage Server' permission to use this command.")


# -------------------------
# Helper Functions
# -------------------------
async def sync_shuffle_role_on_startup(bot, engine):
    """Sync Shuffle Code User role with verified links on bot startup"""
    try:
        # Sync for all guilds
        for guild in bot.guilds:
            # Resolve the verified-Shuffle role from the per-guild dashboard
            # setting (shuffle_verified_role_id). Fall back to the legacy
            # name lookup for servers that haven't configured the ID yet.
            shuffle_role = None
            role_id = get_guild_settings(guild.id).get_int("shuffle_verified_role_id")
            if role_id:
                shuffle_role = guild.get_role(role_id)
            if not shuffle_role:
                shuffle_role = discord.utils.get(guild.roles, name="Shuffle Code User")
            if not shuffle_role:
                continue  # Skip guilds with no verified-Shuffle role configured

            # Get all verified Shuffle links for this guild
            with engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT DISTINCT discord_id
                    FROM raffle_shuffle_links
                    WHERE verified = TRUE
                        AND (discord_server_id IS NULL OR discord_server_id = :guild_id)
                """
                    ),
                    {"guild_id": guild.id},
                )
                verified_discord_ids = {row[0] for row in result.fetchall()}

            if not verified_discord_ids:
                continue  # No verified links for this guild

            added = 0
            removed = 0

            # Remove role from users without verified links
            members_with_role = shuffle_role.members
            for member in members_with_role:
                if member.id not in verified_discord_ids:
                    try:
                        await member.remove_roles(shuffle_role, reason="Startup sync: No verified Shuffle link")
                        removed += 1
                    except Exception as e:
                        logger.warning(f"⚠️ Could not remove Shuffle role from {member}: {e}")

            # Add role to verified users who don't have it
            for discord_id in verified_discord_ids:
                member = guild.get_member(discord_id)
                if member and shuffle_role not in member.roles:
                    try:
                        await member.add_roles(shuffle_role, reason="Startup sync: Has verified Shuffle link")
                        added += 1
                    except Exception as e:
                        logger.warning(f"⚠️ Could not add Shuffle role to {member}: {e}")

            if added > 0 or removed > 0:
                logger.info(f"✅ Shuffle role sync for {guild.name}: +{added}, -{removed}")

    except Exception as e:
        logger.error(f"⚠️ Error syncing Shuffle roles on startup: {e}")


# -------------------------
# Bot events
# -------------------------
@bot.event
async def on_guild_remove(guild):
    """Free per-guild state when the bot is removed from a guild.

    Without this, the guild's Kick websocket connection + maintain/sender tasks keep
    running (and reconnecting) forever, and its entries in the per-guild registries
    (accumulators, trackers, managers, panels) never get released.
    """
    gid = guild.id
    logger.info(f"👋 Removed from guild {gid} ({guild.name}); cleaning up per-guild state")
    try:
        await kick_ws_manager.disconnect(gid)
    except Exception as e:
        logger.info(f"⚠️ Error disconnecting kick ws for guild {gid}: {e}")

    # Cancel the legacy Pusher chat loop for this guild, if one is running.
    _legacy_tasks = getattr(bot, "_legacy_kick_tasks", None)
    if isinstance(_legacy_tasks, dict):
        _legacy = _legacy_tasks.pop(gid, None)
        if _legacy and not _legacy.done():
            _legacy.cancel()

    for _registry in (
        active_viewers_by_guild,
        recent_chatters_by_guild,
        last_chat_activity_by_guild,
        kick_chatroom_ids,
        clip_buffer_active_by_guild,
        last_stream_live_state_by_guild,
        gifted_sub_trackers,
        shuffle_trackers,
        slot_call_trackers,
        gtb_managers,
        giveaway_managers,
        guild_settings_managers,
    ):
        _registry.pop(gid, None)

    for _attr in ("custom_commands_managers", "slot_panels_by_guild", "gtb_panels_by_guild"):
        _reg = getattr(bot, _attr, None)
        if isinstance(_reg, dict):
            _reg.pop(gid, None)


async def ensure_standalone_chat_runtime(sid: int, server_name: str, kick_channel: str) -> None:
    """Spin up the per-server CHAT runtime for a standalone (guild-less)
    workspace: settings manager, slot-call tracker, GTB manager, and the Kick
    chat connection. Idempotent — safe to call again for an already-running
    workspace.

    Standalone workspaces are dashboard-created `servers` rows with a synthetic
    NEGATIVE discord_server_id (email accounts, no Discord guild). They get
    ONLY the chat-driven free features (slot requests, GTB) + chat replies;
    every Discord-side subsystem (panels, announcements, raffle, watchtime,
    giveaways...) stays off because those init loops iterate bot.guilds.
    """
    set_server(sid, server_name)
    try:
        # Settings manager (registers into guild_settings_managers).
        get_guild_settings(sid)

        # Slot-call tracker → the !call/!sr handler resolves it from this
        # registry. discord_channel_id stays None (no guild): the tracker
        # records + replies in chat and skips the Discord embed gracefully.
        from features.slot_requests.slot_calls import SlotCallTracker

        if not hasattr(bot, "slot_call_trackers_by_guild"):
            bot.slot_call_trackers_by_guild = {}
        if sid not in bot.slot_call_trackers_by_guild:
            bot.slot_call_trackers_by_guild[sid] = SlotCallTracker(
                bot=bot,
                discord_channel_id=None,
                kick_send_callback=send_stream_message,
                engine=engine,
                server_id=sid,
            )
            logger.info(f"✅ [Standalone] Slot call tracker initialized")

        # Guess the Balance manager (pure DB/memory — no Discord usage).
        if not hasattr(bot, "gtb_managers_by_guild"):
            bot.gtb_managers_by_guild = {}
        if sid not in bot.gtb_managers_by_guild:
            bot.gtb_managers_by_guild[sid] = GuessTheBalanceManager(engine, sid)
            logger.info(f"✅ [Standalone] GTB manager initialized")

        # Kick chat connection (reading). Same mechanism real guilds use.
        if not kick_channel or not str(kick_channel).strip():
            return
        if KICK_USE_KICKPYTHON_WS:
            await kick_ws_manager.ensure_connection(sid, server_name)
        else:
            if not hasattr(bot, "_legacy_kick_tasks"):
                bot._legacy_kick_tasks = {}
            existing = bot._legacy_kick_tasks.get(sid)
            if not existing or existing.done():
                bot._legacy_kick_tasks[sid] = asyncio.create_task(kick_chat_loop(kick_channel, sid))
        logger.info(f"✅ [Standalone] Kick chat runtime ready for '{kick_channel}'")
    except Exception as e:
        logger.warning(f"⚠️ [Standalone] chat runtime setup failed for {sid}: {e}")
    finally:
        clear_server()


async def refresh_standalone_chat(sid: int) -> None:
    """React to a dashboard settings save for a standalone workspace (called by
    the Redis `dashboard:bot_settings` reload handler). Connects a newly
    configured Kick channel live (no restart), reconnects on a channel CHANGE,
    and tears the connection down when the channel was cleared.
    """
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return
    if sid >= 0:
        return

    server_name, kick_channel = str(sid), None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT s.server_name, bs.value
                    FROM servers s
                    LEFT JOIN bot_settings bs
                      ON bs.discord_server_id = s.discord_server_id AND bs.key = 'kick_channel'
                    WHERE s.discord_server_id = :sid
                    """
                ),
                {"sid": sid},
            ).fetchone()
        if row:
            server_name = row[0] or server_name
            kick_channel = (row[1] or "").strip() or None
    except Exception as e:
        logger.info(f"[Standalone] settings lookup failed for {sid}: {e}")
        return

    # Refresh the cached per-server settings so trackers read the new values.
    try:
        get_guild_settings(sid).refresh()
    except Exception:
        pass

    if KICK_USE_KICKPYTHON_WS:
        current = kick_ws_manager.connected_channels.get(sid)
        if not kick_channel:
            if sid in kick_ws_manager.connections:
                await kick_ws_manager.disconnect(sid)
                logger.info(f"[Standalone] Kick channel cleared — disconnected {sid}")
            return
        if current and str(current).lower() != str(kick_channel).lower():
            await kick_ws_manager.disconnect(sid)
            logger.info(f"[Standalone] Kick channel changed ({current} → {kick_channel}) — reconnecting")
    else:
        task = getattr(bot, "_legacy_kick_tasks", {}).get(sid)
        if task and not task.done():
            if not kick_channel:
                task.cancel()
                return
            # Legacy loop caches the channel arg — restart it on any reload so a
            # channel change takes effect.
            task.cancel()

    await ensure_standalone_chat_runtime(sid, server_name, kick_channel)


@bot.event
async def on_ready():
    # Track bot uptime for health checks
    if not hasattr(bot, "uptime_start"):
        bot.uptime_start = datetime.now()

    logger.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    logger.debug(f"📺 Monitoring Kick channel: {KICK_CHANNEL}")

    # Guard against running full initialization on Discord gateway reconnects.
    # on_ready fires every time the bot reconnects (not just on first login).
    # Cog registration, command registration, and task creation must only happen once.
    _first_ready = not hasattr(bot, "_bot_initialized")

    # START KICKPYTHON WEBSOCKET (for chat messages)
    # NOTE: Subscription events are now handled via Kick webhooks (channel.subscription.*)
    if KICK_USE_KICKPYTHON_WS:
        logger.debug("🔌 Starting kickpython WebSocket for chat messages...")
        logger.debug("ℹ️  Subscription events handled via Kick webhooks (not Pusher)")
        for guild in bot.guilds:
            set_server(guild.id, guild.name)  # tag this guild's iteration logging
            kick_channel = None
            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        text(
                            """
                        SELECT value FROM bot_settings
                        WHERE key = 'kick_channel' AND discord_server_id = :gid
                        LIMIT 1
                    """
                        ),
                        {"gid": guild.id},
                    ).fetchone()
                    if row and row[0]:
                        kick_channel = row[0]
            except Exception as e:
                logger.info(f"⚠️ Error loading kick_channel: {e}")

            if kick_channel:
                await kick_ws_manager.ensure_connection(guild.id, guild.name)
                logger.debug(f"✅ kickpython WebSocket started: {kick_channel}")
            else:
                logger.info(f"⚠️ No kick_channel configured - skipping WebSocket")
    else:
        # LEGACY: Using Pusher only (handles both chat and subscriptions)
        logger.debug("🔌 Starting Pusher WebSocket (LEGACY MODE - handles chat + subscriptions)...")
        for guild in bot.guilds:
            set_server(guild.id, guild.name)  # tag this guild's iteration logging
            kick_channel = None
            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        text(
                            """
                        SELECT value FROM bot_settings
                        WHERE key = 'kick_channel' AND discord_server_id = :gid
                        LIMIT 1
                    """
                        ),
                        {"gid": guild.id},
                    ).fetchone()
                    if row and row[0]:
                        kick_channel = row[0]
            except Exception as e:
                logger.info(f"⚠️ Error loading kick_channel: {e}")

            if kick_channel:
                # on_ready fires on every gateway reconnect, so guard against spawning a
                # duplicate Pusher loop (each holds its own websocket + reconnect loop).
                # Only (re)spawn if there is no live task for this guild.
                if not hasattr(bot, "_legacy_kick_tasks"):
                    bot._legacy_kick_tasks = {}
                existing = bot._legacy_kick_tasks.get(guild.id)
                if existing and not existing.done():
                    logger.debug(f"↩️ Pusher WebSocket already running for guild {guild.id}; not respawning")
                else:
                    bot._legacy_kick_tasks[guild.id] = asyncio.create_task(kick_chat_loop(kick_channel, guild.id))
                    logger.debug(f"✅ Pusher WebSocket started: {kick_channel}")
            else:
                logger.info(f"⚠️ No kick_channel configured - skipping WebSocket")

    # WebSocket-start loop done — clear context so the global setup below (and any
    # task spawned from here, e.g. the Redis subscriber) doesn't inherit the last guild.
    clear_server()

    # Standalone (guild-less) workspaces: start the chat runtime (slot requests,
    # GTB, Kick websocket) for every dashboard-created standalone server with a
    # configured Kick channel. These never appear in bot.guilds; newly created/
    # configured ones are picked up live by the Redis bot_settings reload hook
    # (refresh_standalone_chat), so this loop only covers restarts.
    try:
        from utils.standalone import standalone_chat_servers

        for _sid, _sname, _skick in standalone_chat_servers(engine):
            await ensure_standalone_chat_runtime(_sid, _sname, _skick)
    except Exception as e:
        logger.warning(f"⚠️ Standalone workspace startup failed: {e}")
    clear_server()

    # Attach helper function to bot so other cogs can access it
    bot.get_active_chatters_count = get_active_chatters_count

    # 🎁 Setup giveaway managers for all guilds
    logger.debug("🎁 Setting up giveaway managers...")
    global giveaway_managers
    giveaway_managers = await setup_giveaway_managers(bot, engine)
    bot.giveaway_managers = giveaway_managers
    logger.debug(f"✅ Giveaway managers initialized for {len(giveaway_managers)} guilds")

    # Start Redis subscriber with the platform-aware send callback (fallback for
    # announce_in_chat). NOT gated on KICK_BOT_USER_TOKEN: send_kick_message queues
    # via the kickpython websocket (same path commands use) and send_stream_message
    # fans out to active platforms, so announcements work regardless of that env var.
    # Guard against duplicate subscriber tasks on Discord gateway reconnects.
    kick_callback = send_stream_message
    if not hasattr(bot, "_redis_subscriber_started"):
        bot._redis_subscriber_started = True
        asyncio.create_task(start_redis_subscriber(bot, kick_callback))
    logger.debug("✅ Redis subscriber started (platform-aware chat announcements)")

    # Auto-migrate database tables
    if engine:
        try:
            with engine.begin() as conn:
                # Create slot_call_blacklist table if missing
                logger.debug("🔄 Checking slot_call_blacklist table...")
                conn.execute(
                    text(
                        """
                    CREATE TABLE IF NOT EXISTS slot_call_blacklist (
                        kick_username TEXT,
                        discord_server_id BIGINT,
                        reason TEXT,
                        blacklisted_by BIGINT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (kick_username, discord_server_id)
                    )
                """
                    )
                )
                logger.debug("✅ slot_call_blacklist table ready")

                # Add provably fair columns to giveaways table
                logger.debug("🔄 Checking provably fair columns in giveaways table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE giveaways
                    ADD COLUMN IF NOT EXISTS server_seed TEXT,
                    ADD COLUMN IF NOT EXISTS client_seed TEXT,
                    ADD COLUMN IF NOT EXISTS nonce TEXT,
                    ADD COLUMN IF NOT EXISTS proof_hash TEXT,
                    ADD COLUMN IF NOT EXISTS random_value NUMERIC(5,2)
                """
                    )
                )
                logger.debug("✅ Provably fair columns added to giveaways table")

                # Add provably fair columns to slot_requests table
                logger.debug("🔄 Checking provably fair columns in slot_requests table...")
                conn.execute(
                    text(
                        """
                    ALTER TABLE slot_requests
                    ADD COLUMN IF NOT EXISTS server_seed TEXT,
                    ADD COLUMN IF NOT EXISTS client_seed TEXT,
                    ADD COLUMN IF NOT EXISTS nonce TEXT,
                    ADD COLUMN IF NOT EXISTS proof_hash TEXT,
                    ADD COLUMN IF NOT EXISTS random_value NUMERIC(5,2)
                """
                    )
                )
                logger.debug("✅ Provably fair columns added to slot_requests table")
        except Exception as e:
            logger.warning(f"⚠️ Database migration check failed: {e}")

    try:
        # Multiserver: Validate bot permissions for ALL guilds
        for guild in bot.guilds:
            set_server(guild.id, guild.name)  # tag role-validation warnings with this guild
            me = guild.me
            if not me.guild_permissions.manage_roles:
                logger.warning(f"⚠️ Bot lacks manage_roles permission!")

            # Validate roles exist. DEBUG only — the update_roles_task background loop
            # re-checks and reports missing roles at WARNING on its first tick, so
            # logging them here too would double every missing-role warning at boot.
            current_roles = load_watchtime_roles()
            existing_roles = {role.name for role in guild.roles}
            for role_config in current_roles:
                if role_config["name"] not in existing_roles:
                    logger.debug(f"⚠️ Role {role_config['name']} does not exist in the server!")

        # Per-guild role-check loop done — clear so the global task-start block below
        # isn't tagged with the last guild.
        clear_server()

        # Start background tasks (guarded: tasks already check is_running())
        if not update_watchtime_task.is_running():
            update_watchtime_task.start()
            logger.debug("✅ Watchtime updater started")

        if not update_roles_task.is_running():
            update_roles_task.start()
            logger.debug("✅ Role updater started")

        if not update_discord_usernames_task.is_running() and engine:
            update_discord_usernames_task.start()
            logger.debug("✅ Discord username backfill started (runs every 30 minutes)")

        if not cleanup_pending_links_task.is_running():
            cleanup_pending_links_task.start()
            logger.debug("✅ Cleanup task started")

        if not proactive_token_refresh_task.is_running() and engine:
            proactive_token_refresh_task.start()
            logger.debug("✅ Proactive token refresh task started (runs every 30 minutes)")

        if not proactive_twitch_token_refresh_task.is_running() and engine:
            proactive_twitch_token_refresh_task.start()
            logger.debug("✅ Twitch token maintenance task started (validate + refresh hourly)")

        if not check_oauth_notifications_task.is_running():
            check_oauth_notifications_task.start()
            logger.debug("✅ OAuth notifications task started")

        # Start clip buffer management task
        if not clip_buffer_management_task.is_running():
            clip_buffer_management_task.start()
            logger.debug("✅ Clip buffer management task started (monitors stream status)")

        # Start leaderboard sync task (Tier 4 leaderboard_generator):
        # seeds shuffle baselines for active periods, freezes/rolls over ended ones.
        if not leaderboard_sync_task.is_running() and engine:
            leaderboard_sync_task.start()
            logger.debug("✅ Leaderboard sync task started (runs every 2 minutes)")

        # Initialize raffle system — skip cog/command registration on gateway reconnects.
        # (on_ready fires on every reconnect; cog/command re-registration would raise errors.)
        if not _first_ready:
            # The command tree is already built from the first pass, so a sync that
            # failed then (HTTP 5xx, rate limit, transient network) is retried here.
            # sync_global_slash_commands is a no-op once it has succeeded.
            if await sync_global_slash_commands(bot):
                logger.info("♻️  Gateway reconnect — background tasks checked, initialization skipped")
            else:
                logger.warning(
                    "♻️  Gateway reconnect — background tasks checked, but global slash "
                    "commands are still unsynced; will retry on the next reconnect"
                )
            return
        bot._bot_initialized = True

        # Initialize raffle system
        try:
            global gifted_sub_tracker, shuffle_tracker, slot_call_tracker

            # Setup raffle database (creates tables if needed)
            setup_raffle_database(engine)

            # Run migrations
            migrate_add_created_at_to_shuffle_wagers(engine)
            migrate_add_platform_to_wager_tables(engine)
            # Must run AFTER migrate_add_platform_to_wager_tables (needs the platform column)
            migrate_platform_scope_raffle_constraints(engine)
            # Stream-link platform scoping (Kick + Twitch per Discord user per server)
            migrate_add_platform_to_links(engine)
            migrate_add_provably_fair_to_draws(engine)
            # Commit-reveal columns (raffle_periods seed/commitment + draw commitment).
            # After add_provably_fair_to_draws so raffle_draws already has its base PF columns.
            migrate_add_commit_reveal_to_periods(engine)
            migrate_make_shuffle_links_kick_name_nullable(engine)
            migrate_add_panel_type_to_link_panels(engine)
            # Enforce one active raffle period per server (safety net behind the
            # transient-DB-error fix). Must run before the per-guild loop below.
            migrate_one_active_period_per_server(engine)

            # Initialize per-guild trackers and managers (without adding cogs yet)
            for guild in bot.guilds:
                # Tag every log line emitted while initializing THIS guild (settings
                # load, period check, gifted-sub/watchtime/wager trackers,
                # auto-leaderboard, link panels, giveaways, …) with the server, so
                # startup logs show "[<server name>]" instead of "[-]". Per-guild
                # background tasks that run later set their own context; this covers
                # the synchronous init pass. The clear_server() after the loop stops
                # the last guild's tag bleeding into later global startup logs.
                set_server(guild.id, guild.name)

                logger.debug(f"[Init] Loading settings for guild {guild.name} ({guild.id})...")
                guild_settings = get_guild_settings(guild.id)

                # Debug: Print loaded settings
                logger.debug(f"[Init] Guild {guild.name} loaded {len(guild_settings._cache)} settings:")
                logger.debug(f"  - kick_channel: '{guild_settings.kick_channel}'")
                logger.debug(f"  - slot_calls_channel_id: {guild_settings.slot_calls_channel_id}")
                logger.debug(f"  - raffle_announcement_channel_id: {guild_settings.raffle_announcement_channel_id}")

                # Raffle period creation is opt-in — the bot NEVER auto-creates a
                # period on startup/activation. A server begins with no active
                # period; the user starts one from the dashboard (Start New
                # Period) or with !rafflestart, and auto_renew (off by default)
                # governs whether it rolls over when it ends. We still look up the
                # current period here purely to log its state (and never create).
                # get_current_period re-raises on DB error, so guard the lookup.
                try:
                    current_period = get_current_period(engine, discord_server_id=guild.id)
                except Exception as e:
                    current_period = None
                    logger.warning(f"⚠️ Could not check raffle period (DB error: {e})")
                else:
                    if current_period:
                        logger.debug(f"✅ Active raffle period found (#{current_period['id']})")
                    else:
                        logger.debug(f"No active raffle period for guild {guild.id} — dormant until one is started")

                # Setup gifted sub tracker for this guild
                gifted_sub_trackers[guild.id] = setup_gifted_sub_handler(
                    engine, server_id=guild.id, bot_settings=guild_settings
                )
                logger.debug(f"✅ Gifted sub tracker initialized")

                # Setup watchtime converter for this guild (runs every 10 minutes).
                # Pass the per-guild settings manager so it reads THIS server's
                # "tickets per hour watched" rate, not the global/default one.
                await setup_watchtime_converter(bot, engine, server_id=guild.id, bot_settings=guild_settings)
                logger.debug(f"✅ Watchtime converter initialized")

                # Setup wager tracker for this guild. The tracker self-selects the
                # active platform (shuffle/howl) from wager_platform_name on every
                # refresh, so a platform switch hot-swaps this same instance.
                shuffle_trackers[guild.id] = await setup_shuffle_tracker(
                    bot, engine, server_id=guild.id, bot_settings=guild_settings, server_name=guild.name
                )
                # Expose per-guild wager trackers so the Redis settings-sync handler
                # can call refresh_settings() for an instant platform hot-swap.
                bot.shuffle_trackers_by_guild = shuffle_trackers
                logger.debug(f"✅ Wager tracker initialized")

                # Setup auto-updating leaderboard for this guild
                leaderboard_channel_id = guild_settings.raffle_leaderboard_channel_id
                auto_leaderboard = await setup_auto_leaderboard(bot, engine, leaderboard_channel_id, server_id=guild.id)
                if not hasattr(bot, "auto_leaderboards"):
                    bot.auto_leaderboards = {}
                bot.auto_leaderboards[guild.id] = auto_leaderboard
                logger.debug(f"📊 Leaderboard channel: {leaderboard_channel_id or 'Not configured'}")

                # Setup raffle scheduler for this guild
                raffle_auto_draw = guild_settings.raffle_auto_draw
                raffle_channel_id = guild_settings.raffle_announcement_channel_id
                await setup_raffle_scheduler(
                    bot=bot,
                    engine=engine,
                    auto_draw=raffle_auto_draw,
                    announcement_channel_id=raffle_channel_id,
                    discord_server_id=guild.id,
                )
                logger.debug(f"✅ Raffle system initialized (auto-draw: {raffle_auto_draw})")

                # Create slot call tracker for this guild (but don't register cog yet)
                from features.slot_requests.slot_calls import SlotCallTracker

                slot_calls_channel_id = guild_settings.slot_calls_channel_id
                slot_call_trackers[guild.id] = SlotCallTracker(
                    bot=bot,
                    discord_channel_id=slot_calls_channel_id,
                    # Platform-aware: fans out to the server's active platform(s)
                    # (Kick and/or Twitch), not Kick-only.
                    kick_send_callback=send_stream_message,
                    engine=engine,
                    server_id=guild.id,
                )
                if not hasattr(bot, "slot_call_trackers_by_guild"):
                    bot.slot_call_trackers_by_guild = {}
                bot.slot_call_trackers_by_guild[guild.id] = slot_call_trackers[guild.id]
                logger.debug(f"✅ Slot call tracker initialized (channel: {slot_calls_channel_id or 'Not configured'})")

                # Setup Guess the Balance manager for this guild
                gtb_managers[guild.id] = GuessTheBalanceManager(engine, guild.id)
                if not hasattr(bot, "gtb_managers_by_guild"):
                    bot.gtb_managers_by_guild = {}
                bot.gtb_managers_by_guild[guild.id] = gtb_managers[guild.id]
                logger.debug(f"✅ GTB system initialized")

                # Setup Custom Commands manager for this guild
                custom_commands_manager = CustomCommandsManager(
                    bot=bot,
                    send_message_callback=lambda msg, gid=guild.id: asyncio.create_task(
                        send_stream_message(msg, guild_id=gid)
                    ),
                    discord_server_id=guild.id,
                )
                await custom_commands_manager.start()  # Load commands from database
                if not hasattr(bot, "custom_commands_managers"):
                    bot.custom_commands_managers = {}
                bot.custom_commands_managers[guild.id] = custom_commands_manager
                logger.debug(f"✅ Custom commands system initialized")

            # Per-guild init loop done — clear context so the global setup below
            # (cogs, views, summaries) isn't mis-tagged with the last guild.
            clear_server()

            # Sync Shuffle code user role (run once, not per-guild)
            await sync_shuffle_role_on_startup(bot, engine)

            # Add cogs globally (only once, not per-guild)
            # These cogs will use the per-guild trackers from bot.slot_call_trackers_by_guild
            if slot_call_trackers:
                from features.slot_requests.slot_calls import SlotCallCommands

                first_tracker = list(slot_call_trackers.values())[0]  # Use first guild's tracker for cog
                await bot.add_cog(SlotCallCommands(bot, first_tracker))
                logger.debug(f"✅ Slot call commands cog registered")

            # Import view classes for persistent registration
            from features.games.gtb_panel import GTBPanel, GTBPanelView
            from features.slot_requests.slot_request_panel import SlotPanelView, SlotRequestPanel

            # Register persistent views ONCE globally (they look up panels by guild_id)
            slot_persistent_view = SlotPanelView(bot)
            bot.add_view(slot_persistent_view)
            logger.debug(f"✅ Slot panel persistent view registered (handles all guilds)")

            gtb_persistent_view = GTBPanelView(bot)
            bot.add_view(gtb_persistent_view)
            logger.debug(f"✅ GTB panel persistent view registered (handles all guilds)")

            # Point shop storefront: register a stateless template so the
            # purchase select and balance button on already-posted shop
            # messages keep working after a restart (their callbacks read all
            # state from the interaction + DB, not from this instance).
            bot.add_view(ShopLayoutView.template())
            logger.debug(f"✅ Point shop persistent view registered (handles all guilds)")

            # Create slot panels per-guild (but only add cogs once)
            first_guild = True
            for guild in bot.guilds:
                # Create panel instances
                slot_panel = SlotRequestPanel(
                    bot,
                    engine,
                    slot_call_trackers[guild.id],
                    kick_send_callback=send_stream_message,
                )
                slot_call_trackers[guild.id].panel = slot_panel

                if not hasattr(bot, "slot_panels_by_guild"):
                    bot.slot_panels_by_guild = {}
                bot.slot_panels_by_guild[guild.id] = slot_panel
                logger.debug(f"✅ Slot request panel initialized")

                # Setup GTB panel for this guild (just the instance, no commands)
                gtb_panel = GTBPanel(
                    bot,
                    engine,
                    gtb_managers[guild.id],
                    kick_send_callback=send_stream_message,
                    guild_id=guild.id,
                )
                if not hasattr(bot, "gtb_panels_by_guild"):
                    bot.gtb_panels_by_guild = {}
                bot.gtb_panels_by_guild[guild.id] = gtb_panel
                logger.debug(f"✅ GTB panel initialized")

                # Add cogs only once on first iteration
                if first_guild:
                    from features.slot_requests.slot_request_panel import SlotRequestPanelCommands

                    await bot.add_cog(SlotRequestPanelCommands(bot, slot_panel))
                    logger.debug(f"✅ Slot request panel commands cog registered")

                    # Add GTB panel command (only once)
                    @bot.hybrid_command(name="creategtbpanel")
                    @app_commands.default_permissions(administrator=True)
                    @commands.has_permissions(administrator=True)
                    async def create_gtb_panel_cmd(ctx):
                        """[ADMIN] Create the GTB panel in this channel"""
                        guild_panel = bot.gtb_panels_by_guild.get(ctx.guild.id)
                        if not guild_panel:
                            await ctx.send("❌ GTB panel not initialized for this server")
                            return
                        success = await guild_panel.create_panel(ctx.channel)
                        if success:
                            await ctx.send("✅ GTB panel created!")
                        else:
                            await ctx.send("❌ Failed to create GTB panel.")

                    logger.debug(f"✅ GTB panel commands registered")
                    first_guild = False

            # Panel loop done — clear context; the command setups below are global.
            clear_server()

            logger.debug("📝 About to setup raffle commands...")
            # Setup raffle commands (global cog)
            await setup_raffle_commands(bot, engine)
            logger.debug("✅ Raffle commands setup complete")

            # Setup gambling commands (!bj, !roll, !double)
            await setup_gambling(bot, engine)
            logger.debug("✅ Gambling commands setup complete")

            # Set legacy global references (use first guild for backward compatibility)
            if bot.guilds:
                first_guild_id = bot.guilds[0].id
                gifted_sub_tracker = gifted_sub_trackers.get(first_guild_id)
                shuffle_tracker = shuffle_trackers.get(first_guild_id)
                slot_call_tracker = slot_call_trackers.get(first_guild_id)
                gtb_manager = gtb_managers.get(first_guild_id)
                bot.slot_call_tracker = slot_call_tracker
                bot.gtb_manager = gtb_manager

            logger.debug("✅ All guilds initialized with multiserver features")

            logger.debug("📝 About to setup link panel...")
            # Combined link panel: one panel with Kick and/or Twitch buttons based
            # on the server's stream_platforms. Replaces the separate Kick/Twitch panels.
            link_panels = await setup_combined_link_panel_system(
                bot, engine, generate_signed_oauth_url, generate_signed_twitch_oauth_url
            )
            bot.link_panels = link_panels
            # Back-compat: redis post_panel handler also reads twitch_link_panels; point
            # it at the same combined registry so 'twitch_link' posts still work.
            bot.twitch_link_panels = link_panels

            if link_panels:
                bot.link_panel = next(iter(link_panels.values()))

            logger.debug(f"✅ Combined link panel system initialized ({len(link_panels)} guilds)")

            # Setup Shuffle verify panel with per-guild instances
            bot.shuffle_panels = await setup_shuffle_panel_system(bot, engine, get_guild_settings)
            logger.debug(f"✅ Shuffle verify panel system initialized ({len(bot.shuffle_panels)} guilds)")

            # Setup Howl verify panel with per-guild instances
            bot.howl_panels = await setup_howl_panel_system(bot, engine, get_guild_settings)
            logger.debug(f"✅ Howl verify panel system initialized ({len(bot.howl_panels)} guilds)")

            # Setup global super-admin panels for the official guild. These are
            # posted/moved from the dashboard super-admin console via the existing
            # redis post_panel flow (registry attrs read by _post_panel).
            try:
                bot.patchnotes_panels = await setup_patchnotes_panel_system(bot, engine)
                bot.extension_patchnotes_panels = await setup_extension_patchnotes_panel_system(bot, engine)
                bot.rules_panels = await setup_rules_panel_system(bot, engine)
                bot.features_panels = await setup_features_panel_system(bot, engine)
                bot.sub_role_panels = await setup_sub_role_panel_system(bot, engine)
                logger.debug("✅ Super-admin global panels initialized")
            except Exception as e:
                logger.error(f"⚠️ Failed to initialize super-admin global panels: {e}")

            # Setup timed messages system with per-guild instances.
            # Platform-aware: send_stream_message fans out to the server's active
            # platform(s); each platform sender no-ops if that platform isn't set up.
            timed_messages_managers = await setup_timed_messages(bot, engine, kick_send_callback=send_stream_message)
            # Store as bot attribute for Redis subscriber and commands
            bot.timed_messages_managers = timed_messages_managers

            # Legacy single manager reference (use first guild for backwards compatibility)
            if timed_messages_managers:
                bot.timed_messages_manager = next(iter(timed_messages_managers.values()))

            total_messages = sum(len(mgr.messages) for mgr in timed_messages_managers.values())
            logger.debug(
                f"✅ Timed messages system initialized ({len(timed_messages_managers)} guilds, {total_messages} total messages)"
            )

            # Setup custom commands manager (platform-aware send; works for Kick
            # and/or Twitch servers).
            custom_commands_manager = CustomCommandsManager(bot, send_message_callback=send_stream_message)
            await custom_commands_manager.start()
            bot.custom_commands_manager = custom_commands_manager
            logger.debug(f"✅ Custom commands system initialized")

            # Clip service is now on Dashboard - no local buffer needed
            # Bot calls Dashboard API at /api/clips/create when !clip is used
            # Dashboard URL is configured per-guild in settings
            logger.debug(f"✅ Clip service configured via Dashboard API")
            logger.debug(f"   Configure Dashboard URL in Profile Settings for each guild")

            # ── Grouped startup summary ───────────────────────────────────────
            # Per-subsystem/per-guild detail above is DEBUG; emit one INFO line per
            # category so a normal boot shows ~4 lines instead of dozens.
            # These are GLOBAL lines → clear any lingering per-guild context first.
            clear_server()
            _gc = len(bot.guilds)
            _adraw = sum(1 for g in bot.guilds if get_guild_settings(g.id).raffle_auto_draw)
            logger.info(f"✅ Trackers ready ({_gc} guilds: gifted-sub, watchtime, wager, leaderboard)")
            logger.info(f"✅ Raffle system ready ({_gc} guilds, auto-draw on: {_adraw})")
            logger.info(f"✅ Verification panels ready ({_gc} guilds: link, shuffle, howl)")
            logger.info(f"✅ Panels & commands ready ({_gc} guilds: slot, gtb, gambling, custom, timed)")

        except Exception as e:
            logger.warning(f"⚠️ Failed to initialize raffle system: {e}")
            import traceback

            traceback.print_exc()

    except Exception as e:
        logger.error(f"⚠️ Error during startup: {e}")

    # Start Kick chat listeners for all guilds with configured Kick channels
    # DUAL MODE: Webhooks for chat + Pusher for subscription events
    # Webhooks handle: chat messages, commands, watchtime
    # Pusher handles: gifted subs, subscriptions (not available as webhooks yet)
    use_dual_mode = os.getenv("USE_DUAL_MODE", "true").lower() == "true"

    if use_dual_mode:
        if KICK_USE_KICKPYTHON_WS:
            logger.debug(f"✅ Running in DUAL MODE: Webhooks + kickpython WebSocket")
            logger.debug(f"   • Webhooks: chat.message.sent (registered via OAuth)")
            logger.debug(f"   • kickpython: Direct WebSocket for chat messages")
            # kickpython WebSockets are started in on_ready
        else:
            logger.debug(f"✅ Running in DUAL MODE: Webhooks + Pusher")
            logger.debug(f"   • Webhooks: chat.message.sent (registered via OAuth)")
            logger.debug(f"   • Pusher: subscription events (no webhook support yet)")

            for guild in bot.guilds:
                set_server(guild.id, guild.name)  # tag this guild's iteration logging
                try:
                    guild_settings = get_guild_settings(guild.id)
                    kick_channel = guild_settings.kick_channel

                    if kick_channel and kick_channel.strip():
                        bot.loop.create_task(kick_chat_loop(kick_channel, guild.id))
                        logger.debug(f"✅ Pusher listener started for subscription events → {kick_channel}")
                    else:
                        logger.warning(f"⚠️ No Kick channel configured")
                        logger.info(f"   Configure it in Dashboard → Profile Settings")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to start Pusher listener: {e}")
            clear_server()  # per-guild loop done → global section below
    else:
        logger.info(f"✅ Using webhooks only (Pusher disabled)")
        logger.warning(f"   ⚠️ WARNING: Gifted sub raffle tracking will not work!")
        logger.info(f"   Set USE_DUAL_MODE=true to enable Pusher for subscription events")

    # ========== MERGED CODE FROM SECOND ON_READY ==========
    # Additional slot tracker initialization with backwards compatibility
    clear_server()  # ensure the global "Bot ready" marker below shows [-]
    logger.debug("⚙️  Ensuring slot tracker backwards compatibility...")
    if not hasattr(bot, "slot_trackers"):
        bot.slot_trackers = {}

    # Set default slot_call_tracker for backwards compatibility (use first guild)
    if slot_call_trackers and not hasattr(bot, "slot_call_tracker"):
        first_guild_id = list(slot_call_trackers.keys())[0] if slot_call_trackers else None
        if first_guild_id:
            bot.slot_call_tracker = slot_call_trackers[first_guild_id]
            bot.slot_trackers = slot_call_trackers  # Also expose as slot_trackers
            logger.debug(f"✅ Backwards compatibility attributes set")

    # Cogs and panel commands are registered lazily during the first on_ready
    # pass. Sync only after that work so Discord receives the complete hybrid
    # command tree. Failed syncs remain retryable on the next gateway reconnect.
    if await sync_global_slash_commands(bot):
        logger.info("🎉 Bot ready — slash commands enabled")
    else:
        logger.warning(
            "🎉 Bot ready — but global slash commands could NOT be synced; prefix "
            "commands still work and the sync retries on the next gateway reconnect"
        )
    # ========== END MERGED CODE ==========


async def handle_timer_panel_reaction(payload):
    """Handle reactions on timer panel messages."""
    from features.messaging.timed_messages import TimedMessagesManager

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member:
        return

    # Check admin permissions
    if not member.guild_permissions.administrator:
        channel = bot.get_channel(payload.channel_id)
        if channel:
            await channel.send(f"❌ {member.mention} Only administrators can use the timer panel!", delete_after=5)
        # Remove reaction
        try:
            message = await channel.fetch_message(payload.message_id)
            await message.remove_reaction(payload.emoji, member)
        except:
            pass
        return

    reaction_emoji = str(payload.emoji)
    channel = bot.get_channel(payload.channel_id)
    if not channel:
        return

    # Refresh the panel
    message = await channel.fetch_message(payload.message_id)

    # Get timer manager
    manager = TimedMessagesManager(engine)
    messages = manager.list_messages()

    # Handle different reactions
    if reaction_emoji == "":  # Refresh panel
        embed = discord.Embed(
            title="⏰ Timed Messages Control Panel",
            description="React to this message to manage timers:\n\n"
            " - Refresh panel\n"
            "📋 - Show list of timers\n"
            "❌ - Disable timer (will ask for ID)\n"
            "✅ - Enable timer (will ask for ID)",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )

        if not messages:
            embed.add_field(
                name="📭 No Timers", value="Use `!addtimer <minutes> <message>` to create one!", inline=False
            )
        else:
            enabled_count = sum(1 for m in messages if m.enabled)
            disabled_count = len(messages) - enabled_count

            summary = f"**Total:** {len(messages)} timer(s)\n"
            summary += f"✅ **Enabled:** {enabled_count}\n"
            summary += f"❌ **Disabled:** {disabled_count}"

            embed.add_field(name="📊 Summary", value=summary, inline=False)

            for i, msg in enumerate(messages[:10], 1):
                status_emoji = "✅" if msg.enabled else "❌"
                last_sent = msg.last_sent.strftime("%H:%M") if msg.last_sent else "Never"

                if msg.enabled and msg.last_sent:
                    next_send = msg.last_sent + timedelta(minutes=msg.interval_minutes)
                    time_until = next_send - datetime.utcnow()
                    if time_until.total_seconds() > 0:
                        minutes_left = int(time_until.total_seconds() / 60)
                        next_info = f"in {minutes_left}m"
                    else:
                        next_info = "due now"
                else:
                    next_info = "waiting"

                field_value = (
                    f"{status_emoji} **ID:** {msg.message_id} | **Every:** {msg.interval_minutes}m | **Next:** {next_info}\n"
                    f"💬 {msg.message[:60]}{'...' if len(msg.message) > 60 else ''}"
                )

                embed.add_field(name=f"Timer #{i}", value=field_value, inline=False)

            if len(messages) > 10:
                embed.add_field(
                    name="ℹ️ More Timers",
                    value=f"Showing 10 of {len(messages)}. Use `!listtimers` to see all.",
                    inline=False,
                )

        embed.add_field(
            name="📝 Commands",
            value=(
                "`!addtimer <min> <msg>` • `!removetimer <id>`\n"
                "`!toggletimer <id> on/off` • `!updatetimer <id> <min>`"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Checks every 1 minute • React to manage")

        await message.edit(embed=embed)

    elif reaction_emoji == "📋":  # Show list of timers
        embed = discord.Embed(title="📋 All Timers", color=discord.Color.blue(), timestamp=datetime.utcnow())

        if not messages:
            embed.description = "No timers configured"
        else:
            for msg in messages[:20]:  # Show up to 20
                status_emoji = "✅" if msg.enabled else "❌"
                last_sent = msg.last_sent.strftime("%H:%M") if msg.last_sent else "Never"

                if msg.enabled and msg.last_sent:
                    next_send = msg.last_sent + timedelta(minutes=msg.interval_minutes)
                    time_until = next_send - datetime.utcnow()
                    if time_until.total_seconds() > 0:
                        minutes_left = int(time_until.total_seconds() / 60)
                        next_info = f"in {minutes_left}m"
                    else:
                        next_info = "due now"
                else:
                    next_info = "waiting"

                embed.add_field(
                    name=f"{status_emoji} Timer ID: {msg.message_id}",
                    value=f"**Every:** {msg.interval_minutes}m | **Next:** {next_info}\n{msg.message[:80]}{'...' if len(msg.message) > 80 else ''}",
                    inline=False,
                )

            if len(messages) > 20:
                embed.set_footer(
                    text=f"Showing 20 of {len(messages)} • Use !listtimers to see all • React 🔄 to go back"
                )
            else:
                embed.set_footer(text=f"{len(messages)} total • React  to go back")

        await message.edit(embed=embed)

    elif reaction_emoji == "❌":  # Disable timer (ask for ID)
        embed = discord.Embed(
            title="❌ Disable Timer",
            description=f"{member.mention}, please reply with the **Timer ID** you want to disable.\n\nYou have 30 seconds to respond.",
            color=discord.Color.red(),
        )

        if messages:
            enabled = [m for m in messages if m.enabled]
            if enabled:
                timer_list = "\n".join(
                    [
                        f"• **ID {m.message_id}**: {m.message[:50]}{'...' if len(m.message) > 50 else ''}"
                        for m in enabled[:10]
                    ]
                )
                embed.add_field(name="Enabled Timers:", value=timer_list, inline=False)
            else:
                embed.description = "No enabled timers to disable!"

        embed.set_footer(text="Type 'cancel' to cancel")
        await message.edit(embed=embed)

        # Wait for user response
        def check(m):
            return m.author.id == member.id and m.channel.id == channel.id

        try:
            response = await bot.wait_for("message", timeout=30.0, check=check)

            if response.content.lower() == "cancel":
                await response.delete()
                await channel.send(f"❌ {member.mention} Cancelled.", delete_after=3)
                # Refresh panel
                payload.emoji = "🔄"
                await handle_timer_panel_reaction(payload)
                return

            try:
                timer_id = int(response.content)
                await response.delete()

                # Find and disable the timer
                timer = next((m for m in messages if m.message_id == timer_id), None)
                if timer:
                    if not timer.enabled:
                        await channel.send(f"ℹ️ {member.mention} Timer {timer_id} is already disabled.", delete_after=5)
                    else:
                        manager.toggle_message(timer_id, False)
                        await channel.send(
                            f"✅ {member.mention} Timer {timer_id} has been **disabled**!", delete_after=5
                        )
                else:
                    await channel.send(f"❌ {member.mention} Timer ID {timer_id} not found.", delete_after=5)

            except ValueError:
                await response.delete()
                await channel.send(f"❌ {member.mention} Invalid ID. Please use a number.", delete_after=5)

            # Refresh panel
            payload.emoji = "♻️"
            await handle_timer_panel_reaction(payload)
            return

        except asyncio.TimeoutError:
            await channel.send(f"⏰ {member.mention} Timed out. Please try again.", delete_after=5)
            # Refresh panel
            payload.emoji = "♻️"
            await handle_timer_panel_reaction(payload)
            return

    elif reaction_emoji == "✅":  # Enable timer (ask for ID)
        embed = discord.Embed(
            title="✅ Enable Timer",
            description=f"{member.mention}, please reply with the **Timer ID** you want to enable.\n\nYou have 30 seconds to respond.",
            color=discord.Color.green(),
        )

        if messages:
            disabled = [m for m in messages if not m.enabled]
            if disabled:
                timer_list = "\n".join(
                    [
                        f"• **ID {m.message_id}**: {m.message[:50]}{'...' if len(m.message) > 50 else ''}"
                        for m in disabled[:10]
                    ]
                )
                embed.add_field(name="Disabled Timers:", value=timer_list, inline=False)
            else:
                embed.description = "No disabled timers to enable!"

        embed.set_footer(text="Type 'cancel' to cancel")
        await message.edit(embed=embed)

        # Wait for user response
        def check(m):
            return m.author.id == member.id and m.channel.id == channel.id

        try:
            response = await bot.wait_for("message", timeout=30.0, check=check)

            if response.content.lower() == "cancel":
                await response.delete()
                await channel.send(f"❌ {member.mention} Cancelled.", delete_after=3)
                # Refresh panel
                payload.emoji = "🔄"
                await handle_timer_panel_reaction(payload)
                return

            try:
                timer_id = int(response.content)
                await response.delete()

                # Find and enable the timer
                timer = next((m for m in messages if m.message_id == timer_id), None)
                if timer:
                    if timer.enabled:
                        await channel.send(f"ℹ️ {member.mention} Timer {timer_id} is already enabled.", delete_after=5)
                    else:
                        manager.toggle_message(timer_id, True)
                        await channel.send(
                            f"✅ {member.mention} Timer {timer_id} has been **enabled**!", delete_after=5
                        )
                else:
                    await channel.send(f"❌ {member.mention} Timer ID {timer_id} not found.", delete_after=5)

            except ValueError:
                await response.delete()
                await channel.send(f"❌ {member.mention} Invalid ID. Please use a number.", delete_after=5)

            # Refresh panel
            payload.emoji = "♻️"
            await handle_timer_panel_reaction(payload)
            return

        except asyncio.TimeoutError:
            await channel.send(f"⏰ {member.mention} Timed out. Please try again.", delete_after=5)
            # Refresh panel
            payload.emoji = "🔄"
            await handle_timer_panel_reaction(payload)
            return

    # Remove the reaction
    try:
        await message.remove_reaction(payload.emoji, member)
    except:
        pass


@bot.event
async def on_raw_reaction_add(payload):
    """Handle reactions to link panel and timer panel messages."""

    # Ignore bot's own reactions
    if payload.user_id == bot.user.id:
        return

    # Tag logging for this event with the guild (runs in its own Task, auto-scoped).
    if payload.guild_id:
        _g = bot.get_guild(payload.guild_id)
        set_server(payload.guild_id, _g.name if _g else None)

    # Check if this reaction is on a timer panel message
    with engine.connect() as conn:
        timer_panel = conn.execute(
            text(
                """
            SELECT id FROM timer_panels
            WHERE guild_id = :g AND channel_id = :c AND message_id = :m
        """
            ),
            {"g": payload.guild_id, "c": payload.channel_id, "m": payload.message_id},
        ).fetchone()

        if timer_panel:
            # Handle timer panel reactions
            await handle_timer_panel_reaction(payload)
            return

    # Check if this reaction is on a link panel message
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
            SELECT emoji FROM link_panels
            WHERE guild_id = :g AND channel_id = :c AND message_id = :m
        """
            ),
            {"g": payload.guild_id, "c": payload.channel_id, "m": payload.message_id},
        ).fetchone()

        if not result:
            return  # Not a link panel or timer panel message

        panel_emoji = result[0]

        # Check if the reaction emoji matches
        reaction_emoji = str(payload.emoji)
        if reaction_emoji != panel_emoji:
            return  # Wrong emoji

    # Get the user
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member:
        return

    discord_id = member.id
    guild_id = payload.guild_id

    # Check if already linked
    with engine.connect() as conn:
        existing = conn.execute(
            text("SELECT kick_name FROM links WHERE discord_id = :d AND discord_server_id = :g"),
            {"d": discord_id, "g": guild_id},
        ).fetchone()

        if existing:
            # Send message in channel instead of DM
            channel = bot.get_channel(payload.channel_id)
            if channel:
                await channel.send(
                    f"✅ {member.mention} You are already linked to **{existing[0]}**!",
                    delete_after=8,  # Auto-delete after 8 seconds
                )

            # Remove the reaction
            try:
                if channel:
                    message = await channel.fetch_message(payload.message_id)
                    await message.remove_reaction(payload.emoji, member)
                    logger.info(f"✅ Removed reaction from {member.name} (already linked)")
            except discord.Forbidden:
                logger.warning(f"⚠️ Missing permissions to remove reaction for {member.name}")
            except discord.NotFound:
                logger.warning(f"⚠️ Message or reaction not found for {member.name}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to remove reaction for {member.name}: {e}")

            return

    # Generate cryptographically signed OAuth URL
    oauth_url = generate_signed_oauth_url(discord_id, guild_id)

    embed = discord.Embed(
        title="🔗 Link with Kick OAuth",
        description="Click the button below to securely link your Kick account.",
        color=0x53FC18,
    )
    embed.add_field(
        name="📝 Instructions",
        value="1. Click the link below\n2. Log in to Kick (if needed)\n3. Authorize the connection\n4. You're done!",
        inline=False,
    )
    embed.set_footer(text="Link expires in 10 minutes")

    # Create a view with a button
    view = discord.ui.View()
    button = discord.ui.Button(label="Link with Kick", style=discord.ButtonStyle.link, url=oauth_url, emoji="🎮")
    view.add_item(button)

    # Try to DM the user
    try:
        dm_message = await member.send(embed=embed, view=view)

        # Send confirmation message in channel (only visible to user, auto-deletes)
        channel = bot.get_channel(payload.channel_id)
        if channel:
            confirmation = await channel.send(
                f"✅ {member.mention} Check your DMs for the OAuth link!", delete_after=5  # Auto-delete after 5 seconds
            )

        # Remove the reaction immediately after sending DM
        try:
            if channel:
                message = await channel.fetch_message(payload.message_id)
                await message.remove_reaction(payload.emoji, member)
                logger.info(f"✅ Removed reaction from {member.name} on link panel")
            else:
                logger.warning(f"⚠️ Could not find channel {payload.channel_id}")
        except discord.Forbidden:
            logger.warning(f"⚠️ Missing permissions to remove reaction for {member.name}")
        except discord.NotFound:
            logger.warning(f"⚠️ Message or reaction not found for {member.name}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to remove reaction for {member.name}: {e}")

        # Store the DM message info for later deletion
        with engine.begin() as conn:
            # Delete any existing pending OAuth for this user in this guild
            conn.execute(
                text(
                    "DELETE FROM oauth_notifications WHERE discord_id = :d AND discord_server_id = :g AND processed = FALSE"
                ),
                {"d": discord_id, "g": guild_id},
            )

            # Store DM message info (will be updated with kick_username when OAuth completes)
            conn.execute(
                text(
                    """
                INSERT INTO oauth_notifications (discord_id, kick_username, channel_id, message_id, processed, discord_server_id)
                VALUES (:d, '', :c, :m, FALSE, :g)
            """
                ),
                {"d": discord_id, "c": dm_message.channel.id, "m": dm_message.id, "g": guild_id},
            )

    except discord.Forbidden:
        # User has DMs disabled, send in channel instead
        try:
            channel = bot.get_channel(payload.channel_id)
            channel_message = await channel.send(
                f"{member.mention} Check your DMs for the OAuth link! (If you don't see it, make sure DMs are enabled)",
                embed=embed,
                view=view,
                delete_after=60,  # Auto-delete after 1 minute
            )

            # Remove the reaction immediately after sending message
            try:
                message = await channel.fetch_message(payload.message_id)
                await message.remove_reaction(payload.emoji, member)
                logger.info(f"✅ Removed reaction from {member.name} (DMs disabled)")
            except discord.Forbidden:
                logger.warning(f"⚠️ Missing permissions to remove reaction for {member.name}")
            except discord.NotFound:
                logger.warning(f"⚠️ Message or reaction not found for {member.name}")
            except Exception as e:
                logger.warning(f"⚠️ Failed to remove reaction for {member.name}: {e}")

            # Store the channel message info
            with engine.begin() as conn:
                # Delete any existing pending OAuth for this user in this guild
                conn.execute(
                    text(
                        "DELETE FROM oauth_notifications WHERE discord_id = :d AND discord_server_id = :g AND processed = FALSE"
                    ),
                    {"d": discord_id, "g": guild_id},
                )

                # Store channel message info
                conn.execute(
                    text(
                        """
                    INSERT INTO oauth_notifications (discord_id, kick_username, channel_id, message_id, processed, discord_server_id)
                    VALUES (:d, '', :c, :m, FALSE, :g)
                """
                    ),
                    {"d": discord_id, "c": channel_message.channel.id, "m": channel_message.id, "g": guild_id},
                )

        except Exception as e:
            logger.info(f"Failed to send OAuth link to {member}: {e}")


@bot.event
async def on_command_error(ctx, error):
    """Handle command errors gracefully."""
    # Tag error logging with the guild. before_invoke may not have run (e.g. the
    # command was never found), so set it here too. Runs in its own Task.
    if ctx.guild:
        set_server(ctx.guild.id, ctx.guild.name)
    try:
        # If the command has a local error handler, let it handle the error fully.
        # Without this guard, both the local handler AND this global handler fire
        # for the same error, causing duplicate messages.
        # NOTE: hasattr() is NOT sufficient here — discord.py sets on_error=None on every
        # Command by default, so hasattr() always returns True. Must check `is not None`.
        if ctx.command and ctx.command.on_error is not None:
            return

        # Silently ignore commands suppressed by the deduplication hook
        if isinstance(error, commands.CheckFailure) and str(error) == "duplicate_suppressed":
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Missing argument: `{error.param.name}`")
        elif isinstance(error, commands.CommandNotFound):
            pass  # Ignore unknown commands
        elif isinstance(error, commands.CommandOnCooldown):
            # 🔒 ADMIN BYPASS: Skip cooldown for administrators
            if ctx.guild and ctx.author.guild_permissions.administrator:
                await ctx.reinvoke()
                return

            # Format cooldown message
            seconds = int(error.retry_after)
            if seconds < 60:
                await ctx.send(f"⏳ Please wait **{seconds}** seconds before using this command again.")
            else:
                minutes = seconds // 60
                remaining_seconds = seconds % 60
                if remaining_seconds > 0:
                    await ctx.send(
                        f"⏳ Please wait **{minutes}m {remaining_seconds}s** before using this command again."
                    )
                else:
                    await ctx.send(f"⏳ Please wait **{minutes} minutes** before using this command again.")
        elif isinstance(error, commands.CheckFailure):
            if "in_guild" in str(error):
                await ctx.send("❌ This command can only be used in the configured server.")
            else:
                await ctx.send("❌ You don't have permission to use this command.")
        elif isinstance(error, discord.HTTPException):
            if error.code == 429:  # Rate limit error
                retry_after = error.retry_after
                if retry_after > 1:  # If retry_after is significant
                    await ctx.send(f"⚠️ Rate limited. Please try again in {int(retry_after)} seconds.")
                else:
                    await ctx.send("⚠️ Too many requests. Please try again in a moment.")
            else:
                logger.error(f"[HTTP Error] {error}")
                await ctx.send("❌ A network error occurred. Please try again.")
        else:
            logger.error(f"[Error] {error}")
            await ctx.send("❌ An error occurred. Please try again.")
    except discord.Forbidden:
        # Bot doesn't have permission to send messages in this channel
        logger.info(f"[Permission Error] Cannot send error message in channel {ctx.channel.id}: {error}")
    except Exception as e:
        # Catch any other errors to prevent crash
        logger.info(f"[Critical Error in error handler] {e}")


# -------------------------
# Admin Commands: Links Table Management
# -------------------------


@bot.group(name="fixlinks", invoke_without_command=True)
@commands.has_permissions(administrator=True)
async def fixlinks(ctx):
    """[ADMIN] Fix and manage links table. Use subcommands for specific actions."""
    await ctx.send(
        "**Links Table Management**\n"
        "Use `!fixlinks <subcommand>` with one of:\n"
        "• `check` - Run diagnostics on links table\n"
        "• `backfill` - Fix missing discord_server_id values\n"
        "• `duplicates` - Show duplicate kick names across servers\n"
        "• `resolve <kick_name> <keep_server_id>` - Resolve duplicate by keeping one server"
    )


@fixlinks.command(name="check")
@commands.has_permissions(administrator=True)
async def fixlinks_check(ctx):
    """[ADMIN] Run diagnostics on links table."""
    try:
        with engine.connect() as conn:
            # Check for missing discord_server_id
            result = conn.execute(
                text(
                    """
                SELECT COUNT(*) as cnt
                FROM links
                WHERE discord_server_id IS NULL OR discord_server_id = 0
            """
                )
            ).fetchone()
            missing_count = result[0] if result else 0

            # Get distribution by server
            dist_result = conn.execute(
                text(
                    """
                SELECT COALESCE(discord_server_id, 0) as server_id, COUNT(*) as cnt
                FROM links
                GROUP BY COALESCE(discord_server_id, 0)
                ORDER BY cnt DESC
                LIMIT 10
            """
                )
            ).fetchall()

            # Check for duplicates
            dup_result = conn.execute(
                text(
                    """
                SELECT kick_name, COUNT(DISTINCT discord_server_id) as server_count
                FROM links
                WHERE discord_server_id IS NOT NULL AND discord_server_id != 0
                GROUP BY kick_name
                HAVING COUNT(DISTINCT discord_server_id) > 1
                ORDER BY server_count DESC
                LIMIT 10
            """
                )
            ).fetchall()

            embed = discord.Embed(title="🔍 Links Table Diagnostics", color=0x3498DB)

            if missing_count > 0:
                embed.add_field(
                    name="⚠️ Missing Server IDs",
                    value=f"{missing_count} rows need backfill\nRun `!fixlinks backfill` to fix",
                    inline=False,
                )
            else:
                embed.add_field(name="✅ Server IDs", value="All rows have valid discord_server_id", inline=False)

            # Server distribution
            dist_text = "\n".join(
                [f"{'MISSING/0' if r[0] in (0, None) else str(r[0])}: {r[1]} links" for r in dist_result[:5]]
            )
            embed.add_field(name="📊 Distribution", value=dist_text or "No data", inline=False)

            # Duplicates
            if dup_result:
                dup_text = "\n".join([f"• `{r[0]}` on {r[1]} servers" for r in dup_result[:5]])
                embed.add_field(
                    name="⚠️ Duplicates Across Servers",
                    value=f"{len(dup_result)} kick names on multiple servers:\n{dup_text}",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="✅ No Duplicates", value="Each kick name belongs to one server only", inline=False
                )

            await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"❌ Error running diagnostics: {e}")
        logger.info(f"[fixlinks check] Error: {e}")


@fixlinks.command(name="backfill")
@commands.has_permissions(administrator=True)
async def fixlinks_backfill(ctx):
    """[ADMIN] Fix rows with missing discord_server_id."""
    if not DISCORD_GUILD_ID:
        await ctx.send("❌ DISCORD_GUILD_ID not configured. Cannot determine target server.")
        return

    try:
        with engine.begin() as conn:
            # Count rows needing fix
            result = conn.execute(
                text(
                    """
                SELECT COUNT(*) as cnt
                FROM links
                WHERE discord_server_id IS NULL OR discord_server_id = 0
            """
                )
            ).fetchone()
            missing_count = result[0] if result else 0

            if missing_count == 0:
                await ctx.send("✅ No rows need backfilling. All rows have valid discord_server_id.")
                return

            # Update missing values
            conn.execute(
                text(
                    """
                UPDATE links
                SET discord_server_id = :sid
                WHERE discord_server_id IS NULL OR discord_server_id = 0
            """
                ),
                {"sid": DISCORD_GUILD_ID},
            )

            await ctx.send(
                f"✅ **Backfill Complete**\n" f"Updated {missing_count} rows with server ID `{DISCORD_GUILD_ID}`"
            )
            logger.info(f"[fixlinks backfill] Updated {missing_count} rows")

    except Exception as e:
        await ctx.send(f"❌ Error during backfill: {e}")
        logger.info(f"[fixlinks backfill] Error: {e}")


@fixlinks.command(name="duplicates")
@commands.has_permissions(administrator=True)
async def fixlinks_duplicates(ctx):
    """[ADMIN] Show kick names that exist on multiple servers."""
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT
                    l.kick_name,
                    l.discord_server_id,
                    l.discord_id,
                    l.linked_at
                FROM links l
                WHERE l.kick_name IN (
                    SELECT kick_name
                    FROM links
                    WHERE discord_server_id IS NOT NULL AND discord_server_id != 0
                    GROUP BY kick_name
                    HAVING COUNT(DISTINCT discord_server_id) > 1
                )
                ORDER BY l.kick_name, l.linked_at DESC
            """
                )
            ).fetchall()

            if not result:
                await ctx.send("✅ No duplicate kick names found across servers.")
                return

            # Group by kick_name
            duplicates = {}
            for row in result:
                kick_name = row[0]
                if kick_name not in duplicates:
                    duplicates[kick_name] = []
                duplicates[kick_name].append({"server_id": row[1], "discord_id": row[2], "linked_at": row[3]})

            embed = discord.Embed(
                title="⚠️ Duplicate Kick Names Across Servers",
                description=f"Found {len(duplicates)} kick names on multiple servers",
                color=0xE74C3C,
            )

            for kick_name, entries in list(duplicates.items())[:10]:  # Show max 10
                servers_text = "\n".join(
                    [
                        f"Server `{e['server_id']}`: <@{e['discord_id']}> (linked {e['linked_at'].strftime('%Y-%m-%d') if e['linked_at'] else 'unknown'})"
                        for e in entries
                    ]
                )
                embed.add_field(name=f"🔗 {kick_name}", value=servers_text, inline=False)

            if len(duplicates) > 10:
                embed.set_footer(text=f"Showing 10 of {len(duplicates)} duplicates")

            embed.add_field(
                name="How to Resolve",
                value="Use `!fixlinks resolve <kick_name> <keep_server_id>` to keep one link and remove others",
                inline=False,
            )

            await ctx.send(embed=embed)

    except Exception as e:
        await ctx.send(f"❌ Error fetching duplicates: {e}")
        logger.info(f"[fixlinks duplicates] Error: {e}")


@fixlinks.command(name="resolve")
@commands.has_permissions(administrator=True)
async def fixlinks_resolve(ctx, kick_name: str, keep_server_id: int):
    """[ADMIN] Resolve duplicate by keeping one server's link and removing others."""
    try:
        with engine.begin() as conn:
            # Check if duplicate exists
            result = conn.execute(
                text(
                    """
                SELECT discord_server_id, discord_id
                FROM links
                WHERE kick_name = :name
                ORDER BY discord_server_id
            """
                ),
                {"name": kick_name},
            ).fetchall()

            if not result:
                await ctx.send(f"❌ No links found for kick name `{kick_name}`")
                return

            if len(result) == 1:
                await ctx.send(f"✅ `{kick_name}` only exists on one server. No action needed.")
                return

            # Check if keep_server_id is valid
            server_ids = [r[0] for r in result]
            if keep_server_id not in server_ids:
                await ctx.send(
                    f"❌ Server `{keep_server_id}` doesn't have a link for `{kick_name}`\n"
                    f"Available servers: {', '.join(map(str, server_ids))}"
                )
                return

            # Delete other servers' links
            deleted = conn.execute(
                text(
                    """
                DELETE FROM links
                WHERE kick_name = :name AND discord_server_id != :keep_server
                RETURNING discord_server_id, discord_id
            """
                ),
                {"name": kick_name, "keep_server": keep_server_id},
            ).fetchall()

            deleted_info = ", ".join([f"Server {r[0]} (<@{r[1]}>)" for r in deleted])

            await ctx.send(
                f"✅ **Duplicate Resolved**\n"
                f"Kept: `{kick_name}` on server `{keep_server_id}`\n"
                f"Removed from: {deleted_info}"
            )
            logger.info(
                f"[fixlinks resolve] Kept {kick_name} on server {keep_server_id}, removed {len(deleted)} duplicates"
            )

    except Exception as e:
        await ctx.send(f"❌ Error resolving duplicate: {e}")
        logger.info(f"[fixlinks resolve] Error: {e}")


# -------------------------
# Point Shop Interactive Components
# -------------------------


class _ShopSoldOut(Exception):
    """Raised inside the purchase transaction when the last unit is taken by a
    concurrent buyer. Propagating it out of `engine.begin()` rolls the whole
    transaction back (restoring the just-deducted points); the handler then
    shows a sold-out message instead of the generic error."""


class PointShopConfirmView(discord.ui.View):
    """View with button to confirm purchase"""

    def __init__(
        self,
        item_id: int,
        item_name: str,
        price: int,
        kick_username: str,
        discord_id: int,
        guild_id: int,
        requirement_value: str = None,
        note: str = None,
        item_type: str = "custom",
        raffle_ticket_amount: int = 0,
    ):
        super().__init__(timeout=300)  # 5 minute timeout
        self.item_id = item_id
        self.item_name = item_name
        self.price = price
        self.kick_username = kick_username
        self.discord_id = discord_id
        self.guild_id = guild_id
        self.server_id = guild_id  # Set server_id for database queries
        self.requirement_value = requirement_value
        self.note = note
        self.item_type = item_type or "custom"
        self.raffle_ticket_amount = raffle_ticket_amount or 0

    @discord.ui.button(label="Complete Purchase", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Process the purchase when button is clicked"""
        try:
            with engine.begin() as conn:
                # Get item details (fresh from DB)
                item = conn.execute(
                    text(
                        """
                    SELECT id, name, price, stock, is_active, requirement_title, requirement_footer,
                           COALESCE(item_type, 'custom') as item_type, COALESCE(raffle_ticket_amount, 0) as raffle_ticket_amount
                    FROM point_shop_items
                    WHERE id = :id
                """
                    ),
                    {"id": self.item_id},
                ).fetchone()

                if not item:
                    await interaction.response.edit_message(content="❌ This item no longer exists.", view=None)
                    return

                (
                    item_id,
                    item_name,
                    price,
                    stock,
                    is_active,
                    requirement_title,
                    requirement_footer,
                    item_type,
                    raffle_ticket_amount,
                ) = item

                if not is_active:
                    await interaction.response.edit_message(content="❌ This item is no longer available.", view=None)
                    return

                if stock == 0:
                    await interaction.response.edit_message(content="❌ This item is sold out!", view=None)
                    return

                # Lock this buyer's points row FIRST. FOR UPDATE serializes
                # concurrent purchases by the same buyer (two Discord clicks at
                # once, or a simultaneous web-shop purchase) for the rest of the
                # transaction. Acquiring it BEFORE the per-user-cap count is what
                # makes that check race-safe: a second concurrent request blocks
                # here until the first commits, then counts the first's sale.
                user_points = conn.execute(
                    text(
                        """
                    SELECT points FROM user_points
                    WHERE kick_username = :k AND discord_server_id = :g
                    FOR UPDATE
                """
                    ),
                    {"k": self.kick_username, "g": self.guild_id},
                ).fetchone()

                # Authoritative per-user cap check: re-read inside this transaction so a
                # modal left open (or two clicked at once) can't push the buyer past it.
                reached_limit = per_user_limit_blocked(conn, item_id, self.discord_id, self.server_id)
                if reached_limit is not None:
                    cap, window = reached_limit
                    per = f" per {format_duration(window)}" if window else ""
                    await interaction.response.edit_message(
                        content=f"❌ Purchase limit reached! You can only buy **{cap}** of **{item_name}**{per}.",
                        view=None,
                    )
                    return

                if not user_points or user_points[0] < price:
                    current_balance = user_points[0] if user_points else 0
                    await interaction.response.edit_message(
                        content=f"❌ Insufficient points! You have **{current_balance:,}** points but need **{price:,}** points.",
                        view=None,
                    )
                    return

                # Deduct points atomically. The `points >= :p` guard makes the DB
                # enforce sufficiency; if a concurrent path already spent the
                # balance, no row matches and we abort instead of going negative.
                deducted = conn.execute(
                    text(
                        """
                    UPDATE user_points
                    SET points = points - :p,
                        total_spent = total_spent + :p,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE kick_username = :k AND discord_server_id = :g AND points >= :p
                    RETURNING points
                """
                    ),
                    {"p": price, "k": self.kick_username, "g": self.guild_id},
                ).fetchone()

                if deducted is None:
                    await interaction.response.edit_message(
                        content="❌ Insufficient points! Your balance changed — please try again.",
                        view=None,
                    )
                    return

                # Reduce stock if not unlimited. The `stock > 0` guard rejects an
                # oversell under concurrency; a lost race aborts the whole
                # transaction so the points deduction above is rolled back too.
                if stock > 0:
                    reduced = conn.execute(
                        text(
                            """
                        UPDATE point_shop_items
                        SET stock = stock - 1,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id AND stock > 0
                        RETURNING stock
                    """
                        ),
                        {"id": item_id},
                    ).fetchone()

                    if reduced is None:
                        # Last unit was taken concurrently — undo the deduction.
                        raise _ShopSoldOut()

                # Record the sale with requirement input (and note in requirement if both exist)
                full_requirement = self.requirement_value
                if self.note:
                    full_requirement = (
                        f"{self.requirement_value}\nNote: {self.note}"
                        if self.requirement_value
                        else f"Note: {self.note}"
                    )

                # Determine sale status: auto-complete for raffle ticket items
                sale_status = "completed" if item_type == "raffle_tickets" else "pending"

                # Create sale record and capture order ID
                sale_id_row = conn.execute(
                    text(
                        """
                    INSERT INTO point_sales (item_id, kick_username, discord_id, discord_server_id, item_name, price_paid, quantity, status, requirement_input)
                    VALUES (:item_id, :kick, :discord, :server_id, :name, :price, 1, :status, :req_input)
                    RETURNING id
                """
                    ),
                    {
                        "item_id": item_id,
                        "kick": self.kick_username,
                        "discord": self.discord_id,
                        "server_id": self.server_id,
                        "name": item_name,
                        "price": price,
                        "status": sale_status,
                        "req_input": full_requirement,
                    },
                ).fetchone()

                sale_id = int(sale_id_row[0]) if sale_id_row and sale_id_row[0] is not None else None

                # Create a DB notification and publish a Redis event for the bot subscriber
                notification_data = {
                    "sale_id": sale_id,
                    "item_id": item_id,
                    "item_name": item_name,
                    "buyer": self.kick_username,
                    "price": price,
                    "discord_id": self.discord_id,
                    "requirement_title": requirement_title,
                    "requirement_footer": requirement_footer,
                    "requirement_input": self.requirement_value,
                    "item_type": item_type,
                    "raffle_ticket_amount": raffle_ticket_amount,
                    "sale_status": sale_status,
                }
                if self.note:
                    notification_data["note"] = self.note

                notif_row = conn.execute(
                    text(
                        """
                    INSERT INTO notifications (discord_server_id, type, title, message, data)
                    VALUES (:server_id, 'new_sale', :title, :message, :data)
                    RETURNING id
                """
                    ),
                    {
                        "server_id": self.server_id,
                        "title": f"New purchase: {item_name}",
                        "message": f"{self.kick_username} bought {item_name} for {price:,} points",
                        "data": json.dumps(notification_data),
                    },
                ).fetchone()

                notif_id = int(notif_row[0]) if notif_row and notif_row[0] is not None else None

                # Queue Web Push deliveries in the same transaction as the
                # notification. PostgreSQL emits NOTIFY only after commit, so
                # the dedicated worker always sees durable rows. A savepoint
                # keeps push infrastructure failures from cancelling an order.
                if notif_id is not None:
                    push_savepoint = conn.begin_nested()
                    try:
                        queued_rows = conn.execute(
                            text(
                                """
                            INSERT INTO web_push_deliveries (notification_id, subscription_id)
                            SELECT :notification_id, id
                            FROM web_push_subscriptions
                            WHERE discord_server_id = :server_id
                            ON CONFLICT (notification_id, subscription_id) DO NOTHING
                            RETURNING id
                        """
                            ),
                            {"notification_id": notif_id, "server_id": self.server_id},
                        ).fetchall()
                        if queued_rows:
                            conn.execute(
                                text("SELECT pg_notify('web_push_queued', CAST(:notification_id AS TEXT))"),
                                {"notification_id": notif_id},
                            )
                        push_savepoint.commit()
                    except Exception as push_error:
                        push_savepoint.rollback()
                        logger.warning(
                            "[Point Shop] Web Push enqueue skipped for notification %s: %s",
                            notif_id,
                            push_error,
                        )

                # Auto-award raffle tickets if applicable (use savepoint so failure doesn't abort main transaction)
                raffle_tickets_awarded = False
                if item_type == "raffle_tickets" and raffle_ticket_amount > 0:
                    try:
                        savepoint = conn.begin_nested()
                        period_row = conn.execute(
                            text(
                                """
                                SELECT id FROM raffle_periods
                                WHERE discord_server_id = :server_id AND status = 'active'
                                ORDER BY created_at DESC LIMIT 1
                            """
                            ),
                            {"server_id": self.server_id},
                        ).fetchone()

                        if period_row:
                            period_id = period_row[0]
                            conn.execute(
                                text(
                                    """
                                    INSERT INTO raffle_tickets
                                        (period_id, discord_server_id, discord_id, kick_name, bonus_tickets, total_tickets, last_updated)
                                    VALUES (:period_id, :server_id, :discord_id, :kick_name, :amount, :amount, CURRENT_TIMESTAMP)
                                    ON CONFLICT (period_id, discord_id)
                                    DO UPDATE SET
                                        bonus_tickets = raffle_tickets.bonus_tickets + :amount,
                                        total_tickets = raffle_tickets.total_tickets + :amount,
                                        last_updated = CURRENT_TIMESTAMP
                                """
                                ),
                                {
                                    "period_id": period_id,
                                    "server_id": self.server_id,
                                    "discord_id": self.discord_id,
                                    "kick_name": self.kick_username,
                                    "amount": raffle_ticket_amount,
                                },
                            )
                            conn.execute(
                                text(
                                    """
                                    INSERT INTO raffle_ticket_log
                                        (period_id, discord_id, kick_name, ticket_change, source, description)
                                    VALUES (:period_id, :discord_id, :kick_name, :amount, 'bonus', :desc)
                                """
                                ),
                                {
                                    "period_id": period_id,
                                    "discord_id": self.discord_id,
                                    "kick_name": self.kick_username,
                                    "amount": raffle_ticket_amount,
                                    "desc": f"Shop purchase: {raffle_ticket_amount} raffle tickets (Order #{sale_id})",
                                },
                            )
                            savepoint.commit()
                            raffle_tickets_awarded = True
                            logger.info(
                                f"[Point Shop] ✅ Auto-awarded {raffle_ticket_amount} raffle tickets to {self.kick_username} (Order #{sale_id})"
                            )
                        else:
                            savepoint.rollback()
                            logger.info(
                                f"[Point Shop] ⚠️ No active raffle period for server {self.server_id} - tickets NOT awarded for Order #{sale_id}"
                            )
                    except Exception as ticket_err:
                        try:
                            savepoint.rollback()
                        except Exception:
                            pass
                        logger.info(
                            f"[Point Shop] ⚠️ Failed to auto-award raffle tickets for Order #{sale_id}: {ticket_err}"
                        )
                        import traceback

                        traceback.print_exc()

                # Get updated balance
                new_balance = conn.execute(
                    text(
                        """
                    SELECT points FROM user_points WHERE kick_username = :k AND discord_server_id = :g
                """
                    ),
                    {"k": self.kick_username, "g": self.guild_id},
                ).fetchone()[0]

            # Transaction committed — now publish Redis events so subscribers see committed data
            publish_redis_event(
                "dashboard:notifications",
                "new_notification",
                {
                    "notification_id": notif_id,
                    "discord_server_id": str(self.server_id) if self.server_id is not None else None,
                    "type": "new_sale",
                    "title": f"New purchase: {item_name}",
                    "message": f"{self.kick_username} bought {item_name} for {price:,} points",
                    "data": notification_data,
                },
            )

            # Publish event to notify dashboard of new purchase
            publish_redis_event(
                "point_shop",
                "new_purchase",
                {
                    "item_id": item_id,
                    "item_name": item_name,
                    "buyer": self.kick_username,
                    "price": price,
                    "discord_id": self.discord_id,
                    "item_type": item_type,
                    "raffle_ticket_amount": raffle_ticket_amount,
                },
            )

            # Success response
            note_text = f"\n📝 Note: _{self.note}_" if self.note else ""
            req_text = f"\n📋 {self.requirement_value}" if self.requirement_value else ""
            if raffle_tickets_awarded:
                fulfillment_text = f"🎟️ **{raffle_ticket_amount} raffle ticket{'s' if raffle_ticket_amount != 1 else ''}** have been added to your balance!"
            else:
                fulfillment_text = "_An admin will fulfill your purchase soon._"
            await interaction.response.edit_message(
                content=f"✅ **Purchase Successful!**\n"
                f"🛒 You bought **{item_name}** for **{price:,}** points!\n"
                f"💰 Your new balance: **{new_balance:,}** points{req_text}{note_text}\n\n"
                f"{fulfillment_text}",
                view=None,
            )

            logger.info(
                f"[Point Shop] {self.kick_username} (Discord ID: {self.discord_id}) purchased {item_name} for {price} points"
            )

        except _ShopSoldOut:
            # The transaction rolled back on the way out, so the points deduction
            # was undone — nothing to refund. Just tell the buyer.
            await interaction.response.edit_message(
                content="❌ This item just sold out! Your points were not spent.", view=None
            )
        except Exception as e:
            logger.warning(f"[Point Shop] Purchase error: {e}")
            import traceback

            traceback.print_exc()
            await interaction.response.edit_message(
                content=f"❌ An error occurred during purchase. Please try again later.", view=None
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Cancel the purchase"""
        await interaction.response.edit_message(content="❌ Purchase cancelled.", view=None)


class PointShopPurchaseModal(discord.ui.Modal):
    """Modal for confirming a point shop purchase"""

    def __init__(
        self,
        item_id: int,
        item_name: str,
        price: int,
        description: str,
        stock: int,
        requirement_title: str = None,
        requirement_footer: str = None,
        item_type: str = "custom",
        raffle_ticket_amount: int = 0,
    ):
        super().__init__(title=f"Purchase: {item_name[:40]}")
        self.item_id = item_id
        self.item_name = item_name
        self.price = price
        self.description = description
        self.stock = stock
        self.requirement_title = requirement_title
        self.requirement_footer = requirement_footer
        self.item_type = item_type or "custom"
        self.raffle_ticket_amount = raffle_ticket_amount or 0

        # Stock display text
        stock_text = "Unlimited" if stock < 0 else f"{stock} remaining"

        # Add requirement input field if needed
        self.requirement_input = None
        if requirement_title:
            label = requirement_title[:45]  # Discord label limit
            placeholder = requirement_footer[:100] if requirement_footer else "Enter required information"
            self.requirement_input = discord.ui.TextInput(
                label=label, placeholder=placeholder, required=True, max_length=200, style=discord.TextStyle.short
            )
            self.add_item(self.requirement_input)

        # Optional note/message field
        self.note_input = discord.ui.TextInput(
            label="Note for admin (optional)",
            placeholder="e.g., preferred delivery method, etc.",
            required=False,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.note_input)

    async def on_submit(self, interaction: discord.Interaction):
        """Show confirmation view after modal is submitted"""

        discord_id = interaction.user.id
        note = self.note_input.value.strip() if self.note_input.value else None
        requirement_value = self.requirement_input.value.strip() if self.requirement_input else None

        # Get user's balance
        with engine.connect() as conn:
            # Resolve the canonical account the person's shared balance lives under,
            # so the affordability check AND the later deduction hit the same row.
            kick_username, accounts = resolve_shop_identity(conn, discord_id, interaction.guild.id)

            if not accounts:
                await interaction.response.send_message("❌ You need to link your account first!", ephemeral=True)
                return

            # Get balance (with multiserver support)
            points_data = conn.execute(
                text(
                    """
                SELECT points FROM user_points
                WHERE kick_username = :k AND discord_server_id = :s
            """
                ),
                {"k": kick_username, "s": interaction.guild.id},
            ).fetchone()

            current_balance = points_data[0] if points_data else 0

        # Check if they can afford it
        if current_balance < self.price:
            await interaction.response.send_message(
                f"❌ **Insufficient Points!**\n\n"
                f"**{self.item_name}** costs **{self.price:,}** points\n"
                f"Your balance: **{current_balance:,}** points\n"
                f"You need **{self.price - current_balance:,}** more points!",
                ephemeral=True,
            )
            return

        # Build confirmation message
        confirm_text = f"**Confirm Purchase**\n\n"
        confirm_text += f"🛒 Item: **{self.item_name}**\n"
        confirm_text += f"💰 Price: **{self.price:,}** points\n"
        confirm_text += f"📊 Your balance: **{current_balance:,}** points\n"
        confirm_text += f"📊 Balance after: **{current_balance - self.price:,}** points\n"
        if requirement_value:
            req_label = self.requirement_title or "Requirement"
            confirm_text += f"\n📝 {req_label}: {requirement_value}\n"
        if note:
            confirm_text += f"\n💬 Note: _{note}_\n"

        # Create confirmation view with button
        view = PointShopConfirmView(
            item_id=self.item_id,
            item_name=self.item_name,
            price=self.price,
            kick_username=kick_username,
            discord_id=discord_id,
            guild_id=interaction.guild_id,
            requirement_value=requirement_value,
            note=note,
            item_type=self.item_type,
            raffle_ticket_amount=self.raffle_ticket_amount,
        )

        await interaction.response.send_message(confirm_text, view=view, ephemeral=True)


class PointShopItemSelect(discord.ui.Select):
    """Dropdown to select a shop item"""

    def __init__(self, items: list = None):
        options = []

        for item in (items or [])[:25]:  # Discord limit
            item_id, name, description, price, stock, image_url, is_active = item[:7]
            if is_active:
                # Stock display
                if stock < 0:
                    stock_text = "∞"
                elif stock == 0:
                    stock_text = "SOLD OUT"
                else:
                    stock_text = str(stock)

                # Format: "Item Name: Xpts" with "──── In-stock: X ────" as description
                options.append(
                    discord.SelectOption(
                        label=f"{name}: {price:,}pts"[:100],
                        description=f"━━━━ In-stock: {stock_text} ━━━━"[:100],
                        value=str(item_id),
                        emoji="🎁",
                    )
                )

        if not options:
            options.append(discord.SelectOption(label="No items available", value="none", emoji="❌"))

        super().__init__(
            placeholder="🛍️ Select an item to purchase...",
            options=options,
            custom_id="shop_item_select",
            min_values=0,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        """Show purchase modal when item is selected.

        Item details are read fresh from the DB (never from view state captured
        at post time) so price/stock/availability are always current — and so
        the stateless template view registered at startup can serve shop
        messages posted before a restart.
        """
        if not self.values:
            await interaction.response.defer()
            return

        selected_id = self.values[0]

        if selected_id == "none":
            await interaction.response.send_message("❌ No items are available for purchase right now.", ephemeral=True)
            return

        discord_id = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else None

        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                SELECT id, name, description, price, stock, requirement_title, requirement_footer,
                       COALESCE(item_type, 'custom') as item_type,
                       COALESCE(raffle_ticket_amount, 0) as raffle_ticket_amount,
                       is_active
                FROM point_shop_items
                WHERE id = :id AND discord_server_id = :g
            """
                ),
                {"id": int(selected_id), "g": guild_id},
            ).fetchone()

            if not row or not row[9]:
                await interaction.response.send_message("❌ This item is no longer available.", ephemeral=True)
                return

            (
                item_id,
                item_name,
                description,
                price,
                stock,
                requirement_title,
                requirement_footer,
                item_type,
                raffle_ticket_amount,
                _is_active,
            ) = row

            if stock == 0:
                await interaction.response.send_message(f"❌ **{item_name}** is sold out!", ephemeral=True)
                return

            # Resolve the canonical account the person's shared balance lives under,
            # so this pre-modal affordability check matches the (cross-platform)
            # balance the shop displays and later deducts from.
            kick_username, accounts = resolve_shop_identity(conn, discord_id, guild_id)

            if not accounts:
                await interaction.response.send_message(
                    "❌ You need to link your account first! Use this server's Link Account panel.",
                    ephemeral=True,
                )
                return

            # Get balance
            points_data = conn.execute(
                text(
                    """
                SELECT points FROM user_points WHERE kick_username = :k AND discord_server_id = :s
            """
                ),
                {"k": kick_username, "s": guild_id},
            ).fetchone()

            current_balance = points_data[0] if points_data else 0

            # Stop them at the dropdown if they've already used up their allowance,
            # rather than after they've filled in the purchase modal.
            reached_limit = per_user_limit_blocked(conn, item_id, discord_id, guild_id)

        if reached_limit is not None:
            cap, window = reached_limit
            per = f" per {format_duration(window)}" if window else ""
            await interaction.response.send_message(
                f"❌ **Purchase Limit Reached!**\n\nYou can only buy **{cap}** of **{item_name}**{per}.",
                ephemeral=True,
            )
            return

        # Check if they can afford it
        if current_balance < price:
            await interaction.response.send_message(
                f"❌ **Insufficient Points!**\n\n"
                f"**{item_name}** costs **{price:,}** points\n"
                f"Your balance: **{current_balance:,}** points\n"
                f"You need **{price - current_balance:,}** more points!",
                ephemeral=True,
            )
            return

        # Show the purchase modal
        modal = PointShopPurchaseModal(
            item_id=item_id,
            item_name=item_name,
            price=price,
            description=description or "No description",
            stock=stock,
            requirement_title=requirement_title,
            requirement_footer=requirement_footer,
            item_type=item_type or "custom",
            raffle_ticket_amount=raffle_ticket_amount or 0,
        )
        await interaction.response.send_modal(modal)

        # Discord clients otherwise keep rendering the last selected option on
        # the storefront after the modal opens. Rebuild the message's existing
        # components with no default option so the shared selector is ready for
        # the next purchase (and the next user) without retaining view state.
        if interaction.message:
            try:
                refreshed_view = discord.ui.LayoutView.from_message(interaction.message, timeout=None)
                for child in refreshed_view.walk_children():
                    if getattr(child, "custom_id", None) != "shop_item_select":
                        continue
                    for option in getattr(child, "options", []):
                        option.default = False
                await interaction.message.edit(view=refreshed_view)
            except Exception as exc:
                logger.warning("[Point Shop] Could not reset the item selector: %s", exc)


class PointShopView(discord.ui.View):
    """Persistent view for the point shop with item selector (legacy)"""

    def __init__(self, items: list):
        super().__init__(timeout=None)

        if items:
            self.add_item(PointShopItemSelect(items))

        self.add_item(PointShopBuyNowButton())
        self.add_item(PointShopBalanceButton())


def resolve_shop_identity(conn, discord_id, guild_id):
    """Resolve a member's point-shop identity for THIS server.

    Returns (canonical_username, accounts) where:
      - canonical_username is the single username the person's points accrue under
        (the alphabetically-first of all their linked usernames — the SAME rule the
        watchtime tracker uses, so balance is read from the row points are written
        to). Points are shared across a person's platforms, not per-platform.
      - accounts is the list of (username, platform) they've linked, for display.
    Returns (None, []) if the member has no linked account.
    """
    rows = conn.execute(
        text(
            """
            SELECT kick_name, platform FROM links
            WHERE discord_id = :d AND discord_server_id = :s
            """
        ),
        {"d": discord_id, "s": guild_id},
    ).fetchall()
    accounts = [(r[0], r[1]) for r in rows if r[0]]
    if not accounts:
        return None, []
    canonical = min(name.lower() for name, _ in accounts)
    return canonical, accounts


def format_linked_accounts(accounts):
    """Render linked accounts for a balance footer, e.g.
    'madcats (Kick), madcatstv (Twitch)'."""
    return ", ".join(f"{name} ({(platform or 'kick').capitalize()})" for name, platform in accounts)


# `-1` means "no cap", mirroring the `stock` column's unlimited sentinel.
UNLIMITED_PER_USER = -1

# `0` means the cap is measured over all time and never rolls over.
NO_LIMIT_WINDOW = 0

# point_settings key holding the timestamp of the last manual "reset limits".
LIMITS_RESET_KEY = "shop_limits_reset_at"


def format_duration(seconds):
    """Render a window as the largest whole unit that divides it, e.g. '7 days'."""
    seconds = int(seconds)
    for unit_seconds, name in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= unit_seconds and seconds % unit_seconds == 0:
            n = seconds // unit_seconds
            return f"{n} {name}{'s' if n != 1 else ''}"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def get_item_limit_config(conn, item_id):
    """Read an item's per-person cap and its rolling window, as (limit, window_seconds).

    Deliberately does not swallow errors: silently returning "unlimited" would let
    buyers past a configured cap, and the read runs inside the purchase transaction
    where a caught failure would poison the enclosing block anyway.
    """
    row = conn.execute(
        text(
            """
            SELECT COALESCE(per_user_limit, -1), COALESCE(limit_window_seconds, 0)
            FROM point_shop_items WHERE id = :id
            """
        ),
        {"id": item_id},
    ).fetchone()
    if not row:
        return UNLIMITED_PER_USER, NO_LIMIT_WINDOW
    return int(row[0] or UNLIMITED_PER_USER), int(row[1] or NO_LIMIT_WINDOW)


def get_limits_reset_at(conn, guild_id):
    """Timestamp of the last manual limit reset for this server, or None."""
    row = conn.execute(
        text("SELECT value FROM point_settings WHERE key = :k AND discord_server_id = :g"),
        {"k": LIMITS_RESET_KEY, "g": guild_id},
    ).fetchone()
    return (row[0] or None) if row else None


def count_user_item_purchases(conn, item_id, discord_id, guild_id, window_seconds=0, reset_at=None):
    """How many of `item_id` this person holds against their cap right now.

    Keyed on discord_id (not the platform username) so a viewer cannot reset their
    allowance by buying under their Kick handle and then their Twitch one — the same
    rule the shared point balance follows. Cancelled sales are refunded, so they
    release the allowance and are not counted.

    Two things exclude an otherwise-counting sale, both by raising the floor on
    `purchased_at` rather than deleting anything:
      - `window_seconds`: a rolling cooldown; only recent purchases count.
      - `reset_at`: the admin's last manual "reset limits".
    """
    sql = """
        SELECT COUNT(*) FROM point_sales
        WHERE item_id = :item_id
          AND discord_id = :discord_id
          AND discord_server_id = :guild_id
          AND status IN ('pending', 'completed')
    """
    params = {"item_id": item_id, "discord_id": discord_id, "guild_id": guild_id}

    if window_seconds and window_seconds > 0:
        # `purchased_at` is a naive timestamp defaulted from CURRENT_TIMESTAMP.
        sql += " AND purchased_at > CURRENT_TIMESTAMP - (:window * INTERVAL '1 second')"
        params["window"] = window_seconds

    if reset_at:
        # Round-trips through point_settings.value (TEXT); cast back explicitly.
        sql += " AND purchased_at > CAST(:reset_at AS timestamp)"
        params["reset_at"] = reset_at

    row = conn.execute(text(sql), params).fetchone()
    return int(row[0]) if row else 0


def per_user_limit_blocked(conn, item_id, discord_id, guild_id):
    """Return (limit, window_seconds) if this person has hit their cap, else None."""
    limit, window = get_item_limit_config(conn, item_id)
    if limit == UNLIMITED_PER_USER:
        return None
    bought = count_user_item_purchases(
        conn,
        item_id,
        discord_id,
        guild_id,
        window_seconds=window,
        reset_at=get_limits_reset_at(conn, guild_id),
    )
    if bought >= limit:
        return limit, window
    return None


class PointShopBalanceButton(discord.ui.Button):
    """Button to check point balance"""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary, label="Check Balance", custom_id="shop_check_balance", emoji="💰"
        )

    async def callback(self, interaction: discord.Interaction):
        """Show user's point balance"""
        discord_id = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else None

        with engine.connect() as conn:
            canonical, accounts = resolve_shop_identity(conn, discord_id, guild_id)

            if not accounts:
                await interaction.response.send_message(
                    "❌ You need to link your account first! Use this server's Link Account panel.",
                    ephemeral=True,
                )
                return

            points_data = conn.execute(
                text(
                    """
                SELECT points, total_earned, total_spent
                FROM user_points
                WHERE kick_username = :k AND discord_server_id = :s
            """
                ),
                {"k": canonical, "s": guild_id},
            ).fetchone()

            if not points_data:
                points, total_earned, total_spent = 0, 0, 0
            else:
                points, total_earned, total_spent = points_data

        embed = discord.Embed(title="💰 Your Point Balance", color=0xFFD700)
        embed.add_field(name="Current Balance", value=f"**{points:,}** points", inline=True)
        embed.add_field(name="Total Earned", value=f"{total_earned:,} points", inline=True)
        embed.add_field(name="Total Spent", value=f"{total_spent:,} points", inline=True)
        embed.set_footer(text=f"Account: {format_linked_accounts(accounts)}")

        await interaction.response.send_message(embed=embed, ephemeral=True)


class PointShopPickerView(discord.ui.View):
    """Short-lived private picker opened from the public storefront."""

    def __init__(self, items: list):
        super().__init__(timeout=300)
        self.add_item(PointShopItemSelect(items))


class PointShopBuyNowButton(discord.ui.Button):
    """Open a DB-fresh item picker visible only to the buyer."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Buy Now",
            custom_id="shop_buy_now",
            emoji="🛒",
        )

    async def callback(self, interaction: discord.Interaction):
        guild_id = interaction.guild.id if interaction.guild else None

        with engine.connect() as conn:
            items = conn.execute(
                text(
                    """
                SELECT id, name, description, price, stock, image_url, is_active
                FROM point_shop_items
                WHERE is_active = TRUE AND discord_server_id = :guild_id
                ORDER BY price ASC
            """
                ),
                {"guild_id": guild_id},
            ).fetchall()

        if not items:
            await interaction.response.send_message(
                "❌ No items are available for purchase right now.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select an item to continue your purchase:",
            view=PointShopPickerView(items),
            ephemeral=True,
        )


class ShopLayoutView(discord.ui.LayoutView):
    """Single-message Components V2 storefront: header, mosaic grid, purchase
    select, balance button, and footer all live in one message, so shop updates
    can edit it in place instead of re-posting a multi-message stack."""

    def __init__(self, items: list = None, has_mosaic: bool = False, force_select: bool = False):
        super().__init__(timeout=None)
        items = items or []

        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay("# 🛍️ Point Shop"),
                discord.ui.TextDisplay("Spend your hard-earned points on awesome rewards!"),
                accent_colour=0xFFD700,
            )
        )

        if has_mosaic:
            self.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem("attachment://shop_items.png")))

        if not items:
            self.add_item(discord.ui.TextDisplay("No items available at the moment. Check back later!"))

        self.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))

        if items or force_select:
            self.add_item(discord.ui.ActionRow(PointShopItemSelect(items)))
        self.add_item(discord.ui.ActionRow(PointShopBuyNowButton(), PointShopBalanceButton()))
        self.add_item(discord.ui.TextDisplay("💡 *Earn points by watching streams!*"))

    @classmethod
    def template(cls):
        """Stateless instance for bot.add_view at startup: keeps the select and
        purchase and balance controls on already-posted shop messages working across restarts
        (their callbacks read everything from the interaction + DB)."""
        return cls(items=[], force_select=True)


async def create_shop_mosaic_image(items, max_width=2400):
    """Create a grid mosaic image from shop item images, preserving original aspect ratios

    Layout per item:
    ┌─────────────┐
    │ Item Name   │  <- Title above image (no number)
    ├─────────────┤
    │   IMAGE     │  <- Much larger, high quality image
    │             │
    ├─────────────┤
    │ 500 pts     │  <- Bigger price
    │ In-stock: 10│  <- Full stock text
    └─────────────┘

    Args:
        items: List of shop items
        max_width: Maximum width of the entire mosaic (default 2400px for very large images)
    """
    import io
    import math

    import aiohttp
    from PIL import Image, ImageDraw, ImageFont

    # Filter items with images
    items_with_images = [(i, item) for i, item in enumerate(items) if item[5]]  # item[5] is image_url

    if not items_with_images:
        return None

    # Settings - VERY LARGE sizes for maximum visibility
    COLS = min(len(items_with_images), 3)  # Max 3 columns
    PADDING = 30
    TITLE_HEIGHT = 100  # Title above image
    FOOTER_HEIGHT = 210  # Price + stock below image
    # Sized so text stays readable after Discord downscales the 2400px canvas ~4x to chat width
    TITLE_FONT_SIZE = 64
    PRICE_FONT_SIZE = 72
    STOCK_FONT_SIZE = 52
    BG_COLOR = (30, 30, 35)
    TITLE_BG_COLOR = (45, 45, 55)
    FOOTER_BG_COLOR = (50, 50, 60)
    TEXT_COLOR = (255, 255, 255)
    PRICE_COLOR = (255, 215, 0)  # Gold for price
    STOCK_COLOR = (100, 200, 100)  # Green for stock
    SOLDOUT_COLOR = (255, 100, 100)  # Red for sold out

    # Calculate cell width based on max_width and columns (very large cells)
    cell_width = (max_width - (COLS + 1) * PADDING) // COLS

    # Try to load fonts - prefer bold/semi-bold for titles
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", TITLE_FONT_SIZE)
        price_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", PRICE_FONT_SIZE)
        stock_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", STOCK_FONT_SIZE)
    except:
        try:
            title_font = ImageFont.truetype("arialbd.ttf", TITLE_FONT_SIZE)  # Arial Bold
            price_font = ImageFont.truetype("arialbd.ttf", PRICE_FONT_SIZE)
            stock_font = ImageFont.truetype("arial.ttf", STOCK_FONT_SIZE)
        except:
            try:
                title_font = ImageFont.truetype("arial.ttf", TITLE_FONT_SIZE)
                price_font = ImageFont.truetype("arial.ttf", PRICE_FONT_SIZE)
                stock_font = ImageFont.truetype("arial.ttf", STOCK_FONT_SIZE)
            except:
                title_font = ImageFont.load_default()
                price_font = title_font
                stock_font = title_font

    # First pass: download all images and calculate row heights
    downloaded_images = []
    async with aiohttp.ClientSession() as session:
        for item_idx, item in items_with_images:
            item_id, name, description, price, stock, image_url, is_active, requirement_title, requirement_footer = (
                item[:9]
            )

            try:
                async with session.get(image_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        img_data = await resp.read()
                        img = Image.open(io.BytesIO(img_data))

                        # Convert to RGB if necessary
                        if img.mode in ("RGBA", "P"):
                            bg = Image.new("RGB", img.size, BG_COLOR)
                            if img.mode == "P":
                                img = img.convert("RGBA")
                            bg.paste(img, mask=img.split()[3] if len(img.split()) > 3 else None)
                            img = bg
                        elif img.mode != "RGB":
                            img = img.convert("RGB")

                        # Scale image to fit cell width while preserving aspect ratio
                        orig_w, orig_h = img.size
                        scale = cell_width / orig_w
                        new_h = int(orig_h * scale)
                        img = img.resize((cell_width, new_h), Image.Resampling.LANCZOS)

                        downloaded_images.append((item_idx, item, img))
                    else:
                        downloaded_images.append((item_idx, item, None))
            except Exception as e:
                logger.info(f"[Point Shop Mosaic] Failed to load image for {name}: {e}")
                downloaded_images.append((item_idx, item, None))

    if not downloaded_images:
        return None

    # Group into rows and calculate height per row
    rows_data = [downloaded_images[i : i + COLS] for i in range(0, len(downloaded_images), COLS)]
    row_heights = []

    for row in rows_data:
        # Find max image height in this row
        max_img_height = 0
        for item_idx, item, img in row:
            if img:
                max_img_height = max(max_img_height, img.size[1])
            else:
                max_img_height = max(max_img_height, 500)  # Very large placeholder height
        row_heights.append(max_img_height)

    # Calculate total canvas size (title + image + footer per row)
    total_height = PADDING
    for rh in row_heights:
        total_height += TITLE_HEIGHT + rh + FOOTER_HEIGHT + PADDING

    canvas_width = COLS * cell_width + (COLS + 1) * PADDING
    canvas = Image.new("RGB", (canvas_width, total_height), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # Second pass: place images on canvas
    current_y = PADDING
    for row_idx, row in enumerate(rows_data):
        row_height = row_heights[row_idx]

        for col_idx, (item_idx, item, img) in enumerate(row):
            item_id, name, description, price, stock, image_url, is_active, requirement_title, requirement_footer = (
                item[:9]
            )

            x = PADDING + col_idx * (cell_width + PADDING)

            # Draw TITLE background (above image)
            draw.rectangle([x, current_y, x + cell_width, current_y + TITLE_HEIGHT], fill=TITLE_BG_COLOR)

            # Draw item title (no item number, just the name), trimmed by
            # measured width so it can't overflow the cell at any font size
            name_text = name
            if draw.textlength(name_text, font=title_font) > cell_width - 20:
                while name_text and draw.textlength(name_text + "…", font=title_font) > cell_width - 20:
                    name_text = name_text[:-1]
                name_text += "…"
            # Center the title text in its band
            bbox = draw.textbbox((0, 0), name_text, font=title_font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            text_x = x + (cell_width - text_width) // 2
            text_y = current_y + (TITLE_HEIGHT - text_height) // 2 - bbox[1]
            draw.text((text_x, text_y), name_text, fill=TEXT_COLOR, font=title_font)

            # IMAGE area starts after title
            img_y = current_y + TITLE_HEIGHT

            if img:
                # Center image vertically in its cell if shorter than row height
                img_h = img.size[1]
                y_offset = (row_height - img_h) // 2
                canvas.paste(img, (x, img_y + y_offset))

                # Draw sold out overlay if applicable
                if stock == 0:
                    overlay = Image.new("RGBA", img.size, (0, 0, 0, 150))
                    composited = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
                    canvas.paste(composited, (x, img_y + y_offset))

                    # Draw "SOLD OUT" text centered on image
                    sold_text = "SOLD OUT"
                    bbox = draw.textbbox((0, 0), sold_text, font=price_font)
                    tw = bbox[2] - bbox[0]
                    th = bbox[3] - bbox[1]
                    draw.text(
                        (x + (cell_width - tw) // 2, img_y + y_offset + (img_h - th) // 2),
                        sold_text,
                        fill=SOLDOUT_COLOR,
                        font=price_font,
                    )
            else:
                # Draw placeholder
                draw.rectangle([x, img_y, x + cell_width, img_y + row_height], fill=(60, 60, 70))
                draw.text((x + 10, img_y + row_height // 2), "No Image", fill=TEXT_COLOR, font=title_font)

            # Draw FOOTER background (below image)
            footer_y = img_y + row_height
            draw.rectangle([x, footer_y, x + cell_width, footer_y + FOOTER_HEIGHT], fill=FOOTER_BG_COLOR)

            # Line 1: Price (big, gold)
            price_text = f"{price:,} pts"
            draw.text((x + 10, footer_y + 18), price_text, fill=PRICE_COLOR, font=price_font)

            # Line 2: Stock text
            if stock < 0:
                stock_text = "In-stock: ∞"
                stock_color = STOCK_COLOR
            elif stock == 0:
                stock_text = "In-stock: SOLD OUT"
                stock_color = SOLDOUT_COLOR
            else:
                stock_text = f"In-stock: {stock}"
                stock_color = STOCK_COLOR

            draw.text((x + 10, footer_y + 115), stock_text, fill=stock_color, font=stock_font)

        current_y += TITLE_HEIGHT + row_height + FOOTER_HEIGHT + PADDING

    # Save to bytes with high quality
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=False)  # No optimization for better quality
    output.seek(0)

    return output


async def post_point_shop_to_discord(
    bot, guild_id: int = None, channel_id: int = None, update_existing: bool = True, use_components_v2: bool = True
):
    """Post or update the single-message point shop storefront (Components V2).

    The storefront message carries the header, mosaic grid, purchase select,
    balance button, and footer together, and is edited in place on updates so
    it keeps its channel position and message ID. A fresh message is only
    posted when none exists yet, it was deleted, or the shop channel changed.
    """

    try:
        # If channel_id is provided, get the channel and derive guild_id from it
        if channel_id:
            channel = bot.get_channel(channel_id)
            if not channel:
                logger.debug(f"[Point Shop] Channel {channel_id} not found")
                return False
            # Get guild_id from channel if not provided
            if not guild_id:
                guild_id = channel.guild.id
        else:
            # No channel_id provided, need to look it up using guild_id
            if not guild_id and bot.guilds:
                guild_id = bot.guilds[0].id

            # Get channel_id from settings
            with engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM point_settings
                    WHERE key = 'shop_channel_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchone()

                if not result:
                    logger.debug("[Point Shop] No shop channel configured")
                    return False

                channel_id = int(result[0])

            channel = bot.get_channel(channel_id)
            if not channel:
                logger.debug(f"[Point Shop] Channel {channel_id} not found")
                return False

        # Get active shop items
        logger.debug(f"[Point Shop] Querying items for guild_id: {guild_id}")
        with engine.connect() as conn:
            items = conn.execute(
                text(
                    """
                SELECT id, name, description, price, stock, image_url, is_active, requirement_title, requirement_footer,
                       COALESCE(item_type, 'custom') as item_type, COALESCE(raffle_ticket_amount, 0) as raffle_ticket_amount
                FROM point_shop_items
                WHERE is_active = TRUE AND discord_server_id = :guild_id
                ORDER BY price ASC
            """
                ),
                {"guild_id": guild_id},
            ).fetchall()
            logger.debug(f"[Point Shop] Found {len(items)} items")

            # Get existing message ID
            existing_msg_result = conn.execute(
                text(
                    """
                SELECT value FROM point_settings
                WHERE key = 'shop_message_id' AND discord_server_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()
            existing_message_id = int(existing_msg_result[0]) if existing_msg_result else None

            # Get existing interactive message ID
            existing_interactive_result = conn.execute(
                text(
                    """
                SELECT value FROM point_settings
                WHERE key = 'shop_interactive_id' AND discord_server_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()
            existing_interactive_id = int(existing_interactive_result[0]) if existing_interactive_result else None

            # Get existing footer message ID
            existing_footer_result = conn.execute(
                text(
                    """
                SELECT value FROM point_settings
                WHERE key = 'shop_footer_id' AND discord_server_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()
            existing_footer_id = int(existing_footer_result[0]) if existing_footer_result else None

            # Which channel the storefront message currently lives in (it may
            # differ from `channel` if an admin re-pointed the shop).
            stored_channel_result = conn.execute(
                text(
                    """
                SELECT value FROM point_settings
                WHERE key = 'shop_message_channel_id' AND discord_server_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            ).fetchone()
            stored_channel_id = int(stored_channel_result[0]) if stored_channel_result else None

        old_channel = None
        if stored_channel_id and stored_channel_id != channel.id:
            old_channel = bot.get_channel(stored_channel_id)

        if update_existing:
            # One-time migration from the old 3-message stack: the interactive
            # and footer messages are obsolete now that the storefront message
            # carries the select, balance button, and footer itself.
            for msg_id in (existing_interactive_id, existing_footer_id):
                if not msg_id:
                    continue
                for ch in (channel, old_channel):
                    if ch is None:
                        continue
                    try:
                        old_msg = await ch.fetch_message(msg_id)
                        await old_msg.delete()
                        logger.debug(f"[Point Shop] Deleted legacy shop message {msg_id}")
                        break
                    except discord.NotFound:
                        continue
                    except Exception as e:
                        logger.warning(f"[Point Shop] Error deleting legacy shop message {msg_id}: {e}")
                        break
            if existing_interactive_id or existing_footer_id:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                        DELETE FROM point_settings
                        WHERE key IN ('shop_interactive_id', 'shop_footer_id') AND discord_server_id = :guild_id
                    """
                        ),
                        {"guild_id": guild_id},
                    )

            # If the shop moved to a different channel, remove the storefront
            # from the old one so two shops don't coexist.
            if old_channel and existing_message_id:
                try:
                    old_msg = await old_channel.fetch_message(existing_message_id)
                    await old_msg.delete()
                    logger.debug(f"[Point Shop] Deleted relocated shop message {existing_message_id}")
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.warning(f"[Point Shop] Error deleting relocated shop message {existing_message_id}: {e}")
                existing_message_id = None

        # Check if Components V2 is supported (discord.py 2.6+)
        has_components_v2 = hasattr(discord.ui, "LayoutView") and hasattr(discord.ui, "MediaGallery")

        logger.debug(
            f"[Point Shop] Components V2 available: {has_components_v2}, use_components_v2: {use_components_v2}, items count: {len(items)}"
        )

        if use_components_v2 and has_components_v2:
            # ==================== Components V2 Mode (single storefront message) ====================
            try:
                # Generate the mosaic image (name, price, stock already included in image)
                mosaic_image = await create_shop_mosaic_image(items) if items else None
                mosaic_file = discord.File(mosaic_image, filename="shop_items.png") if mosaic_image else None

                layout = ShopLayoutView(items, has_mosaic=mosaic_file is not None)

                # Edit the existing storefront in place so the shop keeps its
                # message ID and channel position; only post fresh if there is
                # no message yet or it's gone.
                message = None
                if update_existing and existing_message_id:
                    existing_message = None
                    try:
                        existing_message = await channel.fetch_message(existing_message_id)
                    except discord.NotFound:
                        pass
                    except Exception as e:
                        logger.warning(f"[Point Shop] Could not fetch shop message {existing_message_id}: {e}")

                    if existing_message:
                        try:
                            await existing_message.edit(view=layout, attachments=[mosaic_file] if mosaic_file else [])
                            message = existing_message
                        except Exception as e:
                            # e.g. the stored message is a pre-V2 embed that
                            # can't gain the components-v2 flag — replace it.
                            logger.warning(f"[Point Shop] Edit-in-place failed, re-posting: {e}")
                            try:
                                await existing_message.delete()
                            except Exception:
                                pass

                if message is None:
                    if mosaic_image:
                        # A failed edit attempt may have consumed the file stream.
                        mosaic_image.seek(0)
                        mosaic_file = discord.File(mosaic_image, filename="shop_items.png")
                        message = await channel.send(view=layout, file=mosaic_file)
                    else:
                        message = await channel.send(view=layout)

                with engine.begin() as conn:
                    for key, value in (
                        ("shop_message_id", str(message.id)),
                        ("shop_message_channel_id", str(channel.id)),
                    ):
                        conn.execute(
                            text(
                                """
                            INSERT INTO point_settings (key, value, discord_server_id, updated_at)
                            VALUES (:k, :v, :guild_id, CURRENT_TIMESTAMP)
                            ON CONFLICT (key, discord_server_id) DO UPDATE SET value = :v, updated_at = CURRENT_TIMESTAMP
                        """
                            ),
                            {"k": key, "v": value, "guild_id": guild_id},
                        )

                logger.debug(f"[Point Shop] Storefront updated in channel {channel_id} (message {message.id})")
                return True

            except Exception as v2_error:
                logger.warning(f"[Point Shop] Components V2 failed, falling back to legacy: {v2_error}")
                import traceback

                traceback.print_exc()
                # Fall through to legacy mode

        # ==================== Legacy Mode (Embed + Mosaic) ====================
        # Legacy mode re-posts rather than edits; drop the old storefront first.
        if update_existing and existing_message_id:
            try:
                old_msg = await channel.fetch_message(existing_message_id)
                await old_msg.delete()
                logger.debug(f"[Point Shop] Deleted old shop message {existing_message_id}")
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"[Point Shop] Error deleting message {existing_message_id}: {e}")

        if not items:
            embed = discord.Embed(
                title="🛍️ Point Shop", description="No items available at the moment. Check back later!", color=0xFFD700
            )
            embed.set_footer(text="💡 Tip: Earn points by watching streams!")
            message = await channel.send(embed=embed)

            with engine.begin() as conn:
                for key, value in (
                    ("shop_message_id", str(message.id)),
                    ("shop_message_channel_id", str(channel.id)),
                ):
                    conn.execute(
                        text(
                            """
                        INSERT INTO point_settings (key, value, discord_server_id, updated_at)
                        VALUES (:k, :v, :guild_id, CURRENT_TIMESTAMP)
                        ON CONFLICT (key, discord_server_id) DO UPDATE SET value = :v, updated_at = CURRENT_TIMESTAMP
                    """
                        ),
                        {"k": key, "v": value, "guild_id": guild_id},
                    )
            return True

        # Create mosaic image for legacy mode
        mosaic_file = None
        mosaic_image = await create_shop_mosaic_image(items)
        if mosaic_image:
            mosaic_file = discord.File(mosaic_image, filename="shop_items.png")

        embed = discord.Embed(
            title="🛍️ Point Shop",
            description="Spend your hard-earned points on awesome rewards!\nSelect an item from the dropdown below to purchase.",
            color=0xFFD700,
        )

        if mosaic_file:
            embed.set_image(url="attachment://shop_items.png")

        for idx, item in enumerate(items):
            item_id, name, description, price, stock, image_url, is_active, requirement_title, requirement_footer = (
                item[:9]
            )
            stock_text = "∞" if stock < 0 else f"{stock}" if stock > 0 else "SOLD OUT"

            field_value = f"$: **{price:,}** pts | In stock: {stock_text}"
            if description:
                desc_short = description[:50] + "..." if len(description) > 50 else description
                field_value += f"\n_{desc_short}_"

            embed.add_field(name=f"#{idx + 1} {name}", value=field_value, inline=True)

        embed.set_footer(text="💡 Earn points by watching streams! | Check balance: !points")

        view = PointShopView(items)

        if mosaic_file:
            message = await channel.send(embed=embed, file=mosaic_file, view=view)
        else:
            message = await channel.send(embed=embed, view=view)

        # Store the message ID for future updates (legacy mode has interactive in same message)
        with engine.begin() as conn:
            for key, value in (
                ("shop_message_id", str(message.id)),
                ("shop_message_channel_id", str(channel.id)),
            ):
                conn.execute(
                    text(
                        """
                    INSERT INTO point_settings (key, value, discord_server_id, updated_at)
                    VALUES (:k, :v, :guild_id, CURRENT_TIMESTAMP)
                    ON CONFLICT (key, discord_server_id) DO UPDATE SET value = :v, updated_at = CURRENT_TIMESTAMP
                """
                    ),
                    {"k": key, "v": value, "guild_id": guild_id},
                )
            # Clear interactive ID since legacy mode includes it in same message
            conn.execute(
                text(
                    """
                DELETE FROM point_settings WHERE key = 'shop_interactive_id' AND discord_server_id = :guild_id
            """
                ),
                {"guild_id": guild_id},
            )

        logger.debug(f"[Point Shop] Posted shop to channel {channel_id}")
        return True

    except Exception as e:
        logger.warning(f"[Point Shop] Error posting shop: {e}")
        import traceback

        traceback.print_exc()
        return False


async def update_point_shop_message(bot):
    """Update the existing point shop message with current data"""
    return await post_point_shop_to_discord(bot, update_existing=True)


@bot.hybrid_command(name="points", aliases=["balance", "pts"])
async def cmd_points(ctx):
    """Check your point balance"""
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    discord_id = ctx.author.id

    # Resolve the member's shared (cross-platform) balance under their canonical row
    with engine.connect() as conn:
        canonical, accounts = resolve_shop_identity(conn, discord_id, ctx.guild.id)

        if not accounts:
            await ctx.send("❌ You need to link your account first! Use this server's Link Account panel.")
            return

        # Get points balance
        points_data = conn.execute(
            text(
                """
            SELECT points, total_earned, total_spent
            FROM user_points
            WHERE kick_username = :k AND discord_server_id = :s
        """
            ),
            {"k": canonical, "s": ctx.guild.id},
        ).fetchone()

        if not points_data:
            points, total_earned, total_spent = 0, 0, 0
        else:
            points, total_earned, total_spent = points_data

    embed = discord.Embed(title="💰 Your Point Balance", color=0xFFD700)
    embed.add_field(name="Current Balance", value=f"**{points:,}** points", inline=True)
    embed.add_field(name="Total Earned", value=f"{total_earned:,} points", inline=True)
    embed.add_field(name="Total Spent", value=f"{total_spent:,} points", inline=True)
    embed.set_footer(text=f"Account: {format_linked_accounts(accounts)}")

    await ctx.send(embed=embed)


@bot.hybrid_command(name="pointslb", aliases=["pointsleaderboard", "ptslb"])
async def cmd_points_leaderboard(ctx, limit: int = 10):
    """Show the points leaderboard"""
    # Reads the database before its first reply — acknowledge the slash interaction first.
    await defer_slash_response(ctx)

    if limit < 1:
        limit = 10
    if limit > 25:
        limit = 25

    guild_id = ctx.guild.id if ctx.guild else None
    with engine.connect() as conn:
        leaders = conn.execute(
            text(
                """
            SELECT kick_username, points, total_earned
            FROM user_points
            WHERE points > 0 AND discord_server_id = :guild_id
            ORDER BY points DESC
            LIMIT :limit
        """
            ),
            {"limit": limit, "guild_id": guild_id},
        ).fetchall()

    if not leaders:
        await ctx.send("📊 No one has earned any points yet!")
        return

    embed = discord.Embed(title="🏆 Points Leaderboard", color=0xFFD700)

    leaderboard_text = ""
    for i, (username, points, total_earned) in enumerate(leaders, 1):
        rank_emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        leaderboard_text += f"{rank_emoji} **{username}** - {points:,} pts\n"

    embed.description = leaderboard_text
    embed.set_footer(text=f"Top {len(leaders)} point holders")

    await ctx.send(embed=embed)


@bot.hybrid_command(name="postshop")
@app_commands.default_permissions(administrator=True)
@commands.has_permissions(administrator=True)
async def cmd_post_shop(ctx, channel: discord.TextChannel = None):
    """[ADMIN] Post the point shop to a channel"""
    # Writes a setting, then builds and posts the whole storefront message.
    await defer_slash_response(ctx)

    target_channel = channel or ctx.channel

    # Save channel setting
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            INSERT INTO point_settings (key, value, discord_server_id, updated_at)
            VALUES ('shop_channel_id', :c, :guild_id, CURRENT_TIMESTAMP)
            ON CONFLICT (key, discord_server_id) DO UPDATE SET value = :c, updated_at = CURRENT_TIMESTAMP
        """
            ),
            {"c": str(target_channel.id), "guild_id": ctx.guild.id},
        )

    success = await post_point_shop_to_discord(bot, ctx.guild.id, target_channel.id)

    if success:
        # Always confirm. Without an explicit channel the shop lands in the
        # current channel, which used to be treated as confirmation enough — but
        # a slash invocation still needs its own response or Discord reports the
        # command as failed.
        await ctx.send(f"✅ Point shop posted to {target_channel.mention}!")
    else:
        await ctx.send("❌ Failed to post point shop. Check that there are active items.")


# -------------------------
# Run bot
# -------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN environment variable is required")

    logger.debug("✅ Multiserver bot starting")
    logger.debug("   Configure each Discord server in Dashboard → Profile Settings")
    logger.debug("   Set Kick channel name, slot calls channel, raffle settings per-server")
    logger.debug(f"   KICK_USE_KICKPYTHON_WS={KICK_USE_KICKPYTHON_WS}")
    logger.debug("   Bot version: 2025-12-21-v1")  # Trigger redeploy with WebSocket fix

    bot.run(DISCORD_TOKEN)
