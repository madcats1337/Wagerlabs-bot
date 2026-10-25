"""
Custom Commands Manager
Handles dynamic custom commands loaded from database
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import psycopg2

from features.points.points_grants import MAX_GRANT, give_points_to_chat_name, parse_positive_int

logger = logging.getLogger(__name__)

#: `{give_points}` (amount typed after the target) or `{give_points:500}` (fixed).
_GIVE_POINTS_RE = re.compile(r"\{give_points(?::([0-9][0-9,]*))?\}")


_DATABASE_VARIABLES = frozenset(
    {
        "{channel}",
        "{tickets}",
        "{points}",
        "{points_earned}",
        "{points_spent}",
        "{watchtime}",
        "{watchtime_minutes}",
        "{watchtime_formatted}",
        "{points_rank}",
        "{watchtime_rank}",
        "{watchtime_tickets}",
        "{gifted_sub_tickets}",
        "{wager_tickets}",
        "{bonus_tickets}",
        "{raffle_rank}",
        "{raffle_chance}",
        "{raffle_total}",
        "{raffle_participants}",
        "{raffle_ends_in}",
        "{current_slot}",
        "{current_provider}",
        "{slot_requests_open}",
        "{slot_queue_count}",
        "{user_slot_request_count}",
        "{user_latest_slot_request}",
        "{giveaway_title}",
        "{giveaway_method}",
        "{giveaway_keyword}",
        "{giveaway_entries}",
        "{giveaway_participants}",
        "{giveaway_entered}",
        "{hunt_start_balance}",
        "{hunt_bonus_count}",
        "{hunt_remaining}",
        "{hunt_total_payout}",
        "{hunt_profit}",
    }
)


class CustomCommandsManager:
    def __init__(self, bot, send_message_callback=None, discord_server_id=None, engine=None):
        """
        Initialize custom commands manager

        Args:
            bot: The Discord bot instance
            send_message_callback: Async function to send messages to Kick chat
            discord_server_id: Discord server ID for multiserver support
            engine: SQLAlchemy engine, used by the {give_points} grant
        """
        self.bot = bot
        self.send_message_callback = send_message_callback
        self.discord_server_id = discord_server_id
        self.engine = engine
        self.commands = {}  # {command_name: {response, cooldown, enabled, use_count}}
        self.last_used = {}  # {command_name: last_used_timestamp}
        self.database_url = os.getenv("DATABASE_URL")

        logger.debug(f"🔧 Custom Commands Manager initialized for server {discord_server_id}")

    async def load_commands(self):
        """Load all custom commands from database"""
        if not self.database_url:
            logger.warning("⚠️ DATABASE_URL not set, custom commands disabled")
            return

        try:
            # Run DB query in thread pool to avoid blocking
            commands = await asyncio.to_thread(self._fetch_commands_from_db)

            self.commands = {}
            for cmd in commands:
                self.commands[cmd["command"]] = {
                    "id": cmd["id"],
                    "response": cmd["response"],
                    "cooldown": cmd["cooldown"],
                    "enabled": cmd["enabled"],
                    "use_count": cmd["use_count"],
                }

            enabled_count = sum(1 for cmd in self.commands.values() if cmd["enabled"])
            logger.debug(f"✅ Loaded {len(self.commands)} custom commands ({enabled_count} enabled)")

        except Exception as e:
            logger.error(f"❌ Error loading custom commands: {e}")

    def _fetch_commands_from_db(self):
        """Fetch commands from database (blocking - run in thread pool)"""
        conn = psycopg2.connect(self.database_url)
        cursor = conn.cursor()

        if self.discord_server_id:
            cursor.execute(
                """
                SELECT id, command, response, cooldown, enabled, use_count
                FROM custom_commands
                WHERE discord_server_id = %s
                ORDER BY command
            """,
                (self.discord_server_id,),
            )
        else:
            # Fallback for backward compatibility
            cursor.execute(
                """
                SELECT id, command, response, cooldown, enabled, use_count
                FROM custom_commands
                ORDER BY command
            """
            )

        commands = []
        for row in cursor.fetchall():
            commands.append(
                {
                    "id": row[0],
                    "command": row[1],
                    "response": row[2],
                    "cooldown": row[3],
                    "enabled": row[4],
                    "use_count": row[5] or 0,
                }
            )

        cursor.close()
        conn.close()

        return commands

    async def reload_commands(self):
        """Reload commands from database (called when dashboard updates)"""
        logger.info("🔄 Reloading custom commands from database...")
        await self.load_commands()

    async def handle_message(
        self, message_content, username, *, platform="kick", display_name=None, is_moderator=False, recent_names=()
    ):
        """
        Check if message is a custom command and respond

        Args:
            message_content: The message text
            username: Canonical username used for shared viewer statistics
            platform: Originating chat platform (kick or twitch)
            display_name: Native platform username used for display
            is_moderator: Chatter has a moderator or broadcaster badge
            recent_names: Recent chatters, which count as known {give_points} targets

        Returns:
            bool: True if command was handled, False otherwise
        """
        # Check if message starts with !
        if not message_content.startswith("!"):
            return False

        # Extract the command plus both the raw argument tail and its first word.
        invocation = message_content[1:].strip()
        if not invocation:
            return False

        parts = invocation.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        arg1 = args.split(maxsplit=1)[0] if args else ""

        # Check if command exists and is enabled
        if command not in self.commands:
            return False

        cmd_data = self.commands[command]

        if not cmd_data["enabled"]:
            return False

        give_match = _GIVE_POINTS_RE.search(cmd_data["response"])
        native_name = display_name or username

        # A {give_points} command creates points, so only mods and the
        # broadcaster may run it. Anyone else is ignored without a reply (so it
        # can't be used to spam chat), before the cooldown so a viewer's attempt
        # can't block the next mod use.
        if give_match and not is_moderator:
            return True

        # Check cooldown
        if command in self.last_used:
            cooldown = cmd_data["cooldown"]
            time_since_last = (datetime.now() - self.last_used[command]).total_seconds()

            if time_since_last < cooldown:
                remaining = int(cooldown - time_since_last)
                logger.info(f"⏱️  Command !{command} on cooldown ({remaining}s remaining)")
                return True  # Still handled, just on cooldown

        # Send response with variable replacements
        try:
            if self.send_message_callback:
                response = cmd_data["response"]

                grant_replacements = {}
                if give_match:
                    outcome = await self._give_points(give_match, command, args, platform, recent_names)
                    if isinstance(outcome, str):
                        # Usage or lookup problem: tell the mod, grant nothing,
                        # and leave the cooldown and use count alone.
                        await self.send_message_callback(f"@{native_name} {outcome}")
                        return True
                    target, amount, balance = outcome
                    grant_replacements = {
                        give_match.group(0): f"{amount:,}",
                        "{target}": target,
                        "{target_points}": f"{balance:,}",
                    }
                    logger.info(f"Custom command !{command}: {username} gave {amount} points to {target}")

                # Apply variable replacements
                next_use_count = int(cmd_data.get("use_count") or 0) + 1
                response = await self._replace_variables(
                    response,
                    username,
                    command,
                    args=args,
                    arg1=arg1,
                    platform=platform,
                    display_name=display_name,
                    use_count=next_use_count,
                    extra_replacements=grant_replacements,
                )

                await self.send_message_callback(response)
                logger.info(f"✅ Custom command !{command} executed by {username}")

                # Update last used
                self.last_used[command] = datetime.now()
                cmd_data["use_count"] = next_use_count

                # Increment use count in database (don't wait for it)
                asyncio.create_task(self._increment_use_count(cmd_data["id"]))

                return True
            else:
                logger.warning(f"⚠️ No send callback available for command !{command}")
                return False

        except Exception as e:
            logger.error(f"❌ Error executing custom command !{command}: {e}")
            return False

    async def _replace_variables(
        self,
        text: str,
        username: str,
        command: str,
        *,
        args: str = "",
        arg1: str = "",
        platform: str = "kick",
        display_name: str = None,
        use_count: int = 0,
        extra_replacements: dict = None,
    ) -> str:
        """
        Replace custom variables in command response text

        Available variables:
        - {user} - Username who triggered the command
        - {display_name} - Native Kick/Twitch display name
        - {platform} - Kick or Twitch
        - {channel} - Channel name for the originating platform
        - {command} - Command name without the ! prefix
        - {args} - Full argument text after the command
        - {arg1} - First whitespace-delimited argument
        - {uses} - Command use count including this execution
        - {tickets} - User's raffle ticket count (if linked)
        - {points} - User's points balance (if they have points)
        - {points_earned} - User's lifetime points earned
        - {points_spent} - User's lifetime points spent
        - {watchtime} - User's total watchtime in hours (if linked)
        - {watchtime_minutes} - User's total watchtime in minutes
        - {watchtime_formatted} - User's watchtime as hours/minutes
        - {points_rank} - User's current points rank
        - {watchtime_rank} - User's current watchtime rank
        - {watchtime_tickets} - User's raffle tickets earned from watchtime
        - {gifted_sub_tickets} - User's raffle tickets earned from gifted subs
        - {wager_tickets} - User's raffle tickets earned from wager tracking
        - {bonus_tickets} - User's bonus/manual raffle tickets
        - {raffle_rank} - User's rank in the active raffle
        - {raffle_chance} - User's current raffle win chance
        - {raffle_total} - Total tickets in the active raffle
        - {raffle_participants} - Number of participants in the active raffle
        - {raffle_ends_in} - Time remaining in the active raffle
        - {current_slot} - Slot the streamer is currently playing (extension-tracked)
        - {current_provider} - Provider of the current slot
        - {slot_requests_open} - open or closed
        - {slot_queue_count} - Number of unpicked slot requests
        - {user_slot_request_count} - User's number of pending slot requests
        - {user_latest_slot_request} - User's most recently submitted pending slot
        - {giveaway_title} - Active giveaway title
        - {giveaway_method} - Active giveaway entry method
        - {giveaway_keyword} - Active giveaway keyword
        - {giveaway_entries} - Total entries in the active giveaway
        - {giveaway_participants} - Unique participants in the active giveaway
        - {giveaway_entered} - yes if the user entered the active giveaway
        - {hunt_start_balance} - Active bonus hunt starting balance
        - {hunt_bonus_count} - Total bonuses in the active hunt
        - {hunt_remaining} - Unopened bonuses in the active hunt
        - {hunt_total_payout} - Current total payout in the active hunt
        - {hunt_profit} - Current active-hunt profit or loss
        - {give_points} / {give_points:N} - Points granted by a mod grant (see _give_points)
        - {target} - The viewer a mod grant credited
        - {target_points} - That viewer's balance after the grant

        Args:
            text: The response text with variables
            username: Username who triggered the command
            command: Command name without the ! prefix

        Returns:
            Text with variables replaced
        """
        platform_key = (platform or "kick").strip().lower()
        platform_label = {"kick": "Kick", "twitch": "Twitch"}.get(platform_key, platform_key.capitalize())
        native_name = display_name or username
        basic_replacements = {
            "{user}": username,
            "{display_name}": native_name,
            "{platform}": platform_label,
            "{command}": command,
            "{args}": args,
            "{arg1}": arg1,
            "{uses}": f"{int(use_count or 0):,}",
            **(extra_replacements or {}),
        }
        requested_tokens = {token for token in _DATABASE_VARIABLES if token in text}
        if not requested_tokens:
            return self._replace_tokens(text, basic_replacements)

        data = {
            "channel": "unknown",
            "tickets": 0,
            "points": 0,
            "points_earned": 0,
            "points_spent": 0,
            "watchtime_minutes": 0,
            "current_slot": None,
            "current_provider": None,
            "points_rank": None,
            "watchtime_rank": None,
            "watchtime_tickets": 0,
            "gifted_sub_tickets": 0,
            "wager_tickets": 0,
            "bonus_tickets": 0,
            "raffle_rank": None,
            "raffle_pool_tickets": 0,
            "raffle_participants": 0,
            "raffle_end_date": None,
            "slot_requests_open": True,
            "slot_queue_count": 0,
            "user_slot_request_count": 0,
            "user_latest_slot_request": None,
            "giveaway_title": None,
            "giveaway_method": None,
            "giveaway_keyword": None,
            "giveaway_entries": 0,
            "giveaway_participants": 0,
            "giveaway_entered": False,
            "hunt_start_balance": 0,
            "hunt_bonus_count": 0,
            "hunt_remaining": 0,
            "hunt_total_payout": 0,
            "hunt_profit": 0,
        }
        server_id = self.discord_server_id
        if server_id and self.database_url:
            try:
                fetched = await asyncio.to_thread(
                    self._fetch_variable_context,
                    server_id,
                    username,
                    native_name,
                    platform_key,
                    requested_tokens,
                )
                if fetched:
                    data.update(fetched)
            except Exception:
                logger.exception(f"Error fetching custom-command variable data for {username}")

        minutes = int(data.get("watchtime_minutes") or 0)
        hours, remaining_minutes = divmod(minutes, 60)
        if hours:
            formatted_watchtime = f"{hours}h {remaining_minutes}m"
        else:
            formatted_watchtime = f"{remaining_minutes}m"

        tickets = int(data.get("tickets") or 0)
        raffle_pool = int(data.get("raffle_pool_tickets") or 0)
        raffle_chance = (tickets / raffle_pool * 100) if raffle_pool > 0 else 0.0
        provider = data.get("current_provider") or "unknown"
        slot_name = data.get("current_slot")
        current_slot = f"{slot_name} ({provider})" if slot_name and provider != "unknown" else slot_name or "unknown"

        database_replacements = {
            "{channel}": data.get("channel") or "unknown",
            "{tickets}": f"{tickets:,}",
            "{points}": f"{int(data.get('points') or 0):,}",
            "{points_earned}": f"{int(data.get('points_earned') or 0):,}",
            "{points_spent}": f"{int(data.get('points_spent') or 0):,}",
            "{watchtime}": f"{minutes / 60:.1f}",
            "{watchtime_minutes}": f"{minutes:,}",
            "{watchtime_formatted}": formatted_watchtime,
            "{points_rank}": self._format_rank(data.get("points_rank")),
            "{watchtime_rank}": self._format_rank(data.get("watchtime_rank")),
            "{watchtime_tickets}": f"{int(data.get('watchtime_tickets') or 0):,}",
            "{gifted_sub_tickets}": f"{int(data.get('gifted_sub_tickets') or 0):,}",
            "{wager_tickets}": f"{int(data.get('wager_tickets') or 0):,}",
            "{bonus_tickets}": f"{int(data.get('bonus_tickets') or 0):,}",
            "{raffle_rank}": self._format_rank(data.get("raffle_rank")),
            "{raffle_chance}": f"{raffle_chance:.2f}%",
            "{raffle_total}": f"{raffle_pool:,}",
            "{raffle_participants}": f"{int(data.get('raffle_participants') or 0):,}",
            "{raffle_ends_in}": self._format_time_remaining(data.get("raffle_end_date")),
            "{current_slot}": current_slot,
            "{current_provider}": provider,
            "{slot_requests_open}": "open" if data.get("slot_requests_open") else "closed",
            "{slot_queue_count}": f"{int(data.get('slot_queue_count') or 0):,}",
            "{user_slot_request_count}": f"{int(data.get('user_slot_request_count') or 0):,}",
            "{user_latest_slot_request}": data.get("user_latest_slot_request") or "none",
            "{giveaway_title}": data.get("giveaway_title") or "none",
            "{giveaway_method}": (data.get("giveaway_method") or "none").replace("_", " "),
            "{giveaway_keyword}": data.get("giveaway_keyword") or "N/A",
            "{giveaway_entries}": f"{int(data.get('giveaway_entries') or 0):,}",
            "{giveaway_participants}": f"{int(data.get('giveaway_participants') or 0):,}",
            "{giveaway_entered}": "yes" if data.get("giveaway_entered") else "no",
            "{hunt_start_balance}": self._format_amount(data.get("hunt_start_balance")),
            "{hunt_bonus_count}": f"{int(data.get('hunt_bonus_count') or 0):,}",
            "{hunt_remaining}": f"{int(data.get('hunt_remaining') or 0):,}",
            "{hunt_total_payout}": self._format_amount(data.get("hunt_total_payout")),
            "{hunt_profit}": self._format_amount(data.get("hunt_profit")),
        }
        return self._replace_tokens(text, {**basic_replacements, **database_replacements})

    async def _give_points(self, give_match, command, args, platform, recent_names):
        """Run a {give_points} grant: ``!cmd @viewer [amount]``.

        The target is the first word after the command. The amount is fixed by
        ``{give_points:N}``, otherwise it's the second word. Returns
        ``(target, amount, new_balance)``, or a message for the mod when the
        command was used wrong or the viewer isn't known.
        """
        fixed = give_match.group(1)
        words = args.split()
        usage = f"usage: !{command} @viewer" + ("" if fixed else " amount")
        if not words:
            return usage
        amount = parse_positive_int(fixed if fixed else (words[1] if len(words) > 1 else None), MAX_GRANT)
        if amount is None:
            return usage if not fixed else "this command's point amount is invalid."
        if self.engine is None or not self.discord_server_id:
            logger.warning("{give_points} used without an engine or server")
            return "points can't be given right now."

        try:
            balance = await asyncio.to_thread(
                give_points_to_chat_name,
                self.engine,
                self.discord_server_id,
                words[0],
                amount,
                platform,
                list(recent_names),
            )
        except Exception:
            logger.exception(f"{{give_points}} grant failed in !{command}")
            return "points couldn't be given, try again."
        if balance is None:
            return f"couldn't find a viewer named {words[0].lstrip('@')}."
        return words[0].lstrip("@"), amount, balance

    @staticmethod
    def _replace_tokens(text, replacements):
        """Replace template tokens once, without expanding tokens inside values.

        In particular, viewer-supplied {args} text must not be able to inject a
        second response variable and trigger data substitution.
        """
        sentinels = {}
        for index, (token, value) in enumerate(replacements.items()):
            sentinel = f"\0custom_command_variable_{index}\0"
            if token in text:
                text = text.replace(token, sentinel)
                sentinels[sentinel] = "" if value is None else str(value)
        for sentinel, value in sentinels.items():
            text = text.replace(sentinel, value)
        return text

    @staticmethod
    def _format_rank(rank):
        return f"#{int(rank)}" if rank else "unranked"

    @staticmethod
    def _format_amount(amount):
        return f"{float(amount or 0):,.2f}"

    @staticmethod
    def _format_time_remaining(end_date):
        if not end_date:
            return "N/A"
        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            now = now.replace(tzinfo=None)
        seconds = int((end_date - now).total_seconds())
        if seconds <= 0:
            return "ended"
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes = remainder // 60
        if days:
            return f"{days}d {hours}h"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m" if minutes else "<1m"

    def _fetch_variable_context(self, server_id, username, display_name, platform, requested_tokens):
        """Load all database-backed variables with one tenant-scoped query."""
        points_value_tokens = {"{points}", "{points_earned}", "{points_spent}"}
        watchtime_value_tokens = {"{watchtime}", "{watchtime_minutes}", "{watchtime_formatted}"}
        raffle_viewer_tokens = {
            "{tickets}",
            "{watchtime_tickets}",
            "{gifted_sub_tickets}",
            "{wager_tickets}",
            "{bonus_tickets}",
            "{raffle_rank}",
            "{raffle_chance}",
        }
        raffle_stat_tokens = {"{raffle_chance}", "{raffle_total}", "{raffle_participants}"}
        slot_queue_tokens = {
            "{slot_queue_count}",
            "{user_slot_request_count}",
            "{user_latest_slot_request}",
        }
        giveaway_tokens = {
            "{giveaway_title}",
            "{giveaway_method}",
            "{giveaway_keyword}",
            "{giveaway_entries}",
            "{giveaway_participants}",
            "{giveaway_entered}",
        }
        hunt_tokens = {
            "{hunt_start_balance}",
            "{hunt_bonus_count}",
            "{hunt_remaining}",
            "{hunt_total_payout}",
            "{hunt_profit}",
        }
        identity_tokens = (
            points_value_tokens
            | watchtime_value_tokens
            | raffle_viewer_tokens
            | {
                "{points_rank}",
                "{watchtime_rank}",
                "{user_slot_request_count}",
                "{user_latest_slot_request}",
                "{giveaway_entered}",
            }
        )

        conn = psycopg2.connect(self.database_url)
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                WITH matched_link AS (
                    SELECT discord_id
                    FROM links
                    WHERE discord_server_id = %(server_id)s
                      AND platform = %(platform)s
                      AND LOWER(kick_name) = LOWER(%(display_name)s)
                      AND %(need_identity)s
                    LIMIT 1
                ),
                identity AS (
                    SELECT
                        (SELECT discord_id FROM matched_link) AS discord_id,
                        COALESCE(
                            (
                                SELECT MIN(LOWER(l.kick_name))
                                FROM links l
                                WHERE l.discord_server_id = %(server_id)s
                                  AND l.discord_id = (SELECT discord_id FROM matched_link)
                                  AND l.kick_name IS NOT NULL
                            ),
                            LOWER(%(username)s)
                        ) AS canonical_username
                ),
                active_period AS (
                    SELECT id, end_date
                    FROM raffle_periods
                    WHERE discord_server_id = %(server_id)s AND status = 'active'
                      AND %(need_raffle_period)s
                    ORDER BY start_date DESC
                    LIMIT 1
                ),
                viewer_points AS (
                    SELECT points, total_earned, total_spent
                    FROM user_points
                    CROSS JOIN identity
                    WHERE discord_server_id = %(server_id)s
                      AND LOWER(kick_username) = identity.canonical_username
                      AND %(need_points_data)s
                    LIMIT 1
                ),
                point_ranked AS (
                    SELECT
                        LOWER(kick_username) AS username,
                        points,
                        RANK() OVER (ORDER BY points DESC) AS rank
                    FROM user_points
                    WHERE discord_server_id = %(server_id)s
                      AND points > 0
                      AND %(need_points_rank)s
                ),
                viewer_watchtime AS (
                    SELECT minutes
                    FROM watchtime
                    CROSS JOIN identity
                    WHERE discord_server_id = %(server_id)s
                      AND LOWER(username) = identity.canonical_username
                      AND %(need_watchtime_data)s
                    LIMIT 1
                ),
                watchtime_ranked AS (
                    SELECT
                        LOWER(username) AS username,
                        RANK() OVER (ORDER BY minutes DESC) AS rank
                    FROM watchtime
                    WHERE discord_server_id = %(server_id)s
                      AND minutes > 0
                      AND %(need_watchtime_rank)s
                ),
                raffle_entries AS (
                    SELECT
                        discord_id,
                        watchtime_tickets,
                        gifted_sub_tickets,
                        shuffle_wager_tickets,
                        bonus_tickets,
                        total_tickets
                    FROM raffle_tickets
                    WHERE discord_server_id = %(server_id)s
                      AND period_id = (SELECT id FROM active_period)
                      AND total_tickets > 0
                      AND %(need_raffle)s
                ),
                raffle_ranked AS (
                    SELECT
                        discord_id,
                        RANK() OVER (ORDER BY total_tickets DESC) AS rank
                    FROM raffle_entries
                    WHERE %(need_raffle_rank)s
                ),
                raffle_stats AS (
                    SELECT
                        COALESCE(SUM(total_tickets), 0) AS total_tickets,
                        COUNT(*) AS participants
                    FROM raffle_entries
                    WHERE %(need_raffle_stats)s
                ),
                latest_slot AS (
                    SELECT slot_name, provider
                    FROM current_slot_history
                    WHERE discord_server_id = %(server_id)s
                      AND %(need_slot)s
                    ORDER BY started_at DESC, id DESC
                    LIMIT 1
                ),
                slot_queue AS (
                    SELECT
                        slot_call,
                        LOWER(kick_username) AS username,
                        requested_at,
                        id
                    FROM slot_requests
                    WHERE discord_server_id = %(server_id)s
                      AND picked = FALSE
                      AND %(need_slot_queue)s
                ),
                active_giveaway AS (
                    SELECT id, title, entry_method, keyword
                    FROM giveaways
                    WHERE discord_server_id = %(server_id)s
                      AND status = 'active'
                      AND %(need_giveaway)s
                    ORDER BY started_at DESC NULLS LAST, id DESC
                    LIMIT 1
                ),
                giveaway_stats AS (
                    SELECT
                        COALESCE(SUM(entry_count), 0) AS total_entries,
                        COUNT(DISTINCT LOWER(kick_username)) AS participants
                    FROM giveaway_entries
                    WHERE discord_server_id = %(server_id)s
                      AND giveaway_id = (SELECT id FROM active_giveaway)
                      AND %(need_giveaway_stats)s
                ),
                active_hunt AS (
                    SELECT id, starting_balance
                    FROM bonus_hunt_sessions
                    WHERE discord_server_id = %(server_id)s
                      AND status = 'active'
                      AND %(need_hunt)s
                    ORDER BY started_at DESC NULLS LAST, id DESC
                    LIMIT 1
                ),
                hunt_stats AS (
                    SELECT
                        COUNT(*) AS bonus_count,
                        COUNT(*) FILTER (WHERE status IS DISTINCT FROM 'opened') AS remaining,
                        COALESCE(SUM(payout), 0) AS total_payout
                    FROM bonus_hunt_bonuses
                    WHERE discord_server_id = %(server_id)s
                      AND session_id = (SELECT id FROM active_hunt)
                      AND %(need_hunt)s
                )
                SELECT
                    COALESCE(
                        (
                            SELECT value
                            FROM bot_settings
                            WHERE discord_server_id = %(server_id)s
                              AND %(need_channel)s
                              AND key = CASE
                                  WHEN %(platform)s = 'twitch' THEN 'twitch_channel'
                                  ELSE 'kick_channel'
                              END
                            LIMIT 1
                        ),
                        'unknown'
                    ) AS channel,
                    COALESCE(
                        (
                            SELECT total_tickets
                            FROM raffle_entries
                            WHERE discord_id = identity.discord_id
                            LIMIT 1
                        ),
                        0
                    ) AS tickets,
                    COALESCE(
                        (
                            SELECT watchtime_tickets
                            FROM raffle_entries
                            WHERE discord_id = identity.discord_id
                            LIMIT 1
                        ),
                        0
                    ) AS watchtime_tickets,
                    COALESCE(
                        (
                            SELECT gifted_sub_tickets
                            FROM raffle_entries
                            WHERE discord_id = identity.discord_id
                            LIMIT 1
                        ),
                        0
                    ) AS gifted_sub_tickets,
                    COALESCE(
                        (
                            SELECT shuffle_wager_tickets
                            FROM raffle_entries
                            WHERE discord_id = identity.discord_id
                            LIMIT 1
                        ),
                        0
                    ) AS wager_tickets,
                    COALESCE(
                        (
                            SELECT bonus_tickets
                            FROM raffle_entries
                            WHERE discord_id = identity.discord_id
                            LIMIT 1
                        ),
                        0
                    ) AS bonus_tickets,
                    COALESCE((SELECT points FROM viewer_points), 0) AS points,
                    COALESCE((SELECT total_earned FROM viewer_points), 0) AS points_earned,
                    COALESCE((SELECT total_spent FROM viewer_points), 0) AS points_spent,
                    COALESCE((SELECT minutes FROM viewer_watchtime), 0) AS watchtime_minutes,
                    (SELECT slot_name FROM latest_slot) AS current_slot,
                    (SELECT provider FROM latest_slot) AS current_provider,
                    (
                        SELECT rank
                        FROM point_ranked
                        WHERE username = identity.canonical_username
                        LIMIT 1
                    ) AS points_rank,
                    (
                        SELECT rank
                        FROM watchtime_ranked
                        WHERE username = identity.canonical_username
                        LIMIT 1
                    ) AS watchtime_rank,
                    (
                        SELECT rank
                        FROM raffle_ranked
                        WHERE discord_id = identity.discord_id
                        LIMIT 1
                    ) AS raffle_rank,
                    (SELECT total_tickets FROM raffle_stats) AS raffle_pool_tickets,
                    (SELECT participants FROM raffle_stats) AS raffle_participants,
                    CASE
                        WHEN %(need_raffle_end)s THEN (SELECT end_date FROM active_period)
                        ELSE NULL
                    END AS raffle_end_date,
                    COALESCE(
                        (
                            SELECT LOWER(value) = 'true'
                            FROM bot_settings
                            WHERE discord_server_id = %(server_id)s
                              AND key = 'slot_requests_enabled'
                              AND %(need_slot_requests_open)s
                            LIMIT 1
                        ),
                        TRUE
                    ) AS slot_requests_open,
                    (SELECT COUNT(*) FROM slot_queue) AS slot_queue_count,
                    (
                        SELECT COUNT(*)
                        FROM slot_queue
                        WHERE username = identity.canonical_username
                    ) AS user_slot_request_count,
                    (
                        SELECT slot_call
                        FROM slot_queue
                        WHERE username = identity.canonical_username
                        ORDER BY requested_at DESC, id DESC
                        LIMIT 1
                    ) AS user_latest_slot_request,
                    (SELECT title FROM active_giveaway) AS giveaway_title,
                    (SELECT entry_method FROM active_giveaway) AS giveaway_method,
                    (SELECT keyword FROM active_giveaway) AS giveaway_keyword,
                    (SELECT total_entries FROM giveaway_stats) AS giveaway_entries,
                    (SELECT participants FROM giveaway_stats) AS giveaway_participants,
                    CASE
                        WHEN %(need_giveaway_entered)s THEN EXISTS (
                            SELECT 1
                            FROM giveaway_entries
                            WHERE discord_server_id = %(server_id)s
                              AND giveaway_id = (SELECT id FROM active_giveaway)
                              AND LOWER(kick_username) = identity.canonical_username
                        )
                        ELSE FALSE
                    END AS giveaway_entered,
                    COALESCE((SELECT starting_balance FROM active_hunt), 0) AS hunt_start_balance,
                    COALESCE((SELECT bonus_count FROM hunt_stats), 0) AS hunt_bonus_count,
                    COALESCE((SELECT remaining FROM hunt_stats), 0) AS hunt_remaining,
                    COALESCE((SELECT total_payout FROM hunt_stats), 0) AS hunt_total_payout,
                    COALESCE((SELECT total_payout FROM hunt_stats), 0)
                        - COALESCE((SELECT starting_balance FROM active_hunt), 0) AS hunt_profit
                FROM identity
                """,
                {
                    "server_id": server_id,
                    "username": username,
                    "display_name": display_name,
                    "platform": platform,
                    "need_identity": bool(identity_tokens.intersection(requested_tokens)),
                    "need_channel": "{channel}" in requested_tokens,
                    "need_points_data": bool(points_value_tokens.intersection(requested_tokens)),
                    "need_watchtime_data": bool(watchtime_value_tokens.intersection(requested_tokens)),
                    "need_points_rank": "{points_rank}" in requested_tokens,
                    "need_watchtime_rank": "{watchtime_rank}" in requested_tokens,
                    "need_raffle_period": bool(
                        raffle_viewer_tokens.intersection(requested_tokens)
                        or raffle_stat_tokens.intersection(requested_tokens)
                        or "{raffle_ends_in}" in requested_tokens
                    ),
                    "need_raffle": bool(
                        raffle_viewer_tokens.intersection(requested_tokens)
                        or raffle_stat_tokens.intersection(requested_tokens)
                    ),
                    "need_raffle_rank": "{raffle_rank}" in requested_tokens,
                    "need_raffle_stats": bool(raffle_stat_tokens.intersection(requested_tokens)),
                    "need_raffle_end": "{raffle_ends_in}" in requested_tokens,
                    "need_slot": bool({"{current_slot}", "{current_provider}"}.intersection(requested_tokens)),
                    "need_slot_requests_open": "{slot_requests_open}" in requested_tokens,
                    "need_slot_queue": bool(slot_queue_tokens.intersection(requested_tokens)),
                    "need_giveaway": bool(giveaway_tokens.intersection(requested_tokens)),
                    "need_giveaway_stats": bool(
                        {"{giveaway_entries}", "{giveaway_participants}"}.intersection(requested_tokens)
                    ),
                    "need_giveaway_entered": "{giveaway_entered}" in requested_tokens,
                    "need_hunt": bool(hunt_tokens.intersection(requested_tokens)),
                },
            )
            row = cursor.fetchone()
            if not row:
                return {}
            columns = [description[0] for description in cursor.description]
            return dict(zip(columns, row))
        finally:
            cursor.close()
            conn.close()

    async def _increment_use_count(self, command_id):
        """Increment use count in database"""
        try:
            await asyncio.to_thread(self._increment_use_count_db, command_id)
        except Exception as e:
            logger.warning(f"⚠️ Failed to increment use count: {e}")

    def _increment_use_count_db(self, command_id):
        """Increment use count in database (blocking)"""
        conn = psycopg2.connect(self.database_url)
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE custom_commands
            SET use_count = use_count + 1
            WHERE id = %s
        """,
            (command_id,),
        )

        conn.commit()
        cursor.close()
        conn.close()

    def get_all_commands(self):
        """Get list of all enabled commands"""
        return [cmd for cmd, data in self.commands.items() if data["enabled"]]

    async def start(self):
        """Start the custom commands manager"""
        await self.load_commands()
        logger.debug("🎮 Custom Commands Manager started")
