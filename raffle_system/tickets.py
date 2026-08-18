"""
Core Ticket Management Logic
Handles all ticket operations (award, remove, query, transfer)
"""

import logging
from datetime import datetime

from sqlalchemy import text

logger = logging.getLogger(__name__)


class TicketManager:
    """Manages raffle tickets for all users"""

    def __init__(self, engine, server_id=None):
        self.engine = engine
        self.server_id = server_id

    def get_user_tickets(self, discord_id, period_id=None):
        """
        Get ticket balance for a user

        Args:
            discord_id: Discord user ID
            period_id: Raffle period ID (None = current active period)

        Returns:
            dict: Ticket breakdown or None
        """
        try:
            # Get current period if not specified
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    return None

            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT
                        kick_name,
                        watchtime_tickets,
                        gifted_sub_tickets,
                        shuffle_wager_tickets,
                        bonus_tickets,
                        total_tickets,
                        last_updated
                    FROM raffle_tickets
                    WHERE period_id = :period_id AND discord_id = :discord_id
                """
                    ),
                    {"period_id": period_id, "discord_id": discord_id},
                )

                row = result.fetchone()
                if row:
                    return {
                        "discord_id": discord_id,
                        "kick_name": row[0],
                        "watchtime_tickets": row[1],
                        "gifted_sub_tickets": row[2],
                        "shuffle_wager_tickets": row[3],
                        "bonus_tickets": row[4],
                        "total_tickets": row[5],
                        "last_updated": row[6],
                        "period_id": period_id,
                    }
                return None

        except Exception as e:
            logger.error(f"Failed to get tickets for user {discord_id}: {e}")
            return None

    def award_tickets(self, discord_id, kick_name, tickets, source, description=None, period_id=None):
        """
        Award tickets to a user

        Args:
            discord_id: Discord user ID
            kick_name: Kick username
            tickets: Number of tickets to award
            source: Source of tickets ('watchtime', 'gifted_sub', 'shuffle_wager', 'bonus')
            description: Optional description for audit log
            period_id: Raffle period ID (None = current active period)

        Returns:
            bool: True if successful
        """
        if tickets <= 0:
            logger.warning(f"Attempted to award {tickets} tickets (must be > 0)")
            return False

        try:
            # Get current period if not specified
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    logger.error("No active raffle period found")
                    return False

            # Determine which column to update based on source
            source_column_map = {
                "watchtime": "watchtime_tickets",
                "gifted_sub": "gifted_sub_tickets",
                "shuffle_wager": "shuffle_wager_tickets",
                "bonus": "bonus_tickets",
            }

            source_column = source_column_map.get(source)
            if not source_column:
                logger.error(f"Invalid ticket source: {source}")
                return False

            with self.engine.begin() as conn:
                # Get discord_server_id from the period
                period_result = conn.execute(
                    text(
                        """
                    SELECT discord_server_id FROM raffle_periods WHERE id = :period_id
                """
                    ),
                    {"period_id": period_id},
                )
                period_row = period_result.fetchone()
                if not period_row:
                    logger.error(f"Period {period_id} not found")
                    return False
                discord_server_id = period_row[0]

                # Insert or update user's ticket balance
                conn.execute(
                    text(
                        f"""
                    INSERT INTO raffle_tickets
                        (period_id, discord_server_id, discord_id, kick_name, {source_column}, total_tickets, last_updated)
                    VALUES
                        (:period_id, :server_id, :discord_id, :kick_name, :tickets, :tickets, CURRENT_TIMESTAMP)
                    ON CONFLICT (period_id, discord_id)
                    DO UPDATE SET
                        {source_column} = raffle_tickets.{source_column} + :tickets,
                        total_tickets = raffle_tickets.total_tickets + :tickets,
                        last_updated = CURRENT_TIMESTAMP
                """
                    ),
                    {
                        "period_id": period_id,
                        "server_id": discord_server_id,
                        "discord_id": discord_id,
                        "kick_name": kick_name,
                        "tickets": tickets,
                    },
                )

                # Log the transaction
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_ticket_log
                        (period_id, discord_server_id, discord_id, kick_name,
                         ticket_change, source, description)
                    VALUES
                        (:period_id, :server_id, :discord_id, :kick_name, :change, :source, :desc)
                """
                    ),
                    {
                        "period_id": period_id,
                        # NOT NULL with a per-guild DEFAULT: omitting it filed the
                        # row under one hardcoded guild. Reuse the id already
                        # resolved from the period above.
                        "server_id": discord_server_id,
                        "discord_id": discord_id,
                        "kick_name": kick_name,
                        "change": tickets,
                        "source": source,
                        "desc": description or f"Awarded {tickets} tickets from {source}",
                    },
                )

            logger.info(f"✅ Awarded {tickets} {source} tickets to {kick_name} (Discord ID: {discord_id})")
            return True

        except Exception as e:
            logger.error(f"Failed to award tickets: {e}")
            return False

    def remove_tickets(self, discord_id, kick_name, tickets, reason, period_id=None):
        """
        Remove tickets from a user (for violations, etc.)

        Args:
            discord_id: Discord user ID
            kick_name: Kick username
            tickets: Number of tickets to remove
            reason: Reason for removal
            period_id: Raffle period ID (None = current active period)

        Returns:
            bool: True if successful
        """
        if tickets <= 0:
            logger.warning(f"Attempted to remove {tickets} tickets (must be > 0)")
            return False

        try:
            # Get current period if not specified
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    logger.error("No active raffle period found")
                    return False

            with self.engine.begin() as conn:
                # Get current balance
                result = conn.execute(
                    text(
                        """
                    SELECT total_tickets FROM raffle_tickets
                    WHERE period_id = :period_id AND discord_id = :discord_id
                """
                    ),
                    {"period_id": period_id, "discord_id": discord_id},
                )
                row = result.fetchone()

                if not row:
                    logger.warning(f"User {discord_id} has no tickets to remove")
                    return False

                current_total = row[0]
                new_total = max(0, current_total - tickets)  # Don't go negative
                actual_removed = current_total - new_total

                # Update total tickets (proportionally reduce all sources)
                conn.execute(
                    text(
                        """
                    UPDATE raffle_tickets
                    SET
                        watchtime_tickets = GREATEST(0, CAST(watchtime_tickets * :ratio AS INTEGER)),
                        gifted_sub_tickets = GREATEST(0, CAST(gifted_sub_tickets * :ratio AS INTEGER)),
                        shuffle_wager_tickets = GREATEST(0, CAST(shuffle_wager_tickets * :ratio AS INTEGER)),
                        bonus_tickets = GREATEST(0, CAST(bonus_tickets * :ratio AS INTEGER)),
                        total_tickets = :new_total,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE period_id = :period_id AND discord_id = :discord_id
                """
                    ),
                    {
                        "period_id": period_id,
                        "discord_id": discord_id,
                        "ratio": new_total / current_total if current_total > 0 else 0,
                        "new_total": new_total,
                    },
                )

                # Log the removal
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_ticket_log
                        (period_id, discord_server_id, discord_id, kick_name,
                         ticket_change, source, description)
                    VALUES
                        (:period_id,
                         -- The period owns the row; deriving the tenant from it
                         -- is correct by construction and needs nothing in scope.
                         (SELECT discord_server_id FROM raffle_periods WHERE id = :period_id),
                         :discord_id, :kick_name, :change, 'admin_removal', :reason)
                """
                    ),
                    {
                        "period_id": period_id,
                        "discord_id": discord_id,
                        "kick_name": kick_name,
                        "change": -actual_removed,
                        "reason": reason,
                    },
                )

            logger.info(
                f"✅ Removed {actual_removed} tickets from {kick_name} (Discord ID: {discord_id}). Reason: {reason}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to remove tickets: {e}")
            return False

    def get_leaderboard(self, limit=10, period_id=None):
        """
        Get top ticket holders

        Args:
            limit: Number of users to return
            period_id: Raffle period ID (None = current active period)

        Returns:
            list: List of dicts with user info and tickets
        """
        try:
            # Get current period if not specified
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    return []

            with self.engine.begin() as conn:
                # Build WHERE clause based on multiserver support
                if self.server_id:
                    where_clause = (
                        "WHERE period_id = :period_id AND discord_server_id = :server_id AND total_tickets > 0"
                    )
                    params = {"period_id": period_id, "server_id": self.server_id, "limit": limit}
                else:
                    where_clause = "WHERE period_id = :period_id AND total_tickets > 0"
                    params = {"period_id": period_id, "limit": limit}

                result = conn.execute(
                    text(
                        f"""
                    SELECT
                        discord_id,
                        kick_name,
                        watchtime_tickets,
                        gifted_sub_tickets,
                        shuffle_wager_tickets,
                        bonus_tickets,
                        total_tickets,
                        RANK() OVER (ORDER BY total_tickets DESC) as rank
                    FROM raffle_tickets
                    {where_clause}
                    ORDER BY total_tickets DESC
                    LIMIT :limit
                """
                    ),
                    params,
                )

                leaderboard = []
                for row in result:
                    leaderboard.append(
                        {
                            "discord_id": row[0],
                            "kick_name": row[1],
                            "watchtime_tickets": row[2],
                            "gifted_sub_tickets": row[3],
                            "shuffle_wager_tickets": row[4],
                            "bonus_tickets": row[5],
                            "total_tickets": row[6],
                            "rank": row[7],
                        }
                    )

                return leaderboard

        except Exception as e:
            logger.error(f"Failed to get leaderboard: {e}")
            return []

    def get_user_rank(self, discord_id, period_id=None):
        """
        Get a user's rank in the current period

        Args:
            discord_id: Discord user ID
            period_id: Raffle period ID (None = current active period)

        Returns:
            int: Rank (1-indexed) or None
        """
        try:
            # Get current period if not specified
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    return None

            with self.engine.begin() as conn:
                # Build WHERE clause based on multiserver support
                if self.server_id:
                    where_clause = (
                        "WHERE period_id = :period_id AND discord_server_id = :server_id AND discord_id = :discord_id"
                    )
                    params = {"period_id": period_id, "server_id": self.server_id, "discord_id": discord_id}
                else:
                    where_clause = "WHERE period_id = :period_id AND discord_id = :discord_id"
                    params = {"period_id": period_id, "discord_id": discord_id}

                result = conn.execute(
                    text(
                        f"""
                    SELECT rank FROM raffle_leaderboard
                    {where_clause}
                """
                    ),
                    params,
                )

                row = result.fetchone()
                return row[0] if row else None

        except Exception as e:
            logger.error(f"Failed to get user rank: {e}")
            return None

    def get_period_stats(self, period_id=None):
        """
        Get statistics for a raffle period

        Args:
            period_id: Raffle period ID (None = current active period)

        Returns:
            dict: Period statistics
        """
        try:
            # Get current period if not specified
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    return None

            with self.engine.begin() as conn:
                # Build WHERE clause based on multiserver support
                if self.server_id:
                    where_clause = "WHERE period_id = :period_id AND discord_server_id = :server_id"
                    params = {"period_id": period_id, "server_id": self.server_id}
                else:
                    where_clause = "WHERE period_id = :period_id"
                    params = {"period_id": period_id}

                result = conn.execute(
                    text(
                        f"""
                    SELECT * FROM raffle_current_stats
                    {where_clause}
                """
                    ),
                    params,
                )

                row = result.fetchone()
                if row:
                    # Per-server display number for this period. Looked up
                    # directly from raffle_periods (the stats view is positional
                    # and may not carry it); COALESCE to id for legacy rows.
                    number_row = conn.execute(
                        text("SELECT COALESCE(period_number, id) FROM raffle_periods WHERE id = :period_id"),
                        {"period_id": period_id},
                    ).fetchone()
                    period_number = number_row[0] if number_row else period_id
                    return {
                        "period_id": row[0],
                        "period_number": period_number,
                        "discord_server_id": row[1],
                        "start_date": row[2],
                        "end_date": row[3],
                        "status": row[4],
                        "total_participants": row[5],
                        "total_tickets": row[6],
                        "watchtime_tickets": row[7],
                        "gifted_sub_tickets": row[8],
                        "shuffle_wager_tickets": row[9],
                        "bonus_tickets": row[10],
                    }
                return None

        except Exception as e:
            logger.error(f"Failed to get period stats: {e}")
            return None

    def _get_active_period_id(self):
        """Get the ID of the currently active raffle period"""
        try:
            with self.engine.begin() as conn:
                if self.server_id:
                    # Multiserver: filter by discord_server_id
                    result = conn.execute(
                        text(
                            """
                        SELECT id FROM raffle_periods
                        WHERE status = 'active' AND discord_server_id = :sid
                        ORDER BY start_date DESC
                        LIMIT 1
                    """
                        ),
                        {"sid": self.server_id},
                    )
                else:
                    # Backwards compatible: no server filter
                    result = conn.execute(
                        text(
                            """
                        SELECT id FROM raffle_periods
                        WHERE status = 'active'
                        ORDER BY start_date DESC
                        LIMIT 1
                    """
                        )
                    )
                row = result.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get active period: {e}")
            return None
