"""
Roobet Verify Panel - Interactive Discord panel for Roobet affiliate auto-verification.

A user clicks the "Verify Roobet Account" button, enters their Roobet UID in a
modal, and the bot checks that UID against the live affiliate stats
(GET {roobet_affiliate_url} with the guild's roobet_api_key + roobet_user_id).
If the UID appears, the user is auto-verified (raffle_shuffle_links,
platform='roobet', verified=TRUE) and granted `roobet_verified_role_id`.

Modeled on howl_panel.py (the other UID-verified casino). Roobet-specific facts,
all verified against the live endpoint:

- Auth is "Bearer <jwt>" — NOT the raw key Howl uses.
- `userId` is the AFFILIATE's uuid and is a REQUIRED query param, distinct from
  the per-player `uid` values in the response. A key alone cannot fetch.
- Success is a BARE LIST; errors are HTTP 400 with {"code","message"}.
- There is NO server-side filtering: `username`/`uid`/`limit` are silently
  ignored and every call returns the full affiliate roster. So the UID match
  happens here, and the roster is cached briefly to keep a burst of Verify
  clicks from repeatedly pulling the whole list.
- The lookup window is DELIBERATELY WIDE (not the current month like Howl's).
  Roobet returns per-window totals, so a month-scoped query would make anyone
  who hasn't wagered this month unverifiable — including a brand-new signup,
  who is exactly the person most likely to be verifying.
"""

import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from features.linking.panel_embed_config import (
    ROOBET_EMBED_CONFIG_KEY,
    add_container_body,
    load_panel_embed_config,
    resolve_accent,
    resolve_banner_attachment,
    resolve_banner_url,
    resolve_description,
    resolve_footer,
    resolve_title,
)
from raffle_system.reward_settings import is_active_wager_platform

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

ACCENT_COLOR = 0xEEAF0E  # Roobet brand yellow (sampled from roobet-logo.svg)
ROOBET_DEFAULT_STATS_URL = "https://roobetconnect.com/affiliate/v2/stats"
FALLBACK_EMOJI = "🦘"

# Start of the lookup window. Roobet is windowed, so this has to reach back far
# enough to include every referred player, not just this month's active ones.
ROSTER_WINDOW_START = datetime(2020, 1, 1)

# Every call returns the whole roster, so a burst of Verify clicks would each
# re-pull it. Cache per (url, affiliate id, key-hash) for a short spell — long
# enough to absorb a burst, short enough that a just-signed-up user isn't kept
# waiting. The key hash is part of the identity so a rotated credential never
# reads the previous one's roster.
_ROSTER_CACHE_SECONDS = 60
_roster_cache = {}  # (url, user_id, key_hash) -> (fetched_at, rows)

_ASSET_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "assets")
_EMOJI_PATH = os.path.join(_ASSET_ROOT, "emojis", "roobet.png")
_LOGO_PATH = os.path.join(_ASSET_ROOT, "branding", "roobet_logo.png")
_LOGO_FILENAME = "roobet_logo.png"


def _build_panel_message_kwargs(view, banner_file=None, has_logo=False, clear_attachments=False, for_send=False):
    """Build send/edit kwargs for a roobet panel message, including the banner or logo attachment when needed."""
    kwargs = {"view": view}
    files = []
    if banner_file:
        files.append(banner_file)
    elif has_logo:
        files.append(discord.File(_LOGO_PATH, filename=_LOGO_FILENAME))

    if for_send:
        if files:
            kwargs["files"] = files
    else:
        if files:
            kwargs["attachments"] = files
        elif clear_attachments:
            kwargs["attachments"] = []
    return kwargs


async def ensure_roobet_emoji(bot):
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception as e:
        logger.warning(f"[Roobet] Could not fetch application emojis (using unicode fallback): {e}")
        return None

    if "roobet" in existing:
        logger.info("[Roobet] Reusing existing 'roobet' application emoji.")
        return existing["roobet"]

    if not os.path.isfile(_EMOJI_PATH):
        logger.warning(f"[Roobet] roobet.png not found at {_EMOJI_PATH} — button falls back to unicode.")
        return None
    try:
        with open(_EMOJI_PATH, "rb") as f:
            image_bytes = f.read()
        emoji = await bot.create_application_emoji(name="roobet", image=image_bytes)
        logger.info("[Roobet] Uploaded 'roobet' application emoji.")
        return emoji
    except Exception as e:
        logger.error(f"[Roobet] Failed to upload 'roobet' application emoji: {e}")
        return None


