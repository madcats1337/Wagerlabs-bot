"""Encrypt credentials at rest — idempotent, value-free, transactional per table.

MIRRORED FILE — an identical copy lives at
`Kick-dicord-bot/migrations/encrypt_credentials.py`. Either Railway service may
boot first, so both repos ship the migration and whichever runs first does the
work; the second finds nothing left to do.

Targets (all currently plaintext):
    bot_settings.value                        WHERE key = 'howl_api_key'
    kick_oauth_tokens.access_token
    kick_oauth_tokens.refresh_token
    kick_webhook_subscriptions.webhook_secret

Guarantees:
  - IDEMPOTENT: rows already carrying the `wlsec:` envelope are skipped, so
    re-running (or both services running it) is safe.
  - Blank/NULL values stay blank — "not configured" must not become a ciphertext
    that decrypts to "".
  - Each table runs in its OWN transaction and rolls back entirely if any row in
    it fails. A partial table is the one state we cannot tolerate: half-encrypted
    rows with no key would be unreadable.
  - Logs only row COUNTS and table names. Never a value, a ciphertext, a prefix of
    either, or a length.

Requires SECRET_ENCRYPTION_WRITE_ENABLED=true and SETTINGS_ENCRYPTION_KEY. Without
them this is a no-op that logs why, so deploying the code is safe before the key
is configured.

`delete_legacy_credential_rows()` is deliberately NOT called from any startup path.
Deleting the legacy rows is irreversible, and the plan requires canonical coverage
plus runtime smoke tests (Kick send/refresh, webhook delivery, clip auth) to pass
first. Run it explicitly once those are green:

    python -c "from migrations.encrypt_credentials import delete_legacy_credential_rows; ..."
"""

import logging
import os
import sys

# This module is loaded both as a normal import and via importlib from a service
# entrypoint, so make sure the repo root (which holds `utils/`) is importable
# either way.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logger = logging.getLogger(__name__)

# (table, value_column, key_columns, extra_where)
#
# key_columns are the table's REAL primary key. `ctid` would be wrong here: it is a
# physical row pointer that changes when a row is updated, so a row rewritten
# earlier in the same pass could be re-matched or missed.
_TARGETS = [
    ("bot_settings", "value", ("key", "discord_server_id"), "AND key = 'howl_api_key'"),
    ("kick_oauth_tokens", "access_token", ("user_id", "discord_server_id"), ""),
    ("kick_oauth_tokens", "refresh_token", ("user_id", "discord_server_id"), ""),
    ("kick_webhook_subscriptions", "webhook_secret", ("id",), ""),
]


def _encrypt_table(conn, table, column, key_columns, extra_where):
    """Encrypt one column of one table inside a single transaction.

    Returns (encrypted, skipped). Raises on any row failure after rolling back.
    """
    from utils.secret_settings import SECRET_PREFIX, encrypt_secret

    cur = conn.cursor()
    encrypted = 0
    skipped = 0
    key_select = ", ".join(key_columns)
    key_predicate = " AND ".join(f"{col} = %s" for col in key_columns)
    try:
        # Select only rows that need work: non-blank and not already enveloped.
        cur.execute(
            f"""
            SELECT {key_select}, {column}
            FROM {table}
            WHERE {column} IS NOT NULL
              AND {column} <> ''
              AND {column} NOT LIKE %s
              {extra_where}
            """,
            (SECRET_PREFIX + "%",),
        )
        rows = cur.fetchall()

        n_keys = len(key_columns)
        for row in rows:
            key_values = row[:n_keys]
            plaintext = row[n_keys]
            if not plaintext or not str(plaintext).strip():
                skipped += 1
                continue
            token = encrypt_secret(str(plaintext))
            if not token.startswith(SECRET_PREFIX):
                # encrypt_secret passed the value through, which means writes are
                # disabled. Abort rather than "successfully" writing plaintext back.
                raise RuntimeError(
                    "encryption is not enabled (SECRET_ENCRYPTION_WRITE_ENABLED / "
                    "SETTINGS_ENCRYPTION_KEY) — aborting migration"
                )
            cur.execute(
                f"UPDATE {table} SET {column} = %s WHERE {key_predicate}",
                (token, *key_values),
            )
            encrypted += 1

        conn.commit()
        logger.info(f"[encrypt] {table}.{column}: encrypted={encrypted} skipped={skipped}")
        return encrypted, skipped
    except Exception:
        conn.rollback()
        # Deliberately no value/ciphertext in the message.
        logger.exception(f"[encrypt] {table}.{column} FAILED — transaction rolled back")
        raise
    finally:
        cur.close()


