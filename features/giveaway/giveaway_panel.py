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
import json
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


# How long a captcha pass is remembered for a (server, member) pair. Applied at
# READ time so no cleanup job is needed.
_VERIFY_VALID_DAYS = 30
# The one-time verify link. Short enough that a shared link is useless, long
# enough to solve a challenge on a phone.
_VERIFY_TOKEN_TTL_SECONDS = 10 * 60


# Last successfully-read policy per guild. A DB blip must not silently disable
# the gates — that is precisely the alt flood the feature exists to stop — but
# failing fully CLOSED would block every legitimate entrant instead. Replaying
# the last known policy keeps enforcing what the operator configured, and only
# a guild whose rules were never read falls back to "no gates".
_rules_cache: dict[int, dict] = {}


def _entry_rules(engine, guild_id):
    """The server's giveaway entry rules, read FRESH from bot_settings.

    Server-wide policy, not per-giveaway: every Discord-hosted giveaway in a
    guild enforces the same gates, configured on the dashboard's Giveaway
    Management -> Entry rules tab.

    Read straight from the DB rather than through BotSettingsManager, which
    caches — a dashboard change must take effect on the very next Join click,
    not after a bot restart. The cache below is a FAILURE fallback only; the
    happy path always hits the DB.

    Returns {min_account_age_days, min_server_days, require_captcha}.
    """
    rules = {"min_account_age_days": 0, "min_server_days": 0, "require_captcha": False}
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT key, value FROM bot_settings
                    WHERE discord_server_id = :sid
                      AND key IN ('giveaway_min_account_age_days',
                                  'giveaway_min_server_days',
                                  'giveaway_require_captcha')
                    """
                ),
                # bot_settings.discord_server_id is BIGINT — bind an int, the
                # same way the dashboard writes it and BotSettingsManager reads
                # it. A string here relies on driver-side coercion.
                {"sid": int(guild_id)},
            ).fetchall()
    except Exception as e:
        cached = _rules_cache.get(int(guild_id))
        logger.error(
            f"[giveaway] entry-rules lookup failed for {guild_id}: {e} — "
            f"{'replaying last known rules' if cached else 'no cached rules, gates off'}"
        )
        return dict(cached) if cached else rules

    for key, value in rows:
        raw = (value or "").strip()
        if key == "giveaway_require_captcha":
            rules["require_captcha"] = raw.lower() in ("true", "1", "yes", "on")
        else:
            field = "min_account_age_days" if key.endswith("account_age_days") else "min_server_days"
            try:
                rules[field] = max(0, int(raw))
            except (TypeError, ValueError):
                pass  # keep the 0 default; a malformed value must not gate entry

    _rules_cache[int(guild_id)] = dict(rules)
    return rules


def _captcha_configured() -> bool:
    """Are the Turnstile keys present?

    The dashboard serves the challenge page, so both services need the keys in
    their environment. When they are missing the captcha gate must be SKIPPED —
    handing a member a link that can never validate would silently take a
    giveaway to zero entries.
    """
    import os

    return bool(os.getenv("TURNSTILE_SITE_KEY") and os.getenv("TURNSTILE_SECRET_KEY"))


def _is_verified(engine, guild_id, giveaway_id, discord_id) -> bool:
    """Has this member passed the captcha for this giveaway recently?

    Scoped per giveaway on purpose: one solved challenge must not buy entry
    across every giveaway. Fails OPEN on a DB error — a verification lookup
    failing must not block a giveaway.
    """
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT 1 FROM giveaway_verified_members
                    WHERE discord_server_id = :sid AND giveaway_id = :gid AND discord_id = :did
                      AND verified_at > CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')
                    """
                ),
                {"sid": int(guild_id), "gid": int(giveaway_id), "did": int(discord_id), "days": _VERIFY_VALID_DAYS},
            ).fetchone()
        return row is not None
    except Exception as e:
        logger.error(f"[giveaway] verify lookup failed for {discord_id}: {e}")
        return True


