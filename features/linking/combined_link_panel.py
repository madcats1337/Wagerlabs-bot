"""
Combined Link Panel — one Discord panel with per-platform link buttons.

Replaces the separate Kick-only (link_panel.py) and Twitch-only (twitch_panel.py)
panels with a single panel that shows a "Link Kick" and/or "Link Twitch" button
based on the server's `stream_platforms` setting:
  - kick-only server   → only the Kick button
  - twitch-only server → only the Twitch button
  - both               → both buttons

Each button hands the viewer a signed OAuth URL via the same generators the old
panels used (generate_signed_oauth_url / generate_signed_twitch_oauth_url), so the
existing /auth/kick and /auth/twitch/link viewer flows are unchanged. Links are
written per-platform (links.platform) so a viewer can link both.

panel_type = 'link' (one row per guild in link_panels).
"""

import logging
import os

import discord
from discord.ext import commands
from sqlalchemy import text

try:
    from discord import MediaGalleryItem
except Exception:  # pragma: no cover - compatibility with older discord.py versions
    MediaGalleryItem = None

try:
    from discord.ui import ActionRow, Button, Container, LayoutView, MediaGallery, Separator, TextDisplay
except Exception:  # pragma: no cover - compatibility with older discord.py versions
    from discord.ui import Button, View

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


logger = logging.getLogger(__name__)

PANEL_TYPE = "link"

# Unicode fallbacks used when a brand-logo application emoji isn't available
# (PNG missing or upload failed) so the panel buttons always render something.
FALLBACK_EMOJI = {"kick": "🟢", "twitch": "🟣"}

# Application-emoji names + the PNG files supplied under assets/emojis/.
_EMOJI_FILES = {"kick": "kick.png", "twitch": "twitch.png"}
_EMOJI_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "assets", "emojis")
_ASSET_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "assets")
_LOGO_PATH = os.path.join(_ASSET_ROOT, "branding", "wagerlabs_logo.png")
_LOGO_FILENAME = "wagerlabs_logo.png"


async def ensure_link_emojis(bot) -> dict:
    """Idempotently register the Kick/Twitch brand logos as the bot's application
    emojis and return ``{"kick": <Emoji|None>, "twitch": <Emoji|None>}``.

    On first run the PNGs under ``assets/emojis/`` are uploaded; on later runs the
    already-uploaded emojis are reused (matched by name). A missing PNG or any API
    error yields ``None`` for that platform, which makes the buttons fall back to
    the unicode circle — the panel still works either way.
    """
    result = {"kick": None, "twitch": None}
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
    except Exception as e:
        logger.warning(f"[CombinedLink] Could not fetch application emojis (using unicode fallback): {e}")
        return result

    for name, filename in _EMOJI_FILES.items():
        if name in existing:
            result[name] = existing[name]
            logger.info(f"[CombinedLink] Reusing existing '{name}' application emoji.")
            continue
        path = os.path.join(_EMOJI_DIR, filename)
        if not os.path.isfile(path):
            logger.warning(f"[CombinedLink] {filename} not found at {path} — '{name}' button falls back to unicode.")
            continue
        try:
            with open(path, "rb") as f:
                image_bytes = f.read()
            result[name] = await bot.create_application_emoji(name=name, image=image_bytes)
            logger.info(f"[CombinedLink] Uploaded '{name}' application emoji.")
        except Exception as e:
            logger.error(f"[CombinedLink] Failed to upload '{name}' application emoji: {e}")

    return result


