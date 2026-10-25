"""Giveaway entry lists: chat giveaways open to the viewers a website vouches for.

A site replaces a list through the dashboard's public API; a chat giveaway names
it in `entry_list`. With a Discord role as well, either one lets a chatter in.
Kick chat carries only the sender's username, so Kick members match by name;
an account id, when the chat event has one, outranks a name.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import create_engine, text

from features.giveaway import entry_lists
from features.giveaway import giveaway_manager as gm
from features.giveaway.entry_lists import chat_requirement, on_entry_list, refresh_list_names
from features.giveaway.giveaway_manager import GiveawayManager

SERVER = 42
OTHER_SERVER = 43
ROLE = 7001
DISCORD_ID = 900001


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE giveaways (
                    id INTEGER PRIMARY KEY, discord_server_id BIGINT, title TEXT,
                    entry_method TEXT, keyword TEXT, status TEXT, required_role_id BIGINT, entry_list TEXT,
                    allow_multiple_entries BOOLEAN DEFAULT 0, max_entries_per_user INTEGER DEFAULT 1
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE giveaway_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, giveaway_id INTEGER, discord_server_id BIGINT,
                    discord_id BIGINT, kick_username TEXT, kick_user_id INTEGER, entry_method TEXT,
                    entry_count INTEGER DEFAULT 1, profile_pic_url TEXT, display_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE links (
                    discord_id BIGINT, kick_name TEXT, discord_server_id BIGINT, platform TEXT,
                    platform_user_id TEXT, linked_at TIMESTAMP
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE giveaway_lists (
                    id INTEGER PRIMARY KEY, discord_server_id BIGINT, list_key TEXT, name TEXT, hint TEXT,
                    member_count INTEGER DEFAULT 0
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE giveaway_list_members (
                    list_id INTEGER, member_no INTEGER, platform TEXT, platform_user_id TEXT,
                    username_key TEXT, display_name TEXT
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE giveaway_list_discord_names (
                    discord_server_id BIGINT, discord_id TEXT, display_name TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (discord_server_id, discord_id)
                )
                """
            )
        )
    return eng


def make_list(engine, key="affiliates", server=SERVER, members=(), name="Verified affiliates", hint=None):
    """`members`: (platform, platform_user_id, username) tuples, one person each."""
    with engine.begin() as conn:
        list_id = conn.execute(
            text(
                "INSERT INTO giveaway_lists (discord_server_id, list_key, name, hint) VALUES (:s, :k, :n, :h) RETURNING id"
            ),
            {"s": server, "k": key, "n": name, "h": hint},
        ).scalar()
        for n, (platform, uid, username) in enumerate(members):
            conn.execute(
                text(
                    "INSERT INTO giveaway_list_members (list_id, member_no, platform, platform_user_id, username_key, "
                    "display_name) VALUES (:l, :n, :p, :u, :k, :d)"
                ),
                {
                    "l": list_id,
                    "n": n,
                    "p": platform,
                    "u": uid,
                    "k": username.lower() if username else None,
                    "d": username,
                },
            )


def add_giveaway(engine, gid, role_id=None, entry_list=None):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO giveaways (id, discord_server_id, title, entry_method, keyword, status, required_role_id, "
                "entry_list) VALUES (:id, :sid, :title, 'keyword', '!aff', 'active', :role, :list)"
            ),
            {"id": gid, "sid": SERVER, "title": f"Giveaway {gid}", "role": role_id, "list": entry_list},
        )
    return {"id": gid, "entry_method": "keyword", "keyword": "!aff"}


def link(engine, handle, platform="kick", discord_id=DISCORD_ID):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO links (discord_id, kick_name, discord_server_id, platform) VALUES (:d, :u, :s, :p)"),
            {"d": discord_id, "u": handle, "s": SERVER, "p": platform},
        )


def entrants(engine, gid):
    with engine.connect() as conn:
        return [
            tuple(r)
            for r in conn.execute(
                text("SELECT kick_username, discord_id FROM giveaway_entries WHERE giveaway_id = :g ORDER BY id"),
                {"g": gid},
            )
        ]


class FakeGuild:
    def __init__(self, members=None, roles=None):
        self.members = members or {}
        self.roles = roles or {}
        self.lookups = []
        self.fetches = 0

    def get_member(self, discord_id):
        self.lookups.append(discord_id)
        return self.members.get(discord_id)

    async def fetch_member(self, discord_id):
        self.fetches += 1
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")

    def get_role(self, role_id):
        return self.roles.get(role_id)


def manager(engine, guild, monkeypatch):
    m = GiveawayManager(engine, guild_id=SERVER, bot=SimpleNamespace(get_guild=lambda gid: guild))

    async def no_avatar(username, platform="kick"):
        return None

    monkeypatch.setattr(m, "_fetch_avatar", no_avatar)
    return m


