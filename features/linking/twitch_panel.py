"""
Twitch Link Panel — interactive Discord panel for Twitch account linking.

Mirrors features/linking/link_panel.py (Kick) but uses panel_type='twitch_link',
its own button custom_id, and the Twitch viewer-OAuth URL generator. Links are
written with platform='twitch' by the bot's /auth/twitch/link/callback route, so a
viewer can link a Kick AND a Twitch account in the same server.
"""

import logging

import discord
from discord.ext import commands
from discord.ui import Button, View
from sqlalchemy import text

logger = logging.getLogger(__name__)


class TwitchLinkPanelView(View):
    """Button view for the Twitch link panel."""

    def __init__(self, bot, engine, oauth_url_generator):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot
        self.engine = engine
        self.oauth_url_generator = oauth_url_generator

    @discord.ui.button(
        style=discord.ButtonStyle.success,
        label="Link Twitch",
        emoji="🟣",
        custom_id="link_twitch_account",
    )
    async def link_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.handle_link_account(interaction)
        except Exception as e:
            logger.error(f"Error handling Twitch link button interaction: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def handle_link_account(self, interaction: discord.Interaction):
        discord_id = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else None

        # Already linked on Twitch for this server?
        try:
            with self.engine.connect() as conn:
                existing = conn.execute(
                    text(
                        """
                        SELECT kick_name FROM links
                        WHERE discord_id = :d AND (:sid IS NULL OR discord_server_id = :sid)
                        AND platform = 'twitch'
                        """
                    ),
                    {"d": discord_id, "sid": guild_id},
                ).fetchone()
                if existing:
                    await interaction.response.send_message(
                        f"✅ Your Twitch is already linked to **{existing[0]}**!", ephemeral=True
                    )
                    return
        except Exception as e:
            logger.error(f"Error checking existing Twitch link: {e}")
            await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
            return

        try:
            oauth_url = self.oauth_url_generator(discord_id, guild_id)
        except Exception as e:
            logger.error(f"Error generating Twitch OAuth URL: {e}")
            await interaction.response.send_message(
                "❌ Failed to generate OAuth link. Please contact an admin.", ephemeral=True
            )
            return

        view = View(timeout=600)
        view.add_item(Button(label="Authorize with Twitch", style=discord.ButtonStyle.link, url=oauth_url, emoji="🟣"))
        await interaction.response.send_message(
            "🟣 Click the button below to link your Twitch account (expires in 10 minutes):",
            view=view,
            ephemeral=True,
        )
        logger.info(f"Sent Twitch OAuth link to {interaction.user.name} (ID: {discord_id})")


class TwitchLinkPanel:
    """Manages the Twitch link panel message for a specific guild."""

    PANEL_TYPE = "twitch_link"

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
        if not self.engine or self.guild_id is None:
            return
        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                        SELECT guild_id, channel_id, message_id
                        FROM link_panels
                        WHERE guild_id = :guild_id AND panel_type = :ptype
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"guild_id": self.guild_id, "ptype": self.PANEL_TYPE},
                ).fetchone()
                if result:
                    self.panel_guild_id = result[0]
                    self.panel_channel_id = result[1]
                    self.panel_message_id = result[2]
                    logger.debug(f"Loaded Twitch link panel info for guild {self.guild_id}")
        except Exception as e:
            logger.error(f"Failed to load Twitch link panel info for guild {self.guild_id}: {e}")

    def _save_panel_info(self, guild_id: int, channel_id: int, message_id: int):
        if not self.engine:
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM link_panels WHERE guild_id = :guild_id AND panel_type = :ptype"),
                    {"guild_id": guild_id, "ptype": self.PANEL_TYPE},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO link_panels (guild_id, channel_id, message_id, emoji, panel_type, created_at)
                        VALUES (:guild_id, :channel_id, :message_id, '🟣', :ptype, CURRENT_TIMESTAMP)
                        """
                    ),
                    {
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "ptype": self.PANEL_TYPE,
                    },
                )
        except Exception as e:
            logger.error(f"Failed to save Twitch link panel info: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        try:
            embed = discord.Embed(
                title="🟣 Link Your Twitch Account",
                description=(
                    "Link your Twitch account with Discord to participate in raffles and track your watchtime!\n\n"
                    "**Benefits:**\n"
                    "• Earn raffle tickets from watchtime\n"
                    "• Get bonus tickets from subscriptions\n"
                    "• Participate in raffles and giveaways\n"
                    "• Earn roles based on Twitch chat activity\n"
                    "• Track your stats and progress"
                ),
                color=0x9146FF,  # Twitch purple
            )
            embed.add_field(
                name="How to Link",
                value="Click the **'Link Twitch'** button below to get started!",
                inline=False,
            )
            embed.add_field(
                name="🔒 Privacy & Security",
                value=(
                    "• OAuth links are unique and expire after 10 minutes\n"
                    "• Links are sent in private messages only you can see\n"
                    "• Your Twitch password is never shared with this bot"
                ),
                inline=False,
            )
            embed.set_footer(text="Click 'Link Twitch' to get your personal OAuth link")

            view = TwitchLinkPanelView(self.bot, self.engine, self.oauth_url_generator)
            message = await channel.send(embed=embed, view=view)

            self.panel_guild_id = channel.guild.id
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.guild.id, channel.id, message.id)

            logger.info(f"Created Twitch link panel in {channel.guild.name} / #{channel.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create Twitch link panel: {e}")
            return False


async def setup_twitch_link_panel_system(bot, engine, oauth_url_generator):
    """Setup the Twitch link panel system with per-guild instances."""
    panels = {}
    for guild in bot.guilds:
        panels[guild.id] = TwitchLinkPanel(bot, engine, oauth_url_generator, guild_id=guild.id)
        logger.debug("✅ Twitch link panel initialized")

    @bot.hybrid_command(name="createtwitchpanel")
    @commands.has_permissions(administrator=True)
    async def create_twitch_panel_cmd(ctx):
        """[ADMIN] Create the Twitch link panel in this channel."""
        panel = panels.get(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Twitch link panel not initialized for this server")
            return
        if await panel.create_panel(ctx.channel):
            await ctx.send("✅ Twitch link panel created! Users can now link their Twitch accounts.")
        else:
            await ctx.send("❌ Failed to create Twitch link panel. Check logs for details.")

    # Re-attach persistent views on restart.
    for guild_id, panel in panels.items():
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    message = await channel.fetch_message(panel.panel_message_id)
                    await message.edit(view=TwitchLinkPanelView(bot, engine, oauth_url_generator))
                    logger.debug(f"Re-attached Twitch link panel view in guild {guild_id}")
            except Exception as e:
                logger.error(f"Failed to re-attach Twitch link panel view for guild {guild_id}: {e}")

    return panels