def _issue_verify_link(engine, guild_id, giveaway_id, discord_id, interaction_token=None):
    """Mint a one-time verify URL, or None if one can't be built.

    The Discord id is stored SERVER-SIDE against an opaque token and never
    appears in the URL, so a shared link cannot verify someone else — it
    always writes the row for whoever the token was minted for.
    """
    import os
    import secrets

    import redis as _redis

    from utils.server_urls import get_server_public_page_url

    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        logger.error("[giveaway] REDIS_URL not configured; cannot issue verify link")
        return None
    if "://" not in redis_url:
        redis_url = f"redis://{redis_url}"

    token = secrets.token_urlsafe(32)
    client = None
    try:
        client = _redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        payload = {
            "discord_server_id": str(guild_id),
            "giveaway_id": int(giveaway_id),
            "discord_id": str(discord_id),
        }
        if interaction_token:
            payload["interaction_token"] = interaction_token

        client.setex(
            f"giveaway_verify:{token}",
            _VERIFY_TOKEN_TTL_SECONDS,
            json.dumps(payload, separators=(",", ":")),
        )
    except Exception as e:
        logger.error(f"[giveaway] could not store verify token: {e}")
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    # Resolves subdomain servers, free-tier (?server=slug) and the generic
    # apex fallback — always returns a usable URL.
    return f"https://verify.wagerlabs.app/verify/{token}"


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


def winner_label(winner) -> str:
    """How a winner should appear in a Discord announcement.

    Prefers a real <@id> mention (which pings them and renders their current
    name) and falls back to the stored display name for chat-only entrants who
    have no Discord id.
    """
    if isinstance(winner, dict):
        discord_id = winner.get("discord_id")
        name = (
            winner.get("display")
            or winner.get("winner")
            or winner.get("kick_username")
            or winner.get("display_name")
            or "Unknown"
        )
    else:
        discord_id, name = None, str(winner)
    if discord_id:
        try:
            return f"<@{int(discord_id)}>"
        except (TypeError, ValueError):
            pass
    return f"**{name}**"


def is_discord_hosted(engine, giveaway_id) -> bool:
    """Whether this giveaway is entered through the Discord panel.

    Discord-hosted giveaways have no relationship to stream chat — the entrants
    are Discord members and the panel lives in a Discord channel — so their
    winners must NOT be announced on Kick/Twitch. Chat-based giveaways
    (keyword / active_chatter) still announce there.

    Fails CLOSED (returns False) when the row can't be read, so an outage
    degrades to the old behaviour rather than silently dropping announcements
    for chat giveaways.
    """
    if giveaway_id is None:
        return False
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT entry_method FROM giveaways WHERE id = :gid"),
                {"gid": giveaway_id},
            ).fetchone()
        return bool(row) and (row[0] or "") == "discord"
    except Exception as e:
        logger.debug(f"[giveaway] entry_method lookup failed: {e}")
        return False


async def resolve_announce_channel(bot, engine, guild_id, giveaway_id=None):
    """Where a giveaway's winners should be announced.

    The giveaway's OWN channel first — the one chosen when it was created — so
    the announcement lands where the panel and the entrants are. Falls back to
    the guild's raffle announcement channel only when the giveaway has none
    (chat-only giveaways never set one).
    """
    channel_id = None
    if giveaway_id is not None:
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT discord_channel_id FROM giveaways WHERE id = :gid"),
                    {"gid": giveaway_id},
                ).fetchone()
            if row and row[0]:
                channel_id = int(row[0])
        except Exception as e:
            logger.debug(f"[giveaway] could not read giveaway channel: {e}")

    if not channel_id:
        try:
            get_guild_settings = getattr(bot, "get_guild_settings", None)
            settings = get_guild_settings(guild_id) if get_guild_settings else None
            raw = getattr(settings, "raffle_announcement_channel_id", None) if settings else None
            channel_id = int(raw) if raw else None
        except Exception as e:
            logger.debug(f"[giveaway] could not read raffle channel: {e}")

    if not channel_id:
        return None

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    return channel


