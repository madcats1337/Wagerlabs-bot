"""Giveaway prize payouts: asking winners where to send their prize.

The dashboard owns the payout list (utils/giveaway_payouts.py there). A
giveaway with a `prize_amount` owes each winner that much, and the streamer
tips it on the casino the server names in bot_settings `giveaway_payout_casino`
(unset: payouts are off and the bot asks nobody anything). The bot's part:

* Viewers save their username on that casino with the Payout details button,
  on the link panel or in a winner DM (`PayoutDetailsButton`). The button
  carries the server in its custom_id and registers as a dynamic item, so one
  in a DM keeps working after a restart.
* A streamer's own site can send the names it knows (`site_payout_names`,
  through the dashboard's public API: utils/site_payout_names.py there), by
  the Discord, Kick or Twitch account id each player signed in with. Its name
  comes first, ahead of one a viewer saved: the site's own checks tie it to
  them (nickcleveland.com verifies a Stake link against Stake's affiliate
  data; the owner's call, 2026-10-26). So the link panel names the site, and
  the Payout details button and the winner DM send a viewer it knows there
  instead of to the form.
* Once a priced giveaway's winners are announced, or the dashboard publishes
  `giveaway_payout_set`, `notify_prize_winners` DMs each winner nobody asked
  yet: where their prize will go, or how to say where. A chat winner with no
  Discord account linked is told in chat instead, unless the streamer's site
  gave their name. `giveaway_winners.payout_notice` records how it went, for
  the dashboard's Payouts tab.

Everything is read fresh: the casino, the amount, a viewer's saved name and
the site's names can all change between a DM and a click.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import NamedTuple, Optional

import discord
from sqlalchemy import text

logger = logging.getLogger(__name__)

CASINO_SETTING = "giveaway_payout_casino"

#: The casinos a server may tip prizes on, by key. Mirrors the dashboard's
#: utils/slot_platforms.py registry (keys and labels): keep the two in sync.
CASINO_LABELS = {
    "shuffle": "Shuffle",
    "howl": "Howl",
    "thrill": "Thrill",
    "rainbet": "Rainbet",
    "acebet": "Acebet",
    "razed": "Razed",
    "spartans": "Spartans",
    "roobet": "Roobet",
    "stake": "Stake",
    "casino500": "500 Casino",
}

#: A casino username: 2 to 32 characters, none of them whitespace, and nothing
#: that would turn into a Discord mention or escape the bold it is shown in.
USERNAME_RE = re.compile(r"[^\s<>@`\\]{2,32}")
USERNAME_RULE = "That doesn't look like a username: 2 to 32 characters, no spaces."

#: A claim older than this was left by a bot that died mid-send: claim it again.
STALE_CLAIM = timedelta(minutes=10)

#: Kick caps a chat message at 500 characters.
CHAT_LIMIT = 480

#: How the bot names a streamer's site that sent no name for itself.
SITE_FALLBACK = "the streamer's website"


def _utcnow():
    # The timestamp columns are naive and hold UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def money(amount):
    """$25, $12.50, $1,000."""
    value = Decimal(str(amount))
    if value == value.to_integral_value():
        return f"${int(value):,}"
    return f"${value:,.2f}"


def valid_username(name):
    return bool(name) and USERNAME_RE.fullmatch(name) is not None


# ---------------------------------------------------------------------------
# Reads and writes
# ---------------------------------------------------------------------------
def payout_casino(engine, guild_id):
    """The casino this server tips prizes on, or None when payouts are off."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT value FROM bot_settings WHERE key = :k AND discord_server_id = :g"),
            {"k": CASINO_SETTING, "g": int(guild_id)},
        ).fetchone()
    key = ((row[0] if row else "") or "").strip().lower()
    return key if key in CASINO_LABELS else None


class PayoutDestination(NamedTuple):
    """Where a person's prizes go: the username, whether it is their verified
    affiliate link, and, when the streamer's own site sent it, that site's
    name (SITE_FALLBACK when it gave none), else None."""

    username: Optional[str]
    verified: bool
    site: Optional[str]


NOWHERE = PayoutDestination(None, False, None)


