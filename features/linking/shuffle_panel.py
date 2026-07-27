"""
Shuffle Verify Panel - Interactive Discord panel for Shuffle affiliate auto-verification.

A user clicks the "Verify Shuffle Account" button, enters their Shuffle username in a
modal, and the bot checks that username against the live affiliate-stats JSON (fetched
from the guild's configured `shuffle_affiliate_url`). If the username appears in that
JSON for the configured campaign code, the user is auto-verified (raffle_shuffle_links,
verified=TRUE) and granted the configured `shuffle_verified_role_id` role.

This mirrors features/linking/link_panel.py (embed + persistent button view + admin
create command + re-attach on restart) and is the automated equivalent of the admin
`!verifyshuffle` command in raffle_system/commands.py.
"""

import asyncio
import logging
import os

import aiohttp
import discord
from discord.ext import commands
from sqlalchemy import text

try:
    from discord import MediaGalleryItem
except Exception:  # pragma: no cover - compatibility with older discord.py versions
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
except Exception:  # pragma: no cover - compatibility with older discord.py versions
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

ACCENT_COLOR = 0x6C5CE7  # Shuffle purple

# Unicode fallback for the verify button when the brand app emoji isn't available.
FALLBACK_EMOJI = "🎰"

# Brand assets. The square emoji (button) is uploaded as an application emoji; the
# wide logotype is attached to the panel message and shown in a MediaGallery.
#   - assets/emojis/shuffle.png        → 128×128 square PNG (Discord app-emoji limit)
#   - assets/branding/shuffle_logo.png → ~600×160 logotype PNG (panel header image)
_ASSET_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "assets")
_EMOJI_PATH = os.path.join(_ASSET_ROOT, "emojis", "shuffle.png")
_LOGO_PATH = os.path.join(_ASSET_ROOT, "branding", "shuffle_logo.png")
_LOGO_FILENAME = "shuffle_logo.png"


def _build_panel_message_kwargs(view, has_logo=False, clear_attachments=False, for_send=False):
    """Build send/edit kwargs for a shuffle panel message, including the logo attachment when needed.

    Messageable.send() and Message.edit() take different kwargs for files:
    send() wants files=[...] (and can't clear anything), edit() wants attachments=[...]
    (where [] clears existing attachments). Pass for_send=True from the create path.
    """
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


async def ensure_shuffle_emoji(bot):
    """Idempotently register the Shuffle brand logo as the bot's 'shuffle'
    application emoji and return it (or None → unicode fallback on the button).

    Mirrors features/linking/combined_link_panel.ensure_link_emojis: reuse the
    existing app emoji by name, else upload assets/emojis/shuffle.png. Any missing
    file or API error returns None and the button keeps the 🎰 fallback."""
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception as e:
        logger.warning(f"[Shuffle] Could not fetch application emojis (using unicode fallback): {e}")
        return None

    if "shuffle" in existing:
        logger.info("[Shuffle] Reusing existing 'shuffle' application emoji.")
        return existing["shuffle"]

    if not os.path.isfile(_EMOJI_PATH):
        logger.warning(f"[Shuffle] shuffle.png not found at {_EMOJI_PATH} — button falls back to unicode.")
        return None
    try:
        with open(_EMOJI_PATH, "rb") as f:
            image_bytes = f.read()
        emoji = await bot.create_application_emoji(name="shuffle", image=image_bytes)
        logger.info("[Shuffle] Uploaded 'shuffle' application emoji.")
        return emoji
    except Exception as e:
        logger.error(f"[Shuffle] Failed to upload 'shuffle' application emoji: {e}")
        return None


def _shuffle_wager_url(affiliate_url: str) -> str:
    """Derive Shuffle's /wager/<id> endpoint from a stored /stats/<id> URL.

    Mirrors ShuffleWagerTracker._shuffle_wager_url. The /wager/ endpoint returns
    the same referees as /stats/ (username + raw wagerAmount) plus the RTP-
    weighted weightedWagerAmount, without the per-row campaignCode. For link
    verification we only need the username, so this is a drop-in source. URLs
    that don't contain /stats/ are returned unchanged.
    """
    if not affiliate_url:
        return affiliate_url
    return affiliate_url.replace("/stats/", "/wager/", 1)