def _tickets_per_join(giveaway, member) -> int:
    """How many tickets one join is worth for this member.

    A role bonus is a WEIGHTING, not an extra click: `max_entries_per_user` caps
    how many times someone may enter, and each entry is then worth 1 + bonus
    tickets. (Capping the ticket total instead would make bonuses a no-op in the
    usual setup, where multiple entries are off and the cap is 1.)

    When several configured roles match, the HIGHEST bonus wins rather than
    stacking, so overlapping roles stay predictable.
    """
    bonus_roles = giveaway.get("bonus_roles")
    if not isinstance(bonus_roles, dict) or not hasattr(member, "roles"):
        return 1
    member_role_ids = {str(r.id) for r in member.roles}
    extra = 0
    for role_id, bonus in bonus_roles.items():
        if str(role_id) in member_role_ids:
            try:
                extra = max(extra, int(bonus))
            except (TypeError, ValueError):
                continue
    return 1 + max(0, extra)


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


def _active_discord_giveaway(engine, guild_id, message_id=None, giveaway_id=None):
    """The guild's live Discord-hosted giveaway, or None.

    Read fresh on every interaction — the persistent view is shared across all
    guilds and all giveaways, so this is the only source of truth.
    If message_id is provided, accurately finds the specific giveaway.
    If giveaway_id is provided, looks up by ID directly.
    """
    with engine.connect() as conn:
        if giveaway_id:
            row = conn.execute(
                text(
                    """
                    SELECT id, title, description, status, started_at, ends_at, ended_at,
                           max_winners, discord_channel_id, discord_message_id,
                           allow_multiple_entries, max_entries_per_user,
                           required_role_id, entry_prompt, bonus_roles
                    FROM giveaways
                    WHERE discord_server_id = :sid
                      AND id = :gid
                      AND entry_method = 'discord'
                      AND status = 'active'
                    LIMIT 1
                    """
                ),
                {"sid": guild_id, "gid": giveaway_id},
            ).fetchone()
        elif message_id:
            row = conn.execute(
                text(
                    """
                    SELECT id, title, description, status, started_at, ends_at, ended_at,
                           max_winners, discord_channel_id, discord_message_id,
                           allow_multiple_entries, max_entries_per_user,
                           required_role_id, entry_prompt, bonus_roles
                    FROM giveaways
                    WHERE discord_server_id = :sid
                      AND discord_message_id = :mid
                      AND entry_method = 'discord'
                      AND status = 'active'
                    LIMIT 1
                    """
                ),
                {"sid": guild_id, "mid": str(message_id)},
            ).fetchone()
        else:
            row = conn.execute(
                text(
                    """
                    SELECT id, title, description, status, started_at, ends_at, ended_at,
                           max_winners, discord_channel_id, discord_message_id,
                           allow_multiple_entries, max_entries_per_user,
                           required_role_id, entry_prompt, bonus_roles
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
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ This only works in a server.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        giveaway = _active_discord_giveaway(
            self.engine, guild_id, interaction.message.id if interaction.message else None
        )
        if not giveaway:
            await interaction.response.send_message(
                embed=discord.Embed(description="❌ This giveaway has ended.", color=discord.Color.red()),
                ephemeral=True,
            )
            return

        # Deadline is enforced here as well as by the expiry loop: a click can
        # land in the gap between the deadline passing and the loop's next tick.
        ends_at = giveaway.get("ends_at")
        if ends_at:
            deadline = ends_at if ends_at.tzinfo else ends_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= deadline:
                await interaction.response.send_message(
                    embed=discord.Embed(description="❌ This giveaway has ended.", color=discord.Color.red()),
                    ephemeral=True,
                )
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
                    embed=discord.Embed(
                        description=f"❌ This giveaway is limited to <@&{int(required_role_id)}> members.",
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return

        # Alt/bot gates. Ordered cheapest-first and placed before the modal so
        # nobody fills in an entry prompt only to be rejected afterwards.
        # All three are optional; NULL/False means "no requirement".
        blocked_msg, verify_url = await self._alt_gate_reason(giveaway, interaction)
        if blocked_msg:
            embed = discord.Embed(
                description=blocked_msg, color=discord.Color.orange() if verify_url else discord.Color.red()
            )
            if verify_url:
                view = discord.ui.View()
                view.add_item(discord.ui.Button(style=discord.ButtonStyle.link, url=verify_url, label="Verify"))
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Cheap pre-check so an ineligible member is told BEFORE being shown a
        # modal they would fill in for nothing. The authoritative check runs
        # again inside the write transaction.
        blocked = self._entry_block_reason(
            giveaway, guild_id, interaction.user.id, per_join=_tickets_per_join(giveaway, interaction.user)
        )
        if blocked:
            await interaction.response.send_message(
                embed=discord.Embed(description=f"❌ {blocked}", color=discord.Color.red()), ephemeral=True
            )
            return

        prompt = (giveaway.get("entry_prompt") or "").strip()
        if prompt:
            # A modal must be the FIRST response to the interaction - it cannot
            # follow a defer or a send_message.
            await interaction.response.send_modal(GiveawayEntryModal(self, giveaway, prompt))
            return

        await self._commit_entry(interaction, giveaway, answer=None)

    async def _alt_gate_reason(self, giveaway, interaction):
        """Why this member fails the alt/bot gates right now, or None if they pass.
        Returns a tuple of (message, verify_url). If blocked but no verify URL, verify_url is None.
        If passed, returns (None, None).

        Three optional gates, evaluated against the member's state at click time:
        account age, server tenure, and a one-time captcha. Each returns its own
        specific message naming the member's actual value — a bare "you cannot
        enter" just generates support tickets.

        The rules are SERVER-WIDE (bot_settings), not per-giveaway, and are read
        fresh on every click so a dashboard change applies immediately.
        """
        member = interaction.user
        guild_id = interaction.guild_id
        now = datetime.now(timezone.utc)

        rules = _entry_rules(self.engine, guild_id)

        # Account age. `created_at` is decoded from the snowflake, so this needs
        # no API call and no privileged intent.
        min_age = rules["min_account_age_days"]
        if min_age:
            age_days = (now - member.created_at).days
            if age_days < int(min_age):
                return (
                    f"Your Discord account must be at least {int(min_age)} days old to enter "
                    f"this giveaway. Yours is {age_days} {'day' if age_days == 1 else 'days'} old.",
                    None,
                )

        # Server tenure. `joined_at` can be absent on an uncached member, so
        # fetch once. If it still can't be resolved we fail OPEN and log —
        # failing closed on a Discord API hiccup would block every entrant.
        min_tenure = rules["min_server_days"]
        if min_tenure:
            joined = getattr(member, "joined_at", None)
            if joined is None and interaction.guild is not None:
                try:
                    member = await interaction.guild.fetch_member(member.id)
                    joined = member.joined_at
                except (discord.HTTPException, AttributeError) as e:
                    logger.warning(f"[giveaway] joined_at fetch failed for {member.id}: {e}")
            if joined is None:
                logger.warning(f"[giveaway] no joined_at for {member.id}; tenure gate skipped")
            else:
                tenure_days = (now - joined).days
                if tenure_days < int(min_tenure):
                    return (
                        f"You must have been in this server for at least {int(min_tenure)} days "
                        f"to enter. You have been here {tenure_days} "
                        f"{'day' if tenure_days == 1 else 'days'}.",
                        None,
                    )

        # Captcha. Runs last so only members who already passed the cheap gates
        # pay the click-out cost. Verification is remembered per (server, giveaway, member),
        # so this is a one-time step rather than a per-entry one.
        if rules["require_captcha"] and not _is_verified(self.engine, guild_id, giveaway["id"], member.id):
            if not _captcha_configured():
                # Keys missing on the dashboard side: degrade to the age/tenure
                # gates rather than handing out a link nobody can ever pass.
                logger.error("[giveaway] TURNSTILE_SITE_KEY/SECRET_KEY unset; captcha gate skipped")
                return None, None
            url = _issue_verify_link(
                self.engine, guild_id, giveaway["id"], member.id, interaction_token=interaction.token
            )
            if not url:
                # Redis unavailable or no link could be built. Fail OPEN: a
                # broken verification service must not silently kill a giveaway.
                logger.error(f"[giveaway] could not issue verify link for {member.id}; allowing entry")
                return None, None
            return (
                "One quick check before you enter — it takes a few seconds, and you only "
                "need to do it once for this server. Please click the Verify button below to complete the captcha.",
                url,
            )

        return None, None

    def _entry_block_reason(self, giveaway, guild_id, discord_id, per_join=1):
        """Why this member may not enter right now, or None if they may.

        `per_join` is how many tickets one join is worth for THIS member (1 plus
        any role bonus), so the stored ticket count can be converted back into a
        join count for the cap check.
        """
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
        joins_so_far = -(-int(row[0]) // max(1, per_join))  # ceil division
        if joins_so_far >= max_per_user:
            return f"You have reached the maximum of {max_per_user} entries."
        return None

    def _do_commit_entry(self, guild_id, user, giveaway, answer=None):
        giveaway_id = giveaway["id"]
        display = user.display_name or user.name
        allow_multiple = bool(giveaway.get("allow_multiple_entries"))
        max_per_user = int(giveaway.get("max_entries_per_user") or 1)

        entries_to_add = _tickets_per_join(giveaway, user)

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
                    return False, "You are already entered - good luck!"
                per_join = max(1, entries_to_add)
                joins_so_far = -(-int(existing[1]) // per_join)
                if joins_so_far >= max_per_user:
                    return False, f"You have reached the maximum of {max_per_user} entries."
                conn.execute(
                    text(
                        """
                        UPDATE giveaway_entries
                        SET entry_count = entry_count + :entries_to_add,
                            entry_answer = COALESCE(:answer, entry_answer)
                        WHERE id = :id
                        """
                    ),
                    {"id": existing[0], "answer": answer, "entries_to_add": entries_to_add},
                )
                new_count = int(existing[1]) + entries_to_add
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO giveaway_entries
                          (giveaway_id, discord_server_id, discord_id, discord_username,
                           display_name, entry_method, entry_count, profile_pic_url, entry_answer)
                        VALUES (:gid, :sid, :did, :uname, :display, 'discord', :entries_to_add, :pfp, :answer)
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
                        "entries_to_add": entries_to_add,
                    },
                )
                new_count = entries_to_add

        msg = f"✅ You are in **{giveaway.get('title', 'Giveaway')}**! ({new_count} {'entry' if new_count == 1 else 'entries'})"

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

        return True, msg

    async def _commit_entry(self, interaction: discord.Interaction, giveaway, answer=None):
        """Record the entry and acknowledge. Safe to call from the modal too."""
        guild_id = interaction.guild_id
        user = interaction.user

        success, msg = self._do_commit_entry(guild_id, user, giveaway, answer)

        await interaction.response.send_message(msg, ephemeral=True)

        if success:
            asyncio.create_task(schedule_panel_refresh(interaction.client, self.engine, guild_id, giveaway["id"]))


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
                       required_role_id, entry_prompt, bonus_roles
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


async def process_giveaway_verification(bot, engine, payload):
    """Handle a completed captcha from the dashboard and enter the user."""
    guild_id = int(payload.get("discord_server_id", 0))
    giveaway_id = int(payload.get("giveaway_id", 0))
    discord_id = int(payload.get("discord_id", 0))
    interaction_token = payload.get("interaction_token")

    if not guild_id or not giveaway_id or not discord_id or not interaction_token:
        logger.error("[giveaway] missing required fields in verification payload")
        return

    try:
        guild = bot.get_guild(guild_id)
        if not guild:
            guild = await bot.fetch_guild(guild_id)

        member = guild.get_member(discord_id)
        if not member:
            member = await guild.fetch_member(discord_id)

        giveaway = _active_discord_giveaway(engine, guild_id, None, giveaway_id=giveaway_id)
        if not giveaway:
            await _edit_webhook_message(bot, interaction_token, "❌ This giveaway has ended.")
            return

        # Check deadline
        ends_at = giveaway.get("ends_at")
        if ends_at:
            deadline = ends_at if ends_at.tzinfo else ends_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= deadline:
                await _edit_webhook_message(bot, interaction_token, "❌ This giveaway has ended.")
                return

        # Check role gate
        required_role_id = giveaway.get("required_role_id")
        if required_role_id:
            role_ids = {r.id for r in getattr(member, "roles", [])}
            if int(required_role_id) not in role_ids:
                await _edit_webhook_message(
                    bot, interaction_token, f"❌ This giveaway is limited to <@&{int(required_role_id)}> members."
                )
                return

        # Mock an interaction to use the existing gates
        class MockInteraction:
            def __init__(self, guild_id, user, guild):
                self.guild_id = guild_id
                self.user = user
                self.guild = guild
                self.token = interaction_token

        mock_interaction = MockInteraction(guild_id, member, guild)
        view = GiveawayPanelView(engine)

        # Check alt gates (skipping captcha since we just verified it)
        blocked_msg, _ = await view._alt_gate_reason(giveaway, mock_interaction)
        if blocked_msg:
            # If we got a blocked message that IS NOT the captcha request
            # Note: _is_verified will now return True for this user, so captcha gate is skipped
            await _edit_webhook_message(bot, interaction_token, f"❌ {blocked_msg}")
            return

        blocked = view._entry_block_reason(giveaway, guild_id, member.id, per_join=_tickets_per_join(giveaway, member))
        if blocked:
            await _edit_webhook_message(bot, interaction_token, f"❌ {blocked}")
            return

        prompt = (giveaway.get("entry_prompt") or "").strip()
        if prompt:
            # Modals can't be triggered after the fact via webhook edits.
            # We must instruct them to click Join again.
            await _edit_webhook_message(
                bot,
                interaction_token,
                "✅ Verification complete. Please press **Join giveaway** again to answer the entry prompt.",
            )
            return

        success, msg = view._do_commit_entry(guild_id, member, giveaway)
        await _edit_webhook_message(bot, interaction_token, msg)

        if success:
            asyncio.create_task(schedule_panel_refresh(bot, engine, guild_id, giveaway_id))

    except Exception as e:
        logger.error(f"[giveaway] verification process failed: {e}", exc_info=True)
        await _edit_webhook_message(
            bot, interaction_token, "❌ Something went wrong while entering you into the giveaway."
        )


async def _edit_webhook_message(bot, interaction_token, content):
    """Helper to edit the original ephemeral interaction response."""
    try:
        webhook = discord.Webhook.partial(bot.user.id, interaction_token, client=bot)

        if isinstance(content, str):
            embed = discord.Embed(
                description=content, color=discord.Color.red() if "❌" in content else discord.Color.green()
            )
        elif isinstance(content, discord.Embed):
            embed = content
        else:
            embed = discord.Embed(description=str(content))

        await webhook.edit_message("@original", embed=embed, view=None)
    except Exception as e:
        logger.warning(f"[giveaway] failed to edit ephemeral message: {e}")
