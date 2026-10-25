"""Loyalty points granted outside the watchtime conversion.

Three sources share this module:

  * **Mod grants** — the ``{give_points}`` custom-command variable (Kick/Twitch
    chat) and ``/pointsgiveviewer`` (Discord) credit a viewer by their chat name.
  * **Points drops** — ``!pointsdrop`` in chat, or the dashboard's Drop button,
    credit a set amount to random recently-active chatters.
  * **Role bonuses** — members holding a configured Discord role earn extra
    points on every 5-minute watchtime block (applied in ``award_points``).

Every credit lands on the person's ONE canonical ``user_points`` row (the
lowest of their linked handles), the same row ``award_points_for_watchtime``
writes, so the points are spendable from either platform and in the shop.

Grants only ever add to ``points``/``total_earned``; they never touch the
watchtime ledger (``points_watchtime_converted``), so they can't cause the
re-earn trap a balance reset has to guard against.

All functions take a SQLAlchemy connection so callers control the transaction.
bot.py runs as ``__main__`` — this module must never ``import bot``.
"""

import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: One mod grant is capped well under the INTEGER ceiling of user_points.points.
MAX_GRANT = 1_000_000
MAX_DROP_AMOUNT = 100_000
#: A drop names every winner in one chat line (500 chars on Kick and Twitch).
MAX_DROP_WINNERS = 25
MAX_DROP_WINDOW_MINUTES = 60
MAX_ROLE_BONUS = 10_000

DROP_DEFAULTS = {
    "amount": 100,
    "winners": 5,
    "window_minutes": 10,
    "chat_enabled": True,
}

#: Chat bots that sit in most channels and should never win a drop.
KNOWN_CHAT_BOTS = frozenset(
    {
        "botrix",
        "kickbot",
        "nightbot",
        "streamelements",
        "streamlabs",
        "moobot",
        "fossabot",
        "sery_bot",
        "wizebot",
    }
)

#: Badges that may run moderator chat controls. Kick (via kickpython) reports
#: badge texts ("Moderator", "Broadcaster"); Twitch reports set_ids.
MOD_BADGES = frozenset({"moderator", "broadcaster"})

CHAT_LIMIT = 500

_NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_.\-]{0,39}$")


def is_chat_moderator(badges) -> bool:
    """True when a chatter's badges include moderator or broadcaster.

    Accepts the shapes the chat paths produce: kickpython's list of badge
    texts, Kick Pusher's ``{"type", "text"}`` dicts, and Twitch EventSub's
    ``{"set_id"}`` dicts.
    """
    for badge in badges or ():
        if isinstance(badge, dict):
            values = (badge.get("type"), badge.get("set_id"), badge.get("text"))
        else:
            values = (badge,)
        if any(isinstance(v, str) and v.strip().lower() in MOD_BADGES for v in values):
            return True
    return False


def normalize_chat_name(raw) -> str:
    """A chat name as typed (``@Name``) → lowercase handle, or "" if invalid."""
    name = str(raw or "").strip().lstrip("@").strip().lower()
    return name if _NAME_RE.match(name) else ""


