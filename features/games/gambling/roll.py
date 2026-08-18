"""
Roll game logic.
Rolls 1-100 using provably fair random value.
Multiplier varies based on distance from the edges (1 and 100).
Middle range (40-60) = total loss.
"""

from typing import Tuple

# Multiplier brackets: (range_check_function, multiplier, label)
# Checked in order - first match wins
ROLL_BRACKETS = [
    (lambda r: r == 1 or r == 100, 5.0, "🎯 JACKPOT"),
    (lambda r: r <= 5 or r >= 96, 3.0, "🔥 Excellent"),
    (lambda r: r <= 15 or r >= 86, 2.0, "⭐ Great"),
    (lambda r: r <= 25 or r >= 76, 1.5, "✅ Good"),
    (lambda r: r <= 39 or r >= 62, 0.5, "📉 Partial Loss"),
    (lambda r: True, 0.0, "💀 Total Loss"),  # 40-61 inclusive
]

# Human-readable mirror of ROLL_BRACKETS, used by `/roll info`. Kept beside the
# brackets so the two are edited together; `test_roll_table_matches_brackets`
# fails if a range here ever stops describing what the brackets actually pay.
ROLL_TABLE = [
    ("1 or 100", 5.0, "Jackpot"),
    ("2-5 or 96-99", 3.0, "Excellent"),
    ("6-15 or 86-95", 2.0, "Great"),
    ("16-25 or 76-85", 1.5, "Good"),
    ("26-39 or 62-75", 0.5, "Partial loss"),
    ("40-61", 0.0, "Total loss"),
]


def random_value_to_roll(random_value: float) -> int:
    """
    Convert a provably fair random_value (0.00-99.99) to a roll (1-100).

    Maps linearly: 0.00 → 1, 99.99 → 100
    """
    return int(random_value) + 1


def get_roll_multiplier(roll: int) -> Tuple[float, str]:
    """
    Get the multiplier and label for a given roll value.

    Args:
        roll: Integer 1-100

    Returns:
        (multiplier, label)
    """
    for check, mult, label in ROLL_BRACKETS:
        if check(roll):
            return mult, label
    return 0.0, "💀 Total Loss"


def calculate_roll_payout(bet: int, roll: int) -> Tuple[int, float, str]:
    """
    Calculate the payout for a roll.

    Returns:
        (payout, multiplier, label)
    """
    multiplier, label = get_roll_multiplier(roll)
    payout = int(bet * multiplier)
    return payout, multiplier, label


def format_roll_bar(roll: int) -> str:
    """Create a visual bar showing where the roll landed."""
    # 20-char bar representing 1-100
    pos = max(0, min(19, (roll - 1) * 20 // 100))
    bar = list("░" * 20)
    bar[pos] = "🔴"
    return f"1 [{''.join(bar)}] 100"
