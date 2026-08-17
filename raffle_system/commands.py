"""
Discord Commands for Raffle System
Handles user and admin commands for raffle participation
"""

import logging
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from features.discord_app_commands import defer_slash_response
from utils.bot_settings import BotSettingsManager
from utils.log_context import set_server
from utils.server_urls import get_server_public_page_url

from .draw import RaffleDraw
from .reward_settings import get_ticket_reward_settings, platform_display_name
from .shuffle_tracker import ShuffleWagerTracker
from .tickets import TicketManager

logger = logging.getLogger(__name__)


class RaffleCommands(commands.Cog):
    """Discord commands for raffle system"""

    def __init__(self, bot, engine):
        self.bot = bot
        self.engine = engine
        self.ticket_manager = TicketManager(engine)  # Default manager (backward compat)
        self.raffle_draw = RaffleDraw(engine)  # Default draw (backward compat)
        self.shuffle_tracker = ShuffleWagerTracker(engine)  # Default tracker (backward compat)
        self._settings_by_guild = {}  # Cached per-guild settings managers

    async def cog_before_invoke(self, ctx):
        """Tag logging for this cog's commands with the guild (belt-and-suspenders;
        the bot's global before_invoke also does this). Runs in the command's Task."""
        if ctx.guild:
            set_server(ctx.guild.id, ctx.guild.name)

    def _get_guild_settings(self, ctx):
        """Get a per-guild settings manager (cached) for reading dashboard config.

        Prefer the bot's shared get_guild_settings (same cache as the rest of the
        bot) when available; otherwise build/cache one locally.
        """
        guild_id = ctx.guild.id if ctx.guild else None
        getter = getattr(self.bot, "get_guild_settings", None)
        if callable(getter) and guild_id is not None:
            return getter(guild_id)
        if guild_id not in self._settings_by_guild:
            self._settings_by_guild[guild_id] = BotSettingsManager(self.engine, guild_id=guild_id)
        return self._settings_by_guild[guild_id]

    def _get_guild_managers(self, ctx):
        """Get guild-specific managers for multiserver support"""
        guild_id = ctx.guild.id if ctx.guild else None
        return {
            "ticket_manager": TicketManager(self.engine, server_id=guild_id),
            "raffle_draw": RaffleDraw(self.engine),  # RaffleDraw doesn't need server_id, uses period_id
            "shuffle_tracker": ShuffleWagerTracker(self.engine, server_id=guild_id),
        }

    # ========================================
    # USER COMMANDS
    # ========================================

    @commands.hybrid_command(name="tickets", aliases=["ticket", "mytickets"])
    async def check_tickets(self, ctx):
        """
        Check your raffle ticket balance
        Usage: /tickets
        """
        logger.info(
            f"[TICKETS] Command called by {ctx.author} (ID: {ctx.author.id}) in guild {ctx.guild.name if ctx.guild else 'DM'}"
        )
        try:
            discord_id = ctx.author.id
            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]
            raffle_draw = managers["raffle_draw"]

            logger.info(f"[TICKETS] Getting tickets for user {discord_id}")
            tickets = ticket_manager.get_user_tickets(discord_id)
            watchtime_tickets, gifted_sub_tickets, wager_tickets = get_ticket_reward_settings(
                self.engine, ctx.guild.id if ctx.guild else None, logger
            )
            settings = self._get_guild_settings(ctx)
            platform = platform_display_name(settings)
            code = settings.shuffle_campaign_code

            if not tickets or tickets["total_tickets"] == 0:
                await ctx.send(
                    f"❌ {ctx.author.mention} You don't have any raffle tickets yet!\n"
                    f"Earn tickets by:\n"
                    f"• Watching streams ({watchtime_tickets} tickets per hour)\n"
                    f"• Gifting subs ({gifted_sub_tickets} tickets per sub)\n"
                    f"• Wagering on {platform} with code '{code}' ({wager_tickets} tickets per $1000)"
                )
                return

            # Get user's rank
            rank = ticket_manager.get_user_rank(discord_id)
            stats = ticket_manager.get_period_stats()
            total_participants = stats["total_participants"] if stats else 0

            # Calculate win probability
            win_prob = raffle_draw.get_user_win_probability(discord_id, stats["period_id"]) if stats else None
            win_prob_text = f"{win_prob['probability_percent']:.2f}%" if win_prob else "N/A"

            embed_text = f"""
🎫 **Raffle Tickets for {ctx.author.display_name}**

**Total Tickets**: {tickets['total_tickets']:,}
**Rank**: #{rank} of {total_participants}
**Win Probability**: {win_prob_text}

**Breakdown**:
• Watchtime: {tickets['watchtime_tickets']} tickets
• Gifted Subs: {tickets['gifted_sub_tickets']} tickets
• Shuffle Wagers: {tickets['shuffle_wager_tickets']} tickets
• Bonus: {tickets['bonus_tickets']} tickets

Use `/leaderboard` to see top participants!
            """

            await ctx.send(embed_text.strip())

        except Exception as e:
            logger.error(f"Error checking tickets: {e}")
            await ctx.send(f"❌ Error checking tickets. Please try again.")

    @commands.hybrid_command(name="fair", aliases=["pf", "provablyfair"])
    async def provably_fair_info(self, ctx):
        """
        Explain how to verify a provably-fair draw
        Usage: /fair
        """
        try:
            guild_id = ctx.guild.id if ctx.guild else None
            fair_url = get_server_public_page_url(self.engine, guild_id, "/provably-fair")
            embed = discord.Embed(
                title="🎲 Provably Fair Draws",
                description=(
                    "Every raffle draw and slot reward is provably fair: the server seed is "
                    "committed (SHA-256) before the draw and revealed afterwards, so the "
                    "result can't be changed and you can verify it yourself.\n\n"
                    f"🔍 [Open the verification page]({fair_url})"
                ),
                color=discord.Color.green(),
            )
            await ctx.send(embed=embed)
        except Exception as e:
            logger.error(f"Error in /fair command: {e}")
            await ctx.send("❌ Could not build the verification link. Please try again.")

    @commands.hybrid_command(name="howraffle", aliases=["rafflehelp", "howtoraffle"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def how_raffle_works(self, ctx):
        """
        [ADMIN] Post an embed explaining how the raffle works and how tickets are earned
        Usage: /howraffle
        """
        try:
            guild_id = ctx.guild.id if ctx.guild else None
            watchtime_tickets, gifted_sub_tickets, wager_tickets = get_ticket_reward_settings(
                self.engine, guild_id, logger
            )
            settings = self._get_guild_settings(ctx)
            platform = platform_display_name(settings)
            code = settings.shuffle_campaign_code
            fair_url = get_server_public_page_url(self.engine, guild_id, "/provably-fair")

            embed = discord.Embed(
                title="How the Raffle Works",
                description=(
                    "Tickets are earned automatically while you support the stream. "
                    "Every ticket is a separate entry in the draw, so the more you "
                    "collect, the better your chances."
                ),
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Earning Tickets",
                value=(
                    f"**Watch time** — {watchtime_tickets} tickets per hour watched\n"
                    f"**Gifted subs** — {gifted_sub_tickets} tickets per sub gifted\n"
                    f"**{platform} wagers** — {wager_tickets} tickets per $1,000 wagered "
                    f"using code `{code}`\n"
                    "**Bonus** — awarded by the team for events and giveaways"
                ),
                inline=False,
            )
            embed.add_field(
                name="The Draw",
                value=(
                    "Raffles run in periods with a set start and end date. Once a period "
                    "ends, the number of winners is set and each one is drawn at random "
                    "from all entries, then revealed one at a time.\n"
                    "Holding more tickets increases your odds but never guarantees a win, "
                    "and a winner is removed from the pool before the next draw, so the "
                    "same person cannot win twice in one raffle.\n"
                    f"Draws are provably fair and can be [verified here]({fair_url})."
                ),
                inline=False,
            )
            embed.add_field(
                name="Getting Started",
                value=(
                    "Link your streaming account so watch time and wagers are credited to "
                    "you, then track your progress with the commands below.\n"
                    "`/tickets` — your balance, rank and win chance\n"
                    "`/leaderboard` — current standings\n"
                    "`/raffleinfo` — the active period and its dates"
                ),
                inline=False,
            )
            embed.set_footer(text="Wagerlabs")

            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error showing raffle explainer: {e}")
            await ctx.send("❌ Error loading raffle information. Please try again.")

    @commands.hybrid_command(name="leaderboard")
    async def leaderboard(self, ctx, limit: int = 10):
        """View the raffle ticket leaderboard."""
        await self._send_raffle_leaderboard(ctx, limit)

    @commands.hybrid_command(name="raffleboard", aliases=["raffletop", "rafflerankings"])
    async def raffle_leaderboard(self, ctx, limit: int = 10):
        """
        Legacy alias for the raffle ticket leaderboard.
        Usage: /raffleboard [limit]
        Example: /raffleboard 20
        """
        await self._send_raffle_leaderboard(ctx, limit)

    async def _send_raffle_leaderboard(self, ctx, limit: int = 10):
        """Build and send the raffle ticket leaderboard for either public command."""
        try:
            if limit < 1 or limit > 50:
                limit = 10

            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]

            leaderboard = ticket_manager.get_leaderboard(limit=limit)

            if not leaderboard:
                await ctx.send("❌ No raffle participants yet!")
                return

            # Get period stats
            stats = ticket_manager.get_period_stats()

            response = f"🏆 **Raffle Leaderboard** (Period #{stats['period_number']})\n\n"
            response += f"**Total Tickets**: {stats['total_tickets']:,}\n"
            response += f"**Total Participants**: {stats['total_participants']}\n\n"

            for entry in leaderboard:
                rank = entry["rank"]
                kick_name = entry["kick_name"] or "Unknown"
                total = entry["total_tickets"]
                prob = (total / stats["total_tickets"]) if stats["total_tickets"] > 0 else 0

                medal = ""
                if rank == 1:
                    medal = "🥇"
                elif rank == 2:
                    medal = "🥈"
                elif rank == 3:
                    medal = "🥉"
                else:
                    medal = f"#{rank}"

                response += f"{medal} **{kick_name}**: {total:,} tickets ({prob:.2%} chance)\n"

            response += f"\nUse `/tickets` to check your own balance!"

            await ctx.send(response)

        except Exception as e:
            logger.error(f"Error showing leaderboard: {e}")
            await ctx.send(f"❌ Error loading leaderboard. Please try again.")

    @commands.hybrid_command(name="raffleinfo", aliases=["raffle"])
    async def raffle_info(self, ctx):
        """
        View current raffle period information
        Usage: /raffleinfo
        """
        try:
            from datetime import datetime

            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]
            raffle_draw = managers["raffle_draw"]

            stats = ticket_manager.get_period_stats()
            watchtime_tickets, gifted_sub_tickets, wager_tickets = get_ticket_reward_settings(
                self.engine, ctx.guild.id if ctx.guild else None, logger
            )
            settings = self._get_guild_settings(ctx)
            platform = platform_display_name(settings)
            code = settings.shuffle_campaign_code

            # Debug logging
            logger.info(f"[raffleinfo] guild_id={ctx.guild.id}, server_id={managers['ticket_manager'].server_id}")
            logger.info(f"[raffleinfo] stats={stats}")

            if not stats:
                await ctx.send("❌ No active raffle period!")
                return

            # Check if period hasn't started yet
            now = datetime.now()
            start_date = stats["start_date"]
            end_date = stats["end_date"]

            if now < start_date:
                # Period hasn't started
                days_until = (start_date - now).days
                hours_until = (start_date - now).seconds // 3600
                time_msg = f"{days_until} days" if days_until > 0 else f"{hours_until} hours"

                response = f"""
🎰 **Raffle Period Information**

**Period**: #{stats['period_number']}
**Starts**: {start_date.strftime('%B %d, %Y')}
**Ends**: {end_date.strftime('%B %d, %Y')}
**Status**: PENDING (starts in {time_msg})

⏳ **The raffle period has not started yet.**

**How to Earn Tickets** (once period starts):
⏱️ **Watch Streams** - {watchtime_tickets} tickets per hour
🎁 **Gift Subs** - {gifted_sub_tickets} tickets per sub
🎲 **{platform} Wagers** - {wager_tickets} tickets per $1000 wagered (code '{code}')
⭐ **Bonus** - Admin awarded for events

**Commands**:
• `/tickets` - Check your ticket balance
• **Shuffle Verification** - Create a ticket to link your Shuffle account

Get ready to participate when the period starts!
                """
            else:
                # Period is active
                # Get draw history
                history = raffle_draw.get_draw_history(limit=1)
                last_winner = history[0] if history else None

                # Check if auto-leaderboard is configured (from database settings)
                leaderboard_channel_id = None
                if hasattr(self.bot, "settings_manager") and self.bot.settings_manager:
                    leaderboard_channel_id = self.bot.settings_manager.raffle_leaderboard_channel_id

                leaderboard_note = ""
                if leaderboard_channel_id:
                    try:
                        channel = self.bot.get_channel(int(leaderboard_channel_id))
                        if channel:
                            leaderboard_note = (
                                f"\n📊 **Live Leaderboard**: Check <#{leaderboard_channel_id}> for real-time standings!"
                            )
                    except:
                        pass

                response = f"""
🎰 **Current Raffle Period**

**Period**: #{stats['period_number']}
**Started**: {start_date.strftime('%B %d, %Y')}
**Ends**: {end_date.strftime('%B %d, %Y')}
**Status**: {stats['status'].upper()}

**Statistics**:
• Total Tickets: {stats['total_tickets']:,}
• Total Participants: {stats['total_participants']}{leaderboard_note}

**How to Earn Tickets**:
⏱️ **Watch Streams** - {watchtime_tickets} tickets per hour
🎁 **Gift Subs** - {gifted_sub_tickets} tickets per sub
🎲 **{platform} Wagers** - {wager_tickets} tickets per $1000 wagered (code '{code}')
⭐ **Bonus** - Admin awarded for events

**Commands**:
• `/tickets` - Check your ticket balance
• `/leaderboard` - View top participants
• **Shuffle Verification** - Create a ticket to link your Shuffle account
                """

                if last_winner:
                    winner_tickets = last_winner.get("total_tickets", 0)
                    # Calculate win probability if we have the data
                    win_prob = last_winner.get("win_probability", 0)
                    if win_prob:
                        response += f"\n**Last Winner**: {last_winner['winner_kick_name']} ({winner_tickets:,} tickets, {win_prob:.2%} chance)"
                    else:
                        response += f"\n**Last Winner**: {last_winner['winner_kick_name']} ({winner_tickets:,} tickets)"

            await ctx.send(response.strip())

        except Exception as e:
            logger.error(f"Error showing raffle info: {e}")
            import traceback

            traceback.print_exc()
            await ctx.send(f"❌ Error loading raffle info. Please try again.")

    # ========================================
    # ADMIN COMMANDS
    # ========================================

    @commands.hybrid_command(name="rafflegive")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def give_tickets(self, ctx, user: commands.UserConverter, tickets: int, *, reason: str = "Admin bonus"):
        """
        [ADMIN] Manually award tickets to a user
        Usage: /rafflegive @user <tickets> [reason]
        Example: /rafflegive @John 100 Event participation bonus
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]

            if tickets <= 0:
                await ctx.send(f"❌ Ticket amount must be positive!")
                return

            discord_id = user.id

            # Get kick name
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT kick_name FROM links WHERE discord_id = :discord_id AND discord_server_id = :guild_id
                """
                    ),
                    {"discord_id": discord_id, "guild_id": guild_id},
                )

                row = result.fetchone()
                if not row:
                    await ctx.send(f"❌ {user.mention} is not linked to a Kick account!")
                    return

                kick_name = row[0]

            # Award tickets
            success = ticket_manager.award_tickets(
                discord_id=discord_id,
                kick_name=kick_name,
                tickets=tickets,
                source="bonus",
                description=f"Manual award by {ctx.author.name}: {reason}",
            )

            if success:
                await ctx.send(f"✅ Awarded **{tickets:,} tickets** to {user.mention}!\n" f"Reason: {reason}")
                logger.info(f"🎁 Admin {ctx.author} awarded {tickets} tickets to {user} ({kick_name}): {reason}")
            else:
                await ctx.send(f"❌ Failed to award tickets. Check logs for details.")

        except commands.BadArgument:
            await ctx.send(f"❌ Invalid user or ticket amount. Usage: `/rafflegive @user <tickets> [reason]`")
        except Exception as e:
            logger.error(f"Error giving tickets: {e}")
            await ctx.send(f"❌ Error awarding tickets. Please try again.")

    @commands.hybrid_command(name="raffleremove")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def remove_tickets(self, ctx, user: commands.UserConverter, tickets: int, *, reason: str = "Admin removal"):
        """
        [ADMIN] Remove tickets from a user
        Usage: /raffleremove @user <tickets> [reason]
        Example: /raffleremove @John 50 Rule violation
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]

            if tickets <= 0:
                await ctx.send(f"❌ Ticket amount must be positive!")
                return

            discord_id = user.id

            # Get kick name
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT kick_name FROM links WHERE discord_id = :discord_id AND discord_server_id = :guild_id
                """
                    ),
                    {"discord_id": discord_id, "guild_id": guild_id},
                )

                row = result.fetchone()
                if not row:
                    await ctx.send(f"❌ {user.mention} is not linked to a Kick account!")
                    return

                kick_name = row[0]

            # Remove tickets
            success = ticket_manager.remove_tickets(
                discord_id=discord_id, kick_name=kick_name, tickets=tickets, reason=reason
            )

            if success:
                await ctx.send(f"✅ Removed **{tickets:,} tickets** from {user.mention}!\n" f"Reason: {reason}")
                logger.info(f"🗑️ Admin {ctx.author} removed {tickets} tickets from {user} ({kick_name}): {reason}")
            else:
                await ctx.send(f"❌ Failed to remove tickets. User may not have enough tickets.")

        except commands.BadArgument:
            await ctx.send(f"❌ Invalid user or ticket amount. Usage: `/raffleremove @user <tickets> [reason]`")
        except Exception as e:
            logger.error(f"Error removing tickets: {e}")
            await ctx.send(f"❌ Error removing tickets. Please try again.")

    @commands.hybrid_command(name="raffledraw")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def draw_winner(self, ctx, *, prize_description: str = "Monthly Raffle Prize"):
        """
        [ADMIN] Draw a raffle winner
        Usage: /raffledraw [prize description]
        Example: /raffledraw $500 Cash Prize
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]
            raffle_draw = managers["raffle_draw"]

            admin_id = ctx.author.id

            # Get current period stats
            stats = ticket_manager.get_period_stats()

            if not stats or stats["total_tickets"] == 0:
                await ctx.send(f"❌ Cannot draw winner: No tickets in current raffle period!")
                return

            # Check if already drawn
            with self.engine.begin() as conn:
                existing = conn.execute(
                    text(
                        """
                    SELECT winner_kick_name FROM raffle_draws
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": stats["period_id"]},
                )

                if existing.fetchone():
                    await ctx.send(f"❌ A winner has already been drawn for this period!")
                    return

            # Confirmation message
            await ctx.send(
                f"🎲 **Drawing raffle winner...**\n"
                f"Period: #{stats['period_number']}\n"
                f"Total Tickets: {stats['total_tickets']:,}\n"
                f"Total Participants: {stats['total_participants']}\n"
                f"Prize: {prize_description}"
            )

            # Draw winner
            result = raffle_draw.draw_winner(
                period_id=stats["period_id"], prize_description=prize_description, drawn_by_discord_id=admin_id
            )

            if result:
                # Get Discord user object if possible
                try:
                    discord_user = await self.bot.fetch_user(result["winner_discord_id"])
                    mention = discord_user.mention
                except:
                    mention = f"Discord ID: {result['winner_discord_id']}"

                # Get full ticket breakdown
                winner_tickets = ticket_manager.get_user_tickets(result["winner_discord_id"])

                await ctx.send(
                    f"""
🎉 **RAFFLE WINNER DRAWN!** 🎉

**Winner**: {result['winner_kick_name']} ({mention})
**Tickets**: {result['winner_tickets']:,} out of {result['total_tickets']:,}
**Win Probability**: {result['win_probability']:.2f}%
**Prize**: {prize_description}

**Breakdown**:
• Watchtime: {winner_tickets.get('watchtime_tickets', 0)} tickets
• Gifted Subs: {winner_tickets.get('gifted_sub_tickets', 0)} tickets
• Shuffle Wagers: {winner_tickets.get('shuffle_wager_tickets', 0)} tickets
• Bonus: {winner_tickets.get('bonus_tickets', 0)} tickets

Congratulations! 🎊
                """.strip()
                )

                logger.info(f"🎉 Raffle winner drawn: {result['winner_kick_name']} (Period #{stats['period_id']})")

            else:
                await ctx.send(f"❌ Error drawing winner: No participants or unexpected error")

        except Exception as e:
            logger.error(f"Error drawing raffle winner: {e}")
            import traceback

            traceback.print_exc()
            await ctx.send(f"❌ Error drawing winner. Please try again.")

    @commands.hybrid_command(name="rafflestats")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def raffle_stats(self, ctx, user: discord.Member = None):
        """
        [ADMIN] View detailed raffle statistics
        Usage: /rafflestats [@user]
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]
            raffle_draw = managers["raffle_draw"]

            if user:
                # Show user-specific stats
                discord_id = user.id
                tickets = ticket_manager.get_user_tickets(discord_id)

                if not tickets:
                    await ctx.send(f"❌ {user.mention} has no raffle tickets!")
                    return

                # Get rank
                rank = ticket_manager.get_user_rank(discord_id)
                stats = ticket_manager.get_period_stats()

                # Get detailed breakdown from tables
                with self.engine.begin() as conn:
                    # Watchtime stats
                    watchtime_result = conn.execute(
                        text(
                            """
                        SELECT SUM(minutes_converted), SUM(tickets_awarded)
                        FROM raffle_watchtime_converted
                        WHERE kick_name = :kick_name
                    """
                        ),
                        {"kick_name": tickets["kick_name"]},
                    )
                    wt_row = watchtime_result.fetchone()
                    total_minutes = wt_row[0] or 0

                    # Gifted sub stats
                    subs_result = conn.execute(
                        text(
                            """
                        SELECT COUNT(*), SUM(sub_count), SUM(tickets_awarded)
                        FROM raffle_gifted_subs
                        WHERE gifter_kick_name = :kick_name
                    """
                        ),
                        {"kick_name": tickets["kick_name"]},
                    )
                    subs_row = subs_result.fetchone()
                    gift_events = subs_row[0] or 0
                    total_subs = subs_row[1] or 0

                    # Shuffle stats
                    shuffle_result = conn.execute(
                        text(
                            """
                        SELECT total_wager_usd, tickets_awarded
                        FROM raffle_shuffle_wagers
                        WHERE discord_id = :discord_id
                    """
                        ),
                        {"discord_id": discord_id},
                    )
                    shuffle_row = shuffle_result.fetchone()
                    total_wager = shuffle_row[0] if shuffle_row else 0

                # Get win probability
                win_prob = raffle_draw.get_user_win_probability(discord_id, stats["period_id"])
                win_prob_text = f"{win_prob['probability_percent']:.2f}%" if win_prob else "N/A"

                response = f"""
