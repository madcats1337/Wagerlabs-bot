"""Re-creating Kick event subscriptions that Kick dropped on its side.

Kick unsubscribes an app from an event after its webhook fails for more than a
day. After the 2026-10-01 signing-key outage that ended go-live alerts for every
channel that streamed during it, while our rows still said 'active'.
"""

import asyncio

import pytest
from sqlalchemy import create_engine, text

import core.kick_subscription_repair as repair
from core.kick_subscription_repair import plan_repair, repair_kick_subscriptions

NICK_SERVER = 501183572078493696
NICK = "60844"
LELES_SERVER = 914986636629143562
LELES = "152837"


def row(sub_id, broadcaster, event, server=NICK_SERVER, row_id=None, secret="enc:v1:abc"):
    return {
        "id": row_id or abs(hash(sub_id)) % 10_000,
        "subscription_id": sub_id,
        "discord_server_id": server,
        "broadcaster_user_id": broadcaster,
        "event_type": event,
        "event_version": 1,
        "webhook_url": "https://bot.wagerlabs.app/webhooks/kick",
        "webhook_secret": secret,
    }


def live(sub_id, broadcaster, event):
    # Kick returns broadcaster_user_id as an integer; ours is VARCHAR.
    return {"id": sub_id, "broadcaster_user_id": int(broadcaster), "event": event, "version": 1}


# ---------------------------------------------------------------- plan_repair


def test_healthy_row_needs_nothing():
    plan = plan_repair([row("A", NICK, "livestream.status.updated")], [live("A", NICK, "livestream.status.updated")])
    assert plan.create == [] and plan.adopt == [] and plan.retire == []


def test_row_kick_dropped_is_recreated():
    local = [row("GONE", NICK, "livestream.status.updated"), row("B", NICK, "channel.subscription.new")]
    plan = plan_repair(local, [live("B", NICK, "channel.subscription.new")])
    assert [r["subscription_id"] for r in plan.create] == ["GONE"]
    assert plan.adopt == [] and plan.retire == []


def test_row_whose_event_kick_has_under_another_id_is_adopted_not_duplicated():
    plan = plan_repair(
        [row("STALE", NICK, "livestream.status.updated")], [live("LIVE", NICK, "livestream.status.updated")]
    )
    assert plan.create == []
    assert [(r["subscription_id"], new_id) for r, new_id in plan.adopt] == [("STALE", "LIVE")]


def test_stale_duplicate_of_a_healthy_row_is_retired():
    local = [row("OLD", NICK, "livestream.status.updated"), row("CUR", NICK, "livestream.status.updated")]
    plan = plan_repair(local, [live("CUR", NICK, "livestream.status.updated")])
    assert [r["subscription_id"] for r in plan.retire] == ["OLD"]
    assert plan.create == [] and plan.adopt == []


def test_two_stale_rows_for_one_event_create_it_once():
    local = [row("X1", NICK, "livestream.status.updated"), row("X2", NICK, "livestream.status.updated")]
    plan = plan_repair(local, [])
    assert [r["subscription_id"] for r in plan.create] == ["X1"]
    assert [r["subscription_id"] for r in plan.retire] == ["X2"]


def test_chat_and_unknown_events_are_never_repaired():
    # chat.message.sent is read over the kickpython WebSocket and must never be
    # (re)subscribed as a webhook.
    local = [row("C", NICK, "chat.message.sent"), row("U", NICK, "some.future.event")]
    plan = plan_repair(local, [])
    assert plan.create == [] and plan.adopt == [] and plan.retire == []


def test_broadcasters_are_kept_apart():
    local = [row("N", NICK, "livestream.status.updated"), row("L", LELES, "livestream.status.updated", LELES_SERVER)]
    plan = plan_repair(local, [live("L", LELES, "livestream.status.updated")])
    assert [r["subscription_id"] for r in plan.create] == ["N"]


# ------------------------------------------------------------ end to end (DB)


