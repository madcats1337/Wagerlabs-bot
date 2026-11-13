"""
Re-create Kick event subscriptions that Kick dropped on its side.

Kick unsubscribes an app from an event once its webhook "repeatedly fails to
process an event for more than a day" (docs.kick.com). Nothing tells us: our
kick_webhook_subscriptions row stays 'active', and the webhook router waits for
events that will never come. After the 2026-10-01 signing-key outage this
silently ended go-live alerts for every channel that streamed during it, while
the rarer sub events kept arriving and made ingress look healthy.

This compares Kick's live subscription list with our active rows and
re-subscribes what is missing. It uses the app's own access token (client
credentials + broadcaster_user_id, which Kick accepts for creating
subscriptions), so no streamer has to reconnect Kick. The bot runs it on
startup and every hour (bot.py, kick_subscription_repair_task).

Only rows we still have the streamer's consent for are considered: the server
must still be connected to Kick, not revoked, and bound to that channel (see
load_active_rows). Re-subscribing anything else would undo the streamer's choice.
"""

import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import text

from utils.log_context import server_context

logger = logging.getLogger(__name__)

KICK_TOKEN_URL = "https://id.kick.com/oauth/token"
KICK_SUBSCRIPTIONS_URL = "https://api.kick.com/public/v1/events/subscriptions"

# What the dashboard's Kick connect registers. chat.message.sent is deliberately
# absent: chat is read over the kickpython WebSocket, never as a webhook.
REPAIRABLE_EVENTS = (
    "livestream.status.updated",
    "channel.subscription.new",
    "channel.subscription.gifts",
    "channel.subscription.renewal",
)


@dataclass
class RepairPlan:
    create: list = field(default_factory=list)  # rows whose event Kick no longer has
    adopt: list = field(default_factory=list)  # (row, live_id): Kick has it under another id
    retire: list = field(default_factory=list)  # duplicate rows for an event already covered

    @property
    def empty(self) -> bool:
        return not (self.create or self.adopt or self.retire)


def _key(row) -> tuple:
    return (row["discord_server_id"], str(row["broadcaster_user_id"]), row["event_type"])


def plan_repair(local_rows, live_subs) -> RepairPlan:
    """Decide what to do with each active row, given Kick's live list.

    Matches on (broadcaster, event), not only on subscription id: when Kick has
    the event under a different id, creating another would duplicate it and our
    row would still miss its deliveries ("Unknown subscription ID"), so the row
    adopts the live id instead.
    """
    live_by_pair = {}
    for sub in live_subs or []:
        sub_id, event, broadcaster = sub.get("id"), sub.get("event"), sub.get("broadcaster_user_id")
        if sub_id and event and broadcaster is not None:
            live_by_pair.setdefault((str(broadcaster), event), []).append(sub_id)

    rows = [r for r in local_rows if r.get("event_type") in REPAIRABLE_EVENTS]

    def is_healthy(r):
        return r["subscription_id"] in live_by_pair.get((str(r["broadcaster_user_id"]), r["event_type"]), ())

    claimed = {r["subscription_id"] for r in rows if is_healthy(r)}
    covered = {_key(r) for r in rows if is_healthy(r)}

    plan = RepairPlan()
    for r in rows:
        if is_healthy(r):
            continue
        key = _key(r)
        if key in covered:
            plan.retire.append(r)
            continue
        covered.add(key)
        free = [i for i in live_by_pair.get((key[1], key[2]), ()) if i not in claimed]
        if free:
            claimed.add(free[0])
            plan.adopt.append((r, free[0]))
        else:
            plan.create.append(r)
    return plan


def listing_trustworthy(live_subs, local_rows) -> bool:
    """Whether Kick's listing is believable enough to act on.

    `live_subs` is every subscription the app has on Kick (all broadcasters and
    events, each a dict with id / event / broadcaster_user_id). `local_rows` are
    our active rows for servers still connected to Kick. Returning False skips
    this run without changing anything; the next run tries again in an hour.

    A bad listing is costly: every "missing" row becomes a new subscription and
    its old row is retired. A listing can be wrong without an HTTP error, e.g.
    an empty 200 during a Kick incident, or a deployment whose KICK_CLIENT_ID
    belongs to a different Kick app (it sees none of our subscriptions).

    Trusted when at least one of our subscriptions is in it: both bad cases
    share none, while a partial drop does (after the October outage 21 of 25
    were still live). The cost: if Kick ever drops every one of our
    subscriptions, this refuses each run and logs why; reconnecting Kick on the
    dashboard still re-creates them.
    """
    if not local_rows:
        return True
    live_ids = {sub.get("id") for sub in live_subs or []}
    return any(row["subscription_id"] in live_ids for row in local_rows)


