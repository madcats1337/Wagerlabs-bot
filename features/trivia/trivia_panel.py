"""Discord-hosted trivia panel (Components V2).

One message per trivia event, posted to the channel chosen on the dashboard.
It shows the question, the preparation timer, and the time remaining until the
event ends - and re-renders itself as the event moves between its phases.

Countdown model (the part that matters). Both timers render as Discord relative
timestamps, `<t:...:R>`, which tick CLIENT-side. The bot therefore does NOT
edit the message once per second - it edits only when the panel's TEXT actually
has to change:

  * created -> started        (dashboard action)
  * preparation -> answering  (scheduled edit at `answers_open_at`)
  * answering -> expired      (scheduled edit at `ends_at`)
  * paused / resumed / ended  (dashboard action)
  * a winner is detected      (the answer listener)

A per-second edit loop would hit Discord's per-channel rate limit within a
minute and gain nothing, since the client is already animating the countdown.
The scheduled edits are owned by `TriviaTicker` below.

There are no buttons on this panel: trivia is answered by posting in the
channel, so the panel is pure display and needs no persistent view registration.

Rendered as a LayoutView only - a Components V2 message cannot carry an embed
or top-level content.
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from sqlalchemy import text

logger = logging.getLogger(__name__)

ACCENT = 0xFACC15  # Wagerlabs yellow

# Phase-coloured accents so the panel's state reads at a glance in a busy
# channel: neutral while waiting, yellow while answers are open, red once over.
ACCENT_WAITING = 0x6B7280
ACCENT_LIVE = ACCENT
ACCENT_ENDED = 0xEF4444
ACCENT_WON = 0x22C55E

SELECT_COLUMNS = """
    id, discord_server_id, discord_channel_id, discord_message_id,
    question, answer, prep_seconds, duration_seconds, status,
    started_at, answers_open_at, ends_at, paused_at,
    prep_remaining_seconds, duration_remaining_seconds,
    winner_discord_id, winner_name, winner_answer, won_at,
    created_by, created_at, ended_at
"""


def _now():
    return datetime.now(timezone.utc)


def _aware(value):
    """A timestamp column as tz-aware UTC, whatever the driver handed back."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _relative(value) -> str:
    """Discord relative timestamp, e.g. `<t:1699999999:R>` -> "in 2 minutes".

    Ticked client-side by every viewer's Discord, which is why the panel needs
    no fast edit loop.
    """
    value = _aware(value)
    if not value:
        return ""
    return f"<t:{int(value.timestamp())}:R>"


def _duration(seconds) -> str:
    """A frozen duration as "2m 30s" - used where a live countdown cannot be.

    A paused event has no deadline to count down to, and an event that has not
    started yet has only its configured lengths, so those states show a plain
    duration instead of a `<t:...:R>`.
    """
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "-"
    minutes, secs = divmod(seconds, 60)
    if minutes and secs:
        return f"{minutes}m {secs}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def phase_of(event) -> str:
    """The event's phase, derived the same way the dashboard derives it.

    Kept identical to `routes/trivia.py::_serialize` on purpose - the panel and
    the dashboard must never disagree about whether answers are open.
    """
    status = event.get("status")
    if status == "created":
        return "idle"
    if status == "paused":
        return "paused"
    if status != "started":
        return "ended"

    now = _now()
    ends_at = _aware(event.get("ends_at"))
    answers_open_at = _aware(event.get("answers_open_at"))
    if ends_at and now >= ends_at:
        return "ended"
    if answers_open_at and now < answers_open_at:
        return "prep"
    return "live"


def answers_are_open(event) -> bool:
    """Whether a message posted right now could win.

    The single gate the answer listener trusts. Answers during the preparation
    timer do not count (a deliberate product rule), and a paused event accepts
    nothing at all.
    """
    return phase_of(event) == "live" and not event.get("winner_discord_id")


def fetch_event(engine, event_id, guild_id=None):
    """One event, read FRESH. Callers must never cache a row across an await."""
    params = {"eid": event_id}
    clause = "id = :eid"
    if guild_id is not None:
        clause += " AND discord_server_id = :sid"
        params["sid"] = int(guild_id)
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT {SELECT_COLUMNS} FROM trivia_events WHERE {clause}"), params).fetchone()
    return dict(row._mapping) if row else None


def fetch_live_events(engine, guild_id=None):
    """Every event still occupying a channel - used to re-arm after a restart."""
    params = {}
    clause = "status IN ('created', 'started', 'paused')"
    if guild_id is not None:
        clause += " AND discord_server_id = :sid"
        params["sid"] = int(guild_id)
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT {SELECT_COLUMNS} FROM trivia_events WHERE {clause}"), params).fetchall()
    return [dict(row._mapping) for row in rows]


