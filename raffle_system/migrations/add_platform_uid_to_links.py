"""Add a generic `platform_uid` column to raffle_shuffle_links.

`howl_uid` was added when Howl was the only UID-verified casino. Roobet verifies
the same way (its affiliate stats carry a per-player `uid`), and storing a Roobet
id in a column named `howl_uid` is a trap for the next reader — so new UID-based
platforms write `platform_uid` instead.

Existing Howl rows are backfilled into the new column, and `howl_uid` is LEFT IN
PLACE and still written by the Howl panel: the dashboard and bot deploy
independently, so dropping or stopping writes to it here would break whichever
service is still running the old code. Retiring `howl_uid` is a separate cleanup
once both sides read `platform_uid` exclusively.

Mirrored in the dashboard (Admin-Dashboard/app.py run_migrations) because the two
services share this table and either may boot first.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def migrate_add_platform_uid_to_links(engine):
    """Add `platform_uid` and backfill it from `howl_uid`. Idempotent."""
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    ALTER TABLE raffle_shuffle_links
                    ADD COLUMN IF NOT EXISTS platform_uid VARCHAR(255)
                    """
                )
            )

            # Backfill from howl_uid for rows that predate this column. Scoped to
            # platform='howl' because howl_uid only ever held Howl ids.
            result = conn.execute(
                text(
                    """
                    UPDATE raffle_shuffle_links
                    SET platform_uid = howl_uid
                    WHERE platform = 'howl'
                      AND howl_uid IS NOT NULL
                      AND platform_uid IS NULL
                    """
                )
            )
            backfilled = result.rowcount or 0

            # One UID per platform, matching the platform-scoped uniqueness the
            # rest of this table uses. Partial so the many NULL rows (shuffle
            # links, legacy howl rows with no uid) don't collide with each other.
            conn.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS raffle_shuffle_links_platform_uid_key
                    ON raffle_shuffle_links (platform, platform_uid)
                    WHERE platform_uid IS NOT NULL
                    """
                )
            )

        if backfilled:
            logger.info(f"✅ platform_uid added; backfilled {backfilled} howl row(s)")
        else:
            logger.debug("✓ platform_uid present; nothing to backfill")
        return True

    except Exception as e:
        logger.error(f"Migration failed (add_platform_uid_to_links): {e}")
        return False
