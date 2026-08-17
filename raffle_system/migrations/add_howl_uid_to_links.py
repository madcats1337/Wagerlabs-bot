import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)


def migrate_add_howl_uid_to_links(engine):
    """
    Migration: Add howl_uid column to raffle_shuffle_links
    This allows storing the numeric Howl user ID for verification.
    """
    try:
        with engine.begin() as conn:
            # Add howl_uid column if it doesn't exist
            result = conn.execute(
                text(
                    """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'raffle_shuffle_links'
                AND column_name = 'howl_uid'
            """
                )
            )

            if not result.fetchone():
                # Add the column
                conn.execute(
                    text(
                        """
                    ALTER TABLE raffle_shuffle_links
                    ADD COLUMN IF NOT EXISTS howl_uid VARCHAR(255)
                """
                    )
                )

                logger.info("✅ Added 'howl_uid' column to raffle_shuffle_links table")
            else:
                logger.debug("✓ Column 'howl_uid' already exists in raffle_shuffle_links")

            return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False