class TriviaPanelView(discord.ui.LayoutView):
    """The trivia panel: question, preparation timer, remaining time, outcome.

    Built fresh from a row on every render - there is no state on the instance,
    so a panel edited after a bot restart looks exactly like one edited before.
    """

    def __init__(self, event):
        super().__init__(timeout=None)

        phase = phase_of(event)
        winner_id = event.get("winner_discord_id")

        if winner_id:
            accent = ACCENT_WON
        elif phase == "ended":
            accent = ACCENT_ENDED
        elif phase == "live":
            accent = ACCENT_LIVE
        else:
            accent = ACCENT_WAITING

        container = discord.ui.Container(accent_colour=accent)

        # No decorative emojis - anything a viewer sees comes from the question
        # the operator typed.
        lines = ["# Trivia", event.get("question") or ""]
        container.add_item(discord.ui.TextDisplay("\n".join(line for line in lines if line)))
        container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))

        container.add_item(discord.ui.TextDisplay(self._state_block(event, phase)))

        footer = self._footer(event)
        if footer:
            container.add_item(discord.ui.Separator(spacing=discord.SeparatorSpacing.small))
            container.add_item(discord.ui.TextDisplay(f"-# {footer}"))

        self.add_item(container)

    @staticmethod
    def _state_block(event, phase) -> str:
        """Both timers plus the one-line instruction for the current phase."""
        winner_id = event.get("winner_discord_id")

        if winner_id:
            answer = event.get("answer") or ""
            return f"**Winner** <@{int(winner_id)}>\n" f"Correct answer: **{answer}**\n\n" "Answers are closed."

        if phase == "idle":
            return (
                f"**Starts in** {_duration(event.get('prep_seconds'))}  ·  "
                f"**Answering window** {_duration(event.get('duration_seconds'))}\n\n"
                "Waiting to start. Answers posted before the timer opens do not count."
            )

        if phase == "prep":
            return (
                f"**Answers open** {_relative(event.get('answers_open_at'))}  ·  "
                f"**Ends** {_relative(event.get('ends_at'))}\n\n"
                "Get ready. Answers posted before the window opens do not count."
            )

        if phase == "live":
            return (
                f"**Answers are open**  ·  **Ends** {_relative(event.get('ends_at'))}\n\n"
                "Post your answer in this channel. First correct answer wins."
            )

        if phase == "paused":
            # Frozen: no deadline exists to count down to, so both timers show
            # what was banked at the pause.
            prep_left = event.get("prep_remaining_seconds")
            duration_left = event.get("duration_remaining_seconds")
            opening_label = f"**Opens after** {_duration(prep_left)}" if prep_left else "**Answering window** was open"
            return (
                f"**Paused**\n"
                f"{opening_label}  ·  **Time banked** {_duration(duration_left)}\n\n"
                "Answers are not counted while the event is paused."
            )

        return "**Ended**\nNobody answered correctly in time."

    @staticmethod
    def _footer(event) -> str:
        """Absolute stamps, rendered in each viewer's own timezone."""
        parts = []
        started = _aware(event.get("started_at"))
        if started:
            parts.append(f"Started <t:{int(started.timestamp())}:f>")
        ended = _aware(event.get("ended_at"))
        if ended:
            parts.append(f"Ended <t:{int(ended.timestamp())}:f>")
        return "  ·  ".join(parts)


async def _resolve_channel(bot, channel_id):
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except Exception as e:
            logger.warning(f"[trivia] channel {channel_id} unavailable: {e}")
            return None
    return channel


async def _fetch_panel_message(bot, event):
    channel_id = event.get("discord_channel_id")
    message_id = event.get("discord_message_id")
    if not channel_id or not message_id:
        return None
    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        return None
    try:
        return await channel.fetch_message(int(message_id))
    except discord.NotFound:
        logger.info(f"[trivia] panel message {message_id} is gone; not re-posting")
        return None
    except Exception as e:
        logger.warning(f"[trivia] could not fetch panel message {message_id}: {e}")
        return None


