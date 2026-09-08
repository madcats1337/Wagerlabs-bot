"""Database schema for the XP/leveling system.

Mirrored in Admin-Dashboard/app.py::run_migrations() — the dashboard is
read-only against this table (the bot is the sole writer), the same division
of labor as shuffle_wager_totals for the wager leaderboard.
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
"""


def setup_levels_database(engine) -> bool:
    """Create the user_levels table (idempotent).

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

        logger.debug("[levels] user_levels schema ready")
        return True
    except Exception as e:
        logger.error(f"[levels] failed to setup schema: {e}")
        return False
