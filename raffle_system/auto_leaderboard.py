"""
Auto-Updating Raffle Leaderboard
Posts and updates a raffle ticket leaderboard in a specified channel
"""

import logging
import os
from datetime import datetime

import discord
from discord.ext import tasks
from sqlalchemy import text

from .config import AUTO_LEADERBOARD_UPDATE_INTERVAL
from .reward_settings import platform_display_name, platform_display_name_for_server

logger = logging.getLogger(__name__)


class AutoLeaderboard:
    """Manages auto-updating raffle leaderboard"""

    def __init__(self, bot, engine, channel_id, server_id=None):
        self.bot = bot
        self.engine = engine
        self.channel_id = channel_id
        self.server_id = server_id  # Discord server ID for multiserver support
        self.message_id = None
        self.channel = None

    async def initialize(self):
        """Initialize the leaderboard system"""
        try:
            self.channel = self.bot.get_channel(self.channel_id)

            if not self.channel:
                logger.warning(f"[Auto-Leaderboard] ❌ Could not find channel with ID {self.channel_id}")
                return False

            logger.debug(f"[Auto-Leaderboard] Found channel: #{self.channel.name}")

            # Try to find our existing leaderboard message. Two accepted shapes:
            #   • the default heading, matched exactly (covers messages posted
            #     before the footer marker existed, incl. the footer-less
            #     no-period/error variants), or
            #   • a custom raffle title (🎟️-prefixed) TOGETHER WITH the
            #     "Updates every …" footer every leaderboard embed now sets.
            # A bare startswith("🎟️") is not enough — other bot embeds share
            # the emoji (e.g. "🎟️ Watchtime Conversion Result") and would get
            # adopted and overwritten by the next leaderboard edit.
            async for message in self.channel.history(limit=50):
                if message.author == self.bot.user and message.embeds:
                    embed = message.embeds[0]
                    if not embed.title or not embed.title.startswith("🎟️"):
                        continue
                    footer_text = embed.footer.text if embed.footer else None
                    is_ours = embed.title == "🎟️ Raffle Ticket Leaderboard" or (
                        footer_text and footer_text.startswith("Updates every")
                    )
                    if is_ours:
                        self.message_id = message.id
                        logger.debug(f"[Auto-Leaderboard] Found existing message: {self.message_id}")
                        break

            if not self.message_id:
                # Post initial leaderboard
                logger.debug("[Auto-Leaderboard] No existing message found, posting new leaderboard...")
                await self.post_new_leaderboard()

            return True

        except Exception as e:
            logger.error(f"[Auto-Leaderboard] ❌ Failed to initialize: {e}", exc_info=True)
            return False

    async def post_new_leaderboard(self):
        """Post a new leaderboard message"""
        try:
            embed = await self.create_leaderboard_embed()
            message = await self.channel.send(embed=embed)
            self.message_id = message.id
            logger.debug(f"[Auto-Leaderboard] Posted new message: {self.message_id}")

        except Exception as e:
            logger.error(f"[Auto-Leaderboard] ❌ Failed to post: {e}", exc_info=True)

    async def update_leaderboard(self):
        """Update the existing leaderboard message"""
        try:
            if not self.message_id:
                logger.debug("[Auto-Leaderboard] No message ID, posting new leaderboard...")
                await self.post_new_leaderboard()
                return

            # Fetch the message
            try:
                message = await self.channel.fetch_message(self.message_id)
            except discord.NotFound:
                logger.debug("[Auto-Leaderboard] Message not found, posting new one...")
                await self.post_new_leaderboard()
                return

            # Create updated embed
            embed = await self.create_leaderboard_embed()

            # Edit the message
            await message.edit(embed=embed)
            logger.debug(f"[Auto-Leaderboard] ✅ Updated message: {self.message_id}")

        except Exception as e:
            logger.error(f"[Auto-Leaderboard] ❌ Failed to update: {e}", exc_info=True)

    async def create_leaderboard_embed(self):
        """Create the leaderboard embed"""
        try:
            # Import here to avoid circular import
            from .tickets import TicketManager

            # Create ticket manager with server_id for proper multiserver support
            ticket_manager = TicketManager(self.engine, server_id=self.server_id)

            # Get period stats using the proper method
            stats = ticket_manager.get_period_stats()

            if not stats:
                return discord.Embed(
                    title="🎟️ Raffle Ticket Leaderboard",
                    description="No active raffle period",
                    color=discord.Color.red(),
                )

            period_id = stats["period_id"]
            start_date = stats["start_date"]
            end_date = stats["end_date"]

            # Resolve the configured wager platform name for this server's
            # messages. Falls back to reading bot_settings directly rather than
            # to a hardcoded "Shuffle": when get_guild_settings isn't available
            # a Howl server would otherwise be told to wager on Shuffle.
            platform = platform_display_name_for_server(self.engine, self.server_id, logger=logger)
            # Global raffle details (server-wide bot_settings). Shown on the
            # leaderboard when set; blank falls back to the default heading.
            raffle_title = ""
            raffle_prize = ""
            getter = getattr(self.bot, "get_guild_settings", None)
            if callable(getter) and self.server_id is not None:
                guild_settings = getter(self.server_id)
                platform = platform_display_name(guild_settings, default=platform)
                try:
                    # Truncate defensively: Discord caps embed titles at 256
                    # chars and rejects the whole embed past that — a too-long
                    # title would break every leaderboard update.
                    raffle_title = (guild_settings.get("raffle_title", "") or "").strip()[:200]
                    raffle_prize = (guild_settings.get("raffle_prize", "") or "").strip()[:500]
                except Exception:
                    pass

            # Embed heading: prefix the configured raffle title when present.
            embed_title = f"🎟️ {raffle_title}" if raffle_title else "🎟️ Raffle Ticket Leaderboard"

            # Footer marker ("Updates every …") — set on every variant of the
            # leaderboard embed. Message re-detection keys on it (see
            # initialize), so custom-titled embeds stay identifiable as ours.
            if AUTO_LEADERBOARD_UPDATE_INTERVAL >= 3600:
                update_text = f"Updates every {AUTO_LEADERBOARD_UPDATE_INTERVAL/3600:.1f} hours"
            elif AUTO_LEADERBOARD_UPDATE_INTERVAL >= 60:
                update_text = f"Updates every {AUTO_LEADERBOARD_UPDATE_INTERVAL/60:.0f} minutes"
            else:
                update_text = f"Updates every {AUTO_LEADERBOARD_UPDATE_INTERVAL} seconds"

            # Check if period hasn't started yet
            now = datetime.now()
            if now < start_date:
                days_until_start = (start_date - now).days
                hours_until_start = (start_date - now).seconds // 3600

                time_msg = f"{days_until_start} days" if days_until_start > 0 else f"{hours_until_start} hours"

                prize_line = f"🏆 **Prize:** {raffle_prize}\n" if raffle_prize else ""
                not_started = discord.Embed(
                    title=embed_title,
                    description=(
                        f"**Raffle Period Not Started Yet**\n\n"
                        f"{prize_line}"
                        f"📅 **Starts:** {start_date.strftime('%b %d, %Y at %I:%M %p')}\n"
                        f"📅 **Ends:** {end_date.strftime('%b %d, %Y at %I:%M %p')}\n\n"
                        f"⏳ **Time until start:** {time_msg}\n\n"
                        f"Get ready to earn tickets by watching streams, gifting subs, and wagering on {platform}!"
                    ),
                    color=discord.Color.blue(),
                )
                not_started.set_footer(text=update_text)
                return not_started

            # Get leaderboard using the proper method (top 5)
            leaderboard = ticket_manager.get_leaderboard(limit=5)

            # Get total stats from stats dict
            total_participants = stats["total_participants"]
            total_tickets = stats["total_tickets"]

            # Create embed
            prize_line = f"🏆 **Prize:** {raffle_prize}\n" if raffle_prize else ""
            embed = discord.Embed(
                title=embed_title,
                description=f"{prize_line}"
                f"**Period:** {start_date.strftime('%b %d')} - {end_date.strftime('%b %d, %Y')}\n"
                f"**Participants:** {total_participants:,} | **Total Tickets:** {total_tickets:,}",
                color=discord.Color.gold(),
                timestamp=datetime.utcnow(),
            )

            if not leaderboard:
                embed.add_field(name="No Participants Yet", value="Be the first to earn tickets!", inline=False)
            else:
                # Medals for top 3
                medals = ["🥇", "🥈", "🥉"]

                leaderboard_text = ""
                for entry in leaderboard:
                    rank = entry["rank"]
                    kick_name = entry["kick_name"] or "Unknown"
                    total = entry["total_tickets"]
                    watchtime = entry["watchtime_tickets"]
                    gifted = entry["gifted_sub_tickets"]
                    shuffle = entry["shuffle_wager_tickets"]
                    bonus = entry["bonus_tickets"]

                    # Medal or rank number
                    rank_display = medals[rank - 1] if rank <= 3 else f"`{rank}.`"

                    # Calculate win probability
                    win_prob = (total / total_tickets * 100) if total_tickets > 0 else 0

                    leaderboard_text += f"{rank_display} **{kick_name}** - {total:,} tickets ({win_prob:.2f}%)\n"
                    leaderboard_text += f"    ⏱️ {watchtime} 🎁 {gifted} 🎲 {shuffle} ⭐ {bonus}\n"

                embed.add_field(name="🏆 Top 5", value=leaderboard_text, inline=False)

            # Get ticket reward settings from database
            watchtime_tickets = "10"
            gifted_sub_tickets = "15"
            wager_tickets = "20"

            try:
                with self.engine.connect() as conn:
                    # Get watchtime tickets per hour
                    result = conn.execute(
                        text(
                            "SELECT value FROM bot_settings WHERE key = 'watchtime_tickets_per_hour' "
                            "AND discord_server_id = :server_id"
                        ),
                        {"server_id": self.server_id},
                    ).fetchone()
                    if result and result[0]:
                        watchtime_tickets = str(result[0])

                    # Get gifted sub tickets
                    result = conn.execute(
                        text(
                            "SELECT value FROM bot_settings WHERE key = 'gifted_sub_tickets' "
                            "AND discord_server_id = :server_id"
                        ),
                        {"server_id": self.server_id},
                    ).fetchone()
                    if result and result[0]:
                        gifted_sub_tickets = str(result[0])

                    # Get wager tickets per $1000
                    result = conn.execute(
                        text(
                            "SELECT value FROM bot_settings WHERE key = 'shuffle_tickets_per_1000' "
                            "AND discord_server_id = :server_id"
                        ),
                        {"server_id": self.server_id},
                    ).fetchone()
                    if result and result[0]:
                        wager_tickets = str(result[0])
            except Exception as e:
                logger.info(f"[Auto-Leaderboard] ⚠️ Failed to fetch ticket rewards from database: {e}")
                logger.info(
                    f"[Auto-Leaderboard] Using defaults: {watchtime_tickets}, {gifted_sub_tickets}, {wager_tickets}"
                )

            # Add how to earn tickets section with dynamic values from settings
            embed.add_field(
                name="📋 How to Earn Tickets",
                value=(
                    f"⏱️ **Watch Streams** - {watchtime_tickets} tickets per hour\n"
                    f"🎁 **Gift Subs** - {gifted_sub_tickets} tickets per sub\n"
                    f"🎲 **{platform} Wagers** - {wager_tickets} tickets per $1000 wagered\n"
                    "⭐ **Bonus** - Admin awarded for events\n\n"
                    "Use `!tickets` to check your balance!"
                ),
                inline=False,
            )

            # Footer marker computed above (also used by message re-detection).
            embed.set_footer(text=update_text)

            return embed

        except Exception as e:
            logger.info(f"[Auto-Leaderboard] ❌ Failed to create embed: {e}")
            import traceback

            traceback.print_exc()
            return discord.Embed(
                title="🎟️ Raffle Ticket Leaderboard",
                description="Error loading leaderboard data",
                color=discord.Color.red(),
            )


