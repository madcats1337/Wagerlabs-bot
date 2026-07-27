"""
Howl Verify Panel - Interactive Discord panel for Howl.gg affiliate auto-verification.

A user clicks the "Verify Howl Account" button, enters their Howl.gg username in a
modal, and the bot checks that username against the live affiliate leaderboard
(GET {howl_affiliate_url} with the guild's howl_api_key). If the username appears,
the user is auto-verified (raffle_shuffle_links, platform='howl', verified=TRUE)
and granted the configured `howl_verified_role_id` role.

This is the howl counterpart of features/linking/shuffle_panel.py. The structure is
identical (embed + persistent button view + admin create command + re-attach on
restart); the differences are: the affiliate fetch (auth header + date window +
`{success, data:[{name, wageredUSD}]}` shape, mirroring
ShuffleWagerTracker._fetch_shuffle_data / _normalize_rows for howl), the
platform='howl' on insert + all link lookups scoped to platform='howl', the
howl_verified_role_id role, and the 'howl_verify' custom_id / panel_type.
"""

import asyncio
import logging
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, View
from sqlalchemy import text

logger = logging.getLogger(__name__)

EMBED_COLOR = 0x00E0A4  # Howl teal/green
HOWL_DEFAULT_LB_URL = "https://howl.gg/api/user/affiliate/lb"


