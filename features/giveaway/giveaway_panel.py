"""Discord-hosted giveaway panel (Components V2).

A per-giveaway message posted to the channel chosen on the dashboard. It shows
the title/description, the live entry count, an optional countdown, and a Join
button. Unlike the standing panels (link/rules/shop) this is NOT a singleton
bound to a bot_settings channel key — each giveaway owns its own message, and
the channel comes from `giveaways.discord_channel_id`.

Persistence model (the part that matters):
  * `GiveawayPanelView.template()` is registered once via `bot.add_view()` in
    on_ready. It carries the `giveaway_enter` custom_id, so Join buttons on
    messages posted BEFORE a restart re-bind their handler afterwards.
  * The callback is therefore stateless: it resolves everything from
    `interaction.guild_id` + the DB. Nothing about a specific giveaway may be
    captured in the view, because the template instance has no giveaway.

Rendered with Components V2 — a LayoutView only, no embed and no top-level
content (a V2 message cannot carry an embed).
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from sqlalchemy import text

logger = logging.getLogger(__name__)

ACCENT = 0xFACC15  # Wagerlabs yellow

# Panel edits are coalesced per giveaway: a burst of Join clicks would otherwise
# issue one Discord edit each and hit the per-channel rate limit. The entry count
# is cosmetic, so eventual consistency within a few seconds is fine.
_EDIT_DEBOUNCE_SECONDS = 5
_pending_edits: set[int] = set()


def _fmt_deadline(ends_at) -> str:
    """Discord relative timestamp, e.g. "ends <t:1699999999:R>".

    Rendered and ticked CLIENT-side by Discord, which is why the panel doesn't
    need a fast edit loop to keep a countdown current.
    """
    if not ends_at:
        return ""
    if isinstance(ends_at, str):
        try:
            ends_at = datetime.fromisoformat(ends_at)
        except ValueError:
            return ""
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    return f"<t:{int(ends_at.timestamp())}:R>"


def _fmt_stamp(value) -> str:
    """Absolute Discord timestamp: "14 August 2026 21:00".

    `<t:...:f>` renders date + time in each VIEWER's own timezone, which a
    preformatted UTC string cannot do.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:f>"


def _entry_count(engine, giveaway_id, guild_id) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT COALESCE(SUM(entry_count), 0) AS n
                FROM giveaway_entries
                WHERE giveaway_id = :gid AND discord_server_id = :sid
                """
            ),
            {"gid": giveaway_id, "sid": guild_id},
        ).fetchone()
    return int(row[0]) if row else 0


def _active_discord_giveaway(engine, guild_id):
    """The guild's live Discord-hosted giveaway, or None.

    Read fresh on every interaction — the persistent view is shared across all
    guilds and all giveaways, so this is the only source of truth.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, title, description, status, started_at, ends_at, ended_at,
                       max_winners, discord_channel_id, discord_message_id,
                       allow_multiple_entries, max_entries_per_user,
                       required_role_id, entry_prompt
                FROM giveaways
                WHERE discord_server_id = :sid
                  AND entry_method = 'discord'
                  AND status = 'active'
                ORDER BY started_at DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"sid": guild_id},
        ).fetchone()
    return dict(row._mapping) if row else None


