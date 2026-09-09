"""XP award engine: the single entry point for every XP-earning event
(message activity, giveaway/trivia/raffle wins). Every award goes through
`award_xp` so level/rank transitions are detected and announced from one
place, regardless of source.

It is also where an active competition's score is incremented, in the same
transaction as the lifetime total. Funnelling both through this one function is
what keeps "XP earned this period" honest without a second code path per XP
source, and what makes it impossible for the two totals to drift.
"""

import logging

from sqlalchemy import text

from .cards import render_levelup_card, render_rankup_card
from .competition import record_competition_xp
from .curve import level_from_total_xp, rank_for_level

logger = logging.getLogger(__name__)


def _guild_settings(bot, guild_id):
    """The guild's BotSettingsManager, or None when it can't be resolved.

    Read through the bot's shared getter (bot.get_guild_settings, set in
    bot.py) rather than constructing one: that cache is what the dashboard's
    `dashboard:bot_settings` sync refreshes, so a channel/toggle change lands
    here without a bot restart.
    """
    getter = getattr(bot, "get_guild_settings", None)
    if not callable(getter):
        return None
    try:
        return getter(guild_id)
    except Exception as e:
        logger.warning(f"[levels] could not load settings for guild {guild_id}: {e}")
        return None


async def award_xp(engine, bot, guild_id, discord_id, amount: int, source: str, username=None):
    """Award `amount` XP to a member, upsert their row, and announce any
    resulting level-up / rank-up. `source` is one of "message", "giveaway",
    "trivia", "raffle" (informational, logged only).
    """
    guild_id = int(guild_id)
    discord_id = int(discord_id)

    settings = _guild_settings(bot, guild_id)

    # A server that switched leveling off stops EARNING, not just announcing.
    # Gating only the announcement would let XP pile up invisibly while it's
    # off, then jump the whole leaderboard the moment it's switched back on.
    if settings is not None and not settings.levels_enabled:
        return

    # Refresh identity from a live Member when possible. Denormalized onto the
    # row so the dashboard's public leaderboard page (which cannot call the
    # login-gated Discord user endpoint) never needs a live Discord API call.
    avatar_url = None
    resolved_username = username
    try:
        guild = bot.get_guild(guild_id)
        member = guild.get_member(discord_id) if guild else None
        if member:
            resolved_username = member.display_name
            avatar_url = str(member.display_avatar.url)
    except Exception:
        pass

    try:
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    INSERT INTO user_levels (
                        discord_server_id, discord_id, username, avatar_url,
                        total_xp, messages_sent, current_level, current_rank,
                        last_xp_award_at, updated_at
                    )
                    VALUES (
                        :guild_id, :discord_id, :username, :avatar_url,
                        :amount, :msg_inc, 0, 'bronze', NOW(), NOW()
                    )
                    ON CONFLICT (discord_server_id, discord_id) DO UPDATE SET
                        total_xp = user_levels.total_xp + :amount,
                        messages_sent = user_levels.messages_sent + :msg_inc,
                        username = COALESCE(:username, user_levels.username),
                        avatar_url = COALESCE(:avatar_url, user_levels.avatar_url),
                        last_xp_award_at = NOW(),
                        updated_at = NOW()
                    -- current_level/current_rank are deliberately NOT touched
                    -- by the upsert, so RETURNING hands back the PREVIOUS
                    -- level/rank next to the NEW total — that pairing is what
                    -- detects the transition below.
                    RETURNING total_xp, current_level, current_rank
                    """
                ),
                {
                    "guild_id": guild_id,
                    "discord_id": discord_id,
                    "username": resolved_username,
                    "avatar_url": avatar_url,
                    "amount": amount,
                    "msg_inc": 1 if source == "message" else 0,
                },
            ).fetchone()
            new_total_xp, old_level, old_rank = row

            # Same transaction as the lifetime upsert above: a crash between the
            # two would otherwise leave the community board and the competition
            # board disagreeing about the same award, with nothing to reconcile
            # them from. No-ops when no competition is running.
            record_competition_xp(
                conn,
                guild_id,
                discord_id,
                amount,
                is_message=(source == "message"),
                username=resolved_username,
                avatar_url=avatar_url,
            )

            new_level = level_from_total_xp(new_total_xp)
            new_rank = rank_for_level(new_level)

            if new_level != old_level or new_rank != old_rank:
                conn.execute(
                    text(
                        """
                        UPDATE user_levels SET current_level = :level, current_rank = :rank
                        WHERE discord_server_id = :guild_id AND discord_id = :discord_id
                        """
                    ),
                    {"level": new_level, "rank": new_rank, "guild_id": guild_id, "discord_id": discord_id},
                )
    except Exception as e:
        logger.error(f"[levels] award_xp failed for {discord_id}@{guild_id}: {e}")
        return

    if new_level == old_level and new_rank == old_rank:
        return

    logger.info(
        f"[levels] {resolved_username or discord_id} in guild {guild_id}: "
        f"level {old_level}->{new_level}, rank {old_rank}->{new_rank} (source={source})"
    )

    await _announce_transition(
        bot,
        settings,
        guild_id,
        discord_id,
        resolved_username or str(discord_id),
        avatar_url,
        new_level,
        old_rank,
        new_rank,
    )


async def _announce_transition(
    bot, settings, guild_id, discord_id, username, avatar_url, new_level, old_rank, new_rank
):
    """Post the level-up / rank-up card in the configured channel.

    A rank change always implies a level change too, so the rank-up card wins
    and only ONE message is posted per award.

    These cards go to levels_announcement_channel_id, which is separate from
    the standing leaderboard panel's channel (levels_channel_id) — per-award
    chatter and a pinned board want different homes.
    """
    if settings is None:
        return

    # Dedicated announcement channel, falling back to the leaderboard panel's
    # channel for servers configured before the two were split.
    channel_id = getattr(settings, "levels_announcement_channel_id", None) or getattr(
        settings, "levels_channel_id", None
    )
    if not channel_id:
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            logger.warning(f"[levels] announcement channel {channel_id} not found for guild {guild_id}: {e}")
            return

    try:
        if new_rank != old_rank:
            file = await render_rankup_card(username, avatar_url, old_rank, new_rank, new_level)
        else:
            file = await render_levelup_card(username, avatar_url, new_level, new_rank)
        await channel.send(content=f"<@{discord_id}>", file=file)
    except Exception as e:
        logger.error(f"[levels] failed to post level/rank-up card: {e}")
