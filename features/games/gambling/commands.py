"""
Gambling commands Cog for Discord bot.
Provides /bj, /roll, /double commands with provably fair outcomes.
"""

import json
import logging
import traceback

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from features.discord_app_commands import defer_slash_response

from .blackjack import format_hand_with_value, is_blackjack, is_bust, play_dealer, resolve_hand
from .double import WIN_CHANCE, calculate_double_payout
from .provably_fair_gambling import generate_deck_shuffle, generate_gambling_seeds
from .roll import calculate_roll_payout, format_roll_bar, random_value_to_roll
from .views import BlackjackView

logger = logging.getLogger(__name__)


class GamblingCog(commands.Cog, name="Gambling"):
    """Gambling commands using provably fair outcomes."""

    def __init__(self, bot: commands.Bot, engine):
        self.bot = bot
        self.engine = engine

    def _get_guild_settings(self, guild_id: int):
        """Get guild settings manager (imported from bot module)."""
        # Access the global get_guild_settings from bot.py
        import bot as bot_module

        return bot_module.get_guild_settings(guild_id)

    async def _check_gambling_channel(self, ctx: commands.Context) -> bool:
        """Check if the command is in the configured gambling channel. Returns True if OK."""
        settings = self._get_guild_settings(ctx.guild.id)
        channel_id = settings.get("gambling_channel_id")
        if channel_id and str(ctx.channel.id) != str(channel_id):
            await ctx.reply(
                f"🎰 Gambling commands can only be used in <#{channel_id}>.",
                delete_after=10,
            )
            return False
        return True

    async def _get_user_info(self, guild_id: int, discord_id: int):
        """
        Look up a user's kick_username and points balance.
        Returns (kick_username, current_points) or (None, None) if not linked.
        """
        with self.engine.connect() as conn:
            # Find kick_username via links table
            row = conn.execute(
                text(
                    """
                    SELECT kick_name FROM links
                    WHERE discord_id = :did AND discord_server_id = :sid
                    LIMIT 1
                """
                ),
                {"did": discord_id, "sid": guild_id},
            ).fetchone()

            if not row:
                return None, None

            kick_username = row[0]

            # Get points
            pts_row = conn.execute(
                text(
                    """
                    SELECT COALESCE(points, 0) FROM user_points
                    WHERE LOWER(kick_username) = LOWER(:ku) AND discord_server_id = :sid
                """
                ),
                {"ku": kick_username, "sid": guild_id},
            ).fetchone()

            points = int(pts_row[0]) if pts_row else 0
            return kick_username, points

    async def _deduct_points(self, kick_username: str, guild_id: int, amount: int):
        """Deduct points from user."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE user_points
                    SET points = points - :amt,
                        total_spent = total_spent + :amt,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE LOWER(kick_username) = LOWER(:ku) AND discord_server_id = :sid
                """
                ),
                {"amt": amount, "ku": kick_username, "sid": guild_id},
            )

    async def _award_points(self, kick_username: str, guild_id: int, amount: int):
        """Award points to user."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE user_points
                    SET points = points + :amt,
                        total_earned = total_earned + :amt,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE LOWER(kick_username) = LOWER(:ku) AND discord_server_id = :sid
                """
                ),
                {"amt": amount, "ku": kick_username, "sid": guild_id},
            )

    async def _save_history(
        self,
        guild_id: int,
        discord_id: int,
        kick_username: str,
        game_type: str,
        bet: int,
        payout: int,
        net: int,
        game_data: dict,
        seeds: dict,
    ):
        """Save game result to gambling_history."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO gambling_history
                    (discord_server_id, discord_id, kick_username, game_type,
                     bet_amount, payout_amount, net_result, game_data,
                     server_seed, client_seed, nonce, proof_hash, random_value)
                    VALUES (:sid, :did, :ku, :gt, :bet, :payout, :net, :gd,
                            :ss, :cs, :n, :ph, :rv)
                """
                ),
                {
                    "sid": guild_id,
                    "did": discord_id,
                    "ku": kick_username,
                    "gt": game_type,
                    "bet": bet,
                    "payout": payout,
                    "net": net,
                    "gd": json.dumps(game_data),
                    "ss": seeds["server_seed"],
                    "cs": seeds["client_seed"],
                    "n": seeds["nonce"],
                    "ph": seeds["proof_hash"],
                    "rv": seeds["random_value"],
                },
            )

    async def _get_next_game_id(self, guild_id: int) -> int:
        """Get the next game ID for provably fair seed generation."""
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT COALESCE(MAX(id), 0) + 1 FROM gambling_history
                    WHERE discord_server_id = :sid
                """
                ),
                {"sid": guild_id},
            ).fetchone()
            return row[0] if row else 1

    def _parse_bet(self, amount_str: str) -> int | None:
        """Parse a bet amount string. Returns None if invalid."""
        if not amount_str:
            return None
        # Support 'k' suffix for thousands
        amount_str = amount_str.lower().strip()
        multiplier = 1
        if amount_str.endswith("k"):
            multiplier = 1000
            amount_str = amount_str[:-1]
        try:
            val = int(float(amount_str) * multiplier)
            return val if val > 0 else None
        except (ValueError, OverflowError):
            return None

    # -------------------------------------------------------
    # /bj <amount> — Blackjack
    # -------------------------------------------------------
    @commands.hybrid_command(name="bj", aliases=["blackjack"])
    @commands.guild_only()
    async def cmd_blackjack(self, ctx: commands.Context, amount: str = None):
        """Play blackjack! Usage: /bj <bet_amount>"""
        # Several DB round-trips run before the first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        if not amount:
            await ctx.reply("Usage: `/bj <amount>` — e.g. `/bj 100`", delete_after=15)
            return

        if not await self._check_gambling_channel(ctx):
            return

        bet = self._parse_bet(amount)
        if bet is None:
            await ctx.reply("❌ Invalid bet amount.", delete_after=10)
            return

        kick_username, points = await self._get_user_info(ctx.guild.id, ctx.author.id)
        if kick_username is None:
            await ctx.reply(
                "❌ You need to link your Kick account first! Use the link panel.",
                delete_after=15,
            )
            return

        if points < bet:
            await ctx.reply(
                f"❌ Insufficient points. You have **{points:,}** points.",
                delete_after=10,
            )
            return

        # Deduct bet upfront
        await self._deduct_points(kick_username, ctx.guild.id, bet)

        # Generate provably fair seeds and deck
        game_id = await self._get_next_game_id(ctx.guild.id)
        seeds = generate_gambling_seeds(kick_username, game_id, "blackjack")
        deck = generate_deck_shuffle(seeds["server_seed"], seeds["client_seed"])

        # Deal initial cards
        deck_pos = 0
        player_cards = [deck[0], deck[2]]
        dealer_cards = [deck[1], deck[3]]
        deck_pos = 4

        # Check for immediate blackjack
        player_bj = is_blackjack(player_cards)
        dealer_bj = is_blackjack(dealer_cards)

        if player_bj or dealer_bj:
            # Instant resolve
            if not player_bj:
                dealer_cards, deck_pos = play_dealer(dealer_cards, deck, deck_pos)

            mult, outcome = resolve_hand(player_cards, dealer_cards, player_bj, dealer_bj)
            payout = int(bet * mult)
            net = payout - bet

            if payout > 0:
                await self._award_points(kick_username, ctx.guild.id, payout)

            game_data = {
                "player_hands": [[int(c) for c in player_cards]],
                "dealer_cards": [int(c) for c in dealer_cards],
                "bets": [bet],
                "results": [(payout, mult, outcome)],
                "did_split": False,
            }
            await self._save_history(
                ctx.guild.id,
                ctx.author.id,
                kick_username,
                "blackjack",
                bet,
                payout,
                net,
                game_data,
                seeds,
            )

            # Build result embed
            if net > 0:
                color = discord.Color.green()
                emoji = "🎉"
            elif net == 0:
                color = discord.Color.gold()
                emoji = "🤝"
            else:
                color = discord.Color.red()
                emoji = "💔"

            embed = discord.Embed(title=f"{emoji} Blackjack — {outcome}", color=color)
            embed.add_field(name="Dealer", value=format_hand_with_value(dealer_cards), inline=False)
            embed.add_field(name="Your Hand", value=format_hand_with_value(player_cards), inline=False)
            sign = "+" if net >= 0 else ""
            embed.add_field(name="Result", value=f"**{sign}{net:,} points**", inline=False)
            embed.set_footer(text=f"Server Seed: {seeds['server_seed'][:16]}... | Hash: {seeds['proof_hash'][:16]}...")

            await ctx.reply(embed=embed, ephemeral=True)
            return

        # Interactive game
        guild_id = ctx.guild.id
        discord_id = ctx.author.id

        async def save_cb(game_data, payout, net):
            await self._save_history(
                guild_id,
                discord_id,
                kick_username,
                "blackjack",
                bet,
                payout,
                net,
                game_data,
                seeds,
            )

        async def points_cb(amt):
            await self._award_points(kick_username, guild_id, amt)

        async def deduct_cb(amt):
            await self._deduct_points(kick_username, guild_id, amt)

        async def balance_cb():
            _, pts = await self._get_user_info(guild_id, discord_id)
            return pts or 0

        view = BlackjackView(
            player_id=ctx.author.id,
            kick_username=kick_username,
            guild_id=ctx.guild.id,
            bet=bet,
            deck=deck,
            deck_pos=deck_pos,
            player_hands=[player_cards],
            dealer_cards=dealer_cards,
            bets=[bet],
            server_seed=seeds["server_seed"],
            client_seed=seeds["client_seed"],
            nonce=seeds["nonce"],
            proof_hash=seeds["proof_hash"],
            save_callback=save_cb,
            points_callback=points_cb,
            deduct_callback=deduct_cb,
            balance_callback=balance_cb,
        )

        embed = view._build_embed()
        embed.set_footer(text=f"Bet: {bet:,} points | Balance: {points - bet:,}")
        await ctx.reply(embed=embed, view=view, ephemeral=True)

    # -------------------------------------------------------
    # /roll <amount> — Roll 1-100
    # -------------------------------------------------------
    @commands.hybrid_command(name="roll")
    @commands.guild_only()
    async def cmd_roll(self, ctx: commands.Context, amount: str = None):
        """Roll 1-100 for a multiplier! Usage: /roll <bet_amount>"""
        # Several DB round-trips run before the first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        if not amount:
            await ctx.reply(
                "Usage: `/roll <amount>` — e.g. `/roll 100`\n"
                "🎯 1 or 100 = **5x** | 🔥 2-5/96-99 = **3x** | ⭐ 6-15/86-95 = **2x**\n"
                "✅ 16-25/76-85 = **1.5x** | 📉 26-39/62-75 = **0.5x** | 💀 40-61 = **0x**",
                delete_after=20,
            )
            return

        if not await self._check_gambling_channel(ctx):
            return

        bet = self._parse_bet(amount)
        if bet is None:
            await ctx.reply("❌ Invalid bet amount.", delete_after=10)
            return

        kick_username, points = await self._get_user_info(ctx.guild.id, ctx.author.id)
        if kick_username is None:
            await ctx.reply(
                "❌ You need to link your Kick account first! Use the link panel.",
                delete_after=15,
            )
            return

        if points < bet:
            await ctx.reply(
                f"❌ Insufficient points. You have **{points:,}** points.",
                delete_after=10,
            )
            return

        # Deduct bet
        await self._deduct_points(kick_username, ctx.guild.id, bet)

        # Generate provably fair result
        game_id = await self._get_next_game_id(ctx.guild.id)
        seeds = generate_gambling_seeds(kick_username, game_id, "roll")

        roll = random_value_to_roll(seeds["random_value"])
        payout, multiplier, label = calculate_roll_payout(bet, roll)
        net = payout - bet

        # Award payout
        if payout > 0:
            await self._award_points(kick_username, ctx.guild.id, payout)

        # Save history
        game_data = {"roll": roll, "multiplier": multiplier, "label": label}
        await self._save_history(
            ctx.guild.id,
            ctx.author.id,
            kick_username,
            "roll",
            bet,
            payout,
            net,
            game_data,
            seeds,
        )

        # Build embed
        if net > 0:
            color = discord.Color.green()
        elif net == 0:
            color = discord.Color.gold()
        else:
            color = discord.Color.red()

        embed = discord.Embed(title=f"🎲 Roll — {label}", color=color)
        embed.add_field(name="Roll", value=f"**{roll}**", inline=True)
        embed.add_field(name="Multiplier", value=f"**{multiplier}x**", inline=True)
        bar = format_roll_bar(roll)
        embed.add_field(name="Position", value=f"`{bar}`", inline=False)

        sign = "+" if net >= 0 else ""
        embed.add_field(
            name="Result",
            value=f"Bet: {bet:,} → **{sign}{net:,} points**",
            inline=False,
        )
        embed.set_footer(text=f"Server Seed: {seeds['server_seed'][:16]}... | Hash: {seeds['proof_hash'][:16]}...")

        await ctx.reply(embed=embed, ephemeral=True)

    # -------------------------------------------------------
    # /double <amount> — 20% chance to double
    # -------------------------------------------------------
    @commands.hybrid_command(name="double")
    @commands.guild_only()
    async def cmd_double(self, ctx: commands.Context, amount: str = None):
        """20% chance to double your bet! Usage: /double <bet_amount>"""
        # Several DB round-trips run before the first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        if not amount:
            await ctx.reply(
                f"Usage: `/double <amount>` — e.g. `/double 100`\n"
                f"**{WIN_CHANCE:.0f}%** chance to **double** your bet!",
                delete_after=15,
            )
            return

        if not await self._check_gambling_channel(ctx):
            return

        bet = self._parse_bet(amount)
        if bet is None:
            await ctx.reply("❌ Invalid bet amount.", delete_after=10)
            return

        kick_username, points = await self._get_user_info(ctx.guild.id, ctx.author.id)
        if kick_username is None:
            await ctx.reply(
                "❌ You need to link your Kick account first! Use the link panel.",
                delete_after=15,
            )
            return

        if points < bet:
            await ctx.reply(
                f"❌ Insufficient points. You have **{points:,}** points.",
                delete_after=10,
            )
            return

        # Deduct bet
        await self._deduct_points(kick_username, ctx.guild.id, bet)

        # Generate provably fair result
        game_id = await self._get_next_game_id(ctx.guild.id)
        seeds = generate_gambling_seeds(kick_username, game_id, "double")

        payout, won = calculate_double_payout(bet, seeds["random_value"])
        net = payout - bet

        # Award payout
        if payout > 0:
            await self._award_points(kick_username, ctx.guild.id, payout)

        # Save history
        game_data = {
            "won": won,
            "random_value": seeds["random_value"],
            "threshold": WIN_CHANCE,
        }
        await self._save_history(
            ctx.guild.id,
            ctx.author.id,
            kick_username,
            "double",
            bet,
            payout,
            net,
            game_data,
            seeds,
        )

        # Build embed
        if won:
            embed = discord.Embed(
                title="🎰 Double — YOU WIN!",
                description=f"🎉 Your **{bet:,}** turned into **{payout:,}**!",
                color=discord.Color.green(),
            )
        else:
            embed = discord.Embed(
                title="🎰 Double — You Lost",
                description=f"💔 You lost **{bet:,}** points.",
                color=discord.Color.red(),
            )

        embed.add_field(
            name="Roll",
            value=f"{seeds['random_value']:.2f} (needed < {WIN_CHANCE:.2f} to win)",
            inline=False,
        )
        embed.set_footer(text=f"Server Seed: {seeds['server_seed'][:16]}... | Hash: {seeds['proof_hash'][:16]}...")

        await ctx.reply(embed=embed, ephemeral=True)

    # -------------------------------------------------------
    # /setgamblechannel — Set gambling channel (admin only)
    # -------------------------------------------------------
    @commands.hybrid_command(name="setgamblechannel", aliases=["setgambling"])
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def cmd_set_gamble_channel(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Set the channel for gambling commands. Usage: /setgamblechannel #channel (or no arg to clear)"""
        settings = self._get_guild_settings(ctx.guild.id)

        if channel is None:
            # Clear restriction
            settings.set("gambling_channel_id", "", guild_id=ctx.guild.id)
            await ctx.reply("\u2705 Gambling channel restriction cleared. Commands work in any channel.")
        else:
            settings.set("gambling_channel_id", str(channel.id), guild_id=ctx.guild.id)
            await ctx.reply(f"\u2705 Gambling commands restricted to {channel.mention}.")

    # -------------------------------------------------------
    # Error handler
    # -------------------------------------------------------
    async def cog_command_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("❌ Gambling commands can only be used in a server.", delete_after=10)
        else:
            logger.error(f"❌ Gambling command error: {error}")
            traceback.print_exc()
            await ctx.reply("❌ An error occurred. Please try again.", delete_after=10)
