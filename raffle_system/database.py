"""
Database Schema Setup for Raffle System
Creates all tables, indices, and views needed for the monthly raffle
"""

import hashlib
import logging
import secrets

from sqlalchemy import text

logger = logging.getLogger(__name__)

# SQL schema for raffle system
RAFFLE_SCHEMA_SQL = """
-- ============================================
-- RAFFLE SYSTEM DATABASE SCHEMA
-- ============================================

-- Raffle periods
CREATE TABLE IF NOT EXISTS raffle_periods (
    id SERIAL PRIMARY KEY,
    discord_server_id BIGINT NOT NULL,  -- Multi-server support
    start_date TIMESTAMP NOT NULL,
    end_date TIMESTAMP NOT NULL,
    status VARCHAR(20) DEFAULT 'active',  -- active, ended, archived
    total_tickets INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- TRUE when a person ended this period (dashboard button or !raffleend)
    -- rather than it lapsing at end_date. Auto-renew must not roll a manually
    -- ended period over. Mirrored in the dashboard's run_migrations().
    ended_manually BOOLEAN NOT NULL DEFAULT FALSE
);

-- Existing installs: the CREATE above is a no-op once the table exists.
ALTER TABLE raffle_periods ADD COLUMN IF NOT EXISTS ended_manually BOOLEAN NOT NULL DEFAULT FALSE;

-- User ticket balances (resets monthly)
CREATE TABLE IF NOT EXISTS raffle_tickets (
    id SERIAL PRIMARY KEY,
    period_id INTEGER REFERENCES raffle_periods(id) ON DELETE CASCADE,
    discord_server_id BIGINT NOT NULL,  -- Multi-server support
    discord_id BIGINT NOT NULL,
    kick_name TEXT NOT NULL,

    -- Ticket sources
    watchtime_tickets INTEGER DEFAULT 0,
    gifted_sub_tickets INTEGER DEFAULT 0,
    shuffle_wager_tickets INTEGER DEFAULT 0,
    bonus_tickets INTEGER DEFAULT 0,  -- Manual admin awards

    -- Totals
    total_tickets INTEGER DEFAULT 0,

    -- Metadata
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(period_id, discord_id)
);

-- Watchtime conversion tracking (prevent double-counting)
CREATE TABLE IF NOT EXISTS raffle_watchtime_converted (
    id SERIAL PRIMARY KEY,
    period_id INTEGER REFERENCES raffle_periods(id) ON DELETE CASCADE,
    kick_name TEXT NOT NULL,
    minutes_converted INTEGER NOT NULL,
    tickets_awarded INTEGER NOT NULL,
    converted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(period_id, kick_name)
);

-- Gifted sub event log
CREATE TABLE IF NOT EXISTS raffle_gifted_subs (
    id SERIAL PRIMARY KEY,
    period_id INTEGER REFERENCES raffle_periods(id) ON DELETE CASCADE,
    gifter_kick_name TEXT NOT NULL,
    gifter_discord_id BIGINT,  -- NULL if not linked
    recipient_kick_name TEXT,
    sub_count INTEGER DEFAULT 1,  -- For multi-gifts
    tickets_awarded INTEGER NOT NULL,
    gifted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    kick_event_id TEXT UNIQUE  -- Prevent duplicate processing
);

-- Shuffle.com wager tracking
CREATE TABLE IF NOT EXISTS raffle_shuffle_wagers (
    id SERIAL PRIMARY KEY,
    period_id INTEGER REFERENCES raffle_periods(id) ON DELETE CASCADE,
    shuffle_username TEXT NOT NULL,
    kick_name TEXT,  -- If we can map shuffle→kick
    discord_id BIGINT,  -- NULL if not linked

    -- Wager tracking
    total_wager_usd DECIMAL(15, 2) DEFAULT 0,
    last_known_wager DECIMAL(15, 2) DEFAULT 0,
    tickets_awarded INTEGER DEFAULT 0,

    -- Metadata
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    UNIQUE(period_id, shuffle_username)
);

-- Period-INDEPENDENT lifetime wager totals (powers the Tier-4 wager leaderboard).
-- Unlike raffle_shuffle_wagers (which is FK'd to a raffle period and only written
-- while a raffle is active), this table is upserted on EVERY tracker poll
-- regardless of raffle state, so the dashboard leaderboard always has fresh
-- totals to read without hitting shuffle's rate-limited API itself. One row per
-- (server, platform, username); total_wager_usd is the current lifetime figure.
CREATE TABLE IF NOT EXISTS shuffle_wager_totals (
    id SERIAL PRIMARY KEY,
    discord_server_id BIGINT NOT NULL,
    platform VARCHAR(50) NOT NULL DEFAULT 'shuffle',
    shuffle_username TEXT NOT NULL,
    kick_name TEXT,
    discord_id BIGINT,
    total_wager_usd DECIMAL(15, 2) DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(discord_server_id, platform, shuffle_username)
);

-- Shuffle username → Kick/Discord mapping
CREATE TABLE IF NOT EXISTS raffle_shuffle_links (
    id SERIAL PRIMARY KEY,
    shuffle_username TEXT NOT NULL UNIQUE,
    kick_name TEXT,  -- Nullable: self-verify via affiliate panel may have no Kick link yet
    discord_id BIGINT NOT NULL UNIQUE,  -- One Shuffle account per Discord user
    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    verified BOOLEAN DEFAULT FALSE,  -- Admin verification
    verified_by_discord_id BIGINT,
    verified_at TIMESTAMP,
    platform VARCHAR(50) DEFAULT 'shuffle',
    howl_uid VARCHAR(255)
);

-- Ticket transaction log (audit trail)
CREATE TABLE IF NOT EXISTS raffle_ticket_log (
    id SERIAL PRIMARY KEY,
    period_id INTEGER REFERENCES raffle_periods(id) ON DELETE CASCADE,
    discord_id BIGINT NOT NULL,
    kick_name TEXT NOT NULL,
    ticket_change INTEGER NOT NULL,  -- +10, +15, -50, etc.
    source VARCHAR(50) NOT NULL,  -- 'watchtime', 'gifted_sub', 'shuffle_wager', 'bonus', 'reset'
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Raffle draw history
CREATE TABLE IF NOT EXISTS raffle_draws (
    id SERIAL PRIMARY KEY,
    period_id INTEGER REFERENCES raffle_periods(id),  -- REMOVED UNIQUE - allows multiple winners per period
    discord_server_id BIGINT NOT NULL,  -- Multi-server support
    total_tickets INTEGER NOT NULL,
    total_participants INTEGER NOT NULL,
    winner_discord_id BIGINT NOT NULL,
    winner_kick_name TEXT NOT NULL,
    winner_shuffle_name TEXT,
    winning_ticket INTEGER NOT NULL,
    prize_description TEXT,
    drawn_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    drawn_by_discord_id BIGINT,  -- Admin who triggered draw
    server_seed TEXT,  -- Provably fair: server seed (64 hex chars)
    client_seed TEXT,  -- Provably fair: client seed (period:tickets:participants)
    nonce TEXT,  -- Provably fair: nonce (period_id)
    proof_hash TEXT  -- Provably fair: SHA-256 hash result
);

-- ============================================
-- INDICES FOR PERFORMANCE
-- ============================================

CREATE INDEX IF NOT EXISTS idx_raffle_tickets_period ON raffle_tickets(period_id);
CREATE INDEX IF NOT EXISTS idx_raffle_tickets_discord ON raffle_tickets(discord_id);
CREATE INDEX IF NOT EXISTS idx_raffle_tickets_total ON raffle_tickets(total_tickets DESC);
CREATE INDEX IF NOT EXISTS idx_raffle_ticket_log_period ON raffle_ticket_log(period_id);
CREATE INDEX IF NOT EXISTS idx_raffle_ticket_log_discord ON raffle_ticket_log(discord_id);
CREATE INDEX IF NOT EXISTS idx_raffle_gifted_subs_period ON raffle_gifted_subs(period_id);
CREATE INDEX IF NOT EXISTS idx_raffle_gifted_subs_discord ON raffle_gifted_subs(gifter_discord_id);
CREATE INDEX IF NOT EXISTS idx_raffle_shuffle_period ON raffle_shuffle_wagers(period_id);
CREATE INDEX IF NOT EXISTS idx_raffle_shuffle_discord ON raffle_shuffle_wagers(discord_id);
CREATE INDEX IF NOT EXISTS idx_raffle_periods_status ON raffle_periods(status);
CREATE INDEX IF NOT EXISTS idx_raffle_draws_server ON raffle_draws(discord_server_id);

-- ============================================
-- VIEWS FOR EASY QUERYING
-- ============================================

-- Drop views if they exist (for re-creation)
DROP VIEW IF EXISTS raffle_leaderboard;
DROP VIEW IF EXISTS raffle_current_stats;

-- Leaderboard view
CREATE VIEW raffle_leaderboard AS
SELECT
    rt.period_id,
    rt.discord_server_id,
    rt.discord_id,
    rt.kick_name,
    rt.total_tickets,
    rt.watchtime_tickets,
    rt.gifted_sub_tickets,
    rt.shuffle_wager_tickets,
    rt.bonus_tickets,
    RANK() OVER (PARTITION BY rt.period_id, rt.discord_server_id ORDER BY rt.total_tickets DESC) as rank
FROM raffle_tickets rt
WHERE rt.total_tickets > 0
ORDER BY rt.period_id DESC, rt.discord_server_id, rt.total_tickets DESC;

-- Current period stats
CREATE VIEW raffle_current_stats AS
SELECT
    rp.id as period_id,
    rp.discord_server_id,
    rp.start_date,
    rp.end_date,
    rp.status,
    COUNT(DISTINCT rt.discord_id) as total_participants,
    COALESCE(SUM(rt.total_tickets), 0) as total_tickets,
    COALESCE(SUM(rt.watchtime_tickets), 0) as watchtime_tickets,
    COALESCE(SUM(rt.gifted_sub_tickets), 0) as gifted_sub_tickets,
    COALESCE(SUM(rt.shuffle_wager_tickets), 0) as shuffle_wager_tickets,
    COALESCE(SUM(rt.bonus_tickets), 0) as bonus_tickets
FROM raffle_periods rp
LEFT JOIN raffle_tickets rt ON rt.period_id = rp.id AND rt.discord_server_id = rp.discord_server_id
WHERE rp.status = 'active'
GROUP BY rp.id, rp.discord_server_id;
"""


