"""Schema for the commit-reveal gambling provably-fair system.

Creates ``gambling_seeds`` (one active seed pair per player per server, plus the
retired pairs whose seeds have been revealed) and adds the commit-reveal columns
to ``gambling_history`` so each recorded bet is self-contained for a verifier -
the same shape ``raffle_draws`` uses.

Mirrored in the dashboard repo's ``run_migrations()``: both Railway services
deploy independently and either may boot first, so each must be able to create
what it reads.
"""

import logging

logger = logging.getLogger(__name__)


def migrate_gambling_provably_fair(engine):
    """Create gambling_seeds and extend gambling_history (idempotent)."""
    try:
        raw_conn = engine.raw_connection()
        cursor = raw_conn.cursor()

        logger.debug("Checking gambling provably-fair schema...")

        # --- gambling_seeds: the commit-reveal seed pairs ---
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS gambling_seeds (
                id SERIAL PRIMARY KEY,
                discord_server_id BIGINT NOT NULL,
                discord_id BIGINT NOT NULL,
                server_seed TEXT NOT NULL,
                server_seed_commitment TEXT NOT NULL,
                client_seed TEXT NOT NULL,
                nonce BIGINT NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revealed_at TIMESTAMP
            );
            """
        )

        # One ACTIVE pair per player per server. A partial unique index (rather
        # than a plain UNIQUE) leaves retired pairs unconstrained, so a player can
        # accumulate any number of revealed seeds while never holding two live
        # ones - the same invariant the raffle uses for its active period.
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gambling_seeds_one_active
            ON gambling_seeds (discord_server_id, discord_id)
            WHERE is_active = TRUE;
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gambling_seeds_lookup
            ON gambling_seeds (discord_server_id, discord_id, is_active);
            """
        )

        # --- gambling_history: commit-reveal columns ---
        # server_seed becomes nullable-by-policy: it is written only once the
        # owning seed pair is revealed, so a live bet cannot leak its own seed.
        for column, ddl in (
            ("seed_id", "BIGINT"),
            ("server_seed_commitment", "TEXT"),
        ):
            cursor.execute(f"ALTER TABLE gambling_history ADD COLUMN IF NOT EXISTS {column} {ddl}")

        # The old schema declared server_seed NOT NULL, which the commit-reveal
        # model cannot satisfy: the seed is withheld until rotation. Drop the
        # constraint if it is still there.
        cursor.execute(
            """
            SELECT is_nullable FROM information_schema.columns
            WHERE table_name = 'gambling_history' AND column_name = 'server_seed'
            """
        )
        row = cursor.fetchone()
        if row and row[0] == "NO":
            logger.info("   Dropping NOT NULL on gambling_history.server_seed (withheld until reveal)")
            cursor.execute("ALTER TABLE gambling_history ALTER COLUMN server_seed DROP NOT NULL")

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gambling_history_seed
            ON gambling_history (seed_id);
            """
        )

        raw_conn.commit()
        cursor.close()
        raw_conn.close()

        logger.info("Gambling provably-fair schema ready")

    except Exception as e:
        logger.error(f"Gambling provably-fair migration failed: {e}")
        raise


if __name__ == "__main__":
    import os

    from sqlalchemy import create_engine

    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise SystemExit("ERROR: DATABASE_URL environment variable not set")

    migrate_gambling_provably_fair(create_engine(DATABASE_URL))