async def post_panel(bot, engine, event_id, guild_id=None):
    """Post the panel for a newly created event and record its message id.

    Idempotent: an event that already has a live panel message is refreshed
    instead of posted twice, so a re-delivered Redis event cannot litter the
    channel with duplicate panels.
    """
    event = fetch_event(engine, event_id, guild_id)
    if not event:
        logger.warning(f"[trivia] event {event_id} not found; nothing to post")
        return False

    if event.get("discord_message_id"):
        existing = await _fetch_panel_message(bot, event)
        if existing is not None:
            await existing.edit(view=TriviaPanelView(event))
            return True

    channel = await _resolve_channel(bot, event["discord_channel_id"])
    if channel is None:
        return False

    message = await channel.send(view=TriviaPanelView(event))

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE trivia_events SET discord_message_id = :mid WHERE id = :eid"),
            {"mid": message.id, "eid": event_id},
        )
    logger.info(f"[trivia] posted panel for event {event_id} in #{channel} ({message.id})")
    return True


async def refresh_panel(bot, engine, event_id, guild_id=None, event=None):
    """Re-render the panel in place from current DB state.

    `event` may be passed when the caller has just written the row and knows it
    is current; otherwise the row is re-read, which is what makes a dropped
    Redis event self-correcting.
    """
    event = event or fetch_event(engine, event_id, guild_id)
    if not event:
        return False

    message = await _fetch_panel_message(bot, event)
    if message is None:
        # The panel was never posted (created while the bot was down) - post it
        # now rather than leaving the channel with no panel at all.
        if not event.get("discord_message_id"):
            return await post_panel(bot, engine, event_id, guild_id)
        return False

    await message.edit(view=TriviaPanelView(event))
    return True


async def announce_winner(bot, engine, event):
    """Announce and tag the winner in the event's own channel.

    Posted as a normal message, not an edit, so it pings the winner and shows
    up in the channel's flow - the panel above it carries the same result but a
    silent edit is easy to miss.
    """
    channel = await _resolve_channel(bot, event["discord_channel_id"])
    if channel is None:
        return False

    winner_id = event.get("winner_discord_id")
    answer = event.get("answer") or ""
    mention = f"<@{int(winner_id)}>" if winner_id else f"**{event.get('winner_name') or 'Unknown'}**"

    # The bot's global allowed_mentions already blocks @everyone/@here and role
    # pings while permitting user mentions, so the winner is tagged for real.
    await channel.send(f"{mention} got it first. The answer was **{answer}**.")
    logger.info(f"[trivia] announced winner {winner_id} for event {event['id']}")
    return True


