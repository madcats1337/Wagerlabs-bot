"""
Howl Verify Panel - Interactive Discord panel for Howl.gg affiliate auto-verification.

A user clicks the "Verify Howl Account" button, enters their Howl.gg UID in a
modal, and the bot checks that UID against the live affiliate leaderboard
(GET {howl_affiliate_url} with the guild's howl_api_key). If the UID appears,
the user is auto-verified (raffle_shuffle_links, platform='howl', verified=TRUE)
and granted the configured `howl_verified_role_id` role.
"""

import asyncio
import logging
import os
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

try:
    from discord import MediaGalleryItem
except Exception:  # pragma: no cover
    MediaGalleryItem = None

try:
    from discord.ui import (
        ActionRow,
        Button,
        Container,
        LayoutView,
        MediaGallery,
        Modal,
        Separator,
        TextDisplay,
        TextInput,
    )
except Exception:  # pragma: no cover
    from discord.ui import Button, Modal, View

    class ActionRow:
        def __init__(self):
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    class Container:
        def __init__(self, *args, **kwargs):
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    class LayoutView(View):
        pass

    class MediaGallery:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class Separator:
        pass

    class TextDisplay:
        def __init__(self, content):
            self.content = content

    class TextInput:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs


logger = logging.getLogger(__name__)

ACCENT_COLOR = 0x00E0A4  # Howl teal/green
HOWL_DEFAULT_LB_URL = "https://howl.gg/api/user/affiliate/lb"
FALLBACK_EMOJI = "🐺"

_ASSET_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "assets")
_EMOJI_PATH = os.path.join(_ASSET_ROOT, "emojis", "howl.png")
_LOGO_PATH = os.path.join(_ASSET_ROOT, "branding", "howl_logo.png")
_LOGO_FILENAME = "howl_logo.png"


def _build_panel_message_kwargs(view, has_logo=False, clear_attachments=False, for_send=False):
    kwargs = {"view": view}
    if for_send:
        if has_logo:
            kwargs["files"] = [discord.File(_LOGO_PATH, filename=_LOGO_FILENAME)]
    else:
        if has_logo:
            kwargs["attachments"] = [discord.File(_LOGO_PATH, filename=_LOGO_FILENAME)]
        elif clear_attachments:
            kwargs["attachments"] = []
    return kwargs


async def ensure_howl_emoji(bot):
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception as e:
        logger.warning(f"[Howl] Could not fetch application emojis (using unicode fallback): {e}")
        return None

    if "howl" in existing:
        logger.info("[Howl] Reusing existing 'howl' application emoji.")
        return existing["howl"]

    if not os.path.isfile(_EMOJI_PATH):
        logger.warning(f"[Howl] howl.png not found at {_EMOJI_PATH} — button falls back to unicode.")
        return None
    try:
        with open(_EMOJI_PATH, "rb") as f:
            image_bytes = f.read()
        emoji = await bot.create_application_emoji(name="howl", image=image_bytes)
        logger.info("[Howl] Uploaded 'howl' application emoji.")
        return emoji
    except Exception as e:
        logger.error(f"[Howl] Failed to upload 'howl' application emoji: {e}")
        return None


async def _fetch_howl_affiliate_data(affiliate_url: str, api_key: str):
    if not api_key:
        logger.error("Howl verify: no howl_api_key configured")
        return None

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
                return [{"username": r.get("name"), "userId": r.get("userId")} for r in rows if r.get("userId")]
    except asyncio.TimeoutError:
        logger.error("Timeout fetching howl affiliate data")
        return None
    except Exception as e:
        logger.error(f"Error fetching howl affiliate data: {e}")
        return None


def _describe_link(username, howl_uid):
    """Describe an existing Howl link for the user.

    Links made before the panel switched to UID entry have howl_uid NULL, so
    prefer the stored username and only fall back to the UID.
    """
    name = (username or "").strip()
    if name:
        return name
    uid = str(howl_uid).strip() if howl_uid is not None else ""
    return f"UID {uid}" if uid else "a Howl account"


