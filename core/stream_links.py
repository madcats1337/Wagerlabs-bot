"""
Platform-aware helpers for the `links` table (viewer identity).

After the add_platform_to_links migration, `links` holds one row per
(discord_id, discord_server_id, platform). A Discord user may link a Kick AND a
Twitch account in the same server; both credit the SAME discord_id downstream
(unified identity — watchtime/points/raffle tickets stay keyed off discord_id).

Use these helpers wherever a link is created/removed or a chat username must be
resolved to a discord_id, so the platform dimension is handled consistently.
"""

import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

_UPSERT_WITH_ID = """
    INSERT INTO links (discord_id, kick_name, discord_server_id, platform, platform_user_id, linked_at)
    VALUES (:d, :u, :gid, :platform, :puid, CURRENT_TIMESTAMP)
    ON CONFLICT (discord_id, discord_server_id, platform) DO UPDATE
    SET kick_name = excluded.kick_name,
        platform_user_id = COALESCE(excluded.platform_user_id, links.platform_user_id),
        linked_at = CURRENT_TIMESTAMP
"""

_UPSERT_WITHOUT_ID = """
    INSERT INTO links (discord_id, kick_name, discord_server_id, platform, linked_at)
    VALUES (:d, :u, :gid, :platform, CURRENT_TIMESTAMP)
    ON CONFLICT (discord_id, discord_server_id, platform) DO UPDATE
    SET kick_name = excluded.kick_name, linked_at = CURRENT_TIMESTAMP
"""


def upsert_link(
    engine,
    discord_id: int,
    username: str,
    guild_id: int,
    platform: str = "kick",
    platform_user_id=None,
):
    """Create/update a viewer's link for a platform (idempotent upsert).

    `platform_user_id` is the viewer's IMMUTABLE id on that platform (Kick
    `user_id`, Twitch `id`). It is the safe join key for anything that resolves
    to a points balance -- `kick_name` is a handle and can be renamed or
    recycled onto a different person.

    COALESCE on update: a caller that doesn't know the id must never WIPE one
    that is already stored.

    A collision on links_platform_user_id_server_unique means this platform
    account is already linked to a DIFFERENT Discord user in this server. That
    is a real identity ambiguity a human should resolve, so we do not guess:
    log both sides and fall back to writing the row WITHOUT the id, which is
    exactly the pre-column behaviour. Linking must never break because of it.
    """
    params = {"d": discord_id, "u": username.lower(), "gid": guild_id, "platform": platform}
    if platform_user_id is None:
        with engine.begin() as conn:
            conn.execute(text(_UPSERT_WITHOUT_ID), params)
        return

    try:
        with engine.begin() as conn:
            conn.execute(text(_UPSERT_WITH_ID), {**params, "puid": str(platform_user_id)})
        return
    except IntegrityError:
        logger.warning(
            f"link: {platform} user id {platform_user_id} is already linked to another Discord "
            f"user in server {guild_id} (attempted for discord_id={discord_id}, handle={username.lower()}). "
            f"Linking without the platform id -- needs manual review."
        )

    with engine.begin() as conn:
        conn.execute(text(_UPSERT_WITHOUT_ID), params)


def remove_link(engine, discord_id: int, guild_id: int, platform: str = None):
    """Remove a viewer's link. If platform is None, removes ALL platforms for the
    user in this server (full unlink); otherwise just the one platform."""
    with engine.begin() as conn:
        if platform is None:
            conn.execute(
                text("DELETE FROM links WHERE discord_id = :d AND discord_server_id = :gid"),
                {"d": discord_id, "gid": guild_id},
            )
        else:
            conn.execute(
                text("DELETE FROM links WHERE discord_id = :d AND discord_server_id = :gid AND platform = :p"),
                {"d": discord_id, "gid": guild_id, "p": platform},
            )


def resolve_discord_id(engine, username: str, guild_id: int, platform: str):
    """Resolve a chat username on a given platform to the linked discord_id, or None."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT discord_id FROM links
                WHERE LOWER(kick_name) = :u AND discord_server_id = :gid AND platform = :p
                """
            ),
            {"u": username.lower(), "gid": guild_id, "p": platform},
        ).fetchone()
    return row[0] if row else None


