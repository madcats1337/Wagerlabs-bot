"""Tests for core.kick_chat_socket: the Kick chat socket on the Pusher app Kick names.

Kick retired the Pusher key the bot had built in (October 2026): new sockets with
it were refused with 4001 while open ones carried on, so the bot would have lost
Kick chat on its next restart. These cover asking Kick for the app, keeping it,
asking again when a socket is refused, and reading chat through kickpython's
handler on a socket that answers like Pusher.
"""

import asyncio
import json

import pytest
import websockets

from core import kick_chat_socket as kcs


def run(coro):
    """asyncio.run with the module's lock made fresh: a lock is bound to its loop."""
    kcs._lock = None
    return asyncio.run(coro)


def pusher_answer(creds, provider="pusher"):
    return {"data": {"connections": [{"provider": provider, "credentials": creds}], "mode": "websocket"}}


# ── Kick's realtime answer ──────────────────────────────────────────────────


def test_the_pusher_app_is_read_from_kicks_realtime_answer():
    assert kcs.parse_realtime_answer(pusher_answer({"app_key": "testpusherkey001", "cluster": "us2"})) == (
        "testpusherkey001",
        "us2",
    )
    # Its own realtime service is no Pusher app; nor is anything malformed.
    assert kcs.parse_realtime_answer(pusher_answer({"url": "wss://realtime.example/ws"}, "centrifugo")) is None
    assert kcs.parse_realtime_answer({"data": None}) is None
    assert kcs.parse_realtime_answer("not json") is None
    # The key becomes part of a URL and the cluster a host name: nothing else gets through.
    assert kcs.parse_realtime_answer(pusher_answer({"app_key": "34bf/../x", "cluster": "us2"})) is None
    assert kcs.parse_realtime_answer(pusher_answer({"app_key": "testpusherkey001", "cluster": "evil.com/x"})) is None
    assert kcs.socket_url(("testpusherkey001", "mt1")).startswith("wss://ws-mt1.pusher.com/app/testpusherkey001?")


def test_only_pushers_dont_come_back_unchanged_errors_count_as_refusals():
    def frame(data):
        return kcs.parse_frame(json.dumps({"event": "pusher:error", "data": data}))

    assert kcs.refusal_code(frame({"code": 4001, "message": "App key x not in this cluster."})) == 4001
    assert kcs.refusal_code(frame(json.dumps({"code": 4003}))) == 4003  # data as a string, as events carry it
    assert kcs.refusal_code(frame({"code": 4201, "message": "Pong reply not received"})) is None  # reconnect as is
    assert kcs.refusal_code(kcs.parse_frame('{"event":"pusher:connection_established","data":"{}"}')) is None
    assert kcs.refusal_code(kcs.parse_frame("not json")) is None


# ── Keeping the app ─────────────────────────────────────────────────────────


