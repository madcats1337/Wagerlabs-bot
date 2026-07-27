"""Global Discord slash commands exposed by the Wagerlabs application."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.log_context import server_context
from utils.server_urls import get_server_public_page_url

logger = logging.getLogger(__name__)

WAGERLABS_YELLOW = 0xFACC15
WAGERLABS_LANDING_URL = "https://wagerlabs.app/"


def command_invocation_id(ctx) -> int:
    """Return the stable Discord snowflake for a prefix or slash invocation."""

    interaction = getattr(ctx, "interaction", None)
    if interaction is not None:
        return interaction.id
    return ctx.message.id


async def defer_slash_response(ctx, *, ephemeral: bool = False) -> bool:
    """Acknowledge a slash invocation before slow work. No-op for prefix commands.

    Discord closes an interaction that is not acknowledged within 3 seconds, and
    the user sees "The application did not respond" even when the command later
    succeeds. Hybrid commands that hit the database, a third-party API, or a bulk
    Discord operation before their first ``ctx.send`` must call this first.

    Prefix invocations have no interaction and no deadline, so they are left
    untouched — behaviour there is unchanged.

    Returns ``True`` when this call deferred the interaction.
    """

    interaction = getattr(ctx, "interaction", None)
    if interaction is None:
        return False

    response = getattr(interaction, "response", None)
    if response is None:
        return False

    try:
        if response.is_done():
            return False
        await response.defer(ephemeral=ephemeral)
    except Exception as exc:
        # A failed defer must never take the command down with it; the command
        # body can still try to respond and discord.py surfaces the real error.
        logger.warning("Could not defer slash interaction for %s: %s", getattr(ctx, "command", None), exc)
        return False

    return True


def _link_view(*buttons: tuple[str, str]) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, url in buttons:
        view.add_item(discord.ui.Button(label=label, url=url))
    return view


def register_wagerlabs_slash_commands(bot: commands.Bot, engine) -> None:
    """Register application-only commands once on the local command tree.

    Legacy ``commands`` handlers are registered as hybrid commands by their
    owning modules. Keeping this module limited to application-only commands
    avoids duplicate tree entries when those cogs are loaded.
    """

    if bot.tree.get_command("wagerlabs") is None:

        @bot.tree.command(
            name="wagerlabs",
            description="Learn what Wagerlabs does and open the official website.",
        )
        @app_commands.guild_only()
        async def wagerlabs(interaction: discord.Interaction) -> None:
            guild_id = interaction.guild_id
            guild_name = interaction.guild.name if interaction.guild else None
            with server_context(guild_id, guild_name):
                fair_url = get_server_public_page_url(engine, guild_id, "/provably-fair")
                embed = discord.Embed(
                    title="Wagerlabs",
                    description=(
                        "Stream automation for Kick and Twitch creators, with "
                        "optional Discord integration, viewer rewards, raffles, "
                        "slot requests, games, and OBS widgets."
                    ),
                    color=WAGERLABS_YELLOW,
                )
                embed.add_field(
                    name="Official website",
                    value="Open the Wagerlabs landing page to explore the platform.",
                    inline=False,
                )
                embed.set_footer(text="wagerlabs.app")
                view = _link_view(
                    ("Open Wagerlabs", WAGERLABS_LANDING_URL),
                    ("Verify draws", fair_url),
                )
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                logger.debug("Handled /wagerlabs")


async def sync_global_slash_commands(bot: commands.Bot) -> bool:
    """Publish the local command tree globally, once per process.

    A failed HTTP sync leaves the guard unset so a later Discord gateway
    reconnect can retry without restarting the service.
    """

    if getattr(bot, "_wagerlabs_slash_commands_synced", False):
        return True

    try:
        synced = await bot.tree.sync()
    except Exception as exc:
        logger.warning(
            "Could not sync global Discord slash commands: %s",
            exc,
            exc_info=True,
        )
        return False

    bot._wagerlabs_slash_commands_synced = True
    command_names = ", ".join(f"/{command.name}" for command in synced) or "none"
    logger.info("Global Discord slash commands synced: %s", command_names)
    return True
