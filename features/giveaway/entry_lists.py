"""Giveaway entry lists: who a streamer's website vouches for.

A site (nickcleveland.com's verified Stake affiliates, say) replaces a list
whole through the dashboard's public API (Admin-Dashboard
utils/giveaway_lists.py), and a chat giveaway names it in `entry_list`. The
bot only reads the two tables, on every entry to a giveaway that names one.

One row per identity a member has: their Kick or Twitch account (id and/or
username) or their Discord account id.
"""

from sqlalchemy import text


def on_entry_list(engine, guild_id, list_key, platform, handle, platform_user_id=None, discord_id=None):
    """Whether this chatter is on the server's list `list_key`.

    By their account id on `platform` when the chat event carries one (Twitch),
    else by their username: kickpython gives the bot only a Kick sender's
    name, so Kick matches by username. With an id in hand, a username counts
    only against a member the site sent without one, since a handle can be
    renamed away and claimed by someone else. `discord_id`, the Discord account
    the chatter linked in Wagerlabs, matches a Discord id on the list.

    A list that doesn't exist matches nobody.
    """
    platform = (platform or "kick").lower()
    puid = str(platform_user_id) if platform_user_id not in (None, "", 0) else None
    handle_key = handle.strip().lower() if handle else None

    clauses = []
    params = {"sid": guild_id, "key": list_key}
    if puid:
        clauses.append("(m.platform = :p AND m.platform_user_id = :puid)")
        params.update(p=platform, puid=puid)
    if handle_key:
        unnumbered = " AND m.platform_user_id IS NULL" if puid else ""
        clauses.append(f"(m.platform = :p AND m.username_key = :handle{unnumbered})")
        params.update(p=platform, handle=handle_key)
    if discord_id:
        clauses.append("(m.platform = 'discord' AND m.platform_user_id = :did)")
        params["did"] = str(discord_id)
    if not clauses:
        return False

    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"""
                SELECT 1
                FROM giveaway_lists l
                JOIN giveaway_list_members m ON m.list_id = l.id
                WHERE l.discord_server_id = :sid AND l.list_key = :key
                  AND ({" OR ".join(clauses)})
                LIMIT 1
                """
            ),
            params,
        ).fetchone()
    return row is not None


def entry_list_meta(engine, guild_id, list_key):
    """(name, hint) for announcing a list, or None when it doesn't exist."""
    if not list_key:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name, hint FROM giveaway_lists WHERE discord_server_id = :sid AND list_key = :key"),
            {"sid": guild_id, "key": list_key},
        ).fetchone()
    return (row[0], row[1]) if row else None


def chat_requirement(engine, guild, guild_id, role_id, list_key, mention_role=False):
    """Who may enter a gated chat giveaway, as (who, hint), or (None, None).

    `who` reads "Verified affiliates or members with the Affiliate role in
    Discord": either one gets in. The role is named for stream chat and
    mentioned (`mention_role`) in Discord. `hint` is the list's own line on how
    to get on it, set by the site that keeps it.
    """
    parts, hint = [], None
    if list_key:
        meta = entry_list_meta(engine, guild_id, list_key)
        parts.append(meta[0] if meta else "the giveaway's entry list")
        hint = meta[1] if meta else None
    if role_id:
        if mention_role:
            parts.append(f"members with <@&{int(role_id)}>")
        else:
            role = guild.get_role(int(role_id)) if guild else None
            parts.append(f"members with the {role.name + ' role' if role else 'required role'} in Discord")
    return (" or ".join(parts) or None), hint