def _active_platforms(engine, guild_id) -> list:
    """Return the platforms this server runs, from bot_settings.stream_platforms
    (default 'kick'). Always a non-empty list."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM bot_settings WHERE key = 'stream_platforms' AND discord_server_id = :g"),
                {"g": guild_id},
            ).fetchone()
        raw = (row[0] if row and row[0] else "kick") or "kick"
        platforms = [p.strip().lower() for p in str(raw).split(",") if p.strip()]
        return platforms or ["kick"]
    except Exception as e:
        logger.warning(f"[CombinedLink] Could not read stream_platforms for {guild_id}: {e}")
        return ["kick"]


# Brand accent colours used for the V2 Container stripe (matches the old embed
# colour logic): Twitch purple when twitch-only, otherwise Kick green.
TWITCH_COLOUR = 0x9146FF
KICK_COLOUR = 0x53FC18


class CombinedLinkPanelView(LayoutView):
    """Persistent Components-V2 panel with a Kick and/or Twitch link button.

    `platforms=None` builds the GLOBAL persistent view registered at startup —
    it carries BOTH buttons (both custom_ids) so every posted message re-binds its
    handlers after a restart. When posting per-guild, pass `platforms` so only the
    server's active buttons render. Each button still checks at click time whether
    its platform is active and whether the user is already linked."""

    def __init__(
        self,
        bot,
        engine,
        kick_url_generator,
        twitch_url_generator,
        kick_emoji=None,
        twitch_emoji=None,
        platforms=None,
        show_logo=True,
    ):
        super().__init__(timeout=None)  # Persistent
        self.bot = bot
        self.engine = engine
        self.kick_url_generator = kick_url_generator
        self.twitch_url_generator = twitch_url_generator
        # Brand-logo application emojis (or None → unicode fallback).
        self.kick_emoji = kick_emoji or FALLBACK_EMOJI["kick"]
        self.twitch_emoji = twitch_emoji or FALLBACK_EMOJI["twitch"]

        # None → global persistent view: both platforms, generic copy.
        show_kick = platforms is None or "kick" in platforms
        show_twitch = platforms is None or "twitch" in platforms
        if platforms:
            names = " & ".join(p.capitalize() for p in platforms)
        else:
            names = "Kick & Twitch"
        accent = TWITCH_COLOUR if platforms == ["twitch"] else KICK_COLOUR

        container = Container(accent_colour=accent)
        if show_logo and MediaGalleryItem is not None:
            container.add_item(MediaGallery(MediaGalleryItem(f"attachment://{_LOGO_FILENAME}")))
        container.add_item(TextDisplay("## 🔗 Link Your Account"))
        container.add_item(
            TextDisplay(
                f"Link your {names} account with Discord to participate in raffles and track your watchtime!\n\n"
                "**Benefits:**\n"
                "• Earn raffle tickets from watchtime\n"
                "• Get bonus tickets from subscriptions\n"
                "• Participate in raffles and giveaways\n"
                "• Earn roles based on chat activity\n"
                "• Track your stats and progress"
            )
        )
        if show_kick and show_twitch:
            how_to = "**How to Link**\nUse the buttons below — you can link **both** platforms."
        else:
            only = "Twitch" if show_twitch and not show_kick else "Kick"
            how_to = f"**How to Link**\nClick the **'Link {only}'** button below to get started!"
        container.add_item(TextDisplay(how_to))
        container.add_item(
            TextDisplay(
                "**🔒 Privacy & Security**\n"
                "• OAuth links are unique and expire after 10 minutes\n"
                "• Links are sent privately, only you can see them\n"
                "• Your password is never shared with this bot"
            )
        )
        container.add_item(Separator())

        # Buttons live in an ActionRow inside the container. Built imperatively
        # (not via decorators) so per-platform filtering is a simple include/skip.
        row = ActionRow()
        if show_kick:
            kick_btn = Button(
                style=discord.ButtonStyle.success,
                label="Link Kick",
                emoji=self.kick_emoji,
                custom_id="link_kick_account",
            )
            kick_btn.callback = self._kick_callback
            row.add_item(kick_btn)
        if show_twitch:
            twitch_btn = Button(
                style=discord.ButtonStyle.secondary,
                label="Link Twitch",
                emoji=self.twitch_emoji,
                custom_id="link_twitch_account",
            )
            twitch_btn.callback = self._twitch_callback
            row.add_item(twitch_btn)
        container.add_item(row)

        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return True

    async def _kick_callback(self, interaction: discord.Interaction):
        try:
            await self._handle(interaction, "kick", self.kick_url_generator, self.kick_emoji)
        except Exception as e:
            logger.error(f"[CombinedLink] Kick button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)

    async def _twitch_callback(self, interaction: discord.Interaction):
        try:
            await self._handle(interaction, "twitch", self.twitch_url_generator, self.twitch_emoji)
        except Exception as e:
            logger.error(f"[CombinedLink] Twitch button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)

    async def _handle(self, interaction, platform, url_generator, label_emoji):
        discord_id = interaction.user.id
        guild_id = interaction.guild.id if interaction.guild else None

        # Gate by the server's active platforms.
        if platform not in _active_platforms(self.engine, guild_id):
            await interaction.response.send_message(
                f"❌ {platform.capitalize()} linking isn't enabled on this server.", ephemeral=True
            )
            return

        # Already linked on this platform for this server?
        try:
            with self.engine.connect() as conn:
                existing = conn.execute(
                    text(
                        """
                        SELECT kick_name FROM links
                        WHERE discord_id = :d AND (:sid IS NULL OR discord_server_id = :sid)
                        AND platform = :p
                        """
                    ),
                    {"d": discord_id, "sid": guild_id, "p": platform},
                ).fetchone()
            if existing:
                await interaction.response.send_message(
                    f"✅ Your {platform.capitalize()} is already linked to **{existing[0]}**!", ephemeral=True
                )
                return
        except Exception as e:
            logger.error(f"[CombinedLink] DB error checking {platform} link: {e}")
            await interaction.response.send_message("❌ Database error. Please try again.", ephemeral=True)
            return

        try:
            oauth_url = url_generator(discord_id, guild_id)
        except Exception as e:
            logger.error(f"[CombinedLink] Error generating {platform} OAuth URL: {e}")
            await interaction.response.send_message(
                "❌ Failed to generate OAuth link. Please contact an admin.", ephemeral=True
            )
            return

        # Components-V2 ephemeral follow-up: a small container with the prompt text
        # and the OAuth link button.
        accent = TWITCH_COLOUR if platform == "twitch" else KICK_COLOUR
        view = LayoutView(timeout=600)
        container = Container(accent_colour=accent)
        container.add_item(
            TextDisplay(
                f"{label_emoji} Click the button below to link your {platform.capitalize()} account "
                f"(expires in 10 minutes):"
            )
        )
        row = ActionRow()
        row.add_item(
            Button(
                label=f"Authorize with {platform.capitalize()}",
                style=discord.ButtonStyle.link,
                url=oauth_url,
                emoji=label_emoji,
            )
        )
        container.add_item(row)
        view.add_item(container)
        await interaction.response.send_message(view=view, ephemeral=True)
        logger.info(f"[CombinedLink] Sent {platform} OAuth link to {interaction.user.name} ({discord_id})")


def _build_panel_message_kwargs(view, has_logo=False, clear_attachments=False, for_send=False):
    """Build send/edit kwargs for a link panel message, including the logo attachment when needed.

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


