"""
Raffle Period Scheduler
Handles automatic monthly period resets and optional auto-draw
"""

import logging
from datetime import datetime, timedelta

from discord.ext import tasks
from sqlalchemy import text

from utils.log_context import set_server

from .database import create_new_period, get_current_period
from .draw import RaffleDraw
from .reward_settings import get_ticket_reward_settings, platform_campaign_code, platform_display_name

logger = logging.getLogger(__name__)


def _end_of_month(start):
    """23:59:59 on the last day of `start`'s month."""
    if start.month == 12:
        next_month = start.replace(year=start.year + 1, month=1)
    else:
        next_month = start.replace(month=start.month + 1)
    return next_month - timedelta(seconds=1)


def _next_month_start(when):
    """Midnight on the 1st of the month AFTER `when`'s month."""
    first = when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if first.month == 12:
        return first.replace(year=first.year + 1, month=1)
    return first.replace(month=first.month + 1)


def _spans_whole_month(start, end):
    """True when (start, end) looks like one calendar month.

    Matches how monthly periods are written here — 1st at 00:00:00 through the
    last day at 23:59:59 — and tolerates a small slop so a period created a few
    seconds off (or via the dashboard's date-only inputs) still reads as monthly
    and keeps rolling on month boundaries.
    """
    if start.day != 1:
        return False
    return abs((end - _end_of_month(start)).total_seconds()) <= 86400