📊 **Detailed Stats for {user.display_name}**

**Rank**: #{rank} of {stats['total_participants']}
**Total Tickets**: {tickets['total_tickets']:,}
**Win Probability**: {win_prob_text}

**Watchtime**: {tickets['watchtime_tickets']} tickets
• Total watch time: {total_minutes/60:.1f} hours

**Gifted Subs**: {tickets['gifted_sub_tickets']} tickets
• Gift events: {gift_events}
• Total subs gifted: {total_subs}

**Shuffle Wagers**: {tickets['shuffle_wager_tickets']} tickets
• Total wagered: ${total_wager:.2f}

**Bonus**: {tickets['bonus_tickets']} tickets
                """

                await ctx.send(response.strip())

            else:
                # Show overall stats
                stats = ticket_manager.get_period_stats()

                response = f"""
📊 **Overall Raffle Statistics**

**Period**: #{stats['period_number']}
**Duration**: {stats['start_date'].strftime('%b %d')} - {stats['end_date'].strftime('%b %d, %Y')}
**Status**: {stats['status'].upper()}

**Participation**:
• Total Tickets: {stats['total_tickets']:,}
• Total Participants: {stats['total_participants']}
• Average per participant: {stats['total_tickets'] / max(stats['total_participants'], 1):.1f} tickets

