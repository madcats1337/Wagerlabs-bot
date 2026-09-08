"""Standing panels for the levels system: the community leaderboard, and the
active competition board.

Both are the same mechanism with a different query and renderer, so LevelsPanel
is parameterised by panel type rather than duplicated — placement, move
semantics, restart recovery and the "message was deleted" repost are identical
and only want one implementation.

Community-leaderboard panel: posted/moved via the same
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
from datetime import datetime, timezone

import discord
from discord.ext import tasks
from sqlalchemy import text

try:
    from discord import MediaGalleryItem
except Exception:  # pragma: no cover - older discord.py
    MediaGalleryItem = None

try:
    from discord.ui import Container, LayoutView, MediaGallery, Separator, TextDisplay
except Exception:  # pragma: no cover - older discord.py
    Container = LayoutView = MediaGallery = Separator = TextDisplay = None

from .cards import render_competition_card, render_competition_winners_card, render_leaderboard_card
from .competition import (
    PERIOD_LABELS,
    close_competition,
    get_active_competition,
    get_competition_board,
    get_competition_winners,
    mark_announced,
    renew_competition,
)
from .prizes import describe_prize, pay_all_prizes

logger = logging.getLogger(__name__)

PANEL_TYPE = "levels_leaderboard"
COMPETITION_PANEL_TYPE = "levels_competition"
TOP_N = 10

ACCENT_COLOR = 0xFACC15  # Wagerlabs yellow


def _relative_timestamp(moment) -> str:
    """Discord relative timestamp, e.g. "<t:1699999999:R>".

    Rendered and ticked CLIENT-side, which is the whole reason the countdown is
    NOT drawn into the card: the panel image only re-renders every 10 minutes,
    so a baked-in "3d 3h left" is wrong for almost its entire life. Mirrors
    features/giveaway/giveaway_panel.py::_fmt_deadline.
    """
    if not moment:
        return ""
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError:
            return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return f"<t:{int(moment.timestamp())}:R>"


def _competition_view(competition, filename: str):
    """Components V2 layout: the rendered board plus a LIVE countdown.

    A V2 message cannot carry an embed, so the image rides in a MediaGallery
    referencing the attachment and the countdown sits in a TextDisplay beside
    it. Returns None when this discord.py build lacks V2, so the caller can fall
    back to sending the bare image rather than posting nothing.
    """
    if LayoutView is None or MediaGalleryItem is None:
        return None

    label = PERIOD_LABELS.get(competition["period_type"], "Activity")
    ends = _relative_timestamp(competition["end_date"])

    view = LayoutView(timeout=None)
    container = Container(accent_colour=ACCENT_COLOR)
    container.add_item(TextDisplay(f"## {label} Competition"))
    container.add_item(
        TextDisplay(
            f"Top 3 most active members win prizes. Ends {ends}." if ends else "Top 3 most active members win prizes."
        )
    )
    container.add_item(MediaGallery(MediaGalleryItem(f"attachment://{filename}")))
    container.add_item(Separator())
    container.add_item(
        TextDisplay("-# Scores count XP earned during this competition only — separate from the community leaderboard.")
    )
    view.add_item(container)
    return view


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
    """Manages one standing panel message for one guild.

    `panel_type` selects both the link_panels row and which board is rendered,
    so the community leaderboard and the competition board share this class.
    """

    def __init__(self, bot, engine, guild_id, panel_type=PANEL_TYPE):
        self.bot = bot
        self.engine = engine
        self.guild_id = guild_id
        self.panel_type = panel_type
        self.panel_channel_id = None
        self.panel_message_id = None
        self._load_panel_info()

    async def _render(self):
        """(file, view) for this panel, or (None, None) when it has nothing to
        show (competition panel with no competition running).

        `view` is a Components V2 layout for the competition panel — that is
        what carries the live countdown next to the image — and None for the
        plain leaderboard panel.
        """
        if self.panel_type == COMPETITION_PANEL_TYPE:
            competition = get_active_competition(self.engine, self.guild_id)
            if not competition:
                return None, None
            board = get_competition_board(self.engine, competition["id"], limit=TOP_N)
            file = await render_competition_card(board, PERIOD_LABELS.get(competition["period_type"], "Activity"))
            return file, _competition_view(competition, file.filename)

        file = await render_leaderboard_card(_fetch_leaderboard_rows(self.engine, self.guild_id))
        return file, None

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
                    {"guild_id": self.guild_id, "ptype": self.panel_type},
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
                    {"guild_id": self.guild_id, "ptype": self.panel_type},
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
                        "ptype": self.panel_type,
                    },
                )
        except Exception as e:
            logger.error(f"[levels] failed to save panel info for guild {self.guild_id}: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        """Post the panel fresh in `channel` (used for the initial post and moves)."""
        try:
            file, view = await self._render()
            if file is None:
                logger.info(f"[levels] no active competition for guild {self.guild_id}; panel not posted")
                return False
            kwargs = {"file": file}
            if view is not None:
                kwargs["view"] = view
            message = await channel.send(**kwargs)
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.id, message.id)
            return True
        except Exception as e:
            logger.error(f"[levels] failed to create {self.panel_type} panel for guild {self.guild_id}: {e}")
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
            file, view = await self._render()
            if file is None:
                return
            message = await channel.fetch_message(self.panel_message_id)
            # The countdown is a client-ticked timestamp in the view, so the
            # view is re-sent alongside the image to keep the end instant in
            # step with a competition that renewed since the last refresh.
            if view is not None:
                await message.edit(attachments=[file], view=view)
            else:
                await message.edit(attachments=[file])
        except discord.NotFound:
            logger.info(f"[levels] {self.panel_type} panel message gone for guild {self.guild_id}; reposting")
            await self.create_panel(channel)
        except Exception as e:
            logger.error(f"[levels] failed to refresh {self.panel_type} panel for guild {self.guild_id}: {e}")


async def setup_levels_panel_system(bot, engine, panel_type=PANEL_TYPE):
    """Build per-guild panel instances (re-attaching to any already-posted message)."""
    panels = {}
    for guild in bot.guilds:
        panels[guild.id] = LevelsPanel(bot, engine, guild.id, panel_type)
    return panels


async def _announce_competition_end(bot, engine, guild_id, competition, winners):
    """Post the podium card for a just-closed competition.

    Claimed via mark_announced so a restart between closing and announcing
    cannot post the results twice.
    """
    if not mark_announced(engine, competition["id"]):
        return

    getter = getattr(bot, "get_guild_settings", None)
    settings = getter(guild_id) if callable(getter) else None
    channel_id = getattr(settings, "levels_competition_channel_id", None) or getattr(
        settings, "levels_channel_id", None
    )
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.warning(f"[levels] competition channel {channel_id} not found for guild {guild_id}: {e}")
            return

    try:
        card_rows = [
            {
                "place": w["place"],
                "discord_id": w["discord_id"],
                "username": w["username"],
                "avatar_url": None,
                "xp": w["xp"],
                "prize": describe_prize(w["prize_type"], w["prize_amount"], w["prize_text"]),
            }
            for w in winners
        ]
        label = PERIOD_LABELS.get(competition["period_type"], "Activity")
        file = await render_competition_winners_card(card_rows, label)
        mentions = " ".join(f"<@{w['discord_id']}>" for w in winners)

        view = None
        if LayoutView is not None and MediaGalleryItem is not None:
            view = LayoutView(timeout=None)
            container = Container(accent_colour=ACCENT_COLOR)
            container.add_item(TextDisplay(f"## {label} Competition Results"))
            if mentions:
                container.add_item(TextDisplay(f"Congratulations {mentions}!"))
            container.add_item(MediaGallery(MediaGalleryItem(f"attachment://{file.filename}")))
            view.add_item(container)

        if view is not None:
            # A V2 message carries its text inside the container, so `content`
            # would be rejected here.
            await channel.send(file=file, view=view)
        else:
            await channel.send(content=mentions or None, file=file)
    except Exception as e:
        logger.error(f"[levels] failed to post competition results for guild {guild_id}: {e}")


async def check_competitions(bot, engine):
    """Close and renew any competition whose period has lapsed.

    Runs on the existing panel-refresh tick rather than its own loop. A DB error
    inside get_active_competition propagates out to the caller, which skips this
    guild for this tick — deliberately, so a transient failure can never be read
    as "no competition" and end one that is running fine.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    for guild in bot.guilds:
        try:
            competition = get_active_competition(engine, guild.id)
            if not competition:
                continue

            end_date = competition["end_date"]
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if now < end_date:
                continue

            if not close_competition(engine, competition):
                continue  # someone else closed it first

            winners = get_competition_winners(engine, competition["id"])
            pay_all_prizes(engine, guild.id, competition["id"], winners)
            # Re-read so the card shows payout state as actually committed.
            winners = get_competition_winners(engine, competition["id"])
            await _announce_competition_end(bot, engine, guild.id, competition, winners)

            if competition.get("auto_renew"):
                renew_competition(engine, competition)
        except Exception as e:
            logger.error(f"[levels] competition check failed for guild {guild.id}: {e}", exc_info=True)


async def refresh_all_panels(bot):
    """Periodic refresh entry point, called from the task loop below."""
    for attr in ("levels_panels", "levels_competition_panels"):
        for panel in (getattr(bot, attr, None) or {}).values():
            try:
                await panel.refresh()
            except Exception as e:
                logger.error(f"[levels] panel refresh loop error: {e}")


def start_levels_panel_refresh_loop(bot):
    """Periodically re-render every guild's leaderboard panel in place, so the
    numbers stay current even with no new awards to trigger a refresh."""

    @tasks.loop(minutes=10)
    async def refresh_levels_panels():
        # Competitions are closed BEFORE the panels re-render, so the tick that
        # ends a period also repaints the panel with the fresh one.
        engine = getattr(bot, "levels_engine", None)
        if engine is not None:
            await check_competitions(bot, engine)
        await refresh_all_panels(bot)

    @refresh_levels_panels.before_loop
    async def _before():
        await bot.wait_until_ready()

    _levels_refresh_tasks["main"] = refresh_levels_panels
    refresh_levels_panels.start()
    logger.debug("[levels] leaderboard panel refresh loop started (10 min)")
