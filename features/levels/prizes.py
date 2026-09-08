"""Prize payout for competition winners.

The three prize types split exactly the way trivia's already do (see
features/trivia/trivia_payout.py, which this generalises from one winner to
three):

  points  - credited automatically to the winner's loyalty balance
  usd     - a description; the operator settles it
  custom  - a description; the operator settles it

Both rules that module exists to get right carry over unchanged:

1. IDENTITY. A balance is shared per PERSON across Kick and Twitch and lives on
   ONE `user_points` row keyed by the canonical username - `resolve_shop_identity`'s
   `min()` of their lowercased linked handles. Crediting a Discord name, or one
   of two handles directly, strands the points on a row nothing reads.

2. EXACTLY ONCE. `level_competition_winners.prize_paid_at` is claimed in the
   SAME transaction that credits `user_points`, guarded on `prize_paid_at IS
   NULL`. A retry, a redelivered event or a restart mid-payout therefore commits
   both writes or neither.
"""

import logging
from enum import Enum

from sqlalchemy import text

logger = logging.getLogger(__name__)


class PayoutResult(str, Enum):
    """Why a payout did or did not happen - drives the announcement wording."""

    PAID = "paid"
    # No Kick/Twitch account linked, so there is no balance to credit.
    # Recoverable: they link, the operator grants manually. prize_paid_at stays
    # NULL so the prize reads as still owed.
    NOT_LINKED = "not_linked"
    ALREADY_PAID = "already_paid"
    # A usd/custom prize, or no prize configured for that place.
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


def points_prize_amount(winner) -> int:
    """Whole points this winner row pays, or 0 if it is not a points prize."""
    if (winner.get("prize_type") or "") != "points":
        return 0
    try:
        return max(0, int(float(winner.get("prize_amount") or 0)))
    except (TypeError, ValueError):
        return 0


def pay_points_prize(engine, guild_id, competition_id, winner):
    """Credit one winner's loyalty points. Returns (PayoutResult, canonical, amount).

    Safe to call for every winner of a finished competition - anything that is
    not an unpaid points prize returns NOT_APPLICABLE untouched.
    """
    amount = points_prize_amount(winner)
    winner_id = winner.get("discord_id")
    if not amount or not winner_id:
        return PayoutResult.NOT_APPLICABLE, None, 0
    if winner.get("prize_paid_at"):
        return PayoutResult.ALREADY_PAID, winner.get("prize_paid_to"), amount

    place = winner["place"]

    try:
        with engine.begin() as conn:
            # Same resolver the shop, !givepoints and trivia use - imported
            # lazily because bot.py imports this package, and the canonical rule
            # must have exactly one definition.
            from bot import resolve_shop_identity

            canonical, accounts = resolve_shop_identity(conn, int(winner_id), int(guild_id))
            if not accounts:
                return PayoutResult.NOT_LINKED, None, amount

            # Claim FIRST. No row back means another worker (or an earlier
            # delivery) already paid, and the credit below must not run.
            claimed = conn.execute(
                text(
                    """
                    UPDATE level_competition_winners
                    SET prize_paid_at = NOW(), prize_paid_to = :canonical
                    WHERE competition_id = :cid
                      AND place = :place
                      AND prize_paid_at IS NULL
                    RETURNING place
                    """
                ),
                {"canonical": canonical, "cid": competition_id, "place": place},
            ).fetchone()
            if not claimed:
                return PayoutResult.ALREADY_PAID, canonical, amount

            # Upsert, not UPDATE: a linked member who has never earned has no
            # user_points row yet, and the prize must still land.
            conn.execute(
                text(
                    """
                    INSERT INTO user_points (
                        kick_username, discord_id, points, total_earned,
                        discord_server_id, last_updated
                    )
                    VALUES (:u, :d, :p, :p, :sid, CURRENT_TIMESTAMP)
                    ON CONFLICT (kick_username, discord_server_id) DO UPDATE SET
                        points = user_points.points + :p,
                        total_earned = user_points.total_earned + :p,
                        discord_id = COALESCE(:d, user_points.discord_id),
                        last_updated = CURRENT_TIMESTAMP
                    """
                ),
                {"u": canonical, "d": int(winner_id), "p": amount, "sid": int(guild_id)},
            )

        logger.info(
            f"[levels] competition {competition_id}: paid {amount} points to {canonical} "
            f"(discord {winner_id}, place {place})"
        )
        return PayoutResult.PAID, canonical, amount
    except Exception as e:
        # The transaction rolled back, so prize_paid_at is still NULL and the
        # prize remains owed rather than silently lost.
        logger.error(
            f"[levels] competition {competition_id} payout failed for place {place}: {e}",
            exc_info=True,
        )
        return PayoutResult.FAILED, None, amount


def pay_all_prizes(engine, guild_id, competition_id, winners):
    """Run the points payout for every winner. Returns {place: PayoutResult}."""
    results = {}
    for winner in winners:
        result, _canonical, _amount = pay_points_prize(engine, guild_id, competition_id, winner)
        results[winner["place"]] = result
    return results


def describe_prize(prize_type, prize_amount, prize_text) -> str:
    """One-line prize description for cards and announcements."""
    if prize_type == "usd":
        try:
            return f"${float(prize_amount or 0):,.2f}"
        except (TypeError, ValueError):
            return "$0.00"
    if prize_type == "points":
        try:
            return f"{int(float(prize_amount or 0)):,} points"
        except (TypeError, ValueError):
            return "0 points"
    if prize_type == "custom":
        return (prize_text or "").strip() or "Custom prize"
    return ""
