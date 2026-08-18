"""Components V2 views for the gambling games.

Every gambling response - results, live blackjack tables, the info explainers and
the seed panel - is built here as a ``LayoutView`` + ``Container``, matching the
link/shop/points panels rather than the older ``discord.Embed`` style these
replace.

Result views carry a **Verify** link button pointing at the public provably-fair
page, deep-linked to the individual bet, so a player can check any single hand
without hunting for it.
"""

from typing import List, Optional

import discord

from .blackjack import format_hand_with_value
from .double import WIN_CHANCE, WIN_MULTIPLIER
from .roll import ROLL_TABLE, format_roll_bar

# Accent colours per outcome. Gold doubles as the neutral/push tone and matches
# the points system's gold elsewhere in the bot.
WIN_GREEN = 0x22C55E
LOSS_RED = 0xEF4444
NEUTRAL_GOLD = 0xFFD700
TABLE_BLURPLE = 0x5865F2


def outcome_accent(net: int) -> int:
    """Accent colour for a finished bet, keyed on the net point change."""
    if net > 0:
        return WIN_GREEN
    if net == 0:
        return NEUTRAL_GOLD
    return LOSS_RED


def _net_line(bet: int, payout: int, net: int) -> str:
    """The money line, formatted the same way for every game."""
    sign = "+" if net >= 0 else ""
    return f"**Bet** {bet:,} -> **Returned** {payout:,}\n**Net** {sign}{net:,} points"


def _fairness_line(seeds: dict) -> str:
    """The proof summary shown on every result.

    Shows the COMMITMENT and the hash, never the live server seed: the seed stays
    withheld until the player rotates it, which is what makes the commitment
    meaningful. Truncated for readability - the full values are on the verify page.
    """
    commitment = (seeds.get("server_seed_commitment") or "")[:16]
    proof = (seeds.get("proof_hash") or "")[:16]
    return (
        f"**Nonce** `{seeds.get('nonce')}`  ·  **Client seed** `{seeds.get('client_seed')}`\n"
        f"**Seed commitment** `{commitment}...`\n"
        f"**Result hash** `{proof}...`"
    )


def _verify_row(verify_url: Optional[str], seed_label: str = "Verify this bet") -> Optional[discord.ui.ActionRow]:
    """A link button to the public verifier, or None when no URL could be built."""
    if not verify_url:
        return None
    row = discord.ui.ActionRow()
    row.add_item(discord.ui.Button(label=seed_label, url=verify_url))
    return row


def _finish(container: discord.ui.Container, verify_url: Optional[str] = None) -> discord.ui.LayoutView:
    """Wrap a container in a LayoutView, appending the verify button when present."""
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    row = _verify_row(verify_url)
    if row is not None:
        view.add_item(row)
    return view


# ---------------------------------------------------------------------------
# Result views
# ---------------------------------------------------------------------------


def roll_result_view(
    *,
    roll: int,
    label: str,
    multiplier: float,
    bet: int,
    payout: int,
    net: int,
    seeds: dict,
    verify_url: Optional[str] = None,
) -> discord.ui.LayoutView:
    """Result of a ``/roll``."""
    container = discord.ui.Container(accent_colour=outcome_accent(net))
    container.add_item(discord.ui.TextDisplay(f"## Roll - {label}"))
    container.add_item(discord.ui.TextDisplay(f"You rolled **{roll}** for **{multiplier}x**"))
    container.add_item(discord.ui.TextDisplay(f"`{format_roll_bar(roll)}`"))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_net_line(bet, payout, net)))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_fairness_line(seeds)))
    return _finish(container, verify_url)


def double_result_view(
    *,
    won: bool,
    random_value: float,
    bet: int,
    payout: int,
    net: int,
    seeds: dict,
    verify_url: Optional[str] = None,
) -> discord.ui.LayoutView:
    """Result of a ``/double``."""
    container = discord.ui.Container(accent_colour=outcome_accent(net))
    container.add_item(discord.ui.TextDisplay(f"## Double - {'You win' if won else 'You lose'}"))
    container.add_item(
        discord.ui.TextDisplay(f"Your **{bet:,}** became **{payout:,}**." if won else f"You lost **{bet:,}** points.")
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            f"**Rolled** {random_value:.2f}  ·  **Needed** under {WIN_CHANCE:.2f}\n" + _net_line(bet, payout, net)
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_fairness_line(seeds)))
    return _finish(container, verify_url)