def parse_positive_int(raw, maximum):
    """``raw`` as an int in 1..maximum (commas allowed), or None."""
    try:
        value = int(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= maximum else None


def credit_points(conn, server_id, username, amount, discord_id=None) -> int:
    """Add ``amount`` to ``username``'s balance; returns the new balance.

    Upsert, not UPDATE: a viewer who has never earned has no row yet and the
    grant must still land.
    """
    row = conn.execute(
        text(
            """
            INSERT INTO user_points (kick_username, discord_id, points, total_earned, discord_server_id, last_updated)
            VALUES (:u, :d, :p, :p, :sid, CURRENT_TIMESTAMP)
            ON CONFLICT (kick_username, discord_server_id) DO UPDATE SET
                points = user_points.points + :p,
                total_earned = user_points.total_earned + :p,
                discord_id = COALESCE(:d, user_points.discord_id),
                last_updated = CURRENT_TIMESTAMP
            RETURNING points
            """
        ),
        {"u": username, "d": discord_id, "p": int(amount), "sid": server_id},
    ).fetchone()
    return int(row[0]) if row else int(amount)


def _stored_points_username(conn, server_id, name):
    """The existing user_points key for ``name`` (case as stored), or None."""
    row = conn.execute(
        text(
            """
            SELECT kick_username FROM user_points
            WHERE discord_server_id = :sid AND LOWER(kick_username) = :n
            ORDER BY (kick_username = :n) DESC
            LIMIT 1
            """
        ),
        {"sid": server_id, "n": name},
    ).fetchone()
    return row[0] if row else None


def resolve_points_target(conn, server_id, raw_name, platform="kick", recent_names=()):
    """Resolve a chat name to ``(points_username, discord_id)``, or None.

    A linked viewer resolves to their canonical row (lowest linked handle), so a
    grant to either of their handles lands on the one shared balance. Links are
    matched on ``platform`` only: the same name on Kick and on Twitch can be two
    different people, so a name typed in Kick chat never resolves through
    someone's Twitch link. A name linked only on another platform resolves to
    nothing, since the unlinked lookups below are keyed by bare username and
    would land on that other person's row.

    An unlinked viewer must already be known — a points row, watchtime, or a
    recent chat message — so a typo can't mint points onto a name nobody uses.
    """
    name = normalize_chat_name(raw_name)
    if not name:
        return None

    links = conn.execute(
        text(
            """
            SELECT discord_id, COALESCE(platform, 'kick') = :p AS same_platform FROM links
            WHERE discord_server_id = :sid
              AND LOWER(kick_name) = :n
              AND discord_id IS NOT NULL
            ORDER BY linked_at ASC NULLS LAST
            """
        ),
        {"sid": server_id, "n": name, "p": (platform or "kick").lower()},
    ).fetchall()
    link = next((row for row in links if row[1]), None)
    if link is None and links:
        return None

    if link:
        discord_id = link[0]
        canonical = conn.execute(
            text(
                """
                SELECT MIN(LOWER(kick_name)) FROM links
                WHERE discord_server_id = :sid AND discord_id = :d AND kick_name IS NOT NULL
                """
            ),
            {"sid": server_id, "d": discord_id},
        ).scalar()
        canonical = canonical or name
        return _stored_points_username(conn, server_id, canonical) or canonical, discord_id

    stored = _stored_points_username(conn, server_id, name)
    if stored:
        return stored, None
    if name in {str(n).lower() for n in recent_names or ()}:
        return name, None
    seen = conn.execute(
        text("SELECT 1 FROM watchtime WHERE discord_server_id = :sid AND LOWER(username) = :n LIMIT 1"),
        {"sid": server_id, "n": name},
    ).fetchone()
    return (name, None) if seen else None


def give_points_to_chat_name(engine, server_id, raw_name, amount, platform="kick", recent_names=()):
    """Credit a viewer named in chat; returns their new balance, or None if unknown.

    Shared by the built-in ``!givepoints`` and the ``{give_points}`` variable.
    Blocking — call it through ``asyncio.to_thread`` from the event loop.
    """
    with engine.begin() as conn:
        resolved = resolve_points_target(conn, server_id, raw_name, platform, recent_names)
        if not resolved:
            return None
        points_username, discord_id = resolved
        return credit_points(conn, server_id, points_username, amount, discord_id)


# ── Role bonuses ─────────────────────────────────────────────────────────────


def parse_role_bonuses(raw):
    """``role_bonus_points`` setting → ``{role_id: points}``.

    The setting is a JSON list of ``{"role_id": "...", "points": N}``. Garbage
    entries are dropped rather than failing the award loop; a role listed twice
    keeps its higher bonus.
    """
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    bonuses = {}
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict):
            continue
        try:
            role_id = int(str(item.get("role_id", "")).strip())
            points = int(item.get("points"))
        except (TypeError, ValueError):
            continue
        if role_id > 0 and 1 <= points <= MAX_ROLE_BONUS:
            bonuses[role_id] = max(points, bonuses.get(role_id, 0))
    return bonuses


def load_role_bonuses(conn, server_id):
    """This server's role bonuses, ``{role_id: points_per_5min}``."""
    row = conn.execute(
        text("SELECT value FROM point_settings WHERE key = 'role_bonus_points' AND discord_server_id = :sid"),
        {"sid": server_id},
    ).fetchone()
    return parse_role_bonuses(row[0]) if row and row[0] else {}


def role_bonus_for(member, bonuses) -> int:
    """The bonus per 5-minute block for ``member``: the highest of their roles'."""
    if member is None or not bonuses:
        return 0
    return max((bonuses.get(role.id, 0) for role in getattr(member, "roles", ())), default=0)


# ── Points drops ─────────────────────────────────────────────────────────────

#: {guild_id: {username_lower: last_message_at}} — who a drop can pick. The
#: bot's recent_chatters_by_guild is pruned at its 5-minute live-detection
#: window, shorter than the longest drop window, so drops keep their own list.
_drop_chatters = {}