# ---------------------------------------------------------------------------
# Scheduled panel edits
# ---------------------------------------------------------------------------
class TriviaTicker:
    """Schedules the panel's phase-transition edits, one task per event.

    Not a countdown loop: the task sleeps until the next moment the panel's
    text changes (`answers_open_at`, then `ends_at`), edits once, and sleeps
    again. Every dashboard action cancels and re-arms the event's task, so a
    pause genuinely stops the clock and a resume re-derives it from the row.

    One instance lives on the bot (`bot.trivia_ticker`). It also owns the
    open-channel registry the answer listener reads, so that registry can never
    drift from what has actually been scheduled.
    """

    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine
        self._tasks: dict[int, asyncio.Task] = {}
        # (guild_id, channel_id) -> event_id, for channels that currently hold a
        # RUNNING event. The listener's hot path is a dict lookup against this,
        # so a channel with no trivia costs nothing per message.
        self.open_channels: dict[tuple[int, int], int] = {}

    # -- registry ----------------------------------------------------------
    def _register(self, event):
        key = (int(event["discord_server_id"]), int(event["discord_channel_id"]))
        self.open_channels[key] = event["id"]

    def _unregister(self, event):
        key = (int(event["discord_server_id"]), int(event["discord_channel_id"]))
        if self.open_channels.get(key) == event["id"]:
            self.open_channels.pop(key, None)

    def event_id_for(self, guild_id, channel_id):
        return self.open_channels.get((int(guild_id), int(channel_id)))

    # -- scheduling --------------------------------------------------------
    def cancel(self, event_id):
        task = self._tasks.pop(event_id, None)
        if task and not task.done():
            task.cancel()

    def release(self, event):
        """Stop an event's schedule AND close its channel to further answers.

        What every terminal transition (won, ended, paused) needs: leaving the
        channel registered would keep the listener hitting the DB for messages
        that can no longer win.
        """
        self.cancel(event["id"])
        self._unregister(event)

    def arm(self, event):
        """(Re-)schedule an event's transition edits from its current row.

        Only a 'started' event has deadlines to schedule; every other status
        drops the event out of the registry so no message can be matched
        against it.
        """
        event_id = event["id"]
        self.cancel(event_id)

        if event.get("status") != "started" or event.get("winner_discord_id"):
            self._unregister(event)
            return

        self._register(event)
        self._tasks[event_id] = asyncio.create_task(self._run(event_id, int(event["discord_server_id"])))

    async def _run(self, event_id, guild_id):
        """Sleep to each transition, re-reading the row before acting on it.

        Re-reading is what keeps this correct across a dashboard action that
        raced the sleep: if the event was paused, ended or won while this task
        was waiting, the row says so and the task exits without touching the
        panel.
        """
        try:
            while True:
                event = fetch_event(self.engine, event_id, guild_id)
                if not event or event.get("status") != "started" or event.get("winner_discord_id"):
                    if event:
                        self._unregister(event)
                    return

                now = _now()
                answers_open_at = _aware(event.get("answers_open_at"))
                ends_at = _aware(event.get("ends_at"))

                if answers_open_at and now < answers_open_at:
                    # Still in preparation: wake when answers open.
                    await asyncio.sleep(max(0.0, (answers_open_at - now).total_seconds()))
                    refreshed = fetch_event(self.engine, event_id, guild_id)
                    if not refreshed or refreshed.get("status") != "started":
                        return
                    self._register(refreshed)
                    await refresh_panel(self.bot, self.engine, event_id, guild_id, event=refreshed)
                    continue

                if ends_at and now < ends_at:
                    # Answering window is open: wake when it closes.
                    await asyncio.sleep(max(0.0, (ends_at - now).total_seconds()))
                    await self._expire(event_id, guild_id)
                    return

                # Deadline already behind us (a restart mid-window, say).
                await self._expire(event_id, guild_id)
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[trivia] ticker for event {event_id} stopped: {e}", exc_info=True)
        finally:
            # A task that finished on its own (expiry, or a state change it read
            # on wake) is still referenced by `_tasks` — over a long-lived
            # process that is one dead Task per event, forever. `cancel()` only
            # clears the entry when something cancels it, so drop it here.
            # Guarded on identity so a re-arm that already replaced the entry
            # does not lose the live task.
            if self._tasks.get(event_id) is asyncio.current_task():
                self._tasks.pop(event_id, None)

    async def _expire(self, event_id, guild_id):
        """Close an event whose window ran out with nobody correct.

        The operator's answer to "what happens on expiry" was that it will
        rarely happen - so this does the quiet, obvious thing: mark the event
        ended and say so on the panel. No announcement is posted, because there
        is nothing to announce.

        The UPDATE is guarded on `status = 'started'` and no winner, so a win or
        a dashboard END that landed during the final sleep is never overwritten.
        """
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"""
                    UPDATE trivia_events
                    SET status = 'ended', ended_at = NOW(), answers_open_at = NULL, ends_at = NULL
                    WHERE id = :eid AND status = 'started' AND winner_discord_id IS NULL
                    RETURNING {SELECT_COLUMNS}
                    """
                ),
                {"eid": event_id},
            ).fetchone()

        if not row:
            # Someone won, or the operator ended it first - whoever did that
            # already refreshed the panel.
            stale = fetch_event(self.engine, event_id, guild_id)
            if stale:
                self._unregister(stale)
            return

        event = dict(row._mapping)
        self._unregister(event)
        logger.info(f"[trivia] event {event_id} expired with no correct answer")
        await refresh_panel(self.bot, self.engine, event_id, guild_id, event=event)

    # -- lifecycle ---------------------------------------------------------
    async def resume_all(self):
        """Re-arm every live event after a restart.

        Deadlines are absolute, so an event whose window elapsed while the bot
        was down expires immediately on the first pass rather than silently
        staying answerable.
        """
        try:
            events = fetch_live_events(self.engine)
        except Exception as e:
            logger.warning(f"[trivia] could not load live events: {e}")
            return 0

        armed = 0
        for event in events:
            try:
                self.arm(event)
                if event.get("status") == "started":
                    armed += 1

                # An event created or started while the bot was down has a row
                # but no panel — the Redis event announcing it was published to
                # nobody. Post it now, otherwise the operator is looking at a
                # dashboard that says "live" over a channel with nothing in it.
                if not event.get("discord_message_id"):
                    await post_panel(self.bot, self.engine, event["id"], int(event["discord_server_id"]))
                else:
                    # Re-render: the panel may be showing a phase the clock has
                    # since moved past.
                    await refresh_panel(
                        self.bot, self.engine, event["id"], int(event["discord_server_id"]), event=event
                    )
            except Exception as e:
                logger.warning(f"[trivia] re-arming event {event.get('id')} failed: {e}")
        if armed:
            logger.info(f"[trivia] re-armed {armed} running trivia event(s)")
        return armed
