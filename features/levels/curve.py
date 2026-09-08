"""Leveling curve and rank-tier mapping for the XP system.

Pure functions only - no DB, no Discord. Mirrored 1:1 in
Admin-Dashboard/utils/levels.py so the dashboard can compute "XP to next
level" for progress bars without querying the bot. Keep both copies numerically
identical if the curve or rank cutoffs ever change.

Curve: XP required to go from level N to N+1 is 5*N^2 + 50*N + 100 (a
well-tested Mee6-style formula). Cumulative XP grows quadratically, so each
level takes progressively longer than the last. Levels are uncapped; rank
tiers stop advancing at Platinum (level 41+).
"""

RANKS = ("bronze", "silver", "gold", "platinum")

RANK_LABELS = {
    "bronze": "Bronze",
    "silver": "Silver",
    "gold": "Gold",
    "platinum": "Platinum",
}

# Highest level in each rank tier (inclusive). Platinum has no ceiling.
_RANK_LEVEL_CEILING = (
    ("bronze", 10),
    ("silver", 20),
    ("gold", 40),
)


def xp_to_next(level: int) -> int:
    """XP required to advance from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def cumulative_xp_for_level(level: int) -> int:
    """Total XP required to REACH `level` from zero."""
    total = 0
    for n in range(level):
        total += xp_to_next(n)
    return total


def level_from_total_xp(total_xp: int) -> int:
    """Derive the current level from a lifetime XP total."""
    if total_xp is None or total_xp <= 0:
        return 0

    level = 0
    remaining = total_xp
    needed = xp_to_next(level)
    while remaining >= needed:
        remaining -= needed
        level += 1
        needed = xp_to_next(level)
    return level


def rank_for_level(level: int) -> str:
    """Rank tier for a given level. Uncapped levels stay Platinum past 40."""
    for rank, ceiling in _RANK_LEVEL_CEILING:
        if level <= ceiling:
            return rank
    return "platinum"


def xp_progress(total_xp: int):
    """Returns (level, xp_into_level, xp_needed_for_level, rank) for `total_xp`."""
    level = level_from_total_xp(total_xp)
    xp_into_level = total_xp - cumulative_xp_for_level(level)
    xp_needed = xp_to_next(level)
    return level, xp_into_level, xp_needed, rank_for_level(level)