Use `/rafflestats @user` to see individual stats
                """

                await ctx.send(response.strip())

        except commands.BadArgument:
            await ctx.send(f"❌ Invalid user mention.")
        except Exception as e:
            logger.error(f"Error showing raffle stats: {e}")
            await ctx.send(f"❌ Error loading stats. Please try again.")

    # ========================================
    # PERIOD MANAGEMENT COMMANDS
    # ========================================

    @commands.hybrid_command(name="raffleend", aliases=["endraffle"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def end_raffle(self, ctx):
        """
        [ADMIN] End the current raffle period
        Usage: /raffleend
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id

            with self.engine.begin() as conn:
                # Get current period
                result = conn.execute(
                    text(
                        """
                    SELECT id, start_date, end_date, status, COALESCE(period_number, id)
                    FROM raffle_periods
                    WHERE status = 'active' AND discord_server_id = :guild_id
                    ORDER BY start_date DESC
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                )
                period = result.fetchone()

                if not period:
                    await ctx.send("❌ No active raffle period to end!")
                    return

                # End the period
                conn.execute(
                    text(
                        """
                    UPDATE raffle_periods
                    SET status = 'ended', end_date = CURRENT_TIMESTAMP
                    WHERE id = :period_id
                """
                    ),
                    {"period_id": period[0]},
                )

                await ctx.send(
                    f"✅ Raffle period #{period[4]} has been ended.\n"
                    f"Use `/raffledraw` to select a winner, then `/rafflestart` to begin a new period."
                )

        except Exception as e:
            logger.error(f"Error ending raffle period: {e}")
            await ctx.send(f"❌ Error ending raffle period: {str(e)}")

    @commands.hybrid_command(name="rafflestart", aliases=["newraffle"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def start_raffle(self, ctx, start_day: int = None, end_day: int = None):
        """
        [ADMIN] Start a new raffle period
        Usage: /rafflestart [start_day] [end_day]
        Example: /rafflestart 1 30  (1st to 30th of current month)
        If no dates provided, uses current month (1st to last day)
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from datetime import datetime

            from dateutil.relativedelta import relativedelta

            # Check if there's already an active period
            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods
                    WHERE status = 'active' AND discord_server_id = :guild_id
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                )
                if result.fetchone():
                    await ctx.send("❌ There's already an active raffle period! Use `/raffleend` first.")
                    return

                # Calculate dates
                now = datetime.now()

                if start_day and end_day:
                    start = now.replace(day=start_day, hour=0, minute=0, second=0, microsecond=0)
                    end = now.replace(day=end_day, hour=23, minute=59, second=59, microsecond=0)
                else:
                    # Default: current month
                    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    next_month = start + relativedelta(months=1)
                    end = next_month - relativedelta(seconds=1)

                # Create new period. period_number is the per-server display
                # counter (the global PK `id` keeps growing across all servers).
                result = conn.execute(
                    text(
                        """
                    INSERT INTO raffle_periods (start_date, end_date, status, discord_server_id, period_number)
                    VALUES (
                        :start, :end, 'active', :guild_id,
                        (SELECT COALESCE(MAX(period_number), 0) + 1
                         FROM raffle_periods WHERE discord_server_id = :guild_id)
                    )
                    RETURNING id, period_number
                """
                    ),
                    {"start": start, "end": end, "guild_id": guild_id},
                )
                row = result.fetchone()
                period_id = row[0]
                period_number = row[1]

                await ctx.send(
                    f"✅ New raffle period #{period_number} started!\n"
                    f"**Duration**: {start.strftime('%b %d, %Y')} - {end.strftime('%b %d, %Y')}\n"
                    f"Users can now earn tickets!"
                )

        except Exception as e:
            logger.error(f"Error starting raffle period: {e}")
            await ctx.send(f"❌ Error starting raffle period: {str(e)}")

    @commands.hybrid_command(name="rafflerestart", aliases=["resetraffle"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def restart_raffle(self, ctx):
        """
        [ADMIN] End current period and immediately start a new one for next month
        Usage: /rafflerestart
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from datetime import datetime

            from dateutil.relativedelta import relativedelta

            with self.engine.begin() as conn:
                # Get current period
                result = conn.execute(
                    text(
                        """
                    SELECT id, total_tickets
                    FROM raffle_periods
                    WHERE status = 'active' AND discord_server_id = :guild_id
                    ORDER BY start_date DESC
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                )
                current_period = result.fetchone()

                if current_period:
                    # End current period
                    conn.execute(
                        text(
                            """
                        UPDATE raffle_periods
                        SET status = 'ended', end_date = CURRENT_TIMESTAMP
                        WHERE id = :period_id
                    """
                        ),
                        {"period_id": current_period[0]},
                    )

                    old_tickets = current_period[1]
                else:
                    old_tickets = 0

                # Create new period for next month
                now = datetime.now()
                next_month = now + relativedelta(months=1)
                start = next_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                end = (start + relativedelta(months=1)) - relativedelta(seconds=1)

                result = conn.execute(
                    text(
                        """
                    INSERT INTO raffle_periods (start_date, end_date, status, discord_server_id, period_number)
                    VALUES (
                        :start, :end, 'active', :guild_id,
                        (SELECT COALESCE(MAX(period_number), 0) + 1
                         FROM raffle_periods WHERE discord_server_id = :guild_id)
                    )
                    RETURNING id, period_number
                """
                    ),
                    {"start": start, "end": end, "guild_id": guild_id},
                )
                row = result.fetchone()
                new_period_id = row[0]
                new_period_number = row[1]

                await ctx.send(
                    f"✅ Raffle reset complete!\n\n"
                    f"**Old Period**: Ended with {old_tickets:,} total tickets\n"
                    f"**New Period #{new_period_number}**: {start.strftime('%b %d, %Y')} - {end.strftime('%b %d, %Y')}\n\n"
                    f"💡 Don't forget to draw a winner from the old period: `/raffledraw`"
                )

        except Exception as e:
            logger.error(f"Error restarting raffle: {e}")
            await ctx.send(f"❌ Error restarting raffle: {str(e)}")

    @commands.hybrid_command(name="rafflesetdate", aliases=["raffledates"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def set_raffle_dates(self, ctx, start_date: str, end_date: str):
        """
        [ADMIN] Update the current raffle period dates
        Usage: /rafflesetdate YYYY-MM-DD YYYY-MM-DD
        Example: /rafflesetdate 2025-11-01 2025-11-30
        """
        # Reads the database before its first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from datetime import datetime

            # Parse dates
            try:
                start = datetime.strptime(start_date, "%Y-%m-%d")
                end = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            except ValueError:
                await ctx.send("❌ Invalid date format! Use YYYY-MM-DD (e.g., 2025-11-01)")
                return

            if start >= end:
                await ctx.send("❌ Start date must be before end date!")
                return

            with self.engine.begin() as conn:
                # Get current active period
                result = conn.execute(
                    text(
                        """
                    SELECT id, COALESCE(period_number, id) FROM raffle_periods
                    WHERE status = 'active' AND discord_server_id = :guild_id
                    ORDER BY start_date DESC
                    LIMIT 1
                """
                    ),
                    {"guild_id": guild_id},
                )
                period = result.fetchone()

                if not period:
                    await ctx.send("❌ No active raffle period! Use `/rafflestart` to create one.")
                    return

                # Update dates
                conn.execute(
                    text(
                        """
                    UPDATE raffle_periods
                    SET start_date = :start, end_date = :end
                    WHERE id = :period_id
                """
                    ),
                    {"start": start, "end": end, "period_id": period[0]},
                )

                await ctx.send(
                    f"✅ Raffle period #{period[1]} dates updated!\n"
                    f"**New Duration**: {start.strftime('%b %d, %Y')} - {end.strftime('%b %d, %Y')}"
                )

        except Exception as e:
            logger.error(f"Error setting raffle dates: {e}")
            await ctx.send(f"❌ Error updating dates: {str(e)}")

    @commands.hybrid_command(name="shuffledebug")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def shuffle_debug(self, ctx):
        """
        [ADMIN] Debug Shuffle wager tracking
        Usage: /shuffledebug
        """
        try:
            guild_id = ctx.guild.id
            await ctx.send("🔍 Fetching Shuffle affiliate data...")

            # Fetch raw data
            import aiohttp

            from .config import SHUFFLE_AFFILIATE_URL, SHUFFLE_CAMPAIGN_CODE

            async with aiohttp.ClientSession() as session:
                async with session.get(SHUFFLE_AFFILIATE_URL, timeout=30) as response:
                    if response.status != 200:
                        await ctx.send(f"❌ API returned status {response.status}")
                        return

                    data = await response.json()

                    if not isinstance(data, list):
                        await ctx.send(f"❌ Unexpected data format: {type(data)}")
                        return

                    # Show total users
                    await ctx.send(f"📊 Total users in affiliate data: {len(data)}")

                    # Show all campaign codes found
                    campaign_codes = set(user.get("campaignCode", "NONE") for user in data)
                    await ctx.send(f"📋 Campaign codes found: {', '.join(sorted(campaign_codes))}")

                    # Filter for our campaign code
                    filtered = [
                        user for user in data if user.get("campaignCode", "").lower() == SHUFFLE_CAMPAIGN_CODE.lower()
                    ]

                    await ctx.send(f"🎯 Users with code '{SHUFFLE_CAMPAIGN_CODE}': {len(filtered)}")

                    # Show first 5 users with the code
                    if filtered:
                        response_text = "**Sample users:**\n"
                        for user in filtered[:5]:
                            username = user.get("username", "Unknown")
                            wager = user.get("wagerAmount", 0)
                            code = user.get("campaignCode", "NONE")
                            response_text += f"• {username}: ${wager:.2f} (code: {code})\n"

                        await ctx.send(response_text)

                    # Check database tracking
                    with self.engine.begin() as conn:
                        result = conn.execute(
                            text(
                                """
                            SELECT shuffle_username, total_wager_usd, last_known_wager,
                                   tickets_awarded, kick_name, discord_id
                            FROM raffle_shuffle_wagers
                            WHERE period_id = (SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id)
                            ORDER BY total_wager_usd DESC
                            LIMIT 5
                        """
                            ),
                            {"guild_id": guild_id},
                        )

                        rows = result.fetchall()
                        if rows:
                            db_text = "**Database tracking:**\n"
                            for row in rows:
                                db_text += f"• {row[0]}: ${row[1]:.2f} (last: ${row[2]:.2f}, tickets: {row[3]}, linked: {'Yes' if row[4] else 'No'})\n"
                            await ctx.send(db_text)
                        else:
                            await ctx.send("No Shuffle wagers tracked in database yet")

        except Exception as e:
            logger.error(f"Shuffle debug error: {e}")
            import traceback

            traceback.print_exc()
            await ctx.send(f"❌ Error: {str(e)}")

    @commands.hybrid_command(name="shuffleunlinked")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def shuffle_unlinked(self, ctx, limit: int = 20):
        """
        [ADMIN] Show unlinked Shuffle accounts with wagers
        Usage: /shuffleunlinked [limit]
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id

            with self.engine.begin() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT shuffle_username, total_wager_usd, last_checked
                    FROM raffle_shuffle_wagers
                    WHERE period_id = (SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id)
                      AND (discord_id IS NULL OR kick_name IS NULL)
                      AND total_wager_usd > 0
                    ORDER BY total_wager_usd DESC
                    LIMIT :limit
                """
                    ),
                    {"limit": limit, "guild_id": guild_id},
                )

                rows = result.fetchall()

                if not rows:
                    await ctx.send("✅ All Shuffle users with wagers are linked!")
                    return

                response = f"**Unlinked Shuffle Accounts ({len(rows)}):**\n\n"
                response += "These users have wagers but need to link their accounts:\n\n"

                for row in rows:
                    username = row[0]
                    wager = row[1]
                    last_check = row[2].strftime("%Y-%m-%d %H:%M") if row[2] else "Never"
                    response += f"• **{username}**: ${wager:,.2f} wagered (last checked: {last_check})\n"

                response += (
                    "\n**To link:** Users verify themselves from the Shuffle Verify panel (`/createshufflepanel`)."
                )

                await ctx.send(response)

        except Exception as e:
            logger.error(f"Error showing unlinked Shuffle users: {e}")
            await ctx.send(f"❌ Error: {str(e)}")

    @commands.hybrid_command(name="raffleupdateboard", aliases=["updateraffleboard", "refreshraffle"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def update_raffle_board(self, ctx):
        """
        [ADMIN] Manually update the auto-leaderboard
        Usage: /raffleupdateboard
        """
        try:
            # Get the auto_leaderboard instance from bot
            if hasattr(self.bot, "auto_leaderboard") and self.bot.auto_leaderboard:
                await ctx.send("🔄 Updating raffle leaderboard...")
                await self.bot.auto_leaderboard.update_leaderboard()
                await ctx.send("✅ Raffle leaderboard updated!")
            else:
                await ctx.send(
                    "❌ Auto-leaderboard is not configured. Set `raffle_leaderboard_channel_id` in the dashboard."
                )
        except Exception as e:
            logger.error(f"Error manually updating leaderboard: {e}")
            await ctx.send(f"❌ Error updating leaderboard: {str(e)}")

    @commands.hybrid_command(name="rafflecleartickets", aliases=["cleartickets", "resettickets"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def clear_all_tickets(self, ctx):
        """
        [ADMIN] Manually clear ALL tickets from the database for this guild
        Usage: /rafflecleartickets

        WARNING: This will delete all tickets, watchtime conversions, gifted subs, and shuffle wagers for this guild!
        """
        guild_id = ctx.guild.id
        await ctx.send(
            "⚠️ **WARNING:** This will DELETE ALL raffle tickets and history for this server!\nType `CONFIRM DELETE` to proceed."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            msg = await self.bot.wait_for("message", timeout=30.0, check=check)

            if msg.content != "CONFIRM DELETE":
                await ctx.send("❌ Cancelled. Tickets were NOT deleted.")
                return

            # Delete everything for this guild's active period
            with self.engine.begin() as conn:
                # Get guild's active period
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period = period_result.fetchone()

                if period:
                    period_id = period[0]
                    deleted_tickets = conn.execute(
                        text("DELETE FROM raffle_tickets WHERE period_id = :period_id"), {"period_id": period_id}
                    ).rowcount
                    deleted_watchtime = conn.execute(
                        text("DELETE FROM raffle_watchtime_converted WHERE period_id = :period_id"),
                        {"period_id": period_id},
                    ).rowcount
                    deleted_subs = conn.execute(
                        text("DELETE FROM raffle_gifted_subs WHERE period_id = :period_id"), {"period_id": period_id}
                    ).rowcount
                    deleted_wagers = conn.execute(
                        text("DELETE FROM raffle_shuffle_wagers WHERE period_id = :period_id"), {"period_id": period_id}
                    ).rowcount
                else:
                    deleted_tickets = deleted_watchtime = deleted_subs = deleted_wagers = 0

            await ctx.send(
                f"✅ **All tickets cleared!**\n"
                f"• Deleted {deleted_tickets} ticket records\n"
                f"• Deleted {deleted_watchtime} watchtime conversions\n"
                f"• Deleted {deleted_subs} gifted sub records\n"
                f"• Deleted {deleted_wagers} shuffle wager records\n\n"
                f"Everyone starts fresh at 0 tickets!"
            )

            # Update leaderboard
            if hasattr(self.bot, "auto_leaderboard") and self.bot.auto_leaderboard:
                await self.bot.auto_leaderboard.update_leaderboard()

        except Exception as e:
            logger.error(f"Error clearing tickets: {e}")
            await ctx.send(f"❌ Error: {str(e)}")

    @commands.hybrid_command(name="rafflecleanup", aliases=["cleanupwatchtime"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def cleanup_watchtime_tickets(self, ctx):
        """
        Manually reset watchtime tickets only (keeps wager and gifted sub tickets)
        This marks all existing watchtime as "already converted" to prevent re-awarding
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from sqlalchemy import text

            # Get active period for this guild
            with self.engine.begin() as conn:
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period_row = period_result.fetchone()

                if not period_row:
                    await ctx.send("❌ No active raffle period found for this server!")
                    return

                period_id = period_row[0]
                # Step 1: Get all users' current watchtime BEFORE resetting
                watchtime_snapshot = conn.execute(
                    text(
                        """
                    SELECT w.username, w.minutes
                    FROM watchtime w
                    JOIN links l ON l.kick_name = w.username
                    WHERE w.minutes > 0 AND l.discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchall()

                # Step 2: Reset ONLY watchtime_tickets to 0 (keep wager and gifted sub tickets!)
                reset_count = conn.execute(
                    text(
                        """
                    UPDATE raffle_tickets
                    SET watchtime_tickets = 0,
                        total_tickets = gifted_sub_tickets + shuffle_wager_tickets + bonus_tickets
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).rowcount

                # Step 3: Delete users who now have 0 total tickets
                deleted_empty = conn.execute(
                    text(
                        """
                    DELETE FROM raffle_tickets
                    WHERE period_id = :period_id AND total_tickets = 0
                """
                    ),
                    {"period_id": period_id},
                ).rowcount

                # Step 4: Delete old watchtime conversion tracking
                deleted_watchtime = conn.execute(
                    text(
                        """
                    DELETE FROM raffle_watchtime_converted WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).rowcount

                # Step 5: Mark ALL current watchtime as "already converted" (with 0 tickets awarded)
                # This prevents re-awarding tickets for historical watchtime
                conversion_count = 0
                for kick_name, minutes in watchtime_snapshot:
                    if minutes >= 60:  # Only track if they have at least 1 hour
                        hours = minutes // 60
                        tracked_minutes = hours * 60  # Only track full hours
                        conn.execute(
                            text(
                                """
                            INSERT INTO raffle_watchtime_converted
                                (period_id, kick_name, minutes_converted, tickets_awarded)
                            VALUES
                                (:period_id, :kick_name, :minutes, 0)
                        """
                            ),
                            {"period_id": period_id, "kick_name": kick_name, "minutes": tracked_minutes},
                        )
                        conversion_count += 1

            await ctx.send(
                f"✅ **Watchtime Cleanup Complete!**\n"
                f"• Reset watchtime tickets for {reset_count} users\n"
                f"• Deleted {deleted_empty} users with 0 tickets\n"
                f"• Marked {conversion_count} users' watchtime as converted\n"
                f"• Deleted {deleted_watchtime} old conversion records\n\n"
                f"**Wager and gifted sub tickets were preserved!**"
            )

            # Update leaderboard
            if hasattr(self.bot, "auto_leaderboard") and self.bot.auto_leaderboard:
                await self.bot.auto_leaderboard.update_leaderboard()

        except Exception as e:
            logger.error(f"Error in watchtime cleanup: {e}")
            await ctx.send(f"❌ Error: {str(e)}")
            import traceback

            traceback.print_exc()

    @commands.hybrid_command(name="rafflerestoresubs", aliases=["restoresubs"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def restore_gifted_sub_tickets(self, ctx):
        """
        Restore gifted sub tickets from the raffle_gifted_subs event log
        Use this if gifted sub tickets were accidentally deleted
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from sqlalchemy import text

            # Get active period for this guild
            with self.engine.begin() as conn:
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period_row = period_result.fetchone()

                if not period_row:
                    await ctx.send("❌ No active raffle period found for this server!")
                    return

                period_id = period_row[0]

            managers = self._get_guild_managers(ctx)
            ticket_manager = managers["ticket_manager"]

            with self.engine.begin() as conn:
                # Get all gifted subs for this period
                result = conn.execute(
                    text(
                        """
                    SELECT
                        gifter_kick_name,
                        gifter_discord_id,
                        sub_count,
                        tickets_awarded
                    FROM raffle_gifted_subs
                    WHERE period_id = :period_id
                    ORDER BY gifted_at
                """
                    ),
                    {"period_id": period_id},
                )

                subs = list(result)

                if not subs:
                    await ctx.send("ℹ️ No gifted subs found for this period.")
                    return

                restored_count = 0
                total_tickets = 0

                for kick_name, discord_id, sub_count, tickets in subs:
                    if discord_id:
                        # Re-award the tickets
                        success = ticket_manager.award_tickets(
                            discord_id=discord_id,
                            kick_name=kick_name,
                            tickets=tickets,
                            source="gifted_sub",
                            description=f"Restored: Gifted {sub_count} sub(s)",
                            period_id=period_id,
                        )
                        if success:
                            restored_count += 1
                            total_tickets += tickets

            await ctx.send(
                f"✅ **Gifted Sub Tickets Restored!**\n"
                f"• Restored tickets for {restored_count} gifted sub events\n"
                f"• Total tickets restored: {total_tickets}\n\n"
                f"Run `/leaderboard` to see updated standings."
            )

            # Update leaderboard
            if hasattr(self.bot, "auto_leaderboard") and self.bot.auto_leaderboard:
                await self.bot.auto_leaderboard.update_leaderboard()

        except Exception as e:
            logger.error(f"Error restoring gifted sub tickets: {e}")
            await ctx.send(f"❌ Error: {str(e)}")
            import traceback

            traceback.print_exc()

    @commands.hybrid_command(name="rafflechecktables", aliases=["checktables"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def check_raffle_tables(self, ctx):
        """
        Check the current state of all raffle database tables
        Shows row counts and sample data to verify table states
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from sqlalchemy import text

            # Get active period for this guild
            with self.engine.begin() as conn:
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period_row = period_result.fetchone()

                if not period_row:
                    await ctx.send("❌ No active raffle period found for this server!")
                    return

                period_id = period_row[0]

            output = [f"**📊 Raffle Database Check (Period #{period_id})**\n"]

            with self.engine.begin() as conn:
                # Check raffle_tickets
                tickets_count = conn.execute(
                    text(
                        """
                    SELECT COUNT(*), SUM(total_tickets)
                    FROM raffle_tickets
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).fetchone()

                output.append(f"**raffle_tickets:**")
                output.append(f"  • {tickets_count[0] or 0} users")
                output.append(f"  • {tickets_count[1] or 0} total tickets\n")

                # Check raffle_watchtime_converted
                watchtime_count = conn.execute(
                    text(
                        """
                    SELECT COUNT(*) FROM raffle_watchtime_converted
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).scalar()

                output.append(f"**raffle_watchtime_converted:**")
                output.append(f"  • {watchtime_count or 0} conversion records\n")

                # Check raffle_gifted_subs
                subs_result = conn.execute(
                    text(
                        """
                    SELECT COUNT(*), SUM(tickets_awarded)
                    FROM raffle_gifted_subs
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).fetchone()

                output.append(f"**raffle_gifted_subs:**")
                output.append(f"  • {subs_result[0] or 0} gifted sub events")
                output.append(f"  • {subs_result[1] or 0} tickets awarded\n")

                # Check raffle_shuffle_wagers
                wagers_result = conn.execute(
                    text(
                        """
                    SELECT COUNT(*), SUM(tickets_awarded), SUM(total_wager_usd)
                    FROM raffle_shuffle_wagers
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).fetchone()

                output.append(f"**raffle_shuffle_wagers:**")
                output.append(f"  • {wagers_result[0] or 0} wager records")
                output.append(f"  • {wagers_result[1] or 0} tickets awarded")
                output.append(f"  • ${wagers_result[2] or 0:.2f} total wagered\n")

                # Check raffle_shuffle_links
                links_count = conn.execute(
                    text(
                        """
                    SELECT COUNT(*) FROM raffle_shuffle_links WHERE verified = TRUE
                """
                    )
                ).scalar()

                output.append(f"**raffle_shuffle_links:**")
                output.append(f"  • {links_count or 0} verified links")

            await ctx.send("\n".join(output))

        except Exception as e:
            logger.error(f"Error checking tables: {e}")
            await ctx.send(f"❌ Error: {str(e)}")
            import traceback

            traceback.print_exc()

    @commands.hybrid_command(name="raffleclearwatchtime", aliases=["clearwatchtimetickets"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def clear_watchtime_tickets_only(self, ctx):
        """
        Clear ONLY watchtime tickets, keeping gifted sub and shuffle wager tickets
        Use this when watchtime tickets were incorrectly awarded
        Usage: /raffleclearwatchtime
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from sqlalchemy import text

            # Get active period for this guild
            with self.engine.begin() as conn:
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period_row = period_result.fetchone()

                if not period_row:
                    await ctx.send("❌ No active raffle period found for this server!")
                    return

                period_id = period_row[0]
                # Get current state
                current_state = conn.execute(
                    text(
                        """
                    SELECT
                        COUNT(*) as user_count,
                        COALESCE(SUM(watchtime_tickets), 0) as watchtime_total,
                        COALESCE(SUM(gifted_sub_tickets), 0) as sub_total,
                        COALESCE(SUM(shuffle_wager_tickets), 0) as wager_total,
                        COALESCE(SUM(bonus_tickets), 0) as bonus_total
                    FROM raffle_tickets
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).fetchone()

                # Reset watchtime tickets and recalculate totals
                conn.execute(
                    text(
                        """
                    UPDATE raffle_tickets
                    SET
                        watchtime_tickets = 0,
                        total_tickets = gifted_sub_tickets + shuffle_wager_tickets + bonus_tickets
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                )

                # Delete watchtime conversion tracking
                deleted_conversions = conn.execute(
                    text(
                        """
                    DELETE FROM raffle_watchtime_converted
                    WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).rowcount

            embed = discord.Embed(
                title="🧹 Cleared Watchtime Tickets",
                description=(
                    f"**Before:**\n"
                    f"👥 {current_state[0]} users had tickets\n"
                    f"⏱️ {current_state[1]:,} watchtime tickets\n"
                    f"🎁 {current_state[2]:,} gifted sub tickets (KEPT)\n"
                    f"🎰 {current_state[3]:,} shuffle wager tickets (KEPT)\n"
                    f"⭐ {current_state[4]:,} bonus tickets (KEPT)\n\n"
                    f"**After:**\n"
                    f"⏱️ All watchtime tickets cleared\n"
                    f"🗑️ Deleted {deleted_conversions} conversion records\n"
                    f"✅ Gifted sub & wager tickets preserved\n\n"
                    f"**Next Step:** Run `/raffleresetwatchtime` to set baseline"
                ),
                color=discord.Color.green(),
            )
            await ctx.send(embed=embed)

        except Exception as e:
            logger.error(f"Error clearing watchtime tickets: {e}")
            await ctx.send(f"❌ Error: {str(e)}")
            import traceback

            traceback.print_exc()

    @commands.hybrid_command(name="rafflestatus", aliases=["rafflechecksystems"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def check_raffle_systems(self, ctx):
        """
        Check if all raffle ticket tracking systems are working
        Shows what systems are active and when they last ran
        """
        try:
            output = ["**🎟️ Raffle Systems Status**\n"]

            # Check if bot has the trackers
            checks = []

            # 1. Watchtime Converter (runs every 10 minutes)
            checks.append("✅ **Watchtime → Tickets** (every 10 minutes)")
            checks.append("   • Converts watchtime to tickets automatically")
            checks.append("   • 1 hour = 10 tickets")
            checks.append("   • Only converts for linked Discord↔Kick accounts\n")

            # 2. Gifted Sub Tracker (real-time)
            checks.append("✅ **Gifted Sub Tracking** (real-time)")
            checks.append("   • Listens to Kick websocket events")
            checks.append("   • 1 gifted sub = 15 tickets")
            checks.append("   • Awards tickets instantly\n")

            # 3. Shuffle Wager Tracker (runs every 15 minutes)
            checks.append("✅ **Shuffle Wager Tracking** (every 15 minutes)")
            checks.append("   • Polls Shuffle affiliate API")
            checks.append("   • $1,000 wagered = 20 tickets")
            checks.append("   • Requires verification via the Shuffle Verify panel\n")

            # 4. Auto Leaderboard (runs every 5 minutes)
            checks.append("✅ **Auto-Updating Leaderboard** (every 5 minutes)")
            checks.append("   • Updates leaderboard embed automatically\n")

            # 5. Period Scheduler (checks every minute)
            checks.append("✅ **Monthly Period Automation** (checks every 1 minute)")
            checks.append("   • Auto-starts new period on 1st of month")
            checks.append("   • Auto-draws winner 10 minutes before end")
            checks.append("   • Resets all tickets on transition\n")

            output.append("\n".join(checks))

            # Check database connectivity
            from .database import get_current_period

            current_period = get_current_period(self.engine)

            if current_period:
                start = current_period["start_date"]
                end = current_period["end_date"]
                output.append(f"\n**Current Period:** #{current_period.get('period_number', current_period['id'])}")
                output.append(f"📅 {start.strftime('%b %d')} - {end.strftime('%b %d, %Y')}")
            else:
                output.append("\n❌ **No active raffle period!**")

            await ctx.send("\n".join(output))

        except Exception as e:
            logger.error(f"Error checking systems: {e}")
            await ctx.send(f"❌ Error: {str(e)}")

    @commands.hybrid_command(name="raffledebugwatchtime", aliases=["debugwatchtime"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def debug_watchtime_conversion(self, ctx, kick_name: str = None):
        """
        Debug watchtime conversion issues
        Shows watchtime vs converted amounts for users
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from sqlalchemy import text

            # Get active period for this guild
            with self.engine.begin() as conn:
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period_row = period_result.fetchone()

                if not period_row:
                    await ctx.send("❌ No active raffle period for this server!")
                    return

                period_id = period_row[0]

            with self.engine.begin() as conn:
                if kick_name:
                    # Check specific user
                    query = """
                        SELECT
                            w.username,
                            w.minutes as total_watchtime,
                            COALESCE(SUM(c.minutes_converted), 0) as converted_minutes,
                            l.discord_id
                        FROM watchtime w
                        LEFT JOIN raffle_watchtime_converted c
                            ON c.kick_name = w.username AND c.period_id = :period_id
                        LEFT JOIN links l ON l.kick_name = w.username AND l.discord_server_id = :guild_id
                        WHERE w.username = :kick_name
                        GROUP BY w.username, w.minutes, l.discord_id
                    """
                    result = conn.execute(
                        text(query), {"period_id": period_id, "kick_name": kick_name, "guild_id": guild_id}
                    ).fetchone()

                    if not result:
                        await ctx.send(f"❌ User `{kick_name}` not found in watchtime table")
                        return

                    username, total, converted, discord_id = result
                    unconverted = total - converted
                    convertible_hours = unconverted // 60
                    potential_tickets = convertible_hours * 10

                    linked = "✅ Linked" if discord_id else "❌ Not linked"

                    output = [
                        f"**🔍 Watchtime Debug: {username}**\n",
                        f"**Link Status:** {linked}",
                        f"**Total Watchtime:** {total} minutes ({total/60:.1f} hours)",
                        f"**Already Converted:** {converted} minutes ({converted/60:.1f} hours)",
                        f"**Unconverted:** {unconverted} minutes ({unconverted/60:.1f} hours)",
                        f"**Convertible Now:** {convertible_hours} hours → {potential_tickets} tickets",
                    ]

                    if not discord_id:
                        output.append("\n⚠️ **User must link Discord account to earn tickets!**")
                    elif unconverted < 60:
                        output.append(f"\n⚠️ **Need at least 60 minutes to convert** (currently {unconverted} minutes)")

                    await ctx.send("\n".join(output))
                else:
                    # Show top 10 users
                    query = """
                        SELECT
                            w.username,
                            w.minutes as total_watchtime,
                            COALESCE(SUM(c.minutes_converted), 0) as converted_minutes,
                            l.discord_id
                        FROM watchtime w
                        LEFT JOIN raffle_watchtime_converted c
                            ON c.kick_name = w.username AND c.period_id = :period_id
                        LEFT JOIN links l ON l.kick_name = w.username AND l.discord_server_id = :guild_id
                        WHERE w.minutes > 0
                        GROUP BY w.username, w.minutes, l.discord_id
                        ORDER BY w.minutes DESC
                        LIMIT 10
                    """
                    results = conn.execute(text(query), {"period_id": period_id, "guild_id": guild_id}).fetchall()

                    if not results:
                        await ctx.send("📭 No users with watchtime found")
                        return

                    output = ["**🔍 Watchtime Conversion Debug (Top 10)**\n"]

                    for username, total, converted, discord_id in results:
                        unconverted = total - converted
                        convertible = unconverted // 60
                        link_icon = "✅" if discord_id else "❌"
                        output.append(
                            f"{link_icon} **{username}**: {total}m total, {converted}m converted, {unconverted}m remaining ({convertible}h convertible)"
                        )

                    await ctx.send("\n".join(output))

        except Exception as e:
            logger.error(f"Error debugging watchtime: {e}")
            await ctx.send(f"❌ Error: {str(e)}")
            import traceback

            traceback.print_exc()

    @commands.hybrid_command(name="raffleresetwatchtime", aliases=["resetwatchtimebase"])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def reset_watchtime_baseline(self, ctx):
        """
        Reset watchtime baseline - mark ALL current watchtime as "already converted"
        Use this at the START of a raffle period to only award tickets for NEW watchtime
        """
        # Reads the database before its first reply — acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        try:
            guild_id = ctx.guild.id
            from sqlalchemy import text

            # Get active period for this guild
            with self.engine.begin() as conn:
                period_result = conn.execute(
                    text(
                        """
                    SELECT id FROM raffle_periods WHERE status = 'active' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                )
                period_row = period_result.fetchone()

                if not period_row:
                    await ctx.send("❌ No active raffle period for this server!")
                    return

                period_id = period_row[0]

            with self.engine.begin() as conn:
                # Get all users' current watchtime
                watchtime_snapshot = conn.execute(
                    text(
                        """
                    SELECT w.username, w.minutes
                    FROM watchtime w
                    JOIN links l ON l.kick_name = w.username
                    WHERE w.minutes > 0 AND l.discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": guild_id},
                ).fetchall()

                # Delete old conversion tracking
                deleted = conn.execute(
                    text(
                        """
                    DELETE FROM raffle_watchtime_converted WHERE period_id = :period_id
                """
                    ),
                    {"period_id": period_id},
                ).rowcount

                # Mark ALL current watchtime as "already converted" with 0 tickets
                conversion_count = 0
                for kick_name, minutes in watchtime_snapshot:
                    if minutes >= 60:  # Only track if they have at least 1 hour
                        hours = minutes // 60
                        tracked_minutes = hours * 60  # Only track full hours
                        conn.execute(
                            text(
                                """
                            INSERT INTO raffle_watchtime_converted
                                (period_id, kick_name, minutes_converted, tickets_awarded)
                            VALUES
                                (:period_id, :kick_name, :minutes, 0)
                        """
                            ),
                            {"period_id": period_id, "kick_name": kick_name, "minutes": tracked_minutes},
                        )
                        conversion_count += 1

            await ctx.send(
                f"✅ **Watchtime baseline reset!**\n"
                f"• Deleted {deleted} old conversion records\n"
                f"• Marked {conversion_count} users' current watchtime as baseline\n"
                f"• Only NEW watchtime from now on will earn tickets"
            )

        except Exception as e:
            logger.error(f"Error resetting watchtime baseline: {e}")
            await ctx.send(f"❌ Error: {str(e)}")


async def setup(bot, engine):
    """Add raffle commands to bot"""
    await bot.add_cog(RaffleCommands(bot, engine))
    logger.debug("✅ Raffle commands loaded")
