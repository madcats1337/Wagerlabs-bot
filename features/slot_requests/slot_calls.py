"""
Slot Call Tracker - Monitor Kick chat for !call commands and post to Discord
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

import discord
from discord.ext import commands
from sqlalchemy import text

from features.discord_app_commands import defer_slash_response

logger = logging.getLogger(__name__)


class SlotCallTracker:
    """Track slot calls from Kick chat and post to Discord"""

    def __init__(
        self,
        bot,
        discord_channel_id: Optional[int] = None,
        kick_send_callback=None,
        engine=None,
        server_id: Optional[int] = None,
    ):
        self.bot = bot
        self.discord_channel_id = discord_channel_id
        self.engine = engine
        self.server_id = server_id  # Discord server ID for multi-server support
        self.last_call_time: Dict[str, datetime] = {}  # Track per-user cooldown
        self.cooldown_seconds = 30  # Default 30 second cooldown per user (loaded from DB)
        self.max_username_length = 50  # Maximum username length
        self.max_slot_call_length = 200  # Maximum slot call text length
        self.kick_send_callback = kick_send_callback  # Callback to send messages to Kick chat
        self.panel = None  # Reference to SlotRequestPanel (set externally)

        # Lazily-built, guild-aware settings manager for platform lookups.
        self._settings = None

        # Initialize database and load enabled state
        self._init_database()
        self.enabled = self._load_enabled_state()
        self.max_requests_per_user = self._load_max_requests()
        self.cooldown_seconds = self._load_cooldown()  # Load from database

    def _get_platform(self) -> str:
        """Resolve this server's selected WAGER platform → "shuffle" | "howl".

        Mirrors ShuffleTracker._load_settings: reads bot_settings
        `wager_platform_name` (guild-aware), refreshed each call so a platform
        switch from the dashboard takes effect without a bot restart. Defaults
        to "shuffle" when unset/invalid. Used for chat-reply toggles.
        """
        if not self.engine:
            return "shuffle"
        try:
            from utils.bot_settings import BotSettingsManager

            if self._settings is None:
                self._settings = BotSettingsManager(self.engine, guild_id=self.server_id)
            self._settings.refresh()
            platform = (self._settings.get("wager_platform_name") or "shuffle").strip().lower()
            return platform if platform in ("shuffle", "howl") else "shuffle"
        except Exception as e:
            logger.warning(f"SlotCallTracker._get_platform: defaulting to shuffle ({e})")
            return "shuffle"

    def _get_slot_platform(self) -> str:
        """Resolve this server's selected SLOT CATALOG platform.

        Reads bot_settings `slot_platform_name` (any registry casino), falling
        back to the wager platform when unset — mirrors the dashboard's
        get_slot_platform. Picks which catalog table the ban check queries.
        """
        if not self.engine:
            return "shuffle"
        try:
            from utils.bot_settings import BotSettingsManager
            from utils.slot_platforms import SLOT_PLATFORMS

            if self._settings is None:
                self._settings = BotSettingsManager(self.engine, guild_id=self.server_id)
            self._settings.refresh()
            platform = (self._settings.get("slot_platform_name") or "").strip().lower()
            if platform in SLOT_PLATFORMS:
                return platform
        except Exception as e:
            logger.warning(f"SlotCallTracker._get_slot_platform: falling back to wager platform ({e})")
        return self._get_platform()

    def _chat_replies_enabled(self, platform: str) -> bool:
        """Whether the chatbot should post !call/!sr replies into `platform`'s
        chat. Toggled per-platform from the dashboard via bot_settings
        `slot_reply_kick_enabled` / `slot_reply_twitch_enabled` (default on).
        Only suppresses the in-chat reply — the request is still recorded and
        still posted to the Discord channel. Refreshed each call so a dashboard
        toggle takes effect without a bot restart.
        """
        if not self.engine:
            return True
        key = "slot_reply_twitch_enabled" if platform == "twitch" else "slot_reply_kick_enabled"
        try:
            from utils.bot_settings import BotSettingsManager

            if self._settings is None:
                self._settings = BotSettingsManager(self.engine, guild_id=self.server_id)
            self._settings.refresh()
            return self._settings.get_bool(key, default=True)
        except Exception as e:
            logger.warning(f"SlotCallTracker._chat_replies_enabled({key}): defaulting to on ({e})")
            return True

    def _init_database(self):
        """Create feature_settings and slot_requests tables if they don't exist"""
        if not self.engine:
            logger.warning("No database engine provided - slot call state won't persist")
            return

        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                    CREATE TABLE IF NOT EXISTS feature_settings (
                        feature_name TEXT NOT NULL,
                        discord_server_id BIGINT NOT NULL,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (feature_name, discord_server_id)
                    )
                """
                    )
                )

                # Create slot_requests table to store all slot requests
                conn.execute(
                    text(
                        """
                    CREATE TABLE IF NOT EXISTS slot_requests (
                        id SERIAL PRIMARY KEY,
                        kick_username TEXT NOT NULL,
                        slot_call TEXT NOT NULL,
                        requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        picked BOOLEAN DEFAULT FALSE,
                        picked_at TIMESTAMP,
                        discord_server_id BIGINT
                    )
                """
                    )
                )

                # Add discord_server_id column if it doesn't exist (migration)
                try:
                    conn.execute(
                        text(
                            """
                        ALTER TABLE slot_requests ADD COLUMN IF NOT EXISTS discord_server_id BIGINT
                    """
                        )
                    )
                except Exception:
                    pass  # Column might already exist

                # Native display name (Twitch name on Twitch, Kick name on Kick) shown
                # in embeds. kick_username stays the canonical credit/dedup key.
                try:
                    conn.execute(text("ALTER TABLE slot_requests ADD COLUMN IF NOT EXISTS display_name TEXT"))
                except Exception:
                    pass

                # Add performance index for requested_at ordering
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_slot_requests_requested_at
                    ON slot_requests(requested_at DESC)
                """
                    )
                )

                # Append-only request log — persists past queue picks/clears so
                # the dashboard can count all-time requests + trends. Created
                # here too (idempotent) so the bot never fails to log if it
                # starts before the dashboard's migration runs. Mirrors the
                # dashboard's run_migrations() definition.
                conn.execute(
                    text(
                        """
                    CREATE TABLE IF NOT EXISTS slot_request_log (
                        id SERIAL PRIMARY KEY,
                        discord_server_id BIGINT NOT NULL,
                        slot_call TEXT NOT NULL,
                        kick_username TEXT,
                        requested_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """
                    )
                )
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_slot_request_log_server_norm
                    ON slot_request_log(discord_server_id, LOWER(regexp_replace(slot_call, '[^a-zA-Z0-9]', '', 'g')))
                """
                    )
                )
            logger.debug("Slot call tables initialized")
        except Exception as e:
            logger.error(f"Failed to initialize slot call tables: {e}")

    def _load_enabled_state(self) -> bool:
        """Load enabled state from database, default to True if not found"""
        if not self.engine:
            return True  # Default to enabled if no database

        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'slot_requests_enabled' AND discord_server_id = :server_id
                """
                    ),
                    {"server_id": self.server_id},
                ).fetchone()

                if result:
                    enabled = result[0].lower() == "true"
                    logger.debug(f"Loaded slot call state from database: {'enabled' if enabled else 'disabled'}")
                    return enabled
                else:
                    # First time - set default to enabled
                    with self.engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                            INSERT INTO bot_settings (key, value, discord_server_id)
                            VALUES ('slot_requests_enabled', 'true', :server_id)
                            ON CONFLICT (key, discord_server_id) DO NOTHING
                        """
                            ),
                            {"server_id": self.server_id},
                        )
                    logger.info("Initialized slot call state in database: enabled")
                    return True
        except Exception as e:
            logger.error(f"Failed to load slot call state from database: {e}")
            return True  # Default to enabled on error

    def _load_max_requests(self) -> int:
        """Load max requests per user from database, default to 0 (unlimited)"""
        if not self.engine:
            return 0  # 0 = unlimited

        try:
            with self.engine.connect() as conn:
                # Try to load server-specific setting first
                if self.server_id:
                    result = conn.execute(
                        text(
                            """
                        SELECT value FROM bot_settings
                        WHERE key = 'slot_max_requests_per_user'
                        AND discord_server_id = :server_id
                    """
                        ),
                        {"server_id": self.server_id},
                    ).fetchone()

                    if result:
                        max_requests = int(result[0])
                        logger.debug(
                            f"Loaded max slot requests per user (server {self.server_id}): {max_requests if max_requests > 0 else 'unlimited'}"
                        )
                        return max_requests

                # Fallback to global setting if no server-specific setting
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'slot_max_requests_per_user'
                    AND discord_server_id IS NULL
                """
                    )
                ).fetchone()

                if result:
                    max_requests = int(result[0])
                    logger.debug(
                        f"Loaded max slot requests per user (global): {max_requests if max_requests > 0 else 'unlimited'}"
                    )
                    return max_requests
                else:
                    return 0  # Default to unlimited
        except Exception as e:
            logger.error(f"Failed to load max requests setting: {e}")
            return 0  # Default to unlimited on error

    def _load_cooldown(self) -> int:
        """Load cooldown seconds from database, default to 30"""
        if not self.engine:
            return 30  # Default 30 second cooldown

        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'slot_request_cooldown_seconds'
                      AND (discord_server_id = :server_id OR discord_server_id IS NULL)
                    ORDER BY discord_server_id NULLS LAST
                    LIMIT 1
                """
                    ),
                    {"server_id": self.bot.server_id if hasattr(self.bot, "server_id") else None},
                ).fetchone()

                if result and result[0]:
                    cooldown = int(result[0])
                    logger.info(f"Loaded slot request cooldown: {cooldown} seconds")
                    return cooldown if cooldown > 0 else 30  # Min 30 seconds
                else:
                    return 30  # Default to 30 seconds
        except Exception as e:
            logger.error(f"Failed to load cooldown setting: {e}")
            return 30  # Default to 30 seconds on error

    def set_max_requests(self, max_requests: int) -> bool:
        """Set maximum requests per user (0 = unlimited)"""
        if not self.engine:
            return False

        try:
            with self.engine.begin() as conn:
                if self.server_id:
                    # Server-specific setting
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id)
                        VALUES ('slot_max_requests_per_user', :max_requests, :server_id)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = :max_requests
                    """
                        ),
                        {"max_requests": str(max_requests), "server_id": self.server_id},
                    )
                else:
                    # Global setting
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id)
                        VALUES ('slot_max_requests_per_user', :max_requests, NULL)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = :max_requests
                    """
                        ),
                        {"max_requests": str(max_requests)},
                    )

            self.max_requests_per_user = max_requests
            logger.info(
                f"Set max slot requests per user to: {max_requests if max_requests > 0 else 'unlimited'}"
                + (f" (server {self.server_id})" if self.server_id else " (global)")
            )
            return True
        except Exception as e:
            logger.error(f"Failed to set max requests: {e}")
            return False

    def is_enabled(self) -> bool:
        """Check if slot call tracking is enabled"""
        return self.enabled

    async def set_enabled(self, enabled: bool):
        """Enable or disable slot call tracking and persist to database"""
        was_disabled = not self.enabled
        self.enabled = enabled
        logger.info(f"Slot call tracking {'enabled' if enabled else 'disabled'}")

        # Persist to database
        if self.engine:
            try:
                with self.engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id)
                        VALUES ('slot_requests_enabled', :value, :server_id)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                    """
                        ),
                        {"value": str(enabled).lower(), "server_id": self.server_id},
                    )

                    # Clear slot requests table when enabled after being disabled (only for this server!)
                    if enabled and was_disabled:
                        result = conn.execute(
                            text("DELETE FROM slot_requests WHERE discord_server_id = :server_id"),
                            {"server_id": self.server_id},
                        )
                        deleted_count = result.rowcount
                        logger.info(
                            f"Cleared {deleted_count} old slot requests for server {self.server_id} (slot requests re-enabled)"
                        )

                logger.info(f"Persisted slot call state to database")
            except Exception as e:
                logger.error(f"Failed to persist slot call state to database: {e}")

        # Update panel if available
        if self.panel:
            try:
                await self.panel.update_panel()
                logger.info("Updated slot request panel after status change")
            except Exception as e:
                logger.error(f"Failed to update panel: {e}")

    async def handle_slot_call(
        self,
        kick_username: str,
        slot_call: str,
        avatar_url: Optional[str] = None,
        platform: str = "kick",
        display_username: Optional[str] = None,
    ):
        """
        Handle a slot call from chat.

        Args:
            kick_username: Canonical identity username (used for DB writes/dedup).
            slot_call: The slot call text (everything after !call)
            avatar_url: Avatar URL (if available)
            platform: Which chat the command came from ('kick' | 'twitch'). The
                confirmation REPLY is sent back to ONLY this platform (a reply, not
                a broadcast), so a Twitch !call doesn't also post in Kick chat.
            display_username: The chatter's REAL name on their platform, used for the
                @-mention in replies (e.g. @MadcatsTV). Defaults to kick_username.
        """
        # Name shown in @-mentions; identity/crediting still uses kick_username.
        mention_name = display_username or kick_username

        async def _reply(msg: str):
            """Send a command reply to only the originating platform.

            Honors the per-platform reply toggle: when the originating
            platform's chat replies are disabled from the dashboard, the
            request is still recorded + posted to Discord, but no message is
            sent into that platform's chat.
            """
            if not self._chat_replies_enabled(platform):
                logger.debug(f"[Slot Call] {platform} chat replies disabled — suppressing reply")
                return
            try:
                import sys as _sys

                _main = _sys.modules.get("__main__")
                if platform == "twitch":
                    sender = getattr(_main, "send_twitch_message", None)
                else:
                    sender = getattr(_main, "_send_kick_message_raw", None)
                if sender is not None:
                    await sender(msg, guild_id=self.discord_server_id)
                elif self.kick_send_callback:
                    # Fallback (shouldn't normally happen).
                    await self.kick_send_callback(msg, guild_id=self.discord_server_id)
            except Exception as e:
                logger.error(f"[Slot Call] Failed to send {platform} reply: {e}")

        # 🔒 SECURITY: Check if user is blacklisted (defensive check)
        if self.engine:
            try:
                username_lower = kick_username.lower()
                with self.engine.connect() as conn:
                    blacklist_check = conn.execute(
                        text(
                            """
                        SELECT 1 FROM slot_call_blacklist
                        WHERE kick_username = :username AND discord_server_id = :guild_id
                    """
                        ),
                        {"username": username_lower, "guild_id": self.discord_server_id},
                    ).fetchone()
                    if blacklist_check:
                        logger.info(f"[Slot Call] Blocked blacklisted user in handle_slot_call: {kick_username}")
                        return  # Silently block - no response to blacklisted users
            except Exception as e:
                logger.error(f"Error checking slot call blacklist: {e}")

        if not self.enabled:
            await _reply(f"@{mention_name} Slot requests are not open at the moment.")
            return

        # Note: a missing Discord channel must NOT abort the request. The Kick/Twitch
        # confirmation reply and the DB record are what the streamer relies on; the
        # Discord embed is a best-effort mirror. Posting is skipped gracefully below
        # when no channel is configured (see the embed/post block).

        # 🔒 SECURITY: Rate limiting - prevent spam
        now = datetime.utcnow()
        username_lower = kick_username.lower()

        if username_lower in self.last_call_time:
            time_since_last = (now - self.last_call_time[username_lower]).total_seconds()
            if time_since_last < self.cooldown_seconds:
                logger.debug(f"Rate limit: {kick_username} must wait {self.cooldown_seconds - time_since_last:.1f}s")
                return  # Silently ignore - don't spam the user

        # Update last call time
        self.last_call_time[username_lower] = now

        # Check per-user request limit (if enabled)
        # This counts ALL requests (picked, unpicked, added to hunt - everything)
        # Using SELECT FOR UPDATE to prevent race conditions
        logger.info(
            f"[LIMIT CHECK] User: {kick_username}, max_requests_per_user: {self.max_requests_per_user}, engine: {bool(self.engine)}"
        )
        if self.max_requests_per_user > 0 and self.engine:
            try:
                with self.engine.begin() as conn:
                    # Count the user's total requests
                    if self.server_id:
                        result = conn.execute(
                            text(
                                """
                            SELECT COUNT(*) FROM slot_requests
                            WHERE LOWER(kick_username) = LOWER(:username)
                            AND discord_server_id = :server_id
                        """
                            ),
                            {"username": kick_username, "server_id": self.server_id},
                        ).fetchone()
                    else:
                        result = conn.execute(
                            text(
                                """
                            SELECT COUNT(*) FROM slot_requests
                            WHERE LOWER(kick_username) = LOWER(:username)
                        """
                            ),
                            {"username": kick_username},
                        ).fetchone()

                    total_requests = result[0] if result else 0
                    logger.info(
                        f"[LIMIT CHECK] User {kick_username} has {total_requests} total requests, limit is {self.max_requests_per_user}"
                    )

                    if total_requests >= self.max_requests_per_user:
                        # User has reached their request limit
                        logger.warning(
                            f"[LIMIT CHECK] BLOCKING {kick_username}: {total_requests} >= {self.max_requests_per_user}"
                        )
                        await _reply(
                            f"@{mention_name} You have reached the maximum of {self.max_requests_per_user} slot request(s). "
                            f"Please wait for the current hunt to complete."
                        )
                        logger.info(
                            f"User {kick_username} blocked: {total_requests}/{self.max_requests_per_user} total requests"
                        )
                        return
                    else:
                        logger.info(
                            f"[LIMIT CHECK] ALLOWING {kick_username}: {total_requests} < {self.max_requests_per_user}"
                        )
            except Exception as e:
                logger.error(f"Failed to check user request limit: {e}")
                # Continue anyway to not block legitimate requests on DB errors
        else:
            logger.info(
                f"[LIMIT CHECK] SKIPPED - max_requests_per_user is {self.max_requests_per_user} (must be > 0) or engine is None"
            )

        # 🔒 SECURITY: Input validation - prevent excessively long inputs
        kick_username_safe = kick_username[: self.max_username_length]
        mention_name_safe = mention_name[: self.max_username_length]
        slot_call_safe = slot_call[: self.max_slot_call_length]

        # Check if slot is banned or provider is disabled (supports name or slug match, basic slug normalization)
        if self.engine:
            try:
                # Normalize to a slug candidate (lowercase, non-alphanumeric -> '-')
                slot_slug_candidate = re.sub(r"[^a-z0-9]+", "-", slot_call_safe.lower()).strip("-")
                # Strict: check only the server's selected slot catalog, and
                # only this server's rows. `slot_table` is a validated, closed-set
                # name (safe to interpolate); all values stay bound parameters.
                from utils.slot_platforms import slot_table_for_platform

                slot_table = slot_table_for_platform(self._get_slot_platform())
                with self.engine.connect() as conn:
                    slot_check = conn.execute(
                        text(
                            f"""
                        SELECT banned, is_active
                        FROM {slot_table}
                        WHERE (
                                LOWER(name) = LOWER(:slot_name)
                             OR LOWER(slug) = LOWER(:slot_slug)
                             OR LOWER(REPLACE(name,' ','-')) = LOWER(:slot_slug)
                          )
                          AND discord_server_id = :server_id
                        LIMIT 1
                    """
                        ),
                        {
                            "slot_name": slot_call_safe,
                            "slot_slug": slot_slug_candidate,
                            "server_id": self.server_id,
                        },
                    ).fetchone()

                if slot_check:
                    is_banned, is_active = slot_check

                    # Block if slot is banned
                    if is_banned:
                        await _reply(f"@{mention_name_safe} Sorry, {slot_call_safe} is currently banned.")
                        logger.info(f"Blocked banned slot request: {slot_call_safe}")
                        return

                    # Block if provider is disabled (inactive)
                    if not is_active:
                        await _reply(f"@{mention_name_safe} Sorry, {slot_call_safe} is currently unavailable.")
                        logger.info(f"Blocked slot request for disabled provider: {slot_call_safe}")
                        return
            except Exception as e:
                logger.error(f"Failed to check slot availability: {e}")
                # Continue anyway to not block legitimate requests on DB errors

        # Check for duplicate slot requests (if enabled)
        if self.engine:
            try:
                with self.engine.connect() as conn:
                    # Check if duplicate prevention is enabled
                    prevent_result = conn.execute(
                        text(
                            """
                        SELECT value FROM bot_settings
                        WHERE key = 'slot_prevent_duplicates'
                    """
                        )
                    ).fetchone()

                    prevent_duplicates = prevent_result and prevent_result[0] == "true"

                    if prevent_duplicates:
                        # Check if this slot has already been requested (unpicked OR added to hunt)
                        if self.server_id:
                            dup_result = conn.execute(
                                text(
                                    """
                                SELECT COUNT(*) FROM slot_requests
                                WHERE LOWER(slot_call) = LOWER(:slot_call)
                                AND (picked = FALSE OR added_to_hunt = TRUE)
                                AND discord_server_id = :server_id
                            """
                                ),
                                {"slot_call": slot_call_safe, "server_id": self.server_id},
                            ).fetchone()
                        else:
                            dup_result = conn.execute(
                                text(
                                    """
                                SELECT COUNT(*) FROM slot_requests
                                WHERE LOWER(slot_call) = LOWER(:slot_call)
                                AND (picked = FALSE OR added_to_hunt = TRUE)
                            """
                                ),
                                {"slot_call": slot_call_safe},
                            ).fetchone()

                        if dup_result and dup_result[0] > 0:
                            # Slot already requested or in active hunt
                            await _reply(f"@{mention_name_safe} Sorry, {slot_call_safe} has already been requested.")
                            logger.info(f"Blocked duplicate slot request: {slot_call_safe} by {kick_username_safe}")
                            return
            except Exception as e:
                logger.error(f"Failed to check for duplicate slot requests: {e}")
                # Continue anyway to not block legitimate requests on DB errors

        # Use avatar from websocket if provided, otherwise fetch from Kick API
        if not avatar_url:
            try:
                import requests

                # Try the users endpoint first (same as OAuth flow)
                kick_response = requests.get(f"https://kick.com/api/v2/users/{kick_username_safe}", timeout=3)
                if kick_response.status_code == 200:
                    kick_data = kick_response.json()
                    avatar_url = kick_data.get("profile_picture")
                    logger.info(f"✅ Found Kick avatar via /users for {kick_username_safe}: {avatar_url}")
                else:
                    # Fallback to channels endpoint
                    kick_response = requests.get(f"https://kick.com/api/v2/channels/{kick_username_safe}", timeout=3)
                    if kick_response.status_code == 200:
                        kick_data = kick_response.json()
                        # Try multiple possible avatar fields from Kick API
                        user_data = kick_data.get("user", {})
                        avatar_url = (
                            user_data.get("profile_picture")
                            or user_data.get("profile_pic")
                            or user_data.get("profilepic")
                            or user_data.get("avatar")
                        )

                        # Log the API response structure for debugging
                        logger.info(f"Kick API response for {kick_username_safe}: user keys = {list(user_data.keys())}")
                        if avatar_url:
                            logger.info(f"✅ Found Kick avatar via /channels for {kick_username_safe}: {avatar_url}")
                        else:
                            logger.warning(f"❌ No avatar found in Kick API for {kick_username_safe}")
                    else:
                        logger.warning(f"Kick API returned {kick_response.status_code} for {kick_username_safe}")
            except Exception as avatar_err:
                logger.error(f"Failed to fetch Kick avatar for {kick_username_safe}: {avatar_err}")
        else:
            logger.info(f"✅ Using Kick avatar from websocket for {kick_username_safe}: {avatar_url}")

        # Fallback to placeholder if no avatar
        if not avatar_url:
            avatar_url = f"https://ui-avatars.com/api/?name={kick_username_safe}&background=random&size=95"

        # Post to Discord — BEST EFFORT ONLY. A missing/unresolvable channel must
        # not stop the request from being recorded or confirmed in chat. We only
        # attempt the embed when a channel is configured; any failure is logged
        # and swallowed so the DB save + chat reply below always run.
        if self.discord_channel_id:
            channel = self.bot.get_channel(self.discord_channel_id)
            if channel:
                # Show the viewer's NATIVE platform name in the embed.
                platform_label = "Twitch" if platform == "twitch" else "Kick"
                embed = discord.Embed(
                    title="🎰 Slot Call",
                    description=f"**{mention_name_safe}** requested **{slot_call_safe}**",
                    color=discord.Color.gold(),
                    timestamp=datetime.utcnow(),
                )
                embed.set_footer(text=f"From {platform_label} chat • {mention_name_safe}")
                try:
                    await channel.send(embed=embed)
                    logger.info(f"Posted slot call from {kick_username_safe}: {slot_call_safe}")
                except Exception as e:
                    logger.error(f"Failed to post slot call to Discord (request still recorded): {e}")
            else:
                logger.warning(
                    f"Could not find Discord channel {self.discord_channel_id} "
                    f"(request still recorded; check the configured channel)"
                )
        else:
            logger.info(
                "No Discord channel configured for slot requests — skipping Discord post (request still recorded)"
            )

        # Save slot request to database with avatar — ALWAYS, independent of Discord.
        if self.engine:
            try:
                with self.engine.begin() as conn:
                    if self.server_id:
                        conn.execute(
                            text(
                                """
                            INSERT INTO slot_requests (kick_username, slot_call, requested_at, discord_server_id, avatar_url, display_name)
                            VALUES (:username, :slot_call, CURRENT_TIMESTAMP, :server_id, :avatar_url, :display_name)
                        """
                            ),
                            {
                                "username": kick_username_safe,
                                "slot_call": slot_call_safe,
                                "server_id": self.server_id,
                                "avatar_url": avatar_url,
                                "display_name": mention_name_safe,
                            },
                        )
                    else:
                        conn.execute(
                            text(
                                """
                            INSERT INTO slot_requests (kick_username, slot_call, requested_at, avatar_url, display_name)
                            VALUES (:username, :slot_call, CURRENT_TIMESTAMP, :avatar_url, :display_name)
                        """
                            ),
                            {
                                "username": kick_username_safe,
                                "slot_call": slot_call_safe,
                                "avatar_url": avatar_url,
                                "display_name": mention_name_safe,
                            },
                        )

                    # Append-only request log (survives queue picks/clears) so
                    # the dashboard can show all-time "requested N times" +
                    # trends. Server-scoped only (the log's discord_server_id is
                    # NOT NULL); the no-server legacy path above skips it.
                    if self.server_id:
                        conn.execute(
                            text(
                                """
                            INSERT INTO slot_request_log (discord_server_id, slot_call, kick_username)
                            VALUES (:server_id, :slot_call, :username)
                        """
                            ),
                            {
                                "server_id": self.server_id,
                                "slot_call": slot_call_safe,
                                "username": kick_username_safe,
                            },
                        )
                logger.debug(f"Saved slot request to database with avatar")

                # Publish event for real-time dashboard updates
                try:
                    from bot import publish_redis_event

                    publish_redis_event(
                        "dashboard:slot_requests",
                        "new_request",
                        {
                            "username": kick_username_safe,
                            "slot_call": slot_call_safe,
                            "discord_server_id": self.server_id,
                        },
                    )
                except Exception as redis_err:
                    logger.debug(f"Redis publish failed (non-critical): {redis_err}")

                # Tournament auto-match: if this chatter is a live competitor in
                # the server's active tournament and has a current undecided
                # match, fill the slot they just !call'd into their match side so
                # the streamer doesn't have to type it. Gated to TIER2+ servers.
                try:
                    self._tournament_autofill_slot(kick_username_safe, slot_call_safe)
                except Exception as tourney_err:
                    logger.debug(f"Tournament auto-match skipped (non-critical): {tourney_err}")
            except Exception as e:
                logger.error(f"Failed to save slot request to database: {e}")

        # Send confirmation reply to ONLY the originating platform (not a broadcast).
        await _reply(f"@{mention_name_safe} Your slot request for {slot_call_safe} has been received! ✅")
        logger.info(f"Sent slot confirmation to {kick_username_safe} on {platform}")

        # Update panel if available
        if self.panel:
            try:
                await self.panel.update_panel()
                logger.debug("Updated slot request panel after new request")
            except Exception as panel_error:
                logger.error(f"Failed to update panel: {panel_error}")

    def _tournament_autofill_slot(self, kick_username: str, slot_call: str) -> None:
        """Fill a !call slot into the chatter's CURRENT tournament match side, if
        they're a live competitor in the server's active tournament.

        Mirrors utils.tournament.set_competitor_active_slot_by_username on the
        dashboard side, but runs against the bot's SQLAlchemy engine so it works
        without a dashboard round-trip. No-op when: no engine/server, the server
        isn't TIER2+, there's no live tournament, the username doesn't match a
        non-eliminated competitor, or they have no current undecided match.

        On a successful fill it publishes a `match_refresh` widget event so the
        tournament overlay re-reads the current match.
        """
        if not self.engine or not self.server_id or not slot_call.strip():
            return

        # Gate to servers whose tier grants the tournament feature.
        try:
            from utils.subscription_tier import server_has_feature

            if not server_has_feature(self.engine, self.server_id, "tournament"):
                return
        except Exception as e:
            logger.debug(f"tournament feature check failed: {e}")
            return

        matched = False
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT id FROM tournaments
                    WHERE discord_server_id = :sid AND status IN ('drafting','active')
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"sid": self.server_id},
            ).fetchone()
            if not row:
                return
            tid = row[0]

            comp = conn.execute(
                text(
                    """
                    SELECT id FROM tournament_competitors
                    WHERE tournament_id = :tid AND eliminated = FALSE
                      AND LOWER(kick_username) = LOWER(:ku)
                    LIMIT 1
                    """
                ),
                {"tid": tid, "ku": kick_username},
            ).fetchone()
            if not comp:
                return
            competitor_id = comp[0]

            cm = conn.execute(
                text(
                    """
                    SELECT id, competitor1_id FROM tournament_matches
                    WHERE tournament_id = :tid AND status != 'decided'
                      AND (competitor1_id = :cid OR competitor2_id = :cid)
                    ORDER BY round ASC, match_number ASC
                    LIMIT 1
                    """
                ),
                {"tid": tid, "cid": competitor_id},
            ).fetchone()
            if not cm:
                return
            match_id, c1_id = cm[0], cm[1]
            prefix = "c1" if c1_id == competitor_id else "c2"

            conn.execute(
                text(f"UPDATE tournament_matches SET {prefix}_slot_name = :slot WHERE id = :mid"),
                {"slot": slot_call.strip(), "mid": match_id},
            )
            conn.execute(
                text("UPDATE tournament_competitors SET requested_slot = :slot WHERE id = :cid"),
                {"slot": slot_call.strip(), "cid": competitor_id},
            )
            matched = True

        if matched:
            logger.info(f"[Tournament] Auto-filled '{slot_call}' for competitor {kick_username}")
            try:
                import json as _json

                import bot as _bot

                # Publish a widget refresh on the tournament events channel. This
                # channel uses the {type, server_id, payload} shape consumed by
                # the OBS overlay's SSE stream (NOT publish_redis_event's
                # {action,data} dashboard shape), so publish the raw message.
                if getattr(_bot, "redis_client", None):
                    _bot.redis_client.publish(
                        "tournament:match:events",
                        _json.dumps(
                            {
                                "type": "match_refresh",
                                "server_id": str(self.server_id),
                                "payload": {},
                            }
                        ),
                    )
            except Exception as redis_err:
                logger.debug(f"tournament widget refresh publish failed: {redis_err}")