def setup_raffle_database(engine):
    """
    Create all raffle system tables, indices, and views

    Args:
        engine: SQLAlchemy engine instance

    Returns:
        bool: True if successful, False otherwise
    """
    try:
        logger.debug("Setting up raffle system database schema...")

        with engine.begin() as conn:
            # Split SQL into individual statements and execute one by one
            # This is required for SQLite which can only execute one statement at a time
            statements = []
            current_statement = []

            for line in RAFFLE_SCHEMA_SQL.split("\n"):
                # Skip comments and empty lines
                stripped = line.strip()
                if not stripped or stripped.startswith("--"):
                    continue

                current_statement.append(line)

                # Check if this line ends a statement
                if stripped.endswith(";"):
                    statement = "\n".join(current_statement)
                    statements.append(statement)
                    current_statement = []

            # Execute each statement
            for statement in statements:
                if statement.strip():
                    try:
                        conn.execute(text(statement))
                    except Exception as e:
                        # Log but continue (some statements might fail on re-run)
                        logger.debug(f"Statement warning: {e}")

        logger.debug("✅ Raffle database schema created successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to setup raffle database: {e}")
        return False


def verify_raffle_schema(engine):
    """
    Verify that all required tables exist

    Args:
        engine: SQLAlchemy engine instance

    Returns:
        dict: Status of each table (True/False)
    """
    required_tables = [
        "raffle_periods",
        "raffle_tickets",
        "raffle_watchtime_converted",
        "raffle_gifted_subs",
        "raffle_shuffle_wagers",
        "raffle_shuffle_links",
        "raffle_ticket_log",
        "raffle_draws",
    ]

    status = {}

    try:
        with engine.begin() as conn:
            for table in required_tables:
                # `table` iterates a hardcoded list (required_tables); no user input.
                result = conn.execute(
                    text(
                        f"""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables
                        WHERE table_name = '{table}'
                    );
                """
                    )
                )
                status[table] = result.scalar()

    except Exception as e:
        logger.error(f"Failed to verify schema: {e}")

    return status


