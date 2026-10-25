import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from features.custom_commands import manager as manager_module
from features.custom_commands.manager import CustomCommandsManager
from features.points import points_grants
from features.points.points_grants import (
    credit_points,
    format_drop_announcement,
    is_chat_moderator,
    normalize_chat_name,
    parse_positive_int,
    parse_role_bonuses,
    resolve_points_target,
    role_bonus_for,
)

SERVER = 42


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE user_points (
                    kick_username TEXT NOT NULL,
                    discord_id BIGINT,
                    points INTEGER DEFAULT 0,
                    total_earned INTEGER DEFAULT 0,
                    total_spent INTEGER DEFAULT 0,
                    discord_server_id BIGINT NOT NULL,
                    last_updated TIMESTAMP,
                    UNIQUE (kick_username, discord_server_id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE links (
                    discord_id BIGINT, kick_name TEXT, platform TEXT,
                    discord_server_id BIGINT, linked_at TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE TABLE watchtime (username TEXT, minutes INTEGER, discord_server_id BIGINT)"))
    return eng


def test_moderator_badges_from_every_chat_path():
    assert is_chat_moderator(["Moderator"])  # kickpython badge texts
    assert is_chat_moderator(["Broadcaster", "Subscriber"])
    assert is_chat_moderator([{"type": "moderator", "text": "Moderator"}])  # Kick Pusher
    assert is_chat_moderator([{"set_id": "broadcaster", "id": "1"}])  # Twitch EventSub
    assert not is_chat_moderator(["VIP", "OG", "Subscriber"])
    assert not is_chat_moderator(None)


def test_chat_names_and_amounts():
    assert normalize_chat_name("@LuckyLena") == "luckylena"
    assert normalize_chat_name("@") == ""
    assert normalize_chat_name("bad name") == ""
    assert parse_positive_int("1,500", 10_000) == 1500
    assert parse_positive_int("0", 10) is None
    assert parse_positive_int("11", 10) is None
    assert parse_positive_int("abc", 10) is None


def test_role_bonuses_parse_and_pick_highest():
    bonuses = parse_role_bonuses(
        '[{"role_id": "10", "points": 2}, {"role_id": "20", "points": 5},'
        ' {"role_id": "10", "points": 3}, {"role_id": "x", "points": 1}, {"role_id": "30", "points": 0}]'
    )
    assert bonuses == {10: 3, 20: 5}
    assert parse_role_bonuses("not json") == {}

    member = SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=20), SimpleNamespace(id=99)])
    assert role_bonus_for(member, bonuses) == 5
    assert role_bonus_for(SimpleNamespace(roles=[SimpleNamespace(id=99)]), bonuses) == 0
    assert role_bonus_for(None, bonuses) == 0


def test_drop_announcement_fits_chat():
    assert format_drop_announcement(["a", "b"], 100) == "Points drop! 100 points each to: @a, @b"
    many = [f"viewer_with_a_long_name_{i}" for i in range(25)]
    line = format_drop_announcement(many, 1000)
    assert len(line) <= 500
    assert line.endswith("more")


def test_drop_chatter_window():
    now = datetime.now(timezone.utc)
    points_grants._drop_chatters.clear()
    points_grants.record_chatter(SERVER, "Fresh", now - timedelta(minutes=2))
    points_grants.record_chatter(SERVER, "older", now - timedelta(minutes=20))
    points_grants.record_chatter(SERVER, "gone", now - timedelta(minutes=90))
    assert set(points_grants.chatters_since(SERVER, 10, now)) == {"fresh"}
    assert set(points_grants.chatters_since(SERVER, 30, now)) == {"fresh", "older"}
    # Older than the longest window is pruned outright.
    assert "gone" not in points_grants._drop_chatters[SERVER]


def test_linked_target_credits_the_canonical_row(engine):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO links VALUES (7, 'zed_kick', 'kick', :s, NULL), (7, 'Alpha_tw', 'twitch', :s, NULL)"),
            {"s": SERVER},
        )
        target = resolve_points_target(conn, SERVER, "@Zed_Kick", "kick")
        assert target == ("alpha_tw", 7)
        assert credit_points(conn, SERVER, target[0], 50, target[1]) == 50
        assert credit_points(conn, SERVER, target[0], 25, target[1]) == 75