async def _fetch_howl_affiliate_data(affiliate_url: str, api_key: str):
    """Fetch the howl affiliate leaderboard and normalize it to a username list.

    Mirrors ShuffleWagerTracker._fetch_shuffle_data + _normalize_rows for howl:
    GET with Authorization header + from/to/limit (current calendar month),
    parse `{success: True, data: [{name, wageredUSD, ...}]}`. Returns a list of
    `{"username": name}` dicts, or None on any failure.
    """
    if not api_key:
        logger.error("Howl verify: no howl_api_key configured")
        return None

    # Accept/UA match the tracker's: howl.gg's Cloudflare scores bare
    # python-client UAs worst, so identify honestly but not as one.
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; WagerlabsBot/1.0; +https://wagerlabs.app)",
    }
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    params = {
        "from": month_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "limit": "1000",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(affiliate_url, headers=headers, params=params, timeout=30) as response:
                if response.status != 200:
                    logger.error(f"Howl affiliate API returned status {response.status}")
                    return None

                raw = await response.json()
                if not isinstance(raw, dict) or not raw.get("success"):
                    logger.error(f"Unexpected howl affiliate API response: {str(raw)[:200]}")
                    return None

                rows = raw.get("data") or []
                return [{"username": r.get("name")} for r in rows if r.get("name")]
    except asyncio.TimeoutError:
        logger.error("Timeout fetching howl affiliate data")
        return None
    except Exception as e:
        logger.error(f"Error fetching howl affiliate data: {e}")
        return None


async def verify_and_grant(interaction: discord.Interaction, engine, settings_getter, entered_username: str):
    """Verify the entered Howl username against the affiliate leaderboard and grant the role."""
    discord_id = interaction.user.id
    guild = interaction.guild
    guild_id = guild.id if guild else None
    entered = (entered_username or "").strip()

    if not entered:
        await interaction.response.send_message("❌ Please enter your Howl username.", ephemeral=True)
        return

    # 1. Already-verified check — scoped to the HOWL platform.
    try:
        with engine.connect() as conn:
            existing = conn.execute(
                text(
                    "SELECT shuffle_username FROM raffle_shuffle_links " "WHERE discord_id = :d AND platform = 'howl'"
                ),
                {"d": discord_id},
            ).fetchone()
        if existing:
            await interaction.response.send_message(f"✅ You're already verified as **{existing[0]}**!", ephemeral=True)
            return
    except Exception as e:
        logger.error(f"Error checking existing Howl link: {e}")
        await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
        return

    # Resolve per-guild settings
    settings = settings_getter(guild_id) if guild_id is not None else None
    api_key = settings.get("howl_api_key") if settings else ""
    affiliate_url = (settings.get("howl_affiliate_url") if settings else "") or HOWL_DEFAULT_LB_URL

    if not api_key:
        await interaction.response.send_message(
            "❌ Howl verification isn't configured for this server yet. Please contact an admin.",
            ephemeral=True,
        )
        return

    # Gate mode: if a required role is configured, ONLY members who already have
    # that role may verify, and NO role is granted on success (the gate replaces
    # the grant). If blank, fall through to the normal grant flow below.
    required_role_id = settings.get("howl_required_role_id") if settings else None
    gate_mode = bool(required_role_id and str(required_role_id).strip())
    if gate_mode:
        if not guild or guild_id is None:
            await interaction.response.send_message("❌ Howl verification must be used in a server.", ephemeral=True)
            return
        try:
            required_role = guild.get_role(int(required_role_id))
        except (ValueError, TypeError):
            logger.error(f"Invalid howl_required_role_id {required_role_id!r} for guild {guild_id}")
            await interaction.response.send_message(
                "❌ Howl verification is misconfigured for this server. Please contact an admin.",
                ephemeral=True,
            )
            return
        if not required_role:
            logger.warning(f"Howl required role {required_role_id} not found in guild {guild.name}")
            await interaction.response.send_message(
                "❌ Howl verification is misconfigured for this server. Please contact an admin.",
                ephemeral=True,
            )
            return
        member = guild.get_member(discord_id)
        if not member or required_role not in member.roles:
            await interaction.response.send_message(
                f"❌ You need the **{required_role.name}** role to verify.", ephemeral=True
            )
            return

    # Defer: the affiliate fetch can take a few seconds
    await interaction.response.defer(ephemeral=True, thinking=True)

    # 2. Fetch the howl affiliate leaderboard
    data = await _fetch_howl_affiliate_data(affiliate_url, api_key)
    if data is None:
        await interaction.followup.send(
            "❌ Couldn't reach the Howl affiliate stats right now. Please try again later.",
            ephemeral=True,
        )
        return

    # 3. Match case-insensitive username. Howl has no per-row campaign code, and
    #    the leaderboard already contains only your affiliates, so no code filter.
    entered_lower = entered.lower()
    matched = None
    for row in data:
        if str(row.get("username", "")).lower() == entered_lower:
            matched = row
            break

    if not matched:
        await interaction.followup.send(
            f"❌ **{entered}** wasn't found in our Howl affiliate stats. Make sure you signed up "
            f"under our affiliate on Howl.gg, then try again.",
            ephemeral=True,
        )
        return

    matched_username = str(matched.get("username"))

    # 4a. Look up an existing Kick name for this Discord user (optional)
    kick_name = None
    try:
        with engine.connect() as conn:
            link_row = conn.execute(
                text(
                    "SELECT kick_name FROM links WHERE discord_id = :d "
                    "AND (:sid IS NULL OR discord_server_id = :sid)"
                ),
                {"d": discord_id, "sid": guild_id},
            ).fetchone()
        if link_row:
            kick_name = link_row[0]
    except Exception as e:
        logger.error(f"Error looking up Kick name for {discord_id}: {e}")

    # 4b. Persist the verified howl link
    result = _insert_verified_link(engine, matched_username, kick_name, discord_id)
    status = result.get("status")

    if status == "already_linked":
        await interaction.followup.send(
            f"❌ **{matched_username}** is already verified by another Discord account.", ephemeral=True
        )
        return
    if status == "discord_already_linked":
        await interaction.followup.send(
            f"✅ You're already verified as **{result.get('existing_username')}**!", ephemeral=True
        )
        return
    if status != "success":
        await interaction.followup.send("❌ Failed to save your verification. Please try again.", ephemeral=True)
        return

    # 4c. Grant the configured howl verified role — UNLESS we're in gate mode,
    # where the required role is the access control and no role is granted.
    role_note = ""
    if not gate_mode:
        role_note = await _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username)

    await interaction.followup.send(
        f"🎉 Verified! Your Howl account **{matched_username}** is now linked.{role_note}",
        ephemeral=True,
    )