class GiveawayPanelView(discord.ui.LayoutView):
    """Giveaway panel: title/description, entry count, optional countdown, Join.

    `giveaway=None` builds the stateless TEMPLATE registered at startup — it
    still carries the Join button (and therefore its custom_id) so previously
    posted panels keep working across restarts.
    """

    def __init__(self, engine, giveaway=None, entry_count=0, winners=None, ended=False):
        super().__init__(timeout=None)
        self.engine = engine

        container = discord.ui.Container(accent_colour=ACCENT)

        g = giveaway or {}
        title = g.get("title") or "Giveaway"
        description = g.get("description") or ""
        max_winners = int(g.get("max_winners") or 1)

        # No decorative emojis anywhere in this panel — any emoji a viewer sees
        # comes from the title/description the operator typed.
        lines = [f"# {title}"]
        if description:
            lines.append(description)

        if ended:
            if winners:
                names = "\n".join(f"**{w}**" for w in winners)
                lines.append(f"\n**Winner{'s' if len(winners) != 1 else ''}:**\n{names}")
            else:
                lines.append("\nThis giveaway has ended.")
        else:
            meta = [f"**{entry_count}** {'entry' if entry_count == 1 else 'entries'}"]
            if max_winners > 1:
                meta.append(f"**{max_winners}** winners")
            deadline = _fmt_deadline(g.get("ends_at"))
            if deadline:
                meta.append(f"ends {deadline}")
            lines.append("\n" + " · ".join(meta))

            # State the gate up front so members know before clicking, rather
            # than only discovering it from the ephemeral refusal.
            role_id = g.get("required_role_id")
            if role_id:
                lines.append(f"Requires <@&{int(role_id)}>")

        container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        # Footer: started / ended stamps as Discord timestamps, so each viewer
        # sees them in their own timezone. `-#` renders as small subtext.
        footer = []
        started = _fmt_stamp(g.get("started_at"))
        if started:
            footer.append(f"Started {started}")
        ended_stamp = _fmt_stamp(g.get("ended_at"))
        if ended and ended_stamp:
            footer.append(f"Ended {ended_stamp}")
        if footer:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.TextDisplay("-# " + "  ·  ".join(footer)))

        self.add_item(container)

        # Buttons live OUTSIDE the container (a sibling ActionRow on the view),
        # so they render below the panel body rather than inside it — the same
        # arrangement the point-shop storefront uses. Omitted once the giveaway
        # is over: the message stays as a record with nothing left to join.
        if not ended:
            self.add_item(discord.ui.ActionRow(self._JoinButton()))

    @classmethod
    def template(cls, engine):
        """Stateless instance for bot.add_view at startup.

        Carries `giveaway_enter` so Join buttons on already-posted panels
        re-bind after a restart; the callback reads all state from the
        interaction + DB, never from this instance.
        """
        return cls(engine, giveaway=None)

    class _JoinButton(discord.ui.Button):
        def __init__(self):
            # No emoji: the panel carries none unless the operator typed one.
            super().__init__(
                label="Join giveaway",
                style=discord.ButtonStyle.success,
                custom_id="giveaway_enter",
            )

        async def callback(self, interaction: discord.Interaction):
            view: "GiveawayPanelView" = self.view  # type: ignore[assignment]
            try:
                await view._handle_join(interaction)
            except Exception as e:
                logger.error(f"[giveaway] join error: {e}", exc_info=True)
                if not interaction.response.is_done():
                    await interaction.response.send_message("Something went wrong.", ephemeral=True)

    async def _handle_join(self, interaction: discord.Interaction):
        """Validate eligibility, then either commit the entry or open the modal."""
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        giveaway = _active_discord_giveaway(self.engine, guild_id)
        if not giveaway:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return

        # Deadline is enforced here as well as by the expiry loop: a click can
        # land in the gap between the deadline passing and the loop's next tick.
        ends_at = giveaway.get("ends_at")
        if ends_at:
            deadline = ends_at if ends_at.tzinfo else ends_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= deadline:
                await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
                return

        # Role gate. Checked against the member's CURRENT roles at click time,
        # so losing the role later does not retroactively matter and gaining it
        # works without re-posting the panel.
        required_role_id = giveaway.get("required_role_id")
        if required_role_id:
            member = interaction.user
            role_ids = {r.id for r in getattr(member, "roles", [])}
            if int(required_role_id) not in role_ids:
                await interaction.response.send_message(
                    f"This giveaway is limited to <@&{int(required_role_id)}> members.",
                    ephemeral=True,
                )
                return

        # Cheap pre-check so an ineligible member is told BEFORE being shown a
        # modal they would fill in for nothing. The authoritative check runs
        # again inside the write transaction.
        blocked = self._entry_block_reason(giveaway, guild_id, interaction.user.id)
        if blocked:
            await interaction.response.send_message(blocked, ephemeral=True)
            return

        prompt = (giveaway.get("entry_prompt") or "").strip()
        if prompt:
            # A modal must be the FIRST response to the interaction - it cannot
            # follow a defer or a send_message.
            await interaction.response.send_modal(GiveawayEntryModal(self, giveaway, prompt))
            return

        await self._commit_entry(interaction, giveaway, answer=None)

    def _entry_block_reason(self, giveaway, guild_id, discord_id):
        """Why this member may not enter right now, or None if they may."""
        allow_multiple = bool(giveaway.get("allow_multiple_entries"))
        max_per_user = int(giveaway.get("max_entries_per_user") or 1)
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT entry_count FROM giveaway_entries
                    WHERE giveaway_id = :gid AND discord_server_id = :sid AND discord_id = :did
                    """
                ),
                {"gid": giveaway["id"], "sid": guild_id, "did": discord_id},
            ).fetchone()
        if not row:
            return None
        if not allow_multiple:
            return "You are already entered - good luck!"
        if int(row[0]) >= max_per_user:
            return f"You have reached the maximum of {max_per_user} entries."
        return None

    async def _commit_entry(self, interaction: discord.Interaction, giveaway, answer=None):
        """Record the entry and acknowledge. Safe to call from the modal too."""
        guild_id = interaction.guild_id
        giveaway_id = giveaway["id"]
        user = interaction.user
        display = user.display_name or user.name
        allow_multiple = bool(giveaway.get("allow_multiple_entries"))
        max_per_user = int(giveaway.get("max_entries_per_user") or 1)

        with self.engine.begin() as conn:
            existing = conn.execute(
                text(
                    """
                    SELECT id, entry_count FROM giveaway_entries
                    WHERE giveaway_id = :gid AND discord_server_id = :sid AND discord_id = :did
                    """
                ),
                {"gid": giveaway_id, "sid": guild_id, "did": user.id},
            ).fetchone()

            if existing:
                # Re-checked inside the transaction: two fast clicks (or a slow
                # modal submit) can both pass the pre-check above.
                if not allow_multiple:
                    await interaction.response.send_message("You are already entered - good luck!", ephemeral=True)
                    return
                if int(existing[1]) >= max_per_user:
                    await interaction.response.send_message(
                        f"You have reached the maximum of {max_per_user} entries.", ephemeral=True
                    )
                    return
                conn.execute(
                    text(
                        """
                        UPDATE giveaway_entries
                        SET entry_count = entry_count + 1,
                            entry_answer = COALESCE(:answer, entry_answer)
                        WHERE id = :id
                        """
                    ),
                    {"id": existing[0], "answer": answer},
                )
                new_count = int(existing[1]) + 1
            else:
                # kick_username stays NULL for Discord entrants; the canonical
                # identity for the draw is COALESCE(kick_username, discord_username).
                conn.execute(
                    text(
                        """
                        INSERT INTO giveaway_entries
                          (giveaway_id, discord_server_id, discord_id, discord_username,
                           display_name, entry_method, entry_count, profile_pic_url, entry_answer)
                        VALUES (:gid, :sid, :did, :uname, :display, 'discord', 1, :pfp, :answer)
                        """
                    ),
                    {
                        "gid": giveaway_id,
                        "sid": guild_id,
                        "did": user.id,
                        "uname": user.name,
                        "display": display,
                        "pfp": str(user.display_avatar.url) if user.display_avatar else None,
                        "answer": answer,
                    },
                )
                new_count = 1

        await interaction.response.send_message(
            f"You are in! ({new_count} {'entry' if new_count == 1 else 'entries'})",
            ephemeral=True,
        )

        # Tell the dashboard console live entries stream. Imported lazily -
        # bot.py imports this package, so a module-level import would cycle.
        try:
            from bot import publish_redis_event

            publish_redis_event(
                channel="dashboard:giveaway",
                action="giveaway_entry",
                data={
                    "discord_server_id": guild_id,
                    "giveaway_id": giveaway_id,
                    "kick_username": display,
                },
            )
        except Exception as e:
            logger.debug(f"[giveaway] entry publish failed (non-fatal): {e}")

        # Refresh the entry count on the panel (debounced).
        asyncio.create_task(schedule_panel_refresh(interaction.client, self.engine, guild_id, giveaway_id))


class GiveawayEntryModal(discord.ui.Modal):
    """Collects the operator-configured text answer when a member joins.

    Holds a reference to the panel view purely to reuse its commit path; all
    giveaway state is passed in per-interaction, so nothing here is captured
    across restarts.
    """

    def __init__(self, view, giveaway, prompt):
        super().__init__(title=(giveaway.get("title") or "Giveaway")[:45])
        self._view = view
        self._giveaway = giveaway
        # Discord caps a TextInput label at 45 chars; entry_prompt is stored
        # VARCHAR(45) so this never silently truncates.
        self.answer = discord.ui.TextInput(
            label=prompt[:45],
            required=True,
            max_length=300,
            style=discord.TextStyle.short,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        value = (self.answer.value or "").strip()
        await self._view._commit_entry(interaction, self._giveaway, answer=value or None)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"[giveaway] entry modal error: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


async def schedule_panel_refresh(bot, engine, guild_id, giveaway_id):
    """Coalesced panel edit — at most one per _EDIT_DEBOUNCE_SECONDS per giveaway."""
    if giveaway_id in _pending_edits:
        return
    _pending_edits.add(giveaway_id)
    try:
        await asyncio.sleep(_EDIT_DEBOUNCE_SECONDS)
        await refresh_panel(bot, engine, guild_id, giveaway_id)
    except Exception as e:
        logger.warning(f"[giveaway] panel refresh failed: {e}")
    finally:
        _pending_edits.discard(giveaway_id)


async def _fetch_panel_message(bot, giveaway):
    channel_id = giveaway.get("discord_channel_id")
    message_id = giveaway.get("discord_message_id")
    if not channel_id or not message_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception:
            return None
    try:
        return await channel.fetch_message(int(message_id))
    except discord.NotFound:
        logger.info(f"[giveaway] panel message {message_id} is gone; not re-posting")
        return None


async def refresh_panel(bot, engine, guild_id, giveaway_id, ended=False, winners=None):
    """Re-render the panel in place from current DB state."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, title, description, status, started_at, ends_at, ended_at,
                       max_winners, discord_channel_id, discord_message_id,
                       required_role_id, entry_prompt
                FROM giveaways
                WHERE id = :gid AND discord_server_id = :sid
                """
            ),
            {"gid": giveaway_id, "sid": guild_id},
        ).fetchone()
    if not row:
        return
    giveaway = dict(row._mapping)

    message = await _fetch_panel_message(bot, giveaway)
    if message is None:
        return

    count = _entry_count(engine, giveaway_id, guild_id)
    view = GiveawayPanelView(engine, giveaway, entry_count=count, winners=winners, ended=ended)
    await message.edit(view=view)


async def post_panel(bot, engine, guild_id, giveaway):
    """Post the panel for a freshly-started giveaway and record its message id."""
    channel_id = giveaway.get("discord_channel_id")
    if not channel_id:
        logger.warning(f"[giveaway] giveaway {giveaway.get('id')} has no discord_channel_id")
        return False

    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception as e:
            logger.warning(f"[giveaway] channel {channel_id} unavailable: {e}")
            return False

    count = _entry_count(engine, giveaway["id"], guild_id)
    view = GiveawayPanelView(engine, giveaway, entry_count=count)
    message = await channel.send(view=view)

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE giveaways SET discord_message_id = :mid WHERE id = :gid"),
            {"mid": message.id, "gid": giveaway["id"]},
        )
    logger.info(f"[giveaway] posted panel for giveaway {giveaway['id']} in #{channel} ({message.id})")
    return True
