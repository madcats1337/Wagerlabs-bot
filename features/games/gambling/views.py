"""Interactive blackjack table.

Renders through the Components V2 builders in ``components.py``: a
``LayoutView`` carrying the table plus a row of action buttons. Because a
LayoutView owns its whole message, each turn rebuilds the view and swaps it in
with ``edit_message(view=...)`` - there is no embed to update separately.
"""

from typing import Optional

import discord

from .blackjack import can_double, can_split, card_rank, is_blackjack, is_bust, play_dealer, resolve_hand
from .components import blackjack_result_view, blackjack_table_view


class BlackjackView(discord.ui.LayoutView):
    """Interactive view for a blackjack game session."""

    def __init__(
        self,
        *,
        player_id: int,
        kick_username: str,
        guild_id: int,
        bet: int,
        deck: list,
        deck_pos: int,
        player_hands: list,
        dealer_cards: list,
        bets: list,
        seeds: dict,
        balance_after_bet: Optional[int],
        verify_url: Optional[str],
        save_callback,
        points_callback,
        deduct_callback,
        balance_callback,
    ):
        super().__init__(timeout=180)
        self.player_id = player_id
        self.kick_username = kick_username
        self.guild_id = guild_id
        self.original_bet = bet
        self.deck = deck
        self.deck_pos = deck_pos
        self.player_hands = player_hands  # List of lists (supports split)
        self.dealer_cards = dealer_cards
        self.bets = bets  # Bet per hand
        self.current_hand = 0  # Index into player_hands
        self.seeds = seeds
        self.balance_after_bet = balance_after_bet
        self.verify_url = verify_url
        self.save_callback = save_callback  # async fn(game_data, payout, net)
        self.points_callback = points_callback  # async fn(amount) to award points
        self.deduct_callback = deduct_callback  # async fn(amount) to deduct points
        self.balance_callback = balance_callback  # async fn() -> int current balance
        self.game_over = False
        self.did_split = False
        self._message: Optional[discord.Message] = None
        # Set by the command once the hand can be recorded: the verify deep link
        # needs the history row's id, which does not exist until settlement.
        self.verify_url_factory = None

        # Action buttons are owned here (they need bound callbacks) but laid out
        # by the component builder.
        self.hit_button = discord.ui.Button(label="Hit", style=discord.ButtonStyle.success)
        self.stand_button = discord.ui.Button(label="Stand", style=discord.ButtonStyle.secondary)
        self.double_button = discord.ui.Button(label="Double", style=discord.ButtonStyle.primary)
        self.split_button = discord.ui.Button(label="Split", style=discord.ButtonStyle.danger)
        self.hit_button.callback = self._on_hit
        self.stand_button.callback = self._on_stand
        self.double_button.callback = self._on_double
        self.split_button.callback = self._on_split

        self._render()

    # -- state helpers ----------------------------------------------------

    def _current_cards(self):
        return self.player_hands[self.current_hand]

    def _draw_card(self):
        card = self.deck[self.deck_pos]
        self.deck_pos += 1
        return card

    def _update_buttons(self):
        """Enable/disable buttons based on current game state."""
        cards = self._current_cards()
        self.hit_button.disabled = self.game_over
        self.stand_button.disabled = self.game_over
        self.double_button.disabled = self.game_over or not can_double(cards)
        # Split: only on first hand, only if pair, and haven't already split
        self.split_button.disabled = self.game_over or self.did_split or self.current_hand != 0 or not can_split(cards)

    def _render(self):
        """Rebuild this view's components for the current table state.

        A LayoutView holds its layout as children, so each turn clears and
        re-adds them rather than mutating an embed in place.
        """
        self._update_buttons()
        self.clear_items()
        built = blackjack_table_view(
            dealer_cards=self.dealer_cards,
            player_hands=self.player_hands,
            bets=self.bets,
            current_hand=self.current_hand,
            balance_after_bet=self.balance_after_bet,
            seeds=self.seeds,
            components=[self.hit_button, self.stand_button, self.double_button, self.split_button],
        )
        for item in list(built.children):
            built.remove_item(item)
            self.add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        """Auto-stand on timeout so an abandoned hand still settles.

        Without this the bet stays deducted and the hand never resolves. There is
        no interaction left to respond to, so the stored message is edited
        directly.
        """
        if self.game_over:
            return
        await self._settle()
        if self._message is not None:
            try:
                await self._message.edit(view=self._result_view())
            except Exception:
                pass

    # -- resolution -------------------------------------------------------

    async def _settle(self):
        """Play out the dealer, resolve every hand, pay out and record history."""
        self.game_over = True

        # Dealer only draws when at least one player hand is still live.
        if not all(is_bust(hand) for hand in self.player_hands):
            self.dealer_cards, self.deck_pos = play_dealer(self.dealer_cards, self.deck, self.deck_pos)

        dealer_has_bj = is_blackjack(self.dealer_cards)
        self.results = []
        total_payout = 0
        for hand, bet in zip(self.player_hands, self.bets):
            player_has_bj = is_blackjack(hand) and not self.did_split
            mult, outcome_str = resolve_hand(hand, self.dealer_cards, player_has_bj, dealer_has_bj)
            payout = int(bet * mult)
            total_payout += payout
            self.results.append((payout, mult, outcome_str))

        if total_payout > 0:
            await self.points_callback(total_payout)

        total_bet = sum(self.bets)
        self.total_payout = total_payout
        self.net = total_payout - total_bet

        game_data = {
            "player_hands": [[int(c) for c in h] for h in self.player_hands],
            "dealer_cards": [int(c) for c in self.dealer_cards],
            "bets": self.bets,
            "results": [(p, m, o) for p, m, o in self.results],
            "did_split": self.did_split,
        }
        await self.save_callback(game_data, total_payout, self.net)

    def _result_view(self) -> discord.ui.LayoutView:
        headline = self.results[0][2] if len(self.results) == 1 else "Game over"
        verify_url = self.verify_url
        if self.verify_url_factory is not None:
            # Resolved after settlement, so the link points at this exact hand.
            try:
                verify_url = self.verify_url_factory() or verify_url
            except Exception:
                pass
        return blackjack_result_view(
            dealer_cards=self.dealer_cards,
            player_hands=self.player_hands,
            bets=self.bets,
            results=self.results,
            total_payout=self.total_payout,
            net=self.net,
            seeds=self.seeds,
            verify_url=verify_url,
            headline=headline,
        )

    async def _finish_game(self, interaction: discord.Interaction):
        await self._settle()
        self.stop()
        await interaction.response.edit_message(view=self._result_view())

    async def _advance_or_finish(self, interaction: discord.Interaction):
        """Move to the next split hand, or settle when the last one is done."""
        if self.current_hand < len(self.player_hands) - 1:
            self.current_hand += 1
            self._render()
            await interaction.response.edit_message(view=self)
        else:
            await self._finish_game(interaction)

    # -- button callbacks -------------------------------------------------

    async def _on_hit(self, interaction: discord.Interaction):
        self._current_cards().append(self._draw_card())

        if is_bust(self._current_cards()):
            await self._advance_or_finish(interaction)
            return

        self._render()
        await interaction.response.edit_message(view=self)

    async def _on_stand(self, interaction: discord.Interaction):
        await self._advance_or_finish(interaction)

    async def _on_double(self, interaction: discord.Interaction):
        current_bet = self.bets[self.current_hand]
        balance = await self.balance_callback()
        if balance < current_bet:
            await interaction.response.send_message(
                f"You need **{current_bet:,}** more points to double. Balance: **{balance:,}**.",
                ephemeral=True,
            )
            return

        await self.deduct_callback(current_bet)
        self.bets[self.current_hand] *= 2
        if self.balance_after_bet is not None:
            self.balance_after_bet -= current_bet
        self._current_cards().append(self._draw_card())

        await self._advance_or_finish(interaction)

    async def _on_split(self, interaction: discord.Interaction):
        hand = self._current_cards()

        current_bet = self.bets[0]
        balance = await self.balance_callback()
        if balance < current_bet:
            await interaction.response.send_message(
                f"You need **{current_bet:,}** more points to split. Balance: **{balance:,}**.",
                ephemeral=True,
            )
            return

        await self.deduct_callback(current_bet)
        if self.balance_after_bet is not None:
            self.balance_after_bet -= current_bet

        card1, card2 = hand[0], hand[1]
        self.player_hands[0] = [card1, self._draw_card()]
        self.player_hands.append([card2, self._draw_card()])
        self.bets.append(self.bets[0])  # Same bet for second hand
        self.did_split = True

        # Split aces get one card each and stand automatically.
        if card_rank(card1) == 0:
            await self._finish_game(interaction)
            return

        self._render()
        await interaction.response.edit_message(view=self)