def _insert_verified_link(engine, howl_username, kick_name, discord_id):
    """Insert a verified raffle_shuffle_links row with platform='howl' (self-verified).

    All lookups + the insert are scoped to platform='howl' so they never collide
    with a user's shuffle link (the table is UNIQUE on (shuffle_username, platform)
    and (discord_id, platform)).
    """
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                text(
                    "SELECT discord_id FROM raffle_shuffle_links " "WHERE shuffle_username = :u AND platform = 'howl'"
                ),
                {"u": howl_username},
            ).fetchone()
            if existing:
                return {"status": "already_linked", "existing_discord_id": existing[0]}

            discord_existing = conn.execute(
                text(
                    "SELECT shuffle_username FROM raffle_shuffle_links " "WHERE discord_id = :d AND platform = 'howl'"
                ),
                {"d": discord_id},
            ).fetchone()
            if discord_existing:
                return {"status": "discord_already_linked", "existing_username": discord_existing[0]}

            conn.execute(
                text(
                    """
                    INSERT INTO raffle_shuffle_links
                        (shuffle_username, kick_name, discord_id, platform, verified, verified_by_discord_id, verified_at)
                    VALUES
                        (:howl_username, :kick_name, :discord_id, 'howl', TRUE, :verified_by, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "howl_username": howl_username,
                    "kick_name": kick_name,
                    "discord_id": discord_id,
                    "verified_by": discord_id,  # self-verified
                },
            )
        logger.info(
            f"🔗 Auto-verified Howl link: {howl_username} → " f"{kick_name or '(no Kick link)'} (Discord: {discord_id})"
        )
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to insert verified Howl link: {e}")
        return {"status": "error", "error": str(e)}


async def _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username):
    """Grant the configured howl_verified_role_id role. Returns a note for the user."""
    if not guild or not guild_id:
        return ""

    try:
        with engine.connect() as conn:
            role_id = conn.execute(
                text(
                    "SELECT value FROM bot_settings "
                    "WHERE key = 'howl_verified_role_id' AND discord_server_id = :guild_id"
                ),
                {"guild_id": guild_id},
            ).scalar()
    except Exception as e:
        logger.error(f"Failed to query howl_verified_role_id: {e}")
        role_id = None

    if not role_id or not str(role_id).strip():
        return ""  # No role configured — verification still succeeded

    try:
        role = guild.get_role(int(role_id))
    except (ValueError, TypeError):
        logger.error(f"Invalid howl_verified_role_id {role_id!r} for guild {guild_id}")
        return ""

    if not role:
        logger.warning(f"Howl verified role {role_id} not found in guild {guild.name}")
        return ""

    member = guild.get_member(int(discord_id))
    if not member:
        logger.warning(f"Member {discord_id} not found in guild {guild.name}")
        return ""

    if role in member.roles:
        return f" You already have the **{role.name}** role."

    try:
        await member.add_roles(role, reason=f"Verified Howl account: {matched_username}")
        logger.info(f"✅ Granted role '{role.name}' to {member.display_name} for Howl verification")
        return f" You've been given the **{role.name}** role."
    except Exception as e:
        logger.error(f"Error granting Howl verified role: {e}")
        return ""


class HowlVerifyModal(Modal, title="Verify Your Howl Account"):
    """Modal that collects the user's Howl username."""

    howl_username = TextInput(
        label="Howl Username",
        placeholder="Your exact Howl.gg username",
        required=True,
        min_length=1,
        max_length=64,
    )

    def __init__(self, engine, settings_getter):
        super().__init__()
        self.engine = engine
        self.settings_getter = settings_getter

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await verify_and_grant(interaction, self.engine, self.settings_getter, self.howl_username.value)
        except Exception as e:
            logger.error(f"Error handling Howl verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An error occurred.", ephemeral=True)


class HowlPanelView(View):
    """Button view for the Howl verify panel (persistent)."""

    def __init__(self, bot, engine, settings_getter):
        super().__init__(timeout=None)
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter

    @discord.ui.button(
        style=discord.ButtonStyle.success,
        label="Verify Howl Account",
        emoji="🐺",
        custom_id="howl_verify",
    )
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_modal(HowlVerifyModal(self.engine, self.settings_getter))
        except Exception as e:
            logger.error(f"Error opening Howl verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)


class HowlPanel:
    """Manages the Howl verify panel message for a specific guild.

    Reuses the link_panels table, scoped by panel_type = 'howl_verify'.
    """

    PANEL_TYPE = "howl_verify"

    def __init__(self, bot, engine, settings_getter, guild_id=None):
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
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
                    logger.debug(f"Loaded Howl panel info for guild {self.guild_id}")
        except Exception as e:
            logger.error(f"Failed to load Howl panel info for guild {self.guild_id}: {e}")

    def _save_panel_info(self, guild_id: int, channel_id: int, message_id: int):
        if not self.engine:
            return
        try:
            with self.engine.begin() as conn:
                # Only delete this guild's *howl* panels — leave other panel types untouched
                conn.execute(
                    text("DELETE FROM link_panels WHERE guild_id = :guild_id AND panel_type = :ptype"),
                    {"guild_id": guild_id, "ptype": self.PANEL_TYPE},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO link_panels (guild_id, channel_id, message_id, emoji, panel_type, created_at)
                        VALUES (:guild_id, :channel_id, :message_id, '🐺', :ptype, CURRENT_TIMESTAMP)
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
            logger.error(f"Failed to save Howl panel info: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        try:
            embed = discord.Embed(
                title="🐺 Verify Your Howl Account",
                description=(
                    "Verify that you're one of our Howl.gg affiliates to unlock your reward role!\n\n"
                    "**How to Verify:**\n"
                    "Click the **'Verify Howl Account'** button below and enter your "
                    "Howl.gg username. We'll check it against our affiliate stats and "
                    "grant your role instantly."
                ),
                color=EMBED_COLOR,
            )
            embed.add_field(
                name="📋 Before you start",
                value=(
                    "• Make sure you signed up on Howl.gg under our affiliate\n"
                    "• Enter your **exact** Howl username\n"
                    "• One Howl account per Discord user"
                ),
                inline=False,
            )
            embed.set_footer(text="Click 'Verify Howl Account' to get started")

            view = HowlPanelView(self.bot, self.engine, self.settings_getter)
            message = await channel.send(embed=embed, view=view)

            self.panel_guild_id = channel.guild.id
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.guild.id, channel.id, message.id)

            logger.info(f"Created Howl panel in {channel.guild.name} / #{channel.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create Howl panel: {e}")
            return False


async def setup_howl_panel_system(bot, engine, settings_getter):
    """Set up the Howl verify panel system with per-guild instances."""
    panels = {}

    for guild in bot.guilds:
        panel = HowlPanel(bot, engine, settings_getter, guild_id=guild.id)
        panels[guild.id] = panel
        logger.debug(f"✅ Howl verify panel initialized")

    @bot.hybrid_command(name="createhowlpanel")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def create_howl_panel_cmd(ctx):
        """[ADMIN] Create the Howl verify panel in this channel"""
        panel = panels.get(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Howl panel not initialized for this server")
            return

        success = await panel.create_panel(ctx.channel)
        if success:
            await ctx.send("✅ Howl verify panel created! Affiliates can now verify their accounts.")
        else:
            await ctx.send("❌ Failed to create Howl panel. Check logs for details.")

    # Re-attach views to existing panels on bot restart
    for guild_id, panel in panels.items():
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    message = await channel.fetch_message(panel.panel_message_id)
                    view = HowlPanelView(bot, engine, settings_getter)
                    await message.edit(view=view)
                    logger.debug(f"Re-attached Howl panel view to existing message in guild {guild_id}")
            except Exception as e:
                logger.error(f"Failed to re-attach Howl panel view for guild {guild_id}: {e}")

    return panels
