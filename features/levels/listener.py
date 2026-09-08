"""Message-based XP: award 15-25 XP for a minute in which a member sent at
least one message, rate limited to once per minute per user.

A Cog listener (`@commands.Cog.listener()`), never a bare `@bot.event
on_message` — see features/trivia/trivia_listener.py's docstring for why a
bare handler would REPLACE the default handler and kill every `!` prefix
command. The cooldown gate is a plain in-memory dict checked before any DB
access, the same "cheap lookup before DB" shape the trivia listener uses.
"""

import logging
import random
import time

from discord.ext import commands

from .engine import award_xp

logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 60
MIN_XP = 15
MAX_XP = 25

# Above this many tracked members, drop the entries whose cooldown has already
# expired. Without it the dict is append-only across every member of every
# guild for the lifetime of the process.
_PRUNE_THRESHOLD = 5000


class MessageXPCog(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine
        # In-memory only, not persisted — a bot restart resets the cooldown for
        # whoever is mid-window, worth at most one early award. Keyed by
        # (guild_id, user_id) -> monotonic timestamp of the last award.
        self._last_award = {}

    def _prune(self, now: float) -> None:
        """Drop expired cooldowns; entries past COOLDOWN_SECONDS can't gate anything."""
        expired = [key for key, ts in self._last_award.items() if now - ts >= COOLDOWN_SECONDS]
        for key in expired:
            del self._last_award[key]

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        last = self._last_award.get(key)
        if last is not None and now - last < COOLDOWN_SECONDS:
            return

        if len(self._last_award) >= _PRUNE_THRESHOLD:
            self._prune(now)

        self._last_award[key] = now

        amount = random.randint(MIN_XP, MAX_XP)
        try:
            await award_xp(
                self.engine,
                self.bot,
                message.guild.id,
                message.author.id,
                amount,
                source="message",
                username=message.author.display_name,
            )
        except Exception as e:
            logger.error(f"[levels] message XP award failed for {message.author.id}: {e}")


async def setup_message_xp_listener(bot, engine):
    await bot.add_cog(MessageXPCog(bot, engine))
    logger.info("[levels] message XP listener registered")