async def setup_auto_leaderboard(bot, engine, channel_id=None, server_id=None):
    """
    Setup the auto-updating leaderboard as a Discord bot task

    Args:
        bot: Discord bot instance
        engine: SQLAlchemy engine
        channel_id: Discord channel ID for leaderboard (optional, falls back to env var)
        server_id: Discord server/guild ID for multiserver support
    """
    # Get channel ID from parameter, bot_settings, or environment variable
    if channel_id is None:
        # Try to get from bot_settings if available
        if hasattr(bot, "settings_manager") and bot.settings_manager:
            channel_id = bot.settings_manager.raffle_leaderboard_channel_id

        # Fall back to environment variable
        if channel_id is None:
            channel_id_str = os.getenv("RAFFLE_LEADERBOARD_CHANNEL_ID")
            if channel_id_str:
                try:
                    channel_id = int(channel_id_str)
                except ValueError:
                    logger.info(f"[Auto-Leaderboard] ❌ Invalid RAFFLE_LEADERBOARD_CHANNEL_ID: {channel_id_str}")
                    return None

    if not channel_id:
        logger.info("[Auto-Leaderboard] ⚠️ Channel ID not set - auto-leaderboard disabled")
        return None

    logger.debug(f"[Auto-Leaderboard] Setting up for channel ID: {channel_id}")

    leaderboard = AutoLeaderboard(bot, engine, channel_id, server_id=server_id)

    @tasks.loop(seconds=AUTO_LEADERBOARD_UPDATE_INTERVAL)
    async def update_leaderboard_task():
        """Periodic task to update the leaderboard"""
        logger.debug("🔄 [Auto-Leaderboard] Updating...")
        await leaderboard.update_leaderboard()

    @update_leaderboard_task.before_loop
    async def before_leaderboard_task():
        """Wait for bot to be ready and initialize"""
        await bot.wait_until_ready()

        logger.debug(f"[Auto-Leaderboard] Initializing for channel {channel_id}...")

        # Initialize the leaderboard
        success = await leaderboard.initialize()

        if success:
            if AUTO_LEADERBOARD_UPDATE_INTERVAL >= 3600:
                logger.debug(
                    f"✅ [Auto-Leaderboard] Started (updates every {AUTO_LEADERBOARD_UPDATE_INTERVAL/3600:.1f} hours)"
                )
            elif AUTO_LEADERBOARD_UPDATE_INTERVAL >= 60:
                logger.debug(
                    f"✅ [Auto-Leaderboard] Started (updates every {AUTO_LEADERBOARD_UPDATE_INTERVAL/60:.0f} minutes)"
                )
            else:
                logger.debug(
                    f"✅ [Auto-Leaderboard] Started (updates every {AUTO_LEADERBOARD_UPDATE_INTERVAL} seconds)"
                )
        else:
            logger.error("❌ [Auto-Leaderboard] Failed to initialize - task cancelled")
            update_leaderboard_task.cancel()

    # Start the task
    update_leaderboard_task.start()

    return leaderboard
