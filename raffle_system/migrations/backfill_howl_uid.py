"""
Backfill `howl_uid` on legacy Howl links.

The Howl verify panel originally matched users by their Howl *username* and
stored only `shuffle_username`. It now matches on the numeric Howl UID, so rows
created by the old flow have `howl_uid IS NULL`. Those rows are invisible to the
UID-keyed "already verified by another Discord account" check in
`features/linking/howl_panel.py`, which has to fall back to a username match.

This backfill resolves each legacy row's username against the guild's live Howl
affiliate leaderboard and writes the matching `userId` into `howl_uid`.

Deliberately conservative — a link is identity, so a wrong UID is worse than a
missing one:
  * Matching is case-insensitive on the username but must be UNIQUE. If the
    leaderboard has two entries normalising to the same name, the row is skipped.
  * A UID already held by a DIFFERENT discord_id is never reused; the row is
    skipped and reported.
  * Rows whose username doesn't appear in the window are left alone (NULL), and
    the username fallback in howl_panel.py keeps covering them.

Not registered in bot.py's startup migrations: it makes outbound HTTP calls per
guild and is inherently partial (see the window caveat below), so it runs
on demand rather than on every boot.

Usage (from the Kick-dicord-bot directory):

    # report only, writes nothing
    python -m raffle_system.migrations.backfill_howl_uid --dry-run

    # apply
    python -m raffle_system.migrations.backfill_howl_uid

    # a single guild, and/or a wider lookback than the default 365 days
    python -m raffle_system.migrations.backfill_howl_uid --server-id 123 --days 720

Window caveat: Howl's affiliate endpoint is date-windowed, so a legacy user who
has not wagered inside the queried window simply is not in the response and
cannot be matched. Widen with --days; a user who never wagered under the
affiliate is unresolvable by design and stays NULL.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

import requests
from sqlalchemy import text

logger = logging.getLogger(__name__)

HOWL_DEFAULT_LB_URL = "https://howl.gg/api/user/affiliate/lb"
DEFAULT_LOOKBACK_DAYS = 365
# Howl caps a page at 1000 rows; the affiliate lists in play are far smaller.
FETCH_LIMIT = 1000


def _normalize(name):
    return (name or "").strip().lower()


def _read_settings(conn, server_id, keys):
    """Read per-server bot_settings for the given keys. Returns {key: value}."""
    rows = conn.execute(
        text("SELECT key, value FROM bot_settings WHERE discord_server_id = :sid AND key = ANY(:keys)"),
        {"keys": list(keys), "sid": server_id},
    ).fetchall()
    out = {}
    for k, v in rows:
        val = (v or "").strip()
        if val:
            out[k] = val
    return out


def _fetch_affiliate_rows(url, api_key, days):
    """Fetch {normalized_username: userId} for the lookback window.

    Returns None on any API failure (so the caller can distinguish "couldn't
    reach Howl" from "user genuinely absent"), and drops usernames that appear
    more than once rather than guessing between them.
    """
    now = datetime.utcnow()
    start = now - timedelta(days=days)
    params = {
        "from": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "limit": str(FETCH_LIMIT),
    }
    headers = {
        "Authorization": api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; WagerlabsBot/1.0; +https://wagerlabs.app)",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
    except Exception as e:
        logger.error(f"[HowlBackfill] affiliate fetch failed: {e}")
        return None
    if resp.status_code != 200:
        logger.error(f"[HowlBackfill] affiliate API returned status {resp.status_code}")
        return None
    try:
        body = resp.json()
    except Exception as e:
        logger.error(f"[HowlBackfill] affiliate API returned non-JSON: {e}")
        return None
    if not isinstance(body, dict) or not body.get("success"):
        logger.error(f"[HowlBackfill] unexpected affiliate response: {str(body)[:200]}")
        return None

    by_name = {}
    ambiguous = set()
    for row in body.get("data") or []:
        name = _normalize(row.get("name"))
        uid = row.get("userId")
        if not name or uid is None:
            continue
        uid = str(uid)
        if name in by_name and by_name[name] != uid:
            ambiguous.add(name)
            continue
        by_name[name] = uid

    for name in ambiguous:
        by_name.pop(name, None)
        logger.warning(f"[HowlBackfill] username '{name}' maps to multiple UIDs — will not backfill it")

    return by_name


def _legacy_rows(conn):
    """Howl links that still have no UID.

    raffle_shuffle_links has no server column, so this is global — --server-id
    scopes which guild's credentials resolve the names, not which rows are read.
    """
    return conn.execute(
        text(
            "SELECT id, discord_id, shuffle_username FROM raffle_shuffle_links "
            "WHERE platform = 'howl' AND howl_uid IS NULL AND shuffle_username IS NOT NULL"
        )
    ).fetchall()


def _guild_ids_with_howl(conn, server_id=None):
    """Servers configured for Howl, i.e. holding a howl_api_key."""
    if server_id is not None:
        return [int(server_id)]
    rows = conn.execute(
        text(
            "SELECT DISTINCT discord_server_id FROM bot_settings "
            "WHERE key = 'howl_api_key' AND value IS NOT NULL AND value <> '' "
            "AND discord_server_id IS NOT NULL"
        )
    ).fetchall()
    return [int(r[0]) for r in rows]


def backfill_howl_uid(engine, server_id=None, days=DEFAULT_LOOKBACK_DAYS, dry_run=False):
    """Resolve and write howl_uid for legacy Howl links.

    Returns a stats dict: {"updated", "skipped_no_match", "skipped_conflict",
    "skipped_ambiguous", "candidates"}.
    """
    from utils.secret_settings import decrypt_secret

    stats = {
        "candidates": 0,
        "updated": 0,
        "skipped_no_match": 0,
        "skipped_conflict": 0,
        "skipped_ambiguous": 0,
    }

    with engine.connect() as conn:
        candidates = _legacy_rows(conn)
        guild_ids = _guild_ids_with_howl(conn, server_id)

    stats["candidates"] = len(candidates)
    if not candidates:
        logger.info("[HowlBackfill] no legacy rows with a NULL howl_uid — nothing to do")
        return stats
    if not guild_ids:
        logger.warning("[HowlBackfill] no server has a howl_api_key configured — cannot resolve any UIDs")
        return stats

    # raffle_shuffle_links is not server-scoped, so a name is resolved against
    # every configured guild's affiliate list; the first unique hit wins.
    name_to_uid = {}
    for gid in guild_ids:
        with engine.connect() as conn:
            settings = _read_settings(conn, gid, ("howl_affiliate_url", "howl_api_key"))
        url = settings.get("howl_affiliate_url") or HOWL_DEFAULT_LB_URL
        try:
            api_key = decrypt_secret(settings.get("howl_api_key"), key_name="howl_api_key", row_id=gid)
        except Exception as e:
            logger.error(f"[HowlBackfill] could not decrypt howl_api_key for server {gid}: {e}")
            continue
        if not api_key:
            logger.warning(f"[HowlBackfill] server {gid} has no usable howl_api_key — skipping")
            continue

        fetched = _fetch_affiliate_rows(url, api_key, days)
        if fetched is None:
            logger.error(f"[HowlBackfill] server {gid}: affiliate fetch failed — its users stay NULL")
            continue
        logger.info(f"[HowlBackfill] server {gid}: {len(fetched)} affiliate usernames in the last {days}d")
        for name, uid in fetched.items():
            if name in name_to_uid and name_to_uid[name] != uid:
                # Same name, different UID across two guilds' affiliate lists.
                name_to_uid[name] = None
            else:
                name_to_uid.setdefault(name, uid)

    for row_id, discord_id, username in candidates:
        key = _normalize(username)
        uid = name_to_uid.get(key)
        if uid is None:
            if key in name_to_uid:
                stats["skipped_ambiguous"] += 1
                logger.warning(
                    f"[HowlBackfill] '{username}' (discord {discord_id}): ambiguous across servers — skipped"
                )
            else:
                stats["skipped_no_match"] += 1
                logger.info(
                    f"[HowlBackfill] '{username}' (discord {discord_id}): not in the affiliate window — skipped"
                )
            continue

        with engine.begin() as conn:
            # Re-check inside the transaction: another row (or a live
            # verification racing this script) may already hold the UID.
            holder = conn.execute(
                text("SELECT discord_id FROM raffle_shuffle_links WHERE howl_uid = :uid AND platform = 'howl'"),
                {"uid": uid},
            ).fetchone()
            if holder and int(holder[0]) != int(discord_id):
                stats["skipped_conflict"] += 1
                logger.warning(f"[HowlBackfill] '{username}' → UID {uid} already held by discord {holder[0]} — skipped")
                continue

            if dry_run:
                stats["updated"] += 1
                logger.info(f"[HowlBackfill] DRY RUN would set '{username}' (discord {discord_id}) → UID {uid}")
                continue

            conn.execute(
                text(
                    "UPDATE raffle_shuffle_links SET howl_uid = :uid "
                    "WHERE id = :rid AND platform = 'howl' AND howl_uid IS NULL"
                ),
                {"uid": uid, "rid": row_id},
            )
        if not dry_run:
            stats["updated"] += 1
            logger.info(f"[HowlBackfill] '{username}' (discord {discord_id}) → UID {uid}")

    verb = "would update" if dry_run else "updated"
    logger.info(
        f"[HowlBackfill] done: {stats['candidates']} candidates, {verb} {stats['updated']}, "
        f"{stats['skipped_no_match']} not found, {stats['skipped_conflict']} conflicts, "
        f"{stats['skipped_ambiguous']} ambiguous"
    )
    return stats


def main():
    parser = argparse.ArgumentParser(description="Backfill howl_uid on legacy Howl links")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    parser.add_argument("--server-id", type=int, default=None, help="only use this guild's Howl credentials")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"affiliate lookback window in days (default {DEFAULT_LOOKBACK_DAYS})",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    stats = backfill_howl_uid(engine, server_id=args.server_id, days=args.days, dry_run=args.dry_run)
    return 0 if stats["skipped_conflict"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