def payout_destination(engine, guild_id, discord_id, casino, chat=None):
    """Where this person's prizes go on `casino`, as a PayoutDestination.

    In order: the name the streamer's own site sent for them, by `chat` (the
    (platform, account id) a chat winner entered with) first, then by their
    Discord account, since the site's own checks tie it to them; the name they
    saved, since it is where they asked for prizes to go; their verified
    affiliate link there (the Shuffle, Howl and Roobet panels). `discord_id`
    may be None for a chat winner with no Discord linked. NOWHERE when none
    exists. Mirrors the dashboard's utils/giveaway_payouts.destination_of.
    """
    saved = verified = None
    with engine.connect() as conn:
        site = _site_name(conn, guild_id, casino, discord_id, chat)
        if discord_id:
            saved = conn.execute(
                text(
                    """
                    SELECT username FROM viewer_payout_details
                    WHERE discord_server_id = :g AND discord_id = :d AND casino = :c
                    """
                ),
                {"g": int(guild_id), "d": int(discord_id), "c": casino},
            ).fetchone()
            verified = conn.execute(
                text(
                    """
                    SELECT shuffle_username FROM raffle_shuffle_links
                    WHERE discord_id = :d AND platform = :c AND verified IS TRUE
                    LIMIT 1
                    """
                ),
                {"d": int(discord_id), "c": casino},
            ).fetchone()
    if site:
        username, site_name = site
    else:
        username, site_name = (saved[0] if saved else None) or (verified[0] if verified else None), None
    if not username:
        return NOWHERE
    return PayoutDestination(username, bool(verified) and username.lower() == verified[0].lower(), site_name)


def _site_name(conn, guild_id, casino, discord_id=None, chat=None):
    """(username, the site's name) the streamer's site sent for this person on
    `casino`, or None.

    By account id only, never a handle: a handle can change hands, and money
    follows the match. The chat account a winner entered with comes first, as
    their own; their Discord account is the one Wagerlabs linked to it.
    """
    accounts = []
    if chat and chat[0] and chat[1]:
        accounts.append((chat[0], str(chat[1])))
    if discord_id:
        accounts.append(("discord", str(int(discord_id))))
    for platform, account_id in accounts:
        row = conn.execute(
            text(
                """
                SELECT n.username, s.source FROM site_payout_names n
                JOIN site_payout_name_syncs s
                  ON s.discord_server_id = n.discord_server_id AND s.casino = n.casino
                WHERE n.discord_server_id = :g AND n.casino = :c AND n.platform = :p AND n.platform_user_id = :a
                """
            ),
            {"g": int(guild_id), "c": casino, "p": platform, "a": account_id},
        ).fetchone()
        if row:
            return row[0], row[1] or SITE_FALLBACK
    return None


def payout_site_source(engine, guild_id, casino):
    """The name of the site that sent this server's names for `casino`
    ("example.com"), or None when no site sent any, or sent no name for itself."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT source FROM site_payout_name_syncs
                WHERE discord_server_id = :g AND casino = :c AND member_count > 0
                """
            ),
            {"g": int(guild_id), "c": casino},
        ).fetchone()
    return (row[0] if row else None) or None


def save_username(engine, guild_id, discord_id, casino, username):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO viewer_payout_details (discord_server_id, discord_id, casino, username, updated_at)
                VALUES (:g, :d, :c, :u, :now)
                ON CONFLICT (discord_server_id, discord_id, casino)
                DO UPDATE SET username = EXCLUDED.username, updated_at = EXCLUDED.updated_at
                """
            ),
            {"g": int(guild_id), "d": int(discord_id), "c": casino, "u": username, "now": _utcnow()},
        )


def _priced_giveaway(engine, guild_id, giveaway_id):
    """The giveaway when it has a prize amount, else None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, title, prize_amount, entry_method FROM giveaways
                WHERE id = :gid AND discord_server_id = :g
                """
            ),
            {"gid": int(giveaway_id), "g": int(guild_id)},
        ).fetchone()
    if not row or row.prize_amount is None:
        return None
    return dict(row._mapping)


def _claim_winners(engine, guild_id, giveaway_id):
    """Mark the winners nobody asked yet as being asked, and return them.

    One UPDATE, so two announce paths running at once can't both DM a winner.
    A paid winner is never asked.
    """
    now = _utcnow()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                UPDATE giveaway_winners
                SET payout_notice = 'pending', payout_notice_at = :now
                WHERE giveaway_id = :gid AND discord_server_id = :sid
                  AND paid_at IS NULL
                  AND (payout_notice IS NULL OR (payout_notice = 'pending' AND payout_notice_at < :stale))
                RETURNING id, kick_username, discord_id
                """
            ),
            {"now": now, "stale": now - STALE_CLAIM, "gid": int(giveaway_id), "sid": str(guild_id)},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _set_notice(engine, winner_id, notice):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE giveaway_winners SET payout_notice = :n, payout_notice_at = :now WHERE id = :id"),
            {"n": notice, "now": _utcnow(), "id": int(winner_id)},
        )


