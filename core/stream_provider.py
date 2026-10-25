"""
Stream-platform adapter glue.

The Twitch EventSub webhook runs in the Gunicorn OAuth-server process, which is
SEPARATE from the Discord bot process (see combined_server.py). So a Twitch
channel.chat.message cannot call the bot's in-memory chat handler directly — it is
forwarded to the bot over Redis (`bot_events` channel), exactly like the legacy
Kick chat-webhook path. The bot's redis_subscriber picks it up and feeds it into
the existing KickWebSocketManager._handle_incoming_message so ALL downstream
features (watchtime, points, !commands, bonus hunt, slot requests, GTB) work for
Twitch with no logic duplication.

normalize_twitch_chat_event() converts a Twitch channel.chat.message payload into
the same `msg` dict shape that _handle_incoming_message consumes.
"""

import json
import logging
import os

from utils.redis_signing import sign_payload

logger = logging.getLogger(__name__)


def normalize_twitch_chat_event(event: dict) -> dict:
    """
    Map a Twitch `channel.chat.message` EventSub payload to the bot's chat `msg`
    shape (same keys _handle_incoming_message reads: sender_username/username,
    content, chat_id/id).

    Twitch payload shape (event):
      {
        "broadcaster_user_id", "broadcaster_user_login", "broadcaster_user_name",
        "chatter_user_id", "chatter_user_login", "chatter_user_name",
        "message_id", "message": {"text": "..."}, ...
      }
    """
    chatter_login = event.get("chatter_user_login") or event.get("chatter_user_name") or "unknown"
    content = ""
    msg_obj = event.get("message")
    if isinstance(msg_obj, dict):
        content = msg_obj.get("text", "") or ""
    elif isinstance(msg_obj, str):
        content = msg_obj
    return {
        "sender_username": chatter_login,
        "username": chatter_login,
        "content": content,
        "chat_id": event.get("message_id"),
        "id": event.get("message_id"),
        "user": {
            "id": event.get("chatter_user_id"),
            "username": chatter_login,
            "slug": chatter_login,
        },
        # set_ids ("moderator", "broadcaster", ...) — what mod-only chat
        # commands check, alongside Kick's badge texts.
        "badges": [b.get("set_id") for b in (event.get("badges") or []) if isinstance(b, dict)],
        "_platform": "twitch",
        "_server_id": event.get("_server_id"),
        "_broadcaster_user_id": event.get("_broadcaster_user_id"),
    }


async def dispatch_twitch_chat_message(event: dict):
    """
    Forward a normalized Twitch chat message to the bot process via Redis.
    Runs in the webhook (Gunicorn) process — no access to the bot's memory.
    """
    try:
        import redis

        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            logger.info("[Twitch Chat] ⚠️ REDIS_URL not set, cannot forward chat to bot")
            return
        if "://" not in redis_url:
            redis_url = f"redis://{redis_url}"

        msg = normalize_twitch_chat_event(event)
        payload = {
            "type": "twitch_chat_message",
            "data": {
                "_server_id": event.get("_server_id"),
                "_broadcaster_user_id": event.get("_broadcaster_user_id"),
                "msg": msg,
            },
        }
        client = redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        client.publish("bot_events", json.dumps(sign_payload(payload)))
        logger.info(f"[Twitch Chat] ✅ Forwarded {msg.get('username')} message to bot via Redis")
    except Exception as e:
        logger.info(f"[Twitch Chat] ❌ Failed to forward chat: {e}")