def _build_view_for_guild(
    bot,
    engine,
    kick_url_generator,
    twitch_url_generator,
    guild_id,
    kick_emoji=None,
    twitch_emoji=None,
    show_logo=True,
):
    """Build a view showing only the buttons for the guild's active platforms.
    Used when POSTING the panel (so a kick-only server shows just the Kick button).
    The global persistent view (platforms=None) keeps both custom_ids alive."""
    platforms = _active_platforms(engine, guild_id)
    view = CombinedLinkPanelView(
        bot,
        engine,
        kick_url_generator,
        twitch_url_generator,
        kick_emoji=kick_emoji,
        twitch_emoji=twitch_emoji,
        platforms=platforms,
        show_logo=show_logo,
    )
    return view, platforms


class CombinedLinkPanel:
    """Manages the combined link panel message for a specific guild."""

    def __init__(
        self, bot, engine, kick_url_generator, twitch_url_generator, guild_id=None, kick_emoji=None, twitch_emoji=None
    ):
        self.bot = bot
        self.engine = engine
        self.kick_url_generator = kick_url_generator
        self.twitch_url_generator = twitch_url_generator
        self.kick_emoji = kick_emoji
        self.twitch_emoji = twitch_emoji
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
                    {"guild_id": self.guild_id, "ptype": PANEL_TYPE},
                ).fetchone()
                if result:
                    self.panel_guild_id, self.panel_channel_id, self.panel_message_id = result
        except Exception as e:
            logger.error(f"[CombinedLink] Failed to load panel info for {self.guild_id}: {e}")

    def _save_panel_info(self, guild_id, channel_id, message_id):
        if not self.engine:
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM link_panels WHERE guild_id = :guild_id AND panel_type = :ptype"),
                    {"guild_id": guild_id, "ptype": PANEL_TYPE},
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO link_panels (guild_id, channel_id, message_id, emoji, panel_type, created_at)
                        VALUES (:guild_id, :channel_id, :message_id, '🔗', :ptype, CURRENT_TIMESTAMP)
                        """
                    ),
                    {"guild_id": guild_id, "channel_id": channel_id, "message_id": message_id, "ptype": PANEL_TYPE},
                )
        except Exception as e:
            logger.error(f"[CombinedLink] Failed to save panel info: {e}")

    async def create_panel(self, channel: discord.TextChannel):
        try:
            has_logo = os.path.isfile(_LOGO_PATH)
            view, platforms = _build_view_for_guild(
                self.bot,
                self.engine,
                self.kick_url_generator,
                self.twitch_url_generator,
                channel.guild.id,
                kick_emoji=self.kick_emoji,
                twitch_emoji=self.twitch_emoji,
                show_logo=has_logo,
            )
            names = " & ".join(p.capitalize() for p in platforms)
            # Components V2: the panel is a LayoutView (no embed — a V2 message
            # can't carry one). All copy lives inside the view's TextDisplays.
            if not has_logo:
                logger.warning(f"[CombinedLink] {_LOGO_PATH} not found — posting panel without the logotype banner.")
            message = await channel.send(**_build_panel_message_kwargs(view, has_logo=has_logo, for_send=True))
            self.panel_guild_id = channel.guild.id
            self.panel_channel_id = channel.id
            self.panel_message_id = message.id
            self._save_panel_info(channel.guild.id, channel.id, message.id)
            logger.info(f"[CombinedLink] Created panel in {channel.guild.name} / #{channel.name} ({names})")
            return True
        except Exception as e:
            logger.error(f"[CombinedLink] Failed to create panel: {e}")
            return False


async def setup_combined_link_panel_system(bot, engine, kick_url_generator, twitch_url_generator):
    """Set up the combined link panel: per-guild instances, a global persistent
    view (both custom_ids), the !createlinkpanel command, and restart re-attach."""
    # Register (or reuse) the Kick/Twitch brand-logo application emojis once, then
    # thread them into every view so the panel buttons show the logos.
    emojis = await ensure_link_emojis(bot)
    kick_emoji = emojis.get("kick")
    twitch_emoji = emojis.get("twitch")

    panels = {}
    for guild in bot.guilds:
        panels[guild.id] = CombinedLinkPanel(
            bot,
            engine,
            kick_url_generator,
            twitch_url_generator,
            guild_id=guild.id,
            kick_emoji=kick_emoji,
            twitch_emoji=twitch_emoji,
        )

    # Register ONE global persistent view carrying both custom_ids so buttons keep
    # working across restarts regardless of which platforms a guild shows.
    try:
        bot.add_view(
            CombinedLinkPanelView(
                bot, engine, kick_url_generator, twitch_url_generator, kick_emoji=kick_emoji, twitch_emoji=twitch_emoji
            )
        )
    except Exception as e:
        logger.warning(f"[CombinedLink] add_view failed (non-fatal): {e}")

    @bot.hybrid_command(name="createlinkpanel")
    @commands.has_permissions(administrator=True)
    async def create_link_panel_cmd(ctx):
        """[ADMIN] Create the combined link panel in this channel."""
        panel = panels.get(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Link panel not initialized for this server")
            return
        if await panel.create_panel(ctx.channel):
            await ctx.send("✅ Link panel created! Users can now link their accounts.")
        else:
            await ctx.send("❌ Failed to create link panel. Check logs for details.")

    # Re-attach the (platform-filtered) view to existing panel messages on restart.
    from utils.log_context import clear_server, set_server

    for guild_id, panel in panels.items():
        # Tag this guild's panel-refresh logs with the server (not "[-]").
        _g = bot.get_guild(guild_id) if guild_id else None
        set_server(guild_id, _g.name if _g else None)
        if panel.panel_message_id and panel.panel_channel_id:
            try:
                channel = bot.get_channel(panel.panel_channel_id)
                if channel:
                    try:
                        message = await channel.fetch_message(panel.panel_message_id)
                    except discord.NotFound:
                        # The stored panel message was deleted in Discord; the DB row
                        # is a ghost. Re-post the panel (create_panel overwrites the
                        # stale message_id) so linking keeps working after a restart.
                        logger.warning(
                            f"[CombinedLink] Stored panel message for guild {guild_id} is gone (404); re-posting."
                        )
                        if await panel.create_panel(channel):
                            logger.info(f"[CombinedLink] Re-posted missing panel for guild {guild_id}")
                        continue
                    has_logo = os.path.isfile(_LOGO_PATH)
                    view, _ = _build_view_for_guild(
                        bot,
                        engine,
                        kick_url_generator,
                        twitch_url_generator,
                        guild_id,
                        kick_emoji=kick_emoji,
                        twitch_emoji=twitch_emoji,
                        show_logo=has_logo,
                    )
                    await message.edit(
                        **_build_panel_message_kwargs(view, has_logo=has_logo, clear_attachments=not has_logo)
                    )
                    logger.info(f"[CombinedLink] Refreshed panel view for guild {guild_id}")
            except Exception as e:
                logger.error(f"[CombinedLink] Failed to re-attach view for guild {guild_id}: {e}")

    # Don't let the last guild's tag bleed into subsequent global startup logs.
    clear_server()
    return panels