class FakeKick:
    def __init__(self, live_subs, fail_list=False, errors=None):
        self.live_subs = live_subs
        self.fail_list = fail_list
        self.errors = errors or {}
        self.subscribe_calls = []
        self._n = 0

    async def list_subscriptions(self):
        if self.fail_list:
            raise RuntimeError("Kick list failed: 503")
        return list(self.live_subs)

    async def subscribe(self, broadcaster_user_id, events):
        self.subscribe_calls.append((broadcaster_user_id, list(events)))
        out = []
        for e in events:
            if e in self.errors:
                out.append({"name": e, "version": 1, "subscription_id": None, "error": self.errors[e]})
            else:
                self._n += 1
                out.append({"name": e, "version": 1, "subscription_id": f"NEW{self._n}", "error": None})
        return out


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE kick_webhook_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id VARCHAR NOT NULL UNIQUE,
                    discord_server_id BIGINT NOT NULL,
                    broadcaster_user_id VARCHAR NOT NULL,
                    event_type VARCHAR NOT NULL,
                    event_version INTEGER NOT NULL DEFAULT 1,
                    webhook_url TEXT NOT NULL,
                    status VARCHAR DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    deleted_at TIMESTAMP,
                    webhook_secret TEXT NOT NULL DEFAULT ''
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE kick_oauth_tokens (
                    user_id BIGINT NOT NULL, discord_server_id BIGINT NOT NULL DEFAULT 0,
                    access_token TEXT NOT NULL, PRIMARY KEY (user_id, discord_server_id)
                )
                """
            )
        )
        conn.execute(text("INSERT INTO kick_oauth_tokens VALUES (1, :s, 'tok')"), {"s": NICK_SERVER})
    return eng


def add_sub(engine, sub_id, broadcaster, event, server=NICK_SERVER, status="active", secret="enc:v1:abc"):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO kick_webhook_subscriptions
                (subscription_id, discord_server_id, broadcaster_user_id, event_type, webhook_url, status, webhook_secret)
                VALUES (:id, :s, :b, :e, 'https://bot.wagerlabs.app/webhooks/kick', :st, :sec)
                """
            ),
            {"id": sub_id, "s": server, "b": broadcaster, "e": event, "st": status, "sec": secret},
        )


def subs(engine):
    with engine.connect() as conn:
        return {
            r.subscription_id: dict(r._mapping) for r in conn.execute(text("SELECT * FROM kick_webhook_subscriptions"))
        }


@pytest.fixture
def trust_listing(monkeypatch):
    # These tests are about what the repair does once it acts; whether to act
    # on a listing at all is listing_trustworthy's job, tested on its own.
    monkeypatch.setattr(repair, "listing_trustworthy", lambda live_subs, local_rows: True)


def run(coro):
    return asyncio.run(coro)


def test_dropped_go_live_subscription_is_recreated_and_old_row_retired(engine, trust_listing):
    add_sub(engine, "GONE", NICK, "livestream.status.updated", secret="enc:v1:keep-me")
    add_sub(engine, "OK", NICK, "channel.subscription.new")
    kick = FakeKick([live("OK", NICK, "channel.subscription.new")])

    summary = run(repair_kick_subscriptions(engine, client=kick))

    # The app token needs broadcaster_user_id as an integer.
    assert kick.subscribe_calls == [(int(NICK), ["livestream.status.updated"])]
    rows = subs(engine)
    assert rows["GONE"]["status"] == "deleted" and rows["GONE"]["deleted_at"] is not None
    new = rows["NEW1"]
    assert new["status"] == "active"
    assert new["discord_server_id"] == NICK_SERVER
    assert new["broadcaster_user_id"] == NICK
    assert new["event_type"] == "livestream.status.updated"
    # Carried AS STORED (still encrypted) so the router's decrypt keeps working.
    assert new["webhook_secret"] == "enc:v1:keep-me"
    assert rows["OK"]["status"] == "active"
    assert summary["created"] == 1


def test_adopting_a_live_id_already_held_by_a_deleted_row_reactivates_it(engine, trust_listing):
    add_sub(engine, "LIVE", NICK, "livestream.status.updated", status="deleted")
    add_sub(engine, "STALE", NICK, "livestream.status.updated")
    kick = FakeKick([live("LIVE", NICK, "livestream.status.updated")])

    run(repair_kick_subscriptions(engine, client=kick))

    assert kick.subscribe_calls == []
    rows = subs(engine)
    assert rows["LIVE"]["status"] == "active" and rows["LIVE"]["deleted_at"] is None
    assert rows["STALE"]["status"] == "deleted"


