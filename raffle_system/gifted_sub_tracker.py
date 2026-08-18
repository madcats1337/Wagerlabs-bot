"""
Gifted Sub Tracker
Listens for Kick gifted subscription events and awards raffle tickets
"""

import json
import logging
from datetime import datetime

from sqlalchemy import text

from .config import GIFTED_SUB_TICKETS
from .tickets import TicketManager

logger = logging.getLogger(__name__)


class GiftedSubTracker:
    """Tracks gifted subs and awards raffle tickets"""

    def __init__(self, engine, server_id=None, bot_settings=None):
        self.engine = engine
        self.server_id = server_id
        self.bot_settings = bot_settings
        self.ticket_manager = TicketManager(engine, server_id=server_id)
        self._load_settings()

    def _load_settings(self):
        """Load gifted sub ticket settings from bot_settings or config"""
        if self.bot_settings:
            # Try to get from database settings
            try:
                self.bot_settings.refresh()
                self.gifted_sub_tickets = int(self.bot_settings.get("gifted_sub_tickets") or GIFTED_SUB_TICKETS)
            except (ValueError, AttributeError):
                self.gifted_sub_tickets = GIFTED_SUB_TICKETS
        else:
            # Fall back to config default
            self.gifted_sub_tickets = GIFTED_SUB_TICKETS

    async def handle_gifted_sub_event(self, event_data):
        """
        Handle a subscription event from Kick websocket (gifted or regular)

        Expected event_data format from Kick:
        {
            "id": "unique_event_id",
            "sender": {
                "username": "generous_viewer",
                "id": 12345
            },
            "gift_count": 5,  # For gifted subs (optional)
            "months": 1        # For regular subs (optional)
        }

        OR for LuckyUsersWhoGotGiftSubscriptionsEvent:
        {
            "channel": {...},
            "usernames": ["user1", "user2", "user3"],
            "gifter_username": "generous_viewer"
        }

        Args:
            event_data: Parsed JSON data from the Kick websocket event

        Returns:
            dict: Result of ticket awarding
        """
        try:
            # Re-read the reward rate on every event so a dashboard change to
            # "Tickets per gifted sub" applies to the next gifted sub instead of
            # only after a bot restart (this tracker is created once at startup).
            self._load_settings()

            # Extract event details
            event_id = event_data.get("id")

            # Handle different event formats
            # LuckyUsersWhoGotGiftSubscriptionsEvent format
            if "gifter_username" in event_data:
                logger.info(f"[GiftedSubTracker] Processing LuckyUsersWhoGotGiftSubscriptionsEvent")
                gifter_kick_name = event_data.get("gifter_username")
                # usernames is an array of recipients
                recipients = event_data.get("usernames", [])
                # If recipients list is empty or None, default to 1 sub
                gift_count = len(recipients) if (recipients and len(recipients) > 0) else 1
                logger.info(
                    f"[GiftedSubTracker] Gifter: {gifter_kick_name}, Recipients: {recipients}, Count: {gift_count}"
                )
            else:
                # For regular subs, the subscriber might be in "sender" or directly in event
                sender = event_data.get("sender", {})
                gifter_kick_name = sender.get("username") or event_data.get("username")

                # Try different possible field names for gift count
                gift_count = (
                    event_data.get("gift_count")
                    or event_data.get("quantity")
                    or event_data.get("count")
                    or event_data.get("gifted_usernames", [])  # If it's an array of recipients
                )

                # If gift_count is a list (recipients), count the length
                if isinstance(gift_count, list):
                    gift_count = len(gift_count)
                elif gift_count is None:
                    # For regular subs (not gifted), count as 1
                    gift_count = 1

            if not gifter_kick_name:
                logger.warning("Gifted sub event missing gifter username")
                return {"status": "error", "error": "missing_username"}

            if not event_id:
                # Generate a fallback ID if none provided
                event_id = f"{gifter_kick_name}_{int(datetime.now().timestamp())}"
                logger.info(f"[GiftedSubTracker] No event ID provided, generated: {event_id}")

            # Get active raffle period
            period_id = self._get_active_period_id()
            if not period_id:
                logger.warning("No active raffle period - cannot award tickets for gifted sub")
                return {"status": "no_active_period"}

            # Check if this event has already been processed (prevent duplicates)
            with self.engine.begin() as conn:
                existing = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_gifted_subs
                    WHERE kick_event_id = :event_id
                """
                    ),
                    {"event_id": event_id},
                )

                if existing.fetchone():
                    logger.debug(f"Gifted sub event {event_id} already processed - skipping")
                    return {"status": "duplicate", "event_id": event_id}

                # Look up Discord ID from links table (multiserver: filter by server_id)
                if self.server_id:
                    discord_result = conn.execute(
                        text(
                            """
                        SELECT discord_id FROM links
                        WHERE kick_name = :kick_name AND discord_server_id = :sid
                    """
                        ),
                        {"kick_name": gifter_kick_name, "sid": self.server_id},
                    )
                else:
                    discord_result = conn.execute(
                        text(
                            """
                        SELECT discord_id FROM links
                        WHERE kick_name = :kick_name
                    """
                        ),
                        {"kick_name": gifter_kick_name},
                    )

                discord_row = discord_result.fetchone()
                discord_id = discord_row[0] if discord_row else None

                if not discord_id:
                    logger.warning(f"User {gifter_kick_name} not linked - cannot award tickets")
                    # Still log the event but don't award tickets
                    conn.execute(
                        text(
                            """
                        INSERT INTO raffle_gifted_subs
                            (period_id, discord_server_id, gifter_kick_name,
                             gifter_discord_id, sub_count, tickets_awarded, kick_event_id)
                        VALUES
                            (:period_id,
                             -- discord_server_id is NOT NULL; derive the owner from the period.
                             (SELECT discord_server_id FROM raffle_periods WHERE id = :period_id),
                             :kick_name, NULL, :sub_count, 0, :event_id)
                    """
                        ),
                        {
                            "period_id": period_id,
                            "kick_name": gifter_kick_name,
                            "sub_count": gift_count,
                            "event_id": event_id,
                        },
                    )
                    return {"status": "not_linked", "kick_name": gifter_kick_name}

                # Calculate tickets
                tickets_to_award = gift_count * self.gifted_sub_tickets

                # Award the tickets
                sub_description = "Subscribed" if gift_count == 1 else f"Gifted {gift_count} subs"
                success = self.ticket_manager.award_tickets(
                    discord_id=discord_id,
                    kick_name=gifter_kick_name,
                    tickets=tickets_to_award,
                    source="gifted_sub",
                    description=f"{sub_description} in chat",
                    period_id=period_id,
                )

                if not success:
                    logger.error(f"Failed to award tickets to {gifter_kick_name}")
                    return {"status": "award_failed"}

                # Log the gifted sub event
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_gifted_subs
                        (period_id, discord_server_id, gifter_kick_name,
                         gifter_discord_id, sub_count, tickets_awarded, kick_event_id)
                    VALUES
                        (:period_id,
                         -- discord_server_id is NOT NULL; derive the owner from the period.
                         (SELECT discord_server_id FROM raffle_periods WHERE id = :period_id),
                         :kick_name, :discord_id, :sub_count,
                         :tickets, :event_id)
                """
                    ),
                    {
                        "period_id": period_id,
                        "kick_name": gifter_kick_name,
                        "discord_id": discord_id,
                        "sub_count": gift_count,
                        "tickets": tickets_to_award,
                        "event_id": event_id,
                    },
                )

            logger.info(f"🎁 {gifter_kick_name} gifted {gift_count} sub(s) → {tickets_to_award} tickets")

            return {
                "status": "success",
                "gifter": gifter_kick_name,
                "discord_id": discord_id,
                "gift_count": gift_count,
                "tickets_awarded": tickets_to_award,
            }

        except Exception as e:
            logger.error(f"Failed to handle gifted sub event: {e}")
            return {"status": "error", "error": str(e)}

    def get_user_gifted_subs(self, discord_id, period_id=None):
        """
        Get gifted sub history for a user

        Args:
            discord_id: Discord user ID
            period_id: Raffle period ID (None = current active period)

        Returns:
            list: List of gifted sub events
        """
        try:
            if period_id is None:
                period_id = self._get_active_period_id()
                if not period_id:
                    return []

            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT
                        sub_count,
                        tickets_awarded,
                        gifted_at
                    FROM raffle_gifted_subs
                    WHERE period_id = :period_id AND gifter_discord_id = :discord_id
                    ORDER BY gifted_at DESC
                """
                    ),
                    {"period_id": period_id, "discord_id": discord_id},
                )

                events = []
                for row in result:
                    events.append({"sub_count": row[0], "tickets_awarded": row[1], "gifted_at": row[2]})

                return events

        except Exception as e:
            logger.error(f"Failed to get gifted subs: {e}")
            return []

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


def setup_gifted_sub_handler(engine, server_id=None, bot_settings=None):
    """
    Create a gifted sub tracker instance

    Args:
        engine: SQLAlchemy engine
        server_id: Discord server/guild ID for multi-server support (optional)
        bot_settings: BotSettingsManager instance for loading settings (optional)

    Returns:
        GiftedSubTracker: Initialized tracker
    """
    tracker = GiftedSubTracker(engine, server_id=server_id, bot_settings=bot_settings)
    logger.debug(f"✅ Gifted sub tracker initialized" + (f" (server {server_id})" if server_id else ""))
    return tracker


async def track_gifted_sub(engine, gifter_username, guild_id, count=1, event_id=None, period_id=None):
    """Award raffle tickets to a GIFTER for a gifted-sub event (Kick webhook path).

    This is the entry point used by the Kick *webhook* handler
    (`core/kick_webhooks.py::on_gifted_subs`). It mirrors the websocket/Pusher
    path by delegating to `GiftedSubTracker.handle_gifted_sub_event`, so both
    paths share the SAME award math, per-event dedup, per-guild reward rate,
    and fresh-rate read — there is no second copy of the logic to drift.

    Tickets go to the GIFTER (the person who bought the subs), scaled by
    `count`. The webhook fires ONCE per gift event with the full giftee list,
    so callers pass the gifter + the number of subs, NOT one call per giftee.

    Args:
        engine: SQLAlchemy engine (the webhook already builds one per event).
        gifter_username: Kick username of the person who gifted the sub(s).
        guild_id: Discord server id (for per-server scoping + settings).
        count: Number of subs gifted in this event (default 1).
        event_id: Stable Kick event id for dedup. Strongly recommended so a
            webhook re-delivery doesn't double-award; a fallback id is derived
            when absent (see handle_gifted_sub_event).
        period_id: Unused — kept for backwards compatibility with the existing
            call site. The active period is resolved inside the tracker.

    Returns:
        dict: the tracker's result (status success/duplicate/not_linked/…).
    """
    if not gifter_username:
        return {"status": "error", "error": "missing_gifter"}

    # Build a per-guild settings manager so the reward RATE matches the
    # dashboard's "Tickets per gifted sub" for THIS server (not a default).
    bot_settings = None
    try:
        from utils.bot_settings import BotSettingsManager

        bot_settings = BotSettingsManager(engine, guild_id=guild_id)
    except Exception as e:
        logger.warning(f"[GiftedSub] Could not load per-guild settings for {guild_id}: {e}")

    tracker = GiftedSubTracker(engine, server_id=guild_id, bot_settings=bot_settings)

    # Shape a synthetic event in the same format handle_gifted_sub_event parses.
    event = {
        "id": event_id,
        "sender": {"username": gifter_username},
        "gift_count": count,
    }
    return await tracker.handle_gifted_sub_event(event)