def get_current_period(engine, discord_server_id=None):
    """
    Get the currently active raffle period

    Args:
        engine: SQLAlchemy engine instance
        discord_server_id: Discord server ID for multiserver filtering (optional)

    Returns:
        dict: Current period info or None
    """
    try:
        with engine.begin() as conn:
            if discord_server_id is not None:
                result = conn.execute(
                    text(
                        """
                    SELECT id, start_date, end_date, status, total_tickets,
                           COALESCE(period_number, id) AS period_number
                    FROM raffle_periods
                    WHERE status = 'active' AND discord_server_id = :server_id
                    ORDER BY start_date DESC
                    LIMIT 1
                """
                    ),
                    {"server_id": discord_server_id},
                )
            else:
                result = conn.execute(
                    text(
                        """
                    SELECT id, start_date, end_date, status, total_tickets,
                           COALESCE(period_number, id) AS period_number
                    FROM raffle_periods
                    WHERE status = 'active'
                    ORDER BY start_date DESC
                    LIMIT 1
                """
                    )
                )
            row = result.fetchone()

            if row:
                return {
                    "id": row[0],
                    "start_date": row[1],
                    "end_date": row[2],
                    "status": row[3],
                    "total_tickets": row[4],
                    "period_number": row[5],
                }
            return None

    except Exception as e:
        # IMPORTANT: re-raise on error. A query failure (e.g. a transient
        # connection/SSL drop) must NOT be collapsed into None — callers treat
        # None as "no active period exists" and respond by creating a new one,
        # which closes the real active period. Let the error propagate so
        # callers can skip the cycle instead of destroying the period.
        logger.error(f"Failed to get current period: {e}")
        raise