def resolve_canonical_identity(engine, username: str, guild_id: int, platform: str):
    """
    Resolve (username, platform) -> (discord_id, canonical_username) under the
    unified-identity model.

    A viewer may have both a Kick and a Twitch link. Downstream tables
    (watchtime/user_points/raffle_tickets) are keyed by username STRING, so to
    keep a single shared balance we pick a CANONICAL username per Discord user:
    the earliest-linked row for that server (prefer Kick, then by linked_at).
    Both platforms' chat then credit that one canonical username.

    Returns (discord_id, canonical_username) or (None, None) if not linked.
    """
    discord_id = resolve_discord_id(engine, username, guild_id, platform)
    if discord_id is None:
        return None, None
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT kick_name FROM links
                WHERE discord_id = :d AND discord_server_id = :gid
                ORDER BY (platform <> 'kick'), linked_at ASC
                LIMIT 1
                """
            ),
            {"d": discord_id, "gid": guild_id},
        ).fetchone()
    canonical = row[0] if row else username.lower()
    return discord_id, canonical


# ---------------------------------------------------------------------------
# Opportunistic platform_user_id backfill
# ---------------------------------------------------------------------------
# Rows created before the platform_user_id column have NULL and would otherwise
# stay on the weaker handle-match path until the viewer happens to re-link. Chat
# events already carry the sender's IMMUTABLE platform id, so we fill them in as
# people talk.
#
# This must never cost a DB write per message: `_backfill_seen` records the
# viewers already attempted in THIS process, so each one costs at most a single
# UPDATE per bot restart. The set is bounded so a very busy server cannot grow it
# without limit.
_backfill_seen = set()
_BACKFILL_SEEN_MAX = 50_000

_BACKFILL_SQL = """
    UPDATE links
    SET platform_user_id = :puid
    WHERE discord_server_id = :gid
      AND COALESCE(platform, 'kick') = :platform
      AND LOWER(kick_name) = :handle
      AND platform_user_id IS NULL
"""


def extract_chat_platform_user_id(msg: dict):
    """Pull the sender's immutable platform id out of a chat message, or None.

    Shapes differ by source: Twitch normalizes to `user.id`
    (core/stream_provider.normalize_twitch_chat_event), the Kick websocket uses
    `sender.id`, and the Kick webhook forwards `sender.user_id`.
    """
    if not isinstance(msg, dict):
        return None
    user = msg.get("user") if isinstance(msg.get("user"), dict) else {}
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    for value in (user.get("id"), sender.get("user_id"), sender.get("id")):
        if value not in (None, "", 0):
            return str(value)
    return None


def backfill_platform_user_id(engine, guild_id: int, platform: str, username: str, platform_user_id) -> None:
    """Best-effort: stamp a legacy links row with the chatter's immutable id.

    Only touches rows where `platform_user_id IS NULL` -- an id already stored is
    authoritative and is never overwritten.

    A unique-index collision means another row in this server already claims this
    platform id, i.e. the stale-handle corruption this column exists to prevent.
    We do NOT guess which row is right: log both sides and skip, leaving it for a
    human. Every failure is swallowed -- chat handling must not break because a
    backfill did.
    """
    if not (engine and guild_id and username and platform_user_id):
        return

    platform = (platform or "kick").lower()
    handle = username.lower()
    key = (guild_id, platform, handle)
    if key in _backfill_seen:
        return
    # Record before attempting, so a failure doesn't retry on every message.
    if len(_backfill_seen) >= _BACKFILL_SEEN_MAX:
        _backfill_seen.clear()
    _backfill_seen.add(key)

    try:
        with engine.begin() as conn:
            result = conn.execute(
                text(_BACKFILL_SQL),
                {"puid": str(platform_user_id), "gid": guild_id, "platform": platform, "handle": handle},
            )
        if result.rowcount:
            logger.info(f"link: backfilled {platform} user id for {handle} in server {guild_id}")
    except IntegrityError:
        logger.warning(
            f"link: {platform} user id {platform_user_id} (handle {handle}) is already claimed by a "
            f"different links row in server {guild_id} -- backfill skipped, needs manual review."
        )
    except Exception as e:
        logger.debug(f"link: platform_user_id backfill skipped for {handle} in {guild_id}: {e}")