def test_the_app_is_kept_an_hour_and_asked_again_early_when_refused(monkeypatch):
    clock, answer, asks = {"now": 1000.0}, {"app": None}, []

    async def fetch():
        asks.append(clock["now"])
        return answer["app"]

    monkeypatch.setattr(kcs, "_state", {})
    monkeypatch.setattr(kcs.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(kcs, "fetch_pusher_app", fetch)

    assert run(kcs.pusher_app()) == kcs.FALLBACK_APP  # Kick can't be asked: the key it last named
    clock["now"] += 60
    answer["app"] = ("aaaa1111bbbb2222cccc", "us2")
    assert run(kcs.pusher_app()) == kcs.FALLBACK_APP and len(asks) == 1  # the failure is remembered a while
    clock["now"] += kcs.RETRY_S
    assert run(kcs.pusher_app()) == ("aaaa1111bbbb2222cccc", "us2") and len(asks) == 2
    clock["now"] += 1800
    assert run(kcs.pusher_app())[0] == "aaaa1111bbbb2222cccc" and len(asks) == 2  # kept an hour

    # Kick moves to a new key: a socket refused with the kept one makes Kick be asked now.
    answer["app"] = ("dddd3333eeee4444ffff", "us2")
    assert run(kcs.pusher_app(refused="some-other-key"))[0] == "aaaa1111bbbb2222cccc" and len(asks) == 2
    assert run(kcs.pusher_app(refused="aaaa1111bbbb2222cccc"))[0] == "dddd3333eeee4444ffff" and len(asks) == 3
    answer["app"] = None  # Kick can't be asked: the last app found stands, not the fallback
    clock["now"] += 30
    assert run(kcs.pusher_app(refused="dddd3333eeee4444ffff"))[0] == "dddd3333eeee4444ffff" and len(asks) == 3
    clock["now"] += kcs.FORCE_S
    assert run(kcs.pusher_app(refused="dddd3333eeee4444ffff"))[0] == "dddd3333eeee4444ffff" and len(asks) == 4


def test_guilds_connecting_at_once_share_one_ask(monkeypatch):
    asks = []

    async def fetch():
        asks.append(1)
        await asyncio.sleep(0.05)
        return ("aaaa1111bbbb2222cccc", "us2")

    monkeypatch.setattr(kcs, "_state", {})
    monkeypatch.setattr(kcs, "fetch_pusher_app", fetch)

    async def many():
        return await asyncio.gather(*(kcs.pusher_app() for _ in range(6)))

    assert run(many()) == [("aaaa1111bbbb2222cccc", "us2")] * 6 and len(asks) == 1


# ── The socket ──────────────────────────────────────────────────────────────


class FakeKickAPI:
    """What connect_to_chatroom uses of kickpython's KickAPI."""

    def __init__(self):
        self.ws, self.handled = None, []

    async def get_chatroom_id(self, username):
        return (60338 if username == "nickcleveland" else None), None

    async def _handle_ws_message(self, message):
        self.handled.append(json.loads(message))


def test_a_refused_key_is_replaced_and_chat_reaches_kickpythons_handler(monkeypatch):
    """A Pusher stand-in that knows only the new key: the old one is refused
    with 4001, the next connection uses the app Kick names, subscribes to the
    chatroom, has its ping answered, and passes chat to kickpython's handler."""
    retired, current = ("retiredkey000001", "us2"), ("testpusherkey001", "us2")
    seen = {"paths": [], "sent": []}

    async def pusher(conn):
        seen["paths"].append(conn.request.path)
        if f"/app/{current[0]}" not in conn.request.path:
            await conn.send(
                json.dumps({"event": "pusher:error", "data": {"code": 4001, "message": "not in this cluster"}})
            )
            await conn.close(4001, "App key not in this cluster")
            return
        await conn.send(json.dumps({"event": "pusher:connection_established", "data": '{"socket_id":"1.2"}'}))
        seen["sent"].append(json.loads(await conn.recv()))  # the subscription
        await conn.send(json.dumps({"event": "pusher:ping", "data": {}}))
        seen["sent"].append(json.loads(await conn.recv()))  # the pong
        chat = {"id": "m1", "content": "hello", "sender": {"username": "madcats", "identity": {"badges": []}}}
        await conn.send(
            json.dumps(
                {"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.60338.v2", "data": json.dumps(chat)}
            )
        )
        await conn.close()

    async def scenario():
        async with websockets.serve(pusher, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(kcs, "socket_url", lambda app: f"ws://127.0.0.1:{port}/app/{app[0]}?protocol=7")
            api = FakeKickAPI()
            await kcs.connect_to_chatroom(api, "nickcleveland")  # refused: Kick is asked again
            assert api.handled == [] and api.ws is None
            await kcs.connect_to_chatroom(api, "nickcleveland")  # the caller's reconnect
            return api

    names = [retired, current]
    monkeypatch.setattr(kcs, "_state", {"app": (1e18, retired)})

    async def fetch():
        return names[1]

    monkeypatch.setattr(kcs, "fetch_pusher_app", fetch)
    api = run(scenario())

    assert [p.split("?")[0] for p in seen["paths"]] == [f"/app/{retired[0]}", f"/app/{current[0]}"]
    assert seen["sent"][0] == {"event": "pusher:subscribe", "data": {"auth": "", "channel": "chatrooms.60338.v2"}}
    assert seen["sent"][1]["event"] == "pusher:pong"
    assert [m["event"] for m in api.handled] == ["pusher:connection_established", "App\\Events\\ChatMessageEvent"]
    assert api.ws is None


def test_an_unknown_channel_is_an_error_not_a_socket():
    with pytest.raises(ValueError):
        run(kcs.connect_to_chatroom(FakeKickAPI(), "nobody-here"))
