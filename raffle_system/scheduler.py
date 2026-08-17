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
        auto_renew is ON. Rolls into a fresh monthly period once the latest
        period's SCHEDULED end has passed:
          • no periods at all → raffle never started, stay dormant
          • latest end_date still in the future (drawn/ended early) → stay
            dormant until the scheduled end passes, then renew
          • latest end_date passed → create the current month's period

        Returns the transition dict from _create_monthly_period, or None.
        """
        if self.discord_server_id is not None:
            query = text(
                """
                SELECT end_date FROM raffle_periods
                WHERE discord_server_id = :server_id
                ORDER BY end_date DESC
                LIMIT 1
            """
            )
            params = {"server_id": self.discord_server_id}
        else:
            query = text("SELECT end_date FROM raffle_periods ORDER BY end_date DESC LIMIT 1")
            params = {}

        with self.engine.begin() as conn:
            row = conn.execute(query, params).fetchone()

        if not row:
            logger.debug(f"[Server {self.discord_server_id}] Auto-renew ON but no periods exist — staying dormant")
            return None

        latest_end = row[0]
        if isinstance(latest_end, str):
            latest_end = datetime.fromisoformat(latest_end)

        if datetime.now() <= latest_end:
            # Ended early (draw or manual end). Renew when the schedule says the
            # period would have ended, not immediately — otherwise "End Current
            # Period" would be undone within a minute.
            return None

        logger.info(
            f"[Server {self.discord_server_id}] Auto-renew ON — latest period's end passed, creating next monthly period"
        )
        return self._create_monthly_period()

    def _create_monthly_period(self):
        """Create a new monthly raffle period starting on 1st of current month"""
        try:
            now = datetime.now()

            # Start on 1st of current month at midnight
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

            # End on last day of month at 23:59:59
            # Get first day of next month, then subtract 1 second
            if start.month == 12:
                next_month = start.replace(year=start.year + 1, month=1)
            else:
                next_month = start.replace(month=start.month + 1)

            end = next_month - timedelta(seconds=1)

            period_id = create_new_period(self.engine, start, end, discord_server_id=self.discord_server_id)
            logger.info(
                f"✅ Auto-created monthly period #{period_id} ({start.strftime('%b %d')} - {end.strftime('%b %d, %Y')})"
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

            # New period starts on 1st of next month
            old_end = old_period["end_date"]
            if isinstance(old_end, str):
                old_end = datetime.fromisoformat(old_end)

            # Calculate next month
            if old_end.month == 12:
                start = old_end.replace(
                    year=old_end.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
                )
            else:
                start = old_end.replace(month=old_end.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)

            # End on last day of that month at 23:59:59
            if start.month == 12:
                next_month = start.replace(year=start.year + 1, month=1)
            else:
                next_month = start.replace(month=start.month + 1)

            end = next_month - timedelta(seconds=1)

            new_period_id = create_new_period(
                self.engine, start, end, clear_tickets=clear_tickets, discord_server_id=self.discord_server_id
            )
            transition_info["new_period_id"] = new_period_id

            logger.info(
                f"✅ New monthly period #{new_period_id} created ({start.strftime('%b %d')} - {end.strftime('%b %d, %Y')})"
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
