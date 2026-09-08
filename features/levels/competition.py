"""Recurring activity competitions: a time-boxed contest over XP earned
*within* the period, rewarding the top 3 most active members.

Deliberately separate from the community leaderboard. `user_levels.total_xp` is
a lifetime running total and keeps accumulating untouched; a competition score
counts only what was earned between its start and end. Both are written in the
SAME transaction (see engine.award_xp), so they can never drift apart.

Period lengths are calendar-relative for "monthly": a monthly competition that
starts on the 3rd ends on the 3rd, not 30 days later.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)

PERIOD_TYPES = ("weekly", "biweekly", "monthly")

PERIOD_LABELS = {
    "weekly": "Weekly",
    "biweekly": "Bi-Weekly",
    "monthly": "Monthly",
}

PRIZE_TYPES = ("usd", "points", "custom")

TOP_PLACES = 3


def _add_months(moment: datetime, months: int) -> datetime:
    """Same day-of-month `months` later, clamped to the target month's length.

    Calendar arithmetic, not 30-day arithmetic: a monthly competition started on
    the 3rd always ends on the 3rd. A start on the 31st clamps to the 28th/30th
    where the target month is shorter, which is the conventional behaviour and
    keeps end_date monotonic.
    """
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1

    # Days in the target month, without importing calendar for one lookup.
    if month == 12:
        next_month_start = datetime(year + 1, 1, 1, tzinfo=moment.tzinfo)
    else:
        next_month_start = datetime(year, month + 1, 1, tzinfo=moment.tzinfo)
    days_in_month = (next_month_start - timedelta(days=1)).day

    return moment.replace(year=year, month=month, day=min(moment.day, days_in_month))


def period_end(start: datetime, period_type: str) -> datetime:
    """End instant for a period of `period_type` beginning at `start`."""
    if period_type == "weekly":
        return start + timedelta(days=7)
    if period_type == "biweekly":
        return start + timedelta(days=14)
    if period_type == "monthly":
        return _add_months(start, 1)
    raise ValueError(f"unknown period_type: {period_type!r}")


def get_active_competition(engine, guild_id):
    """The server's active competition row, or None when there genuinely is none.

    RE-RAISES on database error rather than returning None. Returning None on a
    transient failure is the exact defect that destroyed a live raffle during a
    Postgres upgrade: every caller reads None as "no competition", and the
    scheduler would then end or recreate a competition that was running fine.
    """
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                SELECT id, discord_server_id, period_type, start_date, end_date,
                       status, auto_renew,
                       prize1_type, prize1_amount, prize1_text,
                       prize2_type, prize2_amount, prize2_text,
                       prize3_type, prize3_amount, prize3_text
                FROM level_competitions
                WHERE discord_server_id = :guild_id AND status = 'active'
                LIMIT 1
                """
                ),
                {"guild_id": guild_id},
            )
            .mappings()
            .fetchone()
        )
    return dict(row) if row else None


def record_competition_xp(conn, guild_id, discord_id, amount, is_message, username=None, avatar_url=None):
    """Add `amount` to the member's score in the server's active competition.

    Takes an open CONNECTION, not an engine: this runs inside award_xp's
    transaction so competition XP and lifetime XP commit together or not at all.

    No-ops when no competition is active. The competition lookup is folded into
    the INSERT ... SELECT so this costs one statement, not a round trip plus a
    write on every XP award.
    """
    conn.execute(
        text(
            """
            INSERT INTO level_competition_scores (
                competition_id, discord_id, username, avatar_url, xp, messages_sent, updated_at
            )
            SELECT c.id, :discord_id, :username, :avatar_url, :amount, :msg_inc, NOW()
            FROM level_competitions c
            WHERE c.discord_server_id = :guild_id
              AND c.status = 'active'
              -- Awards landing outside the window (a period that lapsed before
              -- the scheduler's next tick) must not count toward it.
              AND NOW() >= c.start_date
              AND NOW() < c.end_date
            ON CONFLICT (competition_id, discord_id) DO UPDATE SET
                xp = level_competition_scores.xp + :amount,
                messages_sent = level_competition_scores.messages_sent + :msg_inc,
                username = COALESCE(:username, level_competition_scores.username),
                avatar_url = COALESCE(:avatar_url, level_competition_scores.avatar_url),
                updated_at = NOW()
            """
        ),
        {
            "guild_id": guild_id,
            "discord_id": discord_id,
            "username": username,
            "avatar_url": avatar_url,
            "amount": amount,
            "msg_inc": 1 if is_message else 0,
        },
    )


def get_competition_board(engine, competition_id, limit=10):
    """Top `limit` scorers for a competition, ranked by period XP."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                SELECT discord_id, username, avatar_url, xp, messages_sent
                FROM level_competition_scores
                WHERE competition_id = :cid
                ORDER BY xp DESC, discord_id ASC
                LIMIT :limit
                """
                ),
                {"cid": competition_id, "limit": limit},
            )
            .mappings()
            .fetchall()
        )

    return [
        {
            "position": index + 1,
            "discord_id": row["discord_id"],
            "username": row["username"],
            "avatar_url": row["avatar_url"],
            "xp": row["xp"],
            "messages_sent": row["messages_sent"],
        }
        for index, row in enumerate(rows)
    ]