async def _fetch_affiliate_data(affiliate_url: str):
    """Fetch the affiliate-stats JSON array from the configured URL.

    Mirrors ShuffleWagerTracker._fetch_shuffle_data (shuffle_tracker.py:422):
    GET the URL, expect a JSON array, return None on any failure.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(affiliate_url, timeout=30) as response:
                if response.status != 200:
                    logger.error(f"Affiliate API returned status {response.status}")
                    return None

                data = await response.json()
                # Shuffle serves rate-limit errors as an HTTP 200 whose body is a
                # JSON object ({"statusCode":400,"message":"TOO_MANY_REQUEST"}),
                # not the expected array. Surface that distinctly so a transient
                # rate-limit isn't logged as an "unexpected format" bug.
                if isinstance(data, dict):
                    msg = data.get("message") or data.get("error") or "unknown error"
                    logger.warning(f"Affiliate API returned an error object (HTTP 200): {msg}")
                    return None
                if not isinstance(data, list):
                    logger.error(f"Unexpected affiliate API response format: {type(data)}")
                    return None

                return data
    except asyncio.TimeoutError:
        logger.error("Timeout fetching affiliate data")
        return None
    except Exception as e:
        logger.error(f"Error fetching affiliate data: {e}")
        return None


async def verify_and_grant(interaction: discord.Interaction, engine, settings_getter, entered_username: str):
    """Verify the entered Shuffle username against affiliate stats and grant the role.

    Replies to the interaction with an ephemeral message in every branch.
    """
    discord_id = interaction.user.id
    guild = interaction.guild
    guild_id = guild.id if guild else None
    entered = (entered_username or "").strip()

    if not entered:
        await interaction.response.send_message("❌ Please enter your Shuffle username.", ephemeral=True)
        return

    # 1. Already-verified check — scoped to the SHUFFLE platform so a user's
    #    howl link doesn't make the shuffle flow report "already verified".
    try:
        with engine.connect() as conn:
            existing = conn.execute(
                text(
                    "SELECT shuffle_username FROM raffle_shuffle_links "
                    "WHERE discord_id = :d AND platform = 'shuffle'"
                ),
                {"d": discord_id},
            ).fetchone()
        if existing:
            await interaction.response.send_message(f"✅ You're already verified as **{existing[0]}**!", ephemeral=True)
            return
    except Exception as e:
        logger.error(f"Error checking existing Shuffle link: {e}")
        await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
        return

    # Resolve per-guild settings
    settings = settings_getter(guild_id) if guild_id is not None else None
    affiliate_url = settings.shuffle_affiliate_url if settings else ""
    campaign_code = settings.shuffle_campaign_code if settings else ""

    if not affiliate_url:
        await interaction.response.send_message(
            "❌ Shuffle verification isn't configured for this server yet. Please contact an admin.",
            ephemeral=True,
        )
        return

    # Defer: the affiliate fetch can take a few seconds
    await interaction.response.defer(ephemeral=True, thinking=True)

    # 2. Fetch affiliate JSON.
    # Use Shuffle's newer /wager/<id> endpoint (derived from the stored /stats/
    # URL). It returns the same referees/usernames as /stats/ but drops the
    # per-row campaignCode (the <id> already scopes to this affiliate) — so the
    # campaign-code filter below degrades to a username-only match for these
    # rows, which is correct: everyone the endpoint returns is our referee.
    data = await _fetch_affiliate_data(_shuffle_wager_url(affiliate_url))
    if data is None:
        await interaction.followup.send(
            "❌ Couldn't reach the Shuffle affiliate stats right now. Please try again later.",
            ephemeral=True,
        )
        return

    # 3. Match: case-insensitive username, filtered by campaign code(s).
    # A row only fails the campaign check if it actually carries a campaignCode
    # (legacy /stats/) that isn't one of ours; /wager/ rows have no campaignCode
    # and match on username alone.
    campaign_codes = [code.strip().lower() for code in (campaign_code or "").split(",") if code.strip()]
    entered_lower = entered.lower()
    matched = None
    for row in data:
        if str(row.get("username", "")).lower() != entered_lower:
            continue
        row_code = str(row.get("campaignCode", "")).lower()
        if campaign_codes and row_code and row_code not in campaign_codes:
            continue
        matched = row
        break

    if not matched:
        await interaction.followup.send(
            f"❌ **{entered}** wasn't found in our affiliate stats. Make sure you used code "
            f"**{campaign_code}** when signing up on Shuffle, then try again.",
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

    # 4b. Persist the verified link (verified by the user themselves)
    result = _insert_verified_link(engine, matched_username, kick_name, discord_id)
    status = result.get("status")

    if status == "already_linked":
        await interaction.followup.send(
            f"❌ **{matched_username}** is already verified by another Discord account.", ephemeral=True
        )
        return
    if status == "discord_already_linked":
        await interaction.followup.send(
            f"✅ You're already verified as **{result.get('existing_shuffle_username')}**!", ephemeral=True
        )
        return
    if status != "success":
        await interaction.followup.send("❌ Failed to save your verification. Please try again.", ephemeral=True)
        return

    # 4c. Grant the configured role (mirrors bot.py:3959-3999)
    role_note = await _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username)

    await interaction.followup.send(
        f"🎉 Verified! Your Shuffle account **{matched_username}** is now linked.{role_note}",
        ephemeral=True,
    )


def _insert_verified_link(engine, shuffle_username, kick_name, discord_id):
    """Insert a verified raffle_shuffle_links row (self-verified).

    Uses the same status contract as ShuffleWagerTracker.link_shuffle_account so the
    caller can give friendly messages for the UNIQUE(shuffle_username)/UNIQUE(discord_id)
    cases without relying on a DB exception.
    """
    try:
        with engine.begin() as conn:
            existing = conn.execute(
                text(
                    "SELECT discord_id FROM raffle_shuffle_links "
                    "WHERE shuffle_username = :u AND platform = 'shuffle'"
                ),
                {"u": shuffle_username},
            ).fetchone()
            if existing:
                return {"status": "already_linked", "existing_discord_id": existing[0]}

            discord_existing = conn.execute(
                text(
                    "SELECT shuffle_username FROM raffle_shuffle_links "
                    "WHERE discord_id = :d AND platform = 'shuffle'"
                ),
                {"d": discord_id},
            ).fetchone()
            if discord_existing:
                return {"status": "discord_already_linked", "existing_shuffle_username": discord_existing[0]}

            conn.execute(
                text(
                    """
                    INSERT INTO raffle_shuffle_links
                        (shuffle_username, kick_name, discord_id, platform, verified, verified_by_discord_id, verified_at)
                    VALUES
                        (:shuffle_username, :kick_name, :discord_id, 'shuffle', TRUE, :verified_by, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "shuffle_username": shuffle_username,
                    "kick_name": kick_name,
                    "discord_id": discord_id,
                    "verified_by": discord_id,  # self-verified
                },
            )
        logger.info(
            f"🔗 Auto-verified Shuffle link: {shuffle_username} → "
            f"{kick_name or '(no Kick link)'} (Discord: {discord_id})"
        )
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Failed to insert verified Shuffle link: {e}")
        return {"status": "error", "error": str(e)}