async def _fetch_roobet_affiliate_data(affiliate_url: str, api_key: str, affiliate_user_id: str):
    """Fetch the affiliate roster as [{"username", "userId"}], or None on failure.

    `userId` in the returned rows is the PLAYER's uid (what a verifying user
    enters); `affiliate_user_id` is the streamer's own affiliate id, sent as the
    query param. Cached briefly — see _ROSTER_CACHE_SECONDS.
    """
    if not api_key:
        logger.error("Roobet verify: no roobet_api_key configured")
        return None
    if not affiliate_user_id:
        logger.error("Roobet verify: no roobet_user_id configured")
        return None

    # The api_key is part of the key: a rotated or revoked credential must not be
    # served a previous key's cached roster (which would keep verifying users
    # against data the current credential can no longer fetch). Hashed so the
    # credential itself is never held as a dict key.
    cache_key = (affiliate_url, affiliate_user_id, hashlib.sha256(api_key.encode("utf-8")).hexdigest())
    hit = _roster_cache.get(cache_key)
    if hit and (time.time() - hit[0]) <= _ROSTER_CACHE_SECONDS:
        return hit[1]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; WagerlabsBot/1.0; +https://wagerlabs.app)",
    }
    now = datetime.utcnow()
    params = {
        "userId": affiliate_user_id,
        "startDate": ROSTER_WINDOW_START.strftime("%Y-%m-%dT%H:%M:%S"),
        "endDate": now.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(affiliate_url, headers=headers, params=params, timeout=30) as response:
                if response.status != 200:
                    # Credential/param problems arrive as 400 + {"code","message"},
                    # not 401/403 — surface the message so a bad key is diagnosable.
                    detail = ""
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            detail = body.get("message") or body.get("code") or ""
                    except Exception:
                        pass
                    logger.error(f"Roobet affiliate API returned status {response.status}: {detail}")
                    return None

                raw = await response.json()
                if not isinstance(raw, list):
                    logger.error(f"Unexpected roobet affiliate API response: {str(raw)[:200]}")
                    return None

                rows = [
                    {"username": r.get("username"), "userId": r.get("uid")}
                    for r in raw
                    if isinstance(r, dict) and r.get("uid")
                ]
                _roster_cache[cache_key] = (time.time(), rows)
                return rows
    except asyncio.TimeoutError:
        logger.error("Timeout fetching roobet affiliate data")
        return None
    except Exception as e:
        logger.error(f"Error fetching roobet affiliate data: {e}")
        return None


def _describe_link(username, platform_uid):
    """Describe an existing Roobet link for the user."""
    name = (username or "").strip()
    if name:
        return name
    uid = str(platform_uid).strip() if platform_uid is not None else ""
    return f"UID {uid}" if uid else "a Roobet account"


async def verify_and_grant(interaction: discord.Interaction, engine, settings_getter, entered_uid: str):
    discord_id = interaction.user.id
    guild = interaction.guild
    guild_id = guild.id if guild else None
    entered = (entered_uid or "").strip()

    if not entered:
        await interaction.response.send_message("❌ Please enter your Roobet UID.", ephemeral=True)
        return

    try:
        with engine.connect() as conn:
            existing = conn.execute(
                text(
                    "SELECT shuffle_username, platform_uid FROM raffle_shuffle_links "
                    "WHERE discord_id = :d AND platform = 'roobet'"
                ),
                {"d": discord_id},
            ).fetchone()
        if existing:
            await interaction.response.send_message(
                f"✅ You're already verified as **{_describe_link(existing[0], existing[1])}**!", ephemeral=True
            )
            return
    except Exception as e:
        logger.error(f"Error checking existing Roobet link: {e}")
        await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
        return

    settings = settings_getter(guild_id) if guild_id is not None else None
    api_key = settings.get_secret("roobet_api_key") if settings else ""
    affiliate_user_id = (settings.get("roobet_user_id") if settings else "") or ""
    affiliate_url = (settings.get("roobet_affiliate_url") if settings else "") or ROOBET_DEFAULT_STATS_URL

    if not api_key or not str(affiliate_user_id).strip():
        await interaction.response.send_message(
            "❌ Roobet verification isn't configured for this server yet. Please contact an admin.",
            ephemeral=True,
        )
        return

    required_role_id = settings.get("roobet_required_role_id") if settings else None
    gate_mode = bool(required_role_id and str(required_role_id).strip())
    if gate_mode:
        if not guild or guild_id is None:
            await interaction.response.send_message("❌ Roobet verification must be used in a server.", ephemeral=True)
            return
        try:
            required_role = guild.get_role(int(required_role_id))
        except (ValueError, TypeError):
            await interaction.response.send_message(
                "❌ Roobet verification is misconfigured for this server. Please contact an admin.",
                ephemeral=True,
            )
            return
        if not required_role:
            await interaction.response.send_message(
                "❌ Roobet verification is misconfigured for this server. Please contact an admin.",
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

    data = await _fetch_roobet_affiliate_data(affiliate_url, api_key, str(affiliate_user_id).strip())
    if data is None:
        await interaction.followup.send(
            "❌ Couldn't reach the Roobet affiliate stats right now. Please try again later.",
            ephemeral=True,
        )
        return

    matched = None
    for row in data:
        if str(row.get("userId")) == entered:
            matched = row
            break

    if not matched:
        raw_code = (settings.get("roobet_campaign_code") if settings else "") or ""
        campaign_code = next((c.strip() for c in raw_code.split(",") if c.strip()), "")
        signup_hint = f"on code **{campaign_code}**" if campaign_code else "on our code"
        await interaction.followup.send(
            f"❌ UID **{entered}** wasn't found in our Roobet affiliate stats. Make sure you signed up "
            f"{signup_hint} on Roobet, then try again.",
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

    result = _insert_verified_link(engine, matched_username, kick_name, discord_id, matched_uid, guild_id)
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
        f"🎉 Verified! Your Roobet account **{matched_username}** is now linked.{role_note}",
        ephemeral=True,
    )


def _insert_verified_link(engine, roobet_username, kick_name, discord_id, platform_uid, guild_id):
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                text(
                    "SELECT discord_id FROM raffle_shuffle_links " "WHERE platform_uid = :uid AND platform = 'roobet'"
                ),
                {"uid": platform_uid},
            ).fetchone()
            if existing:
                return {"status": "already_linked", "existing_discord_id": existing[0]}

            discord_existing = conn.execute(
                text(
                    "SELECT shuffle_username, platform_uid FROM raffle_shuffle_links "
                    "WHERE discord_id = :d AND platform = 'roobet'"
                ),
                {"d": discord_id},
            ).fetchone()
            if discord_existing:
                return {
                    "status": "discord_already_linked",
                    "existing_username": discord_existing[0],
                    "existing_uid": discord_existing[1],
                }

            # Fallback: a row for this username with no uid recorded.
            existing_user = conn.execute(
                text(
                    "SELECT discord_id FROM raffle_shuffle_links " "WHERE shuffle_username = :u AND platform = 'roobet'"
                ),
                {"u": roobet_username},
            ).fetchone()
            if existing_user and existing_user[0] != discord_id:
                return {"status": "already_linked", "existing_discord_id": existing_user[0]}

            conn.execute(
                text(
                    """
                    INSERT INTO raffle_shuffle_links
                        (shuffle_username, discord_server_id, kick_name, discord_id,
                         platform, verified, verified_by_discord_id, verified_at, platform_uid)
                    -- discord_server_id is NOT NULL; the linking guild owns the row.
                    -- shuffle_username is the generic per-platform username column.
                    VALUES
                        (:roobet_username, :server_id, :kick_name, :discord_id, 'roobet',
                         TRUE, :verified_by, CURRENT_TIMESTAMP, :platform_uid)
                    """
                ),
                {
                    "roobet_username": roobet_username,
                    "server_id": guild_id,
                    "kick_name": kick_name,
                    "discord_id": discord_id,
                    "verified_by": discord_id,
                    "platform_uid": platform_uid,
                },
            )
        logger.info(
            f"🔗 Auto-verified Roobet link: {roobet_username} (UID {platform_uid}) → "
            f"{kick_name or '(no Kick link)'} (Discord: {discord_id})"
        )
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to insert verified Roobet link: {e}")
        return {"status": "error", "error": str(e)}


async def _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username):
    if not guild or not guild_id:
        return ""

    try:
        with engine.connect() as conn:
            role_id = conn.execute(
                text(
                    "SELECT value FROM bot_settings "
                    "WHERE key = 'roobet_verified_role_id' AND discord_server_id = :guild_id"
                ),
                {"guild_id": guild_id},
            ).scalar()
    except Exception as e:
        logger.error(f"Failed to query roobet_verified_role_id: {e}")
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
        await member.add_roles(role, reason=f"Verified Roobet account: {matched_username}")
        return f" You've been given the **{role.name}** role."
    except Exception as e:
        logger.error(f"Error granting Roobet verified role: {e}")
        return ""


