"""
Giveaway Manager - Core giveaway logic

Handles giveaway lifecycle, entry tracking, and winner selection.
"""

import asyncio
import hashlib
import logging
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)


class GiveawayManager:
    """Manages giveaway system for a specific Discord server"""

    def __init__(self, engine, guild_id=None):
        self.engine = engine
        self.guild_id = guild_id
        self.active_giveaway = None
        self.chat_tracker = None

    async def load_active_giveaway(self):
        """Load currently active giveaway for this server"""
        with self.engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                SELECT * FROM giveaways
                WHERE discord_server_id = :server_id
                AND status = 'active'
                LIMIT 1
            """
                ),
                {"server_id": self.guild_id},
            ).fetchone()

            if result:
                self.active_giveaway = dict(result._mapping)
                logger.info(f"Loaded active giveaway {self.active_giveaway['id']} for server {self.guild_id}")
            else:
                self.active_giveaway = None

        return self.active_giveaway

    async def start_giveaway(self, giveaway_id):
        """Start a giveaway"""
        with self.engine.connect() as conn:
            # Update giveaway status
            conn.execute(
                text(
                    """
                UPDATE giveaways
                SET status = 'active',
                    started_at = :now,
                    updated_at = :now
                WHERE id = :giveaway_id
                AND discord_server_id = :server_id
            """
                ),
                {"giveaway_id": giveaway_id, "server_id": self.guild_id, "now": datetime.utcnow()},
            )
            conn.commit()

        # Reload active giveaway
        await self.load_active_giveaway()
        logger.info(f"Started giveaway {giveaway_id}")
        return True

    async def stop_giveaway(self, giveaway_id):
        """Stop a giveaway"""
        with self.engine.connect() as conn:
            conn.execute(
                text(
                    """
                UPDATE giveaways
                SET status = 'ended',
                    ended_at = :now,
                    updated_at = :now
                WHERE id = :giveaway_id
                AND discord_server_id = :server_id
            """
                ),
                {"giveaway_id": giveaway_id, "server_id": self.guild_id, "now": datetime.utcnow()},
            )
            conn.commit()

        if self.active_giveaway and self.active_giveaway["id"] == giveaway_id:
            self.active_giveaway = None

        logger.info(f"Stopped giveaway {giveaway_id}")
        return True

    async def _fetch_avatar(self, username, platform="kick"):
        """Resolve a chat entrant's avatar URL for the given platform. Best-effort;
        returns None on any failure (entry still records without a picture)."""
        try:
            if platform == "twitch":
                from core.twitch_api import TwitchAPI

                # Resolve via Helix /users using an app token. Fall back silently.
                api = TwitchAPI()
                try:
                    tokens = await api.get_app_token()
                    api.access_token = tokens.access_token
                    user = await api.get_user(login=username)
                    return user.get("profile_image_url") if user else None
                finally:
                    await api.close()
            else:
                from core.kick_api import get_channel_info

                channel_data = await get_channel_info(username)
                if channel_data and "user" in channel_data:
                    return channel_data["user"].get("profile_pic")
        except Exception as e:
            logger.warning(f"Failed to fetch {platform} profile pic for {username}: {e}")
        return None

    async def add_entry(
        self,
        kick_username,
        kick_user_id=None,
        discord_id=None,
        entry_method="keyword",
        platform="kick",
        display_name=None,
    ):
        """Add an entry to the active giveaway.

        `platform` selects where to resolve the entrant's avatar from (Kick API
        vs Twitch Helix). Entries from Twitch chat pass platform='twitch'.
        `display_name` is the native platform name shown in the winner announcement
        (falls back to kick_username); kick_username stays the credit/dedup key.
        """
        if not self.active_giveaway:
            logger.warning(f"No active giveaway for entry from {kick_username}")
            return False

        giveaway_id = self.active_giveaway["id"]
        allow_multiple = self.active_giveaway["allow_multiple_entries"]
        max_entries = self.active_giveaway["max_entries_per_user"]

        # Fetch profile picture from the entrant's platform.
        profile_pic_url = await self._fetch_avatar(kick_username, platform)

        with self.engine.connect() as conn:
            # Check existing entries
            existing = conn.execute(
                text(
                    """
                SELECT entry_count FROM giveaway_entries
                WHERE giveaway_id = :giveaway_id
                AND kick_username = :username
            """
                ),
                {"giveaway_id": giveaway_id, "username": kick_username},
            ).fetchone()

            if existing:
                if not allow_multiple:
                    logger.debug(f"{kick_username} already entered giveaway {giveaway_id}")
                    return False

                current_count = existing[0]
                if current_count >= max_entries:
                    logger.debug(f"{kick_username} reached max entries ({max_entries}) for giveaway {giveaway_id}")
                    return False

                # Increment entry count
                conn.execute(
                    text(
                        """
                    UPDATE giveaway_entries
                    SET entry_count = entry_count + 1
                    WHERE giveaway_id = :giveaway_id
                    AND kick_username = :username
                """
                    ),
                    {"giveaway_id": giveaway_id, "username": kick_username},
                )
                logger.info(f"Added additional entry for {kick_username} in giveaway {giveaway_id}")
            else:
                # Create new entry
                conn.execute(
                    text(
                        """
                    INSERT INTO giveaway_entries
                    (giveaway_id, discord_server_id, discord_id, kick_username, kick_user_id, entry_method, entry_count, profile_pic_url, display_name)
                    VALUES (:giveaway_id, :server_id, :discord_id, :username, :user_id, :method, 1, :profile_pic, :display_name)
                """
                    ),
                    {
                        "giveaway_id": giveaway_id,
                        "server_id": self.guild_id,
                        "discord_id": discord_id,
                        "username": kick_username,
                        "user_id": kick_user_id,
                        "method": entry_method,
                        "profile_pic": profile_pic_url,
                        "display_name": display_name or kick_username,
                    },
                )
                logger.info(f"Added new entry for {kick_username} in giveaway {giveaway_id} via {entry_method}")

            conn.commit()
            return True

    async def track_message(self, kick_username, message, platform="kick", display_name=None):
        """Track a chat message for active chatter detection.

        Returns True only when this message pushed the chatter over the threshold
        and a new/incremented entry was actually recorded, so the caller can emit a
        realtime "entry added" event; False/None otherwise.
        """
        if not self.active_giveaway:
            return False

        giveaway = self.active_giveaway

        # Only track if using active_chatter entry method
        if giveaway["entry_method"] != "active_chatter":
            return False

        giveaway_id = giveaway["id"]
        messages_required = giveaway["messages_required"]
        time_window = giveaway["time_window_minutes"]

        # Create message hash for duplicate detection
        message_hash = hashlib.sha256(message.encode()).hexdigest()

        with self.engine.connect() as conn:
            # Check if this exact message was already sent by this user
            existing = conn.execute(
                text(
                    """
                SELECT id FROM giveaway_chat_activity
                WHERE giveaway_id = :giveaway_id
                AND kick_username = :username
                AND message_hash = :hash
            """
                ),
                {"giveaway_id": giveaway_id, "username": kick_username, "hash": message_hash},
            ).fetchone()

            if existing:
                logger.debug(f"Duplicate message from {kick_username}, not tracking")
                return False

            # Track the message
            conn.execute(
                text(
                    """
                INSERT INTO giveaway_chat_activity
                (giveaway_id, discord_server_id, kick_username, message, message_hash)
                VALUES (:giveaway_id, :server_id, :username, :message, :hash)
            """
                ),
                {
                    "giveaway_id": giveaway_id,
                    "server_id": self.guild_id,
                    "username": kick_username,
                    "message": message[:500],  # Limit message length
                    "hash": message_hash,
                },
            )

            # Check if user qualifies for auto-entry.
            # Compute the time window entirely in the DB (CURRENT_TIMESTAMP - INTERVAL)
            # so both sides of the comparison use the DB server clock. Comparing the
            # DB-populated `timestamp` against a Python `datetime.utcnow()` cutoff would
            # silently return 0 if the DB session timezone were ever behind UTC.
            message_count = conn.execute(
                text(
                    """
                SELECT COUNT(DISTINCT message_hash) as count
                FROM giveaway_chat_activity
                WHERE giveaway_id = :giveaway_id
                AND kick_username = :username
                AND timestamp >= CURRENT_TIMESTAMP - (:window_minutes * INTERVAL '1 minute')
            """
                ),
                {"giveaway_id": giveaway_id, "username": kick_username, "window_minutes": time_window},
            ).fetchone()

            conn.commit()

            if message_count and message_count[0] >= messages_required:
                # User qualifies! Add entry
                logger.info(f"{kick_username} qualified for auto-entry with {message_count[0]} unique messages")
                return await self.add_entry(
                    kick_username, entry_method="active_chatter", platform=platform, display_name=display_name
                )

        return False

    async def get_entries(self, giveaway_id=None):
        """Get all entries for a giveaway (defaults to the cached active one).

        `kick_username` is the CANONICAL identity used for winner bookkeeping —
        Discord entrants have no Kick name, so it falls back to their Discord
        username. `display_name` is what humans should see.
        """
        giveaway_id = giveaway_id or (self.active_giveaway or {}).get("id")
        if not giveaway_id:
            return []

        with self.engine.connect() as conn:
            results = conn.execute(
                text(
                    """
                SELECT COALESCE(kick_username, discord_username) AS kick_username,
                       kick_user_id, entry_count, entry_method, created_at,
                       COALESCE(display_name, kick_username, discord_username) AS display_name
                FROM giveaway_entries
                WHERE giveaway_id = :giveaway_id
                  AND COALESCE(kick_username, discord_username) IS NOT NULL
                ORDER BY created_at ASC
            """
                ),
                {"giveaway_id": giveaway_id},
            ).fetchall()

            return [dict(row._mapping) for row in results]

    async def draw_winner(self, giveaway_id=None, exclude=None, finalize=True):
        """Draw ONE winner using the provably-fair algorithm.

        Split so it can be called repeatedly for a multi-winner giveaway:
          * `exclude` - canonical names already drawn, filtered out of the pool.
          * `finalize` - when False, the giveaway is left 'active' and the
            in-memory cache is kept, so the caller can draw again. The caller
            marks completion once the cap is reached (see `complete()`).

        Records the winner in `giveaway_winners` (the table the dashboard
        console and the exclusion queries read) as well as stamping
        `giveaways.winner_kick_username` for backwards compatibility.

        Returns {"winner", "display"} or None when the pool is exhausted.
        """
        from utils.provably_fair import generate_provably_fair_result

        giveaway_id = giveaway_id or (self.active_giveaway or {}).get("id")
        if not giveaway_id:
            return None

        exclude = set(exclude or [])
        entries = [e for e in await self.get_entries(giveaway_id) if e["kick_username"] not in exclude]
        if not entries:
            logger.warning(f"No entries left to draw from for giveaway {giveaway_id}")
            return None

        # Build weighted list based on entry_count
        weighted_entries = []
        for entry in entries:
            for _ in range(entry["entry_count"]):
                weighted_entries.append(entry["kick_username"])

        # Client seed varies per draw so a multi-winner giveaway doesn't reuse
        # the same proof for every winner.
        client_seed = f"giveaway:{giveaway_id}:{len(weighted_entries)}:{len(exclude)}"

        result = generate_provably_fair_result(
            kick_username=client_seed,
            slot_request_id=giveaway_id,
            slot_call="giveaway_draw",
            chance_percent=100.0,
        )

        winner_index = int((result["random_value"] / 100.0) * len(weighted_entries))
        winner_index = min(winner_index, len(weighted_entries) - 1)
        winner_username = weighted_entries[winner_index]
        winner_display = next(
            (e.get("display_name") or e["kick_username"] for e in entries if e["kick_username"] == winner_username),
            winner_username,
        )

        logger.info(
            f"Provably fair draw - Random value: {result['random_value']}, "
            f"Index: {winner_index}/{len(weighted_entries)}, Winner: {winner_username}"
        )

        with self.engine.begin() as conn:
            # The winners TABLE is authoritative for who has already won —
            # without this row the next draw would not exclude them and could
            # pick the same person twice.
            conn.execute(
                text(
                    """
                INSERT INTO giveaway_winners (giveaway_id, discord_server_id, kick_username)
                VALUES (:giveaway_id, :server_id, :winner)
            """
                ),
                {"giveaway_id": giveaway_id, "server_id": str(self.guild_id), "winner": winner_username},
            )
            conn.execute(
                text(
                    """
                UPDATE giveaways
                SET winner_kick_username = :winner,
                    updated_at = :now,
                    server_seed = :server_seed,
                    client_seed = :client_seed,
                    nonce = :nonce,
                    proof_hash = :proof_hash,
                    random_value = :random_value
                WHERE id = :giveaway_id
            """
                ),
                {
                    "winner": winner_username,
                    "giveaway_id": giveaway_id,
                    "now": datetime.utcnow(),
                    "server_seed": result["server_seed"],
                    "client_seed": result["client_seed"],
                    "nonce": result["nonce"],
                    "proof_hash": result["proof_hash"],
                    "random_value": result["random_value"],
                },
            )

        logger.info(f"Drew winner: {winner_username} (shown as {winner_display}) for giveaway {giveaway_id}")

        if finalize:
            await self.complete(giveaway_id)

        return {"winner": winner_username, "display": winner_display}

    async def draw_all_winners(self, giveaway_id=None):
        """Draw up to `max_winners` winners in one pass, then complete.

        Used by the expiry loop. Stops early when the entry pool runs out.
        Returns the list of display names, newest last.
        """
        giveaway_id = giveaway_id or (self.active_giveaway or {}).get("id")
        if not giveaway_id:
            return []

        with self.engine.connect() as conn:
            row = conn.execute(
                text("SELECT COALESCE(max_winners, 1) FROM giveaways WHERE id = :gid"),
                {"gid": giveaway_id},
            ).fetchone()
        max_winners = max(1, int(row[0])) if row else 1

        drawn, display_names = [], []
        for _ in range(max_winners):
            result = await self.draw_winner(giveaway_id=giveaway_id, exclude=drawn, finalize=False)
            if not result:
                break  # pool exhausted
            drawn.append(result["winner"])
            display_names.append(result["display"])

        await self.complete(giveaway_id)
        return display_names

    async def complete(self, giveaway_id=None):
        """Mark the giveaway finished and drop it from the in-memory cache."""
        giveaway_id = giveaway_id or (self.active_giveaway or {}).get("id")
        if not giveaway_id:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                UPDATE giveaways
                SET status = 'completed', ended_at = :now, updated_at = :now
                WHERE id = :giveaway_id
            """
                ),
                {"giveaway_id": giveaway_id, "now": datetime.utcnow()},
            )
        if (self.active_giveaway or {}).get("id") == giveaway_id:
            self.active_giveaway = None


async def setup_giveaway_managers(bot, engine):
    """Create giveaway manager for each guild"""
    from utils.log_context import clear_server, set_server

    managers = {}

    for guild in bot.guilds:
        # Tag this guild's giveaway-load logs with the server (not "[-]").
        set_server(guild.id, guild.name)
        manager = GiveawayManager(engine, guild_id=guild.id)
        await manager.load_active_giveaway()
        managers[guild.id] = manager
        logger.debug(f"Set up giveaway manager for guild {guild.id}")

    # Don't let the last guild's tag bleed into subsequent global startup logs.
    clear_server()
    return managers