def create_new_period(engine, start_date, end_date, clear_tickets=True, discord_server_id=None):
    """
    Create a new raffle period and optionally reset all tickets

    Args:
        engine: SQLAlchemy engine instance
        start_date: datetime - Period start
        end_date: datetime - Period end
        clear_tickets: bool - Whether to delete all tickets (default True)
                              Set to False if winner needs to be drawn first
        discord_server_id: Discord server ID for multiserver support

    Returns:
        int: New period ID or None
    """
    try:
        with engine.begin() as conn:
            # Get the old active period if it exists (filter by server)
            if discord_server_id is not None:
                old_period = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods
                    WHERE status = 'active' AND discord_server_id = :server_id
                    ORDER BY id DESC
                    LIMIT 1
                """
                    ),
                    {"server_id": discord_server_id},
                ).fetchone()
            else:
                old_period = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods
                    WHERE status = 'active'
                    ORDER BY id DESC
                    LIMIT 1
                """
                    )
                ).fetchone()

            # Close any active periods
            if old_period:
                conn.execute(
                    text(
                        """
                    UPDATE raffle_periods
                    SET status = 'ended'
                    WHERE id = :period_id
                """
                    ),
                    {"period_id": old_period[0]},
                )
                logger.info(f"Closed old raffle period #{old_period[0]}")

            # Create new period. period_number is a per-server display counter
            # (the global PK `id` keeps growing across all servers); compute the
            # next value for THIS server in the same statement so it can't race.
            result = conn.execute(
                text(
                    """
                INSERT INTO raffle_periods (start_date, end_date, status, discord_server_id, period_number)
                VALUES (
                    :start, :end, 'active', :server_id,
                    (SELECT COALESCE(MAX(period_number), 0) + 1
                     FROM raffle_periods WHERE discord_server_id = :server_id)
                )
                RETURNING id
            """
                ),
                {"start": start_date, "end": end_date, "server_id": discord_server_id or 0},
            )
            period_id = result.scalar()

            # Commit-reveal: generate the server seed and its commitment NOW,
            # the moment the period opens. The commitment (SHA256 of the seed)
            # is published immediately so viewers can record it up front; the
            # seed itself stays hidden until the draw reveals it. This closes
            # the seed-grinding gap (the operator can't pick a seed after the
            # ticket pool / client_seed are known). See draw.py reveal logic.
            server_seed = secrets.token_hex(32)
            commitment = hashlib.sha256(server_seed.encode()).hexdigest()
            conn.execute(
                text(
                    """
                UPDATE raffle_periods
                SET server_seed = :seed, server_seed_commitment = :commitment
                WHERE id = :period_id
            """
                ),
                {"seed": server_seed, "commitment": commitment, "period_id": period_id},
            )
            logger.info(f"🔒 Committed server seed for period #{period_id} (commitment {commitment[:16]}…)")

            # NEVER delete tickets - preserve all historical data!
            # Tickets are tied to period_id so they don't interfere with new periods
            # The clear_tickets parameter is now ignored for safety
            if old_period:
                logger.info(
                    f"Period #{old_period[0]} closed. All {old_period[0]} tickets preserved for historical records."
                )
            logger.info(f"New period #{period_id} created. Tickets are isolated by period_id.")

            # Snapshot current watchtime as "already converted" for new period
            # This prevents awarding tickets for watchtime earned before this period
            if discord_server_id is not None:
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_watchtime_converted (period_id, kick_name, minutes_converted, tickets_awarded)
                    SELECT :period_id, username, minutes, 0
                    FROM watchtime
                    WHERE minutes > 0 AND discord_server_id = :server_id
                """
                    ),
                    {"period_id": period_id, "server_id": discord_server_id},
                )
            else:
                # Backward compatibility: snapshot all watchtime if no server_id
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_watchtime_converted (period_id, kick_name, minutes_converted, tickets_awarded)
                    SELECT :period_id, username, minutes, 0
                    FROM watchtime
                    WHERE minutes > 0
                """
                    ),
                    {"period_id": period_id},
                )
            logger.info(f"Snapshotted existing watchtime for new period (prevents double-awarding)")

            # Delete gifted subs only for this server
            if discord_server_id is not None:
                deleted_subs = conn.execute(
                    text(
                        """
                    DELETE FROM raffle_gifted_subs
                    WHERE period_id IN (
                        SELECT id FROM raffle_periods WHERE discord_server_id = :server_id
                    )
                """
                    ),
                    {"server_id": discord_server_id},
                ).rowcount
                logger.info(f"Deleted {deleted_subs} gifted sub records for server {discord_server_id}")

                # Clear shuffle wager tracking only for this server
                deleted_wagers = conn.execute(
                    text(
                        """
                    DELETE FROM raffle_shuffle_wagers
                    WHERE period_id IN (
                        SELECT id FROM raffle_periods WHERE discord_server_id = :server_id
                    )
                """
                    ),
                    {"server_id": discord_server_id},
                ).rowcount
                logger.info(f"Deleted {deleted_wagers} shuffle wager records for server {discord_server_id}")
            else:
                # Backward compatibility: delete all if no server_id
                deleted_subs = conn.execute(text("DELETE FROM raffle_gifted_subs")).rowcount
                logger.info(f"Deleted {deleted_subs} gifted sub records")

                deleted_wagers = conn.execute(text("DELETE FROM raffle_shuffle_wagers")).rowcount
                logger.info(f"Deleted {deleted_wagers} shuffle wager records")

        logger.info(f"✅ Created new raffle period #{period_id} and reset all tickets")
        return period_id

    except Exception as e:
        logger.error(f"Failed to create new period: {e}")
        return None


