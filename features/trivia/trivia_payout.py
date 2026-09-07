"""Automatic loyalty-point payout for a trivia winner.

Runs only for `prize_type = 'points'`. USD and custom prizes are descriptions
the operator settles themselves - nothing here touches them.

Two things this module exists to get right:

1. IDENTITY. A point balance is shared per PERSON across Kick and Twitch, and
   lives on ONE `user_points` row keyed by the canonical username -
   `resolve_shop_identity`'s `min()` of the person's lowercased linked handles,
   which is the same row the watchtime tracker writes to. Crediting the winner's
   Discord name, or one of their two handles directly, would strand the points
   on a row nothing reads. A winner with no linked account therefore cannot be
   paid at all: there is no row to credit, and the announcement tells them to
   link.

2. EXACTLY ONCE. `trivia_events.prize_paid_at` is claimed in the SAME
   transaction that credits `user_points`, guarded on `prize_paid_at IS NULL`.
   A retry, a duplicated Redis event or a restart mid-payout therefore either
   commits both writes or neither - it can never pay twice, and it can never
   mark a payout it did not make.
"""

import logging
from enum import Enum

from sqlalchemy import text

logger = logging.getLogger(__name__)


class PayoutResult(str, Enum):
    """Why a payout did or did not happen - drives the announcement wording."""

    PAID = "paid"
    # The winner has no Kick/Twitch account linked, so they have no balance to
    # credit. Recoverable: they link, the operator grants manually.
    NOT_LINKED = "not_linked"
    # Already credited (a retry), or this event has no points prize.
    ALREADY_PAID = "already_paid"
    NOT_APPLICABLE = "not_applicable"
    FAILED = "failed"


def points_prize_amount(event) -> int:
    """The whole number of points this event pays, or 0 if it is not a points prize."""
    if (event.get("prize_type") or "") != "points":
        return 0
    try:
        return max(0, int(float(event.get("prize_amount") or 0)))
    except (TypeError, ValueError):
        return 0


def pay_points_prize(engine, event):
    """Credit the winner's loyalty points. Returns (PayoutResult, canonical, amount).

    Safe to call for any finished event - it returns NOT_APPLICABLE for anything
    that is not an unpaid points prize with a winner.
    """
    amount = points_prize_amount(event)
    winner_id = event.get("winner_discord_id")
    if not amount or not winner_id:
        return PayoutResult.NOT_APPLICABLE, None, 0
    if event.get("prize_paid_at"):
        return PayoutResult.ALREADY_PAID, event.get("prize_paid_to"), amount

    guild_id = int(event["discord_server_id"])
    event_id = event["id"]

    try:
        with engine.begin() as conn:
            # Same resolver the shop and !givepoints use - imported lazily
            # because bot.py imports this package, and the canonical rule must
            # have exactly one definition.
            from bot import resolve_shop_identity

            canonical, accounts = resolve_shop_identity(conn, int(winner_id), guild_id)
            if not accounts:
                # Nothing to credit, and nothing to record: leaving
                # `prize_paid_at` NULL keeps the door open for a manual grant
                # once they link.
                return PayoutResult.NOT_LINKED, None, amount

            # Claim the payout FIRST. If this returns no row another worker (or
            # an earlier delivery of the same event) already paid, and the
            # credit below must not run.
            claimed = conn.execute(
                text(
                    """
                    UPDATE trivia_events
                    SET prize_paid_at = NOW(), prize_paid_to = :canonical
                    WHERE id = :eid
                      AND discord_server_id = :sid
                      AND prize_paid_at IS NULL
                      AND winner_discord_id IS NOT NULL
                    RETURNING id
                    """
                ),
                {"canonical": canonical, "eid": event_id, "sid": guild_id},
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
                {"u": canonical, "d": int(winner_id), "p": amount, "sid": guild_id},
            )

        logger.info(f"[trivia] paid {amount} points to {canonical} (discord {winner_id}) for event {event_id}")
        return PayoutResult.PAID, canonical, amount
    except Exception as e:
        # The transaction rolled back, so `prize_paid_at` is still NULL and the
        # prize remains owed rather than silently lost.
        logger.error(f"[trivia] points payout failed for event {event_id}: {e}", exc_info=True)
        return PayoutResult.FAILED, None, amount


def payout_sentence(result, amount, prize_label) -> str:
    """The clause appended to the winner announcement for each outcome."""
    if result == PayoutResult.PAID:
        return f" **{amount:,}** points have been added to your balance."
    if result == PayoutResult.ALREADY_PAID:
        return f" **{amount:,}** points are already on your balance."
    if result == PayoutResult.NOT_LINKED:
        # There is no `!link` command in this bot - linking is a button flow on
        # the server's Link Account panel, which is how every other "you must
        # link" message in the codebase phrases it.
        return (
            f" You have **{amount:,}** points waiting: link your Kick or Twitch account"
            " with this server's Link Account panel to receive them."
        )
    if result == PayoutResult.FAILED:
        return f" **{amount:,}** points could not be credited automatically - an admin will sort it out."
    return f" Prize: **{prize_label}**." if prize_label else ""