async def verify_and_grant(interaction: discord.Interaction, engine, settings_getter, entered_uid: str):
    discord_id = interaction.user.id
    guild = interaction.guild
    guild_id = guild.id if guild else None
    entered = (entered_uid or "").strip()

    if not entered:
        await interaction.response.send_message("❌ Please enter your Howl UID.", ephemeral=True)
        return

    try:
        with engine.connect() as conn:
            existing = conn.execute(
                text(
                    "SELECT shuffle_username, howl_uid FROM raffle_shuffle_links "
                    "WHERE discord_id = :d AND platform = 'howl'"
                ),
                {"d": discord_id},
            ).fetchone()
        if existing:
            await interaction.response.send_message(
                f"✅ You're already verified as **{_describe_link(existing[0], existing[1])}**!", ephemeral=True
            )
            return
    except Exception as e:
        logger.error(f"Error checking existing Howl link: {e}")
        await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
        return

    settings = settings_getter(guild_id) if guild_id is not None else None
    api_key = settings.get_secret("howl_api_key") if settings else ""
    affiliate_url = (settings.get("howl_affiliate_url") if settings else "") or HOWL_DEFAULT_LB_URL

    if not api_key:
        await interaction.response.send_message(
            "❌ Howl verification isn't configured for this server yet. Please contact an admin.",
            ephemeral=True,
        )
        return

    required_role_id = settings.get("howl_required_role_id") if settings else None
    gate_mode = bool(required_role_id and str(required_role_id).strip())
    if gate_mode:
        if not guild or guild_id is None:
            await interaction.response.send_message("❌ Howl verification must be used in a server.", ephemeral=True)
            return
        try:
            required_role = guild.get_role(int(required_role_id))
        except (ValueError, TypeError):
            await interaction.response.send_message(
                "❌ Howl verification is misconfigured for this server. Please contact an admin.",
                ephemeral=True,
            )
            return
        if not required_role:
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

    await interaction.response.defer(ephemeral=True, thinking=True)

    data = await _fetch_howl_affiliate_data(affiliate_url, api_key)
    if data is None:
        await interaction.followup.send(
            "❌ Couldn't reach the Howl affiliate stats right now. Please try again later.",
            ephemeral=True,
        )
        return

    matched = None
    for row in data:
        if str(row.get("userId")) == entered:
            matched = row
            break

    if not matched:
        raw_code = (settings.get("howl_campaign_code") if settings else "") or ""
        campaign_code = next((c.strip() for c in raw_code.split(",") if c.strip()), "")
        signup_hint = f"on code **{campaign_code}**" if campaign_code else "on our code"
        await interaction.followup.send(
            f"❌ UID **{entered}** wasn't found in our Howl affiliate stats. Make sure you signed up "
            f"{signup_hint} on Howl.gg, then try again.",
            ephemeral=True,
        )
        return

    matched_username = str(matched.get("username"))
    matched_uid = str(matched.get("userId"))

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

    result = _insert_verified_link(engine, matched_username, kick_name, discord_id, matched_uid)
    status = result.get("status")

    if status == "already_linked":
        await interaction.followup.send(
            f"❌ UID **{matched_uid}** is already verified by another Discord account.", ephemeral=True
        )
        return
    if status == "discord_already_linked":
        await interaction.followup.send(
            f"✅ You're already verified as "
            f"**{_describe_link(result.get('existing_username'), result.get('existing_uid'))}**!",
            ephemeral=True,
        )
        return
    if status != "success":
        await interaction.followup.send("❌ Failed to save your verification. Please try again.", ephemeral=True)
        return

    role_note = ""
    if not gate_mode:
        role_note = await _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username)

    await interaction.followup.send(
        f"🎉 Verified! Your Howl account **{matched_username}** is now linked.{role_note}",
        ephemeral=True,
    )


