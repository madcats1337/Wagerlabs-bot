"""The live state the dashboard's Timed Messages page reads.

Admin-Dashboard/utils/stream_state.py reads `stream_live_kick` and
`stream_live_twitch` from bot_settings ("true" / "false"); nothing used to
write them, so the page always said the state was unknown.
"""

import asyncio

import pytest
from sqlalchemy import create_engine, text

import core.stream_notifications as notifications
import utils.stream_state as stream_state
from utils.stream_state import record_stream_live

SERVER = 914986636629143562


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE bot_settings (
                    key TEXT NOT NULL, value TEXT, discord_server_id BIGINT NOT NULL,
                    updated_at TIMESTAMP, UNIQUE (key, discord_server_id)
                )
                """
            )
        )
    return eng


def settings(engine):
    with engine.connect() as conn:
        return {
            (row.key, row.discord_server_id): row.value
            for row in conn.execute(text("SELECT key, value, discord_server_id FROM bot_settings"))
        }


def test_each_platform_writes_the_key_the_dashboard_reads(engine):
    assert record_stream_live(engine, SERVER, "kick", True)
    assert record_stream_live(engine, str(SERVER), "Twitch", False)
    assert settings(engine) == {
        ("stream_live_kick", SERVER): "true",
        ("stream_live_twitch", SERVER): "false",
    }


def test_going_offline_overwrites_live(engine):
    record_stream_live(engine, SERVER, "kick", True)
    record_stream_live(engine, SERVER, "kick", False)
    assert settings(engine) == {("stream_live_kick", SERVER): "false"}


@pytest.mark.parametrize(
    "server, platform",
    [(SERVER, "youtube"), (SERVER, None), (0, "kick"), (None, "kick"), ("not-a-server", "kick")],
)
def test_nothing_is_written_for_an_unknown_platform_or_server(engine, server, platform):
    assert not record_stream_live(engine, server, platform, True)
    assert settings(engine) == {}


def test_a_database_error_is_logged_not_raised():
    eng = create_engine("sqlite://")  # no bot_settings table
    assert record_stream_live(eng, SERVER, "kick", True) is False


@pytest.mark.parametrize("is_live", [True, False])
def test_twitch_online_and_offline_record_the_state(monkeypatch, is_live):
    # Twitch's stream.online/offline reach the shared sender, which records the
    # state before anything else, whether or not the alert then posts.
    recorded = []
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(
        stream_state, "record_stream_live", lambda eng, sid, platform, live: recorded.append((sid, platform, live))
    )

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(notifications, "_post_discord_notification", nothing)
    monkeypatch.setattr(notifications, "_control_clip_buffer", nothing)

    class Publisher:
        def publish_stream_live(self, **kwargs):
            pass

        def publish_stream_offline(self, **kwargs):
            pass

    import utils.redis_publisher as redis_publisher

    monkeypatch.setattr(redis_publisher, "bot_redis_publisher", Publisher())

    asyncio.run(notifications.send_stream_notification(SERVER, "nickcleveland", is_live, platform="twitch"))
    assert recorded == [(SERVER, "twitch", is_live)]