def _winner_entry(conn, guild_id, giveaway, name):
    """The entry a winner was drawn from: the first one under their name."""
    return conn.execute(
        text(
            """
            SELECT discord_id, platform, platform_user_id, kick_username FROM giveaway_entries
            WHERE giveaway_id = :gid AND discord_server_id = :g
              AND COALESCE(kick_username, discord_username) = :name
            ORDER BY id ASC
            LIMIT 1
            """
        ),
        {"gid": int(giveaway["id"]), "g": int(guild_id), "name": name},
    ).fetchone()


def winner_chat_account(engine, guild_id, giveaway, winner):
    """(platform, account id) of the chat account a chat winner entered with, or
    None: a Discord giveaway, or an entry recorded before the chat or the
    chatter's id was."""
    if giveaway.get("entry_method") == "discord":
        return None
    with engine.connect() as conn:
        entry = _winner_entry(conn, guild_id, giveaway, winner.get("kick_username"))
    if not entry or not entry.platform or not entry.platform_user_id:
        return None
    return entry.platform, str(entry.platform_user_id)


def winner_discord_id(engine, guild_id, giveaway, winner):
    """The Discord account a winner's prize is keyed by, or None.

    A prize follows it, so it names the right person or nobody. A Discord
    winner's own. A chat winner's: the one their winner row or entry holds (a
    gated entry resolves it), else the link on the chat their entry came from,
    by the chatter's id there when the chat gave one, else by handle on that
    chat, and only when either names one person. With no chat recorded there is
    no guess: a bare handle could be someone else's on the other chat. Mirrors
    the dashboard's utils/giveaway_payouts.
    """
    if winner.get("discord_id"):
        return int(winner["discord_id"])
    name = winner.get("kick_username")
    with engine.connect() as conn:
        entry = _winner_entry(conn, guild_id, giveaway, name)
        if entry and entry.discord_id:
            return int(entry.discord_id)
        if giveaway.get("entry_method") == "discord" or not entry or not entry.platform:
            return None
        if entry.platform_user_id:
            sql = """
                SELECT DISTINCT discord_id FROM links
                WHERE discord_server_id = :g AND platform = :p AND platform_user_id = :who
                  AND discord_id IS NOT NULL
            """
            who = str(entry.platform_user_id)
        else:
            sql = """
                SELECT DISTINCT discord_id FROM links
                WHERE discord_server_id = :g AND platform = :p AND LOWER(kick_name) = LOWER(:who)
                  AND discord_id IS NOT NULL
            """
            who = entry.kick_username or name
        rows = conn.execute(text(sql), {"g": int(guild_id), "p": entry.platform, "who": who}).fetchall()
    return int(rows[0][0]) if len(rows) == 1 else None


# ---------------------------------------------------------------------------
# The button and the form
# ---------------------------------------------------------------------------
def _in_server(guild_name):
    return f" in **{discord.utils.escape_markdown(guild_name)}**" if guild_name else ""


def site_account_text(username, casino, site):
    """**name** on Stake, the account you linked on example.com"""
    return (
        f"**{discord.utils.escape_markdown(username)}** on {CASINO_LABELS[casino]}, "
        f"the account you linked on {discord.utils.escape_markdown(site)}"
    )


def site_owns_it_text(dest, casino, guild_name, saved=False):
    """Where the prizes of a viewer the streamer's site knows go, and where to change it."""
    lead = "Saved, but prizes" if saved else "Prizes"
    change = "To change that" if saved else "To change it"
    return (
        f"{lead} you win{_in_server(guild_name)} go to {site_account_text(dest.username, casino, dest.site)}. "
        f"{change}, change your {CASINO_LABELS[casino]} link there."
    )


