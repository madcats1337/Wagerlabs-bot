"""
Guess the Balance - Game where users guess the final balance amount
Admins open/close sessions and set the result to determine winners
"""

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

import redis as _redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def gtb_rank_marker(rank: int) -> str:
    """Podium medal for ranks 1-3, a numbered marker (#4, #5, …) beyond that.

    GTB winner count is configurable (1-10), so announcements/embeds can list
    more than the three medal positions. Keeps the celebratory podium for the
    top three and stays readable for the rest.
    """
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"#{rank}")


# Lazy-init Redis client for publishing guess events to dashboard
_guess_redis_client = None


def _get_guess_redis():
    """Get or create a Redis client for publishing guess events."""
    global _guess_redis_client
    if _guess_redis_client is not None:
        return _guess_redis_client
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    if "://" not in url:
        url = f"redis://{url}"
    try:
        _guess_redis_client = _redis.from_url(url, decode_responses=True, socket_connect_timeout=3)
        _guess_redis_client.ping()
        return _guess_redis_client
    except Exception as e:
        logger.warning(f"GTB Redis publish unavailable: {e}")
        return None


class GuessTheBalanceManager:
    """Manages Guess the Balance game sessions and guesses"""

    def __init__(self, engine: Engine, server_id: int):
        self.engine = engine
        self.server_id = server_id
        logger.debug("GuessTheBalanceManager initialized")

    def get_active_session(self) -> Optional[Dict]:
        """Get the currently active (open) session if one exists"""
        if not self.engine:
            return None

        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT id, opened_by, opened_at, status
                    FROM gtb_sessions
                    WHERE status = 'open' AND discord_server_id = :server_id
                    ORDER BY opened_at DESC
                    LIMIT 1
                """
                    ),
                    {"server_id": self.server_id},
                ).fetchone()

                if result:
                    return {"id": result[0], "opened_by": result[1], "opened_at": result[2], "status": result[3]}
                return None
        except Exception as e:
            logger.error(f"Failed to get active session: {e}")
            return None

    def open_session(self, opened_by: str) -> Tuple[bool, str]:
        """
        Open a new GTB session

        Returns:
            (success, message)
        """
        if not self.engine:
            return False, "Database not available"

        # Check if there's already an active session
        active = self.get_active_session()
        if active:
            return False, f"A session is already open (opened by {active['opened_by']})"

        try:
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    INSERT INTO gtb_sessions (opened_by, status, discord_server_id)
                    VALUES (:opened_by, 'open', :server_id)
                    RETURNING id
                """
                    ),
                    {"opened_by": opened_by, "server_id": self.server_id},
                )

                session_id = result.fetchone()[0]
                logger.info(f"GTB session #{session_id} opened by {opened_by}")
                return True, f"Session #{session_id} opened! Users can now guess with !gtb <amount>"
        except Exception as e:
            logger.error(f"Failed to open session: {e}")
            return False, f"Failed to open session: {str(e)}"

    def close_session(self) -> Tuple[bool, str, Optional[int]]:
        """
        Close the active session (stop accepting guesses)

        Returns:
            (success, message, session_id)
        """
        if not self.engine:
            return False, "Database not available", None

        active = self.get_active_session()
        if not active:
            return False, "No active session to close", None

        session_id = active["id"]

        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        """
                    UPDATE gtb_sessions
                    SET status = 'closed', closed_at = CURRENT_TIMESTAMP
                    WHERE id = :session_id
                """
                    ),
                    {"session_id": session_id},
                )

                # Get guess count
                guess_count = conn.execute(
                    text(
                        """
                    SELECT COUNT(*) FROM gtb_guesses WHERE session_id = :session_id
                """
                    ),
                    {"session_id": session_id},
                ).fetchone()[0]

                logger.info(f"GTB session #{session_id} closed with {guess_count} guesses")
                return (
                    True,
                    f"Session #{session_id} closed with {guess_count} guesses. Use !gtbresult <amount> to set the result.",
                    session_id,
                )
        except Exception as e:
            logger.error(f"Failed to close session: {e}")
            return False, f"Failed to close session: {str(e)}", None

    def add_guess(self, kick_username: str, guess_amount: float, display_name: str = None) -> Tuple[bool, str]:
        """
        Add a user's guess to the active session.

        kick_username = canonical credit/dedup key (ON CONFLICT keeps one guess per
        Discord identity per session). display_name = native platform name shown in
        the winner panel/announcement (falls back to kick_username).

        Returns:
            (success, message)
        """
        if not self.engine:
            return False, "Database not available"

        # Check if there's an active session
        active = self.get_active_session()
        if not active:
            return False, "No active GTB session. Wait for an admin to start one!"

        session_id = active["id"]

        # Validate amount
        if guess_amount <= 0:
            return False, "Guess amount must be greater than 0"

        if guess_amount > 999999999999.99:  # 12 digits total, 2 decimals
            return False, "Guess amount is too large"

        try:
            with self.engine.begin() as conn:
                # Try to insert (will fail if user already guessed)
                conn.execute(
                    text(
                        """
                    INSERT INTO gtb_guesses (session_id, kick_username, guess_amount, discord_server_id, display_name)
                    VALUES (:session_id, :username, :amount, :server_id, :display_name)
                    ON CONFLICT (session_id, kick_username)
                    DO UPDATE SET guess_amount = :amount, guessed_at = CURRENT_TIMESTAMP,
                                  display_name = EXCLUDED.display_name
                """
                    ),
                    {
                        "session_id": session_id,
                        "username": kick_username,
                        "amount": guess_amount,
                        "server_id": self.server_id,
                        "display_name": display_name or kick_username,
                    },
                )

                logger.info(f"GTB: {kick_username} guessed ${guess_amount:,.2f} in session #{session_id}")

                # Publish event to Redis so dashboard SSE can push it in real-time
                try:
                    rc = _get_guess_redis()
                    if rc:
                        rc.publish(
                            "gtb:guess:events",
                            json.dumps(
                                {
                                    "action": "new_guess",
                                    "data": {
                                        "session_id": session_id,
                                        "kick_username": kick_username,
                                        "guess_amount": float(guess_amount),
                                        "discord_server_id": self.server_id,
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                    },
                                }
                            ),
                        )
                except Exception as pub_err:
                    logger.debug(f"Failed to publish GTB guess event: {pub_err}")

                return True, f"Guess recorded: ${guess_amount:,.2f}"
        except Exception as e:
            logger.error(f"Failed to add guess: {e}")
            return False, f"Failed to record guess: {str(e)}"

    def set_result(self, result_amount: float) -> Tuple[bool, str, Optional[List[Dict]]]:
        """
        Set the result for the most recent closed session and calculate winners

        Returns:
            (success, message, winners_list)
        """
        if not self.engine:
            return False, "Database not available", None

        # Validate amount
        if result_amount <= 0:
            return False, "Result amount must be greater than 0", None

        try:
            with self.engine.begin() as conn:
                # Get the most recent closed session
                session = conn.execute(
                    text(
                        """
                    SELECT id, winner_count FROM gtb_sessions
                    WHERE status = 'closed' AND discord_server_id = :server_id
                    ORDER BY closed_at DESC
                    LIMIT 1
                """
                    ),
                    {"server_id": self.server_id},
                ).fetchone()

                if not session:
                    return False, "No closed session found. Close a session first with !gtbclose", None

                session_id = session[0]
                # Number of winners to score — chosen at open time, defaults to
                # 3 for legacy sessions. Clamp to the supported 1..10 range.
                winner_count = session[1] if session[1] else 3
                winner_count = max(1, min(10, int(winner_count)))

                # Update session with result
                conn.execute(
                    text(
                        """
                    UPDATE gtb_sessions
                    SET result_amount = :amount, status = 'completed'
                    WHERE id = :session_id
                """
                    ),
                    {"amount": result_amount, "session_id": session_id},
                )

                # Get the N closest guesses (N = the session's winner_count).
                # Select the native display name (fallback to kick_username).
                guesses = conn.execute(
                    text(
                        """
                    SELECT kick_username, COALESCE(display_name, kick_username) AS display_name,
                           guess_amount,
                           ABS(guess_amount - :result) as difference
                    FROM gtb_guesses
                    WHERE session_id = :session_id
                    ORDER BY difference ASC, guessed_at ASC
                    LIMIT :winner_count
                """
                    ),
                    {"result": result_amount, "session_id": session_id, "winner_count": winner_count},
                ).fetchall()

                if not guesses:
                    return False, f"Session #{session_id} has no guesses!", None

                # Clear any existing winners for this session (in case of re-calculation)
                conn.execute(
                    text(
                        """
                    DELETE FROM gtb_winners WHERE session_id = :session_id
                """
                    ),
                    {"session_id": session_id},
                )

                # Insert winners
                winners = []
                for rank, (username, display_name, guess, diff) in enumerate(guesses, 1):
                    conn.execute(
                        text(
                            """
                        INSERT INTO gtb_winners
                        (session_id, kick_username, rank, guess_amount, result_amount, difference, display_name)
                        VALUES (:session_id, :username, :rank, :guess, :result, :diff, :display_name)
                    """
                        ),
                        {
                            "session_id": session_id,
                            "username": username,
                            "rank": rank,
                            "guess": guess,
                            "result": result_amount,
                            "diff": diff,
                            "display_name": display_name,
                        },
                    )

                    # `username` in the returned dict is what the panel SHOWS → use display name.
                    winners.append(
                        {"rank": rank, "username": display_name, "guess": float(guess), "difference": float(diff)}
                    )

                logger.info(f"GTB session #{session_id} completed with result ${result_amount:,.2f}")
                return True, f"Results set! Winners calculated.", winners

        except Exception as e:
            logger.error(f"Failed to set result: {e}")
            return False, f"Failed to set result: {str(e)}", None

    def get_session_stats(self, session_id: int) -> Optional[Dict]:
        """Get statistics for a specific session"""
        if not self.engine:
            return None

        try:
            with self.engine.connect() as conn:
                session = conn.execute(
                    text(
                        """
                    SELECT id, opened_by, opened_at, closed_at, result_amount, status
                    FROM gtb_sessions
                    WHERE id = :session_id
                """
                    ),
                    {"session_id": session_id},
                ).fetchone()

                if not session:
                    return None

                guess_count = conn.execute(
                    text(
                        """
                    SELECT COUNT(*) FROM gtb_guesses WHERE session_id = :session_id
                """
                    ),
                    {"session_id": session_id},
                ).fetchone()[0]

                return {
                    "id": session[0],
                    "opened_by": session[1],
                    "opened_at": session[2],
                    "closed_at": session[3],
                    "result_amount": float(session[4]) if session[4] else None,
                    "status": session[5],
                    "guess_count": guess_count,
                }
        except Exception as e:
            logger.error(f"Failed to get session stats: {e}")
            return None