def blackjack_table_view(
    *,
    dealer_cards: List[int],
    player_hands: List[List[int]],
    bets: List[int],
    current_hand: int,
    balance_after_bet: Optional[int],
    seeds: dict,
    components: Optional[List[discord.ui.Item]] = None,
) -> discord.ui.LayoutView:
    """The live blackjack table, shown while the hand is still in play.

    The action buttons are passed in by the view so the buttons stay owned by
    ``BlackjackView`` (which needs them as bound callbacks) while their layout
    lives here.
    """
    container = discord.ui.Container(accent_colour=TABLE_BLURPLE)
    container.add_item(discord.ui.TextDisplay("## Blackjack"))
    container.add_item(discord.ui.TextDisplay(f"**Dealer**\n{format_hand_with_value(dealer_cards, hide_second=True)}"))
    container.add_item(discord.ui.Separator())

    for i, (hand, bet) in enumerate(zip(player_hands, bets)):
        if len(player_hands) > 1:
            marker = "> " if i == current_hand else ""
            label = f"{marker}**Hand {i + 1}** (bet {bet:,})"
        else:
            label = f"**Your hand** (bet {bet:,})"
        container.add_item(discord.ui.TextDisplay(f"{label}\n{format_hand_with_value(hand)}"))

    if balance_after_bet is not None:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"Balance: **{balance_after_bet:,}** points"))

    view = discord.ui.LayoutView(timeout=180)
    view.add_item(container)
    if components:
        row = discord.ui.ActionRow()
        for item in components:
            row.add_item(item)
        view.add_item(row)
    return view


def blackjack_result_view(
    *,
    dealer_cards: List[int],
    player_hands: List[List[int]],
    bets: List[int],
    results: List[tuple],
    total_payout: int,
    net: int,
    seeds: dict,
    verify_url: Optional[str] = None,
    headline: Optional[str] = None,
) -> discord.ui.LayoutView:
    """The finished blackjack hand, dealer revealed and every hand settled."""
    total_bet = sum(bets)
    container = discord.ui.Container(accent_colour=outcome_accent(net))
    container.add_item(discord.ui.TextDisplay(f"## Blackjack - {headline or 'Game over'}"))
    container.add_item(discord.ui.TextDisplay(f"**Dealer**\n{format_hand_with_value(dealer_cards)}"))
    container.add_item(discord.ui.Separator())

    for i, ((payout, _mult, outcome_str), hand, bet) in enumerate(zip(results, player_hands, bets)):
        label = f"Hand {i + 1}" if len(player_hands) > 1 else "Your hand"
        delta = payout - bet
        sign = "+" if delta >= 0 else ""
        container.add_item(
            discord.ui.TextDisplay(
                f"**{label} - {outcome_str}**\n{format_hand_with_value(hand)}\nBet {bet:,} -> {sign}{delta:,}"
            )
        )

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_net_line(total_bet, total_payout, net)))
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_fairness_line(seeds)))
    return _finish(container, verify_url)


def error_view(message: str, *, title: str = "Cannot place that bet") -> discord.ui.LayoutView:
    """A refusal (bad bet, no balance, wrong channel, not linked)."""
    container = discord.ui.Container(accent_colour=LOSS_RED)
    container.add_item(discord.ui.TextDisplay(f"## {title}"))
    container.add_item(discord.ui.TextDisplay(message))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


# ---------------------------------------------------------------------------
# Seed panel (/seed)
# ---------------------------------------------------------------------------


def seed_view(
    *,
    commitment: str,
    client_seed: str,
    nonce: int,
    revealed: Optional[dict] = None,
    fair_url: Optional[str] = None,
) -> discord.ui.LayoutView:
    """The player's provably-fair seed state, and any seed just revealed."""
    container = discord.ui.Container(accent_colour=NEUTRAL_GOLD)
    container.add_item(discord.ui.TextDisplay("## Your provably fair seeds"))

    if revealed:
        container.add_item(
            discord.ui.TextDisplay(
                "**Previous seed revealed**\n"
                f"Server seed `{revealed['server_seed']}`\n"
                f"Commitment `{revealed['server_seed_commitment']}`\n"
                f"Client seed `{revealed['client_seed']}`  ·  Bets played: **{revealed['nonce']}**\n"
                "Check `SHA256(server seed)` against the commitment - they must match, "
                "which proves the seed was fixed before those bets were played."
            )
        )
        container.add_item(discord.ui.Separator())

    container.add_item(
        discord.ui.TextDisplay(
            "**Active seed**\n"
            f"Seed commitment `{commitment}`\n"
            f"Client seed `{client_seed}`\n"
            f"Bets on this seed: **{nonce}**"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "The server seed behind that commitment stays hidden while you are betting - "
            "that is the point. It is published the moment you rotate, and every bet you "
            "played under it can then be recomputed and checked.\n\n"
            "`/seed set` picks your own client seed  ·  `/seed rotate` reveals the current "
            "server seed and issues a new one"
        )
    )

    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    if fair_url:
        row = discord.ui.ActionRow()
        row.add_item(discord.ui.Button(label="Verify your bets", url=fair_url))
        view.add_item(row)
    return view


# ---------------------------------------------------------------------------
# Info explainers (/bj info, /roll info, /double info)
# ---------------------------------------------------------------------------