class RaffleScheduler:
    """Manages automatic raffle period transitions"""

    def __init__(self, engine, bot=None, auto_draw=False, announcement_channel_id=None, discord_server_id=None):
        """
        Initialize raffle scheduler

        Args:
            engine: SQLAlchemy database engine
            bot: Discord bot instance (for announcements)
            auto_draw: Whether to automatically draw winner at period end (initial value, refreshed from DB)
            announcement_channel_id: Discord channel ID for winner announcements
            discord_server_id: Discord server ID for multiserver support
        """
        self.engine = engine
        self.bot = bot
        self._auto_draw_default = auto_draw  # Fallback value
        self.announcement_channel_id = announcement_channel_id
        self.discord_server_id = discord_server_id
        self.raffle_draw = RaffleDraw(engine)

        logger.debug(f"📅 Raffle scheduler initialized (auto_draw: {auto_draw}, server: {discord_server_id})")

    @property
    def auto_draw(self):
        """Get auto_draw setting from database (refreshes on each check)"""
        try:
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                        SELECT value FROM bot_settings
                        WHERE key = 'raffle_auto_draw' AND discord_server_id = :server_id
                    """
                    ),
                    {"server_id": self.discord_server_id},
                )
                row = result.fetchone()
                if row:
                    value = row[0]
                    # Handle string 'true'/'false' or boolean
                    if isinstance(value, str):
                        return value.lower() == "true"
                    return bool(value)
        except Exception as e:
            logger.warning(f"Failed to get raffle_auto_draw setting from DB: {e}")
        return self._auto_draw_default

    @property
    def auto_renew(self):
        """
        Get the server-wide raffle_auto_renew setting (refreshes on each check).

        When TRUE, a new period is created automatically once the current one
        ends. When FALSE (the default), the raffle goes dormant at period end —
        no new period is created until the user starts one from the dashboard or
        with !rafflestart. This is what makes period creation opt-in.
        """
        try:
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                        SELECT value FROM bot_settings
                        WHERE key = 'raffle_auto_renew' AND discord_server_id = :server_id
                    """
                    ),
                    {"server_id": self.discord_server_id},
                )
                row = result.fetchone()
                if row:
                    value = row[0]
                    if isinstance(value, str):
                        return value.lower() == "true"
                    return bool(value)
        except Exception as e:
            logger.warning(f"Failed to get raffle_auto_renew setting from DB: {e}")
        # Default OFF: renewal is opt-in.
        return False

    def check_period_transition(self):
        """
        Check if we need to transition to a new raffle period
        Also checks if it's time to draw winner (10 minutes before end)

        Returns:
            dict: Transition info or None if no transition needed
        """
        try:
            current_period = get_current_period(self.engine, self.discord_server_id)

            if not current_period:
                # No active period. Creation is opt-in, so normally we stay
                # dormant — EXCEPT when auto_renew is on: most periods don't end
                # via the now>end_date transition below. An early auto-draw, a
                # dashboard draw, or "End Current Period" marks the period
                # 'ended' BEFORE end_date passes (draw_winner's update_period),
                # so _transition_to_new_period never runs for them and renewal
                # has to be recovered here once the scheduled end has passed.
                if self.auto_renew:
                    return self._maybe_renew_after_end()
                logger.debug(
                    f"[Server {self.discord_server_id}] No active raffle period — staying dormant (start one to begin)"
                )
                return None

            now = datetime.now()
            end_date = current_period["end_date"]
            start_date = current_period["start_date"]

            # Ensure dates are datetime objects
            if isinstance(end_date, str):
                end_date = datetime.fromisoformat(end_date)
            if isinstance(start_date, str):
                start_date = datetime.fromisoformat(start_date)

            # Automatic cleanup disabled - use !rafflecleanup command instead
            # This prevents unwanted cleanup after manual operations

            # Check if it's 10 minutes before period ends and winner not drawn yet
            # ONLY draw if auto_draw is enabled!
            time_until_end = (end_date - now).total_seconds()
            if (
                self.auto_draw and 0 < time_until_end <= 600 and not current_period.get("winner_discord_id")
            ):  # 600 seconds = 10 minutes
                logger.info(f"⏰ 10 minutes until period end - auto_draw enabled, drawing winner now!")
                self._draw_winner_for_period(current_period)
            elif 0 < time_until_end <= 600 and not current_period.get("winner_discord_id"):
                logger.info(f"⏰ 10 minutes until period end - auto_draw DISABLED, skipping automatic draw")

            # Check if current period has ended
            if now > end_date:
                logger.info(
                    f"🔔 [Server {self.discord_server_id}] Raffle period #{current_period['id']} has ended! (end_date: {end_date}, now: {now})"
                )
                return self._transition_to_new_period(current_period)
            else:
                # Period still active
                time_remaining = end_date - now
                hours_remaining = time_remaining.total_seconds() / 3600
                if hours_remaining < 24:
                    logger.debug(
                        f"[Server {self.discord_server_id}] Period #{current_period['id']} active ({hours_remaining:.1f} hours remaining)"
                    )
                return None

        except Exception as e:
            logger.error(f"Failed to check period transition: {e}")
            return None

    def _maybe_renew_after_end(self):
        """
        Auto-renew recovery for periods that ended without a transition.

        Called from check_period_transition when no period is active and
        auto_renew is ON. Rolls into a fresh period — same LENGTH as the one
        that just ended — once the latest period's SCHEDULED end has passed:
          • no periods at all → raffle never started, stay dormant
          • latest period was ended BY A PERSON → stay dormant permanently
          • latest end_date still in the future (drawn early) → stay dormant
            until the scheduled end passes, then renew
          • latest end_date passed → create the next period

        Returns the transition dict from _create_renewal_period, or None.
        """
        # `ended_manually` is read through a guarded column check rather than
        # selected directly: the two services deploy independently, so the bot
        # can run against a database whose migration hasn't landed yet. A bare
        # SELECT of a missing column raises, and check_period_transition's
        # except-block would turn that into "no renewal" for every server until
        # the dashboard caught up.
        has_flag = self._has_ended_manually_column()
        flag_select = "COALESCE(ended_manually, FALSE)" if has_flag else "FALSE"

        if self.discord_server_id is not None:
            query = text(
                f"""
                SELECT end_date, {flag_select} FROM raffle_periods
                WHERE discord_server_id = :server_id
                ORDER BY end_date DESC
                LIMIT 1
            """
            )
            params = {"server_id": self.discord_server_id}
        else:
            query = text(f"SELECT end_date, {flag_select} FROM raffle_periods ORDER BY end_date DESC LIMIT 1")
            params = {}

        with self.engine.begin() as conn:
            row = conn.execute(query, params).fetchone()

        if not row:
            logger.debug(f"[Server {self.discord_server_id}] Auto-renew ON but no periods exist — staying dormant")
            return None

        latest_end = row[0]
        ended_manually = bool(row[1])
        if isinstance(latest_end, str):
            latest_end = datetime.fromisoformat(latest_end)

        if ended_manually:
            # A person pressed "End Current Period" (or ran !raffleend). Ending
            # the raffle is an explicit decision and auto-renew must not undo
            # it — previously the flag didn't exist, so renewal fired as soon as
            # the ORIGINAL end_date passed and resurrected the raffle. Starting
            # the next period is now the operator's call.
            logger.debug(
                f"[Server {self.discord_server_id}] Latest period was ended manually — "
                f"staying dormant (start the next one from the dashboard)"
            )
            return None

        if datetime.now() <= latest_end:
            # Drawn early: the winner is picked but the period's scheduled end
            # hasn't arrived. Renew on schedule, not immediately.
            return None

        logger.info(
            f"[Server {self.discord_server_id}] Auto-renew ON — latest period's end passed, creating next monthly period"
        )
        return self._create_renewal_period()

    def _has_ended_manually_column(self):
        """Whether raffle_periods.ended_manually exists yet (cached per instance).

        Lets the bot run safely against a database that hasn't received the
        migration (the dashboard applies it on boot, and the two services deploy
        independently). Absent column ⇒ treated as "nothing ended manually",
        which is the pre-migration behaviour.
        """
        cached = getattr(self, "_ended_manually_column", None)
        if cached is not None:
            return cached
        try:
            with self.engine.begin() as conn:
                found = conn.execute(
                    text(
                        """
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'raffle_periods' AND column_name = 'ended_manually'
                        """
                    )
                ).fetchone()
            present = bool(found)
        except Exception as e:
            logger.warning(f"Could not check for raffle_periods.ended_manually: {e}")
            present = False
        if not present:
            logger.info(
                "raffle_periods.ended_manually is missing — manual-end protection is inactive "
                "until the dashboard migration runs"
            )
        # Only cache a positive result: once the migration lands mid-process the
        # next check picks it up instead of staying degraded until a restart.
        if present:
            self._ended_manually_column = True
        return present

    def _latest_period_dates(self):
        """(start_date, end_date) of this server's most recent period, or None.

        Used to carry the operator's chosen period LENGTH into the next period.
        """
        if self.discord_server_id is not None:
            query = text(
                """
                SELECT start_date, end_date FROM raffle_periods
                WHERE discord_server_id = :server_id
                ORDER BY end_date DESC
                LIMIT 1
                """
            )
            params = {"server_id": self.discord_server_id}
        else:
            query = text("SELECT start_date, end_date FROM raffle_periods ORDER BY end_date DESC LIMIT 1")
            params = {}

        try:
            with self.engine.begin() as conn:
                row = conn.execute(query, params).fetchone()
        except Exception as e:
            logger.warning(f"Could not read latest period dates: {e}")
            return None
        if not row or row[0] is None or row[1] is None:
            return None

        start, end = row[0], row[1]
        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        if isinstance(end, str):
            end = datetime.fromisoformat(end)
        return start, end

    def _next_period_window(self, previous_start, previous_end, now=None):
        """Start/end for the period that follows (previous_start, previous_end).

        The new period keeps the LENGTH the operator configured — a 7-day period
        renews as 7 days, a 2-week period as 2 weeks. Renewal used to always
        build a calendar month here, which silently discarded the dates chosen
        on the dashboard and made short periods impossible to sustain.

        A period that is (within a day of) a whole calendar month keeps calendar
        semantics — it rolls to the 1st..end-of-next-month — so existing monthly
        raffles land on month boundaries rather than drifting by the 28/30/31-day
        difference.
        """
        now = now or datetime.now()
        start = previous_end + timedelta(seconds=1)

        if previous_start is not None and _spans_whole_month(previous_start, previous_end):
            # Step off the PREVIOUS month rather than off `start`: a date-only
            # end (Aug 31 00:00:00, what the dashboard's date inputs produce)
            # puts `start` inside the same month, and snapping it to day 1 would
            # recreate the month that just finished.
            month_start = _next_month_start(previous_start)
            return month_start, _end_of_month(month_start)

        duration = previous_end - previous_start if previous_start is not None else None
        if duration is None or duration <= timedelta(0):
            # Degenerate/unknown length: fall back to the next calendar month
            # rather than creating a zero-length period that ends immediately.
            month_start = _next_month_start(now)
            return month_start, _end_of_month(month_start)

        return start, start + duration

    def _create_renewal_period(self):
        """Create the next period, preserving the previous period's length.

        Falls back to a month starting NOW when this server has no previous
        period to take a length from.

        Never backdates. Renewal used to start the period at "the 1st of the
        current month" whatever the date actually was, so a renewal that fired
        on the 17th produced a period that had already been running for 16 days
        and ended two weeks later — the shape of the bad row this fixed.
        """
        try:
            now = datetime.now()

            previous = self._latest_period_dates()
            if previous:
                start, end = self._next_period_window(previous[0], previous[1], now=now)
            else:
                start = now
                end = _end_of_month(now)

            # A renewal recovering late (service down, a period left un-renewed
            # for days) must not open a period that already started in the past:
            # a monthly renewal firing on the 17th would otherwise create an
            # Aug 1 - Aug 31 window that is already half spent, and viewers get
            # a "month" with two weeks in it. Re-anchor to now, keeping LENGTH.
            #
            # Skips the whole-calendar-month case ONLY when it is genuinely
            # starting now-ish (renewal fired on time at the boundary), which is
            # the common path and the one that must stay month-aligned.
            if start < now - timedelta(hours=1):
                length = end - start
                start = now
                end = start + (length if length > timedelta(0) else timedelta(days=30))
                logger.info(
                    f"[Server {self.discord_server_id}] Late renewal — anchoring the new period to now "
                    f"({start:%b %d} - {end:%b %d, %Y}) instead of backdating its start"
                )

            period_id = create_new_period(self.engine, start, end, discord_server_id=self.discord_server_id)
            logger.info(
                f"✅ Auto-created period #{period_id} ({start.strftime('%b %d')} - {end.strftime('%b %d, %Y')})"
            )

            return {
                "old_period_id": None,
                "old_period_start": None,
                "old_period_end": None,
                "winner_drawn": False,
                "winner_info": None,
                "new_period_id": period_id,
                "transition_time": now,
            }
        except Exception as e:
            logger.error(f"Failed to create monthly period: {e}")
            return None

    def _draw_winner_for_period(self, period):
        """Draw winner for the given period"""
        try:
            winner = self.raffle_draw.draw_winner(
                period_id=period["id"],
                prize_description="Monthly Raffle Prize",
                drawn_by_discord_id=None,  # Automatic draw
            )

            if winner:
                logger.info(f"🎉 Winner drawn for period #{period['id']}: {winner['winner_kick_name']}")
                return winner
            else:
                logger.warning(f"No participants to draw from for period #{period['id']}")
                return None
        except Exception as e:
            logger.error(f"Failed to draw winner: {e}")
            return None

    def _transition_to_new_period(self, old_period):
        """
        Transition from old period to new period

        Args:
            old_period: Current period that's ending

        Returns:
            dict: Transition details
        """
        try:
            transition_info = {
                "old_period_id": old_period["id"],
                "old_period_start": old_period["start_date"],
                "old_period_end": old_period["end_date"],
                "winner_drawn": False,
                "winner_info": None,
                "new_period_id": None,
                "transition_time": datetime.now(),
            }

            # Step 1: Draw winner if auto-draw enabled and not already drawn
            if self.auto_draw and not old_period.get("winner_discord_id"):
                logger.info(f"🎲 Auto-drawing winner for period #{old_period['id']}...")

                winner = self.raffle_draw.draw_winner(
                    period_id=old_period["id"],
                    prize_description="Monthly Raffle Prize",
                    drawn_by_discord_id=None,  # Automatic draw
                )

                if winner:
                    transition_info["winner_drawn"] = True
                    transition_info["winner_info"] = winner
                    logger.info(f"🎉 Winner drawn: {winner['winner_kick_name']}")
                else:
                    logger.warning("No participants to draw from")

            # Step 2: Close the old period
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                    UPDATE raffle_periods
                    SET status = 'ended'
                    WHERE id = :period_id
                """
                    ),
                    {"period_id": old_period["id"]},
                )

            logger.info(f"✅ Period #{old_period['id']} closed")

            # Step 3: Roll over into a new period ONLY when auto_renew is on.
            # When off (the default), the raffle goes dormant after the period
            # ends — the user must start the next one manually. This is the
            # core opt-in behavior; it mirrors the wager-leaderboard rollover.
            if not self.auto_renew:
                logger.info(
                    f"[Server {self.discord_server_id}] Auto-renew OFF — period #{old_period['id']} ended, no new period created"
                )
                return transition_info

            # IMPORTANT: Don't clear tickets if auto-draw is disabled and no winner drawn yet
            # This allows manual winner drawing after period ends
            clear_tickets = self.auto_draw or old_period.get("winner_discord_id") is not None

            if not clear_tickets:
                logger.warning(f"⚠️  Tickets preserved! Winner NOT drawn for period #{old_period['id']}")
                logger.warning("   Use !raffledraw or dashboard to draw winner before cleanup")

            # The next period keeps the LENGTH the operator configured for the
            # one that just ended (a monthly period still rolls to the 1st).
            old_start = old_period["start_date"]
            old_end = old_period["end_date"]
            if isinstance(old_start, str):
                old_start = datetime.fromisoformat(old_start)
            if isinstance(old_end, str):
                old_end = datetime.fromisoformat(old_end)

            start, end = self._next_period_window(old_start, old_end)

            new_period_id = create_new_period(
                self.engine, start, end, clear_tickets=clear_tickets, discord_server_id=self.discord_server_id
            )
            transition_info["new_period_id"] = new_period_id

            logger.info(
                f"✅ New period #{new_period_id} created ({start.strftime('%b %d')} - {end.strftime('%b %d, %Y')})"
            )
            logger.info("Auto-renew ON — next period rolled over automatically")

            return transition_info

        except Exception as e:
            logger.error(f"Failed to transition periods: {e}")
            import traceback

            traceback.print_exc()
            return None

    async def announce_winner(self, winner_info):
        """
        Announce raffle winner to Discord channel

        Args:
            winner_info: Winner details from draw_winner()
        """
        if not self.bot or not self.announcement_channel_id:
            logger.warning("Cannot announce winner: bot or channel not configured")
            return

        try:
            channel = self.bot.get_channel(self.announcement_channel_id)
            if not channel:
                logger.error(f"Announcement channel {self.announcement_channel_id} not found")
                return

            # Try to mention winner if possible
            try:
                discord_user = await self.bot.fetch_user(winner_info["winner_discord_id"])
                mention = discord_user.mention
            except:
                mention = winner_info["winner_kick_name"]

            message = f"""
