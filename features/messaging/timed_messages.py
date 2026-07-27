"""
Timed Messages System - Schedule recurring messages to Kick chat
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks
from sqlalchemy import text

from utils.log_context import set_server

logger = logging.getLogger(__name__)


class TimedMessage:
    """Represents a timed message"""

    def __init__(
        self,
        message_id: int,
        message: str,
        interval_minutes: int,
        enabled: bool = True,
        last_sent: Optional[datetime] = None,
    ):
        self.message_id = message_id
        self.message = message
        self.interval_minutes = interval_minutes
        self.enabled = enabled
        self.last_sent = last_sent


class TimedMessagesManager:
    """Manage timed messages to Kick chat

    NOTE: In multiserver deployments, this should be refactored to be per-guild.
    Currently uses guild_id=None to load all messages (backwards compatible).
    """

    def __init__(self, engine, kick_send_callback=None, guild_id=None):
        self.engine = engine
        self.kick_send_callback = kick_send_callback
        self.guild_id = guild_id  # None = load all guilds (backwards compatible)
        self.messages: Dict[int, TimedMessage] = {}
        self._init_database()
        self._load_messages()

    def _init_database(self):
        """Create timed_messages table if it doesn't exist"""
        if not self.engine:
            logger.error("No database engine provided")
            return

        try:
            # Detect database type
            db_url = str(self.engine.url)
            is_postgres = "postgresql" in db_url.lower()

            with self.engine.begin() as conn:
                if is_postgres:
                    # PostgreSQL uses SERIAL for auto-increment
                    # Use discord_server_id to match dashboard schema
                    conn.execute(
                        text(
                            """
                        CREATE TABLE IF NOT EXISTS timed_messages (
                            id SERIAL PRIMARY KEY,
                            discord_server_id VARCHAR(50),
                            message TEXT NOT NULL,
                            interval_minutes INTEGER NOT NULL,
                            channel VARCHAR(100),
                            enabled BOOLEAN DEFAULT TRUE,
                            last_sent TIMESTAMP,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            created_by BIGINT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """
                        )
                    )
                else:
                    # SQLite uses AUTOINCREMENT
                    conn.execute(
                        text(
                            """
                        CREATE TABLE IF NOT EXISTS timed_messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            discord_server_id VARCHAR(50),
                            message TEXT NOT NULL,
                            interval_minutes INTEGER NOT NULL,
                            channel VARCHAR(100),
                            enabled BOOLEAN DEFAULT TRUE,
                            last_sent TIMESTAMP,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            created_by BIGINT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """
                        )
                    )
            logger.debug("timed_messages table initialized")
        except Exception as e:
            logger.error(f"Failed to initialize timed_messages table: {e}")

    def _load_messages(self):
        """Load all timed messages from database"""
        if not self.engine:
            return

        try:
            with self.engine.begin() as conn:
                # Load messages for this guild or all guilds (if guild_id=None)
                if self.guild_id is not None:
                    # Load for specific guild (use discord_server_id column)
                    result = conn.execute(
                        text(
                            """
                        SELECT id, message, interval_minutes, enabled, last_sent
                        FROM timed_messages
                        WHERE discord_server_id = :guild_id OR discord_server_id IS NULL
                        ORDER BY id
                    """
                        ),
                        {"guild_id": str(self.guild_id)},
                    ).fetchall()
                else:
                    # Load all messages (backwards compatible)
                    result = conn.execute(
                        text(
                            """
                        SELECT id, message, interval_minutes, enabled, last_sent
                        FROM timed_messages
                        ORDER BY id
                    """
                        )
                    ).fetchall()

                self.messages.clear()
                for row in result:
                    msg = TimedMessage(
                        message_id=row[0],
                        message=row[1],
                        interval_minutes=row[2],
                        enabled=bool(row[3]),
                        last_sent=row[4],
                    )
                    self.messages[msg.message_id] = msg

                logger.debug(f"Loaded {len(self.messages)} timed messages")
        except Exception as e:
            logger.error(f"Failed to load timed messages: {e}")

    def add_message(self, message: str, interval_minutes: int, created_by: int) -> Dict:
        """Add a new timed message"""
        if not self.engine:
            return {"status": "error", "message": "No database connection"}

        if interval_minutes < 1:
            return {"status": "error", "message": "Interval must be at least 1 minute"}

        if len(message) > 500:
            return {"status": "error", "message": "Message too long (max 500 chars)"}

        try:
            # Detect database type
            db_url = str(self.engine.url)
            is_postgres = "postgresql" in db_url.lower()

            with self.engine.begin() as conn:
                if is_postgres:
                    # PostgreSQL returns ID with RETURNING clause
                    # Use discord_server_id to match dashboard schema
                    result = conn.execute(
                        text(
                            """
                        INSERT INTO timed_messages (discord_server_id, message, interval_minutes, enabled, created_by)
                        VALUES (:guild_id, :message, :interval, TRUE, :created_by)
                        RETURNING id
                    """
                        ),
                        {
                            "guild_id": str(self.guild_id) if self.guild_id else None,
                            "message": message,
                            "interval": interval_minutes,
                            "created_by": created_by,
                        },
                    )
                    message_id = result.scalar()
                else:
                    # SQLite uses lastrowid
                    result = conn.execute(
                        text(
                            """
                        INSERT INTO timed_messages (discord_server_id, message, interval_minutes, enabled, created_by)
                        VALUES (:guild_id, :message, :interval, TRUE, :created_by)
                    """
                        ),
                        {
                            "guild_id": str(self.guild_id) if self.guild_id else None,
                            "message": message,
                            "interval": interval_minutes,
                            "created_by": created_by,
                        },
                    )
                    message_id = result.lastrowid

            # Reload messages
            self._load_messages()

            logger.info(f"Added timed message #{message_id}: {message[:50]}...")
            return {"status": "success", "message": f"Added timed message #{message_id}", "message_id": message_id}
        except Exception as e:
            logger.error(f"Error adding timed message: {e}")
            return {"status": "error", "message": str(e)}

    def remove_message(self, message_id: int) -> Dict:
        """Remove a timed message"""
        if not self.engine:
            return {"status": "error", "message": "No database connection"}

        try:
            with self.engine.begin() as conn:
                # Delete with discord_server_id filter for multiserver isolation
                if self.guild_id is not None:
                    result = conn.execute(
                        text(
                            """
                        DELETE FROM timed_messages
                        WHERE id = :id AND (discord_server_id = :guild_id OR discord_server_id IS NULL)
                    """
                        ),
                        {"id": message_id, "guild_id": str(self.guild_id)},
                    )
                else:
                    # No guild filter (backwards compatible)
                    result = conn.execute(
                        text(
                            """
                        DELETE FROM timed_messages WHERE id = :id
                    """
                        ),
                        {"id": message_id},
                    )

                if result.rowcount == 0:
                    return {"status": "error", "message": f"Message #{message_id} not found"}

            # Reload messages
            self._load_messages()

            logger.info(f"Removed timed message #{message_id}")
            return {"status": "success", "message": f"Removed timed message #{message_id}"}
        except Exception as e:
            logger.error(f"Error removing timed message: {e}")
            return {"status": "error", "message": str(e)}

    def toggle_message(self, message_id: int, enabled: bool) -> Dict:
        """Enable or disable a timed message"""
        if not self.engine:
            return {"status": "error", "message": "No database connection"}

        try:
            with self.engine.begin() as conn:
                # Update with discord_server_id filter for multiserver isolation
                if self.guild_id is not None:
                    result = conn.execute(
                        text(
                            """
                        UPDATE timed_messages
                        SET enabled = :enabled, updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id AND (discord_server_id = :guild_id OR discord_server_id IS NULL)
                    """
                        ),
                        {"id": message_id, "enabled": enabled, "guild_id": str(self.guild_id)},
                    )
                else:
                    result = conn.execute(
                        text(
                            """
                        UPDATE timed_messages
                        SET enabled = :enabled, updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                    """
                        ),
                        {"id": message_id, "enabled": enabled},
                    )

                if result.rowcount == 0:
                    return {"status": "error", "message": f"Message #{message_id} not found"}

            # Reload messages
            self._load_messages()

            status = "enabled" if enabled else "disabled"
            logger.info(f"Timed message #{message_id} {status}")
            return {"status": "success", "message": f"Message #{message_id} {status}"}
        except Exception as e:
            logger.error(f"Error toggling timed message: {e}")
            return {"status": "error", "message": str(e)}

    def update_interval(self, message_id: int, interval_minutes: int) -> Dict:
        """Update message interval"""
        if not self.engine:
            return {"status": "error", "message": "No database connection"}

        if interval_minutes < 1:
            return {"status": "error", "message": "Interval must be at least 1 minute"}

        try:
            with self.engine.begin() as conn:
                # Update with discord_server_id filter for multiserver isolation
                if self.guild_id is not None:
                    result = conn.execute(
                        text(
                            """
                        UPDATE timed_messages
                        SET interval_minutes = :interval, updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id AND (discord_server_id = :guild_id OR discord_server_id IS NULL)
                    """
                        ),
                        {"id": message_id, "interval": interval_minutes, "guild_id": str(self.guild_id)},
                    )
                else:
                    result = conn.execute(
                        text(
                            """
                        UPDATE timed_messages
                        SET interval_minutes = :interval, updated_at = CURRENT_TIMESTAMP
                        WHERE id = :id
                    """
                        ),
                        {"id": message_id, "interval": interval_minutes},
                    )

                if result.rowcount == 0:
                    return {"status": "error", "message": f"Message #{message_id} not found"}

            # Reload messages
            self._load_messages()

            logger.info(f"Updated timed message #{message_id} interval to {interval_minutes}m")
            return {
                "status": "success",
                "message": f"Updated message #{message_id} interval to {interval_minutes} minutes",
            }
        except Exception as e:
            logger.error(f"Error updating interval: {e}")
            return {"status": "error", "message": str(e)}

    def reload_messages(self):
        """Reload messages from database (public method for external calls)"""
        self._load_messages()
        logger.info(f"Reloaded {len(self.messages)} timed messages from database")

    def list_messages(self) -> List[TimedMessage]:
        """Get all timed messages"""
        return list(self.messages.values())

    def get_message(self, message_id: int) -> Optional[TimedMessage]:
        """Get a specific message"""
        return self.messages.get(message_id)

    async def check_and_send_messages(self, active_chatters_count: int = 0):
        """Check if any messages need to be sent and send them

        Args:
            active_chatters_count: Number of active chatters in the last window (default: 0)
        """
        if not self.kick_send_callback:
            return

        # Timed messages are a Tier 2+ feature. Skip the whole cycle when the
        # guild's subscription doesn't grant it. (guild_id=None is the legacy
        # all-guilds mode, which predates per-guild tiers — leave it ungated.)
        if self.guild_id is not None:
            try:
                from utils.subscription_tier import server_has_feature

                if not server_has_feature(self.engine, self.guild_id, "timed_messages"):
                    return
            except Exception as e:
                logger.debug(f"[Timed Messages] tier check failed, allowing: {e}")

        # Routine per-minute/per-guild check — DEBUG to avoid log spam.
        enabled_count = sum(1 for msg in self.messages.values() if msg.enabled)
        if enabled_count > 0:
            logger.debug(
                f"[Timed Messages] Check: {active_chatters_count} active chatters, {enabled_count} enabled messages"
            )

        # Don't send timed messages if there are less than 2 active chatters
        # This prevents spam when stream is offline
        if active_chatters_count < 2:
            if enabled_count > 0:
                logger.debug(
                    f"[Timed Messages] Skipping - need 2+ active chatters, currently have {active_chatters_count}"
                )
            return

        now = datetime.utcnow()

        for msg in self.messages.values():
            if not msg.enabled:
                continue

            # Check if it's time to send
            if msg.last_sent is None:
                should_send = True
            else:
                time_since_last = now - msg.last_sent
                should_send = time_since_last >= timedelta(minutes=msg.interval_minutes)

            if should_send:
                try:
                    # Send the message with guild_id for proper routing
                    await self.kick_send_callback(msg.message, guild_id=self.guild_id)
                    logger.info(
                        f"[Guild {self.guild_id}] Sent timed message #{msg.message_id}: {msg.message[:50]}... ({active_chatters_count} active chatters)"
                    )

                    # Update last_sent timestamp
                    if self.engine:
                        with self.engine.begin() as conn:
                            # Update with discord_server_id filter for multiserver isolation
                            if self.guild_id is not None:
                                conn.execute(
                                    text(
                                        """
                                    UPDATE timed_messages
                                    SET last_sent = CURRENT_TIMESTAMP
                                    WHERE id = :id AND (discord_server_id = :guild_id OR discord_server_id IS NULL)
                                """
                                    ),
                                    {"id": msg.message_id, "guild_id": str(self.guild_id)},
                                )
                            else:
                                conn.execute(
                                    text(
                                        """
                                    UPDATE timed_messages
                                    SET last_sent = CURRENT_TIMESTAMP
                                    WHERE id = :id
                                """
                                    ),
                                    {"id": msg.message_id},
                                )

                        # Update in memory
                        msg.last_sent = now

                except Exception as e:
                    logger.error(f"Failed to send timed message #{msg.message_id}: {e}")


