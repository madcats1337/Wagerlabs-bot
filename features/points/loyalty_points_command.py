"""`/loyaltypoints` — viewer-facing explainers for the loyalty points system.

Two topics, chosen with a required command choice:

  * ``earn`` — how points accrue (watchtime conversion) and what gates it.
  * ``shop`` — how to spend them, in the Discord storefront and the web shop.

Both replies are Components V2 (``LayoutView`` + ``Container``), matching the
link/verify/shop panels rather than the older ``discord.Embed`` help commands.

The copy is generated from the server's LIVE configuration — the actual points
rate, the real shop channel, the real shop URL — so it can never drift from what
the bot is doing. Anything not configured is described in general terms instead
of being asserted, and every DB read degrades to the generic wording rather than
failing the command.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from utils.log_context import server_context
from utils.server_urls import get_server_public_page_url

logger = logging.getLogger(__name__)

# Gold, matching the point shop storefront + /points embeds.
POINTS_GOLD = 0xFFD700

# Mirrors award_points() in bot.py: watchtime converts in whole 5-minute blocks,
# and the rate is per block. Kept as the fallback when point_settings has no row.
MINUTES_PER_BLOCK = 5
DEFAULT_POINTS_PER_BLOCK = 1


def _points_rate(engine, guild_id):
    """This server's points-per-5-minutes, or the default when unset.

    Reads `point_settings` the same way `award_points` does, including the
    NULL-server fallback row, so the number quoted to viewers is the number they
    will actually be paid.
    """
    if engine is None or guild_id is None:
        return DEFAULT_POINTS_PER_BLOCK
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT value FROM point_settings
                    WHERE key = 'points_per_5min'
                      AND (discord_server_id = :sid OR discord_server_id IS NULL)
                    ORDER BY discord_server_id NULLS LAST
                    LIMIT 1
                    """
                ),
                {"sid": guild_id},
            ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception as exc:
        logger.warning("Could not read points_per_5min for guild %s: %s", guild_id, exc)
    return DEFAULT_POINTS_PER_BLOCK


def _shop_channel_id(engine, guild_id):
    """The channel holding this server's Discord storefront, or None."""
    if engine is None or guild_id is None:
        return None
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM point_settings " "WHERE key = 'shop_channel_id' AND discord_server_id = :sid"),
                {"sid": guild_id},
            ).fetchone()
        if row and row[0]:
            return int(row[0])
    except Exception as exc:
        logger.warning("Could not read shop_channel_id for guild %s: %s", guild_id, exc)
    return None


def _has_shop_items(engine, guild_id):
    """Whether the server has any active shop item.

    Drives whether the shop explainer promises a catalog or tells the viewer to
    check back — describing a storefront that renders "No items available" reads
    as the bot being broken.
    """
    if engine is None or guild_id is None:
        return False
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM point_shop_items " "WHERE is_active = TRUE AND discord_server_id = :sid LIMIT 1"),
                {"sid": guild_id},
            ).fetchone()
        return bool(row)
    except Exception as exc:
        logger.warning("Could not check shop items for guild %s: %s", guild_id, exc)
    return False