🎉 **MONTHLY RAFFLE WINNER!** 🎉

Congratulations {mention}!

**Winner**: {winner_info['winner_kick_name']}
**Tickets**: {winner_info['winner_tickets']:,} out of {winner_info['total_tickets']:,}
**Win Probability**: {winner_info['win_probability']:.2f}%

Please contact an admin to claim your prize! 🎊
            """

            await channel.send(message.strip())
            logger.info(f"📢 Winner announcement sent to channel {self.announcement_channel_id}")

        except Exception as e:
            logger.error(f"Failed to announce winner: {e}")

    async def announce_new_period(self, new_period_id, start_date, end_date):
        """
        Announce new raffle period to Discord channel

        Args:
            new_period_id: New period ID
            start_date: Period start date
            end_date: Period end date
        """
        if not self.bot or not self.announcement_channel_id:
            return

        try:
            channel = self.bot.get_channel(self.announcement_channel_id)
            if not channel:
                return

            watchtime_tickets, gifted_sub_tickets, wager_tickets = get_ticket_reward_settings(
                self.engine, self.discord_server_id, logger
            )

            # Resolve the configured wager platform + campaign code for this server.
            platform = "Shuffle"
            code = ""
            getter = getattr(self.bot, "get_guild_settings", None)
            if callable(getter) and self.discord_server_id is not None:
                settings = getter(self.discord_server_id)
                platform = platform_display_name(settings)
                # Per-platform key (howl_campaign_code vs shuffle_campaign_code);
                # "" when unset, so the "(code 'x')" clause is dropped instead of
                # advertising another tenant's code.
                code = platform_campaign_code(settings)
            code_suffix = f" (code '{code}')" if code else ""

            # Resolve the per-server display number from the global period id.
            period_display = new_period_id
            try:
                with self.engine.begin() as conn:
                    num_row = conn.execute(
                        text("SELECT COALESCE(period_number, id) FROM raffle_periods WHERE id = :pid"),
                        {"pid": new_period_id},
                    ).fetchone()
                    if num_row:
                        period_display = num_row[0]
            except Exception:
                pass

            message = f"""
