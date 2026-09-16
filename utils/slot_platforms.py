"""Slot-catalog platform table map — hand-kept MIRROR of
Admin-Dashboard/utils/slot_platforms.py.

The dashboard owns the catalog sync; the bot only READS a slot table (the
!call/!sr ban check). When a casino is added to the dashboard registry, add its
key→table here too so bot-side slot reads target the right catalog.
"""

# key -> catalog table. Mirrors the dashboard registry's table_map().
SLOT_TABLE_BY_PLATFORM = {
    "shuffle": "shuffle_slots",
    "howl": "howl_slots",
    "thrill": "thrill_slots",
    "rainbet": "rainbet_slots",
    "acebet": "acebet_slots",
    "razed": "razed_slots",
    "spartans": "spartans_slots",
    "roobet": "roobet_slots",
    "stake": "stake_slots",
    "casino500": "casino500_slots",
}

SLOT_PLATFORMS = tuple(SLOT_TABLE_BY_PLATFORM)
# Wager/affiliate tracking. MIRRORS Admin-Dashboard/utils/database.py's
# WAGER_PLATFORMS — keep the two in sync (nothing enforces it, and the two
# Railway services deploy independently).
WAGER_PLATFORMS = ("shuffle", "howl", "roobet")


def slot_table_for_platform(platform):
    """Validated platform key → catalog table (safe to interpolate). Unknown →
    the shuffle table."""
    return SLOT_TABLE_BY_PLATFORM.get(platform, "shuffle_slots")
