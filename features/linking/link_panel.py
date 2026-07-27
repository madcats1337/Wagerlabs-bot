"""
Link Panel - Interactive Discord panel for Kick OAuth linking
Uses buttons and ephemeral messages instead of reactions and DMs
"""

import logging

import discord
from discord.ext import commands
from discord.ui import Button, View
from sqlalchemy import text

logger = logging.getLogger(__name__)


class LinkPanelView(View):
    """Button view for link panel"""

    def __init__(self, bot, engine, oauth_url_generator):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot
        self.engine = engine
        self.oauth_url_generator = oauth_url_generator

    @discord.ui.button(style=discord.ButtonStyle.success, label="Link Account", emoji="🔗", custom_id="link_account")
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle link account button click"""
        try:
            await self.handle_link_account(interaction)
        except Exception as e:
            logger.error(f"Error handling link button interaction: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """This runs before any button callback"""
        return True  # Allow everyone to use the link button

    async def handle_link_account(self, interaction: discord.Interaction):
        """Handle link account button click"""
        discord_id = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else None

        # Check if already linked
        try:
            with self.engine.connect() as conn:
                server_id = guild_id
                existing = conn.execute(
                    text(
                        "SELECT kick_name FROM links WHERE discord_id = :d AND (:sid IS NULL OR discord_server_id = :sid)"
                    ),
                    {"d": discord_id, "sid": server_id},
                ).fetchone()

                if existing:
                    await interaction.response.send_message(
                        f"✅ You are already linked to **{existing[0]}**!", ephemeral=True
                    )
                    return
        except Exception as e:
            logger.error(f"Error checking existing link: {e}")
            await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
            return

        # Generate OAuth URL with guild_id
        try:
            oauth_url = self.oauth_url_generator(discord_id, guild_id)
        except Exception as e:
            logger.error(f"Error generating OAuth URL: {e}")
            await interaction.response.send_message(
                "❌ Failed to generate OAuth link. Please contact an admin.", ephemeral=True
            )
            return

        # Create simple view with just the OAuth link button
        view = View(timeout=600)  # 10 minute timeout
        oauth_button = Button(label="Authorize with Kick", style=discord.ButtonStyle.link, url=oauth_url, emoji="🎮")
        view.add_item(oauth_button)

        # Send simple ephemeral message - just the button, no extra steps
        await interaction.response.send_message(
            "🔗 Click the button below to link your Kick account (expires in 10 minutes):", view=view, ephemeral=True
        )

        logger.info(f"Sent OAuth link to {interaction.user.name} (ID: {discord_id}) via ephemeral message")


class LinkPanel:
    """Manages the link panel message for a specific guild"""

    def __init__(self, bot, engine, oauth_url_generator, guild_id=None):
        self.bot = bot
        self.engine = engine
        self.oauth_url_generator = oauth_url_generator
        self.guild_id = guild_id
        self.panel_message_id = None
        self.panel_channel_id = None
        self.panel_guild_id = None
        self._load_panel_info()

    def _load_panel_info(self):
        """Load panel message info from database for this guild

        In multiserver deployments, loads only this guild's panel.
        """
        if not self.engine or self.guild_id is None:
            return

        try:
            with self.engine.connect() as conn:
                # Get the most recent link panel for this guild
                result = conn.execute(
                    text(
                        """
                    SELECT guild_id, channel_id, message_id
                    FROM link_panels
                    WHERE guild_id = :guild_id AND panel_type = 'kick_link'
                    ORDER BY created_at DESC
                    LIMIT 1
                """
                    ),
                    {"guild_id": self.guild_id},
                ).fetchone()

                if result:
                    self.panel_guild_id = result[0]
                    self.panel_channel_id = result[1]
                    self.panel_message_id = result[2]
                    logger.debug(f"Loaded link panel info for guild {self.guild_id}")

        except Exception as e:
            logger.error(f"Failed to load link panel info for guild {self.guild_id}: {e}")

    def _save_panel_info(self, guild_id: int, channel_id: int, message_id: int):
        """Save panel message info to database"""
        if not self.engine:
            return

        try:
            with self.engine.begin() as conn:
                # Delete old Kick link panels for this guild (leave other panel
                # types — e.g. the Shuffle verify panel — untouched)
                conn.execute(
                    text(
                        """
                    DELETE FROM link_panels WHERE guild_id = :guild_id AND panel_type = 'kick_link'
                """
                    ),
                    {"guild_id": guild_id},
                )

                # Insert new panel info
                conn.execute(
                    text(
                        """
                    INSERT INTO link_panels (guild_id, channel_id, message_id, emoji, panel_type, created_at)
                    VALUES (:guild_id, :channel_id, :message_id, '🔗', 'kick_link', CURRENT_TIMESTAMP)
                """
                    ),
                    {"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id},
                )

        except Exception as e:
            logger.error(f"Failed to save link panel info: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        """Create a new link panel in the specified channel"""
        try:
            # Create the embed
            embed = discord.Embed(
                title="🔗 Link Your Kick Account",
                description=(
                    "Link your Kick account with Discord to participate in raffles and track your watchtime!\n\n"
                    "**Benefits:**\n"
                    "• Earn raffle tickets from watchtime\n"
                    "• Get bonus tickets from subscriptions\n"
                    "• Participate in raffles and giveaways\n"
                    "• Earn roles based on Kick chat activity\n"
                    "• Track your stats and progress"
                ),
                color=0x53FC18,
            )
            embed.add_field(
                name="How to Link", value="Click the **'Link Account'** button below to get started!", inline=False
            )
            embed.add_field(
                name="🔒 Privacy & Security",
                value=(
                    "• OAuth links are unique and expire after 10 minutes\n"
                    "• Links are sent in private messages only you can see\n"
                    "• Your Kick password is never shared with this bot"
                ),
                inline=False,
            )
            embed.set_footer(text="Click 'Link Account' to get your personal OAuth link")

            # Create the view
            view = LinkPanelView(self.bot, self.engine, self.oauth_url_generator)

            # Send the message
            message = await channel.send(embed=embed, view=view)

            # Save panel info
            self.panel_guild_id = channel.guild.id
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.guild.id, channel.id, message.id)

            logger.info(f"Created link panel in {channel.guild.name} / #{channel.name}")
            return True

        except Exception as e:
            logger.error(f"Failed to create link panel: {e}")
            return False


async def setup_link_panel_system(bot, engine, oauth_url_generator):
    """Setup the link panel system with per-guild instances"""
    panels = {}

    # Create a panel instance for each guild
    for guild in bot.guilds:
        panel = LinkPanel(bot, engine, oauth_url_generator, guild_id=guild.id)
        panels[guild.id] = panel
        logger.debug(f"✅ Link panel initialized")

    # Add command only once
    @bot.hybrid_command(name="createlinkpanel")
    @commands.has_permissions(administrator=True)
    async def create_link_panel_cmd(ctx):
        """[ADMIN] Create the link panel in this channel"""
        panel = panels.get(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Link panel not initialized for this server")
            return

        success = await panel.create_panel(ctx.channel)
        if success:
            await ctx.send("✅ Link panel created! Users can now click the button to link their accounts.")
        else:
            await ctx.send("❌ Failed to create link panel. Check logs for details.")

    # Re-attach views to existing panels on bot restart
    from utils.log_context import clear_server, set_server

    for guild_id, panel in panels.items():
        # Tag this guild's panel-refresh logs with the server (not "[-]").
        _g = bot.get_guild(guild_id) if guild_id else None
        set_server(guild_id, _g.name if _g else None)
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    message = await channel.fetch_message(panel.panel_message_id)
                    view = LinkPanelView(bot, engine, oauth_url_generator)

                    # Re-attach the view
                    await message.edit(view=view)
                    logger.debug(f"Re-attached link panel view to existing message in guild {guild_id}")
            except Exception as e:
                logger.error(f"Failed to re-attach link panel view for guild {guild_id}: {e}")

    # Don't let the last guild's tag bleed into subsequent global startup logs.
    clear_server()
    return panels