def migrate_one_active_period_per_server(engine):
    """
    Migration: Enforce at most one active raffle period per Discord server.

    Adds a partial unique index so a second status='active' row for the same
    discord_server_id is rejected at the DB level. This is the safety net behind
    create_new_period(): a spurious "create" (e.g. triggered by a transient DB
    error being misread as "no period") can no longer silently duplicate the
    active period — the INSERT fails loudly instead.

    Idempotent (IF NOT EXISTS). If the index cannot be created because duplicate
    active rows already exist, we log a clear error and return False WITHOUT
    deleting/merging anything — the duplicates must be cleaned up manually, after
    which this migration applies on the next startup.
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_period_per_server
                ON raffle_periods (discord_server_id)
                WHERE status = 'active'
            """
                )
            )
        logger.debug("✓ Ensured unique index uq_one_active_period_per_server (one active period per server)")
        return True

    except Exception as e:
        # Most likely cause: existing duplicate active periods violate the index.
        logger.error(
            f"❌ Could not create uq_one_active_period_per_server: {e}. "
            "This usually means there are already TWO OR MORE active periods for the "
            "same discord_server_id. Resolve the duplicates manually "
            "(end the bogus period via the dashboard / SQL), then restart so this "
            "index can apply. No rows were modified."
        )
        return False