def run(get_connection):
    """Encrypt every target column. `get_connection` returns a fresh DB connection.

    Returns a summary dict of counts (safe to log).
    """
    from utils.secret_settings import encryption_available, write_enabled

    if not write_enabled():
        logger.info("[encrypt] SECRET_ENCRYPTION_WRITE_ENABLED is not true — skipping (no-op)")
        return {"skipped_reason": "writes_disabled"}
    if not encryption_available():
        logger.error("[encrypt] SETTINGS_ENCRYPTION_KEY is not configured — refusing to run")
        return {"skipped_reason": "no_key"}

    summary = {}
    conn = get_connection()
    try:
        for table, column, key_columns, extra_where in _TARGETS:
            try:
                enc, skip = _encrypt_table(conn, table, column, key_columns, extra_where)
                summary[f"{table}.{column}"] = {"encrypted": enc, "skipped": skip}
            except Exception as e:
                # One table failing must not prevent the others from being migrated;
                # each is independently transactional.
                summary[f"{table}.{column}"] = {"error": type(e).__name__}
    finally:
        conn.close()

    logger.info(f"[encrypt] migration summary: {summary}")
    return summary


def count_remaining_plaintext(conn):
    """Value-free verification: how many targeted rows are still plaintext.

    Used by the post-migration check ("confirm no targeted non-empty plaintext rows
    remain"). Returns {target: count}.
    """
    from utils.secret_settings import SECRET_PREFIX

    out = {}
    cur = conn.cursor()
    try:
        for table, column, _key_columns, extra_where in _TARGETS:
            cur.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE {column} IS NOT NULL
                  AND {column} <> ''
                  AND {column} NOT LIKE %s
                  {extra_where}
                """,
                (SECRET_PREFIX + "%",),
            )
            out[f"{table}.{column}"] = cur.fetchone()[0]
    finally:
        cur.close()
    return out


# Legacy credential rows in bot_settings. Nothing reads these any more:
#   - kick_oauth_token / kick_access_token / kick_refresh_token -> kick_oauth_tokens
#   - bot_api_key                                               -> CLIPS_API_KEY env
#   - kick_webhook_secret -> kick_webhook_subscriptions.webhook_secret
# `howl_api_key` is deliberately NOT deleted: bot_settings is still its home (now
# encrypted, write-only) until it gets a dedicated table.
_LEGACY_CREDENTIAL_KEYS = (
    "bot_api_key",
    "kick_oauth_token",
    "kick_access_token",
    "kick_refresh_token",
    "kick_webhook_secret",
)


def delete_legacy_credential_rows(conn, require_canonical_coverage=True):
    """Delete the legacy bot_settings credential rows. Idempotent.

    By default REFUSES to run while any server still has a legacy Kick token but no
    canonical `kick_oauth_tokens` row — deleting those would take that workspace's
    Kick integration offline. (Production had exactly one such server: a standalone
    workspace whose canonical row is created by the backfill in the dashboard's
    run_migrations.)

    Returns a value-free summary.
    """
    cur = conn.cursor()
    try:
        if require_canonical_coverage:
            cur.execute(
                """
                SELECT bs.discord_server_id
                FROM (
                    SELECT DISTINCT discord_server_id FROM bot_settings
                    WHERE key IN ('kick_oauth_token', 'kick_access_token', 'kick_refresh_token')
                      AND value IS NOT NULL AND value <> ''
                ) bs
                LEFT JOIN (
                    SELECT DISTINCT discord_server_id FROM kick_oauth_tokens
                    WHERE access_token IS NOT NULL AND access_token <> ''
                ) kot ON kot.discord_server_id = bs.discord_server_id
                WHERE kot.discord_server_id IS NULL
                """
            )
            uncovered = [r[0] for r in cur.fetchall()]
            if uncovered:
                logger.error(
                    "[cleanup] refusing to delete legacy Kick tokens — these servers have no "
                    f"canonical kick_oauth_tokens row: {sorted(uncovered)}"
                )
                return {"deleted": 0, "blocked_servers": sorted(uncovered)}

        cur.execute(
            "DELETE FROM bot_settings WHERE key = ANY(%s)",
            (list(_LEGACY_CREDENTIAL_KEYS),),
        )
        deleted = cur.rowcount or 0
        conn.commit()
        if deleted:
            logger.info(f"[cleanup] deleted {deleted} legacy credential row(s) from bot_settings")
        return {"deleted": deleted, "blocked_servers": []}
    except Exception:
        conn.rollback()
        logger.exception("[cleanup] legacy credential deletion FAILED — rolled back")
        raise
    finally:
        cur.close()


def has_encrypted_rows(conn):
    """True when ANY targeted row is already encrypted.

    Drives the startup readiness check: if encrypted rows exist but this service
    has no key, it must fail loudly rather than serve broken integrations.
    """
    from utils.secret_settings import SECRET_PREFIX

    cur = conn.cursor()
    try:
        for table, column, _key_columns, extra_where in _TARGETS:
            cur.execute(
                f"""
                SELECT 1 FROM {table}
                WHERE {column} LIKE %s {extra_where}
                LIMIT 1
                """,
                (SECRET_PREFIX + "%",),
            )
            if cur.fetchone():
                return True
        return False
    finally:
        cur.close()
