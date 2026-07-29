"""
Redis Subscriber for Discord Bot
Listens for events from the Admin Dashboard and triggers bot actions

Add this to your bot's main file after the bot is initialized:

    from redis_subscriber import start_redis_subscriber

    @bot.event
    async def on_ready():
        print(f'Bot logged in as {bot.user}')
        # Start Redis subscriber
        asyncio.create_task(start_redis_subscriber(bot))
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime

import discord
import redis
from sqlalchemy import create_engine, text  # type: ignore

from features.games.guess_the_balance import gtb_rank_marker
from utils.log_context import server_context
from utils.redis_signing import signing_enabled, verify_payload
from utils.server_urls import get_server_public_page_url

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = None
if DATABASE_URL:
    try:
        engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    except Exception as e:
        logger.warning(f"⚠️  Failed to initialize DB engine in redis_subscriber: {e}")
        engine = None

_engine = None


def get_engine():
    """Return a single shared SQLAlchemy engine for the bot database.

    Reuses the module-level ``engine`` created at import when available, otherwise
    lazily builds and caches one. Never creates a new engine/pool per call - event
    handlers used to ``create_engine`` per raffle draw / leaderboard post, leaking a
    connection pool every time.
    """
    global _engine
    if engine is not None:
        return engine
    if _engine is not None:
        return _engine

    database_url = os.getenv("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set; cannot query point_settings")

    # SQLAlchemy expects postgresql://
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    _engine = create_engine(database_url, pool_pre_ping=True)
    return _engine


def build_raffle_kick_message(winner: dict, prize_description: str) -> str:
    """Build the Kick-chat announcement line for a raffle winner.

    Shared by the post-animation reveal (handle_raffle_event → animation_complete)
    and the on-demand re-announce (handle_raffle_event → announce_winner) so both
    paths produce identical chat output.
    """
    name = winner.get("winner_kick_name", "Unknown")
    ticket = winner.get("winning_ticket", "?")
    prize = prize_description or "Monthly Raffle Prize"
    return f"🎉 Raffle Winner: {name} won {prize}! Ticket #{ticket}. Congratulations! 🎊"


class RedisSubscriber:
    def __init__(self, bot, send_message_callback=None):
        self.bot = bot
        self.send_message_callback = send_message_callback
        self.redis_url = os.getenv("REDIS_URL")
        self.enabled = False
        self.last_shop_sync = 0  # Timestamp for debouncing shop sync
        # Per-(guild,item) cooldown for restock DM fan-outs, so a burst of
        # item edits that each cross 0->positive can't spam viewers.
        self._last_restock_dm = {}
        # Per-guild asyncio.Event fired when the slot-request-picker OBS widget
        # reports its reveal animation landed (action="slot_reveal_landed").
        # A pending pick that opted into the widget delay waits on this Event
        # (with a backstop timeout) so the chat/Discord announcement fires in
        # sync with the on-stream reveal instead of on a fixed timer.
        self._slot_reveal_events = {}

        # Ensure point_sales has columns for tracking Discord order notification messages
        try:
            if engine is not None:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                        ALTER TABLE point_sales
                        ADD COLUMN IF NOT EXISTS discord_notification_channel_id BIGINT
                    """
                        )
                    )
                    conn.execute(
                        text(
                            """
                        ALTER TABLE point_sales
                        ADD COLUMN IF NOT EXISTS discord_notification_message_id BIGINT
                    """
                        )
                    )
        except Exception as e:
            logger.warning(f"⚠️  Failed to ensure point_sales notification columns: {e}")

        if self.redis_url:
            if "://" not in self.redis_url:
                self.redis_url = f"redis://{self.redis_url}"
            try:
                self.client = redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5,
                )
                self.pubsub = self.client.pubsub()
                self.enabled = True
                logger.debug("✅ Redis subscriber initialized")
            except Exception as e:
                logger.warning(f"⚠️  Redis unavailable: {e}")
                self.enabled = False
        else:
            logger.warning("⚠️  REDIS_URL not set, dashboard events will not be received")

    async def announce_in_chat(self, message, guild_id=None):
        """Send a message to the server's active stream chat(s).

        Routes through send_stream_message (from __main__) so the announcement
        fans out to whichever platform(s) the server runs (Kick and/or Twitch).
        Falls back to the Kick-only callback if send_stream_message is unavailable.

        Args:
            message: Message to send
            guild_id: Discord server ID (if None, uses first available guild)
        """
        try:
            # If no guild_id provided, try to get from bot's first guild
            if guild_id is None and hasattr(self.bot, "guilds") and self.bot.guilds:
                guild_id = self.bot.guilds[0].id
            if guild_id is not None:
                guild_id = int(guild_id)

            # Prefer the platform-aware fan-out sender from the running entrypoint.
            import sys as _sys

            _main = _sys.modules.get("__main__")
            send_stream_message = getattr(_main, "send_stream_message", None)
            if send_stream_message is not None:
                sent = await send_stream_message(message, guild_id=guild_id)
                if sent:
                    logger.info(f"💬 Announced to active stream chat(s): {message}")
                else:
                    logger.warning(f"⚠️ Announcement returned no successful send (guild={guild_id}): {message}")
                return

            if self.send_message_callback:
                await self.send_message_callback(message, guild_id=guild_id)
                logger.info(f"💬 Sent to chat (fallback): {message}")
            else:
                logger.info(f"ℹ️  No chat sender available: {message}")
        except Exception as e:
            logger.warning(f"⚠️ Chat announcement not sent: {e}")
            logger.info(f"   Message was: {message}")
            import traceback

            traceback.print_exc()

    # ❌ WEBHOOK HANDLING DISABLED - Using direct Pusher WebSocket instead
    # NOTE: The previous webhook handler implementation was intentionally removed
    # because it was disabled and must not run at import/class-definition time.

    def _slot_reveal_event(self, guild_id):
        """Get (creating if needed) the per-guild reveal Event."""
        ev = self._slot_reveal_events.get(guild_id)
        if ev is None:
            ev = asyncio.Event()
            self._slot_reveal_events[guild_id] = ev
        return ev

    async def _wait_for_slot_reveal(self, guild_id, backstop_seconds):
        """Wait until the slot-picker widget reports its reveal landed, or until
        the backstop timeout elapses — whichever comes first. Returns True if the
        widget trigger fired, False if we fell through on the timeout.

        The Event is cleared before waiting so a stale set from a previous pick
        can't short-circuit this one, and removed afterward so guilds that stop
        using the widget don't accumulate Events."""
        ev = self._slot_reveal_event(guild_id)
        ev.clear()
        try:
            await asyncio.wait_for(ev.wait(), timeout=backstop_seconds)
            logger.info(f"✅ [BOT-REDIS] Slot reveal trigger received for guild {guild_id}; announcing now")
            return True
        except asyncio.TimeoutError:
            logger.info(
                f"⏰ [BOT-REDIS] No slot reveal trigger within {backstop_seconds}s for guild {guild_id}; "
                f"announcing on backstop"
            )
            return False
        finally:
            self._slot_reveal_events.pop(guild_id, None)

    async def handle_slot_requests_event(self, action, data):
        """Handle slot request events from dashboard"""
        logger.info(f"📥 Slot Requests Event: {action}")

        # Extract discord_server_id from event data and convert to int
        guild_id = data.get("discord_server_id")
        if guild_id is not None:
            guild_id = int(guild_id)

        if action == "toggle":
            enabled = data.get("enabled")
            # Announce in Kick chat
            if enabled:
                await self.announce_in_chat(
                    '✅ Slot requests are now ENABLED! Use !call "slot name" or !sr "slot name"', guild_id=guild_id
                )
            else:
                await self.announce_in_chat("❌ Slot requests have been DISABLED", guild_id=guild_id)

            # Post update to Discord slot calls channel if available
            if hasattr(self.bot, "slot_calls_channel_id") and self.bot.slot_calls_channel_id:
                try:
                    channel = self.bot.get_channel(self.bot.slot_calls_channel_id)
                    if channel:
                        emoji = "✅" if enabled else "❌"
                        status = "ENABLED" if enabled else "DISABLED"
                        await channel.send(f"{emoji} **Slot Requests {status}** (changed via dashboard)")
                except Exception as e:
                    logger.info(f"Failed to send Discord notification: {e}")

            # Update guild-specific tracker (preferred approach for multi-server)
            tracker = None
            if guild_id and hasattr(self.bot, "slot_call_trackers_by_guild"):
                tracker = self.bot.slot_call_trackers_by_guild.get(guild_id)

            # Fallback to global tracker if no guild-specific one exists
            if not tracker and hasattr(self.bot, "slot_call_tracker"):
                tracker = self.bot.slot_call_tracker

            if tracker:
                try:
                    # Reload enabled state from database for the correct guild_id
                    # The tracker's server_id should already be set correctly
                    tracker.enabled = tracker._load_enabled_state()
                    tracker.max_requests_per_user = tracker._load_max_requests()
                    logger.info(
                        f"✅ Updated slot_call_tracker enabled state to: {tracker.enabled} for server {guild_id}"
                    )
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update slot_call_tracker: {e}")

            # Update panel tracker (if panel exists for this guild)
            panel = None
            if guild_id and hasattr(self.bot, "slot_panels_by_guild"):
                panel = self.bot.slot_panels_by_guild.get(guild_id)

            if panel:
                try:
                    # Refresh tracker state from database before updating panel
                    if hasattr(panel, "tracker") and panel.tracker:
                        panel.tracker.enabled = panel.tracker._load_enabled_state()
                        panel.tracker.max_requests_per_user = panel.tracker._load_max_requests()
                        logger.info(
                            f"✅ Updated panel tracker enabled state to: {panel.tracker.enabled} for server {guild_id}"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update tracker via panel: {e}")

            # Update Discord panel (if it exists for this guild)
            if panel:
                try:
                    logger.info(
                        f"🔍 Panel IDs: message_id={panel.panel_message_id}, channel_id={panel.panel_channel_id}"
                    )
                    success = await panel.update_panel(force=True)
                    if success:
                        logger.info(f"✅ Slot request panel updated in Discord for guild {guild_id}")
                    else:
                        logger.info(
                            f"ℹ️  Slot panel not created yet for guild {guild_id} (Discord admin: use !slotpanel to create)"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update slot panel: {e}")
                    import traceback

                    traceback.print_exc()

        elif action == "pick":
            slot_id = data.get("id")
            slot_call = data.get("slot_call")
            username = data.get("username")
            guild_id = data.get("discord_server_id")
            if guild_id is not None:
                guild_id = int(guild_id)
            delay_announcement = data.get("delay_announcement", 0)

            logger.info(f"📥 [BOT-REDIS] Received 'pick' event with delay_announcement={delay_announcement}")

            # When the OBS widget is enabled, `delay_announcement` carries the
            # BACKSTOP timeout (seconds). Wait for the widget's reveal trigger and
            # announce the instant it lands (in sync with the on-stream reveal);
            # fall through on the backstop if the widget never reports (closed /
            # crashed / refreshed mid-spin). When 0, announce immediately.
            if delay_announcement > 0:
                logger.info(
                    f"⏳ [BOT-REDIS] Waiting for slot-picker reveal trigger (backstop {delay_announcement}s) for overlay animation"
                )
                await self._wait_for_slot_reveal(guild_id, delay_announcement)
            else:
                logger.info(f"⚡ [BOT-REDIS] No overlay, announcing immediately")

            # Announce the picked slot in Kick chat (tag the user)
            await self.announce_in_chat(f"🎰 PICKED: {slot_call} (requested by @{username})", guild_id=guild_id)

            # Post to Discord
            if hasattr(self.bot, "slot_calls_channel_id") and self.bot.slot_calls_channel_id:
                try:
                    channel = self.bot.get_channel(self.bot.slot_calls_channel_id)
                    if channel:
                        await channel.send(f"🎰 **PICKED**: {slot_call} (requested by {username})")
                except Exception as e:
                    logger.info(f"Failed to send Discord notification: {e}")

            # Update Discord panel for this guild
            panel = None
            if guild_id and hasattr(self.bot, "slot_panels_by_guild"):
                panel = self.bot.slot_panels_by_guild.get(guild_id)

            if panel:
                try:
                    # Refresh tracker state from database before updating panel
                    if hasattr(panel, "tracker") and panel.tracker:
                        panel.tracker.enabled = panel.tracker._load_enabled_state()  # Reload enabled state from DB
                        panel.tracker.max_requests_per_user = (
                            panel.tracker._load_max_requests()
                        )  # Reload max requests too
                    success = await panel.update_panel(force=True)
                    if success:
                        logger.info(f"✅ Slot request panel updated in Discord for guild {guild_id}")
                    else:
                        logger.info(f"ℹ️  Slot panel not created yet for guild {guild_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update slot panel: {e}")

        elif action == "pick_with_reward":
            slot_id = data.get("slot_id")
            slot_call = data.get("slot_call")
            username = data.get("username")
            reward_type = data.get("reward_type")
            reward_amount = data.get("reward_amount")
            guild_id = data.get("discord_server_id")
            if guild_id is not None:
                guild_id = int(guild_id)
            delay_announcement = data.get("delay_announcement", 0)

            logger.info(
                f"📥 [BOT-REDIS] Received 'pick_with_reward' event with delay_announcement={delay_announcement}"
            )

            # Format reward type for display
            reward_type_display = "Bonus Buy" if reward_type == "bonus_buy" else reward_type.capitalize()

            # See the 'pick' handler: with the OBS widget on, `delay_announcement`
            # is the BACKSTOP timeout — wait for the widget reveal trigger, else
            # fall through on timeout. When 0, announce immediately.
            if delay_announcement > 0:
                logger.info(
                    f"⏳ [BOT-REDIS] Waiting for slot-picker reveal trigger (backstop {delay_announcement}s) for overlay animation"
                )
                await self._wait_for_slot_reveal(guild_id, delay_announcement)
            else:
                logger.info(f"⚡ [BOT-REDIS] No overlay, announcing immediately")

            # Announce the picked slot WITH reward in Kick chat (tag the user)
            amount = float(reward_amount)
            await self.announce_in_chat(
                f"🎰 PICKED: {slot_call} (requested by @{username}) 💰 WON ${amount:.2f} {reward_type_display}!",
                guild_id=guild_id,
            )

            # Post to Discord
            if hasattr(self.bot, "slot_calls_channel_id") and self.bot.slot_calls_channel_id:
                try:
                    channel = self.bot.get_channel(self.bot.slot_calls_channel_id)
                    if channel:
                        await channel.send(
                            f"🎰 **PICKED**: {slot_call} (requested by {username})\n💰 **WON**: ${amount:.2f} {reward_type_display}!"
                        )
                except Exception as e:
                    logger.info(f"Failed to send Discord notification: {e}")

            # Update Discord panel for this guild
            panel = None
            if guild_id and hasattr(self.bot, "slot_panels_by_guild"):
                panel = self.bot.slot_panels_by_guild.get(guild_id)

            if panel:
                try:
                    # Refresh tracker state from database before updating panel
                    if hasattr(panel, "tracker") and panel.tracker:
                        panel.tracker.enabled = panel.tracker._load_enabled_state()  # Reload enabled state from DB
                        panel.tracker.max_requests_per_user = (
                            panel.tracker._load_max_requests()
                        )  # Reload max requests too
                    success = await panel.update_panel(force=True)
                    if success:
                        logger.info(f"✅ Slot request panel updated in Discord for guild {guild_id}")
                    else:
                        logger.info(f"ℹ️  Slot panel not created yet for guild {guild_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to update slot panel: {e}")

        elif action == "slot_reveal_landed":
            # The slot-request-picker OBS widget reported its reveal animation
            # landed. Fire the per-guild Event so a pending pick that's waiting
            # (see _wait_for_slot_reveal) announces immediately, in sync with the
            # on-stream reveal. Harmless if no pick is waiting (nothing listens).
            guild_id = data.get("discord_server_id")
            if guild_id is not None:
                try:
                    guild_id = int(guild_id)
                except (TypeError, ValueError):
                    guild_id = None
            logger.info(f"🎬 [BOT-REDIS] Slot reveal landed for guild {guild_id}")
            if guild_id is not None:
                self._slot_reveal_event(guild_id).set()

        elif action == "update_max":
            max_requests = data.get("max_requests")
            guild_id = data.get("discord_server_id")

            # Convert guild_id to int (may come as string from JSON)
            if guild_id is not None:
                guild_id = int(guild_id)

            logger.info(f"📥 Updated max slot requests to {max_requests} for guild {guild_id}")

            # Update the per-guild tracker if available
            if guild_id and hasattr(self.bot, "slot_call_trackers_by_guild"):
                tracker = self.bot.slot_call_trackers_by_guild.get(guild_id)
                if tracker:
                    try:
                        tracker.max_requests_per_user = tracker._load_max_requests()
                        logger.info(
                            f"✅ Updated slot_call_tracker max_requests for guild {guild_id}: {tracker.max_requests_per_user}"
                        )
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update slot_call_tracker for guild {guild_id}: {e}")

            # Update Discord panel for the specific guild
            if guild_id and hasattr(self.bot, "slot_panels_by_guild"):
                panel = self.bot.slot_panels_by_guild.get(guild_id)
                if panel:
                    try:
                        # Refresh tracker state from database before updating panel
                        if hasattr(panel, "tracker") and panel.tracker:
                            panel.tracker.max_requests_per_user = panel.tracker._load_max_requests()
                            logger.info(
                                f"✅ Updated panel tracker max_requests for guild {guild_id}: {panel.tracker.max_requests_per_user}"
                            )
                        success = await panel.update_panel(force=True)
                        if success:
                            logger.info(f"✅ Slot request panel updated in Discord for guild {guild_id}")
                        else:
                            logger.info(f"ℹ️  Slot panel not created yet for guild {guild_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update slot panel for guild {guild_id}: {e}")

    async def handle_timed_messages_event(self, action, data):
        """Handle timed message events from dashboard"""
        logger.info(f"📥 Timed Messages Event: {action}")

        # Extract discord_server_id from event data
        guild_id = data.get("discord_server_id")

        # Convert guild_id to int (may come as string from JSON)
        if guild_id is not None:
            guild_id = int(guild_id)

        # Reload timed messages from database for the specific guild
        if hasattr(self.bot, "timed_messages_managers") and guild_id:
            manager = self.bot.timed_messages_managers.get(guild_id)
            if manager:
                try:
                    manager.reload_messages()
                    logger.info(f"✅ Timed messages reloaded for guild {guild_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to reload timed messages: {e}")
        elif hasattr(self.bot, "timed_messages_manager") and self.bot.timed_messages_manager:
            # Fallback for backwards compatibility (single global manager)
            try:
                self.bot.timed_messages_manager.reload_messages()
                logger.info(f"✅ Timed messages reloaded from database")
            except Exception as e:
                logger.warning(f"⚠️ Failed to reload timed messages: {e}")

        if action == "create":
            message_id = data.get("id")
            message = data.get("message")
            interval = data.get("interval_minutes")
            logger.info(f"✅ Created timed message #{message_id}: {message} (every {interval}m)")

        elif action == "update":
            message_id = data.get("id")
            logger.info(f"✅ Updated timed message #{message_id}")

        elif action == "delete":
            message_id = data.get("id")
            logger.info(f"✅ Deleted timed message #{message_id}")

        elif action == "toggle":
            message_id = data.get("id")
            enabled = data.get("enabled")
            status = "enabled" if enabled else "disabled"
            logger.info(f"✅ Timed message #{message_id} {status}")

    async def handle_gtb_event(self, action, data):
        """Handle Guess the Balance events from dashboard"""
        logger.info(f"📥 GTB Event: {action}")

        # Extract discord_server_id from event data and convert to int
        guild_id = data.get("discord_server_id")
        if guild_id is not None:
            guild_id = int(guild_id)

        if action == "open":
            session_id = data.get("session_id")
            opened_by = data.get("opened_by")
            winner_count = data.get("winner_count", 3) or 3
            # "winner" vs "winners" so a count of 1 reads correctly.
            winner_label = "winner" if winner_count == 1 else "winners"
            # Announce in Kick chat
            await self.announce_in_chat(
                f"💰 Guess the Balance session #{session_id} is now OPEN! "
                f"Use !gtb <amount> to guess — top {winner_count} {winner_label} win!",
                guild_id=guild_id,
            )

            # Post to Discord GTB channel if available
            if hasattr(self.bot, "gtb_channel_id") and self.bot.gtb_channel_id:
                try:
                    channel = self.bot.get_channel(self.bot.gtb_channel_id)
                    if channel:
                        await channel.send(
                            f"💰 **GTB Session #{session_id} OPENED** by {opened_by} "
                            f"— top {winner_count} {winner_label} will be picked"
                        )
                except Exception as e:
                    logger.info(f"Failed to send Discord notification: {e}")

            # Update Discord GTB panel for this guild
            if guild_id and hasattr(self.bot, "gtb_panels_by_guild"):
                gtb_panel = self.bot.gtb_panels_by_guild.get(guild_id)
                if gtb_panel:
                    try:
                        success = await gtb_panel.update_panel(force=True)
                        if success:
                            logger.info(f"✅ GTB panel updated in Discord for guild {guild_id}")
                        else:
                            logger.info(f"ℹ️  GTB panel not created yet for guild {guild_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update GTB panel: {e}")

        elif action == "close":
            session_id = data.get("session_id")
            # Announce in Kick chat
            await self.announce_in_chat(
                f"🔒 Guess the Balance session #{session_id} is now CLOSED! No more guesses allowed.", guild_id=guild_id
            )

            # Post to Discord
            if hasattr(self.bot, "gtb_channel_id") and self.bot.gtb_channel_id:
                try:
                    channel = self.bot.get_channel(self.bot.gtb_channel_id)
                    if channel:
                        await channel.send(f"🔒 **GTB Session #{session_id} CLOSED** - Guessing is over!")
                except Exception as e:
                    logger.info(f"Failed to send Discord notification: {e}")

            # Update Discord GTB panel for this guild
            if guild_id and hasattr(self.bot, "gtb_panels_by_guild"):
                gtb_panel = self.bot.gtb_panels_by_guild.get(guild_id)
                if gtb_panel:
                    try:
                        success = await gtb_panel.update_panel(force=True)
                        if success:
                            logger.info(f"✅ GTB panel updated in Discord for guild {guild_id}")
                        else:
                            logger.info(f"ℹ️  GTB panel not created yet for guild {guild_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update GTB panel: {e}")

        elif action == "set_result":
            session_id = data.get("session_id")
            result_amount = data.get("result_amount")
            winners = data.get("winners", [])  # Get winners from dashboard

            # Announce result in Kick chat
            await self.announce_in_chat(
                f"🎉 GTB Result: ${result_amount:,.2f}! Calculating winners...", guild_id=guild_id
            )

            # If winners were provided by dashboard, use them
            if winners and len(winners) > 0:
                # Small delay to ensure messages don't get combined
                await asyncio.sleep(1)

                # Announce every winner the dashboard scored (count is the
                # session's configured winner_count, already applied there).
                winner_messages = []
                for winner in winners:
                    rank_emoji = gtb_rank_marker(winner["rank"])
                    winner_messages.append(
                        f"{rank_emoji} {winner['username']}: ${winner['guess']:,.2f} (${winner['difference']:,.2f} off)"
                    )

                # Announce all winners in one message
                winner_text = f"🏆 Winners: " + " | ".join(winner_messages)
                logger.info(f"📢 Announcing winners in Kick chat: {winner_text}")
                await self.announce_in_chat(winner_text, guild_id=guild_id)
                logger.info(f"✅ Announced {len(winners)} GTB winners in Kick chat")
            else:
                # Fallback: Calculate winners using GTB manager if available
                if hasattr(self.bot, "gtb_manager") and self.bot.gtb_manager:
                    try:
                        logger.info(f"🔍 No winners provided, calling set_result with amount: ${result_amount:,.2f}")
                        success, message, calculated_winners = self.bot.gtb_manager.set_result(result_amount)
                        logger.info(
                            f"🔍 set_result returned - success: {success}, message: {message}, winners: {calculated_winners}"
                        )

                        if success and calculated_winners and len(calculated_winners) > 0:
                            # Small delay to ensure messages don't get combined
                            await asyncio.sleep(1)

                            # Announce every winner the bot's own scorer
                            # produced (honours the session winner_count).
                            winner_messages = []
                            for winner in calculated_winners:
                                rank_emoji = gtb_rank_marker(winner["rank"])
                                winner_messages.append(
                                    f"{rank_emoji} {winner['username']}: ${winner['guess']:,.2f} (${winner['difference']:,.2f} off)"
                                )

                            # Announce all winners in one message
                            winner_text = f"🏆 Winners: " + " | ".join(winner_messages)
                            logger.info(f"📢 Announcing winners in Kick chat: {winner_text}")
                            await self.announce_in_chat(winner_text, guild_id=guild_id)
                            logger.info(f"✅ Announced {len(calculated_winners)} GTB winners in Kick chat")
                        else:
                            logger.warning(
                                f"⚠️ GTB result set but no winners - success: {success}, message: {message}, winner count: {len(calculated_winners) if calculated_winners else 0}"
                            )
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to calculate GTB winners: {e}")
                        import traceback

                        traceback.print_exc()
                else:
                    logger.warning(f"⚠️ GTB manager not available and no winners provided in message")

            # Post to Discord
            if hasattr(self.bot, "gtb_channel_id") and self.bot.gtb_channel_id:
                try:
                    channel = self.bot.get_channel(self.bot.gtb_channel_id)
                    if channel:
                        await channel.send(f"🎉 **GTB Result Set**: ${result_amount:,.2f}")
                except Exception as e:
                    logger.info(f"Failed to send Discord notification: {e}")

            # Update Discord GTB panel for this guild
            if guild_id and hasattr(self.bot, "gtb_panels_by_guild"):
                gtb_panel = self.bot.gtb_panels_by_guild.get(guild_id)
                if gtb_panel:
                    try:
                        success = await gtb_panel.update_panel(force=True)
                        if success:
                            logger.info(f"✅ GTB panel updated in Discord for guild {guild_id}")
                        else:
                            logger.info(f"ℹ️  GTB panel not created yet for guild {guild_id}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to update GTB panel: {e}")

    async def handle_management_event(self, action, data):
        """Handle management events from dashboard"""
        logger.info(f"📥 Management Event: {action}")

        if action == "adjust_tickets":
            discord_id = data.get("discord_id")
            ticket_source = data.get("ticket_source")
            change = data.get("change")
            reason = data.get("reason")
            logger.info(f"Tickets adjusted for {discord_id}: {change} {ticket_source} tickets ({reason})")

        elif action == "start_period":
            start_date = data.get("start_date")
            end_date = data.get("end_date")
            server_id = data.get("discord_server_id")

            # Pull the global raffle title/prize (server-wide bot_settings) so the
            # announcement names the raffle and states the prize when configured.
            raffle_title = ""
            raffle_prize = ""
            if server_id is not None:
                try:
                    from bot import get_guild_settings

                    gs = get_guild_settings(int(server_id))
                    if gs is not None:
                        # The manager caches bot_settings; the dashboard saved
                        # title/prize moments ago and the settings-sync event may
                        # not have been processed yet — re-read from the DB so
                        # the announcement can't use stale values.
                        gs.refresh()
                        raffle_title = (gs.get("raffle_title", "") or "").strip()[:200]
                        raffle_prize = (gs.get("raffle_prize", "") or "").strip()[:500]
                except Exception as e:
                    logger.warning(f"[Raffle] Could not read raffle title/prize for start announcement: {e}")

            label = raffle_title or "New raffle period"
            msg = f"🎟️ {label} started! {start_date} to {end_date}"
            if raffle_prize:
                msg += f" — Prize: {raffle_prize}"
            await self.announce_in_chat(
                msg,
                guild_id=int(server_id) if server_id is not None else None,
            )

    async def handle_raffle_event(self, action, data):
        """Handle raffle events from dashboard"""
        logger.info(f"📥 Raffle Event: {action}")

        if action == "draw":
            request_id = data.get("request_id")
            period_id = data.get("period_id")
            winner_count = data.get("winner_count", 1)
            prize_description = data.get("prize_description", "")
            drawn_by_discord_id = data.get("drawn_by_discord_id")
            server_id = data.get("server_id")
            # Support for rerolls - exclude specific discord IDs
            initial_excluded_ids = data.get("excluded_discord_ids", [])
            is_reroll = data.get("is_reroll", False)

            logger.info(f"🎲 Processing raffle draw request {request_id} for period {period_id}")
            if is_reroll:
                logger.info(f"🔄 This is a REROLL - excluding IDs: {initial_excluded_ids}")

            try:
                from sqlalchemy import create_engine

                from raffle_system.draw import RaffleDraw

                # Reuse the shared engine (was create_engine per draw - leaked a pool).
                draw_handler = RaffleDraw(get_engine())

                # Convert initial excluded IDs to integers
                excluded_discord_ids = [int(eid) for eid in initial_excluded_ids if eid]

                if winner_count == 1:
                    # Single winner (or reroll)
                    winner = draw_handler.draw_winner(
                        period_id=period_id,
                        drawn_by_discord_id=drawn_by_discord_id,
                        prize_description=prize_description,
                        excluded_discord_ids=excluded_discord_ids if excluded_discord_ids else None,
                    )

                    if not winner:
                        result = {"success": False, "error": "No eligible participants found"}
                    else:
                        result = {"success": True, "winner": winner, "winners": [winner], "is_reroll": is_reroll}
                else:
                    # Multiple winners
                    winners = []

                    for i in range(winner_count):
                        winner = draw_handler.draw_winner(
                            period_id=period_id,
                            drawn_by_discord_id=drawn_by_discord_id,
                            # Suffix only when a prize was given — an empty prize
                            # must stay empty so draw_winner's global raffle_prize
                            # fallback applies instead of recording "(Winner 1/3)".
                            prize_description=(
                                f"{prize_description} (Winner {i+1}/{winner_count})"
                                if (prize_description or "").strip()
                                else ""
                            ),
                            excluded_discord_ids=excluded_discord_ids if excluded_discord_ids else None,
                            update_period=(i == 0),
                        )

                        if not winner:
                            if i == 0:
                                result = {"success": False, "error": "No eligible participants found"}
                                break
                            break

                        winners.append(winner)
                        excluded_discord_ids.append(winner["winner_discord_id"])

                    if winners:
                        result = {"success": True, "winners": winners, "winner": winners[0]}

                # Store result in Redis for dashboard to retrieve
                result_key = f"raffle_draw_result:{request_id}"
                self.client.setex(result_key, 30, json.dumps(result))  # Expire after 30 seconds
                logger.info(f"✅ Raffle draw completed, result stored in Redis")

                # Store pending announcements as a QUEUE - each winner announced individually when their animation completes
                if result.get("success"):
                    winners_list = result.get("winners", [])
                    queue_key = f"raffle_announcement_queue:{server_id}"

                    # Store winners as a list (queue) - each animation_complete pops one
                    # Include server_id in queue for multi-server announcement channel lookup
                    self.client.setex(
                        queue_key,
                        600,
                        json.dumps(
                            {
                                "winners_queue": winners_list,
                                "prize_description": prize_description,
                                "total_winners": len(winners_list),
                                "announced_count": 0,
                                "server_id": server_id,  # Store server_id for channel lookup
                            }
                        ),
                    )  # Expire after 10 minutes (streamer may take time between draws)
                    logger.info(
                        f"⏳ {len(winners_list)} winner(s) queued for announcement (server_id={server_id}), waiting for animation(s) to complete..."
                    )

            except Exception as e:
                logger.error(f"❌ Raffle draw failed: {e}")
                import traceback

                traceback.print_exc()
                result = {"success": False, "error": str(e)}
                result_key = f"raffle_draw_result:{request_id}"
                self.client.setex(result_key, 30, json.dumps(result))

        elif action == "animation_complete":
            # OBS widget animation finished for ONE winner - announce that winner now
            winner_kick_name = data.get("winner_kick_name")
            server_id = data.get("server_id")
            logger.info(f"🎬 [RAFFLE] Animation complete for winner: {winner_kick_name} (server_id={server_id})")

            try:
                # If server_id provided, use specific queue; otherwise search all (backwards compatibility)
                if server_id:
                    queue_keys = [f"raffle_announcement_queue:{server_id}"]
                else:
                    queue_keys = self.client.keys("raffle_announcement_queue:*")

                for key in queue_keys:
                    queue_data = self.client.get(key)
                    if queue_data:
                        queue = json.loads(queue_data)
                        winners_queue = queue.get("winners_queue", [])
                        prize_description = queue.get("prize_description", "")

                        if winners_queue:
                            # Pop the first winner from queue and announce
                            winner_to_announce = winners_queue.pop(0)
                            announced_count = queue.get("announced_count", 0) + 1
                            queue_server_id = queue.get("server_id", server_id)  # Get server_id from queue

                            logger.info(
                                f"✅ Announcing winner {announced_count}/{queue['total_winners']}: {winner_to_announce.get('winner_kick_name')} (server_id={queue_server_id})"
                            )

                            # Announce this single winner (pass server_id for multi-server support)
                            await self.announce_raffle_winners(
                                [winner_to_announce], prize_description, guild_id=queue_server_id
                            )

                            # Also announce in Kick chat. announce_in_chat swallows
                            # its own errors, so a Kick failure never blocks the
                            # Discord announce above.
                            kick_msg = build_raffle_kick_message(winner_to_announce, prize_description)
                            await self.announce_in_chat(kick_msg, guild_id=queue_server_id)

                            # Update queue in Redis
                            if winners_queue:
                                # More winners remaining
                                queue["winners_queue"] = winners_queue
                                queue["announced_count"] = announced_count
                                self.client.setex(key, 300, json.dumps(queue))
                            else:
                                # All winners announced, delete queue
                                self.client.delete(key)
                                logger.info(f"🎉 All {announced_count} winner(s) announced!")

                            break  # Only announce one winner per animation_complete event

            except Exception as e:
                logger.error(f"❌ Error handling animation_complete: {e}")
                import traceback

                traceback.print_exc()

        elif action == "announce_winner":
            # On-demand re-announce of a single historic winner (from the
            # Previous Winners table). No queue involved — announce directly to
            # both Discord and Kick chat.
            winner = data.get("winner") or {}
            prize_description = data.get("prize_description", "")
            server_id = data.get("server_id")
            logger.info(f"📢 [RAFFLE] Re-announce winner: {winner.get('winner_kick_name')} (server_id={server_id})")
            try:
                await self.announce_raffle_winners([winner], prize_description, guild_id=server_id)
                kick_msg = build_raffle_kick_message(winner, prize_description)
                await self.announce_in_chat(kick_msg, guild_id=server_id)
            except Exception as e:
                logger.error(f"❌ Error handling announce_winner: {e}")
                import traceback

                traceback.print_exc()

        elif action == "leaderboard_post":
            # Dashboard saved a new raffle leaderboard / announcement channel
            # ID. Re-initialize the auto-leaderboard so it posts a fresh
            # message in the new channel immediately instead of waiting for
            # the next periodic update tick.
            guild_id = data.get("guild_id")
            logger.info(f"📋 [RAFFLE] Leaderboard re-post requested (guild_id={guild_id})")
            try:
                from raffle_system.auto_leaderboard import setup_auto_leaderboard

                # Refresh settings first so the new channel ID is visible.
                if hasattr(self.bot, "settings_manager") and self.bot.settings_manager:
                    self.bot.settings_manager.refresh()

                # Reuse the shared engine (was create_engine per repost - leaked a pool).
                engine = get_engine()

                # setup_auto_leaderboard picks the channel ID from
                # settings_manager when not passed explicitly, and posts an
                # initial leaderboard message (or finds an existing one to
                # update) before kicking off its periodic task.
                lb = setup_auto_leaderboard(self.bot, engine, channel_id=None, server_id=guild_id)
                if lb:
                    await lb.initialize()
                    await lb.update_leaderboard()
                    logger.info(f"✅ Leaderboard posted/refreshed for guild {guild_id}")
                else:
                    logger.warning(f"⚠️ Auto-leaderboard not configured for guild {guild_id}")
            except Exception as e:
                logger.error(f"❌ Error handling leaderboard_post: {e}")
                import traceback

                traceback.print_exc()

    async def handle_tournament_event(self, action, data):
        """Handle bonus-buy tournament events from the dashboard.

        Currently the only action is `announce_winner`, fired when the OBS
        match-reveal animation lands. We announce the decided match to the
        server's stream chat (Kick/Twitch fan-out) and to Discord, and DM the
        loser an elimination notice when possible.
        """
        logger.info(f"🏆 Tournament Event: {action}")

        if action != "announce_winner":
            return

        # The dashboard forwards the widget's `match` object plus server id.
        match = data.get("match") or {}
        server_id = data.get("discord_server_id") or data.get("server_id")
        winner_side = data.get("winner_side")
        is_final = bool(data.get("is_final"))

        # Resolve winner / loser sides from the match payload.
        c1 = match.get("c1") or {}
        c2 = match.get("c2") or {}
        winner_cid = match.get("winner_competitor_id")
        if winner_cid is not None and c1.get("competitor_id") == winner_cid:
            winner, loser = c1, c2
        elif winner_cid is not None and c2.get("competitor_id") == winner_cid:
            winner, loser = c2, c1
        elif winner_side == "c1":
            winner, loser = c1, c2
        else:
            winner, loser = c2, c1

        w_name = winner.get("name") or winner.get("kick_username") or "Winner"
        l_name = loser.get("name") or loser.get("kick_username") or "Opponent"
        slot = winner.get("slot_name") or "their slot"
        payout = winner.get("payout")
        mult = winner.get("multiplier")

        def _money(v):
            try:
                return f"${float(v):,.2f}"
            except (TypeError, ValueError):
                return None

        parts = [f"🏆 {w_name} beats {l_name}"]
        detail = []
        if slot:
            detail.append(str(slot))
        if mult is not None:
            try:
                detail.append(f"{float(mult):.2f}x")
            except (TypeError, ValueError):
                pass
        if _money(payout):
            detail.append(_money(payout))
        if detail:
            parts.append("(" + " · ".join(detail) + ")")
        if is_final:
            parts.insert(0, "FINAL —")
            parts.append("is the tournament CHAMPION! 🎉")
        chat_msg = " ".join(parts)

        # Stream chat (Kick / Twitch). Swallows its own errors.
        try:
            await self.announce_in_chat(chat_msg, guild_id=server_id)
        except Exception as e:
            logger.warning(f"[Tournament] chat announce failed: {e}")

        # Discord announcement — use the dedicated tournament channel, falling
        # back to the slot-calls channel for setups that haven't picked one yet.
        try:
            channel_id = None
            if server_id:
                from bot import get_guild_settings

                gs = get_guild_settings(int(server_id))
                if gs:
                    channel_id = gs.get_int("tournament_announcement_channel_id") or gs.get_int("slot_calls_channel_id")
            if channel_id:
                channel = self.bot.get_channel(int(channel_id))
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(int(channel_id))
                    except Exception:
                        channel = None
                if channel is not None:
                    await channel.send(chat_msg)
            else:
                logger.info("[Tournament] no Discord channel configured; skipping Discord announce")
        except Exception as e:
            logger.warning(f"[Tournament] Discord announce failed: {e}")

    async def handle_commands_event(self, action, data):
        """Handle custom commands events from dashboard"""
        guild_id = data.get("discord_server_id")
        logger.info(f"📥 Commands Event: {action} (guild_id={guild_id})")

        if action == "reload":
            # Trigger custom commands reload for specific guild or all guilds
            if hasattr(self.bot, "custom_commands_managers"):
                try:
                    if guild_id:
                        # Reload commands for specific guild
                        if guild_id in self.bot.custom_commands_managers:
                            await self.bot.custom_commands_managers[guild_id].reload_commands()
                            logger.info(f"✅ Custom commands reloaded for guild {guild_id}")
                        else:
                            logger.warning(f"⚠️ No custom commands manager found for guild {guild_id}")
                    else:
                        # Reload commands for all guilds
                        for gid, manager in self.bot.custom_commands_managers.items():
                            await manager.reload_commands()
                        logger.info(
                            f"✅ Custom commands reloaded for all {len(self.bot.custom_commands_managers)} guilds"
                        )
                except Exception as e:
                    logger.warning(f"⚠️ Failed to reload custom commands: {e}")
                    import traceback

                    traceback.print_exc()
            else:
                logger.warning("⚠️ Custom commands managers not initialized")

    async def handle_point_shop_event(self, action, data):
        """Handle point shop events from dashboard"""
        guild_id = data.get("discord_server_id")
        guild = self.bot.get_guild(int(guild_id)) if guild_id else None
        guild_name = guild.name if guild else "Unknown"
        logger.info(f"📥 Point Shop Event: {action} (guild={guild_name}, guild_id={guild_id})")

        if action == "post_shop":
            channel_id = data.get("channel_id")

            # Import the post function from bot module
            try:
                from bot import post_point_shop_to_discord

                success = await post_point_shop_to_discord(self.bot, channel_id=channel_id, update_existing=True)
                if success:
                    logger.info(f"✅ Point shop posted to Discord (guild={guild_name})")
                else:
                    logger.warning(f"⚠️  Failed to post point shop (guild={guild_name})")
            except ImportError:
                logger.warning(f"⚠️  post_point_shop_to_discord function not implemented yet (guild={guild_name})")
            except Exception as e:
                logger.warning(f"⚠️  Failed to post point shop (guild={guild_name}): {e}")
                import traceback

                traceback.print_exc()

        elif action == "sync_shop":
            # Debounce: prevent duplicate syncs within 3 seconds
            current_time = time.time()
            if current_time - self.last_shop_sync < 3:
                logger.info(
                    f"⏭️  Ignoring duplicate sync_shop for {guild_name} (last sync {current_time - self.last_shop_sync:.1f}s ago)"
                )
                return

            self.last_shop_sync = current_time

            if not guild_id:
                logger.error("❌ sync_shop event missing discord_server_id - cannot sync without guild context")
                return

            # Force update the shop message
            try:
                from bot import post_point_shop_to_discord

                success = await post_point_shop_to_discord(self.bot, guild_id=guild_id, update_existing=True)
                if success:
                    logger.info(f"✅ Point shop force synced for {guild_name} (guild_id={guild_id})")
                else:
                    logger.warning(f"⚠️  Failed to sync point shop for {guild_name} (guild_id={guild_id})")
            except ImportError:
                logger.warning(
                    f"⚠️  post_point_shop_to_discord function not implemented yet (guild={guild_name}, guild_id={guild_id})"
                )
                logger.info("💡 Tip: Implement this function in bot.py to auto-sync shop embeds to Discord")
            except Exception as e:
                logger.warning(f"⚠️  Failed to sync point shop for {guild_name} (guild_id={guild_id}): {e}")
                import traceback

                traceback.print_exc()

        elif action == "update_settings":
            logger.info(f"✅ Point settings updated: {data}")
            # Settings are stored in DB, no action needed here

        elif action == "item_update":
            item_id = data.get("item_id")
            item_name = data.get("item_name")
            update_type = data.get("type", "update")  # create, update, delete
            guild_id = data.get("discord_server_id")
            logger.info(f"✅ Point shop item {update_type}: {item_name} (ID: {item_id})")

            # Auto-update the shop message when items change
            try:
                from bot import post_point_shop_to_discord

                success = await post_point_shop_to_discord(self.bot, guild_id=guild_id, update_existing=True)
                if success:
                    logger.info("✅ Point shop message auto-updated")
                else:
                    logger.warning("⚠️ Could not auto-update point shop (no channel configured?)")
            except Exception as e:
                logger.warning(f"⚠️ Failed to auto-update point shop: {e}")
                import traceback

                traceback.print_exc()

        elif action == "sale_status_updated":
            # Update the previously posted order notification message, if we have its message ID.
            try:
                sale_id = data.get("sale_id")
                new_status = data.get("new_status")
                if not sale_id or not new_status:
                    return

                if engine is None:
                    logger.info("[Point Shop] DB engine not available; cannot update order message")
                    return

                with engine.connect() as conn:
                    row = conn.execute(
                        text(
                            """
                        SELECT discord_notification_channel_id, discord_notification_message_id
                        FROM point_sales
                        WHERE id = :sale_id
                    """
                        ),
                        {"sale_id": int(sale_id)},
                    ).fetchone()

                if not row or not row[0] or not row[1]:
                    logger.info(f"[Point Shop] No stored Discord message for sale_id={sale_id}; skipping update")
                    return

                channel_id = int(row[0])
                message_id = int(row[1])

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    logger.info(f"[Point Shop] Channel not found for update: {channel_id}")
                    return

                try:
                    message = await channel.fetch_message(message_id)
                except Exception as e:
                    logger.info(f"[Point Shop] Failed to fetch order message {message_id}: {e}")
                    return

                status_lower = str(new_status).lower()
                if status_lower == "completed":
                    color = discord.Color.green()
                    status_label = "Completed"
                elif status_lower == "cancelled":
                    color = discord.Color.red()
                    status_label = "Cancelled"
                else:
                    color = discord.Color.purple()
                    status_label = "Pending"

                embed = message.embeds[0] if message.embeds else discord.Embed(title="🛒 Point Shop Order", color=color)
                embed.color = color

                # Update (or add) the Status field
                status_field_index = None
                for i, f in enumerate(embed.fields):
                    if (f.name or "").strip().lower() == "status":
                        status_field_index = i
                        break
                if status_field_index is None:
                    embed.add_field(name="Status", value=status_label, inline=True)
                else:
                    embed.set_field_at(status_field_index, name="Status", value=status_label, inline=True)

                await message.edit(embed=embed)
                logger.info(f"✅ Updated order embed for sale_id={sale_id} to status={status_lower}")
            except Exception as e:
                logger.info(f"[Point Shop] Failed to update order message for status change: {e}")
                import traceback

                traceback.print_exc()

        elif action == "item_restocked":
            # A sold-out item is back in stock. DM every viewer on this server
            # who opted in via notify_restock.
            item_name = data.get("item_name") or "An item"
            if not guild_id:
                return
            # Cooldown: swallow repeat restock events for the same item within
            # a short window (dashboard edits can fire several updates quickly).
            cooldown_key = (str(guild_id), str(data.get("item_id")))
            now = time.time()
            if now - self._last_restock_dm.get(cooldown_key, 0) < 60:
                logger.info(f"⏭️  Skipping duplicate restock DM for {item_name} (guild={guild_name})")
                return
            self._last_restock_dm[cooldown_key] = now

            message = f"**{item_name}** is back in stock in **{guild_name}**'s point shop!"
            await self._dm_shop_opted_in(guild_id, "notify_restock", message)

        elif action == "limits_reset":
            # Per-user purchase limits were reset. DM every viewer on this
            # server who opted in via notify_limit_reset.
            if not guild_id:
                return
            message = f"Purchase limits have been reset in **{guild_name}**'s point shop — you can buy again!"
            await self._dm_shop_opted_in(guild_id, "notify_limit_reset", message)

    async def _dm_shop_opted_in(self, guild_id, pref_column, message):
        """DM `message` to every viewer on `guild_id` who opted into `pref_column`.

        `pref_column` is one of the shop_notification_prefs boolean columns
        (notify_restock / notify_limit_reset) — a fixed literal chosen by the
        caller, never user input, so it's safe to interpolate into the query.
        Each send is isolated: a user who blocks DMs or left the guild is
        logged and skipped, never aborting the fan-out.
        """
        if engine is None:
            logger.info("[Point Shop] DB engine not available; cannot fan out shop notification")
            return

        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        f"""
                        SELECT discord_id
                        FROM shop_notification_prefs
                        WHERE discord_server_id = :guild_id AND {pref_column} = TRUE
                        """
                    ),
                    {"guild_id": int(guild_id)},
                ).fetchall()
        except Exception as e:
            logger.warning(f"[Point Shop] Failed to load opted-in viewers ({pref_column}): {e}")
            return

        discord_ids = [r[0] for r in rows if r[0]]
        if not discord_ids:
            logger.info(f"[Point Shop] No viewers opted in ({pref_column}) for guild {guild_id}")
            return

        sent = 0
        for discord_id in discord_ids:
            try:
                user = await self.bot.fetch_user(int(discord_id))
                await user.send(message)
                sent += 1
            except Exception as e:
                # Blocked DMs, deleted account, or a bad id — skip and continue.
                logger.info(f"[Point Shop] Could not DM {discord_id}: {e}")

        logger.info(f"✅ Sent {sent}/{len(discord_ids)} shop notification DMs ({pref_column}) for guild {guild_id}")

    async def handle_notifications_event(self, action, data):
        """Handle notifications events from dashboard and forward to Discord if configured."""
        try:
            if action != "new_notification":
                return

            notif_type = data.get("type")
            if notif_type != "new_sale":
                return

            source_server_id = data.get("discord_server_id")
            if not source_server_id:
                return

            try:
                source_server_id_int = int(source_server_id)
            except Exception:
                return

            notif_data = data.get("data") or {}

            if engine is None:
                logger.info("[Notifications] DB engine not available; cannot lookup point_settings")
                return

            # Lookup target channel (and optional target server) from point_settings
            with engine.connect() as conn:
                notify_channel_row = conn.execute(
                    text(
                        """
                    SELECT value FROM point_settings
                    WHERE key = 'shop_notification_channel_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": source_server_id_int},
                ).fetchone()
                notify_channel_id = int(notify_channel_row[0]) if notify_channel_row and notify_channel_row[0] else None

                notify_server_row = conn.execute(
                    text(
                        """
                    SELECT value FROM point_settings
                    WHERE key = 'shop_order_notify_server_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": source_server_id_int},
                ).fetchone()
                notify_server_id = (
                    int(notify_server_row[0]) if notify_server_row and notify_server_row[0] else source_server_id_int
                )

            if not notify_channel_id:
                return

            channel = self.bot.get_channel(notify_channel_id)
            if not channel:
                guild = self.bot.get_guild(notify_server_id)
                channel = guild.get_channel(notify_channel_id) if guild else None

            if not channel:
                logger.info(
                    f"[Notifications] Channel not found: {notify_channel_id} (notify_server_id={notify_server_id})"
                )
                return

            buyer = notif_data.get("buyer") or notif_data.get("username") or "Unknown"
            discord_id = notif_data.get("discord_id")
            item_name = notif_data.get("item_name") or "Unknown"
            price = notif_data.get("price")
            sale_id = notif_data.get("sale_id")
            requirement_title = notif_data.get("requirement_title")
            requirement_input = notif_data.get("requirement_input") or notif_data.get("requirement")
            note = notif_data.get("note")
            item_type = notif_data.get("item_type", "custom")
            raffle_ticket_amount = notif_data.get("raffle_ticket_amount", 0)
            sale_status = notif_data.get("sale_status", "pending")

            # Auto-completed items (like raffle tickets) get green embed
            if sale_status == "completed":
                embed_color = discord.Color.green()
                status_label = "Completed (Auto-fulfilled)"
            else:
                embed_color = discord.Color.purple()
                status_label = "Pending"

            buyer_display = str(buyer)
            try:
                buyer_discord_id = int(discord_id)
                if buyer_discord_id > 0:
                    buyer_display = f"<@{buyer_discord_id}>"
            except (TypeError, ValueError):
                pass

            embed = discord.Embed(
                title="🛒 New Point Shop Order",
                description=f"{buyer_display} placed an order.",
                color=embed_color,
            )

            if sale_id is not None:
                embed.add_field(name="Order ID", value=f"#{sale_id}", inline=True)
            embed.add_field(name="Item", value=str(item_name), inline=True)
            if price is not None:
                try:
                    embed.add_field(name="Price", value=f"{int(price):,} points", inline=True)
                except Exception:
                    embed.add_field(name="Price", value=f"{price} points", inline=True)
            embed.add_field(name="Status", value=status_label, inline=True)

            # Show raffle ticket info if applicable
            if item_type == "raffle_tickets" and raffle_ticket_amount:
                embed.add_field(
                    name="🎟️ Raffle Tickets",
                    value=f"{raffle_ticket_amount} ticket{'s' if raffle_ticket_amount != 1 else ''} awarded",
                    inline=True,
                )

            if requirement_input:
                field_name = (str(requirement_title).strip() if requirement_title else "") or "Details"
                field_name = field_name[:256]
                field_value = str(requirement_input)
                if len(field_value) > 1024:
                    field_value = field_value[:1021] + "…"
                embed.add_field(name=field_name, value=field_value, inline=False)

            if note:
                note_value = str(note)
                if len(note_value) > 1024:
                    note_value = note_value[:1021] + "…"
                embed.add_field(name="Note", value=note_value, inline=False)

            sent_message = await channel.send(embed=embed)

            # Store message mapping so we can edit it on status changes
            try:
                if sale_id is not None and engine is not None:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                            UPDATE point_sales
                            SET discord_notification_channel_id = :channel_id,
                                discord_notification_message_id = :message_id
                            WHERE id = :sale_id
                        """
                            ),
                            {
                                "channel_id": int(channel.id),
                                "message_id": int(sent_message.id),
                                "sale_id": int(sale_id),
                            },
                        )
            except Exception as e:
                logger.info(f"[Notifications] WARN: failed to store sale->message mapping: {e}")
        except Exception as e:
            logger.info(f"[Notifications] Failed to forward notification: {e}")
            import traceback

            traceback.print_exc()

    async def handle_bot_settings_event(self, action, data):
        """Handle bot settings events from dashboard"""
        logger.info(f"📥 Bot Settings Event: {action}")

        if action == "sync":
            # 1. Refresh the global settings_manager (legacy / single-server).
            if hasattr(self.bot, "settings_manager") and self.bot.settings_manager:
                try:
                    self.bot.settings_manager.refresh()
                    logger.info("✅ Bot settings refreshed from database")

                    # Log the updated values
                    settings = self.bot.settings_manager
                    logger.info(f"   • kick_channel: {settings.kick_channel}")
                    logger.info(f"   • kick_chatroom_id: {settings.kick_chatroom_id}")
                    logger.info(f"   • slot_calls_channel_id: {settings.slot_calls_channel_id}")
                    logger.info(f"   • raffle_auto_draw: {settings.raffle_auto_draw}")
                    logger.info(f"   • raffle_announcement_channel_id: {settings.raffle_announcement_channel_id}")
                    logger.info(f"   • raffle_leaderboard_channel_id: {settings.raffle_leaderboard_channel_id}")

                    # Update bot attributes for channel IDs that can be hot-reloaded
                    if settings.slot_calls_channel_id:
                        self.bot.slot_calls_channel_id = int(settings.slot_calls_channel_id)
                        logger.info(f"   ✓ Updated bot.slot_calls_channel_id")
                    if settings.raffle_announcement_channel_id:
                        self.bot.raffle_announcement_channel_id = int(settings.raffle_announcement_channel_id)
                        logger.info(f"   ✓ Updated bot.raffle_announcement_channel_id")
                    if settings.raffle_leaderboard_channel_id:
                        self.bot.raffle_leaderboard_channel_id = int(settings.raffle_leaderboard_channel_id)
                        logger.info(f"   ✓ Updated bot.raffle_leaderboard_channel_id")

                    # Note: KICK_CHANNEL and KICK_CHATROOM_ID require bot restart
                    # as they're used in the kick_chat_loop that runs continuously
                    if settings.kick_channel:
                        logger.warning(
                            f"   ⚠️ kick_channel updated - bot restart required for Kick chat to use new channel"
                        )

                except Exception as e:
                    logger.warning(f"⚠️ Failed to refresh bot settings: {e}")
                    import traceback

                    traceback.print_exc()
            else:
                logger.warning("⚠️ Bot settings manager not initialized")

            # 2. Refresh every per-guild settings manager + push hot-reloadable
            #    values into the per-guild trackers.
            #
            #    Why this matters: the dashboard writes settings keyed by
            #    `discord_server_id = <guild_id>`, while the global manager only
            #    reads `WHERE discord_server_id IS NULL`. Without this block,
            #    saving "Slot Calls Discord Channel" in the dashboard would
            #    succeed but the slot-call tracker (constructed once at
            #    startup with discord_channel_id=None) would never see the
            #    new value, producing the warning:
            #        "Slot call received but no Discord channel configured"
            try:
                # Pull the per-guild registry out of bot.py without forcing a
                # circular import at module load time.
                from bot import guild_settings_managers  # type: ignore
            except Exception as import_err:
                logger.warning(f"⚠️ Could not import guild_settings_managers: {import_err}")
                guild_settings_managers = {}

            trackers = getattr(self.bot, "slot_call_trackers_by_guild", {}) or {}

            # Always refresh every guild we know about (either through the
            # registry or the bot's connected guilds). The dashboard publish
            # payload doesn't currently carry a guild_id, so refreshing all
            # is the correct behaviour and is cheap.
            guild_ids = set(guild_settings_managers.keys()) | {g.id for g in self.bot.guilds}
            for guild_id in guild_ids:
                gs = guild_settings_managers.get(guild_id)
                if gs is None:
                    continue
                try:
                    gs.refresh()
                except Exception as refresh_err:
                    logger.warning(f"⚠️ [Guild {guild_id}] Settings refresh failed: {refresh_err}")
                    continue

                new_channel_id = gs.slot_calls_channel_id
                tracker = trackers.get(guild_id)
                if tracker is not None:
                    try:
                        tracker.discord_channel_id = int(new_channel_id) if new_channel_id else None
                        logger.info(f"   ✓ [Guild {guild_id}] slot_calls_channel_id = " f"{tracker.discord_channel_id}")
                    except (TypeError, ValueError) as cast_err:
                        logger.warning(
                            f"⚠️ [Guild {guild_id}] Bad slot_calls_channel_id " f"value '{new_channel_id}': {cast_err}"
                        )

                # Hot-swap the wager tracker's platform (shuffle/howl). The tracker
                # re-resolves wager_platform_name + the matching affiliate config in
                # refresh_settings(), so a dashboard platform switch takes effect
                # within seconds instead of waiting for the next 15-min poll.
                wager_trackers = getattr(self.bot, "shuffle_trackers_by_guild", {}) or {}
                wager_tracker = wager_trackers.get(guild_id)
                if wager_tracker is not None:
                    try:
                        wager_tracker.refresh_settings()
                        logger.info(f"   ✓ [Guild {guild_id}] wager platform = {wager_tracker.platform_name}")
                    except Exception as wager_err:
                        logger.warning(f"⚠️ [Guild {guild_id}] wager tracker refresh failed: {wager_err}")

        elif action == "update":
            key = data.get("key")
            value = data.get("value")
            logger.info(f"✅ Bot setting updated: {key} = {value}")

            # Refresh settings to pick up the change
            if hasattr(self.bot, "settings_manager") and self.bot.settings_manager:
                try:
                    self.bot.settings_manager.refresh()
                    logger.info("✅ Bot settings refreshed after update")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to refresh bot settings: {e}")

        elif action == "reload":
            # Profile settings were updated - refresh for specific guild or all
            guild_id = data.get("guild_id")
            logger.info(f"✅ Reload request for guild: {guild_id}")
            # Settings will be reloaded automatically on next access

            # Standalone (guild-less) workspaces: a settings save is also how
            # their Kick channel gets configured/changed — (re)build the chat
            # runtime live so slot requests/GTB work without a bot restart.
            try:
                if guild_id is not None and int(guild_id) < 0:
                    import sys as _sys

                    _main = _sys.modules.get("__main__")
                    _refresh = getattr(_main, "refresh_standalone_chat", None)
                    if _refresh is None:
                        from bot import refresh_standalone_chat as _refresh
                    await _refresh(int(guild_id))
            except Exception as standalone_err:
                logger.warning(f"⚠️ Standalone chat refresh failed for {guild_id}: {standalone_err}")

        elif action == "post_panel":
            # Dashboard chose a channel for a link/verify panel → post (or move) it there.
            await self._post_panel(data)

    async def _post_panel(self, data):
        """Post or move a link/verify panel into the channel chosen on the dashboard.

        data: {panel_type: 'kick_link' | 'twitch_link' | 'shuffle_verify' | 'howl_verify', channel_id, discord_server_id}
        Reuses the panel's existing create_panel(channel) (which rewrites the
        link_panels DB row); we additionally delete the previous Discord message
        so the panel "moves" rather than leaving a stale copy behind.
        """
        panel_type = data.get("panel_type")
        channel_id = data.get("channel_id")
        guild_id = data.get("discord_server_id")

        if not panel_type or not channel_id or not guild_id:
            logger.warning(f"⚠️ post_panel missing fields: {data}")
            return

        try:
            guild_id = int(guild_id)
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            logger.warning(f"⚠️ post_panel bad ids: guild={guild_id!r} channel={channel_id!r}")
            return

        # Resolve the right per-guild panel registry. kick_link / twitch_link /
        # link all map to the single combined link panel now (bot.link_panels).
        registry_attr = {
            "kick_link": "link_panels",
            "twitch_link": "link_panels",
            "link": "link_panels",
            "shuffle_verify": "shuffle_panels",
            "howl_verify": "howl_panels",
            # Global super-admin panels for the official guild.
            "patchnotes": "patchnotes_panels",
            "patchnotes_extension": "extension_patchnotes_panels",
            "rules": "rules_panels",
            "features": "features_panels",
            "sub_roles": "sub_role_panels",
        }.get(panel_type)
        if not registry_attr:
            logger.warning(f"⚠️ post_panel unknown panel_type: {panel_type}")
            return

        panels = getattr(self.bot, registry_attr, None) or {}
        panel = panels.get(guild_id)
        if panel is None:
            logger.warning(f"⚠️ No {panel_type} panel instance for guild {guild_id}")
            return

        new_channel = self.bot.get_channel(channel_id)
        if new_channel is None:
            logger.warning(f"⚠️ post_panel channel {channel_id} not found / not visible to bot")
            return

        # Standing panels (link/verify/rules/features/sub_roles) are a single
        # message that should "move" to the new channel, so we delete the old one.
        # Patch-notes panels are a changelog FEED: each post is a distinct release
        # announcement and earlier releases must stay in the channel — so we never
        # delete the previous patch-notes message, we just track the newest one.
        FEED_PANEL_TYPES = ("patchnotes", "patchnotes_extension")
        if panel_type not in FEED_PANEL_TYPES:
            # If the panel already exists somewhere, delete the old message (move semantics)
            old_channel_id = getattr(panel, "panel_channel_id", None)
            old_message_id = getattr(panel, "panel_message_id", None)
            if old_message_id and old_channel_id:
                try:
                    old_channel = self.bot.get_channel(int(old_channel_id))
                    if old_channel:
                        old_message = await old_channel.fetch_message(int(old_message_id))
                        await old_message.delete()
                        logger.info(f"🗑️ Removed old {panel_type} panel message in channel {old_channel_id}")
                except Exception as e:
                    # Non-fatal: old message may already be gone
                    logger.info(f"ℹ️ Could not delete old {panel_type} panel message: {e}")

        try:
            # The patch-notes panel renders content forwarded in the event data
            # (the latest release from the /patch-notes page); other global panels
            # read their content from bot_settings. Existing link/verify panels
            # take only the channel.
            if panel_type in ("patchnotes", "patchnotes_extension", "rules", "features", "sub_roles"):
                success = await panel.create_panel(new_channel, data=data)
            else:
                success = await panel.create_panel(new_channel)
            if success:
                logger.info(f"✅ Posted {panel_type} panel in channel {channel_id} (guild {guild_id})")
            else:
                logger.warning(f"⚠️ Failed to post {panel_type} panel in channel {channel_id}")
        except Exception as e:
            logger.error(f"⚠️ Error posting {panel_type} panel: {e}")
            import traceback

            traceback.print_exc()

    async def handle_giveaway_event(self, action, data):
        """Handle giveaway events from dashboard"""
        logger.info(f"🎁 Giveaway event: {action}")

        try:
            guild_id = data.get("discord_server_id")
            giveaway_id = data.get("giveaway_id")

            if not guild_id:
                logger.warning("⚠️ No guild_id in giveaway event")
                return

            # Convert guild_id to int (may come as string from JSON)
            guild_id = int(guild_id)

            # Get giveaway manager for this guild
            if not hasattr(self.bot, "giveaway_managers"):
                logger.warning("⚠️ Giveaway managers not initialized on bot")
                return

            giveaway_manager = self.bot.giveaway_managers.get(guild_id)
            if not giveaway_manager:
                logger.warning(f"⚠️ No giveaway manager found for guild {guild_id}")
                logger.info(f"   Available guilds: {list(self.bot.giveaway_managers.keys())}")
                return

            guild = self.bot.get_guild(guild_id)
            guild_name = guild.name if guild else str(guild_id)

            if action == "giveaway_started":
                logger.info(f"▶️  Starting giveaway {giveaway_id} for guild {guild_id}")

                # Reload active giveaway from database
                await giveaway_manager.load_active_giveaway()

                if giveaway_manager.active_giveaway:
                    giveaway_title = giveaway_manager.active_giveaway.get("title", "New Giveaway")
                    entry_method = giveaway_manager.active_giveaway.get("entry_method", "keyword")

                    # Announce in Discord
                    if hasattr(self.bot, "settings_manager"):
                        announcement_channel_id = getattr(
                            self.bot.settings_manager, "raffle_announcement_channel_id", None
                        )
                        if announcement_channel_id:
                            channel = self.bot.get_channel(announcement_channel_id)
                            if channel:
                                import discord

                                embed = discord.Embed(
                                    title="🎁 New Giveaway Started!", description=giveaway_title, color=0x00FF00
                                )

                                if entry_method == "keyword":
                                    keyword = giveaway_manager.active_giveaway.get("keyword", "")
                                    embed.add_field(
                                        name="How to Enter", value=f"Type `{keyword}` in Kick chat!", inline=False
                                    )
                                elif entry_method == "active_chatter":
                                    messages_required = giveaway_manager.active_giveaway.get("messages_required", 10)
                                    time_window = giveaway_manager.active_giveaway.get("time_window_minutes", 10)
                                    embed.add_field(
                                        name="How to Enter",
                                        value=f"Send {messages_required} unique messages in {time_window} minutes in Kick chat!",
                                        inline=False,
                                    )

                                allow_multiple = giveaway_manager.active_giveaway.get("allow_multiple_entries", False)
                                if allow_multiple:
                                    max_entries = giveaway_manager.active_giveaway.get("max_entries_per_user", 5)
                                    embed.add_field(
                                        name="Multiple Entries",
                                        value=f"You can enter up to {max_entries} times!",
                                        inline=False,
                                    )

                                await channel.send(embed=embed)
                                logger.info(f"✅ Announced giveaway start in Discord")

                    # Announce in Kick chat
                    if self.send_message_callback:
                        if entry_method == "keyword":
                            keyword = giveaway_manager.active_giveaway.get("keyword", "")
                            message = f"🎁 GIVEAWAY STARTED: {giveaway_title} | Type {keyword} to enter!"
                        else:
                            message = f"🎁 GIVEAWAY STARTED: {giveaway_title} | Be active in chat to enter!"

                        await self.announce_in_chat(message, guild_id=guild_id)
                        logger.info(f"✅ Announced giveaway start in Kick chat")

                    logger.info(f"✅ Giveaway {giveaway_id} started: {giveaway_title}")

            elif action == "giveaway_stopped":
                logger.info(f"⏹️  Stopping giveaway {giveaway_id} for guild {guild_id}")

                # Clear active giveaway
                giveaway_manager.active_giveaway = None
                logger.info(f"✅ Giveaway {giveaway_id} stopped")

                # Announce in Kick chat
                if self.send_message_callback:
                    await self.announce_in_chat("🎁 Giveaway has been stopped by moderators.", guild_id=guild_id)

            elif action == "giveaway_winner":
                winner = data.get("winner_username")
                giveaway_title = data.get("giveaway_title", "Giveaway")
                delay_announcement = data.get("delay_announcement", False)

                if not winner:
                    logger.warning("⚠️ No winner in giveaway_winner event")
                    return

                logger.info(f"🎉 Winner drawn: {winner}")

                # If delay requested, wait 7 seconds for OBS animation to complete (6s animation + 1s buffer)
                if delay_announcement:
                    logger.info(f"⏳ Waiting 7 seconds for animation to complete...")
                    await asyncio.sleep(7)

                # Announce in Discord
                if hasattr(self.bot, "settings_manager"):
                    announcement_channel_id = getattr(self.bot.settings_manager, "raffle_announcement_channel_id", None)
                    if announcement_channel_id:
                        channel = self.bot.get_channel(announcement_channel_id)
                        if channel:
                            import discord

                            embed = discord.Embed(
                                title="🎉 Giveaway Winner!", description=f"**{giveaway_title}**", color=0xFFD700
                            )
                            embed.add_field(name="Winner", value=f"🏆 **{winner}**", inline=False)
                            embed.add_field(name="", value="Congratulations! 🎊", inline=False)

                            await channel.send(embed=embed)
                            logger.info(f"✅ Announced giveaway winner in Discord: {winner}")

                # Announce in Kick chat
                if self.send_message_callback:
                    message = f"🎉 GIVEAWAY WINNER: @{winner} won {giveaway_title}! Congratulations! 🎊"
                    await self.announce_in_chat(message, guild_id=guild_id)
                    logger.info(f"✅ Announced giveaway winner in Kick chat: {winner}")

                # Clear active giveaway
                giveaway_manager.active_giveaway = None

        except Exception as e:
            logger.error(f"❌ Error handling giveaway event: {e}")
            import traceback

            traceback.print_exc()

    async def handle_stream_notification_event(self, action, data):
        """Handle stream notification events from dashboard - send Discord message when stream goes live"""
        logger.info(f"📺 Stream notification event: {action}")

        try:
            if action != "send":
                return

            channel_id = data.get("channel_id")
            streamer = data.get("streamer")
            discord_server_id = data.get("discord_server_id")
            is_test = data.get("test", False)

            if not channel_id or not streamer:
                logger.warning("⚠️ Missing channel_id or streamer in stream notification event")
                return

            import os

            import aiohttp

            from core.stream_notifications import (
                _alert_columns,
                _build_live_message,
                _stream_url,
                build_alert_components,
            )

            # Platform of this notification ('kick' | 'twitch'); the dashboard test
            # button sends it. Defaults to kick for legacy callers.
            platform = (data.get("platform") or "kick").lower()
            if platform not in ("kick", "twitch"):
                platform = "kick"

            stream_url = _stream_url(platform, streamer)

            bot_token = os.getenv("DISCORD_TOKEN")
            if not bot_token:
                logger.error("❌ DISCORD_TOKEN not configured")
                return

            # Load this platform's live-alert settings (Kick falls back to legacy).
            settings = {}
            if discord_server_id and engine:
                try:
                    from sqlalchemy import text

                    cols = _alert_columns(platform)
                    ph = ", ".join(f":k{i}" for i in range(len(cols)))
                    params = {"guild_id": discord_server_id}
                    params.update({f"k{i}": c for i, c in enumerate(cols)})
                    with engine.connect() as conn:
                        result = conn.execute(
                            text(
                                f"SELECT key, value FROM bot_settings "
                                f"WHERE discord_server_id = :guild_id AND key IN ({ph})"
                            ),
                            params,
                        ).fetchall()
                    settings = {key: value for key, value in result}
                except Exception as db_err:
                    logger.warning(f"⚠️ Failed to fetch notification settings: {db_err}")

            # Build the message via the shared builder (test ≡ real). Twitch gets its
            # correct no-video message for free.
            message_content, footer_text = _build_live_message(settings, platform, streamer, "", "")

            # 30s timeout + footer posted INDEPENDENTLY of the main POST's read
            # outcome: Discord can accept the alert but be slow to return, and a
            # tight timeout / nesting the footer inside the open response dropped
            # the footer. The two messages are unrelated Discord objects (the
            # footer needs no message id). Matches the real go-live path.
            headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
            channel_url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession() as session:
                # Action-row link buttons (primary Watch Stream + any custom buttons),
                # built from the same per-platform settings as the real go-live post.
                # Built inside the session so app:<name> emoji tokens resolve against
                # the bot's application emojis over the same connection.
                components = await build_alert_components(settings, platform, stream_url, session, bot_token)
                try:
                    async with session.post(
                        channel_url,
                        headers=headers,
                        json={"content": message_content, "components": components},
                        timeout=timeout,
                    ) as resp:
                        if resp.status in [200, 201]:
                            test_label = " (TEST)" if is_test else ""
                            logger.info(f"✅ Discord stream notification sent to channel {channel_id}{test_label}")
                        else:
                            error_text = await resp.text()
                            logger.error(f"❌ Failed to send Discord notification: {resp.status} - {error_text[:200]}")
                except asyncio.TimeoutError:
                    logger.warning("⚠️ Timed out reading Discord response for alert (may have posted)")

                # If footer is set, send it as a follow-up message (independent of
                # the main POST's read outcome).
                if footer_text:
                    await asyncio.sleep(0.5)
                    try:
                        async with session.post(
                            channel_url,
                            headers=headers,
                            json={"content": f"-# {footer_text}"},
                            timeout=timeout,
                        ) as footer_resp:
                            if footer_resp.status not in [200, 201]:
                                footer_err = (await footer_resp.text())[:200]
                                logger.warning(f"⚠️ Failed to send footer: {footer_resp.status} - {footer_err}")
                    except asyncio.TimeoutError:
                        logger.warning("⚠️ Timed out reading Discord response for footer (may have posted)")

        except Exception as e:
            logger.error(f"❌ Error handling stream notification event: {e}")
            import traceback

            traceback.print_exc()

    async def announce_raffle_winners(self, winners, prize_description, guild_id=None):
        """Announce raffle winner(s) to Discord raffle announcement channel

        Args:
            winners: List of winner data dicts
            prize_description: Description of the prize
            guild_id: Server ID for multi-server support (optional)
        """
        try:
            channel_id = None

            # Multi-server: Get channel from guild-specific settings
            if guild_id:
                try:
                    # Import get_guild_settings directly from bot module
                    from bot import get_guild_settings

                    guild_settings = get_guild_settings(int(guild_id))
                    if guild_settings:
                        channel_id = guild_settings.get_int("raffle_announcement_channel_id")
                        logger.info(f"[Raffle] Got channel_id {channel_id} from guild settings for guild {guild_id}")
                except ImportError as ie:
                    logger.info(f"[Raffle] Could not import get_guild_settings: {ie}")
                except Exception as e:
                    logger.info(f"[Raffle] Error getting guild settings: {e}")

            def build_verify_block(w):
                """Per-winner proof hash + 'Verify this draw' deep link.

                Returns '' when we lack a proof hash or draw id, so the
                announcement degrades gracefully (no broken/empty link). The
                link resolves per server: subdomain host when one exists,
                otherwise the apex with ?server=<slug> (subdomain-less
                free-tier workspaces).
                """
                proof_hash = w.get("proof_hash")
                draw_id = w.get("draw_id")
                lines = []
                if proof_hash:
                    lines.append(f"**Proof**: `{proof_hash[:16]}…`")
                if draw_id:
                    verify_url = get_server_public_page_url(engine, guild_id, f"/provably-fair/winners?draw={draw_id}")
                    lines.append(f"🔍 [Verify this draw]({verify_url})")
                return ("\n" + "\n".join(lines)) if lines else ""

            # Fallback to global settings manager
            if not channel_id and hasattr(self.bot, "settings_manager"):
                channel_id = self.bot.settings_manager.get_int("raffle_announcement_channel_id")
                if channel_id:
                    logger.info(f"[Raffle] Got channel_id {channel_id} from global settings_manager")

            if not channel_id:
                logger.warning(
                    f"⚠️ Raffle announcement channel not configured for server {guild_id}, skipping announcement"
                )
                return

            channel = self.bot.get_channel(channel_id)
            if not channel:
                # Try to fetch the channel if not in cache
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception as e:
                    logger.warning(f"⚠️ Raffle announcement channel {channel_id} not found: {e}")
                    return

            # Single or multiple winners
            if len(winners) == 1:
                winner = winners[0]

                # Safely get winner data with defaults
                winner_discord_id = winner.get("winner_discord_id")
                winner_kick_name = winner.get("winner_kick_name", "Unknown")
                winning_ticket = winner.get("winning_ticket", "?")
                total_tickets = winner.get("total_tickets", 0)
                win_probability = winner.get("win_probability", 0)

                # Try to mention winner
                if winner_discord_id:
                    try:
                        discord_user = await self.bot.fetch_user(winner_discord_id)
                        mention = discord_user.mention
                    except:
                        mention = f"Discord ID: {winner_discord_id}"
                else:
                    mention = "Unknown User"

                message = f"""
