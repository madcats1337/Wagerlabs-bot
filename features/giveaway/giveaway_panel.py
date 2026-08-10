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
                SELECT id, title, description, status, ends_at, max_winners,
                       discord_channel_id, discord_message_id,
                       allow_multiple_entries, max_entries_per_user
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

        title = (giveaway or {}).get("title") or "Giveaway"
        description = (giveaway or {}).get("description") or ""
        max_winners = int((giveaway or {}).get("max_winners") or 1)

        lines = [f"# 🎁 {title}"]
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
            deadline = _fmt_deadline((giveaway or {}).get("ends_at"))
            if deadline:
                meta.append(f"ends {deadline}")
            lines.append("\n" + " · ".join(meta))

        container.add_item(discord.ui.TextDisplay("\n".join(lines)))

        # The button is omitted once the giveaway is over — the message stays as
        # a record, but there's nothing left to join.
        if not ended:
            container.add_item(discord.ui.ActionRow(self._JoinButton()))

        self.add_item(container)

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
            super().__init__(
                label="Join giveaway",
                style=discord.ButtonStyle.success,
                emoji="🎉",
                custom_id="giveaway_enter",
            )

        async def callback(self, interaction: discord.Interaction):
            view: "GiveawayPanelView" = self.view  # type: ignore[assignment]
            try:
                await view._handle_join(interaction)
            except Exception as e:
                logger.error(f"[giveaway] join error: {e}", exc_info=True)
                if not interaction.response.is_done():
                    await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)

    async def _handle_join(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("❌ This only works in a server.", ephemeral=True)
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
                if not allow_multiple:
                    await interaction.response.send_message("You're already entered — good luck! 🍀", ephemeral=True)
                    return
                if int(existing[1]) >= max_per_user:
                    await interaction.response.send_message(
                        f"You've reached the maximum of {max_per_user} entries.", ephemeral=True
                    )
                    return
                conn.execute(
                    text("UPDATE giveaway_entries SET entry_count = entry_count + 1 WHERE id = :id"),
                    {"id": existing[0]},
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
                           display_name, entry_method, entry_count, profile_pic_url)
                        VALUES (:gid, :sid, :did, :uname, :display, 'discord', 1, :pfp)
                        """
                    ),
                    {
                        "gid": giveaway_id,
                        "sid": guild_id,
                        "did": user.id,
                        "uname": user.name,
                        "display": display,
                        "pfp": str(user.display_avatar.url) if user.display_avatar else None,
                    },
                )
                new_count = 1

        await interaction.response.send_message(
            f"You're in! 🎉 ({new_count} {'entry' if new_count == 1 else 'entries'})",
            ephemeral=True,
        )

        # Tell the dashboard console's live entries stream. Imported lazily —
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
                SELECT id, title, description, status, ends_at, max_winners,
                       discord_channel_id, discord_message_id
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
