"""`/giveaway start` — launch a giveaway from a saved template.

Templates hold the reusable settings (entry method, winners, channel, role
gate, bonus roles); the command supplies only what changes per run: the title,
an optional description, and the duration.

Registered once on the local command tree in on_ready, alongside the other
application-only commands (see features/discord_app_commands.py).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from utils.log_context import server_context

logger = logging.getLogger(__name__)

WAGERLABS_YELLOW = 0xFACC15

# Discord shows at most 25 autocomplete choices.
_MAX_CHOICES = 25


def _fetch_templates(engine, guild_id, needle=""):
    """Templates for this guild whose name contains `needle` (case-insensitive)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, name
                FROM giveaway_templates
                WHERE discord_server_id = :sid
                  AND (:needle = '' OR name ILIKE '%' || :needle || '%')
                ORDER BY name ASC
                LIMIT :limit
                """
            ),
            {"sid": str(guild_id), "needle": needle or "", "limit": _MAX_CHOICES},
        ).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def _fetch_template(engine, template_id, guild_id):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, name, entry_method, max_winners, allow_multiple_entries,
                       max_entries_per_user, required_role_id, discord_channel_id,
                       keyword, messages_required, time_window_minutes, bonus_roles,
                       entry_prompt
                FROM giveaway_templates
                WHERE id = :tid AND discord_server_id = :sid
                """
            ),
            {"tid": template_id, "sid": str(guild_id)},
        ).fetchone()
    return dict(row._mapping) if row else None