def migrate_add_created_at_to_shuffle_wagers(engine):
    """
    Migration: Add created_at column to raffle_shuffle_wagers table
    This is needed to determine if wagers were made during the current period
    """
    try:
        with engine.begin() as conn:
            # Check if column already exists
            result = conn.execute(
                text(
                    """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'raffle_shuffle_wagers'
                AND column_name = 'created_at'
            """
                )
            )

            if result.fetchone():
                logger.debug("✓ Column 'created_at' already exists in raffle_shuffle_wagers")
                return True

            # Add the column
            conn.execute(
                text(
                    """
                ALTER TABLE raffle_shuffle_wagers
                ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            """
                )
            )

            # Set created_at for existing rows to last_checked (best approximation)
            conn.execute(
                text(
                    """
                UPDATE raffle_shuffle_wagers
                SET created_at = last_checked
                WHERE created_at IS NULL
            """
                )
            )

            logger.info("✅ Added 'created_at' column to raffle_shuffle_wagers table")
            return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


def migrate_add_platform_to_wager_tables(engine):
    """
    Migration: Add platform column to wager tracking tables
    This allows the bot to work with multiple gambling platforms (Shuffle, Stake, etc.)
    """
    try:
        from .config import WAGER_PLATFORM_NAME

        with engine.begin() as conn:
            # Add platform column to raffle_shuffle_wagers if it doesn't exist
            result = conn.execute(
                text(
                    """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'raffle_shuffle_wagers'
                AND column_name = 'platform'
            """
                )
            )

            if not result.fetchone():
                # Add the column
                conn.execute(
                    text(
                        """
                    ALTER TABLE raffle_shuffle_wagers
                    ADD COLUMN platform VARCHAR(50) DEFAULT 'shuffle'
                """
                    )
                )

                # Set platform for existing rows
                conn.execute(
                    text(
                        """
                    UPDATE raffle_shuffle_wagers
                    SET platform = 'shuffle'
                    WHERE platform IS NULL
                """
                    )
                )

                logger.info("✅ Added 'platform' column to raffle_shuffle_wagers table")
            else:
                logger.debug("✓ Column 'platform' already exists in raffle_shuffle_wagers")

            # Add platform column to raffle_shuffle_links if it doesn't exist
            result2 = conn.execute(
                text(
                    """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'raffle_shuffle_links'
                AND column_name = 'platform'
            """
                )
            )

            if not result2.fetchone():
                # Add the column
                conn.execute(
                    text(
                        """
                    ALTER TABLE raffle_shuffle_links
                    ADD COLUMN platform VARCHAR(50) DEFAULT 'shuffle'
                """
                    )
                )

                # Set platform for existing rows
                conn.execute(
                    text(
                        """
                    UPDATE raffle_shuffle_links
                    SET platform = 'shuffle'
                    WHERE platform IS NULL
                """
                    )
                )

                logger.info("✅ Added 'platform' column to raffle_shuffle_links table")
            else:
                logger.debug("✓ Column 'platform' already exists in raffle_shuffle_links")

            return True

    except Exception as e:
        logger.error(f"Migration failed: {e}")
        return False


