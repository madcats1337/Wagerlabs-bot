"""First-correct-answer detection for Discord trivia events.

A Cog listener, deliberately - `@commands.Cog.listener()` is ADDITIVE, while a
bare `@bot.event on_message` would REPLACE the default handler and silently
kill every `!` prefix command in the bot (linking, gtb, slot calls). Nothing
here needs to touch command dispatch, so it must not sit in that path.

Hot path. `on_message` fires for every message in every guild the bot can see,
so the first thing it does is a dict lookup against the ticker's registry of
channels that currently hold a RUNNING event. A channel with no trivia costs
one tuple hash and returns; only a message in an armed channel reaches the DB.

Winner selection is decided by the database, not by this process. The claiming
UPDATE is guarded on `winner_discord_id IS NULL` and on the answering window,
so of two members who answer in the same instant exactly one UPDATE returns a
row - the other sees zero rows and does nothing. That is what makes "first
correct answer" true rather than approximately true.
"""

import logging

from discord.ext import commands
from sqlalchemy import text

from .trivia_panel import SELECT_COLUMNS, announce_winner, answers_are_open, fetch_event, refresh_panel

logger = logging.getLogger(__name__)

# Discord caps a message at 2000 characters; an answer is capped at 200 by the
# dashboard, so anything longer cannot be a match and is dropped before the
# string comparison.
MAX_ANSWER_LEN = 200


def normalize(value: str) -> str:
    """The comparison form of an answer: trimmed and case-folded.

    Case-insensitive matching is the product rule. `casefold()` rather than
    `lower()` so non-ASCII answers compare the way a reader would expect.
    Surrounding whitespace is stripped because it is invisible in Discord and
    rejecting "paris " would read as a bug; nothing else is normalized - no
    fuzzy or partial matching, so a wrong answer stays wrong.
    """
    return (value or "").strip().casefold()


class TriviaAnswerListener(commands.Cog):
    """Watches configured channels for the first correct trivia answer."""

    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine

    @commands.Cog.listener()
    async def on_message(self, message):
        # Bots (including this one - the winner announcement itself) never play.
        if message.author.bot or not message.guild:
            return

        ticker = getattr(self.bot, "trivia_ticker", None)
        if ticker is None:
            return

        event_id = ticker.event_id_for(message.guild.id, message.channel.id)
        if event_id is None:
            return

        content = message.content or ""
        if not content.strip() or len(content) > MAX_ANSWER_LEN:
            return

        try:
            await self._check_answer(message, event_id)
        except Exception as e:
            logger.error(f"[trivia] answer check failed for event {event_id}: {e}", exc_info=True)

    async def _check_answer(self, message, event_id):
        guild_id = message.guild.id

        # Read the row FRESH. The registry only says "this channel had a running
        # event"; whether answers are open right now is the row's call.
        event = fetch_event(self.engine, event_id, guild_id)
        if not event or not answers_are_open(event):
            return

        if normalize(message.content) != normalize(event.get("answer")):
            return

        winner = self._claim(event_id, message.author, message.content)
        if not winner:
            # Someone beat this message to it by milliseconds, or the event
            # closed between the read and the write. Either way there is
            # nothing to announce.
            return

        ticker = getattr(self.bot, "trivia_ticker", None)
        if ticker is not None:
            # Stop the expiry task and close the channel to further answers
            # before anything awaits, so a flood of correct answers arriving in
            # the same tick cannot queue up behind the announcement.
            ticker.release(winner)

        logger.info(
            f"[trivia] event {event_id} won by {message.author} ({message.author.id}) " f"in #{message.channel}"
        )

        await refresh_panel(self.bot, self.engine, event_id, guild_id, event=winner)
        await announce_winner(self.bot, self.engine, winner)

    def _claim(self, event_id, author, content):
        """Atomically record the winner, or return None if someone else won.

        The window is re-checked in SQL (`answers_open_at <= NOW() < ends_at`)
        so the DATABASE clock decides whether the answer landed in time - the
        bot's clock is never the authority on a race it is trying to settle.
        A winning answer ends the event: this is single-question trivia, so
        there is nothing left to play for.

        `winner_answer` stores what the member actually typed, not the
        configured answer, so the operator can see the exact winning message
        (casing and all) on the dashboard afterwards.
        """
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    f"""
                    UPDATE trivia_events
                    SET winner_discord_id = :did,
                        winner_name = :name,
                        winner_answer = :answer,
                        won_at = NOW(),
                        status = 'ended',
                        ended_at = NOW()
                    WHERE id = :eid
                      AND status = 'started'
                      AND winner_discord_id IS NULL
                      AND answers_open_at IS NOT NULL AND answers_open_at <= NOW()
                      AND ends_at IS NOT NULL AND ends_at > NOW()
                    RETURNING {SELECT_COLUMNS}
                    """
                ),
                {
                    "did": int(author.id),
                    "name": str(author),
                    "answer": (content or "").strip()[:MAX_ANSWER_LEN],
                    "eid": event_id,
                },
            ).fetchone()
        return dict(row._mapping) if row else None


async def setup_trivia_listener(bot, engine):
    """Register the answer listener once, globally (it resolves guild per message)."""
    await bot.add_cog(TriviaAnswerListener(bot, engine))
    logger.info("[trivia] answer listener registered")