def _earn_view(rate, points_url):
    """Components V2 explainer for how points are earned."""
    per_hour = rate * (60 // MINUTES_PER_BLOCK)

    container = discord.ui.Container(accent_colour=POINTS_GOLD)
    container.add_item(discord.ui.TextDisplay("## Earning Loyalty Points"))
    container.add_item(
        discord.ui.TextDisplay(
            "Loyalty points are earned automatically by watching the stream. "
            "There is nothing to claim and no command to run — just watch."
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**How it works**\n"
            f"• Every **{MINUTES_PER_BLOCK} minutes** of watchtime earns "
            f"**{rate:,}** point{'' if rate == 1 else 's'}\n"
            f"• That works out to roughly **{per_hour:,}** points per hour of watching\n"
            "• Watchtime is tracked while the stream is live and you are in chat\n"
            "• Points are credited in whole blocks, so partial minutes carry over "
            "to your next block rather than being lost"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Before you can earn**\n"
            "• Link your Kick or Twitch account using this server's "
            "**Link Account** panel — unlinked viewers are not tracked\n"
            "• Your balance is shared across Kick and Twitch: linking both does "
            "not earn twice, and either account spends the same points"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Checking your balance**\n"
            "• Run `/points` here in Discord\n"
            "• Or press **Check Balance** on the point shop message\n"
            "• `/pointslb` shows the server's top point holders"
        )
    )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    if points_url:
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Open the web shop", url=points_url))
        view.add_item(row)
    return view


def _shop_view(shop_channel_id, shop_url, has_items):
    """Components V2 explainer for spending points in either storefront."""
    container = discord.ui.Container(accent_colour=POINTS_GOLD)
    container.add_item(discord.ui.TextDisplay("## Using the Points Shop"))
    container.add_item(
        discord.ui.TextDisplay(
            "Spend your loyalty points on rewards. The shop is available in two "
            "places and both draw from the same balance and the same stock."
            if has_items
            else "Spend your loyalty points on rewards. No items are listed right "
            "now — check back once the shop has been stocked."
        )
    )
    container.add_item(discord.ui.Separator())

    channel_line = (
        f"• Go to <#{shop_channel_id}> and find the **Point Shop** message"
        if shop_channel_id
        else "• Find the **Point Shop** message in this server"
    )
    container.add_item(
        discord.ui.TextDisplay(
            "**In Discord**\n"
            f"{channel_line}\n"
            "• Press **Buy Now** to open a private item picker, then choose an item\n"
            "• Confirm the purchase — points are deducted immediately and the "
            "item's stock goes down\n"
            "• **Check Balance** shows your points without leaving the channel\n"
            "• The shop is one message that updates in place, so it stays where it is"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**On the web**\n"
            "• Open the web shop with the button below\n"
            "• Sign in with Discord so the shop knows whose points to spend\n"
            "• Browse the catalog, then purchase — the web shop also shows your "
            "full purchase history\n"
            "• You can opt in to a DM when a sold-out item is restocked, or when "
            "purchase limits reset"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Good to know**\n"
            "• Some items have a per-person purchase limit, so you may be capped "
            "even with enough points\n"
            "• Sold-out items stay listed until restocked\n"
            "• If a purchase is cancelled, your points are refunded and the "
            "purchase no longer counts against your limit"
        )
    )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    if shop_url:
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Open the web shop", url=shop_url))
        view.add_item(row)
    return view


def register_loyalty_points_command(bot: commands.Bot, engine) -> None:
    """Register `/loyaltypoints` on the local tree (idempotent).

    Guarded like the other application-only commands so a gateway reconnect
    that re-runs setup does not raise a duplicate-command error.
    """

    if bot.tree.get_command("loyaltypoints") is not None:
        return

    @bot.tree.command(
        name="loyaltypoints",
        description="How to earn loyalty points and how to use the points shop.",
    )
    @app_commands.guild_only()
    @app_commands.describe(topic="Which explainer to show")
    @app_commands.choices(
        topic=[
            app_commands.Choice(name="earn — how to earn loyalty points", value="earn"),
            app_commands.Choice(name="shop — how to use the points shop", value="shop"),
        ]
    )
    async def loyaltypoints(interaction: discord.Interaction, topic: app_commands.Choice[str]) -> None:
        guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else None
        with server_context(guild_id, guild_name):
            # Reads the DB before replying, so acknowledge the interaction first
            # or Discord closes it at 3s with "application did not respond".
            try:
                await interaction.response.defer(ephemeral=True)
            except Exception as exc:
                logger.warning("Could not defer /loyaltypoints: %s", exc)

            shop_url = get_server_public_page_url(engine, guild_id, "/shop")

            if topic.value == "earn":
                view = _earn_view(_points_rate(engine, guild_id), shop_url)
            else:
                view = _shop_view(
                    _shop_channel_id(engine, guild_id),
                    shop_url,
                    _has_shop_items(engine, guild_id),
                )

            # Ephemeral: this is a help reply to one viewer, not a channel post.
            await interaction.followup.send(view=view, ephemeral=True)
            logger.debug("Handled /loyaltypoints %s", topic.value)