class PayoutDetailsModal(discord.ui.Modal):
    """Asks for the viewer's username on the casino the server tips prizes on."""

    def __init__(self, engine, guild_id, guild_name, casino, current=None):
        super().__init__(title="Payout details")
        self.engine = engine
        self.guild_id = int(guild_id)
        self.guild_name = guild_name
        self.casino = casino
        label = CASINO_LABELS[casino]
        self.username = discord.ui.TextInput(
            label=f"Your {label} username"[:45],
            placeholder=f"Exactly as it shows on {label}"[:100],
            default=current or None,
            min_length=2,
            max_length=32,
            required=True,
            style=discord.TextStyle.short,
        )
        self.add_item(self.username)

    async def on_submit(self, interaction: discord.Interaction):
        name = (self.username.value or "").strip()
        if not valid_username(name):
            await interaction.response.send_message(USERNAME_RULE, ephemeral=True)
            return
        # The server may have switched casinos while the form was open: a name
        # for the old one would be paid nowhere.
        if payout_casino(self.engine, self.guild_id) != self.casino:
            await interaction.response.send_message(
                "This server changed where it sends prizes. Press the button again.", ephemeral=True
            )
            return
        save_username(self.engine, self.guild_id, interaction.user.id, self.casino, name)
        # The streamer's site comes first: a name saved here doesn't move a
        # prize the site's name already points somewhere.
        dest = payout_destination(self.engine, self.guild_id, interaction.user.id, self.casino)
        if dest.site:
            message = site_owns_it_text(dest, self.casino, self.guild_name, saved=True)
        else:
            message = (
                f"Saved. Prizes you win{_in_server(self.guild_name)} go to "
                f"**{discord.utils.escape_markdown(name)}** on {CASINO_LABELS[self.casino]}."
            )
        await interaction.response.send_message(message, ephemeral=True)
        logger.info(f"[giveaway] saved a payout username for {interaction.user.id} on {self.casino}")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"[giveaway] payout details form error: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong. Try again.", ephemeral=True)


class PayoutDetailsButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"wl_payout:(?P<guild_id>\d+)",
):
    """Opens the payout details form for the server in its custom_id.

    The server rides in the custom_id because a DM has no server of its own,
    and the engine is resolved at dispatch, so buttons already sent keep
    working across a restart (registered once via `bot.add_dynamic_items`).
    """

    def __init__(self, guild_id, label="Payout details", style=discord.ButtonStyle.secondary, engine=None):
        self.guild_id = int(guild_id)
        self.engine = engine
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"wl_payout:{int(guild_id)}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["guild_id"]), engine=_engine())

    async def callback(self, interaction: discord.Interaction):
        engine = self.engine or _engine()
        casino = payout_casino(engine, self.guild_id)
        if not casino:
            await interaction.response.send_message(
                "This server isn't collecting payout details right now.", ephemeral=True
            )
            return
        dest = payout_destination(engine, self.guild_id, interaction.user.id, casino)
        guild = interaction.client.get_guild(self.guild_id)
        guild_name = guild.name if guild else None
        if dest.site:
            # The streamer's site comes first, so the form would change nothing.
            await interaction.response.send_message(site_owns_it_text(dest, casino, guild_name), ephemeral=True)
            return
        await interaction.response.send_modal(
            PayoutDetailsModal(engine, self.guild_id, guild_name, casino, dest.username)
        )


def _engine():
    # Imported here: redis_subscriber imports the giveaway modules lazily, and
    # a top-level import would close the cycle.
    from redis_subscriber import get_engine

    return get_engine()


# ---------------------------------------------------------------------------
# Asking winners
# ---------------------------------------------------------------------------
def winner_dm_text(amount, title, guild_name, casino, username, site=None):
    """The DM a winner gets, and the label of its button: None when the name
    came from the streamer's site, which only the site can change."""
    label = CASINO_LABELS[casino]
    won = f"You won **{money(amount)}** in **{discord.utils.escape_markdown(title or 'a giveaway')}**"
    if guild_name:
        won += f" on **{discord.utils.escape_markdown(guild_name)}**"
    if username and site:
        return f"{won}. It will be tipped to {site_account_text(username, casino, site)}.", None
    if username:
        return (
            f"{won}. It will be tipped to **{discord.utils.escape_markdown(username)}** on {label}. "
            "Wrong name? Change it below.",
            "Change payout details",
        )
    return f"{won}. Add your {label} username below so it can be tipped to you.", "Add payout details"


