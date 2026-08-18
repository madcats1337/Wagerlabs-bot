"""Gambling commands Cog for Discord bot.

Provides `/bj`, `/roll`, `/double` (each with an `info` subcommand explaining the
game) plus `/seed` for the provably-fair seed lifecycle.

Every response is Components V2 (``LayoutView``), built in ``components.py``.
Outcomes come from the commit-reveal engine in ``provably_fair_gambling.py``,
which mirrors the raffle draw's guarantee: the server seed is committed before
any bet is placed and revealed only on rotation.
"""

import json
import logging
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from features.discord_app_commands import defer_slash_response
from utils.server_urls import get_server_public_page_url

from .blackjack import is_blackjack, play_dealer, resolve_hand
from .components import (
    blackjack_info_view,
    blackjack_result_view,
    double_info_view,
    double_result_view,
    error_view,
    roll_info_view,
    roll_result_view,
    seed_view,
)
from .double import WIN_CHANCE, calculate_double_payout
from .provably_fair_gambling import (
    generate_deck_shuffle,
    get_or_create_active_seed,
    roll_bet,
    rotate_seed,
    set_client_seed,
)
from .roll import calculate_roll_payout, random_value_to_roll
from .views import BlackjackView

logger = logging.getLogger(__name__)

# Client seeds are echoed back into Discord messages, so keep them short and
# free of anything that would break the surrounding markdown.
MAX_CLIENT_SEED_LEN = 64


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
                view=error_view(f"Gambling commands can only be used in <#{channel_id}>.", title="Wrong channel"),
                ephemeral=True,
            )
            return False
        return True

    # -------------------------------------------------------
    # Verify links
    # -------------------------------------------------------

    def _fair_url(self, guild_id: int, bet_id: Optional[int] = None) -> Optional[str]:
        """Public verifier URL, deep-linked to one bet when we have its id.

        Mirrors the raffle's `?draw=<id>` deep link so the page auto-selects and
        verifies the row instead of making the player find it.
        """
        path = "/provably-fair/winners"
        if bet_id is not None:
            path = f"{path}?bet={bet_id}"
        try:
            return get_server_public_page_url(self.engine, guild_id, path)
        except Exception as exc:
            logger.warning("Could not build verify URL for guild %s: %s", guild_id, exc)
            return None

    # -------------------------------------------------------
    # Points / user lookups
    # -------------------------------------------------------

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
    ) -> Optional[int]:
        """Record a settled bet and return its id (used for the verify deep link).

        ``server_seed`` is deliberately NOT written here: under commit-reveal the
        seed stays secret until the player rotates, and the reveal is served from
        ``gambling_seeds`` by joining on ``seed_id``. Writing it per-bet would
        publish the live seed and let a player predict their next hand.
        """
        with self.engine.begin() as conn:
            return conn.execute(
                text(
                    """
                    INSERT INTO gambling_history
                    (discord_server_id, discord_id, kick_username, game_type,
                     bet_amount, payout_amount, net_result, game_data,
                     seed_id, server_seed_commitment, client_seed, nonce,
                     proof_hash, random_value)
                    VALUES (:sid, :did, :ku, :gt, :bet, :payout, :net, :gd,
                            :seed_id, :commit, :cs, :n, :ph, :rv)
                    RETURNING id
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
                    "seed_id": seeds["seed_id"],
                    "commit": seeds["server_seed_commitment"],
                    "cs": seeds["client_seed"],
                    "n": seeds["nonce"],
                    "ph": seeds["proof_hash"],
                    "rv": seeds["random_value"],
                },
            ).scalar()

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

    async def _prepare_bet(self, ctx: commands.Context, amount: str):
        """Shared pre-flight for every game.

        Validates the channel, the amount and the balance, then deducts the stake.
        Returns ``(kick_username, bet, balance_after_bet)`` or ``None`` when the
        bet was refused - the refusal has already been sent to the player.
        """
        if not await self._check_gambling_channel(ctx):
            return None

        bet = self._parse_bet(amount)
        if bet is None:
            await ctx.reply(
                view=error_view("Enter a positive amount, for example `100` or `2.5k`.", title="Invalid bet"),
                ephemeral=True,
            )
            return None

        kick_username, points = await self._get_user_info(ctx.guild.id, ctx.author.id)
        if kick_username is None:
            await ctx.reply(
                view=error_view(
                    "Link your Kick or Twitch account first using this server's link panel.",
                    title="Account not linked",
                ),
                ephemeral=True,
            )
            return None

        if points < bet:
            await ctx.reply(
                view=error_view(
                    f"That bet is **{bet:,}** points but you have **{points:,}**.",
                    title="Not enough points",
                ),
                ephemeral=True,
            )
            return None

        await self._deduct_points(kick_username, ctx.guild.id, bet)
        return kick_username, bet, points - bet

    @staticmethod
    def _is_info(amount: Optional[str]) -> bool:
        """Whether the argument asks for the explainer rather than a bet.

        `/bj info` is modelled as a magic first argument rather than a real
        subcommand: these are hybrid commands, so `/bj <amount>` and `!bj
        <amount>` share one signature, and a true subcommand group would break
        the prefix form and change how players already type the bet.
        """
        return (amount or "").strip().lower() in {"info", "help", "?"}

    # -------------------------------------------------------
    # /bj <amount> | /bj info — Blackjack
    # -------------------------------------------------------
    @commands.hybrid_command(name="bj", aliases=["blackjack"])
    @commands.guild_only()
    @app_commands.describe(amount="Points to bet, or 'info' to see how blackjack works")
    async def cmd_blackjack(self, ctx: commands.Context, amount: str = None):
        """Play blackjack. Usage: /bj <bet_amount> or /bj info"""
        # Several DB round-trips run before the first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        if self._is_info(amount) or not amount:
            await ctx.reply(view=blackjack_info_view(self._fair_url(ctx.guild.id)), ephemeral=True)
            return

        prepared = await self._prepare_bet(ctx, amount)
        if prepared is None:
            return
        kick_username, bet, balance_after_bet = prepared

        # Provably fair: claim this bet's nonce on the player's committed seed
        # pair, then shuffle the deck from that bet's own hash.
        seeds = roll_bet(self.engine, ctx.guild.id, ctx.author.id)
        deck = generate_deck_shuffle(seeds["server_seed"], seeds["client_seed"], seeds["nonce"])

        # Deal initial cards
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
            bet_id = await self._save_history(
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

            await ctx.reply(
                view=blackjack_result_view(
                    dealer_cards=dealer_cards,
                    player_hands=[player_cards],
                    bets=[bet],
                    results=[(payout, mult, outcome)],
                    total_payout=payout,
                    net=net,
                    seeds=seeds,
                    verify_url=self._fair_url(ctx.guild.id, bet_id),
                    headline=outcome,
                ),
                ephemeral=True,
            )
            return

        # Interactive game
        guild_id = ctx.guild.id
        discord_id = ctx.author.id
        # The verify link needs the history row's id, which only exists once the
        # hand settles - so the view is handed a callback that records the bet and
        # reports the id back for the result view's button.
        state = {"bet_id": None}

        async def save_cb(game_data, payout, net):
            state["bet_id"] = await self._save_history(
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
            seeds=seeds,
            balance_after_bet=balance_after_bet,
            verify_url=None,
            save_callback=save_cb,
            points_callback=points_cb,
            deduct_callback=deduct_cb,
            balance_callback=balance_cb,
        )
        # Resolved lazily so the button carries the real bet id recorded by save_cb.
        view.verify_url_factory = lambda: self._fair_url(guild_id, state["bet_id"])

        message = await ctx.reply(view=view, ephemeral=True)
        view._message = message

    # -------------------------------------------------------
    # /roll <amount> | /roll info — Roll 1-100
    # -------------------------------------------------------
    @commands.hybrid_command(name="roll")
    @commands.guild_only()
    @app_commands.describe(amount="Points to bet, or 'info' to see the payout table")
    async def cmd_roll(self, ctx: commands.Context, amount: str = None):
        """Roll 1-100 for a multiplier. Usage: /roll <bet_amount> or /roll info"""
        # Several DB round-trips run before the first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        if self._is_info(amount) or not amount:
            await ctx.reply(view=roll_info_view(self._fair_url(ctx.guild.id)), ephemeral=True)
            return

        prepared = await self._prepare_bet(ctx, amount)
        if prepared is None:
            return
        kick_username, bet, _balance = prepared

        seeds = roll_bet(self.engine, ctx.guild.id, ctx.author.id)

        roll = random_value_to_roll(seeds["random_value"])
        payout, multiplier, label = calculate_roll_payout(bet, roll)
        net = payout - bet

        if payout > 0:
            await self._award_points(kick_username, ctx.guild.id, payout)

        game_data = {"roll": roll, "multiplier": multiplier, "label": label}
        bet_id = await self._save_history(
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

        await ctx.reply(
            view=roll_result_view(
                roll=roll,
                label=label,
                multiplier=multiplier,
                bet=bet,
                payout=payout,
                net=net,
                seeds=seeds,
                verify_url=self._fair_url(ctx.guild.id, bet_id),
            ),
            ephemeral=True,
        )

    # -------------------------------------------------------
    # /double <amount> | /double info — 20% chance to double
    # -------------------------------------------------------
    @commands.hybrid_command(name="double")
    @commands.guild_only()
    @app_commands.describe(amount="Points to bet, or 'info' to see how double works")
    async def cmd_double(self, ctx: commands.Context, amount: str = None):
        """20% chance to double your bet. Usage: /double <bet_amount> or /double info"""
        # Several DB round-trips run before the first reply - acknowledge the slash interaction first.
        await defer_slash_response(ctx)

        if self._is_info(amount) or not amount:
            await ctx.reply(view=double_info_view(self._fair_url(ctx.guild.id)), ephemeral=True)
            return

        prepared = await self._prepare_bet(ctx, amount)
        if prepared is None:
            return
        kick_username, bet, _balance = prepared

        seeds = roll_bet(self.engine, ctx.guild.id, ctx.author.id)

        payout, won = calculate_double_payout(bet, seeds["random_value"])
        net = payout - bet

        if payout > 0:
            await self._award_points(kick_username, ctx.guild.id, payout)

        game_data = {
            "won": won,
            "random_value": seeds["random_value"],
            "threshold": WIN_CHANCE,
        }
        bet_id = await self._save_history(
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

        await ctx.reply(
            view=double_result_view(
                won=won,
                random_value=seeds["random_value"],
                bet=bet,
                payout=payout,
                net=net,
                seeds=seeds,
                verify_url=self._fair_url(ctx.guild.id, bet_id),
            ),
            ephemeral=True,
        )

    # -------------------------------------------------------
    # /seed — provably fair seed lifecycle
    # -------------------------------------------------------
    @commands.hybrid_command(name="seed")
    @commands.guild_only()
    @app_commands.describe(
        action="show your seeds, set a new client seed, or rotate (reveals the current server seed)",
        value="the new client seed, when using 'set'",
    )
    async def cmd_seed(self, ctx: commands.Context, action: str = "show", value: str = None):
        """View, set or rotate your provably fair seeds. Usage: /seed show|set|rotate"""
        await defer_slash_response(ctx, ephemeral=True)

        action = (action or "show").strip().lower()
        guild_id = ctx.guild.id
        discord_id = ctx.author.id
        fair_url = self._fair_url(guild_id)

        if action == "set":
            candidate = (value or "").strip()
            if not candidate:
                await ctx.reply(
                    view=error_view(
                        "Give the seed you want, for example `/seed set mylucky123`.", title="No seed given"
                    ),
                    ephemeral=True,
                )
                return
            if len(candidate) > MAX_CLIENT_SEED_LEN:
                await ctx.reply(
                    view=error_view(
                        f"Client seeds are at most {MAX_CLIENT_SEED_LEN} characters.", title="Seed too long"
                    ),
                    ephemeral=True,
                )
                return
            if "`" in candidate or "\n" in candidate:
                await ctx.reply(
                    view=error_view("Client seeds cannot contain backticks or line breaks.", title="Invalid seed"),
                    ephemeral=True,
                )
                return

            seed = set_client_seed(self.engine, guild_id, discord_id, candidate)
            await ctx.reply(
                view=seed_view(
                    commitment=seed["server_seed_commitment"],
                    client_seed=seed["client_seed"],
                    nonce=seed["nonce"],
                    fair_url=fair_url,
                ),
                ephemeral=True,
            )
            return

        if action == "rotate":
            retired, fresh = rotate_seed(self.engine, guild_id, discord_id)
            await ctx.reply(
                view=seed_view(
                    commitment=fresh["server_seed_commitment"],
                    client_seed=fresh["client_seed"],
                    nonce=fresh["nonce"],
                    revealed=retired,
                    fair_url=fair_url,
                ),
                ephemeral=True,
            )
            return

        seed = get_or_create_active_seed(self.engine, guild_id, discord_id)
        await ctx.reply(
            view=seed_view(
                commitment=seed["server_seed_commitment"],
                client_seed=seed["client_seed"],
                nonce=seed["nonce"],
                fair_url=fair_url,
            ),
            ephemeral=True,
        )

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
            await ctx.reply("✅ Gambling channel restriction cleared. Commands work in any channel.")
        else:
            settings.set("gambling_channel_id", str(channel.id), guild_id=ctx.guild.id)
            await ctx.reply(f"✅ Gambling commands restricted to {channel.mention}.")

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