async def _grant_role(interaction, engine, guild, discord_id, guild_id, matched_username):
    """Grant the configured shuffle_verified_role_id role. Returns a note for the user."""
    if not guild or not guild_id:
        return ""

    try:
        with engine.connect() as conn:
            role_id = conn.execute(
                text(
                    "SELECT value FROM bot_settings "
                    "WHERE key = 'shuffle_verified_role_id' AND discord_server_id = :guild_id"
                ),
                {"guild_id": guild_id},
            ).scalar()
    except Exception as e:
        logger.error(f"Failed to query shuffle_verified_role_id: {e}")
        role_id = None

    if not role_id or not str(role_id).strip():
        return ""  # No role configured — verification still succeeded

    try:
        role = guild.get_role(int(role_id))
    except (ValueError, TypeError):
        logger.error(f"Invalid shuffle_verified_role_id {role_id!r} for guild {guild_id}")
        return ""

    if not role:
        logger.warning(f"Shuffle verified role {role_id} not found in guild {guild.name}")
        return ""

    member = guild.get_member(int(discord_id))
    if not member:
        logger.warning(f"Member {discord_id} not found in guild {guild.name}")
        return ""

    if role in member.roles:
        return f" You already have the **{role.name}** role."

    try:
        await member.add_roles(role, reason=f"Verified Shuffle account: {matched_username}")
        logger.info(f"✅ Granted role '{role.name}' to {member.display_name} for Shuffle verification")
        return f" You've been given the **{role.name}** role."
    except Exception as e:
        logger.error(f"Error granting Shuffle verified role: {e}")
        return ""


