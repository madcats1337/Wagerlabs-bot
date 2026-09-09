"""Guess the Balance session announcement (Components V2).

Posted to the channel chosen on the dashboard's Guess the Balance page
(`gtb_channel_id`) when a session opens, and edited in place as the session
closes and is scored. It carries a Submit Guess button, so a member whose
Discord account is LINKED can guess without leaving Discord.

Distinct from `gtb_panel.py`, which is the admin CONTROL panel (open/close/set
result, administrator-only). This one is the member-facing announcement.

Persistence model (the part that matters):
  * `GtbNotificationView.template()` is registered once via `bot.add_view()` in
    on_ready. It carries the `gtb_submit_guess` custom_id, so buttons on
    announcements posted BEFORE a restart re-bind their handler afterwards.
  * The callback is therefore stateless: it resolves the guild, the open
    session and the member's linked handle from `interaction` + the DB on every
    click. Nothing session-specific may be captured in the view, because the
    template instance has no session.

Rendered with Components V2 — a LayoutView only, no embed and no top-level
content (a V2 message cannot carry an embed).
"""

import logging

import discord
from sqlalchemy import text

from features.games.guess_the_balance import gtb_rank_marker, parse_amount

logger = logging.getLogger(__name__)

ACCENT = 0xFACC15  # Wagerlabs yellow

#: bot_settings key holding the message id of the announcement for a session,
#: so close/result can edit the same message instead of posting a new one.
_MESSAGE_KEY = "gtb_notification_message_id"
_CHANNEL_KEY = "gtb_notification_channel_id"


# ---------------------------------------------------------------------------
# Settings / lookup helpers
# ---------------------------------------------------------------------------
def _get_setting(engine, guild_id, key):
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM bot_settings WHERE key = :k AND discord_server_id = :sid"),
                {"k": key, "sid": int(guild_id)},
            ).fetchone()
        return (row[0] or "").strip() if row else ""
    except Exception as e:
        logger.warning(f"[gtb-notify] could not read {key} for {guild_id}: {e}")
        return ""


def _set_setting(engine, guild_id, key, value):
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                    VALUES (:k, :v, :sid, CURRENT_TIMESTAMP)
                    ON CONFLICT (key, discord_server_id)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {"k": key, "v": str(value), "sid": int(guild_id)},
            )
    except Exception as e:
        logger.warning(f"[gtb-notify] could not save {key} for {guild_id}: {e}")


def notification_channel_id(engine, guild_id):
    """The channel the dashboard bound notifications to, or None.

    Read FRESH from `bot_settings` on every announcement rather than through a
    cached settings manager: picking a channel on the dashboard must take
    effect on the very next session, not after a bot restart.

    Deliberately NO fallback to `slot_calls_channel_id`. `BotSettingsManager.
    gtb_channel_id` does fall back, which is right for the legacy plain-text
    line, but this panel is opt-in — an operator who has not chosen a channel
    should not have an interactive announcement appear in their slot-calls
    channel.
    """
    raw = _get_setting(engine, guild_id, "gtb_channel_id")
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(f"[gtb-notify] gtb_channel_id for {guild_id} is not an id: {raw!r}")
        return None


async def _resolve_channel(bot, channel_id):
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception as e:
            logger.warning(f"[gtb-notify] channel {channel_id} unavailable: {e}")
            return None
    return channel


def linked_handle(engine, guild_id, discord_id):
    """The handle this member's GTB guesses are credited to, or None.

    Same ordering as `core.stream_links.resolve_canonical_identity` — prefer the
    Kick link, then the earliest one — because `gtb_guesses` is keyed by that
    handle. Resolving it any other way would give a member with both a Kick and
    a Twitch link a SECOND guess row alongside the one their `!gtb` in chat
    writes.

    Returning None is what makes this feature linked-members-only: there is no
    handle to credit an unlinked member's guess to.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT LOWER(kick_name) FROM links
                    WHERE discord_id = :d AND discord_server_id = :sid AND kick_name IS NOT NULL
                    ORDER BY (platform <> 'kick'), linked_at ASC
                    LIMIT 1
                    """
                ),
                {"d": int(discord_id), "sid": int(guild_id)},
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        # Fail CLOSED. A DB blip here must not let an unlinked member through —
        # the whole point of the gate is that a guess maps to a real viewer.
        logger.error(f"[gtb-notify] link lookup failed for {discord_id} in {guild_id}: {e}")
        return None