🎉 **RAFFLE WINNER DRAWN!** 🎉

**Winner**: {winner_kick_name} ({mention})
**Winning Ticket**: #{winning_ticket} out of {total_tickets:,}
**Win Probability**: {win_probability:.2f}%
**Prize**: {prize_description or 'Monthly Raffle Prize'}{build_verify_block(winner)}

Congratulations! Please contact an admin to claim your prize! 🎊
                """.strip()

                await channel.send(message)
                logger.info(f"✅ Raffle winner announced in Discord channel {channel_id}")
            else:
                # Multiple winners
                message = (
                    f"🎉 **RAFFLE WINNERS DRAWN!** 🎉\n\n**Prize**: {prize_description or 'Monthly Raffle Prize'}\n\n"
                )

                for i, winner in enumerate(winners, 1):
                    # Safely get winner data with defaults
                    winner_discord_id = winner.get("winner_discord_id")
                    winner_kick_name = winner.get("winner_kick_name", "Unknown")
                    winning_ticket = winner.get("winning_ticket", "?")
                    total_tickets = winner.get("total_tickets", 0)
                    win_probability = winner.get("win_probability", 0)

                    if winner_discord_id:
                        try:
                            discord_user = await self.bot.fetch_user(winner_discord_id)
                            mention = discord_user.mention
                        except:
                            mention = f"Discord ID: {winner_discord_id}"
                    else:
                        mention = "Unknown User"

                    message += f"**{i}. {winner_kick_name}** ({mention})\n"
                    message += f"   • Ticket #{winning_ticket}/{total_tickets:,} ({win_probability:.2f}%)"
                    message += build_verify_block(winner).replace("\n", "\n   • ")
                    message += "\n\n"

                message += "Congratulations to all winners! Please contact an admin to claim your prizes! 🎊"

                await channel.send(message)
                logger.info(f"✅ {len(winners)} raffle winners announced in Discord channel {channel_id}")

        except Exception as e:
            logger.error(f"❌ Error announcing raffle winners to Discord: {e}")
            import traceback

            traceback.print_exc()

    # ------------------------------------------------------------------
    # Clip buffer ownership
    #
    # The bot is the single owner of every rolling buffer. The dashboard cannot
    # hold one: it runs four gunicorn workers, so an in-process registry meant a
    # buffer started by one worker was invisible to the other three (status read
    # "inactive" 3 times in 4, clip creation failed just as often), and worker
    # recycling killed the ffmpeg process mid-recording.
    #
    # The dashboard drives this over Redis and reads state back from
    # `clip_buffer_status:<server_id>`; results for a clip request come back on
    # `clip_create_result:<request_id>`, mirroring the raffle-draw pattern.
    # ------------------------------------------------------------------

    BUFFER_STATUS_TTL = 120

    def _publish_buffer_status(self, server_id, channel_name=None):
        """Mirror a server's buffer state into Redis for the dashboard to read."""
        if not server_id:
            return
        try:
            from utils.clip_service import get_clip_manager

            manager = get_clip_manager()
            buf = manager.get_buffer(channel_name) if channel_name else None
            payload = {
                "active": bool(buf and buf.is_recording),
                "channel": channel_name,
                "clip_count": buf.get_segment_count() if buf else 0,
                "updated_at": time.time(),
            }
            self.client.setex(
                f"clip_buffer_status:{server_id}",
                self.BUFFER_STATUS_TTL,
                json.dumps(payload),
            )
        except Exception as e:
            logger.warning(f"⚠️ Could not publish buffer status for {server_id}: {e}")

    async def _start_buffer(self, server_id, channel_name):
        """Start (or adopt) the rolling buffer for a channel. Idempotent."""
        from utils.clip_service import get_clip_manager

        manager = get_clip_manager()
        existing = manager.get_buffer(channel_name)
        if existing and existing.is_recording:
            logger.info(f"🎬 Buffer already recording for {channel_name}")
            self._publish_buffer_status(server_id, channel_name)
            return True, "already_running"

        buffer = manager.create_buffer(channel_name)
        started = await buffer.start()
        self._publish_buffer_status(server_id, channel_name)
        if started:
            logger.info(f"🎬 Buffer started for {channel_name}")
            return True, "started"
        logger.warning(f"⚠️ Buffer failed to start for {channel_name} (stream offline?)")
        return False, "not_live"

    async def handle_clips_event(self, action, data):
        """Own the clip buffer and post finished clips to Discord."""
        server_id = data.get("discord_server_id")
        channel_name = (data.get("channel") or "").strip() or None

        if action == "buffer_start":
            if not channel_name:
                logger.warning("⚠️ buffer_start without a channel")
                return
            with server_context(server_id, None):
                await self._start_buffer(server_id, channel_name)
            return

        if action == "buffer_stop":
            from utils.clip_service import get_clip_manager

            manager = get_clip_manager()
            buffer = manager.get_buffer(channel_name) if channel_name else None
            if buffer:
                await buffer.stop()
                manager.remove_buffer(channel_name)
                logger.info(f"🛑 Buffer stopped for {channel_name}")
            self._publish_buffer_status(server_id, channel_name)
            return

        if action == "buffer_status":
            # Refresh on demand so a dashboard poll never reads a stale key.
            self._publish_buffer_status(server_id, channel_name)
            return

        if action == "create":
            await self._handle_clip_create(data)
            return

        if action != "uploaded":
            return

        await self._post_clip_to_discord(
            channel_id=data.get("channel_id"),
            watch_url=data.get("watch_url"),
            filename=data.get("filename") or "clip",
            title=(data.get("title") or "").strip(),
            uploaded_by=data.get("uploaded_by"),
        )

    async def _post_clip_to_discord(self, *, channel_id, watch_url, filename, title, uploaded_by=None):
        """Post a clip permalink so Discord unfurls it into a preview.

        MUST stay a plain-content message. Attaching a bot-authored embed
        suppresses the URL unfurl and replaces the preview with a static card —
        the same reason the go-live alert posts plain content plus a link button.
        """
        if not channel_id or not watch_url:
            logger.warning("⚠️ Clip post missing channel_id or watch_url")
            return

        try:
            channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        except Exception as e:
            # Deleted channel, revoked access, malformed id, or a transient API
            # error - all leave us with nothing to post to.
            logger.error(f"❌ Clip channel {channel_id} unavailable: {e}")
            return

        # The bare URL goes on its own line so Discord unfurls it. Subtext (-#)
        # keeps the attribution from competing with the preview.
        lines = [f"**{title}**" if title else "**New clip**", watch_url]
        if uploaded_by:
            lines.append(f"-# Clipped by {uploaded_by}")

        try:
            await channel.send(content="\n".join(lines))
            logger.info(f"✅ Clip {filename} posted to channel {channel_id}")
        except Exception as e:
            logger.error(f"❌ Failed to post clip to channel {channel_id}: {e}")

    def _store_clip(self, filename, local_path):
        """Move a finished clip into object storage. Blocking — call in a thread.

        The bot has no Railway volume, so local files are scratch: the clip is
        only reachable once the bucket holds it, and the dashboard serves it from
        there. The local copy is dropped afterwards to keep the container disk
        from filling up over a long stream.
        """
        from utils import object_storage

        if not object_storage.is_enabled():
            logger.error("❌ Object storage not configured on the bot - clip cannot be served")
            return False
        if not local_path or not os.path.exists(local_path):
            logger.error(f"❌ Clip file missing at {local_path}")
            return False

        ok = object_storage.put_file(object_storage.clip_key(filename), local_path, "video/mp4")
        if ok:
            try:
                os.remove(local_path)
            except OSError as e:
                logger.warning(f"⚠️ Could not delete local clip {local_path}: {e}")
        return ok

    def _server_base_url(self, server_id):
        """Public origin for a server's dashboard, used to build clip links."""
        try:
            from utils.server_urls import get_server_base_url

            return (get_server_base_url(engine, server_id) or "").rstrip("/")
        except Exception as e:
            logger.warning(f"⚠️ Could not resolve base URL for {server_id}: {e}")
            return ""

    def _clip_channel_id(self, server_id):
        """The Discord channel configured to receive clips, or None."""
        if not engine or not server_id:
            return None
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT value FROM bot_settings "
                        "WHERE key = 'clip_channel_id' AND discord_server_id = :guild_id"
                    ),
                    {"guild_id": server_id},
                ).fetchone()
            return (row[0] or "").strip() if row and row[0] else None
        except Exception as e:
            logger.warning(f"⚠️ Could not read clip_channel_id for {server_id}: {e}")
            return None

    def _record_clip_row(self, *, server_id, username, title, duration, clip_url, channel_name, filename, file_size):
        """Insert the clips row that backs the library listing and watch page."""
        if not engine:
            return
        try:
            with engine.connect() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO clips (kick_username, clip_title, clip_duration, clip_url,
                                           discord_server_id, kick_channel, filename, file_size,
                                           created_at)
                        VALUES (:username, :title, :duration, :url, :guild_id, :channel,
                                :filename, :file_size, CURRENT_TIMESTAMP)
                        """
                    ),
                    {
                        "username": username,
                        "title": title or None,
                        "duration": duration,
                        "url": clip_url,
                        "guild_id": server_id,
                        "channel": channel_name,
                        "filename": filename,
                        "file_size": file_size,
                    },
                )
                conn.commit()
        except Exception as e:
            # The clip is stored and playable; losing the row only costs the
            # library listing, so this must not fail the request.
            logger.warning(f"⚠️ Could not record clip {filename}: {e}")

    async def _handle_clip_create(self, data):
        """Cut a clip from the live buffer, store it, and announce it.

        The reply goes back on a Redis key rather than the pub/sub channel: the
        dashboard request is still blocked waiting for a concrete result, and
        pub/sub has no addressing for that. Mirrors the raffle-draw handshake.
        """
        request_id = data.get("request_id")
        server_id = data.get("discord_server_id")
        channel_name = (data.get("channel") or "").strip()
        duration = int(data.get("duration") or 30)
        username = data.get("username") or "dashboard"
        title = (data.get("title") or "").strip()

        def reply(payload):
            if not request_id:
                return
            try:
                self.client.setex(f"clip_create_result:{request_id}", 60, json.dumps(payload))
            except Exception as e:
                logger.warning(f"⚠️ Could not publish clip result {request_id}: {e}")

        from utils.clip_service import get_clip_manager

        manager = get_clip_manager()
        buffer = manager.get_buffer(channel_name) if channel_name else None

        if not buffer:
            reply({"success": False, "error": "no_buffer", "message": f"No buffer running for {channel_name}."})
            return
        if not buffer.is_recording:
            reply(
                {
                    "success": False,
                    "error": "not_recording",
                    "message": "Buffer is not recording. The stream may have gone offline.",
                }
            )
            return
        segments = buffer.get_segment_count()
        if segments < 3:
            reply(
                {
                    "success": False,
                    "error": "no_segments",
                    "message": f"Buffer still filling ({segments} segments). Try again in 30 seconds.",
                }
            )
            return

        try:
            result = await buffer.create_clip(duration, username, title)
        except Exception as e:
            logger.error(f"❌ Clip creation failed for {channel_name}: {e}")
            reply({"success": False, "error": "clip_failed", "message": str(e)})
            return

        if not result or not result.get("success"):
            reply(result or {"success": False, "error": "clip_failed", "message": "Failed to create clip"})
            return

        filename = result.get("filename")
        local_path = result.get("filepath")
        stored = await asyncio.to_thread(self._store_clip, filename, local_path)
        if not stored:
            reply({"success": False, "error": "storage_failed", "message": "Clip made but could not be stored."})
            return

        base_url = self._server_base_url(server_id)
        watch_url = f"{base_url}/clips/watch/{filename}"
        clip_url = f"{base_url}/clips/file/{filename}"
        actual_duration = result.get("duration", duration)

        self._record_clip_row(
            server_id=server_id,
            username=username,
            title=title,
            duration=actual_duration,
            clip_url=clip_url,
            channel_name=channel_name,
            filename=filename,
            file_size=result.get("file_size", 0),
        )

        reply(
            {
                "success": True,
                "filename": filename,
                "clip_url": clip_url,
                "watch_url": watch_url,
                "duration": actual_duration,
            }
        )

        clip_channel_id = self._clip_channel_id(server_id)
        if clip_channel_id:
            await self._post_clip_to_discord(
                channel_id=clip_channel_id,
                watch_url=watch_url,
                filename=filename,
                title=title,
                uploaded_by=username,
            )

        self._publish_buffer_status(server_id, channel_name)

    async def handle_subscriptions_event(self, action, data):
        """Handle subscription tier changes from the dashboard.

        On any tier change (Stripe webhook or super-admin override), the
        dashboard publishes here so the bot drops its local tier cache for that
        guild immediately — making chat-command gating reflect the new tier
        instantly instead of waiting for the ~60s cache TTL to expire.
        """
        if action != "tier_changed":
            return
        guild_id = data.get("discord_server_id")
        try:
            from utils.subscription_tier import invalidate_cache

            invalidate_cache(guild_id)
            logger.info(f"🔄 Tier cache invalidated for guild {guild_id}")
        except Exception as e:
            logger.warning(f"⚠️ Failed to invalidate tier cache for {guild_id}: {e}")

    async def handle_bot_event(self, payload):
        """Handle events forwarded from the Gunicorn webhook process via Redis
        `bot_events`. Currently: Twitch chat messages routed into the shared chat
        handler so watchtime/points/!commands/bonus-hunt/slot/GTB work for Twitch."""
        event_type = payload.get("type")
        data = payload.get("data", {}) or {}

        if event_type != "twitch_chat_message":
            logger.debug(f"[bot_events] Ignoring unknown type: {event_type}")
            return

        try:
            server_id = data.get("_server_id")
            msg = data.get("msg", {}) or {}
            if server_id is None:
                logger.info("[Twitch Chat] ⚠️ Missing _server_id, dropping message")
                return
            guild_id = int(server_id)

            # IMPORTANT: bot.py runs as the "__main__" module (python bot.py), so
            # its configured Bot/engine/kick_ws_manager live in sys.modules["__main__"].
            # `from bot import ...` would RE-IMPORT bot.py as a SECOND module with a
            # fresh, unconfigured Bot instance (no on_ready → guilds=0, no trackers).
            # Always resolve the live instances from __main__ to avoid that split.
            import sys as _sys

            _main = _sys.modules.get("__main__")
            engine = getattr(_main, "engine", None)
            kick_ws_manager = getattr(_main, "kick_ws_manager", None)
            if engine is None or kick_ws_manager is None:
                # Fallback for when bot IS imported as 'bot' (e.g. tests).
                from bot import engine, kick_ws_manager

            # Multi-platform paywall: Twitch is the secondary platform. If this
            # server also runs Kick (both configured) but lacks the 'multi_platform'
            # tier, skip Twitch chat. Single-platform Twitch is always allowed.
            try:
                from utils.bot_settings import BotSettingsManager
                from utils.subscription_tier import server_has_feature

                platforms_raw = BotSettingsManager(engine, guild_id).get("stream_platforms", "kick") or "kick"
                active = {p.strip() for p in str(platforms_raw).split(",") if p.strip()}
                if len(active) > 1 and not server_has_feature(engine, guild_id, "multi_platform"):
                    logger.debug(f"[Twitch Chat] Skipping (multi_platform not entitled) for {guild_id}")
                    return
            except Exception:
                pass  # never block chat on a gating hiccup

            # Preserve the REAL chatter display name (their Twitch name) so replies
            # address them correctly — e.g. "@MadcatsTV", not their canonical Kick
            # name. Identity crediting still uses the canonical username below.
            msg["display_username"] = msg.get("username", "")

            # Unified identity: remap the Twitch chatter to the viewer's canonical
            # username so username-keyed downstream tables (watchtime/points/GTB/
            # tickets) credit + dedupe to ONE shared viewer across platforms.
            try:
                from core.stream_links import resolve_canonical_identity

                _discord_id, canonical = resolve_canonical_identity(engine, msg.get("username", ""), guild_id, "twitch")
                if canonical:
                    msg["username"] = canonical
                    msg["sender_username"] = canonical
            except Exception as e:
                logger.debug(f"[Twitch Chat] canonical resolve skipped: {e}")

            guild = self.bot.get_guild(guild_id)
            guild_name = guild.name if guild else str(guild_id)

            await kick_ws_manager._handle_incoming_message(guild_id, guild_name, msg)
            logger.info(f"[Twitch Chat] 💬 Processed {msg.get('username')} via shared handler")
        except Exception as e:
            logger.error(f"[Twitch Chat] Error handling forwarded message: {e}")

    async def listen(self):
        """Listen for events on all dashboard channels"""
        if not self.enabled:
            logger.info("Redis subscriber not enabled, skipping...")
            return

        # Subscribe to all dashboard channels (webhook events disabled - using direct WebSocket)
        await asyncio.to_thread(
            self.pubsub.subscribe,
            "dashboard:slot_requests",
            "dashboard:timed_messages",
            "dashboard:gtb",
            "dashboard:management",
            "dashboard:raffle",
            "dashboard:commands",
            "dashboard:point_shop",
            "dashboard:notifications",
            "dashboard:bot_settings",
            "dashboard:giveaway",
            "dashboard:stream_notification",
            "dashboard:subscriptions",
            "dashboard:tournament",
            "dashboard:clips",
            # bot_events: Twitch chat (via EventSub webhook in the Gunicorn process)
            # is forwarded here for the bot to process. Kick chat still uses the
            # direct WebSocket (not this channel).
            "bot_events",
        )

        if signing_enabled():
            logger.info("🎧 Redis subscriber listening (message signatures REQUIRED)...")
        else:
            logger.warning(
                "🎧 Redis subscriber listening WITHOUT message authentication - "
                "set REDIS_MSG_SECRET on the dashboard and bot to reject forged events."
            )

        while True:
            try:
                # Run blocking listen() in a thread to avoid blocking event loop
                message = await asyncio.to_thread(self.pubsub.get_message, timeout=1.0)

                if message and message["type"] == "message":
                    channel = message["channel"]
                    try:
                        payload = json.loads(message["data"])

                        # Reject forged commands: every publisher signs with the
                        # shared REDIS_MSG_SECRET. No-op when the secret is unset.
                        if not verify_payload(payload):
                            logger.warning(f"Dropped Redis message on {channel}: bad or missing signature")
                            continue

                        action = payload.get("action")
                        data = payload.get("data", {})

                        # Tag all logging for this event with its server. Events are
                        # processed sequentially, so one wrap covers every handler.
                        _sid = data.get("discord_server_id") if isinstance(data, dict) else None
                        _sname = None
                        if _sid is not None:
                            try:
                                _g = self.bot.get_guild(int(_sid))
                                _sname = _g.name if _g else None
                            except (ValueError, TypeError, AttributeError):
                                _sname = None

                        with server_context(_sid, _sname):
                            # Route to appropriate handler
                            if channel == "dashboard:slot_requests":
                                await self.handle_slot_requests_event(action, data)
                            elif channel == "dashboard:timed_messages":
                                await self.handle_timed_messages_event(action, data)
                            elif channel == "dashboard:gtb":
                                await self.handle_gtb_event(action, data)
                            elif channel == "dashboard:management":
                                await self.handle_management_event(action, data)
                            elif channel == "dashboard:raffle":
                                await self.handle_raffle_event(action, data)
                            elif channel == "dashboard:commands":
                                await self.handle_commands_event(action, data)
                            elif channel == "dashboard:point_shop":
                                await self.handle_point_shop_event(action, data)
                            elif channel == "dashboard:notifications":
                                await self.handle_notifications_event(action, data)
                            elif channel == "dashboard:bot_settings":
                                await self.handle_bot_settings_event(action, data)
                            elif channel == "dashboard:giveaway":
                                await self.handle_giveaway_event(action, data)
                            elif channel == "dashboard:stream_notification":
                                await self.handle_stream_notification_event(action, data)
                            elif channel == "dashboard:subscriptions":
                                await self.handle_subscriptions_event(action, data)
                            elif channel == "dashboard:tournament":
                                await self.handle_tournament_event(action, data)
                            elif channel == "dashboard:clips":
                                await self.handle_clips_event(action, data)
                            elif channel == "bot_events":
                                # bot_events uses {type, data} rather than {action, data}.
                                await self.handle_bot_event(payload)

                    except json.JSONDecodeError as e:
                        logger.info(f"Failed to decode message: {e}")
                    except Exception as e:
                        logger.error(f"Error handling message: {e}")

                # Small delay to prevent busy loop
                await asyncio.sleep(0.01)

            except Exception as e:
                logger.info(f"Redis listener error: {e}")
                # Reconnect after delay
                await asyncio.sleep(5)
                if self.enabled:
                    # Resubscribe after error
                    await asyncio.to_thread(
                        self.pubsub.subscribe,
                        "dashboard:slot_requests",
                        "dashboard:timed_messages",
                        "dashboard:gtb",
                        "dashboard:management",
                        "dashboard:raffle",
                        "dashboard:commands",
                        "dashboard:point_shop",
                        "dashboard:notifications",
                        "dashboard:bot_settings",
                        "dashboard:giveaway",
                        "dashboard:stream_notification",
                        "dashboard:subscriptions",
                        "dashboard:tournament",
                        "dashboard:clips",
                        # 'bot_events' removed - no longer using webhooks
                    )


async def start_redis_subscriber(bot, send_message_callback=None):
    """
    Start the Redis subscriber in the background with retry logic.

    Args:
        bot: Discord bot instance
        send_message_callback: Async function to send messages to Kick chat (e.g., send_kick_message)
    """
    subscriber = RedisSubscriber(bot, send_message_callback)

    if subscriber.enabled:
        # Auto-sync point shop embeds on bot startup for all guilds
        logger.debug("🔄 Auto-syncing point shop embeds on startup...")
        for guild in bot.guilds:
            # Scope each guild's sync logging to that guild; server_context auto-resets
            # on exit so the trailing context doesn't bleed into the listen() loop below.
            with server_context(guild.id, guild.name):
                try:
                    from bot import post_point_shop_to_discord

                    await post_point_shop_to_discord(bot, guild_id=guild.id, update_existing=True)
                    logger.debug(f"✅ Synced shop for {guild.name} (ID: {guild.id})")
                except ImportError:
                    logger.warning(f"⚠️  post_point_shop_to_discord not available - skipping auto-sync")
                    break  # Don't try other guilds if function doesn't exist
                except Exception as e:
                    logger.warning(f"⚠️  Failed to auto-sync shop: {e}")

        # Run the listener with automatic retry on connection failures
        retry_delay = 5
        max_delay = 60
        while True:
            try:
                await subscriber.listen()
            except Exception as e:
                logger.warning(f"⚠️  Redis subscriber error: {e} — retrying in {retry_delay}s")
                # Fully recreate the Redis client and pubsub for a clean reconnect
                try:
                    subscriber.pubsub.close()
                except Exception:
                    pass
                try:
                    subscriber.client.close()
                except Exception:
                    pass
                try:
                    subscriber.client = redis.from_url(
                        subscriber.redis_url,
                        decode_responses=True,
                        socket_connect_timeout=5,
                        socket_timeout=5,
                    )
                    subscriber.pubsub = subscriber.client.pubsub()
                    subscriber.enabled = True
                    logger.info("🔄 Redis subscriber client recreated")
                except Exception as re:
                    logger.warning(f"⚠️  Failed to recreate Redis client: {re}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_delay)
            else:
                # listen() returned normally (shouldn't happen), reset delay
                retry_delay = 5
    else:
        logger.warning("⚠️  Redis subscriber disabled, bot will poll database instead")


# Example integration in your bot's main file:
"""
import asyncio
from redis_subscriber import start_redis_subscriber

# After your bot is defined and ready
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

    # Start Redis subscriber for dashboard events
    asyncio.create_task(start_redis_subscriber(bot))

    # Your other startup tasks...
"""