class RoobetVerifyModal(Modal, title="Verify Your Roobet Account"):
    roobet_uid = TextInput(
        label="Roobet UID",
        placeholder="Your Roobet user ID",
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
            await verify_and_grant(interaction, self.engine, self.settings_getter, self.roobet_uid.value)
        except Exception as e:
            logger.error(f"Error handling Roobet verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An error occurred.", ephemeral=True)


class RoobetPanelView(LayoutView):
    def __init__(
        self,
        bot,
        engine,
        settings_getter,
        roobet_emoji=None,
        show_logo=True,
        campaign_code=None,
        embed_cfg=None,
        guild_id=None,
        banner_media_url=None,
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
        self.roobet_emoji = roobet_emoji or FALLBACK_EMOJI
        self.guild_id = guild_id

        cfg = embed_cfg or {}
        banner_url = banner_media_url or resolve_banner_url(cfg, engine=engine, guild_id=guild_id)

        # roobet_campaign_code may hold several comma-separated codes. The panel
        # names the FIRST one — it's the sign-up instruction, and a list of codes
        # reads as a choice the viewer has to make.
        code = next((c.strip() for c in (campaign_code or "").split(",") if c.strip()), "")
        signup_line = (
            f'Sign up under code **"{code}"** to unlock rewards!'
            if code
            else "Sign up under our code to unlock rewards!"
        )
        bullet_line = (
            f"• Make sure you signed up on code **{code}**" if code else "• Make sure you signed up on our code"
        )

        container = Container(accent_colour=resolve_accent(cfg, ACCENT_COLOR))
        if MediaGalleryItem is not None:
            if banner_url:
                container.add_item(MediaGallery(MediaGalleryItem(banner_url)))
            elif show_logo:
                container.add_item(MediaGallery(MediaGalleryItem(f"attachment://{_LOGO_FILENAME}")))
        container.add_item(TextDisplay(resolve_title(cfg, "Verify Your Roobet Account")))
        custom_desc = resolve_description(cfg)
        if custom_desc:
            add_container_body(container, custom_desc, TextDisplay, Separator)
        else:
            container.add_item(
                TextDisplay(
                    f"{signup_line}\n\n"
                    "**How to Verify:**\n"
                    "Click the **'Verify Roobet Account'** button below and enter your "
                    "Roobet UID. We'll check it against our affiliate stats and "
                    "grant your role instantly."
                )
            )
            container.add_item(
                TextDisplay(
                    "**📋 Before you start**\n"
                    f"{bullet_line}\n"
                    "• Enter your **exact** Roobet UID (found in your account settings)\n"
                    "• One Roobet account per Discord user"
                )
            )
        container.add_item(Separator())

        verify_btn = Button(
            style=discord.ButtonStyle.success,
            label="Verify Roobet Account",
            emoji=self.roobet_emoji,
            custom_id="roobet_verify",
        )
        verify_btn.callback = self._verify_callback
        row = ActionRow()
        row.add_item(verify_btn)
        container.add_item(row)

        footer = resolve_footer(cfg)
        if footer:
            container.add_item(Separator())
            container.add_item(TextDisplay(footer))

        self.add_item(container)

    async def _verify_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(RoobetVerifyModal(self.engine, self.settings_getter))
        except Exception as e:
            logger.error(f"Error opening Roobet verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)


class RoobetPanel:
    PANEL_TYPE = "roobet_verify"

    def __init__(self, bot, engine, settings_getter, guild_id=None, roobet_emoji=None):
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
        self.roobet_emoji = roobet_emoji
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
            logger.error(f"Failed to load Roobet panel info for guild {self.guild_id}: {e}")

    def _campaign_code(self, guild_id=None):
        """Per-guild roobet_campaign_code, read fresh so panel copy tracks the dashboard."""
        gid = guild_id if guild_id is not None else self.guild_id
        if gid is None or not self.settings_getter:
            return ""
        try:
            settings = self.settings_getter(gid)
            return (settings.get("roobet_campaign_code") if settings else "") or ""
        except Exception as e:
            logger.error(f"Failed to read roobet_campaign_code for guild {gid}: {e}")
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
                        INSERT INTO link_panels
                            (guild_id, discord_server_id, channel_id, message_id,
                             emoji, panel_type, created_at)
                        -- discord_server_id is NOT NULL; for a panel it is the guild itself.
                        VALUES (:guild_id, :guild_id, :channel_id, :message_id,
                                '🦘', :ptype, CURRENT_TIMESTAMP)
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
            logger.error(f"Failed to save Roobet panel info: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        try:
            embed_cfg = load_panel_embed_config(self.engine, channel.guild.id, ROOBET_EMBED_CONFIG_KEY)
            banner_url = resolve_banner_url(embed_cfg, engine=self.engine, guild_id=channel.guild.id)
            banner_media_url, banner_file = (
                await resolve_banner_attachment(banner_url, engine=self.engine) if banner_url else ("", None)
            )
            # A dashboard banner URL renders directly or via attachment, so the bundled logo is not attached.
            has_logo = (not banner_url) and os.path.isfile(_LOGO_PATH)
            campaign_code = self._campaign_code(channel.guild.id)
            view = RoobetPanelView(
                self.bot,
                self.engine,
                self.settings_getter,
                roobet_emoji=self.roobet_emoji,
                show_logo=has_logo,
                campaign_code=campaign_code,
                embed_cfg=embed_cfg,
                guild_id=channel.guild.id,
                banner_media_url=banner_media_url,
            )
            if not has_logo and not banner_url:
                logger.warning(f"[Roobet] {_LOGO_PATH} not found — posting panel without the logotype banner.")

            try:
                message = await channel.send(
                    **_build_panel_message_kwargs(view, banner_file=banner_file, has_logo=has_logo, for_send=True)
                )
            except discord.Forbidden as e:
                if has_logo or banner_file:
                    logger.warning(
                        "[Roobet] Missing permissions to send with attachment (likely 'Attach Files'). Retrying without attachment..."
                    )
                    view_no_attachment = RoobetPanelView(
                        self.bot,
                        self.engine,
                        self.settings_getter,
                        roobet_emoji=self.roobet_emoji,
                        show_logo=False,
                        campaign_code=campaign_code,
                        embed_cfg={**embed_cfg, "bannerUrl": ""},
                        guild_id=channel.guild.id,
                        banner_media_url="",
                    )
                    message = await channel.send(
                        **_build_panel_message_kwargs(
                            view_no_attachment, banner_file=None, has_logo=False, for_send=True
                        )
                    )
                else:
                    raise e
            except discord.HTTPException as e:
                if not banner_url:
                    raise
                # An unreachable/invalid banner URL makes Discord reject the message.
                logger.warning(f"[Roobet] Banner URL rejected ({e}); re-posting with the bundled logo.")
                has_logo = os.path.isfile(_LOGO_PATH)
                view_no_banner = RoobetPanelView(
                    self.bot,
                    self.engine,
                    self.settings_getter,
                    roobet_emoji=self.roobet_emoji,
                    show_logo=has_logo,
                    campaign_code=campaign_code,
                    embed_cfg={**embed_cfg, "bannerUrl": ""},
                    guild_id=channel.guild.id,
                    banner_media_url="",
                )
                message = await channel.send(
                    **_build_panel_message_kwargs(view_no_banner, banner_file=None, has_logo=has_logo, for_send=True)
                )

            self.panel_guild_id = channel.guild.id
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.guild.id, channel.id, message.id)

            logger.info(f"Created Roobet panel in {channel.guild.name} / #{channel.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create Roobet panel: {e}")
            return False

    async def refresh_panel(self, channel: discord.TextChannel):
        """Re-render the standing panel message in place (see CombinedLinkPanel)."""
        if not self.panel_message_id or not self.panel_channel_id:
            return False
        try:
            message = await channel.fetch_message(int(self.panel_message_id))
        except discord.NotFound:
            logger.info(f"[Roobet] Stored panel message for guild {self.guild_id} is gone; will re-post.")
            return False
        except Exception as e:
            logger.warning(f"[Roobet] Could not fetch panel message for guild {self.guild_id}: {e}")
            return False

        try:
            embed_cfg = load_panel_embed_config(self.engine, channel.guild.id, ROOBET_EMBED_CONFIG_KEY)
            banner_url = resolve_banner_url(embed_cfg, engine=self.engine, guild_id=channel.guild.id)
            banner_media_url, banner_file = (
                await resolve_banner_attachment(banner_url, engine=self.engine) if banner_url else ("", None)
            )
            has_logo = (not banner_url) and os.path.isfile(_LOGO_PATH)
            view = RoobetPanelView(
                self.bot,
                self.engine,
                self.settings_getter,
                roobet_emoji=self.roobet_emoji,
                show_logo=has_logo,
                campaign_code=self._campaign_code(channel.guild.id),
                embed_cfg=embed_cfg,
                guild_id=channel.guild.id,
                banner_media_url=banner_media_url,
            )
            # clear_attachments drops a previously attached logo when a banner URL
            # took over; otherwise it would linger below the container.
            clear_attachments = not banner_file and not has_logo
            await message.edit(
                **_build_panel_message_kwargs(
                    view, banner_file=banner_file, has_logo=has_logo, clear_attachments=clear_attachments
                )
            )
            logger.info(f"[Roobet] Refreshed panel in place for guild {self.guild_id}")
            return True
        except Exception as e:
            logger.warning(f"[Roobet] Failed to edit panel for guild {self.guild_id}: {e}")
            return False


async def setup_roobet_panel_system(bot, engine, settings_getter):
    panels = {}

    roobet_emoji = await ensure_roobet_emoji(bot)

    for guild in bot.guilds:
        panel = RoobetPanel(bot, engine, settings_getter, guild_id=guild.id, roobet_emoji=roobet_emoji)
        panels[guild.id] = panel
        logger.debug("✅ Roobet verify panel initialized")

    @bot.hybrid_command(name="createroobetpanel")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def create_roobet_panel_cmd(ctx):
        """[ADMIN] Create the Roobet verify panel in this channel"""
        panel = panels.get(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Roobet panel not initialized for this server")
            return

        success = await panel.create_panel(ctx.channel)
        if success:
            await ctx.send("✅ Roobet verify panel created! Affiliates can now verify their accounts.")
        else:
            await ctx.send("❌ Failed to create Roobet panel. Check logs for details.")

    for guild_id, panel in panels.items():
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(panel.panel_message_id)
                    except discord.NotFound:
                        # Don't resurrect a panel the operator deleted after switching
                        # the server to a different wager platform (the panel row
                        # survives that switch).
                        if not is_active_wager_platform(engine, guild_id, "roobet", logger=logger):
                            logger.info(
                                f"[Roobet] Stored panel message for guild {guild_id} is gone (404) but "
                                f"Roobet is no longer the selected wager platform; not re-posting."
                            )
                            continue
                        logger.warning(f"[Roobet] Stored panel message for guild {guild_id} is gone (404); re-posting.")
                        if await panel.create_panel(channel):
                            logger.info(f"[Roobet] Re-posted missing panel for guild {guild_id}")
                        continue
                    embed_cfg = load_panel_embed_config(engine, guild_id, ROOBET_EMBED_CONFIG_KEY)
                    banner_url = resolve_banner_url(embed_cfg, engine=engine, guild_id=guild_id)
                    banner_media_url, banner_file = (
                        await resolve_banner_attachment(banner_url, engine=engine) if banner_url else ("", None)
                    )
                    has_logo = (not banner_url) and os.path.isfile(_LOGO_PATH)
                    view = RoobetPanelView(
                        bot,
                        engine,
                        settings_getter,
                        roobet_emoji=roobet_emoji,
                        show_logo=has_logo,
                        campaign_code=panel._campaign_code(guild_id),
                        embed_cfg=embed_cfg,
                        guild_id=guild_id,
                        banner_media_url=banner_media_url,
                    )
                    clear_attachments = not banner_file and not has_logo
                    await message.edit(
                        **_build_panel_message_kwargs(
                            view, banner_file=banner_file, has_logo=has_logo, clear_attachments=clear_attachments
                        )
                    )
                    logger.info(f"[Roobet] Refreshed panel view for guild {guild_id}")
            except Exception as e:
                logger.error(f"Failed to re-attach Roobet panel view for guild {guild_id}: {e}")

    return panels
