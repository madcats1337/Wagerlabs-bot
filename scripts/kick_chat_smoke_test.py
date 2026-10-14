"""
Kick Chat Smoke Test using kickpython

Usage:
  python scripts/kick_chat_smoke_test.py --channel <kick_username> --message "hello world"

Requirements:
  - env KICK_BOT_USER_TOKEN set (chat:write scope)
  - env KICK_CLIENT_ID, KICK_CLIENT_SECRET, OAUTH_BASE_URL set

If --channel is omitted, uses env KICK_CHANNEL.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

try:
    from kickpython import KickAPI
except Exception as e:
    print(f"kickpython not installed or import failed: {e}")
    sys.exit(1)

# kickpython's own connect_to_chatroom builds in the Pusher key Kick retired; this one asks Kick for its current app.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.kick_chat_socket import connect_to_chatroom  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", dest="channel", default=os.getenv("KICK_CHANNEL"))
    parser.add_argument("--message", dest="message", default="smoke test")
    parser.add_argument("--timeout", dest="timeout", type=int, default=10)
    args = parser.parse_args()

    if not args.channel:
        print("--channel or env KICK_CHANNEL required")
        sys.exit(2)

    token = os.getenv("KICK_BOT_USER_TOKEN")
    cid = os.getenv("KICK_CLIENT_ID")
    csec = os.getenv("KICK_CLIENT_SECRET")
    base = os.getenv("OAUTH_BASE_URL", "")

    if not token:
        print("KICK_BOT_USER_TOKEN env is required")
        sys.exit(2)

    api = KickAPI(client_id=cid, client_secret=csec, redirect_uri=f"{base}/auth/kick/callback" if base else None)
    api.access_token = token

    got_message = asyncio.Event()

    async def on_msg(msg: dict):
        ts = datetime.now(timezone.utc).isoformat()
        print(f"[{ts}] recv: {msg.get('sender_username')}: {msg.get('content')}")
        # If we see our message content, set event (best-effort)
        if args.message in str(msg.get("content", "")):
            got_message.set()

    api.add_message_handler(on_msg)

    print(f"Connecting to channel chat: {args.channel} ...")
    # Start connection in background
    connect_task = asyncio.create_task(connect_to_chatroom(api, args.channel))

    # Give it a moment to connect
    await asyncio.sleep(2)

    # Try to send a message (needs chatroom_id resolved inside kickpython)
    if hasattr(api, "chatroom_id") and api.chatroom_id:
        try:
            print(f"Sending message to chatroom {api.chatroom_id} ...")
            await api.post_chat(channel_id=api.chatroom_id, content=args.message)
            print("send ok")
        except Exception as e:
            print(f"send failed: {e}")
    else:
        print("chatroom_id not yet available; will just listen for messages")

    try:
        await asyncio.wait_for(got_message.wait(), timeout=args.timeout)
        print("Observed our message in chat (likely).")
    except asyncio.TimeoutError:
        print("Timeout waiting for echo; listener is still likely okay if public chat is active.")

    # Cancel connection and exit
    connect_task.cancel()
    try:
        await connect_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())
