"""Role-gated chat giveaways: who a chatter is on Discord, and what an entry does.

A keyword or active-chatter giveaway can require a Discord role. The chatter's
linked Discord account must hold it in the server, checked against the row on
every attempt, so a live edit of the requirement applies to the next message.
"""

import asyncio
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import create_engine, text

from core.stream_links import resolve_chatter_discord_id
from features.giveaway import giveaway_manager as gm
from features.giveaway.giveaway_manager import GiveawayManager

SERVER = 42
ROLE = 7001
OTHER_ROLE = 7002
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
                    entry_method TEXT, keyword TEXT, status TEXT, required_role_id BIGINT,
                    allow_multiple_entries BOOLEAN DEFAULT 0, max_entries_per_user INTEGER DEFAULT 1,
                    messages_required INTEGER DEFAULT 1, time_window_minutes INTEGER DEFAULT 10
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
    return eng


def add_giveaway(engine, gid, role_id=None, status="active", keyword="!join"):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO giveaways (id, discord_server_id, title, entry_method, keyword, status, required_role_id) "
                "VALUES (:id, :sid, :title, 'keyword', :kw, :status, :role)"
            ),
            {"id": gid, "sid": SERVER, "title": f"Giveaway {gid}", "kw": keyword, "status": status, "role": role_id},
        )
    return {"id": gid, "entry_method": "keyword", "keyword": keyword}


def link(engine, handle, platform="kick", platform_user_id=None, discord_id=DISCORD_ID):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO links (discord_id, kick_name, discord_server_id, platform, platform_user_id) "
                "VALUES (:d, :u, :sid, :p, :puid)"
            ),
            {"d": discord_id, "u": handle, "sid": SERVER, "p": platform, "puid": platform_user_id},
        )


def entries(engine, gid):
    with engine.connect() as conn:
        return [
            dict(r._mapping)
            for r in conn.execute(
                text("SELECT kick_username, discord_id, entry_count FROM giveaway_entries WHERE giveaway_id = :g"),
                {"g": gid},
            )
        ]


class FakeGuild:
    """A guild whose member cache holds `members`; anyone else 404s on fetch."""

    def __init__(self, members=None, fetch_error=None):
        self.members = members or {}
        self.fetch_error = fetch_error
        self.fetches = 0

    def get_member(self, discord_id):
        return self.members.get(discord_id)

    async def fetch_member(self, discord_id):
        self.fetches += 1
        if self.fetch_error:
            raise self.fetch_error
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")


def member(*role_ids):
    return SimpleNamespace(roles=[SimpleNamespace(id=r) for r in role_ids])


def manager(engine, guild, monkeypatch):
    m = GiveawayManager(engine, guild_id=SERVER, bot=SimpleNamespace(get_guild=lambda gid: guild))
    avatars = []

    async def no_avatar(username, platform="kick"):
        avatars.append(username)
        return None

    monkeypatch.setattr(m, "_fetch_avatar", no_avatar)
    m.avatar_lookups = avatars
    return m


def enter(m, giveaway, username="viewer", platform="kick", platform_user_id=None, display_name=None):
    return asyncio.run(
        m.add_entry(
            username,
            platform=platform,
            display_name=display_name,
            giveaway=giveaway,
            platform_user_id=platform_user_id,
        )
    )


# ---------------------------------------------------------------------------
# Who a chatter is on Discord
# ---------------------------------------------------------------------------
def test_the_immutable_platform_id_wins_over_the_handle(engine):
    link(engine, "oldname", platform_user_id="555")
    assert resolve_chatter_discord_id(engine, SERVER, "kick", "renamed", "555") == DISCORD_ID


def test_a_handle_claimed_by_someone_else_is_not_them(engine):
    link(engine, "viewer", platform_user_id="555")
    assert resolve_chatter_discord_id(engine, SERVER, "kick", "viewer", "999") is None


def test_without_an_id_the_handle_resolves(engine):
    # Kick today: kickpython hands the bot only the sender's username.
    link(engine, "viewer")
    assert resolve_chatter_discord_id(engine, SERVER, "kick", "Viewer", None) == DISCORD_ID


def test_with_an_id_only_a_row_carrying_it_counts(engine):
    # A legacy row (no id) could be a handle someone renamed away from. The
    # chat handler's backfill stamps the id onto the chatter's own row first,
    # and from then on it matches.
    link(engine, "viewer")
    assert resolve_chatter_discord_id(engine, SERVER, "twitch", "viewer", "555") is None
    assert resolve_chatter_discord_id(engine, SERVER, "kick", "viewer", "555") is None
    with engine.begin() as conn:
        conn.execute(text("UPDATE links SET platform_user_id = '555'"))
    assert resolve_chatter_discord_id(engine, SERVER, "kick", "viewer", "555") == DISCORD_ID


def test_a_link_counts_only_on_its_own_platform(engine):
    link(engine, "viewer", platform="twitch")
    assert resolve_chatter_discord_id(engine, SERVER, "kick", "viewer") is None
    assert resolve_chatter_discord_id(engine, SERVER, "twitch", "viewer") == DISCORD_ID