async def _dm_winner(bot, guild_id, guild_name, giveaway, casino, discord_id, dest):
    """True when the DM went out."""
    try:
        user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
    except discord.HTTPException:
        return False
    content, button_label = winner_dm_text(
        giveaway["prize_amount"], giveaway.get("title"), guild_name, casino, dest.username, dest.site
    )
    view = None
    if button_label:
        view = discord.ui.View(timeout=None)
        view.add_item(PayoutDetailsButton(guild_id, label=button_label, style=discord.ButtonStyle.primary))
    try:
        await user.send(content=content, view=view)
        return True
    except discord.HTTPException as e:
        # Forbidden when their DMs are closed or no server is shared.
        logger.info(f"[giveaway] couldn't DM prize winner {discord_id}: {e}")
        return False


def not_linked_chat_text(names, title, casino):
    """Chat follow-up for chat winners the bot can't DM, under Kick's limit."""
    label = CASINO_LABELS[casino]
    tail = (
        f" you won {title or 'the giveaway'}! To get your prize, link your account with the Link Account "
        f"panel in Discord, then press Payout details there and add your {label} username."
    )
    mentions = []
    for name in names:
        candidate = " ".join(mentions + [f"@{name}"])
        if mentions and len(candidate) + len(tail) > CHAT_LIMIT:
            break
        mentions.append(f"@{name}")
    return " ".join(mentions) + tail


async def notify_prize_winners(bot, engine, guild_id, giveaway_id, send_chat=None):
    """Ask every winner of a priced giveaway whom nobody asked yet where to
    send their prize. Returns how many winners were asked.

    Does nothing unless the server tips prizes on a casino and the giveaway has
    a prize amount. Call it after the winners are announced; calling it again
    asks nobody twice. `send_chat(message, guild_id=...)` posts the follow-up
    for chat winners with no Discord account linked; leave it None where the
    server turned winner announcements in chat off.
    """
    casino = payout_casino(engine, guild_id)
    if not casino:
        return 0
    giveaway = _priced_giveaway(engine, guild_id, giveaway_id)
    if not giveaway:
        return 0
    winners = _claim_winners(engine, guild_id, giveaway_id)
    if not winners:
        return 0

    guild = bot.get_guild(int(guild_id))
    guild_name = guild.name if guild else None
    not_linked = []
    for winner in winners:
        try:
            discord_id = winner_discord_id(engine, guild_id, giveaway, winner)
            chat = winner_chat_account(engine, guild_id, giveaway, winner)
            if not discord_id:
                _set_notice(engine, winner["id"], "not_linked")
                # Nobody to DM. The chat note tells them how to add a name,
                # which they needn't when the streamer's site gave one.
                if not payout_destination(engine, guild_id, None, casino, chat).username:
                    not_linked.append(winner.get("kick_username") or "winner")
                continue
            dest = payout_destination(engine, guild_id, discord_id, casino, chat)
            sent = await _dm_winner(bot, int(guild_id), guild_name, giveaway, casino, discord_id, dest)
            _set_notice(engine, winner["id"], "dm_sent" if sent else "dm_closed")
        except Exception as e:
            # Left 'pending': the next call past STALE_CLAIM tries them again.
            logger.warning(f"[giveaway] asking prize winner {winner.get('id')} failed: {e}")

    if not_linked and send_chat and giveaway.get("entry_method") != "discord":
        try:
            await send_chat(not_linked_chat_text(not_linked, giveaway.get("title"), casino), guild_id=int(guild_id))
        except Exception as e:
            logger.warning(f"[giveaway] chat note for unlinked prize winners failed: {e}")

    logger.info(f"[giveaway] asked {len(winners)} winner(s) of {giveaway_id} where to send their prize")
    return len(winners)


#: Asks in flight, so each task stays referenced until it finishes.
_asking = set()


def ask_prize_winners_soon(bot, engine, guild_id, giveaway_id, send_chat=None):
    """`notify_prize_winners` in the background, returning its task.

    A DM per winner takes a while, and the callers (the dashboard-event
    subscriber, the giveaway timer loop) handle one event at a time. Safe to
    hand off: each winner is claimed before any DM goes out.
    """

    async def ask():
        try:
            await notify_prize_winners(bot, engine, guild_id, giveaway_id, send_chat=send_chat)
        except Exception as e:
            logger.warning(f"[giveaway] asking the prize winners of {giveaway_id} failed: {e}")

    task = asyncio.create_task(ask())
    _asking.add(task)
    task.add_done_callback(_asking.discard)
    return task
