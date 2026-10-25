"""
Whether a server's stream is live, recorded for the dashboard.

The dashboard's Timed Messages settings show it from these bot_settings rows
(Admin-Dashboard/utils/stream_state.py reads them, and only reads them):

    stream_live_kick    "true" / "false"
    stream_live_twitch  "true" / "false"

Written here from every place the bot learns the state:
  - the Kick webhook (core/oauth_server.py), online and offline;
  - the shared sender core/stream_notifications.py, which Twitch's
    stream.online/offline reach;
  - the 5-minute Kick poll in bot.py, so the Kick value stays right when
    webhooks don't arrive (from 2026-10-01 they all failed verification).

A missing row reads as "not known yet" on the dashboard. A write that fails is
logged and dropped: the go-live alert and the poll carry on regardless.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

LIVE_SETTING_KEYS = {"kick": "stream_live_kick", "twitch": "stream_live_twitch"}

_UPSERT = """
    INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
    VALUES (:key, :value, :sid, CURRENT_TIMESTAMP)
    ON CONFLICT (key, discord_server_id)
    DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
"""


def record_stream_live(engine, discord_server_id, platform, is_live) -> bool:
    """Store whether `platform`'s stream is live for the server. True when written."""
    key = LIVE_SETTING_KEYS.get(str(platform or "").lower())
    if not key or engine is None:
        return False
    try:
        server_id = int(discord_server_id)
    except (TypeError, ValueError):
        return False
    if not server_id:  # 0 is the global-settings sentinel, never a server
        return False
    try:
        with engine.begin() as conn:
            conn.execute(
                text(_UPSERT),
                # bot_settings.discord_server_id is BIGINT: bind an int.
                {"key": key, "value": "true" if is_live else "false", "sid": server_id},
            )
        return True
    except Exception as e:
        logger.warning(f"[StreamState] ⚠️ Could not record {key}={bool(is_live)} for server {server_id}: {e}")
        return False
