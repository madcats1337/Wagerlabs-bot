"""
Bot Settings Manager
Loads configuration from database with fallback to environment variables.
Supports real-time updates via Redis pub/sub.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from utils.secret_settings import decrypt_secret

logger = logging.getLogger(__name__)

# Credentials that must never enter the settings cache. These are either owned by
# the environment (bot_api_key -> CLIPS_API_KEY) or live in a dedicated table
# (kick_oauth_tokens). Mirrors the dashboard's utils/setting_policy.SECRET_SETTINGS.
_NEVER_CACHE_KEYS = frozenset(
    {
        "bot_api_key",
        "howl_api_key",
        "kick_oauth_token",
        "kick_access_token",
        "kick_refresh_token",
        "kick_webhook_secret",
    }
)


class BotSettingsManager:
    """
    Manages bot configuration settings from database.

    Settings are loaded from the bot_settings table with fallback to
    environment variables for backwards compatibility.

    Usage:
        # Single-server mode (backwards compatible)
        settings = BotSettingsManager(engine)

        # Multi-server mode
        settings = BotSettingsManager(engine, guild_id=123456789)

        # Get a setting (with env var fallback)
        channel = settings.get('kick_channel', env_fallback='KICK_CHANNEL')

        # Get as specific type
        channel_id = settings.get_int('slot_calls_channel_id')
        auto_draw = settings.get_bool('raffle_auto_draw')

        # Reload from database
        settings.refresh()
    """

    def __init__(self, database_or_engine: Union[str, Engine], guild_id: Optional[int] = None):
        """
        Initialize settings manager.

        Args:
            database_or_engine: Either a SQLAlchemy Engine or a database URL string
            guild_id: Discord guild/server ID for multi-server support (optional)
        """
        self._cache: Dict[str, str] = {}
        self._last_loaded: Optional[datetime] = None
        self._engine: Optional[Engine] = None
        self._guild_id: Optional[int] = guild_id

        # Accept either an Engine or a connection string
        if isinstance(database_or_engine, Engine):
            self._engine = database_or_engine
        elif database_or_engine:
            # It's a connection string
            database_url = database_or_engine
            if database_url.startswith("postgres://"):
                database_url = database_url.replace("postgres://", "postgresql://", 1)
            self._engine = create_engine(database_url, pool_pre_ping=True)

        # Load settings
        self.refresh()

    def refresh(self, guild_id: Optional[int] = None) -> bool:
        """
        Refresh settings from database.

        Args:
            guild_id: Override the guild_id for this refresh (optional)

        Returns:
            True if successful, False otherwise
        """
        if not self._engine:
            logger.info("[Settings] No database engine, using env vars only")
            return False

        # Use provided guild_id or instance's guild_id
        active_guild_id = guild_id if guild_id is not None else self._guild_id

        try:
            with self._engine.connect() as conn:
                if active_guild_id:
                    # Multi-server mode: Load global settings first, then override with server-specific
                    result = conn.execute(
                        text(
                            """
                        SELECT key, value FROM bot_settings
                        WHERE discord_server_id IS NULL
                        UNION ALL
                        SELECT key, value FROM bot_settings
                        WHERE discord_server_id = :guild_id
                    """
                        ),
                        {"guild_id": active_guild_id},
                    )
                else:
                    # Single-server mode (backwards compatible): Load all settings
                    result = conn.execute(
                        text(
                            """
                        SELECT key, value FROM bot_settings
                        WHERE discord_server_id IS NULL
                    """
                        )
                    )

                rows = result.fetchall()

                # Later rows override earlier ones (server-specific overrides global).
                # Credential keys are dropped on the way in: this cache is long-lived,
                # is dumped by to_dict()/debug paths, and is refreshed from Redis
                # settings-reload events. Keeping secrets out of it means they cannot
                # leak through any of those, and nothing reads them from here any more.
                self._cache = {row[0]: row[1] for row in rows if row[0] not in _NEVER_CACHE_KEYS}
                self._last_loaded = datetime.now(timezone.utc)

                guild_info = f" for guild {active_guild_id}" if active_guild_id else ""
                logger.debug(f"[Settings] Loaded {len(self._cache)} settings from database{guild_info}")
                return True
        except Exception as e:
            logger.warning(f"[Settings] Error loading settings: {e}")
            return False

    # Alias for backwards compatibility
    reload = refresh

    def get_secret(self, key: str, env_fallback: Optional[str] = None) -> str:
        """Read a credential FRESH from the database and decrypt it.

        Deliberately bypasses `_cache` (secrets are excluded from it) and queries on
        every call. Credentials are read at the point of use — right before an API
        call — so a key rotated on the dashboard takes effect without a restart, and
        so the plaintext never sits in a long-lived process cache.

        Returns "" when not configured. RAISES on a decryption failure rather than
        returning "" — an unreadable credential must not be mistaken for an absent
        one, which would silently disable the integration instead of alerting.
        """
        if key not in _NEVER_CACHE_KEYS:
            logger.warning(f"[Settings] get_secret() used for non-secret key '{key}'")

        raw = None
        if self._engine is not None:
            try:
                with self._engine.connect() as conn:
                    if self._guild_id:
                        row = conn.execute(
                            text(
                                """
                                SELECT value FROM bot_settings
                                WHERE key = :key AND discord_server_id = :guild_id
                                """
                            ),
                            {"key": key, "guild_id": self._guild_id},
                        ).fetchone()
                    else:
                        row = conn.execute(
                            text(
                                """
                                SELECT value FROM bot_settings
                                WHERE key = :key AND discord_server_id IS NULL
                                """
                            ),
                            {"key": key},
                        ).fetchone()
                raw = row[0] if row else None
            except Exception as e:
                logger.warning(f"[Settings] Error reading secret '{key}': {e}")
                raw = None

        if not raw:
            return (os.getenv(env_fallback, "") if env_fallback else "") or ""

        return decrypt_secret(raw, key_name=key, row_id=self._guild_id) or ""

    @property
    def guild_id(self) -> Optional[int]:
        """Get the guild ID for this settings manager."""
        return self._guild_id

    def get(self, key: str, default: str = "", env_fallback: Optional[str] = None) -> str:
        """
        Get a setting value.

        Priority:
        1. Database value (if not empty)
        2. Environment variable (if env_fallback specified)
        3. Default value

        Args:
            key: Setting key
            default: Default value if not found
            env_fallback: Environment variable name to check as fallback

        Returns:
            Setting value as string
        """
        # Check database cache
        db_value = self._cache.get(key, "")
        if db_value:
            return db_value

        # Check environment variable
        if env_fallback:
            env_value = os.getenv(env_fallback, "")
            if env_value:
                return env_value

        return default

    def get_int(self, key: str, default: int = 0, env_fallback: Optional[str] = None) -> Optional[int]:
        """
        Get a setting as integer.

        Returns:
            Integer value or None if empty/invalid
        """
        value = self.get(key, "", env_fallback)
        if not value:
            return default if default != 0 else None

        try:
            return int(value)
        except (ValueError, TypeError):
            return default if default != 0 else None

    def get_bool(self, key: str, default: bool = False, env_fallback: Optional[str] = None) -> bool:
        """
        Get a setting as boolean.

        Truthy values: 'true', '1', 'yes', 'on'
        """
        value = self.get(key, "", env_fallback).lower()
        if not value:
            return default
        return value in ("true", "1", "yes", "on")

    def set(self, key: str, value: Any, guild_id: Optional[int] = None) -> bool:
        """
        Update a setting in the database.

        Args:
            key: Setting key
            value: New value (will be converted to string)
            guild_id: Discord guild/server ID (uses instance guild_id if not provided)

        Returns:
            True if successful
        """
        if not self._engine:
            return False

        # Use provided guild_id or instance's guild_id
        active_guild_id = guild_id if guild_id is not None else self._guild_id

        try:
            str_value = str(value) if value is not None else ""

            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                    INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                    VALUES (:key, :value, :guild_id, CURRENT_TIMESTAMP)
                    ON CONFLICT (key, discord_server_id) DO UPDATE
                    SET value = :value, updated_at = CURRENT_TIMESTAMP
                """
                    ),
                    {"key": key, "value": str_value, "guild_id": active_guild_id},
                )

            # Update cache
            self._cache[key] = str_value
            return True
        except Exception as e:
            logger.info(f"[Settings] Error setting {key}: {e}")
            return False

    @property
    def kick_channel(self) -> str:
        """Kick channel name (e.g., 'maikelele')"""
        return self.get("kick_channel", env_fallback="KICK_CHANNEL")

    @property
    def kick_chatroom_id(self) -> Optional[str]:
        """Kick chatroom ID (cached)"""
        return self.get("kick_chatroom_id", env_fallback="KICK_CHATROOM_ID") or None

    @property
    def kick_broadcaster_user_id(self) -> Optional[int]:
        """Kick broadcaster user ID from OAuth"""
        return self.get_int("kick_broadcaster_user_id")

    @property
    def slot_calls_channel_id(self) -> Optional[int]:
        """Discord channel ID for slot calls"""
        return self.get_int("slot_calls_channel_id", env_fallback="SLOT_CALLS_CHANNEL_ID")

    @property
    def raffle_announcement_channel_id(self) -> Optional[int]:
        """Discord channel ID for raffle announcements"""
        return self.get_int("raffle_announcement_channel_id", env_fallback="RAFFLE_ANNOUNCEMENT_CHANNEL_ID")

    @property
    def raffle_leaderboard_channel_id(self) -> Optional[int]:
        """Discord channel ID for raffle leaderboard"""
        return self.get_int("raffle_leaderboard_channel_id", env_fallback="RAFFLE_LEADERBOARD_CHANNEL_ID")

    @property
    def raffle_auto_draw(self) -> bool:
        """Whether to automatically draw raffle at period end"""
        return self.get_bool("raffle_auto_draw", env_fallback="RAFFLE_AUTO_DRAW")

    @property
    def gtb_channel_id(self) -> Optional[int]:
        """Discord channel ID for Guess The Balance"""
        # Falls back to slot_calls_channel_id if not set
        gtb = self.get_int("gtb_channel_id")
        if gtb:
            return gtb
        return self.slot_calls_channel_id

    @property
    def gambling_channel_id(self) -> Optional[int]:
        """Discord channel ID for gambling commands"""
        return self.get_int("gambling_channel_id")

    @property
    def clip_duration(self) -> int:
        """Default clip duration in seconds"""
        return self.get_int("clip_duration", default=30)

    @property
    def dashboard_url(self) -> str:
        """Dashboard API URL for clip service etc."""
        # Check database first, no env fallback since we want DB only
        return self._cache.get("dashboard_url", "") or ""

    @property
    def bot_api_key(self) -> str:
        """DEPRECATED — always empty. Use utils.clip_auth.get_clip_api_key().

        The clip API key is a system secret owned by the environment
        (CLIPS_API_KEY), not per-server settings data. It is no longer read from
        or cached out of bot_settings: that row was writable through the
        dashboard's generic settings endpoints, and caching it here also meant the
        credential rode along in Redis settings-reload payloads and debug dumps.
        Retained as an always-empty property so any missed caller degrades to
        "not configured" rather than raising.
        """
        return ""

    @property
    def shuffle_affiliate_url(self) -> str:
        """Wager/Shuffle affiliate stats API URL - checks wager_affiliate_url first, then shuffle_affiliate_url"""
        # Priority: wager_affiliate_url (DB) -> shuffle_affiliate_url (DB) -> WAGER_AFFILIATE_URL (env) -> SHUFFLE_AFFILIATE_URL (env)
        return (
            self.get("wager_affiliate_url")
            or self.get("shuffle_affiliate_url")
            or self.get("wager_affiliate_url", env_fallback="WAGER_AFFILIATE_URL")
            or self.get("shuffle_affiliate_url", env_fallback="SHUFFLE_AFFILIATE_URL")
            or ""
        )

    @property
    def shuffle_campaign_code(self) -> str:
        """
        Wager/Shuffle campaign/affiliate code(s) to track.
        Supports multiple codes separated by comma (e.g., 'lele,maikelele,lele2')
        """
        # Priority: wager_campaign_code (DB) -> shuffle_campaign_code (DB) -> env vars
        #
        # Returns "" when nothing is configured. There is deliberately NO
        # hardcoded default: this used to end in `or "lele"`, which is ONE
        # tenant's Shuffle code and leaked onto every server that hadn't set
        # one (including Howl servers, which key off howl_campaign_code
        # entirely). Callers must treat "" as "no code configured" and omit it
        # from user-facing copy rather than substituting a placeholder.
        return (
            self.get("wager_campaign_code")
            or self.get("shuffle_campaign_code")
            or self.get("wager_campaign_code", env_fallback="WAGER_CAMPAIGN_CODE")
            or self.get("shuffle_campaign_code", env_fallback="SHUFFLE_CAMPAIGN_CODE")
            or ""
        )

    @property
    def shuffle_verified_role_id(self) -> Optional[int]:
        """Discord role ID granted when a user verifies their Shuffle affiliate account."""
        return self.get_int("shuffle_verified_role_id")

    @property
    def shuffle_tickets_per_1000(self) -> int:
        """Tickets to award per $1000 wagered"""
        # Priority: wager_tickets_per_1000 (DB) -> shuffle_tickets_per_1000 (DB) -> env vars
        val = self.get_int("wager_tickets_per_1000")
        if val:
            return val
        val = self.get_int("shuffle_tickets_per_1000")
        if val:
            return val
        val = self.get_int("wager_tickets_per_1000", env_fallback="WAGER_TICKETS_PER_1000_USD")
        if val:
            return val
        val = self.get_int("shuffle_tickets_per_1000", env_fallback="SHUFFLE_TICKETS_PER_1000_USD")
        return val if val else 20

    def to_dict(self) -> Dict[str, Any]:
        """Get all settings as a dictionary"""
        return {
            "kick_channel": self.kick_channel,
            "kick_chatroom_id": self.kick_chatroom_id,
            "kick_broadcaster_user_id": self.kick_broadcaster_user_id,
            "slot_calls_channel_id": self.slot_calls_channel_id,
            "raffle_announcement_channel_id": self.raffle_announcement_channel_id,
            "raffle_leaderboard_channel_id": self.raffle_leaderboard_channel_id,
            "raffle_auto_draw": self.raffle_auto_draw,
            "gtb_channel_id": self.gtb_channel_id,
            "clip_duration": self.clip_duration,
            "dashboard_url": self.dashboard_url,
            # bot_api_key intentionally absent — it is no longer a settings value
            # (see the property docstring). Omitted rather than masked so this dict
            # can't even advertise whether a credential is present.
            "wager_affiliate_url": "***configured***" if self.shuffle_affiliate_url else "",  # Don't expose full URL
            "wager_campaign_code": self.shuffle_campaign_code,
            "wager_tickets_per_1000": self.shuffle_tickets_per_1000,
        }


# Global settings instance (initialized in bot.py)
_settings: Optional[BotSettingsManager] = None


def get_settings() -> Optional[BotSettingsManager]:
    """Get the global settings manager instance"""
    return _settings


def init_settings(database_url: str) -> BotSettingsManager:
    """Initialize the global settings manager"""
    global _settings
    _settings = BotSettingsManager(database_url)
    return _settings
