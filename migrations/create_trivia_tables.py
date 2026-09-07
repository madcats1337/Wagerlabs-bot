"""
Migration: trivia_events - Discord community trivia events.

MIRROR of the `trivia_events` block in Admin-Dashboard/app.py::run_migrations.
Both copies exist because the two Railway services deploy independently and
either may boot first: whichever wins creates the table, and the other's
CREATE ... IF NOT EXISTS is a no-op. Keep the two in sync.

One row per event. Deadlines are absolute TIMESTAMPTZ (`answers_open_at`,
`ends_at`) so the panel can render client-ticking `<t:...:R>` countdowns and
so a bot restart can re-derive exactly how much time is left; the
`*_remaining_seconds` columns hold the frozen clock while an event is paused.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def migrate_create_trivia_tables(engine):
    """Create trivia_events and its indexes if they do not exist."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS trivia_events (
                        id SERIAL PRIMARY KEY,
                        discord_server_id BIGINT NOT NULL,
                        discord_channel_id BIGINT NOT NULL,
                        discord_message_id BIGINT,
                        question TEXT NOT NULL,
                        answer TEXT NOT NULL,
                        -- `prize` is the RENDERED display string; the bot
                        -- prints it verbatim and never interprets a type.
                        prize VARCHAR(255),
                        prize_type VARCHAR(20),
                        prize_amount NUMERIC(14, 2),
                        prep_seconds INTEGER NOT NULL DEFAULT 30,
                        duration_seconds INTEGER NOT NULL DEFAULT 120,
                        status VARCHAR(20) NOT NULL DEFAULT 'created',
                        started_at TIMESTAMPTZ,
                        answers_open_at TIMESTAMPTZ,
                        ends_at TIMESTAMPTZ,
                        paused_at TIMESTAMPTZ,
                        prep_remaining_seconds INTEGER,
                        duration_remaining_seconds INTEGER,
                        winner_discord_id BIGINT,
                        winner_name VARCHAR(255),
                        winner_answer TEXT,
                        won_at TIMESTAMPTZ,
                        created_by VARCHAR(255),
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        ended_at TIMESTAMPTZ
                    )
                    """
                )
            )

            # Optional prize, shown on the panel and named in the winner
            # announcement. Additive for tables created before it existed.
            conn.execute(
                text(
                    """
                    ALTER TABLE trivia_events
                        ADD COLUMN IF NOT EXISTS prize VARCHAR(255),
                        ADD COLUMN IF NOT EXISTS prize_type VARCHAR(20),
                        ADD COLUMN IF NOT EXISTS prize_amount NUMERIC(14, 2)
                    """
                )
            )

            # One live event per CHANNEL (the product rule). The partial unique
            # index is the real guarantee - the dashboard's pre-check only
            # exists to turn a violation into a readable 409.
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS trivia_events_one_live_per_channel
                    ON trivia_events (discord_server_id, discord_channel_id)
                    WHERE status IN ('created', 'started', 'paused')
                    """
                )
            )

            # Drives the startup re-arm scan (TriviaTicker.resume_all).
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_trivia_events_live
                    ON trivia_events (discord_server_id, status)
                    WHERE status IN ('created', 'started', 'paused')
                    """
                )
            )

        logger.debug("✅ trivia_events table ready")
        return True
    except Exception as e:
        # Non-fatal: the dashboard's run_migrations creates the same table, so a
        # failure here (a race with that deploy, a transient DB blip) resolves
        # itself rather than blocking bot startup.
        logger.warning(f"trivia_events migration skipped: {e}")
        return False
