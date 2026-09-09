"""Standing panels for the levels system: the community leaderboard, and the
active competition board.

Both are the same mechanism with a different query and renderer, so LevelsPanel
is parameterised by panel type rather than duplicated — placement, move
semantics, restart recovery and the "message was deleted" repost are identical
and only want one implementation.

Community-leaderboard panel: posted/moved via the same
dashboard -> redis `post_panel` flow used by the Shuffle/Howl verify panels
(features/linking/shuffle_panel.py is the direct model). Content is a themable
BANNER image above the first page of entries as text, with Prev/Next beneath
(see ./pagination) — the standing message always shows page 1, and paging is
served privately to whoever clicks so one reader cannot move another's view.

Refresh is EVENT-DRIVEN: a level-up or rank-up schedules a debounced repaint
(see request_panel_refresh), so the board tracks activity within seconds
instead of on a ten-minute tick. The periodic loop is kept as a slow safety
net — it still closes competitions on schedule and repairs a panel whose
event-driven refresh failed or whose message was deleted.

Panel placement is stored in the existing `link_panels` table (panel_type =
'levels_leaderboard'), the same generic table Shuffle/Howl verify panels use,
rather than adding a dedicated table for one row of state per guild.
"""

import asyncio
import io
import logging
from datetime import datetime, timezone

import discord
from discord.ext import tasks
from sqlalchemy import text

from .cards import BannerTheme, render_banner_png, render_competition_winners_card
from .competition import (
    PERIOD_LABELS,
    close_competition,
    get_active_competition,
    get_competition_winners,
    mark_announced,
    renew_competition,
)
from .pagination import (
    COMPETITION_PAGE_ID,
    LEADERBOARD_PAGE_ID,
    PAGE_SIZE,
    BoardPager,
    build_board_embed,
    competition_columns,
    competition_stats,
    fetch_competition_page,
    fetch_leaderboard_page,
    leaderboard_columns,
    leaderboard_stats,
)
from .prizes import describe_prize, pay_all_prizes

logger = logging.getLogger(__name__)

PANEL_TYPE = "levels_leaderboard"
COMPETITION_PANEL_TYPE = "levels_competition"

# Attachment name the panel's MediaGallery references.
BANNER_FILENAME = "leaderboard-banner.png"

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


# Holds the refresh tasks.loop — module-level so the closure-defined loop
# isn't garbage-collected once start_levels_panel_refresh_loop returns (same
# reason bot.py keeps _giveaway_expiry_tasks).
_levels_refresh_tasks = {}


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

    def _banner_theme(self):
        """The guild's dashboard-configured banner appearance."""
        getter = getattr(self.bot, "get_guild_settings", None)
        if not callable(getter):
            return BannerTheme()
        try:
            return BannerTheme.from_settings(getter(self.guild_id))
        except Exception:
            return BannerTheme()

    async def _render(self):
        """(file, embed, view) for this panel, or (None, None, None) when it has
        nothing to show (competition panel with no competition running).

        The board is a rich EMBED: the banner as its image, then three inline
        fields — "# USER", "MESSAGES", "XP" — which is Discord's own column
        mechanism. The pager is a classic View alongside it, because a
        Components V2 message cannot carry an embed.

        The standing message always shows page 1; paging is served privately to
        whoever clicks, so a refresh never moves another reader's page.
        """
        theme = self._banner_theme()

        if self.panel_type == COMPETITION_PANEL_TYPE:
            competition = get_active_competition(self.engine, self.guild_id)
            if not competition:
                return None, None, None

            label = PERIOD_LABELS.get(competition["period_type"], "Activity")
            rows, total, page = fetch_competition_page(self.engine, competition["id"], 0)
            png = await asyncio.to_thread(
                render_banner_png,
                f"{label} Competition",
                "Top 3 most active members win prizes",
                competition_stats(competition),
                theme,
            )
            ends = _relative_timestamp(competition["end_date"])
            embed = build_board_embed(
                columns=competition_columns(rows),
                banner_filename=BANNER_FILENAME,
                header=f"{label} Competition",
                # The countdown is a client-ticked timestamp rather than baked
                # into the image: the panel only re-renders on refresh, so a
                # drawn "3d 3h left" is wrong for almost its entire life.
                subheader=f"Ends {ends}." if ends else None,
                empty_text="No activity yet this period.",
                footer="Scores count XP earned during this competition only — separate from the community leaderboard.",
            )
            view = BoardPager(COMPETITION_PAGE_ID, page, total) if total > PAGE_SIZE else None
            return discord.File(io.BytesIO(png), filename=BANNER_FILENAME), embed, view

        rows, total, page = fetch_leaderboard_page(self.engine, self.guild_id, 0)
        png = await asyncio.to_thread(
            render_banner_png,
            "Community Leaderboard",
            "Every member's lifetime XP",
            leaderboard_stats(self.engine, self.guild_id),
            theme,
        )
        embed = build_board_embed(
            columns=leaderboard_columns(rows),
            banner_filename=BANNER_FILENAME,
            header="Community Leaderboard",
            empty_text="No activity yet — members appear here once they start earning XP.",
        )
        view = BoardPager(LEADERBOARD_PAGE_ID, page, total) if total > PAGE_SIZE else None
        return discord.File(io.BytesIO(png), filename=BANNER_FILENAME), embed, view

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
            file, embed, view = await self._render()
            if file is None:
                logger.info(f"[levels] no active competition for guild {self.guild_id}; panel not posted")
                return False
            message = await channel.send(file=file, embed=embed, view=view)
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
            file, embed, view = await self._render()
            if file is None:
                return
            message = await channel.fetch_message(self.panel_message_id)
            # Embed and view are re-sent alongside the image: the rows change on
            # every refresh, and the competition countdown is a client-ticked
            # timestamp that must track a period which may have renewed.
            await message.edit(attachments=[file], embed=embed, view=view)
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

        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_colour=ACCENT_COLOR)
        container.add_item(discord.ui.TextDisplay(f"## {label} Competition Results"))
        if mentions:
            container.add_item(discord.ui.TextDisplay(f"Congratulations {mentions}!"))
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{file.filename}")))
        view.add_item(container)

        # A V2 message carries its text inside the container, so `content`
        # would be rejected here.
        await channel.send(file=file, view=view)
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