def enter(m, giveaway, username="viewer", platform="kick", platform_user_id=None):
    return asyncio.run(m.add_entry(username, platform=platform, giveaway=giveaway, platform_user_id=platform_user_id))


# ---------------------------------------------------------------------------
# Who is on a list
# ---------------------------------------------------------------------------
def test_kick_members_match_by_username_since_kick_chat_carries_no_id(engine):
    make_list(engine, members=[("kick", "123", "NickFan")])
    assert on_entry_list(engine, SERVER, "affiliates", "kick", "nickfan")
    assert not on_entry_list(engine, SERVER, "affiliates", "kick", "someoneelse")


def test_an_account_id_outranks_a_username(engine):
    make_list(engine, members=[("twitch", "55", "streamfan")])
    assert on_entry_list(engine, SERVER, "affiliates", "twitch", "renamed", "55")
    assert not on_entry_list(
        engine, SERVER, "affiliates", "twitch", "streamfan", "66"
    ), "the name now belongs to a different account"


def test_a_member_sent_without_an_id_matches_by_name_even_from_an_id_carrying_chat(engine):
    make_list(engine, members=[("twitch", None, "streamfan")])
    assert on_entry_list(engine, SERVER, "affiliates", "twitch", "StreamFan", "66")


def test_a_discord_member_matches_the_account_they_linked(engine):
    make_list(engine, members=[("discord", str(DISCORD_ID), None)])
    assert on_entry_list(engine, SERVER, "affiliates", "kick", None, discord_id=DISCORD_ID)
    assert not on_entry_list(engine, SERVER, "affiliates", "kick", "viewer")


def test_a_list_counts_only_on_its_own_platform_server_and_key(engine):
    make_list(engine, members=[("kick", None, "viewer")])
    make_list(engine, key="others", server=OTHER_SERVER, members=[("kick", None, "stranger")])
    assert not on_entry_list(engine, SERVER, "affiliates", "twitch", "viewer")
    assert not on_entry_list(engine, OTHER_SERVER, "affiliates", "kick", "viewer")
    assert not on_entry_list(engine, SERVER, "others", "kick", "stranger")
    assert not on_entry_list(engine, SERVER, "missing", "kick", "viewer"), "a missing list matches nobody"


# ---------------------------------------------------------------------------
# Entering a gated giveaway
# ---------------------------------------------------------------------------
def test_a_list_only_giveaway_takes_whoever_is_on_the_list(engine, monkeypatch):
    make_list(engine, members=[("kick", "1", "viewer")])
    g = add_giveaway(engine, 1, entry_list="affiliates")
    m = manager(engine, FakeGuild(), monkeypatch)

    assert enter(m, g, username="viewer")
    outcome = enter(m, g, username="stranger")
    assert (outcome.refusal, outcome.list_key, outcome.role_id) == (gm.NOT_ON_LIST, "affiliates", None)
    assert entrants(engine, 1) == [("viewer", None)]


def test_a_list_member_known_only_by_discord_gets_in_through_their_link(engine, monkeypatch):
    make_list(engine, members=[("discord", str(DISCORD_ID), None)])
    link(engine, "viewer")
    g = add_giveaway(engine, 1, entry_list="affiliates")
    assert enter(manager(engine, FakeGuild(), monkeypatch), g, username="viewer")
    assert entrants(engine, 1) == [("viewer", DISCORD_ID)], "the link's Discord id rides on the entry"


def test_with_a_role_and_a_list_either_one_gets_a_chatter_in(engine, monkeypatch):
    make_list(engine, members=[("kick", None, "affiliate")])
    link(engine, "rolemember")
    g = add_giveaway(engine, 1, role_id=ROLE, entry_list="affiliates")
    guild = FakeGuild({DISCORD_ID: SimpleNamespace(roles=[SimpleNamespace(id=ROLE)])})
    m = manager(engine, guild, monkeypatch)

    assert enter(m, g, username="affiliate"), "on the list, no role needed"
    assert guild.lookups == [], "the list is asked before Discord"
    assert enter(m, g, username="rolemember"), "the role, no list needed"
    outcome = enter(m, g, username="stranger")
    assert (outcome.refusal, outcome.role_id, outcome.list_key) == (gm.NOT_LINKED, ROLE, "affiliates")


def test_the_list_is_read_from_the_row_on_every_attempt(engine, monkeypatch):
    make_list(engine, members=[("kick", None, "viewer")])
    g = add_giveaway(engine, 1)
    m = manager(engine, FakeGuild(), monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("UPDATE giveaways SET entry_list = 'affiliates' WHERE id = 1"))
    assert enter(m, g, username="stranger").refusal == gm.NOT_ON_LIST, "a live edit applies to the next message"
    assert enter(m, g, username="viewer")


