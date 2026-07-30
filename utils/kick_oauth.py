"""
Utility functions for fetching Kick OAuth tokens from database.

`kick_oauth_tokens` is the ONE canonical home for Kick access/refresh tokens.
The legacy duplicates in `bot_settings` ('kick_oauth_token' /
'kick_refresh_token') are no longer read anywhere — every lookup is scoped by
`discord_server_id`, which is part of that table's primary key.
"""

import logging

from sqlalchemy import text

from utils.secret_settings import SecretConfigError, SecretDecryptError, decrypt_secret

logger = logging.getLogger(__name__)


def get_kick_token_for_server(engine, discord_server_id):
    """
    Fetch the canonical Kick OAuth token for a Discord server.

    Scoped by discord_server_id — NOT resolved via kick_channel -> kick_username.
    The old username join was both fragile (a renamed Kick channel silently
    orphaned the token) and unscoped (any server whose kick_channel matched a
    username got that token, regardless of which server authorized it).

    Args:
        engine: SQLAlchemy engine
        discord_server_id: Discord server/guild ID (as int or None)

    Returns:
        dict with 'access_token', 'refresh_token', etc., or None if not found

    Raises:
        SecretConfigError / SecretDecryptError when a stored credential exists but
        cannot be decrypted. Deliberately NOT swallowed: "cannot read it" must
        never be reported to callers as "not configured".
    """
    if not discord_server_id:
        logger.info("[Kick OAuth] No discord_server_id provided")
        return None

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT access_token, refresh_token, expires_at, kick_username, user_id
                    FROM kick_oauth_tokens
                    WHERE discord_server_id = :server_id
                    ORDER BY updated_at DESC NULLS LAST
                    LIMIT 1
                """
                ),
                {"server_id": int(discord_server_id)},
            ).fetchone()

            if not row:
                logger.info(f"[Kick OAuth] No canonical token row for server {discord_server_id}")
                return None

            return {
                "access_token": decrypt_secret(row[0], key_name="kick_oauth_tokens.access_token", row_id=row[4]),
                "refresh_token": decrypt_secret(row[1], key_name="kick_oauth_tokens.refresh_token", row_id=row[4]),
                "expires_at": row[2],
                "kick_username": row[3],
            }

    except (SecretConfigError, SecretDecryptError):
        raise
    except Exception as e:
        logger.info(f"[Kick OAuth] Error fetching token: {e}")
        return None


def get_chatroom_id_for_server(engine, discord_server_id):
    """
    Fetch stored chatroom ID from bot_settings table

    Args:
        engine: SQLAlchemy engine
        discord_server_id: Discord server/guild ID (as int or None)

    Returns:
        str: Chatroom ID or None
    """
    if not discord_server_id:
        return None

    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    """
                    SELECT kick_chatroom_id
                    FROM bot_settings
                    WHERE discord_server_id = :server_id
                    LIMIT 1
                """
                ),
                {"server_id": discord_server_id},
            )
            row = result.fetchone()

            if row and row[0]:
                return str(row[0])
            return None

    except Exception as e:
        logger.info(f"[Kick OAuth] Error fetching chatroom ID: {e}")
        return None
