"""Database schema for the XP/leveling system.

Mirrored in Admin-Dashboard/app.py::run_migrations(). The bot is the sole
writer of scores and winners — the dashboard reads those, and only writes the
competition row itself (create/edit/end from the Levels page). Same division of
labor as shuffle_wager_totals for the wager leaderboard.

Competition scores live in their own table rather than being derived from an XP
ledger, because there is no ledger: user_levels.total_xp is a running total.
Windowing "XP earned since the competition started" off history would mean
recording every award forever; a per-competition counter is O(1) per award and
keeps no history at all.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

LEVELS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS user_levels (
    discord_server_id BIGINT NOT NULL,
    discord_id BIGINT NOT NULL,
    username TEXT,
    avatar_url TEXT,
    total_xp BIGINT NOT NULL DEFAULT 0,
    messages_sent BIGINT NOT NULL DEFAULT 0,
    current_level INTEGER NOT NULL DEFAULT 0,
    current_rank TEXT NOT NULL DEFAULT 'bronze',
    last_xp_award_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (discord_server_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_user_levels_leaderboard ON user_levels (discord_server_id, total_xp DESC);

CREATE TABLE IF NOT EXISTS level_competitions (
    id SERIAL PRIMARY KEY,
    discord_server_id BIGINT NOT NULL,
    period_type TEXT NOT NULL,
    start_date TIMESTAMPTZ NOT NULL,
    end_date TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    ended_manually BOOLEAN NOT NULL DEFAULT FALSE,
    auto_renew BOOLEAN NOT NULL DEFAULT TRUE,
    prize1_type TEXT,
    prize1_amount NUMERIC(12, 2),
    prize1_text TEXT,
    prize2_type TEXT,
    prize2_amount NUMERIC(12, 2),
    prize2_text TEXT,
    prize3_type TEXT,
    prize3_amount NUMERIC(12, 2),
    prize3_text TEXT,
    announced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_competition
    ON level_competitions (discord_server_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS level_competition_scores (
    competition_id INTEGER NOT NULL REFERENCES level_competitions(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    username TEXT,
    avatar_url TEXT,
    xp INTEGER NOT NULL DEFAULT 0,
    messages_sent INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (competition_id, discord_id)
);

CREATE INDEX IF NOT EXISTS idx_competition_scores_board
    ON level_competition_scores (competition_id, xp DESC);

CREATE TABLE IF NOT EXISTS level_competition_winners (
    competition_id INTEGER NOT NULL REFERENCES level_competitions(id) ON DELETE CASCADE,
    place SMALLINT NOT NULL,
    discord_id BIGINT NOT NULL,
    username TEXT,
    xp INTEGER NOT NULL,
    prize_type TEXT,
    prize_amount NUMERIC(12, 2),
    prize_text TEXT,
    prize_paid_at TIMESTAMPTZ,
    prize_paid_to TEXT,
    PRIMARY KEY (competition_id, place)
);
"""


def setup_levels_database(engine) -> bool:
    """Create the levels + competition tables (idempotent).

    Mirrors raffle_system/database.py::setup_raffle_database's
    split-and-tolerate-per-statement-errors pattern.
    """
    try:
        with engine.begin() as conn:
            statements = []
            current_statement = []

            for line in LEVELS_SCHEMA_SQL.split("\n"):
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue

                current_statement.append(line)
                if stripped.endswith(";"):
                    statements.append("\n".join(current_statement))
                    current_statement = []

            for statement in statements:
                if statement.strip():
                    try:
                        conn.execute(text(statement))
                    except Exception as e:
                        logger.debug(f"[levels] statement warning: {e}")

        logger.debug("[levels] schema ready")
        return True
    except Exception as e:
        logger.error(f"[levels] failed to setup schema: {e}")
        return False