class ShuffleVerifyModal(Modal, title="Verify Your Shuffle Account"):
    """Modal that collects the user's Shuffle username."""

    shuffle_username = TextInput(
        label="Shuffle Username",
        placeholder="Your exact Shuffle.com username",
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
            await verify_and_grant(interaction, self.engine, self.settings_getter, self.shuffle_username.value)
        except Exception as e:
            logger.error(f"Error handling Shuffle verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            else:
                await interaction.followup.send("❌ An error occurred.", ephemeral=True)


class ShufflePanelView(LayoutView):
    """Components-V2 panel for the Shuffle verify flow (persistent).

    The panel copy lives in TextDisplays inside a Container; the single
    "Verify Shuffle Account" button (stable custom_id, so the message re-binds
    its handler after a restart) opens the verification modal."""

    def __init__(self, bot, engine, settings_getter, shuffle_emoji=None, show_logo=True):
        super().__init__(timeout=None)
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
        self.shuffle_emoji = shuffle_emoji or FALLBACK_EMOJI

        container = Container(accent_colour=ACCENT_COLOR)
        # Shuffle logotype banner at the very top, shown from the message's
        # attachment (ShufflePanel.create_panel sends shuffle_logo.png). show_logo
        # is False only when that file is missing, so the panel still posts.
        if show_logo and MediaGalleryItem is not None:
            container.add_item(MediaGallery(MediaGalleryItem(f"attachment://{_LOGO_FILENAME}")))
        container.add_item(TextDisplay("## Verify Your Shuffle Account"))
        container.add_item(
            TextDisplay(
                "Verify that you're one of our Shuffle affiliates to unlock your reward role!\n\n"
                "**How to Verify:**\n"
                "Click the **'Verify Shuffle Account'** button below and enter your "
                "Shuffle.com username. We'll check it against our affiliate stats and "
                "grant your role instantly."
            )
        )
        container.add_item(
            TextDisplay(
                "**📋 Before you start**\n"
                "• Make sure you signed up on Shuffle using our affiliate code\n"
                "• Enter your **exact** Shuffle username\n"
                "• One Shuffle account per Discord user"
            )
        )
        container.add_item(Separator())

        verify_btn = Button(
            style=discord.ButtonStyle.success,
            label="Verify Shuffle Account",
            emoji=self.shuffle_emoji,
            custom_id="shuffle_verify",
        )
        verify_btn.callback = self._verify_callback
        row = ActionRow()
        row.add_item(verify_btn)
        container.add_item(row)

        self.add_item(container)

    async def _verify_callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(ShuffleVerifyModal(self.engine, self.settings_getter))
        except Exception as e:
            logger.error(f"Error opening Shuffle verify modal: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)


class ShufflePanel:
    """Manages the Shuffle verify panel message for a specific guild.

    Reuses the link_panels table, scoped by panel_type = 'shuffle_verify'.
    """

    PANEL_TYPE = "shuffle_verify"

    def __init__(self, bot, engine, settings_getter, guild_id=None, shuffle_emoji=None):
        self.bot = bot
        self.engine = engine
        self.settings_getter = settings_getter
        self.shuffle_emoji = shuffle_emoji
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
                    logger.debug(f"Loaded Shuffle panel info for guild {self.guild_id}")
        except Exception as e:
            logger.error(f"Failed to load Shuffle panel info for guild {self.guild_id}: {e}")

    def _save_panel_info(self, guild_id: int, channel_id: int, message_id: int):
        if not self.engine:
            return
        try:
            with self.engine.begin() as conn:
                # Only delete this guild's *shuffle* panels — leave the Kick link panel untouched
                conn.execute(
                    text("DELETE FROM link_panels WHERE guild_id = :guild_id AND panel_type = :ptype"),
                    {"guild_id": guild_id, "ptype": self.PANEL_TYPE},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO link_panels (guild_id, channel_id, message_id, emoji, panel_type, created_at)
                        VALUES (:guild_id, :channel_id, :message_id, '🎰', :ptype, CURRENT_TIMESTAMP)
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
            logger.error(f"Failed to save Shuffle panel info: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        try:
            # Components V2: the panel is a LayoutView (no embed — a V2 message
            # can't carry one). All copy lives inside the view's TextDisplays.
            # The logotype banner is attached and shown via the view's MediaGallery
            # (attachment://shuffle_logo.png); skip the gallery if the file is
            # missing so the panel still posts.
            has_logo = os.path.isfile(_LOGO_PATH)
            view = ShufflePanelView(
                self.bot, self.engine, self.settings_getter, shuffle_emoji=self.shuffle_emoji, show_logo=has_logo
            )
            if not has_logo:
                logger.warning(f"[Shuffle] {_LOGO_PATH} not found — posting panel without the logotype banner.")
            message = await channel.send(**_build_panel_message_kwargs(view, has_logo=has_logo, for_send=True))

            self.panel_guild_id = channel.guild.id
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.guild.id, channel.id, message.id)

            logger.info(f"Created Shuffle panel in {channel.guild.name} / #{channel.name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create Shuffle panel: {e}")
            return False


async def setup_shuffle_panel_system(bot, engine, settings_getter):
    """Set up the Shuffle verify panel system with per-guild instances."""
    panels = {}

    # Register (or reuse) the Shuffle brand-logo application emoji once, then
    # thread it into every panel so the verify button shows the logo.
    shuffle_emoji = await ensure_shuffle_emoji(bot)

    for guild in bot.guilds:
        panel = ShufflePanel(bot, engine, settings_getter, guild_id=guild.id, shuffle_emoji=shuffle_emoji)
        panels[guild.id] = panel
        logger.debug(f"✅ Shuffle verify panel initialized")

    @bot.hybrid_command(name="createshufflepanel")
    @commands.has_permissions(administrator=True)
    async def create_shuffle_panel_cmd(ctx):
        """[ADMIN] Create the Shuffle verify panel in this channel"""
        panel = panels.get(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Shuffle panel not initialized for this server")
            return

        success = await panel.create_panel(ctx.channel)
        if success:
            await ctx.send("✅ Shuffle verify panel created! Affiliates can now verify their accounts.")
        else:
            await ctx.send("❌ Failed to create Shuffle panel. Check logs for details.")

    # Re-attach views to existing panels on bot restart
    for guild_id, panel in panels.items():
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(panel.panel_message_id)
                    except discord.NotFound:
                        # The stored panel message was deleted in Discord; the DB row
                        # is a ghost. Re-post the panel (create_panel overwrites the
                        # stale message_id) so verification keeps working after a restart.
                        logger.warning(
                            f"[Shuffle] Stored panel message for guild {guild_id} is gone (404); re-posting."
                        )
                        if await panel.create_panel(channel):
                            logger.info(f"[Shuffle] Re-posted missing panel for guild {guild_id}")
                        continue
                    has_logo = os.path.isfile(_LOGO_PATH)
                    view = ShufflePanelView(
                        bot,
                        engine,
                        settings_getter,
                        shuffle_emoji=shuffle_emoji,
                        show_logo=has_logo,
                    )
                    await message.edit(
                        **_build_panel_message_kwargs(view, has_logo=has_logo, clear_attachments=not has_logo)
                    )
                    logger.info(f"[Shuffle] Refreshed panel view for guild {guild_id}")
            except Exception as e:
                logger.error(f"Failed to re-attach Shuffle panel view for guild {guild_id}: {e}")

    return panels