class SlotCallCommands(commands.Cog):
    """Discord commands for managing slot call tracking"""

    def __init__(self, bot, tracker: SlotCallTracker = None):
        self.bot = bot
        self.default_tracker = tracker  # Fallback for backwards compatibility

    def _get_tracker_for_guild(self, guild_id: int) -> Optional[SlotCallTracker]:
        """Get the correct tracker for a guild"""
        # First try per-guild trackers
        if hasattr(self.bot, "slot_call_trackers_by_guild"):
            tracker = self.bot.slot_call_trackers_by_guild.get(guild_id)
            if tracker:
                return tracker
        # Fallback to default tracker
        return self.default_tracker

    @commands.hybrid_command(name="slotcalls")
    @commands.has_permissions(administrator=True)
    async def toggle_slot_calls(self, ctx, action: str = None):
        """
        [ADMIN] Toggle slot call tracking on/off or check status
        Usage: /slotcalls [on|off|status]
        """
        tracker = self._get_tracker_for_guild(ctx.guild.id)
        if not tracker:
            await ctx.send("❌ Slot call tracker not initialized for this server")
            return

        if action is None or action.lower() == "status":
            status = "✅ **enabled**" if tracker.is_enabled() else "❌ **disabled**"
            channel_id = tracker.discord_channel_id
            channel_mention = f"<#{channel_id}>" if channel_id else "Not configured"

            embed = discord.Embed(
                title="🎰 Slot Call Tracker Status",
                color=discord.Color.green() if tracker.is_enabled() else discord.Color.red(),
            )
            embed.add_field(name="Status", value=status, inline=True)
            embed.add_field(name="Channel", value=channel_mention, inline=True)
            embed.add_field(
                name="🔒 Security",
                value=f"• Rate limit: {tracker.cooldown_seconds}s cooldown per user\n"
                f"• Max username: {tracker.max_username_length} chars\n"
                f"• Max slot call: {tracker.max_slot_call_length} chars",
                inline=False,
            )
            embed.add_field(
                name="How it works",
                value="When users type `!call <slot>` or `!sr <slot>` in Kick chat, it posts to the configured Discord channel.",
                inline=False,
            )

            await ctx.send(embed=embed)
            return

        if action.lower() == "on":
            await tracker.set_enabled(True)
            await ctx.send(
                "✅ Slot call tracking **enabled**! Users can now use `!call <slot>` or `!sr <slot>` in Kick chat."
            )
        elif action.lower() == "off":
            await tracker.set_enabled(False)
            await ctx.send("❌ Slot call tracking **disabled**. `!call` and `!sr` commands will be ignored.")
        else:
            await ctx.send("❌ Invalid action. Use `/slotcalls on`, `/slotcalls off`, or `/slotcalls status`")

    @commands.hybrid_command(name="pickslot", aliases=["randomslot", "slotpick"])
    @commands.has_permissions(administrator=True)
    async def pick_random_slot(self, ctx):
        """
        [ADMIN] Pick a random slot request from the list
        Usage: /pickslot
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        tracker = self._get_tracker_for_guild(ctx.guild.id)
        if not tracker:
            await ctx.send("❌ Slot call tracker not initialized for this server")
            return

        if not tracker.engine:
            await ctx.send("❌ Database not available")
            return

        try:
            with tracker.engine.connect() as conn:
                # Get a random unpicked slot request (filter by server_id if available)
                if tracker.server_id:
                    result = conn.execute(
                        text(
                            """
                        SELECT id, COALESCE(display_name, kick_username), slot_call, requested_at
                        FROM slot_requests
                        WHERE picked = FALSE AND discord_server_id = :server_id
                        ORDER BY RANDOM()
                        LIMIT 1
                    """
                        ),
                        {"server_id": tracker.server_id},
                    ).fetchone()
                else:
                    result = conn.execute(
                        text(
                            """
                        SELECT id, COALESCE(display_name, kick_username), slot_call, requested_at
                        FROM slot_requests
                        WHERE picked = FALSE
                        ORDER BY RANDOM()
                        LIMIT 1
                    """
                        )
                    ).fetchone()

                if not result:
                    await ctx.send(
                        "❌ No slot requests available. The list may be empty or all requests have been picked."
                    )
                    return

                # `username` here is the native display name (COALESCE display_name→kick_username).
                request_id, username, slot_call, requested_at = result

                # Mark as picked
                with tracker.engine.begin() as update_conn:
                    update_conn.execute(
                        text(
                            """
                        UPDATE slot_requests
                        SET picked = TRUE, picked_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                    """
                        ),
                        {"id": request_id},
                    )

                # Create embed
                embed = discord.Embed(
                    title="🎰 Random Slot Picked!", description=f"**{slot_call}**", color=discord.Color.gold()
                )
                embed.add_field(name="Requested by", value=username, inline=True)
                embed.add_field(
                    name="Requested at",
                    value=requested_at.strftime("%Y-%m-%d %H:%M:%S UTC") if requested_at else "Unknown",
                    inline=True,
                )
                embed.set_footer(text=f"Request ID: {request_id}")

                await ctx.send(embed=embed)
                logger.info(f"Picked random slot: {slot_call} by {username}")

                # Send message to Kick chat
                if tracker.kick_send_callback:
                    try:
                        kick_message = f"🎰 Random slot picked: {slot_call} (requested by @{username})"
                        await tracker.kick_send_callback(kick_message, guild_id=tracker.discord_server_id)
                        logger.info(f"Sent pick notification to Kick chat")
                    except Exception as kick_error:
                        logger.error(f"Failed to send pick notification to Kick: {kick_error}")

                # Update panel if available
                if tracker.panel:
                    try:
                        await tracker.panel.update_panel()
                        logger.info("Updated slot request panel after pick")
                    except Exception as panel_error:
                        logger.error(f"Failed to update panel: {panel_error}")

        except Exception as e:
            logger.error(f"Failed to pick random slot: {e}")
            await ctx.send(f"❌ Error picking random slot: {e}")

    @commands.hybrid_command(name="slotlist", aliases=["listslots", "slots"])
    @commands.has_permissions(administrator=True)
    async def list_slot_requests(self, ctx):
        """
        [ADMIN] Show statistics about slot requests
        Usage: /slotlist
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        tracker = self._get_tracker_for_guild(ctx.guild.id)
        if not tracker:
            await ctx.send("❌ Slot call tracker not initialized for this server")
            return

        if not tracker.engine:
            await ctx.send("❌ Database not available")
            return

        try:
            with tracker.engine.connect() as conn:
                # Get counts (filter by server_id if available)
                if tracker.server_id:
                    total = conn.execute(
                        text(
                            """
                        SELECT COUNT(*) FROM slot_requests
                        WHERE discord_server_id = :server_id
                    """
                        ),
                        {"server_id": tracker.server_id},
                    ).fetchone()[0]
                    unpicked = conn.execute(
                        text(
                            """
                        SELECT COUNT(*) FROM slot_requests
                        WHERE picked = FALSE AND discord_server_id = :server_id
                    """
                        ),
                        {"server_id": tracker.server_id},
                    ).fetchone()[0]
                    picked = conn.execute(
                        text(
                            """
                        SELECT COUNT(*) FROM slot_requests
                        WHERE picked = TRUE AND discord_server_id = :server_id
                    """
                        ),
                        {"server_id": tracker.server_id},
                    ).fetchone()[0]
                else:
                    total = conn.execute(text("SELECT COUNT(*) FROM slot_requests")).fetchone()[0]
                    unpicked = conn.execute(text("SELECT COUNT(*) FROM slot_requests WHERE picked = FALSE")).fetchone()[
                        0
                    ]
                    picked = conn.execute(text("SELECT COUNT(*) FROM slot_requests WHERE picked = TRUE")).fetchone()[0]

                embed = discord.Embed(title="🎰 Slot Request Statistics", color=discord.Color.blue())
                embed.add_field(name="Total Requests", value=str(total), inline=True)
                embed.add_field(name="Available", value=str(unpicked), inline=True)
                embed.add_field(name="Already Picked", value=str(picked), inline=True)

                if unpicked > 0:
                    embed.set_footer(text=f"Use /pickslot to pick a random slot from {unpicked} available requests")
                else:
                    embed.set_footer(text="No unpicked requests available")

                await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Failed to get slot request stats: {e}")
            await ctx.send(f"❌ Error getting slot list: {e}")

    @commands.hybrid_command(name="clearslots", aliases=["clearrequests", "resetslots"])
    @commands.has_permissions(administrator=True)
    async def clear_slot_requests(self, ctx):
        """
        [ADMIN] Clear all slot requests (useful when starting a new hunt)
        Usage: /clearslots
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        tracker = self._get_tracker_for_guild(ctx.guild.id)
        if not tracker:
            await ctx.send("❌ Slot call tracker not initialized for this server")
            return

        if not tracker.engine:
            await ctx.send("❌ Database not available")
            return

        try:
            with tracker.engine.begin() as conn:
                if tracker.server_id:
                    # Delete slot_picks first (foreign key constraint)
                    conn.execute(
                        text(
                            """
                        DELETE FROM slot_picks WHERE slot_request_id IN
                        (SELECT id FROM slot_requests WHERE discord_server_id = :server_id)
                    """
                        ),
                        {"server_id": tracker.server_id},
                    )

                    # Now delete slot_requests
                    result = conn.execute(
                        text(
                            """
                        DELETE FROM slot_requests
                        WHERE discord_server_id = :server_id
                    """
                        ),
                        {"server_id": tracker.server_id},
                    )
                else:
                    # Safety: don't delete without server_id to prevent cross-server data loss
                    await ctx.send("❌ Cannot clear slots: server ID not configured. Please contact support.")
                    return
                deleted_count = result.rowcount

            embed = discord.Embed(
                title="🗑️ Slot Requests Cleared",
                description=f"Deleted **{deleted_count}** slot request(s)",
                color=discord.Color.green(),
            )
            embed.set_footer(text="Users can now make new requests")

            await ctx.send(embed=embed)
            logger.info(f"Cleared {deleted_count} slot requests (manual clear by {ctx.author})")

            # Send message to Kick chat if callback available
            if tracker.kick_send_callback:
                try:
                    await tracker.kick_send_callback(
                        "🔄 Slot requests have been cleared! You can now make new requests with !call or !sr",
                        guild_id=tracker.discord_server_id,
                    )
                except Exception as e:
                    logger.error(f"Failed to send Kick notification: {e}")

            # Update panel if available
            if tracker.panel:
                try:
                    await tracker.panel.update_panel(force=True)
                    logger.info("Updated slot request panel after clear")
                except Exception as panel_error:
                    logger.error(f"Failed to update panel: {panel_error}")

        except Exception as e:
            logger.error(f"Failed to clear slot requests: {e}")
            await ctx.send(f"❌ Error clearing slot requests: {e}")

    @toggle_slot_calls.error
    async def toggle_slot_calls_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need administrator permission to use this command.")


async def setup_slot_call_tracker(
    bot, discord_channel_id: Optional[int] = None, kick_send_callback=None, engine=None, server_id: Optional[int] = None
):
    """
    Setup slot call tracker

    Args:
        bot: Discord bot instance
        discord_channel_id: Discord channel ID to post slot calls to
        kick_send_callback: Optional callback function to send messages to Kick chat
        engine: SQLAlchemy engine for persisting state
        server_id: Discord server/guild ID for multi-server support

    Returns:
        SlotCallTracker instance
    """
    tracker = SlotCallTracker(bot, discord_channel_id, kick_send_callback, engine, server_id)

    # Add commands
    await bot.add_cog(SlotCallCommands(bot, tracker))

    logger.info(f"✅ Slot call tracker initialized (channel: {discord_channel_id or 'Not set'})")

    return tracker
