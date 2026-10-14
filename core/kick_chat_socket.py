"""Kick's chat socket, opened with the Pusher app Kick's own web client is given.

Kick's chat is Pusher's, under an app key that is Kick's to change. It changed it
in October 2026: the key this bot connected with (32cbd69e4b950bf97679, which
kickpython 0.1.5 also builds in) began turning every new socket away with 4001
"App key ... not in this cluster", while sockets already open carried on. So a
bot that was already running kept reading chat, and would have lost Kick chat for
every streamer on its next restart, with nothing in the logs but reconnects.

kick.com's own client is told the key when it connects, by Kick's realtime
endpoint, and pusher_app() asks it the same way: it keeps the answer an hour,
falls back to the key Kick gave on 2026-10-14 when Kick can't be asked, and asks
again early when a socket is refused with the key it holds.

connect_to_chatroom() is kickpython's KickAPI.connect_to_chatroom with that one
change. Messages still go through kickpython's own handler, so what the bot's
handlers receive is unchanged. It also answers Pusher's pings, which kickpython
doesn't.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Optional, Tuple

import websockets

logger = logging.getLogger(__name__)

#: The app Kick named on 2026-10-14, for when it can't be asked.
FALLBACK_APP: Tuple[str, str] = ("34bf7a0ff419a2a775b9", "us2")
REALTIME_URL = "https://web.kick.com/api/v1/realtime/connection"
#: A found app is kept an hour; a failed ask is tried again after five minutes;
#: a refused key makes Kick be asked early, but not more than once a minute.
APP_TTL_S = 3600
RETRY_S = 300
FORCE_S = 60
#: The key goes into the socket's URL path and the cluster into its host name.
KEY_RE = re.compile(r"[A-Za-z0-9]{8,64}")
CLUSTER_RE = re.compile(r"[a-z0-9]{1,16}(?:-[a-z0-9]{1,16}){0,3}")

_state: dict = {}  # "app": (expires, (key, cluster)), "forced": monotonic time of the last early ask
_lock: Optional[asyncio.Lock] = None


def parse_realtime_answer(body) -> Optional[Tuple[str, str]]:
    """(key, cluster) of the Pusher app in Kick's realtime answer, or None."""
    data = body.get("data") if isinstance(body, dict) else None
    connections = data.get("connections") if isinstance(data, dict) else None
    for c in connections if isinstance(connections, list) else []:
        cred = c.get("credentials") if isinstance(c, dict) and c.get("provider") == "pusher" else None
        if not isinstance(cred, dict):
            continue
        key, cluster = cred.get("app_key"), cred.get("cluster")
        if (
            isinstance(key, str)
            and isinstance(cluster, str)
            and KEY_RE.fullmatch(key)
            and CLUSTER_RE.fullmatch(cluster)
        ):
            return key, cluster
    return None


async def fetch_pusher_app(timeout: float = 8) -> Optional[Tuple[str, str]]:
    """Ask Kick's realtime endpoint for its chat's Pusher app, offering only
    Pusher (its other provider is Kick's own realtime service, which needs a
    token of Kick's for every connection). As a browser, like kickpython's
    kick.com reads, so Cloudflare serves it."""
    payload = {
        "client": {"id": str(uuid.uuid4()), "type": "web"},
        "capabilities": {"accepted_providers": [{"provider": "pusher"}]},
    }
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession() as session:
            resp = await session.post(
                REALTIME_URL,
                json=payload,
                headers={"Accept": "application/json"},
                impersonate="chrome",
                timeout=timeout,
            )
        if resp.status_code != 200:
            logger.info(f"[Kick chat] Realtime lookup: HTTP {resp.status_code}")
            return None
        app = parse_realtime_answer(resp.json())
        if not app:
            logger.info("[Kick chat] Realtime lookup: no Pusher app in the answer")
        return app
    except Exception as e:
        logger.info(f"[Kick chat] Realtime lookup failed: {type(e).__name__}: {e}")
        return None


async def pusher_app(refused: Optional[str] = None) -> Tuple[str, str]:
    """(key, cluster) of the Pusher app Kick's chat is on. `refused` is a key a
    socket was just turned away with: if it's the one kept here, Kick is asked
    again now, at most once a FORCE_S, instead of every guild retrying it until
    the hour is up. Guilds connecting at once share one ask."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        now = time.monotonic()
        hit = _state.get("app")
        early = bool(refused and hit and hit[1][0] == refused and now - _state.get("forced", -1e9) >= FORCE_S)
        if hit and hit[0] > now and not early:
            return hit[1]
        if early:
            _state["forced"] = now
        found = await fetch_pusher_app()
        app = found or (hit[1] if hit else FALLBACK_APP)
        if found and hit and found != hit[1]:
            logger.info(f"[Kick chat] Kick's Pusher app changed: {hit[1][0]}@{hit[1][1]} -> {found[0]}@{found[1]}")
        _state["app"] = (now + (APP_TTL_S if found else RETRY_S), app)
        return app


def socket_url(app: Tuple[str, str]) -> str:
    key, cluster = app
    return f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"


def parse_frame(message) -> dict:
    """A Pusher frame as a dict ({} for anything else)."""
    try:
        frame = json.loads(message)
    except (TypeError, ValueError):
        return {}
    return frame if isinstance(frame, dict) else {}


def refusal_code(frame: dict) -> Optional[int]:
    """The code of a Pusher error that says not to connect again unchanged
    (4000-4099: an unknown or disabled app, among others), else None."""
    if frame.get("event") != "pusher:error":
        return None
    data = frame.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    code = data.get("code") if isinstance(data, dict) else None
    return code if isinstance(code, int) and 4000 <= code < 4100 else None


async def connect_to_chatroom(api, username_or_chatroom_id: str) -> None:
    """kickpython's KickAPI.connect_to_chatroom on the Pusher app Kick names.
    Blocks while the socket is open and returns when it closes, as kickpython's
    does; the caller reconnects. A refusal for the key makes the next call use
    the app Kick names now."""
    target = str(username_or_chatroom_id)
    chatroom_id = target if target.isdigit() else (await api.get_chatroom_id(target))[0]
    if not chatroom_id:
        raise ValueError(f"Could not find chatroom ID for {target}")

    app = await pusher_app()
    async with websockets.connect(socket_url(app)) as websocket:
        api.ws = websocket
        try:
            # Subscribed once Pusher says the connection is up (kickpython sends it at once, which a refusal
            # closing the socket first turns into an error that hides the refusal).
            async for message in websocket:
                frame = parse_frame(message)
                event = frame.get("event")
                code = refusal_code(frame)
                if code is not None:
                    logger.info(f"[Kick chat] Pusher refused key {app[0]} ({code}); asking Kick for its current app")
                    await pusher_app(refused=app[0])
                    return
                if event == "pusher:connection_established":
                    channel = f"chatrooms.{chatroom_id}.v2"
                    await websocket.send(
                        json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": channel}})
                    )
                    logger.info(f"Connected to chatroom {chatroom_id}")
                elif event == "pusher:ping":
                    await websocket.send(json.dumps({"event": "pusher:pong", "data": {}}))
                    continue
                await api._handle_ws_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
        finally:
            api.ws = None
