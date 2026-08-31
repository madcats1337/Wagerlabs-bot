"""
Migration: Add `platform_user_id` to the `links` table — the viewer's IMMUTABLE
id on their streaming platform (Kick `user_id`, Twitch `id`).

Why this exists
---------------
`links.kick_name` stores a MUTABLE handle. Kick and Twitch both allow username
changes, so a handle is not a safe join key for anything that resolves to a
points balance: if viewer A renames off `bob` and viewer B later claims `bob`,
a handle lookup resolves B onto A's balance. `discord_id` is immutable and safe;
the platform handle is not.

Both OAuth callbacks already FETCH the immutable id and discard it
(core/oauth_server.py: `get_kick_user_info` returns `id`; the Twitch
`helix/users` response carries `id`), so populating this column needs no new API
calls and no new OAuth scopes — only persistence.

Naming
------
`platform_user_id`, not `kick_user_id`. add_platform_to_links.py deliberately
kept `kick_name` as the GENERIC per-platform username column (a twitch row
stores its Twitch login there) because renaming it is high-blast-radius. This
column follows that convention rather than compounding the mismatch.

VARCHAR(64) rather than BIGINT: Kick returns an integer, Twitch a numeric
string, and a future platform may use a non-numeric id.

Constraints
-----------
The unique index is PARTIAL (`WHERE platform_user_id IS NOT NULL`). Legacy rows
are NULL and Postgres treats NULLs as distinct, so the constraint binds only
populated rows and the migration cannot fail on existing data. It is
3-column INCLUDING platform — add_platform_to_links.py owns links uniqueness and
a 2-column UNIQUE on this table must never be reintroduced.

Idempotent + safe to re-run: ADD COLUMN IF NOT EXISTS, CREATE INDEX IF NOT EXISTS.
"""

import logging
import os

logger = logging.getLogger(__name__)

_COLUMN_SQL = "ALTER TABLE links ADD COLUMN IF NOT EXISTS platform_user_id VARCHAR(64)"

# Partial so legacy NULL rows never collide; 3-column so it composes with the
# platform-scoped uniqueness add_platform_to_links.py established.
_UNIQUE_INDEX_SQL = """
    CREATE UNIQUE INDEX IF NOT EXISTS links_platform_user_id_server_unique
        ON links (platform, platform_user_id, discord_server_id)
        WHERE platform_user_id IS NOT NULL
"""

# Lookup index for the resolve-by-id path (the API's preferred identity route).
_LOOKUP_INDEX_SQL = """
    CREATE INDEX IF NOT EXISTS idx_links_platform_user_id
        ON links (platform_user_id, discord_server_id)
        WHERE platform_user_id IS NOT NULL
"""


def migrate_add_platform_user_id_to_links(engine):
    """Add links.platform_user_id plus its partial unique + lookup indexes."""
    try:
        raw_conn = engine.raw_connection()
        cursor = raw_conn.cursor()

        # Table may not exist yet on a fresh DB — bot.py creates it at startup.
        cursor.execute("SELECT to_regclass('public.links')")
        if not cursor.fetchone()[0]:
            logger.debug("   ℹ️ links table doesn't exist yet — skipping (bot.py will create it)")
            cursor.close()
            raw_conn.close()
            return

        logger.debug("🔄 Adding platform_user_id to links...")

        cursor.execute(_COLUMN_SQL)
        cursor.execute(_UNIQUE_INDEX_SQL)
        cursor.execute(_LOOKUP_INDEX_SQL)

        raw_conn.commit()
        cursor.close()
        raw_conn.close()
        logger.debug("✅ links.platform_user_id present")
    except Exception as e:
        logger.error(f"❌ Migration failed (add_platform_user_id_to_links): {e}")
        raise


if __name__ == "__main__":
    from sqlalchemy import create_engine

    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL environment variable not set")
        exit(1)
    engine = create_engine(DATABASE_URL)
    migrate_add_platform_user_id_to_links(engine)