# ---------------------------------------------------------------------------
# What chat is told
# ---------------------------------------------------------------------------
def test_the_requirement_reads_as_either_one(engine):
    make_list(engine, hint="Verify your Stake account at nickcleveland.com to enter.")
    guild = FakeGuild(roles={ROLE: SimpleNamespace(name="Affiliate")})

    who, hint = chat_requirement(engine, guild, SERVER, ROLE, "affiliates")
    assert who == "Verified affiliates or members with the Affiliate role in Discord"
    assert hint == "Verify your Stake account at nickcleveland.com to enter."

    who, _ = chat_requirement(engine, guild, SERVER, ROLE, "affiliates", mention_role=True)
    assert who == f"Verified affiliates or members with <@&{ROLE}>"
    assert chat_requirement(engine, guild, SERVER, None, None) == (None, None)
    assert chat_requirement(engine, guild, SERVER, None, "missing") == ("the giveaway's entry list", None)


# ---------------------------------------------------------------------------
# Names for the Discord ids on a list (the dashboard's Entry lists tab)
# ---------------------------------------------------------------------------
class NamesBot:
    """The bot's user cache, and Discord's API behind fetch_user."""

    def __init__(self, users=None, api=None):
        self.users = users or {}
        self.api = api or {}
        self.fetched = []

    def get_user(self, discord_id):
        return self.users.get(discord_id)

    async def fetch_user(self, discord_id):
        self.fetched.append(discord_id)
        if discord_id not in self.api:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown User")
        return self.api[discord_id]


def person(global_name, name):
    return SimpleNamespace(global_name=global_name, name=name)


def guild_with(members=None, server=SERVER):
    guild = FakeGuild(members=members)
    guild.id = server
    return guild


def names(engine, server=SERVER):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT discord_id, display_name FROM giveaway_list_discord_names WHERE discord_server_id = :s"),
                {"s": server},
            ).fetchall()
        )


def refresh(engine, guild, bot):
    return asyncio.run(refresh_list_names(bot, engine, guild))


def test_a_discord_member_on_a_list_is_named_from_the_member_cache(engine):
    make_list(engine, members=[("discord", "111", None), ("discord", "222", None), ("kick", "5", "kickguy")])
    guild = guild_with({111: person("Nick Fan", "nickfan")})
    bot = NamesBot(users={222: person(None, "plain_name")})

    assert refresh(engine, guild, bot) == 2
    # Display name = global_name, else the account username.
    assert names(engine) == {"111": "Nick Fan", "222": "plain_name"}
    assert bot.fetched == []


def test_someone_no_cache_holds_is_fetched_once(engine):
    make_list(engine, members=[("discord", "333", None), ("discord", "444", None)])
    bot = NamesBot(api={333: person("Fetched", "fetched")})

    refresh(engine, guild_with(), bot)
    assert names(engine) == {"333": "Fetched"} and sorted(bot.fetched) == [333, 444]
    # 333 has a name now: only the unknown one is asked about again.
    refresh(engine, guild_with(), bot)
    assert sorted(bot.fetched) == [333, 444, 444]


def test_lookups_through_the_api_are_capped_per_refresh(engine, monkeypatch):
    monkeypatch.setattr(entry_lists, "NAME_FETCHES_PER_REFRESH", 2)
    make_list(engine, members=[("discord", str(500 + i), None) for i in range(5)])
    bot = NamesBot()
    refresh(engine, guild_with(), bot)
    assert len(bot.fetched) == 2


def test_a_rename_is_picked_up_and_someone_off_every_list_is_forgotten(engine):
    make_list(engine, members=[("discord", "111", None), ("discord", "222", None)])
    make_list(engine, key="theirs", server=OTHER_SERVER, members=[("discord", "111", None)])
    guild = guild_with({111: person("Old", "x"), 222: person("Stays", "y")})
    refresh(engine, guild, NamesBot())
    refresh(engine, guild_with({111: person("Theirs", "x")}, server=OTHER_SERVER), NamesBot())

    guild.members[111] = person("New", "x")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM giveaway_list_members WHERE platform_user_id = '222'"))
    assert refresh(engine, guild, NamesBot()) == 1
    assert names(engine) == {"111": "New"}
    # Another server's names are its own.
    assert names(engine, OTHER_SERVER) == {"111": "Theirs"}


def test_an_unchanged_name_is_not_written_again(engine):
    make_list(engine, members=[("discord", "111", None)])
    guild = guild_with({111: person("Same", "s")})
    assert refresh(engine, guild, NamesBot()) == 1
    assert refresh(engine, guild, NamesBot()) == 0
