"""The Kick linked role for accounts a streamer's site linked (features/linking/site_link_roles.py).

Pinned: each account is read afresh from `links`, so whoever has a Kick link has the
role and whoever has none loses it, whatever the event said; nobody outside the
server is touched; and nothing happens without a role the bot may give.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import pytest
from sqlalchemy import create_engine, text

import features.linking.site_link_roles as slr

GUILD = 4242
ROLE = 777


class Role:
    def __init__(self, position=1):
        self.id = ROLE
        self.name = "Kick linked"
        self.position = position

    def __ge__(self, other):
        return self.position >= other.position


def member(discord_id, has_role):
    m = MagicMock()
    m.id = discord_id
    m.roles = [slr_role] if has_role else []
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


slr_role = Role(position=1)


@pytest.fixture
def guild():
    g = MagicMock()
    g.id = GUILD
    g.chunked = True
    g.members = {}
    g.get_member.side_effect = lambda discord_id: g.members.get(discord_id)
    g.get_role.side_effect = lambda role_id: slr_role if role_id == ROLE else None
    g.me.guild_permissions.manage_roles = True
    g.me.top_role = Role(position=5)
    return g


@pytest.fixture
def bot(guild):
    b = MagicMock()
    b.get_guild.side_effect = lambda guild_id: guild if guild_id == GUILD else None
    return b


def run(bot, data, role_id=ROLE, linked=(), monkeypatch=None):
    monkeypatch.setattr(slr, "read_links", lambda engine, guild_id, ids: (role_id, set(linked)))
    return asyncio.run(slr.apply_site_link_roles(bot, object(), data))


def test_an_account_with_a_kick_link_gets_the_role_and_one_without_loses_it(bot, guild, monkeypatch):
    alice, bob = member(1, has_role=False), member(2, has_role=True)
    guild.members = {1: alice, 2: bob}
    given, taken = run(
        bot, {"discord_server_id": GUILD, "added": ["1"], "removed": ["2"]}, linked={1}, monkeypatch=monkeypatch
    )
    assert (given, taken) == (1, 1)
    alice.add_roles.assert_awaited_once_with(slr_role, reason=slr.GIVE_REASON)
    bob.remove_roles.assert_awaited_once_with(slr_role, reason=slr.TAKE_REASON)


def test_the_links_decide_not_the_event(bot, guild, monkeypatch):
    """Unlinked by the site, but linked again through the panel meanwhile: keeps it."""
    bob = member(2, has_role=True)
    guild.members = {2: bob}
    assert run(bot, {"discord_server_id": GUILD, "removed": [2]}, linked={2}, monkeypatch=monkeypatch) == (0, 0)
    bob.remove_roles.assert_not_awaited()


def test_nobody_outside_the_server_is_touched(bot, guild, monkeypatch):
    guild.fetch_member = AsyncMock()
    assert run(bot, {"discord_server_id": GUILD, "added": ["9"]}, linked={9}, monkeypatch=monkeypatch) == (0, 0)
    guild.fetch_member.assert_not_awaited()  # a chunked server's cache is the answer


def test_a_server_not_yet_chunked_asks_discord(bot, guild, monkeypatch):
    guild.chunked = False
    alice = member(1, has_role=False)
    guild.fetch_member = AsyncMock(side_effect=lambda discord_id: alice if discord_id == 1 else None)
    assert run(bot, {"discord_server_id": GUILD, "added": [1]}, linked={1}, monkeypatch=monkeypatch) == (1, 0)


@pytest.mark.parametrize("role_id, manage, top", [(None, True, 5), (999, True, 5), (ROLE, False, 5), (ROLE, True, 1)])
def test_nothing_happens_without_a_role_the_bot_may_give(bot, guild, monkeypatch, role_id, manage, top):
    alice = member(1, has_role=False)
    guild.members = {1: alice}
    guild.me.guild_permissions.manage_roles = manage
    guild.me.top_role = Role(position=top)
    assert run(
        bot, {"discord_server_id": GUILD, "added": [1]}, role_id=role_id, linked={1}, monkeypatch=monkeypatch
    ) == (0, 0)
    alice.add_roles.assert_not_awaited()


def test_a_refused_role_change_does_not_stop_the_rest(bot, guild, monkeypatch):
    alice, cara = member(1, has_role=False), member(3, has_role=False)
    alice.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "Missing Permissions")
    guild.members = {1: alice, 3: cara}
    assert run(bot, {"discord_server_id": GUILD, "added": [1, 3]}, linked={1, 3}, monkeypatch=monkeypatch) == (1, 0)
    cara.add_roles.assert_awaited_once()


@pytest.mark.parametrize("data", [{}, {"discord_server_id": "x", "added": [1]}, {"discord_server_id": GUILD}, None])
def test_a_malformed_or_empty_event_does_nothing(bot, data, monkeypatch):
    assert run(bot, data, linked={1}, monkeypatch=monkeypatch) == (0, 0)


def test_the_read_counts_only_kick_links_in_this_server():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bot_settings (key TEXT, discord_server_id BIGINT, value TEXT)"))
        conn.execute(text("CREATE TABLE links (discord_id BIGINT, discord_server_id BIGINT, platform TEXT)"))
        conn.execute(text(f"INSERT INTO bot_settings VALUES ('kick_linked_role_id', {GUILD}, ' {ROLE} ')"))
        conn.execute(
            text(
                f"INSERT INTO links VALUES (1, {GUILD}, 'kick'), (2, {GUILD}, 'twitch'), (3, 99, 'kick'), (4, {GUILD}, NULL)"
            )
        )
    assert slr.read_links(engine, GUILD, [1, 2, 3, 4, 5]) == (ROLE, {1, 4})


def test_the_subscriber_hands_the_event_to_a_task(monkeypatch):
    import redis_subscriber as rs

    seen = []
    monkeypatch.setattr(slr, "schedule_site_link_roles", lambda bot, engine, data: seen.append(data))
    monkeypatch.setattr(rs, "get_engine", lambda: "engine")
    subscriber = rs.RedisSubscriber.__new__(rs.RedisSubscriber)
    subscriber.bot = MagicMock()
    data = {"discord_server_id": GUILD, "added": ["1"], "removed": []}
    asyncio.run(subscriber.handle_management_event("site_links_changed", data))
    assert seen == [data]
