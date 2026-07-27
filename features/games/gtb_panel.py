"""
Guess the Balance Panel - Interactive Discord panel for managing GTB sessions
Shows session status, guess count, and controls for opening/closing sessions and setting results
"""

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View
from sqlalchemy import text

from features.discord_app_commands import defer_slash_response
from features.games.guess_the_balance import gtb_rank_marker, parse_amount

logger = logging.getLogger(__name__)


class SetResultModal(Modal, title="Set Result Amount"):
    """Modal for setting the final balance result"""

    result_amount = TextInput(
        label="Final Balance Amount",
        placeholder="Enter the final balance (e.g., 1234.56)",
        required=True,
        min_length=1,
        max_length=20,
    )

    def __init__(self, panel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        try:
            # Parse the amount
            amount = parse_amount(self.result_amount.value)

            if amount is None or amount <= 0:
                await interaction.response.send_message("❌ Please enter a valid positive number.", ephemeral=True)
                return

            # Set the result and calculate winners
            success, message, winners = self.panel.gtb_manager.set_result(amount)

            if success and winners:
                # Create winner announcement embed
                embed = discord.Embed(
                    title="🏆 Guess the Balance - Results!",
                    description=f"**Final Balance:** ${amount:,.2f}",
                    color=discord.Color.gold(),
                    timestamp=datetime.utcnow(),
                )

                # Add winners (medal for the podium, #N beyond it).
                for winner in winners:
                    medal = gtb_rank_marker(winner["rank"])
                    embed.add_field(
                        name=f"{medal} {winner['username']}",
                        value=f"Guess: ${winner['guess']:,.2f} (off by ${winner['difference']:,.2f})",
                        inline=False,
                    )

                embed.set_footer(text="Congratulations to the winners! 🎉")

                await interaction.response.send_message(embed=embed)

                # Announce to Kick chat if available
                if self.panel.kick_send_callback:
                    try:
                        kick_msg = f"🏆 GTB RESULTS - Final Balance: ${amount:,.2f} | "
                        winner_texts = []
                        for winner in winners:
                            medal = gtb_rank_marker(winner["rank"])
                            winner_texts.append(f"{medal} {winner['username']} (${winner['guess']:,.2f})")
                        kick_msg += " | ".join(winner_texts)
                        await self.panel.kick_send_callback(kick_msg, guild_id=self.panel.guild_id)
                        logger.info(f"Announced GTB winners to stream chat")
                    except Exception as e:
                        logger.error(f"Failed to announce winners to chat: {e}")

                # Update the panel
                await self.panel.update_panel(force=True)

            elif success:
                await interaction.response.send_message(f"⚠️ {message}", ephemeral=True)
                await self.panel.update_panel(force=True)
            else:
                await interaction.response.send_message(f"❌ {message}", ephemeral=True)

        except ValueError:
            await interaction.response.send_message("❌ Please enter a valid number.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error setting GTB result: {e}")
            await interaction.response.send_message("❌ An error occurred while setting the result.", ephemeral=True)


class GTBPanelView(View):
    """Button view for GTB panel - looks up panel by guild_id for multi-server support"""

    def __init__(self, bot=None):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has permission to use buttons"""
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can use this panel.", ephemeral=True)
            return False
        return True

    def _get_panel(self, interaction: discord.Interaction):
        """Get the panel for the interaction's guild"""
        if not self.bot or not hasattr(self.bot, "gtb_panels_by_guild"):
            return None
        return self.bot.gtb_panels_by_guild.get(interaction.guild_id)

    @discord.ui.button(style=discord.ButtonStyle.success, label="Open Session", emoji="🎮", custom_id="gtb_open")
    async def open_button(self, interaction: discord.Interaction, button: Button):
        """Handle open session button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.open_session_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Close Session", emoji="🔒", custom_id="gtb_close")
    async def close_button(self, interaction: discord.Interaction, button: Button):
        """Handle close session button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.close_session_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.primary, label="Set Result", emoji="💰", custom_id="gtb_result")
    async def result_button(self, interaction: discord.Interaction, button: Button):
        """Handle set result button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.set_result_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Refresh", emoji="♻️", custom_id="gtb_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        """Handle refresh button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.refresh_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )


class GTBPanel:
    """Manages the Guess the Balance panel message"""

    def __init__(self, bot, engine, gtb_manager, kick_send_callback=None, guild_id=None):
        self.bot = bot
        self.engine = engine
        self.gtb_manager = gtb_manager
        self.kick_send_callback = kick_send_callback
        self.guild_id = guild_id or (gtb_manager.server_id if gtb_manager else None)
        self.panel_message_id = None
        self.panel_channel_id = None
        self.last_update_time = None
        self.update_cooldown = 30  # Minimum 30 seconds between updates
        self._load_panel_info()

    def _load_panel_info(self):
        """Load panel message ID and channel from database"""
        if not self.engine or not self.guild_id:
            return

        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'gtb_panel_message_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": str(self.guild_id)},
                ).fetchone()

                if result:
                    self.panel_message_id = int(result[0])

                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'gtb_panel_channel_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": str(self.guild_id)},
                ).fetchone()

                if result:
                    self.panel_channel_id = int(result[0])

        except Exception as e:
            logger.error(f"Failed to load GTB panel info: {e}")

    def _save_panel_info(self):
        """Save panel message ID and channel to database"""
        if not self.engine or not self.guild_id:
            return

        try:
            with self.engine.begin() as conn:
                if self.panel_message_id:
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                        VALUES ('gtb_panel_message_id', :value, :guild_id, CURRENT_TIMESTAMP)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = :value, updated_at = CURRENT_TIMESTAMP
                    """
                        ),
                        {"value": str(self.panel_message_id), "guild_id": str(self.guild_id)},
                    )

                if self.panel_channel_id:
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                        VALUES ('gtb_panel_channel_id', :value, :guild_id, CURRENT_TIMESTAMP)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = :value, updated_at = CURRENT_TIMESTAMP
                    """
                        ),
                        {"value": str(self.panel_channel_id), "guild_id": str(self.guild_id)},
                    )

        except Exception as e:
            logger.error(f"Failed to save GTB panel info: {e}")

    def _create_panel_embed(self):
        """Create the panel embed with current session info"""
        # Get active session
        session = self.gtb_manager.get_active_session()

        embed = discord.Embed(
            title="🎮 Guess the Balance", color=discord.Color.green() if session else discord.Color.red()
        )

        if session:
            # Get guess count
            try:
                with self.engine.connect() as conn:
                    guess_count = conn.execute(
                        text(
                            """
                        SELECT COUNT(*) FROM gtb_guesses WHERE session_id = :session_id
                    """
                        ),
                        {"session_id": session["id"]},
                    ).fetchone()[0]
            except Exception:
                guess_count = 0

            status_text = "🟢 **OPEN**" if session["status"] == "open" else "🔴 **CLOSED**"
            embed.add_field(name="Status", value=status_text, inline=True)
            embed.add_field(name="Session #", value=f"{session['id']}", inline=True)
            embed.add_field(name="Guesses", value=f"{guess_count}", inline=True)

            opened_time = session["opened_at"].strftime("%H:%M:%S") if session["opened_at"] else "Unknown"
            embed.add_field(
                name="Session Info",
                value=f"Opened by: {session['opened_by']}\nOpened at: {opened_time} UTC",
                inline=False,
            )

            if session["status"] == "open":
                embed.add_field(
                    name="How to Play",
                    value="Users can guess in Kick chat with: `!gtb <amount>`\nExample: `!gtb 1234.56`",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Session Closed", value="Use **Set Result** button to calculate winners", inline=False
                )
        else:
            embed.add_field(name="Status", value="⚪ **NO ACTIVE SESSION**", inline=False)
            embed.add_field(name="Get Started", value="Click **Open Session** to start a new game!", inline=False)

            # Show last completed session stats for THIS server
            try:
                with self.engine.connect() as conn:
                    last_session = conn.execute(
                        text(
                            """
                        SELECT id, result_amount, opened_at
                        FROM gtb_sessions
                        WHERE status = 'completed' AND discord_server_id = :server_id
                        ORDER BY closed_at DESC
                        LIMIT 1
                    """
                        ),
                        {"server_id": self.guild_id},
                    ).fetchone()

                    if last_session:
                        session_id, result, opened_at = last_session
                        embed.add_field(
                            name="Last Session",
                            value=f"Session #{session_id} - Result: ${float(result):,.2f}",
                            inline=False,
                        )
            except Exception:
                pass

        embed.set_footer(text=f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")

        return embed

    async def create_panel(self, channel: discord.TextChannel):
        """Create a new panel in the specified channel"""
        try:
            embed = self._create_panel_embed()
            view = GTBPanelView(self.bot)  # Pass bot for guild lookup

            message = await channel.send(embed=embed, view=view)

            self.panel_message_id = message.id
            self.panel_channel_id = channel.id
            self._save_panel_info()

            logger.info(f"Created GTB panel in {channel.name}")
            return True

        except Exception as e:
            logger.error(f"Failed to create GTB panel: {e}")
            return False

    async def update_panel(self, force=False):
        """Update the panel embed"""
        # Rate limiting
        if not force and self.last_update_time:
            time_since_last = (datetime.utcnow() - self.last_update_time).total_seconds()
            if time_since_last < self.update_cooldown:
                logger.debug(
                    f"GTB panel update skipped (cooldown: {self.update_cooldown - time_since_last:.1f}s remaining)"
                )
                return False

        if not self.panel_message_id or not self.panel_channel_id:
            return False

        try:
            channel = self.bot.get_channel(self.panel_channel_id)
            if not channel:
                return False

            message = await channel.fetch_message(self.panel_message_id)
            embed = self._create_panel_embed()
            view = GTBPanelView(self.bot)  # Pass bot for guild lookup

            await message.edit(embed=embed, view=view)
            self.last_update_time = datetime.utcnow()

            return True

        except discord.NotFound:
            logger.warning("GTB panel message not found, needs to be recreated")
            self.panel_message_id = None
            self.panel_channel_id = None
            return False
        except Exception as e:
            logger.error(f"Failed to update GTB panel: {e}")
            return False

    async def open_session_interaction(self, interaction: discord.Interaction):
        """Handle open session button click"""
        await interaction.response.defer(ephemeral=True)

        opener = interaction.user.name
        success, message = self.gtb_manager.open_session(opener)

        if success:
            await interaction.followup.send(f"✅ {message}", ephemeral=True)

            # Announce to Kick chat
            if self.kick_send_callback:
                try:
                    await self.kick_send_callback(
                        "🎮 Guess the Balance is now OPEN! Type !gtb <amount> to guess the final balance. "
                        "Example: !gtb 1234.56",
                        guild_id=self.guild_id,
                    )
                except Exception as e:
                    logger.error(f"Failed to announce session open to chat: {e}")

            await self.update_panel(force=True)
        else:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)

    async def close_session_interaction(self, interaction: discord.Interaction):
        """Handle close session button click"""
        await interaction.response.defer(ephemeral=True)

        success, message, session_id = self.gtb_manager.close_session()

        if success:
            await interaction.followup.send(f"✅ {message}", ephemeral=True)

            # Announce to Kick chat
            if self.kick_send_callback:
                try:
                    await self.kick_send_callback(
                        f"🔒 Guess the Balance session is now CLOSED! No more guesses accepted. "
                        f"Waiting for final result...",
                        guild_id=self.guild_id,
                    )
                except Exception as e:
                    logger.error(f"Failed to announce session close to chat: {e}")

            await self.update_panel(force=True)
        else:
            await interaction.followup.send(f"❌ {message}", ephemeral=True)

    async def set_result_interaction(self, interaction: discord.Interaction):
        """Handle set result button click - show modal"""
        modal = SetResultModal(self)
        await interaction.response.send_modal(modal)

    async def refresh_interaction(self, interaction: discord.Interaction):
        """Handle refresh button click"""
        await interaction.response.defer(ephemeral=True)

        success = await self.update_panel(force=True)
        if success:
            await interaction.followup.send("✅ Panel refreshed!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to refresh panel.", ephemeral=True)

    def start_auto_update(self):
        """Start the auto-update task"""
        if not self.auto_update.is_running():
            self.auto_update.start()

    @tasks.loop(minutes=3)
    async def auto_update(self):
        """Automatically update the panel every 3 minutes"""
        await self.update_panel()

    @auto_update.before_loop
    async def before_auto_update(self):
        """Wait for bot to be ready before starting auto-update"""
        await self.bot.wait_until_ready()


async def setup_gtb_panel(bot, engine, gtb_manager, kick_send_callback=None):
    """Setup the GTB panel"""
    panel = GTBPanel(bot, engine, gtb_manager, kick_send_callback)

    # Add command to create the panel
    @bot.hybrid_command(name="creategtbpanel")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def create_gtb_panel_cmd(ctx):
        """[ADMIN] Create the GTB panel in this channel"""
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        success = await panel.create_panel(ctx.channel)
        if success:
            await ctx.send("✅ GTB panel created!")
        else:
            await ctx.send("❌ Failed to create GTB panel.")

    # Start auto-update task
    panel.start_auto_update()

    return panel