class TimedMessagesCommands(commands.Cog):
    """Discord commands for managing timed messages"""

    def __init__(self, bot, manager: TimedMessagesManager = None):
        self.bot = bot
        # manager param kept for backwards compatibility but not used
        self.check_messages_task.start()

    def cog_unload(self):
        """Clean up when cog is unloaded"""
        self.check_messages_task.cancel()

    def _get_manager(self, guild_id: int):
        """Get the manager for a specific guild"""
        if hasattr(self.bot, "timed_messages_managers"):
            return self.bot.timed_messages_managers.get(guild_id)
        # Fallback for backwards compatibility
        if hasattr(self.bot, "timed_messages_manager"):
            return self.bot.timed_messages_manager
        return None

    @tasks.loop(minutes=1)
    async def check_messages_task(self):
        """Background task to check and send timed messages for all guilds"""
        if not hasattr(self.bot, "timed_messages_managers"):
            return

        for guild_id, manager in self.bot.timed_messages_managers.items():
            # Tag this guild's logging with the server name.
            _g = self.bot.get_guild(int(guild_id)) if guild_id else None
            set_server(guild_id, _g.name if _g else None)

            # Get active chatters count for this guild
            active_chatters_count = 0
            if hasattr(self.bot, "get_active_chatters_count"):
                active_chatters_count = self.bot.get_active_chatters_count(guild_id)

            await manager.check_and_send_messages(active_chatters_count)

    @check_messages_task.before_loop
    async def before_check_messages(self):
        """Wait for bot to be ready before starting task"""
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="addtimer", aliases=["addtimedmessage", "timermessage"])
    @commands.has_permissions(administrator=True)
    async def add_timed_message(self, ctx, interval: int, *, message: str):
        """
        [ADMIN] Add a timed message to Kick chat
        Usage: /addtimer <minutes> <message>
        Example: /addtimer 30 Check out our Discord: discord.gg/example
        """
        manager = self._get_manager(ctx.guild.id)
        if not manager:
            await ctx.send("❌ Timed messages not initialized for this server")
            return

        result = manager.add_message(message, interval, ctx.author.id)

        if result["status"] == "success":
            embed = discord.Embed(
                title="✅ Timed Message Added",
                description=f"Message #{result['message_id']} will be sent every {interval} minutes",
                color=discord.Color.green(),
            )
            embed.add_field(name="Message", value=message, inline=False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ {result['message']}")

    @commands.hybrid_command(name="removetimer", aliases=["deletetimer", "rmtimer"])
    @commands.has_permissions(administrator=True)
    async def remove_timed_message(self, ctx, message_id: int):
        """
        [ADMIN] Remove a timed message
        Usage: /removetimer <message_id>
        Example: /removetimer 1
        """
        manager = self._get_manager(ctx.guild.id)
        if not manager:
            await ctx.send("❌ Timed messages not initialized for this server")
            return

        result = manager.remove_message(message_id)

        if result["status"] == "success":
            await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")

    @commands.hybrid_command(name="toggletimer", aliases=["enabletimer", "disabletimer"])
    @commands.has_permissions(administrator=True)
    async def toggle_timed_message(self, ctx, message_id: int, enabled: str = None):
        """
        [ADMIN] Enable or disable a timed message
        Usage: /toggletimer <message_id> <on|off>
        Example: /toggletimer 1 off
        """
        manager = self._get_manager(ctx.guild.id)
        if not manager:
            await ctx.send("❌ Timed messages not initialized for this server")
            return
        if enabled is None:
            # Toggle current state
            msg = self.manager.get_message(message_id)
            if not msg:
                await ctx.send(f"❌ Message #{message_id} not found")
                return
            enabled_bool = not msg.enabled
        else:
            enabled_bool = enabled.lower() in ["on", "enable", "enabled", "true", "yes"]

        result = manager.toggle_message(message_id, enabled_bool)

        if result["status"] == "success":
            await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")

    @commands.hybrid_command(name="updatetimer", aliases=["settimer"])
    @commands.has_permissions(administrator=True)
    async def update_timer_interval(self, ctx, message_id: int, interval: int):
        """
        [ADMIN] Update the interval of a timed message
        Usage: /updatetimer <message_id> <minutes>
        Example: /updatetimer 1 60
        """
        manager = self._get_manager(ctx.guild.id)
        if not manager:
            await ctx.send("❌ Timed messages not initialized for this server")
            return

        result = manager.update_interval(message_id, interval)

        if result["status"] == "success":
            await ctx.send(f"✅ {result['message']}")
        else:
            await ctx.send(f"❌ {result['message']}")

    @commands.hybrid_command(name="listtimers", aliases=["timers", "timedmessages"])
    @commands.has_permissions(administrator=True)
    async def list_timed_messages(self, ctx):
        """
        [ADMIN] List all timed messages
        Usage: /listtimers
        """
        manager = self._get_manager(ctx.guild.id)
        if not manager:
            await ctx.send("❌ Timed messages not initialized for this server")
            return

        messages = manager.list_messages()

        if not messages:
            await ctx.send("📭 No timed messages configured")
            return

        embed = discord.Embed(
            title="⏰ Timed Messages", description=f"{len(messages)} message(s) configured", color=discord.Color.blue()
        )

        for msg in messages:
            status = "✅ Enabled" if msg.enabled else "❌ Disabled"
            last_sent = msg.last_sent.strftime("%Y-%m-%d %H:%M UTC") if msg.last_sent else "Never"

            # Calculate next send time
            if msg.enabled and msg.last_sent:
                next_send = msg.last_sent + timedelta(minutes=msg.interval_minutes)
                time_until = next_send - datetime.utcnow()
                if time_until.total_seconds() > 0:
                    minutes_left = int(time_until.total_seconds() / 60)
                    next_info = f"in {minutes_left}m"
                else:
                    next_info = "due now"
            else:
                next_info = "waiting to start"

            value = (
                f"{status}\n"
                f"**Interval:** {msg.interval_minutes} minutes\n"
                f"**Last sent:** {last_sent}\n"
                f"**Next:** {next_info}\n"
                f"**Message:** {msg.message[:100]}"
            )

            embed.add_field(name=f"Message #{msg.message_id}", value=value, inline=False)

        await ctx.send(embed=embed)

    @commands.hybrid_command(name="timerpanel", aliases=["managetimers"])
    @commands.has_permissions(administrator=True)
    async def timer_control_panel(self, ctx):
        """
        [ADMIN] Open interactive timer management panel
        Usage: /timerpanel

        React to manage timers:
        📋 - Refresh panel
        ✅ - Enable timer (react to timer field)
        ❌ - Disable timer (react to timer field)
        🗑️ - Delete timer (react to timer field)
        """
        manager = self._get_manager(ctx.guild.id)
        if not manager:
            await ctx.send("❌ Timed messages not initialized for this server")
            return

        messages = manager.list_messages()

        embed = discord.Embed(
            title="⏰ Timed Messages Control Panel",
            description="React to this message to manage timers:\n\n"
            "♻️ - Refresh panel\n"
            "📋 - Show list of timers\n"
            "❌ - Disable timer (will ask for ID)\n"
            "✅ - Enable timer (will ask for ID)",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )

        if not messages:
            embed.add_field(
                name="📭 No Timers", value="Use `/addtimer <minutes> <message>` to create one!", inline=False
            )
        else:
            # Show summary of all messages
            enabled_count = sum(1 for m in messages if m.enabled)
            disabled_count = len(messages) - enabled_count

            summary = f"**Total:** {len(messages)} timer(s)\n"
            summary += f"✅ **Enabled:** {enabled_count}\n"
            summary += f"❌ **Disabled:** {disabled_count}"

            embed.add_field(name="📊 Summary", value=summary, inline=False)

            # Show each timer with ID for commands
            for i, msg in enumerate(messages[:10], 1):  # Show max 10
                status_emoji = "✅" if msg.enabled else "❌"
                last_sent = msg.last_sent.strftime("%H:%M") if msg.last_sent else "Never"

                # Calculate next send time
                if msg.enabled and msg.last_sent:
                    next_send = msg.last_sent + timedelta(minutes=msg.interval_minutes)
                    time_until = next_send - datetime.utcnow()
                    if time_until.total_seconds() > 0:
                        minutes_left = int(time_until.total_seconds() / 60)
                        next_info = f"in {minutes_left}m"
                    else:
                        next_info = "due now"
                else:
                    next_info = "waiting"

                field_value = (
                    f"{status_emoji} **ID:** {msg.message_id} | **Every:** {msg.interval_minutes}m | **Next:** {next_info}\n"
                    f"💬 {msg.message[:60]}{'...' if len(msg.message) > 60 else ''}"
                )

                embed.add_field(name=f"Timer #{i}", value=field_value, inline=False)

            if len(messages) > 10:
                embed.add_field(
                    name="ℹ️ More Timers",
                    value=f"Showing 10 of {len(messages)}. Use `/listtimers` to see all.",
                    inline=False,
                )

        # Add command reference
        embed.add_field(
            name="📝 Commands",
            value=(
                "`/addtimer <min> <msg>` • `/removetimer <id>`\n"
                "`/toggletimer <id> on/off` • `/updatetimer <id> <min>`"
            ),
            inline=False,
        )

        embed.set_footer(text=f"Checks every 1 minute • React to manage")

        panel = await ctx.send(embed=embed)

        # Add reaction buttons
        try:
            await panel.add_reaction("♻️")  # Refresh
            await panel.add_reaction("📋")  # Show list
            await panel.add_reaction("❌")  # Disable (asks for ID)
            await panel.add_reaction("✅")  # Enable (asks for ID)
        except discord.HTTPException as e:
            logger.error(f"Failed to add reactions: {e}")
            await ctx.send(f"⚠️ Warning: Could not add reaction buttons (Discord API error)")

        # Store panel in database for reaction handling
        manager = self._get_manager(ctx.guild.id)
        if manager and manager.engine:
            try:
                with manager.engine.begin() as conn:
                    conn.execute(
                        text(
                            """
                        INSERT INTO timer_panels (guild_id, channel_id, message_id)
                        VALUES (:guild, :channel, :message)
                        ON CONFLICT (guild_id, channel_id, message_id) DO NOTHING
                    """
                        ),
                        {"guild": ctx.guild.id, "channel": ctx.channel.id, "message": panel.id},
                    )
                logger.info(f"Stored timer panel {panel.id}")
            except Exception as e:
                logger.error(f"Failed to store timer panel: {e}")


async def setup_timed_messages(bot, engine, kick_send_callback=None):
    """
    Initialize timed messages system with per-guild instances

    Args:
        bot: Discord bot instance
        engine: SQLAlchemy engine
        kick_send_callback: Callback function to send messages to Kick chat

    Returns:
        Dict of TimedMessagesManager instances keyed by guild_id
    """
    managers = {}

    # Create a manager instance for each guild
    for guild in bot.guilds:
        manager = TimedMessagesManager(engine, kick_send_callback, guild_id=guild.id)
        managers[guild.id] = manager
        logger.debug(f"✅ Timed messages initialized ({len(manager.messages)} messages)")

    # Add commands cog only once (it will access managers via bot attribute)
    if managers:
        # Use first guild's manager for the cog (it will be updated to use per-guild managers)
        first_manager = next(iter(managers.values()))
        await bot.add_cog(TimedMessagesCommands(bot, first_manager))

    return managers