def migrate_make_shuffle_links_kick_name_nullable(engine):
    """
    Migration: Drop the NOT NULL constraint on raffle_shuffle_links.kick_name.

    The Shuffle affiliate self-verify panel can verify a user before they've
    linked a Kick account, so kick_name may be NULL. All existing readers of
    this column already tolerate NULL (see shuffle_tracker / draw / startup sync).
    """
    try:
        with engine.begin() as conn:
            is_nullable = conn.execute(
                text(
                    """
                SELECT is_nullable
                FROM information_schema.columns
                WHERE table_name = 'raffle_shuffle_links'
                AND column_name = 'kick_name'
            """
                )
            ).scalar()

            if is_nullable == "NO":
                conn.execute(text("ALTER TABLE raffle_shuffle_links ALTER COLUMN kick_name DROP NOT NULL"))
                logger.info("✅ Dropped NOT NULL on raffle_shuffle_links.kick_name")
            else:
                logger.debug("✓ raffle_shuffle_links.kick_name already nullable")

        return True

    except Exception as e:
        logger.error(f"Migration failed (kick_name nullable): {e}")
        return False


def migrate_add_panel_type_to_link_panels(engine):
    """
    Migration: Add a panel_type column to link_panels so the Shuffle verify
    panel can reuse this table alongside the Kick link panel without colliding.
    Existing Kick panels default to 'kick_link'; Shuffle panels use 'shuffle_verify'.
    """
    try:
        with engine.begin() as conn:
            conn.execute(
                text("ALTER TABLE link_panels ADD COLUMN IF NOT EXISTS panel_type text NOT NULL DEFAULT 'kick_link'")
            )
            logger.debug("✓ Ensured link_panels.panel_type column exists")

        return True

    except Exception as e:
        logger.error(f"Migration failed (link_panels.panel_type): {e}")
        return False


if __name__ == "__main__":
    """
    Run this script directly to setup the database schema
    """
    import os

    from dotenv import load_dotenv
    from sqlalchemy import create_engine

    # Setup logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    # Load environment variables
    load_dotenv()

    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        logger.error("❌ DATABASE_URL not found in environment variables")
        exit(1)

    # Create engine
    engine = create_engine(DATABASE_URL)

    # Setup schema
    success = setup_raffle_database(engine)

    if success:
        # Verify schema
        logger.info("\nVerifying schema...")
        status = verify_raffle_schema(engine)

        all_ok = all(status.values())
        if all_ok:
            logger.info("✅ All tables created successfully:")
            for table, exists in status.items():
                logger.info(f"  ✓ {table}")
        else:
            logger.warning("⚠️ Some tables missing:")
            for table, exists in status.items():
                symbol = "✓" if exists else "✗"
                logger.info(f"  {symbol} {table}")
    else:
        logger.error("❌ Schema setup failed")
        exit(1)