def record_chatter(guild_id, username, now=None):
    """Note a chat message; called by every chat path alongside watchtime."""
    if not guild_id or not username:
        return
    now = now or datetime.now(timezone.utc)
    chatters = _drop_chatters.setdefault(guild_id, {})
    chatters[str(username).lower()] = now
    if len(chatters) % 256 == 0:
        _prune(chatters, now)


def _prune(chatters, now):
    cutoff = now - timedelta(minutes=MAX_DROP_WINDOW_MINUTES)
    for name in [n for n, t in chatters.items() if t < cutoff]:
        del chatters[name]


def chatters_since(guild_id, window_minutes, now=None):
    """``{username: last_message_at}`` for chatters in the last ``window_minutes``."""
    now = now or datetime.now(timezone.utc)
    chatters = _drop_chatters.get(guild_id, {})
    _prune(chatters, now)
    cutoff = now - timedelta(minutes=window_minutes)
    return {n: t for n, t in chatters.items() if t >= cutoff}


def _setting_int(values, key, default, maximum):
    parsed = parse_positive_int(values.get(key), maximum)
    return parsed if parsed is not None else default


def load_drop_settings(conn, server_id):
    """Drop defaults and the exclusion list from ``point_settings``."""
    rows = conn.execute(
        text(
            """
            SELECT key, value FROM point_settings
            WHERE discord_server_id = :sid
              AND key IN ('points_drop_amount', 'points_drop_winners',
                          'points_drop_window_minutes', 'points_drop_chat_enabled',
                          'points_drop_exclude')
            """
        ),
        {"sid": server_id},
    ).fetchall()
    values = {k: v for k, v in rows}
    exclude = {normalize_chat_name(n) for n in re.split(r"[\s,]+", values.get("points_drop_exclude") or "")}
    exclude.discard("")
    return {
        "amount": _setting_int(values, "points_drop_amount", DROP_DEFAULTS["amount"], MAX_DROP_AMOUNT),
        "winners": _setting_int(values, "points_drop_winners", DROP_DEFAULTS["winners"], MAX_DROP_WINNERS),
        "window_minutes": _setting_int(
            values, "points_drop_window_minutes", DROP_DEFAULTS["window_minutes"], MAX_DROP_WINDOW_MINUTES
        ),
        "chat_enabled": str(values.get("points_drop_chat_enabled", "true")).strip().lower() != "false",
        "exclude": exclude,
    }


def broadcaster_names(conn, server_id):
    """The streamer's own Kick/Twitch channel names, never drop winners."""
    rows = conn.execute(
        text(
            """
            SELECT value FROM bot_settings
            WHERE discord_server_id = :sid AND key IN ('kick_channel', 'twitch_channel')
            """
        ),
        {"sid": server_id},
    ).fetchall()
    return {str(r[0]).strip().lower() for r in rows if r[0]}


def run_points_drop(conn, server_id, candidates, amount, winners, source, triggered_by=None, rng=None):
    """Credit ``amount`` to up to ``winners`` random ``candidates``.

    ``candidates`` are points usernames already collapsed to one per person (the
    caller runs them through the same dedupe as watchtime), so a dual-platform
    viewer can't win twice. Records the drop in ``points_drops`` and returns the
    winners in draw order.
    """
    if not 1 <= int(amount) <= MAX_DROP_AMOUNT:
        raise ValueError(f"drop amount {amount} is outside 1..{MAX_DROP_AMOUNT}")
    pool = sorted(set(candidates))
    if not pool:
        return []
    picked = (rng or random.SystemRandom()).sample(pool, min(winners, len(pool)))
    for name in picked:
        credit_points(conn, server_id, _stored_points_username(conn, server_id, name) or name, amount)
    conn.execute(
        text(
            """
            INSERT INTO points_drops (discord_server_id, amount, winners, source, triggered_by)
            VALUES (:sid, :amount, :winners, :source, :by)
            """
        ),
        {"sid": server_id, "amount": amount, "winners": picked, "source": source, "by": triggered_by},
    )
    return picked


def format_drop_announcement(winners, amount, limit=CHAT_LIMIT) -> str:
    """One chat line naming the winners, shortened to fit ``limit``."""
    head = f"Points drop! {amount:,} points each to: "
    names = [f"@{w}" for w in winners]
    for shown in range(len(names), 0, -1):
        rest = len(names) - shown
        line = head + ", ".join(names[:shown]) + (f" and {rest} more" if rest else "")
        if len(line) <= limit:
            return line
    return f"Points drop! {amount:,} points each to {len(names)} chatters."
