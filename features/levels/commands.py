"""The /rank command — replies with the caller's (or a mentioned member's)
rank card: avatar, XP progress bar, current/needed XP, leaderboard position,
rank + level.
"""

import logging

import discord
from discord.ext import commands
from sqlalchemy import text

from features.discord_app_commands import defer_slash_response
from utils.log_context import set_server

from .cards import render_rank_card

logger = logging.getLogger(__name__)


class RankCommands(commands.Cog):
    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine

    async def cog_before_invoke(self, ctx):
        if ctx.guild:
            set_server(ctx.guild.id, ctx.guild.name)
        await defer_slash_response(ctx)

    @commands.hybrid_command(name="rank", description="Show your (or another member's) rank card.")
    async def rank(self, ctx, member: discord.Member = None):
        if not ctx.guild:
            await ctx.send("This command only works in a server.")
            return

        target = member or ctx.author

        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT total_xp, avatar_url, username
                    FROM user_levels
                    WHERE discord_server_id = :guild_id AND discord_id = :discord_id
                    """
                ),
                {"guild_id": ctx.guild.id, "discord_id": target.id},
            ).fetchone()

            if row is None:
                await ctx.send(f"{target.display_name} hasn't earned any XP yet.")
                return

            total_xp, avatar_url, username = row

            position = conn.execute(
                text(
                    """
                    SELECT COUNT(*) + 1 FROM user_levels
                    WHERE discord_server_id = :guild_id AND total_xp > :xp
                    """
                ),
                {"guild_id": ctx.guild.id, "xp": total_xp},
            ).scalar()

        if target.display_avatar:
            avatar_url = str(target.display_avatar.url)

        file = await render_rank_card(username or target.display_name, avatar_url, total_xp, position)
        await ctx.send(file=file)


async def setup(bot, engine):
    await bot.add_cog(RankCommands(bot, engine))
    logger.info("[levels] /rank command registered")