_FAIRNESS_EXPLAINER = (
    "**Provably fair**\n"
    "Before you bet, the bot commits to a secret server seed by publishing "
    "`SHA256(server seed)`. Each bet is decided by "
    "`SHA256(server seed:client seed:nonce)`, where the client seed is yours to "
    "choose and the nonce counts up once per bet.\n"
    "Because the commitment is published first, the server seed cannot be changed "
    "afterwards to alter a result. Run `/seed rotate` to reveal it and verify every "
    "bet you played under it."
)


def _info_container(title: str) -> discord.ui.Container:
    container = discord.ui.Container(accent_colour=NEUTRAL_GOLD)
    container.add_item(discord.ui.TextDisplay(f"## {title}"))
    return container


def blackjack_info_view(fair_url: Optional[str] = None) -> discord.ui.LayoutView:
    """``/bj info`` - how blackjack works here."""
    container = _info_container("How Blackjack works")
    container.add_item(
        discord.ui.TextDisplay(
            "Beat the dealer's hand without going over 21. Run `/bj <amount>` to play - "
            "your bet is taken up front and winnings are paid when the hand settles."
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Card values**\n"
            "- Number cards are worth their face value\n"
            "- J, Q, K are worth 10\n"
            "- An Ace is 11, or 1 when 11 would bust you (a hand using an Ace as 11 is 'soft')"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Your options**\n"
            "- **Hit** - take another card\n"
            "- **Stand** - keep your total and pass to the dealer\n"
            "- **Double** - double your bet, take exactly one more card, then stand "
            "(first two cards only)\n"
            "- **Split** - with a pair, split it into two hands, each with its own bet "
            "matching the first. Split Aces get one card each and stand automatically"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Dealer rules and payouts**\n"
            "- The dealer draws to 16 and stands on all 17s\n"
            "- A win pays **2x** your bet (you get your stake back plus the same again)\n"
            "- Blackjack - an Ace with a ten-value card on the first two cards - pays **2.5x**\n"
            "- A push returns your bet\n"
            "- Going over 21 busts and loses immediately, even if the dealer busts later"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_FAIRNESS_EXPLAINER))
    container.add_item(
        discord.ui.TextDisplay(
            "The full 52-card deck is shuffled from that hash before the first card is "
            "dealt, so the whole shoe is fixed in advance and no card can be chosen "
            "against you mid-hand."
        )
    )
    return _finish(container, fair_url)


def roll_info_view(fair_url: Optional[str] = None) -> discord.ui.LayoutView:
    """``/roll info`` - the payout table."""
    container = _info_container("How Roll works")
    container.add_item(
        discord.ui.TextDisplay(
            "Run `/roll <amount>` to roll a number from **1 to 100**. Your payout depends "
            "on how close the roll lands to either end - the extremes pay best and the "
            "middle pays nothing."
        )
    )
    container.add_item(discord.ui.Separator())

    table = "\n".join(f"- **{rng}** - {mult}x  ({label})" for rng, mult, label in ROLL_TABLE)
    container.add_item(discord.ui.TextDisplay(f"**Payout table**\n{table}"))
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**Reading the payout**\n"
            "The multiplier applies to your bet, and your bet is already staked. "
            "**1x returns exactly what you staked**, above 1x is a profit, below 1x is a "
            "partial loss, and 0x loses the bet."
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_FAIRNESS_EXPLAINER))
    container.add_item(
        discord.ui.TextDisplay(
            "The first 8 characters of that hash become a number from 0.00 to 99.99, "
            "which maps to your roll of 1-100."
        )
    )
    return _finish(container, fair_url)


def double_info_view(fair_url: Optional[str] = None) -> discord.ui.LayoutView:
    """``/double info`` - the flat-odds game."""
    container = _info_container("How Double works")
    container.add_item(
        discord.ui.TextDisplay(
            f"Run `/double <amount>` for a single flat bet: a **{WIN_CHANCE:.0f}%** chance to "
            f"win **{WIN_MULTIPLIER:.0f}x** your stake, and an **{100 - WIN_CHANCE:.0f}%** "
            "chance to lose it."
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(
        discord.ui.TextDisplay(
            "**How a bet resolves**\n"
            f"- A number from 0.00 to 99.99 is drawn for your bet\n"
            f"- Under **{WIN_CHANCE:.2f}** wins and pays **{WIN_MULTIPLIER:.0f}x**\n"
            f"- **{WIN_CHANCE:.2f}** or above loses the stake\n"
            "- There are no decisions to make once the bet is placed"
        )
    )
    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(_FAIRNESS_EXPLAINER))
    container.add_item(
        discord.ui.TextDisplay(
            "The drawn number comes from the first 8 characters of that hash, so you can "
            "confirm it landed where the result says it did."
        )
    )
    return _finish(container, fair_url)
