"""
Subscription tier lookups for the bot.

The bot shares ONE Postgres database with the Admin-Dashboard, which owns the
`server_subscriptions` table. This module reads (never writes) that table to
decide whether a per-server feature is enabled for a guild, so chat commands
can be gated by the same tiers the dashboard enforces.

Mirrors Admin-Dashboard/utils/tiers.py — keep the feature map and effective-
tier rules in sync between the two repos.

Sync by design: the bot's command handlers already use synchronous
`engine.connect()` queries inside async callbacks, so these helpers follow the
same idiom (no await).
"""

import logging
import time

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Cumulative feature -> tier map. MUST match Admin-Dashboard/utils/tiers.py.
FREE = ["bonus_hunt", "slot_requests", "gtb", "widgets", "extension"]
TIER2 = FREE + [
    "point_shop",
    "point_rewards",
    "commands",
    "timed_messages",
    "personal_subdomain",
    "giveaway",
    "tournament",
]
TIER3 = TIER2 + [
    "full_dashboard",
    "raffle",
    "clips",
    "wagers",
    # Running more than one stream platform (Kick + Twitch) at once. Mirror of
    # Admin-Dashboard/utils/tiers.py.
    "multi_platform",
]
TIER4 = TIER3 + [
    # Public affiliate leaderboard page generator. Mirror of
    # Admin-Dashboard/utils/tiers.py.
    "leaderboard_generator",
]

TIER_FEATURES = {"free": FREE, "tier2": TIER2, "tier3": TIER3, "tier4": TIER4}

# Features that need a real Discord guild. Standalone (Discord-less) workspaces
# — servers rows with a synthetic NEGATIVE discord_server_id, created by email
# accounts on the dashboard — never have these regardless of tier. MUST match
# Admin-Dashboard/utils/tiers.py REQUIRES_DISCORD.
REQUIRES_DISCORD = {
    "commands",
    "timed_messages",
    "point_shop",
    "point_rewards",
    "giveaway",
    "raffle",
    "clips",
    "wagers",
    "tournament",
    "full_dashboard",
    "leaderboard_generator",
}


def is_standalone_server(guild_id) -> bool:
    """True for synthetic (guild-less) workspace ids."""
    try:
        return guild_id is not None and int(guild_id) < 0
    except (TypeError, ValueError):
        return False


# Small in-process cache so we don't hit the DB on every chat line. Keyed by
# guild_id -> (tier, expires_at). The dashboard caches in Redis (~60s); a short
# local TTL here is enough since tier changes are rare and not latency-critical.
_CACHE_TTL = 60.0
_cache: dict[int, tuple[str, float]] = {}


def get_server_tier(engine, guild_id) -> str:
    """Effective tier for a guild: 'free' | 'tier2' | 'tier3' | 'tier4'.

    Resolution order (mirrors the dashboard):
      1. No row / no guild      -> 'free'
      2. manual_override        -> override_tier
      3. status past_due/cancel -> 'free'
      4. otherwise              -> stored tier

    Fails open to 'free' on any DB error so a billing hiccup never blocks chat.
    """
    if guild_id is None:
        return "free"
    try:
        guild_id = int(guild_id)
    except (TypeError, ValueError):
        return "free"

    now = time.monotonic()
    cached = _cache.get(guild_id)
    if cached and cached[1] > now:
        return cached[0]

    tier = "free"
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT tier, status, manual_override, override_tier
                    FROM server_subscriptions
                    WHERE discord_server_id = :guild_id
                    """
                ),
                {"guild_id": guild_id},
            ).fetchone()
        if row:
            db_tier, status, manual_override, override_tier = row[0], row[1], row[2], row[3]
            if manual_override and override_tier:
                tier = override_tier
            elif status in ("past_due", "canceled"):
                tier = "free"
            else:
                tier = db_tier or "free"
    except Exception as e:
        logger.warning(f"get_server_tier({guild_id}) failed, defaulting to free: {e}")
        return "free"

    if tier not in TIER_FEATURES:
        tier = "free"

    _cache[guild_id] = (tier, now + _CACHE_TTL)
    return tier


def server_has_feature(engine, guild_id, feature_key: str) -> bool:
    """True if the guild's effective tier grants `feature_key`.

    Standalone (guild-less) workspaces never get the Discord-dependent
    features regardless of tier — mirrors the dashboard's server_has_feature.
    """
    if feature_key in REQUIRES_DISCORD and is_standalone_server(guild_id):
        return False
    return feature_key in TIER_FEATURES.get(get_server_tier(engine, guild_id), FREE)


# Tier ordering for picking a user's "highest" paid tier.
_TIER_RANK = {"free": 0, "tier2": 1, "tier3": 2, "tier4": 3}


def get_user_highest_tier(engine, discord_id) -> str:
    """The highest effective tier across all servers a Discord user administers.

    Drives the subscription-role panel: a user clicks "claim" and gets the role
    for the best active paid tier among the servers they admin (recorded in
    `server_admins` at dashboard login). Returns 'free' if they administer no
    server / no paid server / on any error (fails open to no paid role).
    """
    if discord_id is None:
        return "free"
    try:
        discord_id = int(discord_id)
    except (TypeError, ValueError):
        return "free"

    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT discord_server_id FROM server_admins WHERE discord_id = :d"),
                {"d": discord_id},
            ).fetchall()
    except Exception as e:
        logger.warning(f"get_user_highest_tier({discord_id}) failed: {e}")
        return "free"

    best = "free"
    for (server_id,) in rows:
        tier = get_server_tier(engine, server_id)
        if _TIER_RANK.get(tier, 0) > _TIER_RANK.get(best, 0):
            best = tier
    return best


def tier_needed_for(feature_key: str) -> str:
    """Cheapest tier that grants `feature_key` (for upgrade messages)."""
    for tier in ("free", "tier2", "tier3", "tier4"):
        if feature_key in TIER_FEATURES[tier]:
            return tier
    return "tier3"


# Human labels for chat replies.
TIER_LABEL = {"free": "Tier 1", "tier2": "Tier 2", "tier3": "Tier 3", "tier4": "Tier 4"}


def upgrade_message(username: str, feature_key: str) -> str:
    """Standard chat reply when a command is gated by tier."""
    needed = TIER_LABEL.get(tier_needed_for(feature_key), "a higher tier")
    return f"@{username} This feature requires {needed}. Ask the streamer to upgrade on the dashboard."


def invalidate_cache(guild_id=None) -> None:
    """Drop the local tier cache (all, or one guild). Called by the Redis
    subscriber's handle_subscriptions_event on every dashboard tier change so
    gating reflects the new tier instantly."""
    if guild_id is None:
        _cache.clear()
    else:
        try:
            _cache.pop(int(guild_id), None)
        except (TypeError, ValueError):
            pass