def register_giveaway_commands(bot: commands.Bot, engine) -> None:
    """Add the /giveaway group to the tree (idempotent)."""
    if bot.tree.get_command("giveaway") is not None:
        return

    giveaway_group = app_commands.Group(
        name="giveaway",
        description="Start and manage giveaways.",
        guild_only=True,
    )

    @giveaway_group.command(name="start", description="Start a giveaway from a saved template.")
    @app_commands.describe(
        template="Which saved template to use",
        title="Giveaway title, shown on the panel",
        days="Duration: days",
        hours="Duration: hours",
        minutes="Duration: minutes",
        description="Optional description shown under the title",
    )
    async def giveaway_start(
        interaction: discord.Interaction,
        template: str,
        title: str,
        days: int = 0,
        hours: int = 0,
        minutes: int = 0,
        description: str = None,
    ) -> None:
        guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else None

        with server_context(guild_id, guild_name):
            # Only admins may start a giveaway — the panel posts publicly and
            # the draw is real.
            perms = getattr(interaction.user, "guild_permissions", None)
            if not perms or not (perms.administrator or perms.manage_guild):
                await interaction.response.send_message(
                    "You need Manage Server permission to start a giveaway.", ephemeral=True
                )
                return

            duration_minutes = (max(0, days) * 1440) + (max(0, hours) * 60) + max(0, minutes)
            if duration_minutes <= 0:
                await interaction.response.send_message(
                    "Set a duration: give at least one of days, hours or minutes.", ephemeral=True
                )
                return

            try:
                template_id = int(template)
            except (TypeError, ValueError):
                await interaction.response.send_message("Pick a template from the list.", ephemeral=True)
                return

            tpl = _fetch_template(engine, template_id, guild_id)
            if not tpl:
                await interaction.response.send_message("That template no longer exists.", ephemeral=True)
                return

            # One active giveaway per server, matching the dashboard's rule.
            with engine.connect() as conn:
                active = conn.execute(
                    text("SELECT id FROM giveaways WHERE discord_server_id = :sid AND status = 'active' LIMIT 1"),
                    {"sid": guild_id},
                ).fetchone()
            if active:
                await interaction.response.send_message(
                    "There is already an active giveaway. Stop it before starting another.",
                    ephemeral=True,
                )
                return

            # Posting the panel is a Discord round-trip; acknowledge first.
            await interaction.response.defer(ephemeral=True)

            import json as _json

            bonus_roles = tpl.get("bonus_roles")
            with engine.begin() as conn:
                # Created ACTIVE with its deadline already resolved — the bot's
                # expiry loop compares ends_at against the DB clock, so both use
                # the same clock.
                row = conn.execute(
                    text(
                        """
                        INSERT INTO giveaways
                          (discord_server_id, title, description, entry_method, keyword,
                           messages_required, time_window_minutes, allow_multiple_entries,
                           max_entries_per_user, status, created_by, discord_channel_id,
                           duration_minutes, max_winners, required_role_id, bonus_roles,
                           entry_prompt, started_at, ends_at)
                        VALUES
                          (:sid, :title, :description, :entry_method, :keyword,
                           :messages_required, :time_window_minutes, :allow_multiple,
                           :max_per_user, 'active', :created_by, :channel_id,
                           :duration, :max_winners, :required_role_id, CAST(:bonus AS JSONB),
                           :entry_prompt, CURRENT_TIMESTAMP,
                           CURRENT_TIMESTAMP + (:duration * INTERVAL '1 minute'))
                        RETURNING id
                        """
                    ),
                    {
                        "sid": guild_id,
                        "title": title[:255],
                        "description": description,
                        "entry_method": tpl.get("entry_method") or "discord",
                        "keyword": tpl.get("keyword"),
                        "messages_required": tpl.get("messages_required"),
                        "time_window_minutes": tpl.get("time_window_minutes"),
                        "allow_multiple": bool(tpl.get("allow_multiple_entries")),
                        "max_per_user": int(tpl.get("max_entries_per_user") or 1),
                        "created_by": str(interaction.user),
                        "channel_id": tpl.get("discord_channel_id"),
                        "duration": duration_minutes,
                        "max_winners": max(1, int(tpl.get("max_winners") or 1)),
                        "required_role_id": tpl.get("required_role_id"),
                        "bonus": _json.dumps(bonus_roles) if bonus_roles else None,
                        "entry_prompt": tpl.get("entry_prompt"),
                    },
                ).fetchone()
                giveaway_id = int(row[0])

            # Refresh the manager cache so chat-based entry methods see it too.
            try:
                manager = getattr(bot, "giveaway_managers", {}).get(guild_id)
                if manager:
                    await manager.load_active_giveaway()
            except Exception as e:
                logger.warning(f"[giveaway] manager reload failed: {e}")

            # Post the interactive panel for Discord-hosted giveaways.
            posted = False
            if (tpl.get("entry_method") or "discord") == "discord":
                try:
                    from .giveaway_panel import post_panel

                    giveaway = _fetch_started_giveaway(engine, giveaway_id, guild_id)
                    if giveaway:
                        posted = await post_panel(bot, engine, guild_id, giveaway)
                except Exception as e:
                    logger.error(f"[giveaway] panel post failed: {e}", exc_info=True)

            embed = discord.Embed(
                title="Giveaway started",
                description=f"**{title}**",
                color=WAGERLABS_YELLOW,
            )
            embed.add_field(name="Template", value=tpl["name"], inline=True)
            embed.add_field(
                name="Ends",
                value=f"<t:{_ends_at_epoch(engine, giveaway_id)}:R>",
                inline=True,
            )
            if not posted and (tpl.get("entry_method") or "discord") == "discord":
                embed.add_field(
                    name="Note",
                    value="The panel could not be posted — check the template's channel.",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"[giveaway] /giveaway start -> id={giveaway_id} via template {tpl['name']}")

    @giveaway_start.autocomplete("template")
    async def template_autocomplete(interaction: discord.Interaction, current: str):
        try:
            rows = _fetch_templates(engine, interaction.guild_id, current or "")
        except Exception as e:
            logger.warning(f"[giveaway] template autocomplete failed: {e}")
            return []
        # The value carries the id; Discord shows the name.
        return [app_commands.Choice(name=name, value=str(tid)) for tid, name in rows]

    bot.tree.add_command(giveaway_group)
    logger.debug("Registered /giveaway command group")


def _fetch_started_giveaway(engine, giveaway_id, guild_id):
    """The row shape post_panel expects."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, title, description, status, started_at, ends_at, ended_at,
                       max_winners, discord_channel_id, discord_message_id,
                       required_role_id, entry_prompt, bonus_roles
                FROM giveaways
                WHERE id = :gid AND discord_server_id = :sid
                """
            ),
            {"gid": giveaway_id, "sid": guild_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def _ends_at_epoch(engine, giveaway_id) -> int:
    """Unix seconds for the giveaway's deadline, for a Discord timestamp."""
    from datetime import timezone

    with engine.connect() as conn:
        row = conn.execute(text("SELECT ends_at FROM giveaways WHERE id = :gid"), {"gid": giveaway_id}).fetchone()
    if not row or not row[0]:
        return 0
    value = row[0]
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())