def test_unknown_unlinked_target_is_refused(engine):
    with engine.begin() as conn:
        assert resolve_points_target(conn, SERVER, "typo_name", "kick") is None
        # Known through watchtime, recent chat, or an existing points row.
        conn.execute(text("INSERT INTO watchtime VALUES ('watcher', 30, :s)"), {"s": SERVER})
        assert resolve_points_target(conn, SERVER, "watcher") == ("watcher", None)
        assert resolve_points_target(conn, SERVER, "chatter", recent_names=["chatter"]) == ("chatter", None)
        conn.execute(
            text("INSERT INTO user_points (kick_username, discord_server_id) VALUES ('Legacy', :s)"), {"s": SERVER}
        )
        assert resolve_points_target(conn, SERVER, "legacy") == ("Legacy", None)


def _manager(response, monkeypatch, granted=100):
    sent = []

    async def send(msg):
        sent.append(msg)

    mgr = CustomCommandsManager(bot=None, send_message_callback=send, discord_server_id=SERVER, engine=object())
    mgr.commands = {"gift": {"id": 1, "response": response, "cooldown": 0, "enabled": True, "use_count": 0}}
    calls = []

    def fake_give(engine, server_id, name, amount, platform, recent):
        calls.append((server_id, name, amount, platform))
        return granted

    async def no_increment(command_id):
        return None

    monkeypatch.setattr(manager_module, "give_points_to_chat_name", fake_give)
    monkeypatch.setattr(mgr, "_increment_use_count", no_increment)
    return mgr, sent, calls


def test_give_points_variable_grants_for_mods(monkeypatch):
    mgr, sent, calls = _manager("{display_name} gave {give_points} to {target} ({target_points})", monkeypatch, 1500)
    handled = asyncio.run(mgr.handle_message("!gift @Lena 500", "mod1", display_name="Mod1", is_moderator=True))
    assert handled
    assert calls == [(SERVER, "@Lena", 500, "kick")]
    assert sent == ["Mod1 gave 500 to Lena (1,500)"]


def test_give_points_fixed_amount(monkeypatch):
    mgr, sent, calls = _manager("+{give_points:250} for {target}", monkeypatch)
    asyncio.run(mgr.handle_message("!gift lena", "mod1", is_moderator=True))
    assert calls[0][2] == 250
    assert sent == ["+250 for lena"]


def test_give_points_refuses_viewers_and_bad_usage(monkeypatch):
    mgr, sent, calls = _manager("{give_points}", monkeypatch)
    asyncio.run(mgr.handle_message("!gift lena 500", "viewer", is_moderator=False))
    asyncio.run(mgr.handle_message("!gift lena", "mod1", is_moderator=True))
    assert calls == []
    # Viewers are ignored silently; a mod using it wrong gets the usage.
    assert sent == ["@mod1 usage: !gift @viewer amount"]


def test_give_points_unknown_viewer(monkeypatch):
    mgr, sent, _ = _manager("{give_points}", monkeypatch, granted=None)
    asyncio.run(mgr.handle_message("!gift nobody 5", "mod1", is_moderator=True))
    assert sent == ["@mod1 couldn't find a viewer named nobody."]


def test_names_resolve_only_through_their_own_platform(engine):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO links VALUES (8, 'bob', 'twitch', :s, NULL)"), {"s": SERVER})
        conn.execute(text("INSERT INTO watchtime VALUES ('bob', 30, :s)"), {"s": SERVER})
        # Typed in Kick chat, "bob" is not the Twitch-linked person, and the
        # bare-username fallback would land on that person's row, so it's refused.
        assert resolve_points_target(conn, SERVER, "bob", "kick") is None
        assert resolve_points_target(conn, SERVER, "bob", "twitch") == ("bob", 8)


def test_drop_rejects_out_of_range_amounts():
    with pytest.raises(ValueError):
        points_grants.run_points_drop(None, SERVER, ["a"], -5, 1, "dashboard")
    with pytest.raises(ValueError):
        points_grants.run_points_drop(None, SERVER, ["a"], points_grants.MAX_DROP_AMOUNT + 1, 1, "dashboard")