def _insert_verified_link(engine, howl_username, kick_name, discord_id, howl_uid):
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                text("SELECT discord_id FROM raffle_shuffle_links " "WHERE howl_uid = :uid AND platform = 'howl'"),
                {"uid": howl_uid},
            ).fetchone()
            if existing:
                return {"status": "already_linked", "existing_discord_id": existing[0]}

            discord_existing = conn.execute(
                text(
                    "SELECT shuffle_username, howl_uid FROM raffle_shuffle_links "
                    "WHERE discord_id = :d AND platform = 'howl'"
                ),
                {"d": discord_id},
            ).fetchone()
            if discord_existing:
                return {
                    "status": "discord_already_linked",
                    "existing_username": discord_existing[0],
                    "existing_uid": discord_existing[1],
                }

            # Fallback: if somehow howl_uid wasn't set but they have a shuffle_username linked
            existing_user = conn.execute(
                text(
                    "SELECT discord_id FROM raffle_shuffle_links " "WHERE shuffle_username = :u AND platform = 'howl'"
                ),
                {"u": howl_username},
            ).fetchone()
            if existing_user and existing_user[0] != discord_id:
                return {"status": "already_linked", "existing_discord_id": existing_user[0]}

            conn.execute(
                text(
                    """
                    INSERT INTO raffle_shuffle_links
                        (shuffle_username, kick_name, discord_id, platform, verified, verified_by_discord_id, verified_at, howl_uid)
                    VALUES
                        (:howl_username, :kick_name, :discord_id, 'howl', TRUE, :verified_by, CURRENT_TIMESTAMP, :howl_uid)
                    """
                ),
                {
                    "howl_username": howl_username,
                    "kick_name": kick_name,
                    "discord_id": discord_id,
                    "verified_by": discord_id,
                    "howl_uid": howl_uid,
                },
            )
        logger.info(
            f"🔗 Auto-verified Howl link: {howl_username} (UID {howl_uid}) → "
            f"{kick_name or '(no Kick link)'} (Discord: {discord_id})"
        )
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to insert verified Howl link: {e}")
        return {"status": "error", "error": str(e)}


async def _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username):
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
        return ""

    try:
        role = guild.get_role(int(role_id))
    except (ValueError, TypeError):
        return ""

    if not role:
        return ""

    member = guild.get_member(int(discord_id))
    if not member:
        return ""

    if role in member.roles:
        return f" You already have the **{role.name}** role."

    try:
        await member.add_roles(role, reason=f"Verified Howl account: {matched_username}")
        return f" You've been given the **{role.name}** role."
    except Exception as e:
        logger.error(f"Error granting Howl verified role: {e}")
        return ""


