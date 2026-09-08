"""Standing community-leaderboard panel: posted/moved via the same
dashboard -> redis `post_panel` flow used by the Shuffle/Howl verify panels
(features/linking/shuffle_panel.py is the direct model). Content is the
top-N `user_levels` rows rendered as one image, refreshed periodically and
edited in place. Unlike the verify panels there is no button to click — this
is a read-only display, so `create_panel`/`refresh` just send/edit an image.

Panel placement is stored in the existing `link_panels` table (panel_type =
'levels_leaderboard'), the same generic table Shuffle/Howl verify panels use,
rather than adding a dedicated table for one row of state per guild.
"""

import logging

import discord
from discord.ext import tasks
from sqlalchemy import text

from .cards import render_leaderboard_card

logger = logging.getLogger(__name__)

PANEL_TYPE = "levels_leaderboard"
TOP_N = 10

# Holds the refresh tasks.loop — module-level so the closure-defined loop
# isn't garbage-collected once start_levels_panel_refresh_loop returns (same
# reason bot.py keeps _giveaway_expiry_tasks).
_levels_refresh_tasks = {}


def _fetch_leaderboard_rows(engine, guild_id, limit=TOP_N):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT discord_id, username, avatar_url, total_xp, messages_sent,
                       current_level, current_rank
                FROM user_levels
                WHERE discord_server_id = :guild_id
                ORDER BY total_xp DESC
                LIMIT :limit
                """
            ),
            {"guild_id": guild_id, "limit": limit},
        ).fetchall()

    return [
        {
            "position": i + 1,
            "discord_id": r[0],
            "username": r[1],
            "avatar_url": r[2],
            "total_xp": r[3],
            "messages_sent": r[4],
            "current_level": r[5],
            "current_rank": r[6],
        }
        for i, r in enumerate(rows)
    ]


class LevelsPanel:
    """Manages the community-leaderboard panel message for one guild."""

    def __init__(self, bot, engine, guild_id):
        self.bot = bot
        self.engine = engine
        self.guild_id = guild_id
        self.panel_channel_id = None
        self.panel_message_id = None
        self._load_panel_info()

    def _load_panel_info(self):
        try:
            with self.engine.connect() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT channel_id, message_id FROM link_panels
                        WHERE guild_id = :guild_id AND panel_type = :ptype
                        ORDER BY created_at DESC LIMIT 1
                        """
                    ),
                    {"guild_id": self.guild_id, "ptype": PANEL_TYPE},
                ).fetchone()
            if row:
                self.panel_channel_id, self.panel_message_id = row[0], row[1]
        except Exception as e:
            logger.error(f"[levels] failed to load panel info for guild {self.guild_id}: {e}")

    def _save_panel_info(self, channel_id, message_id):
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM link_panels WHERE guild_id = :guild_id AND panel_type = :ptype"),
                    {"guild_id": self.guild_id, "ptype": PANEL_TYPE},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO link_panels
                            (guild_id, discord_server_id, channel_id, message_id, emoji, panel_type, created_at)
                        VALUES (:guild_id, :guild_id, :channel_id, :message_id, '🏆', :ptype, CURRENT_TIMESTAMP)
                        """
                    ),
                    {
                        "guild_id": self.guild_id,
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "ptype": PANEL_TYPE,
                    },
                )
        except Exception as e:
            logger.error(f"[levels] failed to save panel info for guild {self.guild_id}: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        """Post the panel fresh in `channel` (used for the initial post and moves)."""
        try:
            rows = _fetch_leaderboard_rows(self.engine, self.guild_id)
            file = await render_leaderboard_card(rows)
            message = await channel.send(file=file)
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.id, message.id)
            return True
        except Exception as e:
            logger.error(f"[levels] failed to create leaderboard panel for guild {self.guild_id}: {e}")
            return False

    def _enabled(self) -> bool:
        """Whether this guild still has leveling switched on.

        Only the PERIODIC refresh consults this — an explicit dashboard
        channel pick still posts the panel, so an admin setting the channel
        up before flipping the toggle on isn't left staring at nothing.
        """
        getter = getattr(self.bot, "get_guild_settings", None)
        if not callable(getter):
            return True
        try:
            return bool(getter(self.guild_id).levels_enabled)
        except Exception:
            return True

    async def refresh(self):
        """Re-render and edit the standing message in place; re-post if it's gone."""
        if not self.panel_channel_id or not self.panel_message_id:
            return
        if not self._enabled():
            return

        channel = self.bot.get_channel(self.panel_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(self.panel_channel_id)
            except Exception:
                return

        try:
            rows = _fetch_leaderboard_rows(self.engine, self.guild_id)
            file = await render_leaderboard_card(rows)
            message = await channel.fetch_message(self.panel_message_id)
            await message.edit(attachments=[file])
        except discord.NotFound:
            logger.info(f"[levels] leaderboard panel message gone for guild {self.guild_id}; reposting")
            await self.create_panel(channel)
        except Exception as e:
            logger.error(f"[levels] failed to refresh leaderboard panel for guild {self.guild_id}: {e}")


async def setup_levels_panel_system(bot, engine):
    """Build per-guild panel instances (re-attaching to any already-posted message)."""
    panels = {}
    for guild in bot.guilds:
        panels[guild.id] = LevelsPanel(bot, engine, guild.id)
    return panels


async def refresh_all_panels(bot):
    """Periodic refresh entry point, called from the task loop below."""
    panels = getattr(bot, "levels_panels", None) or {}
    for panel in panels.values():
        try:
            await panel.refresh()
        except Exception as e:
            logger.error(f"[levels] panel refresh loop error: {e}")


def start_levels_panel_refresh_loop(bot):
    """Periodically re-render every guild's leaderboard panel in place, so the
    numbers stay current even with no new awards to trigger a refresh."""

    @tasks.loop(minutes=10)
    async def refresh_levels_panels():
        await refresh_all_panels(bot)

    @refresh_levels_panels.before_loop
    async def _before():
        await bot.wait_until_ready()

    _levels_refresh_tasks["main"] = refresh_levels_panels
    refresh_levels_panels.start()
    logger.debug("[levels] leaderboard panel refresh loop started (10 min)")