def load_active_rows(engine) -> list:
    """Active subscription rows we still have the streamer's consent to keep.

    The app token can subscribe to ANY channel, so consent is checked per row:
      - the server still has a Kick connection (unlinking deletes its
        kick_oauth_tokens row but leaves these rows 'active');
      - that connection is not dead: needs_reauth is set on invalid_grant, i.e.
        the streamer revoked the app on Kick;
      - the row is for the channel the server is bound to now
        (bot_settings.kick_broadcaster_user_id, written by the dashboard's Kick
        connect). Rebinding to another channel only clears the NEW channel's
        rows, so the old channel's would otherwise be re-subscribed.
    kick_oauth_tokens.user_id is the Discord user, not the Kick broadcaster, so
    the binding comes from bot_settings.
    """
    with engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT s.id, s.subscription_id, s.discord_server_id, s.broadcaster_user_id,
                       s.event_type, s.event_version, s.webhook_url, s.webhook_secret
                FROM kick_webhook_subscriptions s
                WHERE s.status = 'active'
                  AND EXISTS (
                      SELECT 1 FROM kick_oauth_tokens t
                      WHERE t.discord_server_id = s.discord_server_id
                        AND NOT COALESCE(t.needs_reauth, FALSE)
                  )
                  AND EXISTS (
                      SELECT 1 FROM bot_settings b
                      WHERE b.discord_server_id = s.discord_server_id
                        AND b.key = 'kick_broadcaster_user_id'
                        AND b.value = s.broadcaster_user_id
                  )
                ORDER BY s.id
                """
            )
        )
        return [dict(r._mapping) for r in result]


_RELINK = """
    INSERT INTO kick_webhook_subscriptions
        (subscription_id, discord_server_id, broadcaster_user_id, event_type, event_version,
         webhook_url, webhook_secret, status, created_at, updated_at, deleted_at)
    VALUES (:new_id, :server_id, :broadcaster, :event, :version,
            :url, :secret, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
    ON CONFLICT (subscription_id) DO UPDATE SET
        discord_server_id = EXCLUDED.discord_server_id,
        broadcaster_user_id = EXCLUDED.broadcaster_user_id,
        event_type = EXCLUDED.event_type,
        event_version = EXCLUDED.event_version,
        webhook_url = EXCLUDED.webhook_url,
        webhook_secret = EXCLUDED.webhook_secret,
        status = 'active',
        deleted_at = NULL,
        updated_at = CURRENT_TIMESTAMP
"""

_RETIRE = """
    UPDATE kick_webhook_subscriptions
    SET status = 'deleted', deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
    WHERE id = :row_id
"""


def _relink(engine, row, new_id):
    """Point the row's event at `new_id` (a live Kick subscription) and retire the row.

    An upsert, not UPDATE subscription_id: subscription_id is UNIQUE and an old
    'deleted' row may already hold the live id; that row is reactivated instead.
    The secret is carried AS STORED (still encrypted); the router decrypts it.
    """
    with engine.begin() as conn:
        conn.execute(
            text(_RELINK),
            {
                "new_id": new_id,
                "server_id": row["discord_server_id"],
                "broadcaster": str(row["broadcaster_user_id"]),
                "event": row["event_type"],
                "version": row.get("event_version") or 1,
                "url": row["webhook_url"],
                "secret": row.get("webhook_secret") or "",
            },
        )
        if row["subscription_id"] != new_id:
            conn.execute(text(_RETIRE), {"row_id": row["id"]})


def _retire(engine, row):
    with engine.begin() as conn:
        conn.execute(text(_RETIRE), {"row_id": row["id"]})


class KickAppClient:
    """Kick's subscription API with the app's own token (client credentials)."""

    def __init__(self, client_id=None, client_secret=None, timeout_seconds=20):
        self.client_id = client_id or os.getenv("KICK_CLIENT_ID")
        self.client_secret = client_secret or os.getenv("KICK_CLIENT_SECRET")
        self._timeout_seconds = timeout_seconds
        self._session = None
        self._token = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _authorized_session(self):
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout_seconds))
        if self._token is None:
            async with self._session.post(
                KICK_TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Kick app token request failed: HTTP {resp.status}")
                self._token = (await resp.json()).get("access_token")
            if not self._token:
                raise RuntimeError("Kick app token response had no access_token")
        return self._session, {"Authorization": f"Bearer {self._token}"}

    async def list_subscriptions(self) -> list:
        session, headers = await self._authorized_session()
        async with session.get(KICK_SUBSCRIPTIONS_URL, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Kick subscription list failed: HTTP {resp.status}: {(await resp.text())[:200]}")
            data = (await resp.json()).get("data")
        if not isinstance(data, list):
            raise RuntimeError("Kick subscription list had no data array")
        return data

    async def subscribe(self, broadcaster_user_id: int, events) -> list:
        """Create subscriptions; returns Kick's per-event results ({name, subscription_id, error})."""
        session, headers = await self._authorized_session()
        body = {
            "events": [{"name": e, "version": 1} for e in events],
            # Required with an app token (the broadcaster can't come from it).
            "broadcaster_user_id": int(broadcaster_user_id),
            "method": "webhook",
        }
        async with session.post(KICK_SUBSCRIPTIONS_URL, headers=headers, json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Kick subscribe failed: HTTP {resp.status}: {(await resp.text())[:200]}")
            return (await resp.json()).get("data") or []

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None


def _describe(rows) -> list:
    return [(r["discord_server_id"], str(r["broadcaster_user_id"]), r["event_type"]) for r in rows]


async def repair_kick_subscriptions(engine, client=None, dry_run=False) -> dict:
    """Re-subscribe events Kick dropped. Returns a summary; never raises for Kick errors."""
    summary = {"created": 0, "adopted": 0, "retired": 0, "failed": 0, "skipped": None}
    if engine is None:
        summary["skipped"] = "no database"
        return summary

    local_rows = load_active_rows(engine)
    if not local_rows:
        summary["skipped"] = "nothing to check"
        return summary

    own_client = client is None
    if own_client:
        client = KickAppClient()
        if not client.configured:
            summary["skipped"] = "no Kick app credentials"
            return summary

    try:
        try:
            live_subs = await client.list_subscriptions()
        except Exception as e:
            logger.warning(f"[KickSubs] ⚠️ Could not list Kick's subscriptions, skipping this run: {e}")
            summary["skipped"] = "listing failed"
            return summary

        if not listing_trustworthy(live_subs, local_rows):
            logger.warning(
                f"[KickSubs] ⚠️ Not acting on Kick's listing: none of our {len(local_rows)} active "
                f"subscriptions are among its {len(live_subs)}. Skipping this run; if Kick really "
                f"dropped them all, reconnecting Kick on the dashboard re-creates them"
            )
            summary["skipped"] = "listing not trusted"
            return summary

        plan = plan_repair(local_rows, live_subs)
        summary["planned_create"] = _describe(plan.create)
        summary["planned_adopt"] = [(*_describe([r])[0], live_id) for r, live_id in plan.adopt]
        summary["planned_retire"] = _describe(plan.retire)
        if dry_run or plan.empty:
            return summary

        for row, live_id in plan.adopt:
            with server_context(row["discord_server_id"]):
                _relink(engine, row, live_id)
                summary["adopted"] += 1
                logger.info(f"[KickSubs] 🔗 {row['event_type']}: our row now follows Kick's live {live_id}")

        for row in plan.retire:
            with server_context(row["discord_server_id"]):
                _retire(engine, row)
                summary["retired"] += 1
                logger.info(f"[KickSubs] 🧹 Retired duplicate {row['event_type']} row {row['subscription_id']}")

        groups = {}
        for row in plan.create:
            groups.setdefault((row["discord_server_id"], str(row["broadcaster_user_id"])), []).append(row)

        for (server_id, broadcaster), rows in groups.items():
            with server_context(server_id):
                events = [r["event_type"] for r in rows]
                try:
                    results = await client.subscribe(int(broadcaster), events)
                except Exception as e:
                    logger.warning(f"[KickSubs] ⚠️ Could not re-subscribe {', '.join(events)} for {broadcaster}: {e}")
                    summary["failed"] += len(rows)
                    continue
                by_event = {res.get("name"): res for res in results if isinstance(res, dict)}
                for row in rows:
                    res = by_event.get(row["event_type"]) or {}
                    new_id = res.get("subscription_id")
                    if not new_id:
                        logger.warning(
                            f"[KickSubs] ⚠️ Kick did not re-create {row['event_type']} for {broadcaster}: "
                            f"{res.get('error') or 'no subscription_id in response'}"
                        )
                        summary["failed"] += 1
                        continue
                    _relink(engine, row, new_id)
                    summary["created"] += 1
                    logger.info(
                        f"[KickSubs] ✅ Re-subscribed {row['event_type']} for {broadcaster} "
                        f"(Kick had dropped {row['subscription_id']}; now {new_id})"
                    )
    finally:
        if own_client:
            await client.close()

    return summary