class HowlVerifyModal(Modal, title="Verify Your Howl Account"):
    howl_uid = TextInput(
        label="Howl UID",
        placeholder="Your numeric Howl.gg UID",
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
            await verify_and_grant(interaction, self.engine, self.settings_getter, self.howl_uid.value)
        except Exception as e:
            logger.error(f"Error handling Howl verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An error occurred.", ephemeral=True)


class HowlPanelView(LayoutView):
    def __init__(self, bot, engine, settings_getter, howl_emoji=None, show_logo=True, campaign_code=None):
        super().__init__(timeout=None)
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
        self.howl_emoji = howl_emoji or FALLBACK_EMOJI

        # howl_campaign_code may hold several comma-separated codes (they all
        # count for wager tracking). The panel names the FIRST one — it's the
        # sign-up instruction, and a list of codes reads as a choice the viewer
        # has to make. Servers with no code set get the generic wording.
        code = next((c.strip() for c in (campaign_code or "").split(",") if c.strip()), "")
        signup_line = (
            f'Sign up under code **"{code}"** to unlock rewards!'
            if code
            else "Sign up under our code to unlock rewards!"
        )
        bullet_line = (
            f"• Make sure you signed up on code **{code}**" if code else "• Make sure you signed up on our code"
        )

        container = Container(accent_colour=ACCENT_COLOR)
        if show_logo and MediaGalleryItem is not None:
            container.add_item(MediaGallery(MediaGalleryItem(f"attachment://{_LOGO_FILENAME}")))
        container.add_item(TextDisplay("## Verify Your Howl Account"))
        container.add_item(
            TextDisplay(
                f"{signup_line}\n\n"
                "**How to Verify:**\n"
                "Click the **'Verify Howl Account'** button below and enter your "
                "Howl.gg UID. We'll check it against our affiliate stats and "
                "grant your role instantly."
            )
        )
        container.add_item(
            TextDisplay(
                "**📋 Before you start**\n"
                f"{bullet_line}\n"
                "• Enter your **exact** Howl UID (found in your account settings)\n"
                "• One Howl account per Discord user"
            )
        )
        container.add_item(Separator())

        verify_btn = Button(
            style=discord.ButtonStyle.success,
            label="Verify Howl Account",
            emoji=self.howl_emoji,
            custom_id="howl_verify",
        )
        verify_btn.callback = self._verify_callback
        row = ActionRow()
        row.add_item(verify_btn)
        container.add_item(row)

        self.add_item(container)

    async def _verify_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(HowlVerifyModal(self.engine, self.settings_getter))
        except Exception as e:
            logger.error(f"Error opening Howl verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)


class HowlPanel:
    PANEL_TYPE = "howl_verify"

    def __init__(self, bot, engine, settings_getter, guild_id=None, howl_emoji=None):
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
        self.howl_emoji = howl_emoji
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
        except Exception as e:
            logger.error(f"Failed to load Howl panel info for guild {self.guild_id}: {e}")

    def _campaign_code(self, guild_id=None):
        """Per-guild howl_campaign_code, read fresh so panel copy tracks the dashboard."""
        gid = guild_id if guild_id is not None else self.guild_id
        if gid is None or not self.settings_getter:
            return ""
        try:
            settings = self.settings_getter(gid)
            return (settings.get("howl_campaign_code") if settings else "") or ""
        except Exception as e:
            logger.error(f"Failed to read howl_campaign_code for guild {gid}: {e}")
            return ""

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
            has_logo = os.path.isfile(_LOGO_PATH)
            campaign_code = self._campaign_code(channel.guild.id)
            view = HowlPanelView(
                self.bot,
                self.engine,
                self.settings_getter,
                howl_emoji=self.howl_emoji,
                show_logo=has_logo,
                campaign_code=campaign_code,
            )
            if not has_logo:
                logger.warning(f"[Howl] {_LOGO_PATH} not found — posting panel without the logotype banner.")

            try:
                message = await channel.send(**_build_panel_message_kwargs(view, has_logo=has_logo, for_send=True))
            except discord.Forbidden as e:
                if has_logo:
                    logger.warning(
                        f"[Howl] Missing permissions to send with logo (likely 'Attach Files'). Retrying without logo..."
                    )
                    # Recreate view without logo to avoid missing attachment references
                    view_no_logo = HowlPanelView(
                        self.bot,
                        self.engine,
                        self.settings_getter,
                        howl_emoji=self.howl_emoji,
                        show_logo=False,
                        campaign_code=campaign_code,
                    )
                    message = await channel.send(
                        **_build_panel_message_kwargs(view_no_logo, has_logo=False, for_send=True)
                    )
                else:
                    raise e

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
    panels = {}

    howl_emoji = await ensure_howl_emoji(bot)

    for guild in bot.guilds:
        panel = HowlPanel(bot, engine, settings_getter, guild_id=guild.id, howl_emoji=howl_emoji)
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

    for guild_id, panel in panels.items():
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(panel.panel_message_id)
                    except discord.NotFound:
                        logger.warning(f"[Howl] Stored panel message for guild {guild_id} is gone (404); re-posting.")
                        if await panel.create_panel(channel):
                            logger.info(f"[Howl] Re-posted missing panel for guild {guild_id}")
                        continue
                    has_logo = os.path.isfile(_LOGO_PATH)
                    view = HowlPanelView(
                        bot,
                        engine,
                        settings_getter,
                        howl_emoji=howl_emoji,
                        show_logo=has_logo,
                        campaign_code=panel._campaign_code(guild_id),
                    )
                    await message.edit(
                        **_build_panel_message_kwargs(view, has_logo=has_logo, clear_attachments=not has_logo)
                    )
                    logger.info(f"[Howl] Refreshed panel view for guild {guild_id}")
            except Exception as e:
                logger.error(f"Failed to re-attach Howl panel view for guild {guild_id}: {e}")

    return panels