async def refresh_guild_panels(bot, guild_id):
    """Repaint one guild's panels now."""
    for attr in ("levels_panels", "levels_competition_panels"):
        panel = (getattr(bot, attr, None) or {}).get(guild_id)
        if panel is None:
            continue
        try:
            await panel.refresh()
        except Exception as e:
            logger.error(f"[levels] panel refresh failed for guild {guild_id}: {e}")


async def refresh_all_panels(bot):
    """Periodic refresh entry point, called from the task loop below."""
    for attr in ("levels_panels", "levels_competition_panels"):
        for panel in (getattr(bot, attr, None) or {}).values():
            try:
                await panel.refresh()
            except Exception as e:
                logger.error(f"[levels] panel refresh loop error: {e}")


# ── Event-driven refresh ────────────────────────────────────────────────────
#
# A level-up or rank-up repaints the board within seconds instead of waiting
# for the slow tick. Requests are DEBOUNCED per guild rather than acted on
# immediately: a busy server can produce a burst of level-ups in the same
# second, and one panel edit per award would hit Discord's per-channel edit
# rate limit and start getting throttled — while showing the reader a board
# that flickers through a dozen near-identical states.
#
# Coalescing collapses a burst into ONE edit a few seconds later. Nothing is
# lost: each repaint re-reads the board, so the single edit reflects every award
# in the window, not just the first.

# Per-guild pending refresh tasks, keyed by guild id. Module-level so the tasks
# are not garbage-collected while they sleep (same reason _levels_refresh_tasks
# exists).
_pending_refreshes = {}

# How long to wait for the burst to settle. Long enough that a wave of awards
# costs one edit, short enough that the board still reads as live.
REFRESH_DEBOUNCE_SECONDS = 5.0


def request_panel_refresh(bot, guild_id, delay=REFRESH_DEBOUNCE_SECONDS):
    """Ask for a repaint of `guild_id`'s panels, coalescing rapid requests.

    Safe to call from anywhere a board-visible change happens (see
    engine.award_xp). Returns immediately; the repaint happens on a background
    task. A request while one is already pending is absorbed by it rather than
    resetting the timer, so a continuous stream of awards still repaints every
    `delay` seconds instead of being starved by its own traffic.
    """
    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return

    pending = _pending_refreshes.get(guild_id)
    if pending is not None and not pending.done():
        return

    # Check for the loop BEFORE building the coroutine: constructing one and
    # then failing to schedule it leaves it un-awaited, which Python reports as
    # a RuntimeWarning at collection time.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (a sync context in a test/CLI) — the safety-net loop
        # will pick the change up on its next pass.
        logger.debug(f"[levels] no event loop for panel refresh of guild {guild_id}")
        return

    async def _run():
        try:
            await asyncio.sleep(delay)
            await refresh_guild_panels(bot, guild_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[levels] debounced refresh failed for guild {guild_id}: {e}")
        finally:
            _pending_refreshes.pop(guild_id, None)

    _pending_refreshes[guild_id] = asyncio.create_task(_run())


def start_levels_panel_refresh_loop(bot):
    """Slow safety net behind the event-driven refresh.

    Panels repaint on level-up/rank-up (see request_panel_refresh), so this loop
    is no longer what keeps the numbers current. It still earns its place:

      • it closes and renews competitions whose period has lapsed, which is
        time-based and has no award to trigger it;
      • it repairs a panel whose event-driven refresh failed, or whose message
        was deleted, without waiting for the next level-up;
      • it keeps a dead-quiet server's board from drifting stale.

    Hence 10 minutes rather than something tighter: a period ending is the only
    thing here that is genuinely time-sensitive, and a period boundary is not
    accurate to the second anyway.
    """

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
    logger.debug("[levels] panel safety-net loop started (10 min); refreshes are event-driven")
