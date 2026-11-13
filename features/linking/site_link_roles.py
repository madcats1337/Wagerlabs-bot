"""The Kick linked role for accounts a streamer's own site linked.

A site that signs players in with both Kick and Discord links the two through
the public API (the dashboard's utils/site_links.py), as the link panel would.
A panel link gives the Kick linked role (bot.py's process_oauth_notifications),
so a site link gets it too, and the two kinds of link read the same in the
server. Never a DM, nor the panel's fallback of posting in a public channel
when DMs are closed: a site's first sync may link hundreds of players at once
(the owner, 2026-11-13).

The event (`dashboard:management`, `site_links_changed`) names the Discord
accounts that gained or lost a site link, and each is read afresh from `links`
rather than trusted: whoever has a Kick link in the server now has the role,
and whoever has none loses it. So a late or repeated event does no harm, and a
viewer the site unlinked who linked again through the panel meanwhile keeps it.

Run as a task of its own: Discord paces role changes, and hundreds of them
would otherwise hold every other dashboard event behind them, as the
subscriber handles events one at a time.
"""

import asyncio
import logging

import discord
from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

#: Holds each running task, which asyncio would otherwise be free to collect.
_running = set()

GIVE_REASON = "Kick account linked by the streamer's site"
TAKE_REASON = "The streamer's site unlinked this Kick account"


def schedule_site_link_roles(bot, engine, data):
    """Start one event's role changes without waiting for them."""
    task = asyncio.get_running_loop().create_task(apply_site_link_roles(bot, engine, data))
    _running.add(task)
    task.add_done_callback(_running.discard)
    return task


def _ids(values):
    ids = []
    for value in values if isinstance(values, list) else []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def read_links(engine, guild_id, discord_ids):
    """The server's Kick linked role id (or None), and which of these accounts
    have a Kick link in the server now."""
    with engine.connect() as conn:
        role_id = conn.execute(
            text("SELECT value FROM bot_settings WHERE key = 'kick_linked_role_id' AND discord_server_id = :g"),
            {"g": guild_id},
        ).scalar()
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT discord_id FROM links
                WHERE discord_server_id = :g AND COALESCE(platform, 'kick') = 'kick' AND discord_id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"g": guild_id, "ids": list(discord_ids)},
        ).fetchall()
    role_id = str(role_id or "").strip()
    return (int(role_id) if role_id.isdigit() else None), {int(row[0]) for row in rows}


async def _member(guild, discord_id):
    """The member, or None when they aren't in the server. The cache answers
    once the server is chunked; before then a miss is asked of Discord."""
    member = guild.get_member(discord_id)
    if member is not None or getattr(guild, "chunked", False):
        return member
    try:
        return await guild.fetch_member(discord_id)
    except discord.NotFound:
        return None


async def apply_site_link_roles(bot, engine, data):
    """Give or take the Kick linked role for each account the event names.
    Returns (given, taken), for the log and the tests."""
    try:
        guild_id = int(data.get("discord_server_id"))
    except (AttributeError, TypeError, ValueError):
        return 0, 0
    ids = list(dict.fromkeys(_ids(data.get("added")) + _ids(data.get("removed"))))
    guild = bot.get_guild(guild_id) if ids else None
    if guild is None:
        return 0, 0

    try:
        role_id, linked = await asyncio.to_thread(read_links, engine, guild_id, ids)
    except Exception as e:
        logger.error(f"[LINKS] could not read site links for the linked role: {e}")
        return 0, 0
    if role_id is None:
        return 0, 0  # no Kick linked role set up: nothing to give
    role = guild.get_role(role_id)
    if role is None:
        logger.warning(f"[LINKS] the Kick linked role {role_id} is not in this server")
        return 0, 0
    if not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
        logger.warning(
            f"[LINKS] cannot give '{role.name}' for site links: the bot needs Manage Roles "
            f"and a role above it ('{guild.me.top_role.name}' is its highest)"
        )
        return 0, 0

    given = taken = 0
    for discord_id in ids:
        try:
            member = await _member(guild, discord_id)
            if member is None:
                continue
            has_role = role in member.roles
            if discord_id in linked and not has_role:
                await member.add_roles(role, reason=GIVE_REASON)
                given += 1
            elif discord_id not in linked and has_role:
                await member.remove_roles(role, reason=TAKE_REASON)
                taken += 1
        except discord.HTTPException as e:
            logger.warning(f"[LINKS] linked role for {discord_id}: {e}")
    if given or taken:
        logger.info(f"[LINKS] site links: gave the Kick linked role to {given}, took it from {taken}")
    return given, taken
