"""Giveaway entry lists: who a streamer's website vouches for.

A site (nickcleveland.com's verified Stake affiliates, say) replaces a list
whole through the dashboard's public API (Admin-Dashboard
utils/giveaway_lists.py), and a chat giveaway names it in `entry_list`. The
bot only reads the two tables, on every entry to a giveaway that names one.

One row per identity a member has: their Kick or Twitch account (id and/or
username) or their Discord account id. A Discord id alone means nothing to a
streamer, so the bot also keeps giveaway_list_discord_names, the display name
behind each one, for the owner's Entry lists tab (refresh_list_names).
"""

import discord
from sqlalchemy import text

#: Discord users looked up through the API per guild per refresh: people on a
#: list whom no member cache holds and who have no stored name yet.
NAME_FETCHES_PER_REFRESH = 25


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


async def refresh_list_names(bot, engine, guild):
    """Store the Discord display name behind every Discord id on this guild's lists.

    The dashboard's Entry lists tab (the owner's) shows each Discord member by
    name, which only Discord knows. As with links.discord_username, the name is
    resolved here from the member cache and stored, so the dashboard never
    calls Discord per row. Someone no cache holds is fetched once, a few per
    refresh. Names of people no longer on any of the guild's lists are
    dropped. Display name = global_name, else the account username. Returns
    how many names were written.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT m.platform_user_id, n.display_name
                FROM giveaway_list_members m
                JOIN giveaway_lists l ON l.id = m.list_id
                LEFT JOIN giveaway_list_discord_names n
                       ON n.discord_server_id = l.discord_server_id AND n.discord_id = m.platform_user_id
                WHERE l.discord_server_id = :sid AND m.platform = 'discord'
                """
            ),
            {"sid": guild.id},
        ).fetchall()

    updates, fetched = [], 0
    for discord_id, stored in rows:
        user = guild.get_member(int(discord_id)) or bot.get_user(int(discord_id))
        if user is None and stored is None and fetched < NAME_FETCHES_PER_REFRESH:
            fetched += 1
            try:
                user = await bot.fetch_user(int(discord_id))
            except discord.HTTPException:
                user = None
        if user is None:
            continue
        name = (getattr(user, "global_name", None) or user.name or "")[:100]
        if name and name != stored:
            updates.append({"sid": guild.id, "d": discord_id, "n": name})

    with engine.begin() as conn:
        if updates:
            conn.execute(
                text(
                    """
                    INSERT INTO giveaway_list_discord_names (discord_server_id, discord_id, display_name, updated_at)
                    VALUES (:sid, :d, :n, CURRENT_TIMESTAMP)
                    ON CONFLICT (discord_server_id, discord_id)
                    DO UPDATE SET display_name = EXCLUDED.display_name, updated_at = CURRENT_TIMESTAMP
                    """
                ),
                updates,
            )
        conn.execute(
            text(
                """
                DELETE FROM giveaway_list_discord_names
                WHERE discord_server_id = :sid
                  AND discord_id NOT IN (
                      SELECT m.platform_user_id
                      FROM giveaway_list_members m
                      JOIN giveaway_lists l ON l.id = m.list_id
                      WHERE l.discord_server_id = :sid AND m.platform = 'discord'
                  )
                """
            ),
            {"sid": guild.id},
        )
    return len(updates)