def test_server_that_unlinked_kick_is_left_alone(engine, trust_listing):
    # api_kick_oauth_unlink deletes kick_oauth_tokens but leaves these rows
    # 'active'. Re-subscribing with the app token would undo the streamer's choice.
    add_sub(engine, "L", LELES, "livestream.status.updated", server=LELES_SERVER)
    kick = FakeKick([])

    run(repair_kick_subscriptions(engine, client=kick))

    assert kick.subscribe_calls == []
    assert subs(engine)["L"]["status"] == "active"


def test_event_kick_refuses_keeps_old_row_for_the_next_run(engine, trust_listing):
    add_sub(engine, "GONE", NICK, "livestream.status.updated")
    kick = FakeKick([], errors={"livestream.status.updated": "broadcaster not found"})

    summary = run(repair_kick_subscriptions(engine, client=kick))

    rows = subs(engine)
    assert set(rows) == {"GONE"} and rows["GONE"]["status"] == "active"
    assert summary["created"] == 0 and summary["failed"] == 1


def test_failed_listing_changes_nothing(engine, trust_listing):
    add_sub(engine, "GONE", NICK, "livestream.status.updated")
    kick = FakeKick([], fail_list=True)

    summary = run(repair_kick_subscriptions(engine, client=kick))

    assert kick.subscribe_calls == []
    assert subs(engine)["GONE"]["status"] == "active"
    assert summary["skipped"] == "listing failed"


def test_untrusted_listing_changes_nothing(engine, monkeypatch):
    monkeypatch.setattr(repair, "listing_trustworthy", lambda live_subs, local_rows: False)
    add_sub(engine, "GONE", NICK, "livestream.status.updated")
    kick = FakeKick([])

    summary = run(repair_kick_subscriptions(engine, client=kick))

    assert kick.subscribe_calls == []
    assert subs(engine)["GONE"]["status"] == "active"
    assert summary["skipped"] == "listing not trusted"


def test_dry_run_reports_the_plan_without_touching_anything(engine, trust_listing):
    add_sub(engine, "GONE", NICK, "livestream.status.updated")
    kick = FakeKick([])

    summary = run(repair_kick_subscriptions(engine, client=kick, dry_run=True))

    assert kick.subscribe_calls == []
    assert subs(engine)["GONE"]["status"] == "active"
    assert summary["planned_create"] == [(NICK_SERVER, NICK, "livestream.status.updated")]


# ------------------------------------------------------- listing_trustworthy


def test_empty_listing_is_not_trusted():
    # An empty 200 during a Kick incident would otherwise re-create everything.
    assert repair.listing_trustworthy([], [row("A", NICK, "livestream.status.updated")]) is False


def test_listing_from_another_app_is_not_trusted():
    # e.g. a deployment whose KICK_CLIENT_ID belongs to a different Kick app.
    others = [live("THEIRS", "999", "livestream.status.updated")]
    assert repair.listing_trustworthy(others, [row("OURS", NICK, "livestream.status.updated")]) is False


def test_partial_drop_is_trusted():
    local = [row("GONE", NICK, "livestream.status.updated"), row("KEPT", NICK, "channel.subscription.new")]
    assert repair.listing_trustworthy([live("KEPT", NICK, "channel.subscription.new")], local) is True


def test_october_outage_is_repaired_with_the_real_guard(engine):
    # The real case: Kick dropped the go-live sub, the sub events survived.
    add_sub(engine, "GONE", NICK, "livestream.status.updated")
    add_sub(engine, "KEPT", NICK, "channel.subscription.new")
    kick = FakeKick([live("KEPT", NICK, "channel.subscription.new")])

    summary = run(repair_kick_subscriptions(engine, client=kick))

    assert kick.subscribe_calls == [(int(NICK), ["livestream.status.updated"])]
    assert summary["created"] == 1 and summary["skipped"] is None


def test_empty_listing_with_the_real_guard_changes_nothing(engine):
    add_sub(engine, "A", NICK, "livestream.status.updated")
    kick = FakeKick([])

    summary = run(repair_kick_subscriptions(engine, client=kick))

    assert kick.subscribe_calls == []
    assert subs(engine)["A"]["status"] == "active"
    assert summary["skipped"] == "listing not trusted"