🎰 **NEW RAFFLE PERIOD STARTED!** 🎰

**Period**: #{period_display}
**Duration**: {start_date.strftime('%B %d')} - {end_date.strftime('%B %d, %Y')}

**How to Earn Tickets**:
• Watch streams: {watchtime_tickets} tickets per hour
• Gift subs: {gifted_sub_tickets} tickets per sub
• Wager on {platform}{code_suffix}: {wager_tickets} tickets per $1000

Use `!tickets` to check your balance!
Good luck! 🍀
            """

            await channel.send(message.strip())
            logger.info(f"📢 New period announcement sent to channel {self.announcement_channel_id}")

        except Exception as e:
            logger.error(f"Failed to announce new period: {e}")


# Store active scheduler tasks to prevent garbage collection
_active_scheduler_tasks = {}


async def setup_raffle_scheduler(bot, engine, auto_draw=False, announcement_channel_id=None, discord_server_id=None):
    """
    Setup automatic raffle period management as a Discord bot task

    Args:
        bot: Discord bot instance
        engine: SQLAlchemy database engine
        auto_draw: Whether to auto-draw winners at period end
        announcement_channel_id: Channel ID for announcements
        discord_server_id: Discord server ID for multiserver support

    Returns:
        RaffleScheduler instance
    """
    scheduler = RaffleScheduler(
        engine=engine,
        bot=bot,
        auto_draw=auto_draw,
        announcement_channel_id=announcement_channel_id,
        discord_server_id=discord_server_id,
    )

    @tasks.loop(minutes=1)  # Check every minute
    async def check_raffle_period():
        """Check every minute for: winner drawing (10 min before end) and period transitions"""
        # Tag this scheduler tick's logging with the server (runs in its own Task).
        _guild = bot.get_guild(int(discord_server_id)) if discord_server_id else None
        set_server(discord_server_id, _guild.name if _guild else None)
        try:
            now = datetime.now()

            # Log that the task is running (debug level to avoid spam)
            logger.debug(f"🔄 [Server {discord_server_id}] Raffle scheduler check running at {now}")

            # Always check for winner drawing and period transitions
            transition = scheduler.check_period_transition()

            # If cleanup was performed, update leaderboard
            if transition and transition.get("cleanup_performed"):
                if hasattr(bot, "auto_leaderboards") and bot.auto_leaderboards.get(discord_server_id):
                    logger.info(f"📊 [Server {discord_server_id}] Updating leaderboard after cleanup...")
                    await bot.auto_leaderboards[discord_server_id].update_leaderboard()
                else:
                    logger.info(
                        f"📊 [Server {discord_server_id}] Leaderboard not initialized yet, skipping update after cleanup"
                    )
                return

            # Announce winner if drawn — independent of rollover. With
            # auto-renew OFF the transition ends the period WITHOUT creating a
            # successor (new_period_id=None), and the drawn winner must still
            # be announced.
            if transition and transition.get("winner_drawn") and transition.get("winner_info"):
                await scheduler.announce_winner(transition["winner_info"])

            # Process period transition whenever it happens (not just at midnight on 1st)
            if transition and transition.get("new_period_id"):
                logger.info(f"📊 [Server {discord_server_id}] Period transition completed:")
                if transition["old_period_id"]:
                    logger.info(f"   Old period: #{transition['old_period_id']}")
                logger.info(f"   New period: #{transition['new_period_id']}")

                # Announce new period
                new_period = get_current_period(engine, discord_server_id=scheduler.discord_server_id)
                if new_period:
                    await scheduler.announce_new_period(
                        new_period["id"], new_period["start_date"], new_period["end_date"]
                    )

                # Update the leaderboard immediately after period transition
                if hasattr(bot, "auto_leaderboards") and bot.auto_leaderboards.get(discord_server_id):
                    logger.info(f"🔄 [Server {discord_server_id}] Updating leaderboard after period transition...")
                    await bot.auto_leaderboards[discord_server_id].update_leaderboard()

        except Exception as e:
            logger.error(f"[Server {discord_server_id}] Error in raffle period check task: {e}")
            import traceback

            traceback.print_exc()

    # Store the task to prevent garbage collection
    _active_scheduler_tasks[discord_server_id] = check_raffle_period

    # Start the task
    check_raffle_period.start()
    logger.debug(f"✅ [Server {discord_server_id}] Raffle scheduler task started (checks every minute)")

    return scheduler