#: gtb_guesses.guess_amount is NUMERIC(12, 2) — 10 digits before the point.
MAX_GUESS_AMOUNT = 999999999999.99


def parse_amount(amount_str: str) -> Optional[float]:
    """
    Parse amount string to float, handling commas and dollar signs

    Examples:
        "$1234.56" -> 1234.56
        "1,234.56" -> 1234.56
        "1234" -> 1234.0

    Returns None for anything that is not a usable amount, which INCLUDES the
    special floats: `float()` happily accepts "nan", "inf" and overflowing
    literals like "1e400". `nan <= 0` is False, so a bare positivity check lets
    NaN through and it reaches the NUMERIC column as a value no comparison can
    order — which would quietly corrupt winner scoring. Bound the magnitude here
    too, so an out-of-range guess is refused with a message instead of raising
    a numeric-overflow error out of the INSERT.
    """
    try:
        # Remove dollar signs, commas, and spaces
        cleaned = amount_str.replace("$", "").replace(",", "").replace(" ", "").strip()

        # Convert to float
        amount = float(cleaned)

        # Validate. `amount != amount` is the NaN test (NaN is not equal to
        # itself); the range check rejects the infinities on the same pass.
        if amount != amount or amount <= 0 or amount > MAX_GUESS_AMOUNT:
            return None

        return amount
    except (ValueError, InvalidOperation, AttributeError, TypeError):
        return None
