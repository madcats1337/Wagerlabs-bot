"""
Combined server that runs both Flask OAuth server and Discord bot
Runs Flask in main process, bot in background subprocess
"""

import logging
import os
import subprocess
import sys
import threading
import time

# Configure logging early so this supervisor's own logs are formatted/visible
# (it's a separate process from the bot.py and gunicorn subprocesses it launches).
from utils.logging_config import setup_logging

setup_logging("kick_combined", log_level=os.getenv("LOG_LEVEL", "INFO"), source_tag="BOT")
logger = logging.getLogger(__name__)


def run_database_migration():
    """Run database migration before starting services"""
    logger.info("📋 Running database migration...")
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(os.getenv("DATABASE_URL"))

        with engine.begin() as conn:
            # First check if table exists
            table_exists = conn.execute(
                text(
                    """
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = 'links'
                )
            """
                )
            ).scalar()

            if not table_exists:
                logger.info("   ℹ️ links table doesn't exist yet, will be created by bot.py")
                return

            # Check current primary key structure
            pk_result = conn.execute(
                text(
                    """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'links'::regclass AND i.indisprimary
                ORDER BY a.attnum
            """
                )
            )
            pk_columns = [row[0] for row in pk_result]
            logger.info(f"   📊 Current primary key columns: {pk_columns}")

            # If PK is only discord_id (old schema), we need to recreate it
            if pk_columns == ["discord_id"] or "discord_server_id" not in pk_columns:
                logger.info("   🔄 Migrating links table to composite primary key...")

                # First, handle any NULL discord_server_id values
                # Set them to 0 as a default server
                null_count = conn.execute(
                    text(
                        """
                    SELECT COUNT(*) FROM links WHERE discord_server_id IS NULL
                """
                    )
                ).scalar()
                if null_count > 0:
                    logger.info(f"   📝 Setting {null_count} NULL discord_server_id values to 0...")
                    conn.execute(
                        text(
                            """
                        UPDATE links SET discord_server_id = 0 WHERE discord_server_id IS NULL
                    """
                        )
                    )

                # Remove duplicates - keep most recent link per discord_id + server combo
                # For rows with same discord_id, keep the one with most recent linked_at
                conn.execute(
                    text(
                        """
                    DELETE FROM links a
                    WHERE EXISTS (
                        SELECT 1 FROM links b
                        WHERE b.discord_id = a.discord_id
                        AND b.discord_server_id = a.discord_server_id
                        AND b.linked_at > a.linked_at
                    )
                """
                    )
                )

                # Also remove strict duplicates (same discord_id + server_id)
                conn.execute(
                    text(
                        """
                    DELETE FROM links a
                    WHERE a.ctid <> (
                        SELECT MIN(b.ctid)
                        FROM links b
                        WHERE b.discord_id = a.discord_id
                        AND b.discord_server_id = a.discord_server_id
                    )
                """
                    )
                )

                # Drop old primary key
                conn.execute(
                    text(
                        """
                    ALTER TABLE links DROP CONSTRAINT IF EXISTS links_pkey
                """
                    )
                )

                # Make discord_server_id NOT NULL
                conn.execute(
                    text(
                        """
                    ALTER TABLE links ALTER COLUMN discord_server_id SET NOT NULL
                """
                    )
                )

                # Add new composite primary key
                conn.execute(
                    text(
                        """
                    ALTER TABLE links ADD PRIMARY KEY (discord_id, discord_server_id)
                """
                    )
                )

                logger.info("   ✅ Primary key migrated to composite (discord_id, discord_server_id)!")

            # NOTE: links UNIQUE constraints (on kick_name / discord_id) are owned
            # entirely by raffle_system/migrations/add_platform_to_links.py, which
            # platform-scopes them to UNIQUE (..., platform) so a user can link both
            # a Kick and a Twitch account in one server. Re-adding the old unscoped
            # UNIQUE (kick_name, discord_server_id) here fought that migration and
            # failed on the now-valid dual-platform duplicate rows, so it was removed.

    except Exception as e:
        logger.warning(f"   ⚠️ Migration warning: {e}")
        logger.info("   Continuing startup anyway...")

    # Encrypt credentials at rest. Mirrored from the dashboard repo because either
    # Railway service may boot first; the migration is idempotent so whichever runs
    # first does the work and the other finds nothing to do. A NO-OP until
    # SETTINGS_ENCRYPTION_KEY + SECRET_ENCRYPTION_WRITE_ENABLED are set.
    try:
        import importlib.util as _ilu

        import psycopg2

        _mig_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations", "encrypt_credentials.py")
        _spec = _ilu.spec_from_file_location("encrypt_credentials", _mig_path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _db_url = os.getenv("DATABASE_URL")
        if _db_url:
            _mod.run(lambda: psycopg2.connect(_db_url))

            # Fail-closed readiness check: encrypted rows with no key on THIS service
            # means every credential read raises later. Say so at boot.
            from utils.secret_settings import encryption_available

            if not encryption_available():
                _c = psycopg2.connect(_db_url)
                try:
                    if _mod.has_encrypted_rows(_c):
                        logger.error(
                            "   ❌ encrypted credential rows exist but SETTINGS_ENCRYPTION_KEY is not "
                            "configured on the bot service — Kick/Howl/webhook reads WILL fail"
                        )
                finally:
                    _c.close()
    except Exception as e:
        logger.warning(f"   ⚠️ credential encryption migration: {e}")


def run_discord_bot():
    """Start Discord bot as a subprocess, return the process handle"""
    logger.info("🤖 Starting Discord bot subprocess...")
    try:
        # Run bot.py with unbuffered output, piped through reader threads
        process = subprocess.Popen(
            [sys.executable, "-u", "bot.py"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # Line buffered
        )
        logger.info(f"✅ Bot subprocess started (PID: {process.pid})")
        return process
    except Exception as e:
        logger.error(f"❌ Discord bot error: {e}")
        import traceback

        traceback.print_exc()
        return None


def stream_subprocess_output(process, prefix="[BOT]"):
    """Relay subprocess output to our stdout line by line.

    The bot subprocess already formats its own lines via setup_logging
    (e.g. "[time] INFO [BOT] [server] msg"), so we pass them through VERBATIM.
    Re-logging them through `logger` here would double-wrap each line with a
    second "[BOT] ..." prefix.
    """
    try:
        for line in iter(process.stdout.readline, ""):
            if line:
                sys.stdout.write(line if line.endswith("\n") else line + "\n")
                sys.stdout.flush()
    except Exception as e:
        logger.warning(f"{prefix} output stream ended: {e}")
    finally:
        if process.stdout:
            process.stdout.close()


if __name__ == "__main__":
    logger.info("🚀 Starting combined OAuth + Discord Bot server...")
    logger.info(f"Python: {sys.version}")
    logger.info(f"Working directory: {os.getcwd()}")

    # Run database migration first
    run_database_migration()

    # Start Discord bot subprocess
    bot_process = run_discord_bot()

    # Stream bot output in a background thread
    if bot_process:
        bot_output_thread = threading.Thread(
            target=stream_subprocess_output,
            args=(bot_process, "[BOT]"),
            daemon=True,
        )
        bot_output_thread.start()

    # Give bot a moment to start
    logger.info("⏳ Waiting for bot to initialize...")
    time.sleep(3)

    # Now run Flask OAuth server in main process using Gunicorn
    logger.info("📡 Starting OAuth web server with Gunicorn...")
    port = int(os.getenv("PORT", 8000))
    logger.info(f"🌐 Port: {port}")
    logger.info(f"🌐 OAuth Base URL: {os.getenv('OAUTH_BASE_URL', 'Not set')}")

    # Use Gunicorn for production
    # IMPORTANT: Use subprocess.Popen (NOT os.execvp) so the bot process stays alive.
    # os.execvp replaces the entire process image, killing all subprocesses.
    try:
        gunicorn_process = subprocess.Popen(
            [
                "gunicorn",
                "--bind",
                f"0.0.0.0:{port}",
                # 1 worker: this is a low-traffic OAuth/webhook callback server, and each
                # worker imports Flask/authlib/cryptography + its own DB engine, so a
                # second worker roughly doubles this process's memory for no throughput
                # need. Override with GUNICORN_WORKERS if real load ever appears.
                "--workers",
                os.getenv("GUNICORN_WORKERS", "1"),
                # Recycle workers periodically to reclaim any slow memory growth.
                "--max-requests",
                "1000",
                "--max-requests-jitter",
                "100",
                "--timeout",
                "120",
                # Config re-asserts our single root logging handler per worker so
                # app logs aren't emitted twice (raw + formatted).
                "--config",
                "config/oauth_gunicorn_config.py",
                # No --access-logfile: per-request access logs are noise; the config
                # also sets gunicorn's own loglevel to warning.
                "--error-logfile",
                "-",
                "core.oauth_server:app",
            ],
        )
        logger.info(f"✅ Gunicorn started (PID: {gunicorn_process.pid})")

        # Monitor both processes - if either dies, restart or exit
        while True:
            # Check bot process
            if bot_process and bot_process.poll() is not None:
                logger.error(f"❌ Discord bot exited with code {bot_process.returncode}")
                # Restart bot
                logger.info("🔄 Restarting Discord bot...")
                bot_process = run_discord_bot()
                if bot_process:
                    bot_output_thread = threading.Thread(
                        target=stream_subprocess_output,
                        args=(bot_process, "[BOT]"),
                        daemon=True,
                    )
                    bot_output_thread.start()

            # Check gunicorn process
            if gunicorn_process.poll() is not None:
                logger.error(f"❌ Gunicorn exited with code {gunicorn_process.returncode}")
                # Kill bot and exit so Railway can restart everything
                if bot_process:
                    bot_process.terminate()
                sys.exit(gunicorn_process.returncode)

            time.sleep(5)

    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
        if bot_process:
            bot_process.terminate()
        gunicorn_process.terminate()
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Failed to start Gunicorn: {e}")
        import traceback

        traceback.print_exc()
        if bot_process:
            bot_process.terminate()
        sys.exit(1)