def get_competition_winners(engine, competition_id):
    """Frozen winner rows for a finished competition."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                SELECT place, discord_id, username, xp,
                       prize_type, prize_amount, prize_text, prize_paid_at, prize_paid_to
                FROM level_competition_winners
                WHERE competition_id = :cid
                ORDER BY place ASC
                """
                ),
                {"cid": competition_id},
            )
            .mappings()
            .fetchall()
        )
    return [dict(row) for row in rows]


def prize_for_place(competition, place: int):
    """(prize_type, amount, text) configured for `place` (1-3)."""
    return (
        competition.get(f"prize{place}_type"),
        competition.get(f"prize{place}_amount"),
        competition.get(f"prize{place}_text"),
    )


def freeze_winners(conn, competition):
    """Snapshot the top 3 into level_competition_winners.

    Winners are frozen rather than derived on read so a later score correction
    (or a renewed competition reusing the same members) can never rewrite who
    won. Idempotent: ON CONFLICT DO NOTHING means a retried end-of-period leaves
    the original result standing.
    """
    competition_id = competition["id"]
    board = (
        conn.execute(
            text(
                """
            SELECT discord_id, username, xp
            FROM level_competition_scores
            WHERE competition_id = :cid AND xp > 0
            ORDER BY xp DESC, discord_id ASC
            LIMIT :places
            """
            ),
            {"cid": competition_id, "places": TOP_PLACES},
        )
        .mappings()
        .fetchall()
    )

    for index, row in enumerate(board):
        place = index + 1
        prize_type, prize_amount, prize_text = prize_for_place(competition, place)
        conn.execute(
            text(
                """
                INSERT INTO level_competition_winners (
                    competition_id, place, discord_id, username, xp,
                    prize_type, prize_amount, prize_text
                )
                VALUES (:cid, :place, :discord_id, :username, :xp,
                        :prize_type, :prize_amount, :prize_text)
                ON CONFLICT (competition_id, place) DO NOTHING
                """
            ),
            {
                "cid": competition_id,
                "place": place,
                "discord_id": row["discord_id"],
                "username": row["username"],
                "xp": row["xp"],
                "prize_type": prize_type,
                "prize_amount": prize_amount,
                "prize_text": prize_text,
            },
        )

    return len(board)


def close_competition(engine, competition, *, manual=False):
    """Freeze winners and mark the competition ended, in one transaction.

    Returns True when THIS call performed the close. A concurrent or retried
    close returns False, so exactly one caller announces and pays out.
    """
    with engine.begin() as conn:
        claimed = conn.execute(
            text(
                """
                UPDATE level_competitions
                SET status = 'ended', ended_manually = :manual
                WHERE id = :cid AND status = 'active'
                RETURNING id
                """
            ),
            {"cid": competition["id"], "manual": manual},
        ).fetchone()

        # Someone else ended it between our read and this write.
        if claimed is None:
            return False

        freeze_winners(conn, competition)

    return True


def renew_competition(engine, competition):
    """Start the next period of the same length, carrying the prize config over.

    Chains from the finished period's end_date rather than "now" so a scheduler
    tick that runs late doesn't leave a gap in the ladder. If that would place
    the new period entirely in the past (the bot was down for a while), it walks
    forward until the period contains now.
    """
    start = competition["end_date"]
    period_type = competition["period_type"]
    now = datetime.now(timezone.utc)

    end = period_end(start, period_type)
    while end <= now:
        start = end
        end = period_end(start, period_type)

    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO level_competitions (
                    discord_server_id, period_type, start_date, end_date, status, auto_renew,
                    prize1_type, prize1_amount, prize1_text,
                    prize2_type, prize2_amount, prize2_text,
                    prize3_type, prize3_amount, prize3_text
                )
                VALUES (
                    :guild_id, :period_type, :start, :end, 'active', TRUE,
                    :p1t, :p1a, :p1x, :p2t, :p2a, :p2x, :p3t, :p3a, :p3x
                )
                -- The partial unique index makes a second active competition
                -- impossible; if one raced us in, keep theirs.
                ON CONFLICT DO NOTHING
                RETURNING id
                """
            ),
            {
                "guild_id": competition["discord_server_id"],
                "period_type": period_type,
                "start": start,
                "end": end,
                "p1t": competition.get("prize1_type"),
                "p1a": competition.get("prize1_amount"),
                "p1x": competition.get("prize1_text"),
                "p2t": competition.get("prize2_type"),
                "p2a": competition.get("prize2_amount"),
                "p2x": competition.get("prize2_text"),
                "p3t": competition.get("prize3_type"),
                "p3a": competition.get("prize3_amount"),
                "p3x": competition.get("prize3_text"),
            },
        ).fetchone()

    return row[0] if row else None


def mark_announced(engine, competition_id):
    """Claim the announcement for a finished competition.

    Returns True only for the caller that claims it, so a restart mid-close
    cannot post the podium card twice.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                UPDATE level_competitions
                SET announced_at = NOW()
                WHERE id = :cid AND announced_at IS NULL
                RETURNING id
                """
            ),
            {"cid": competition_id},
        ).fetchone()
    return row is not None