def _session_snapshot(engine, guild_id, session_id=None):
    """The session to render, with its live guess count.

    Without `session_id` this picks the most recent non-deleted session for the
    guild, which is what a click on an older announcement should act on.
    """
    try:
        with engine.connect() as conn:
            if session_id is not None:
                row = conn.execute(
                    text(
                        """
                        SELECT id, opened_by, opened_at, closed_at, status, winner_count, result_amount
                        FROM gtb_sessions
                        WHERE id = :sid AND discord_server_id = :gid
                        """
                    ),
                    {"sid": int(session_id), "gid": int(guild_id)},
                ).fetchone()
            else:
                row = conn.execute(
                    text(
                        """
                        SELECT id, opened_by, opened_at, closed_at, status, winner_count, result_amount
                        FROM gtb_sessions
                        WHERE discord_server_id = :gid
                        ORDER BY opened_at DESC
                        LIMIT 1
                        """
                    ),
                    {"gid": int(guild_id)},
                ).fetchone()
            if not row:
                return None
            session = {
                "id": row[0],
                "opened_by": row[1],
                "opened_at": row[2],
                "closed_at": row[3],
                "status": row[4],
                "winner_count": row[5] or 3,
                "result_amount": row[6],
            }
            session["guess_count"] = (
                conn.execute(
                    text("SELECT COUNT(*) FROM gtb_guesses WHERE session_id = :sid"),
                    {"sid": session["id"]},
                ).fetchone()[0]
                or 0
            )
            session["winners"] = [
                {
                    "rank": w[0],
                    "username": w[1],
                    "guess": float(w[2]),
                    "difference": float(w[3]),
                }
                for w in conn.execute(
                    text(
                        """
                        SELECT rank, COALESCE(display_name, kick_username), guess_amount, difference
                        FROM gtb_winners
                        WHERE session_id = :sid
                        ORDER BY rank ASC
                        """
                    ),
                    {"sid": session["id"]},
                ).fetchall()
            ]
            return session
    except Exception as e:
        logger.error(f"[gtb-notify] session snapshot failed for {guild_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# The panel
# ---------------------------------------------------------------------------
class GtbNotificationView(discord.ui.LayoutView):
    """Session announcement: status, guess count, and a Submit Guess button.

    `session=None` builds the stateless TEMPLATE registered at startup — it
    still carries the button (and therefore its custom_id) so announcements
    posted before a restart keep working.
    """

    def __init__(self, engine, session=None):
        super().__init__(timeout=None)
        self.engine = engine

        container = discord.ui.Container(accent_colour=ACCENT)
        s = session or {}
        status = s.get("status") or "open"

        lines = ["# Guess the Balance"]

        if status == "completed":
            result = s.get("result_amount")
            lines.append(f"Session #{s.get('id')} is over. **Final balance: ${float(result or 0):,.2f}**")
            winners = s.get("winners") or []
            if winners:
                lines.append("")
                for w in winners:
                    lines.append(
                        f"{gtb_rank_marker(w['rank'])} **{w['username']}** — "
                        f"${w['guess']:,.2f} (off by ${w['difference']:,.2f})"
                    )
        elif status == "closed":
            lines.append(f"Session #{s.get('id')} is **closed**. No more guesses — " "waiting on the final balance.")
            lines.append(
                f"\n**{s.get('guess_count', 0)}** " f"{'guess' if s.get('guess_count') == 1 else 'guesses'} in."
            )
        else:
            winner_count = int(s.get("winner_count") or 3)
            lines.append(
                f"A new session is **open**. Guess what the final balance will be — "
                f"the closest {winner_count} {'guess wins' if winner_count == 1 else 'guesses win'}."
            )
            count = s.get("guess_count", 0)
            lines.append(f"\n**{count}** {'guess' if count == 1 else 'guesses'} so far.")
            lines.append(
                "-# Press Submit Guess below, or type `!gtb <amount>` in chat. " "Your Discord account must be linked."
            )

        container.add_item(discord.ui.TextDisplay("\n".join(lines)))
        self.add_item(container)

        # Button lives OUTSIDE the container (a sibling ActionRow on the view),
        # so it renders below the panel body — the same arrangement the
        # giveaway panel and point-shop storefront use. Dropped once the
        # session stops accepting guesses: the message stays as a record.
        if status == "open" or session is None:
            self.add_item(discord.ui.ActionRow(self._SubmitButton()))

    @classmethod
    def template(cls, engine):
        """Stateless instance for `bot.add_view` at startup.

        Carries `gtb_submit_guess` so buttons on already-posted announcements
        re-bind after a restart; the callback reads all state from the
        interaction + DB, never from this instance.
        """
        return cls(engine, session=None)

    class _SubmitButton(discord.ui.Button):
        def __init__(self):
            super().__init__(
                label="Submit Guess",
                style=discord.ButtonStyle.success,
                custom_id="gtb_submit_guess",
            )

        async def callback(self, interaction: discord.Interaction):
            view: "GtbNotificationView" = self.view  # type: ignore[assignment]
            try:
                await view._handle_submit(interaction)
            except Exception as e:
                logger.error(f"[gtb-notify] submit error: {e}", exc_info=True)
                if not interaction.response.is_done():
                    await interaction.response.send_message("Something went wrong.", ephemeral=True)

    async def _handle_submit(self, interaction: discord.Interaction):
        """Check the gates, then open the guess modal.

        Everything is validated BEFORE the modal so nobody types an amount into
        a popup that was never going to be accepted. The session and the link
        are checked AGAIN on submit, because a modal can sit open for minutes.
        """
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        session = _session_snapshot(self.engine, guild_id)
        if not session or session["status"] != "open":
            await interaction.response.send_message("This session is no longer accepting guesses.", ephemeral=True)
            return

        handle = linked_handle(self.engine, guild_id, interaction.user.id)
        if not handle:
            await interaction.response.send_message(
                "Link your Kick or Twitch account first — use the Link Account panel in this "
                "server. Guesses are credited to your linked account.",
                ephemeral=True,
            )
            return

        # A modal must be the FIRST response to an interaction — it cannot
        # follow a defer or a send_message.
        await interaction.response.send_modal(GtbGuessModal(self.engine, session, handle))


class GtbGuessModal(discord.ui.Modal, title="Guess the Balance"):
    """Collects the amount and records it against the open session.

    Constructed per-click with the session and handle already resolved, so it
    never has to be persistent — a modal only exists between the click that
    opened it and its submit.
    """

    guess_amount = discord.ui.TextInput(
        label="Your guess",
        placeholder="e.g. 1234.56",
        required=True,
        min_length=1,
        max_length=20,
    )

    def __init__(self, engine, session, handle):
        super().__init__()
        self.engine = engine
        self.session = session
        self.handle = handle

    async def on_submit(self, interaction: discord.Interaction):
        amount = parse_amount(self.guess_amount.value)
        if amount is None:
            await interaction.response.send_message(
                "That is not a valid amount. Enter a positive number, e.g. `1234.56`.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild_id

        # Re-check both gates: the modal may have been open for minutes, and
        # the session could have closed or the member unlinked meanwhile. The
        # write below is what actually has to be correct, not the pre-check.
        session = _session_snapshot(self.engine, guild_id, self.session["id"])
        if not session or session["status"] != "open":
            await interaction.response.send_message("This session closed before your guess landed.", ephemeral=True)
            return

        handle = linked_handle(self.engine, guild_id, interaction.user.id)
        if not handle:
            await interaction.response.send_message(
                "Your account is no longer linked, so the guess could not be credited.",
                ephemeral=True,
            )
            return

        gtb_mgr = None
        managers = getattr(interaction.client, "gtb_managers_by_guild", None)
        if managers:
            gtb_mgr = managers.get(guild_id)
        if not gtb_mgr:
            logger.error(f"[gtb-notify] no GTB manager for guild {guild_id}")
            await interaction.response.send_message("Guess the Balance is not available right now.", ephemeral=True)
            return

        # Goes through the SAME manager the `!gtb` chat command uses, so the
        # upsert, the dedup key and the Redis event that refreshes the OBS
        # widget are all identical to a guess made in chat.
        success, message = gtb_mgr.add_guess(handle, amount, display_name=interaction.user.display_name)

        if success:
            await interaction.response.send_message(
                f"Guess recorded: **${amount:,.2f}** (as `{handle}`). Good luck.", ephemeral=True
            )
            # Reflect the new count on the announcement itself.
            await refresh_notification(interaction.client, self.engine, guild_id, session["id"])
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"[gtb-notify] guess modal error: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ---------------------------------------------------------------------------
# Posting / refreshing
# ---------------------------------------------------------------------------
async def post_notification(bot, engine, guild_id, session_id):
    """Announce a newly opened session in the configured channel.

    A no-op (returning False) when no channel is bound — notifications are
    opt-in, and a server that has not chosen one simply keeps announcing in
    stream chat as before.
    """
    channel_id = notification_channel_id(engine, guild_id)
    if not channel_id:
        logger.debug(f"[gtb-notify] no channel bound for guild {guild_id}; skipping")
        return False

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return False

    session = _session_snapshot(engine, guild_id, session_id)
    if not session:
        logger.warning(f"[gtb-notify] session {session_id} not found for guild {guild_id}")
        return False

    try:
        message = await channel.send(view=GtbNotificationView(engine, session))
    except discord.Forbidden:
        logger.warning(f"[gtb-notify] missing permission to post in {channel_id} (guild {guild_id})")
        return False
    except Exception as e:
        logger.error(f"[gtb-notify] failed to post announcement for guild {guild_id}: {e}")
        return False

    # Remember where it landed so close/result edit this message rather than
    # posting a second one. Stored per guild: only one session is ever active.
    _set_setting(engine, guild_id, _MESSAGE_KEY, message.id)
    _set_setting(engine, guild_id, _CHANNEL_KEY, channel.id)
    logger.info(f"[gtb-notify] announced session #{session_id} in #{channel} ({message.id})")
    return True


async def refresh_notification(bot, engine, guild_id, session_id=None):
    """Re-render the announcement in place (guess count, status, winners).

    Called on the events that MATTER — session closed, result set, and a guess
    submitted from the button itself. Deliberately NOT on every incoming guess:
    guesses from stream chat and from the public API arrive on
    `gtb:guess:events`, and editing the message once per guess would spend a
    Discord edit per chat message and hit the per-channel rate limit during a
    busy session. The count is cosmetic, so it catches up on the next event.

    Silently does nothing when there is no announcement to edit — the feature
    is opt-in, and a missing message is the normal state for a server that has
    never bound a channel.
    """
    message_id = _get_setting(engine, guild_id, _MESSAGE_KEY)
    channel_id = _get_setting(engine, guild_id, _CHANNEL_KEY)
    if not message_id or not channel_id:
        return False

    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return False

    session = _session_snapshot(engine, guild_id, session_id)
    if not session:
        return False

    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(view=GtbNotificationView(engine, session))
        return True
    except discord.NotFound:
        # Deleted by hand. Forget it so we stop trying on every event.
        logger.info(f"[gtb-notify] announcement {message_id} is gone; clearing")
        _set_setting(engine, guild_id, _MESSAGE_KEY, "")
        return False
    except Exception as e:
        logger.warning(f"[gtb-notify] could not refresh announcement for {guild_id}: {e}")
        return False