# ---------------------------------------------------------------------------
# Entering a role-only chat giveaway
# ---------------------------------------------------------------------------
def test_an_open_giveaway_enters_anyone(engine, monkeypatch):
    g = add_giveaway(engine, 1)
    outcome = enter(manager(engine, FakeGuild(), monkeypatch), g)
    assert outcome and outcome.refusal is None
    assert entries(engine, 1) == [{"kick_username": "viewer", "discord_id": None, "entry_count": 1}]


def test_an_unlinked_chatter_is_turned_away(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    m = manager(engine, FakeGuild(), monkeypatch)
    outcome = enter(m, g)
    assert not outcome
    assert (outcome.refusal, outcome.role_id) == (gm.NOT_LINKED, ROLE)
    assert entries(engine, 1) == []
    assert m.avatar_lookups == [], "nothing is looked up for someone who isn't entered"


def test_a_member_without_the_role_is_turned_away(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    link(engine, "viewer")
    outcome = enter(manager(engine, FakeGuild({DISCORD_ID: member(OTHER_ROLE)}), monkeypatch), g)
    assert outcome.refusal == gm.MISSING_ROLE
    assert entries(engine, 1) == []


def test_a_member_with_the_role_is_entered_with_their_discord_id(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    link(engine, "viewer", platform_user_id="555")
    outcome = enter(
        manager(engine, FakeGuild({DISCORD_ID: member(OTHER_ROLE, ROLE)}), monkeypatch), g, platform_user_id="555"
    )
    assert outcome
    # Carried onto the winner so the announcement can @mention them.
    assert entries(engine, 1) == [{"kick_username": "viewer", "discord_id": DISCORD_ID, "entry_count": 1}]


def test_twitch_chatters_are_checked_under_their_twitch_name(engine, monkeypatch):
    # Identity crediting remaps a dual-linked Twitch chatter to their canonical
    # (Kick) name; the gate must look up the name they chat under on Twitch.
    g = add_giveaway(engine, 1, role_id=ROLE)
    link(engine, "kickname", platform="kick")
    link(engine, "TwitchName", platform="twitch")
    outcome = enter(
        manager(engine, FakeGuild({DISCORD_ID: member(ROLE)}), monkeypatch),
        g,
        username="kickname",
        platform="twitch",
        display_name="TwitchName",
    )
    assert outcome
    assert entries(engine, 1)[0]["kick_username"] == "kickname", "the canonical name stays the dedup key"


def test_a_linked_chatter_who_left_the_server_is_looked_up_once(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    link(engine, "viewer")
    guild = FakeGuild()
    m = manager(engine, guild, monkeypatch)
    assert enter(m, g).refusal == gm.NOT_MEMBER
    assert enter(m, g).refusal == gm.NOT_MEMBER
    assert guild.fetches == 1, "the 404 is remembered, so spamming the keyword doesn't spend the rate limit"


def test_discord_being_unreachable_refuses_rather_than_lets_everyone_in(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    link(engine, "viewer")
    error = discord.HTTPException(SimpleNamespace(status=503, reason="Service Unavailable"), "down")
    outcome = enter(manager(engine, FakeGuild(fetch_error=error), monkeypatch), g)
    assert outcome.refusal == gm.GATE_UNAVAILABLE
    assert entries(engine, 1) == []


def test_the_requirement_is_read_from_the_row_on_every_attempt(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    m = manager(engine, FakeGuild(), monkeypatch)
    assert enter(m, g).refusal == gm.NOT_LINKED
    with engine.begin() as conn:
        conn.execute(text("UPDATE giveaways SET required_role_id = NULL WHERE id = 1"))
    assert enter(m, g), "a live edit removing the role opens it on the next message"


def test_someone_already_in_is_turned_away_before_any_lookup(engine, monkeypatch):
    g = add_giveaway(engine, 1, role_id=ROLE)
    link(engine, "viewer")
    guild = FakeGuild({DISCORD_ID: member(ROLE)})
    m = manager(engine, guild, monkeypatch)
    assert enter(m, g)
    calls = []
    monkeypatch.setattr(m, "_role_gate", lambda *a: calls.append(a))
    outcome = enter(m, g)
    assert outcome.refusal == gm.ALREADY_ENTERED
    assert calls == [] and m.avatar_lookups == ["viewer"]
    assert entries(engine, 1)[0]["entry_count"] == 1


def test_a_giveaway_that_stopped_takes_no_entries_and_leaves_the_cache(engine, monkeypatch):
    g = add_giveaway(engine, 1, status="ended")
    m = manager(engine, FakeGuild(), monkeypatch)
    m.active_giveaways = [g]
    outcome = enter(m, g)
    assert outcome.refusal == gm.CLOSED
    assert m.active_giveaways == [] and entries(engine, 1) == []


def test_two_chat_giveaways_take_entries_side_by_side(engine, monkeypatch):
    # One open to everyone, one for a role: same keyword, separate pools.
    everyone = add_giveaway(engine, 1)
    members_only = add_giveaway(engine, 2, role_id=ROLE)
    link(engine, "member")
    m = manager(engine, FakeGuild({DISCORD_ID: member(ROLE)}), monkeypatch)

    assert enter(m, everyone, username="member") and enter(m, members_only, username="member")
    assert enter(m, everyone, username="stranger")
    assert enter(m, members_only, username="stranger").refusal == gm.NOT_LINKED

    assert [e["kick_username"] for e in entries(engine, 1)] == ["member", "stranger"]
    assert [e["kick_username"] for e in entries(engine, 2)] == ["member"]
