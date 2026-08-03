"""
Pure helpers for the wager leaderboard (Tier 4: "leaderboard_generator").

Everything here is side-effect free — no DB, no discord, no network — so the
period-window math and the shuffle baseline model can be unit tested without
importing bot.py. The SQL that feeds these lives in bot.py's
`leaderboard_sync_task` section.

Two concerns:

  • next_period_window() — where an auto-renewing period rolls to. Calendar
    windows must roll by CALENDAR months, not by their raw duration, or a
    "monthly" board drifts off the 1st a little more every renewal.
  • period_leaderboard_rows() — the shuffle baseline model
    (max(0, current_lifetime_total - period_baseline)). Mirrors the dashboard's
    utils/database.py::compute_shuffle_leaderboard so the snapshot the bot
    freezes matches exactly what the public page was showing.
"""

import calendar
from datetime import datetime, timedelta

_ONE_SECOND = timedelta(seconds=1)

# Window lengths (in calendar months) an auto-renewing board may be configured
# with: monthly, bi-monthly, quarterly, half-yearly, yearly.
_CALENDAR_SPANS = (1, 2, 3, 6, 12)


def is_month_end(dt: datetime) -> bool:
    """True when `dt` falls on the last day of its own month."""
    return dt.day == calendar.monthrange(dt.year, dt.month)[1]


def add_months(dt: datetime, months: int, *, keep_month_end: bool = False) -> datetime:
    """`dt` shifted by N calendar months, clamping the day to the target month.

    Jan 31 + 1 month -> Feb 28 (Feb 29 in a leap year). Time of day is preserved.

    `keep_month_end` makes a date that is ALREADY on the last day of its month
    land on the last day of the target month instead of on the clamped day
    number: Sep 30 + 1 month -> Oct 31, not Oct 30. Without it a window anchored
    to month-end ratchets down every time it passes a short month (31 -> 30 ->
    28) and never recovers.
    """
    index = (dt.year * 12 + dt.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if (keep_month_end and is_month_end(dt)) else min(dt.day, last_day)
    return dt.replace(year=year, month=month, day=day)


def next_period_window(start: datetime, end: datetime):
    """The next back-to-back window for an auto-renewing leaderboard period.

    Calendar-aligned windows roll by CALENDAR months so a monthly board stays
    pinned to its anchor. Adding the raw duration instead makes it walk: Aug 1 ->
    Sep 1 is 31 days, so Sep 1 + 31d lands on Oct 2, then Nov 2, then Dec 3 —
    which is the "start/end date shifts after a few periods" the monthly board
    was showing.

    Recognised shapes, in order:
      • month-end anchored — Jul 31 -> Aug 31 rolls to Aug 31 -> Sep 30 -> Oct 31
      • exclusive end      — Aug 1 00:00:00 -> Sep 1 00:00:00
      • inclusive end      — Aug 1 00:00:00 -> Aug 31 23:59:59
    Anything else (a 7-day sprint, an arbitrary window) keeps its exact duration,
    which is already correct for a non-calendar board.

    Returns (new_start, new_end).
    """
    # Month-end semantics only ever apply to a window that STARTS on a month end,
    # and are tried first so such a window doesn't ratchet down through short
    # months. A plain calendar match still wins for anything else.
    anchors = (True, False) if is_month_end(start) else (False,)
    for months in _CALENDAR_SPANS:
        for keep_month_end in anchors:
            boundary = add_months(start, months, keep_month_end=keep_month_end)
            if boundary == end:
                return end, add_months(end, months, keep_month_end=keep_month_end)
            if boundary - _ONE_SECOND == end:
                # Inclusive end: the window stops one second before the boundary,
                # so the next starts on it (and stops a second before the next).
                new_start = end + _ONE_SECOND
                return new_start, add_months(new_start, months, keep_month_end=keep_month_end) - _ONE_SECOND
    return end, end + (end - start)


def period_leaderboard_rows(totals, identity, baselines, winner_count):
    """Top-N rows for a shuffle period via the baseline model.

    Args:
        totals:    {uname_key: (username, current_lifetime_total)} — the
                   RTP-weighted totals the tracker stores in
                   `shuffle_wager_totals`.
        identity:  {uname_key: (kick_name, discord_id)} for link badges.
        baselines: {uname_key: baseline_amount} snapshotted at period start.
        winner_count: how many rows the board shows.

    The displayed value is max(0, total - baseline), so the baseline itself is
    never shown — only wagering inside the period. A user with no baseline yet is
    treated as baselined at their current total (they contribute 0 rather than
    their whole lifetime).

    $0.00 rows are KEPT, not filtered: the dashboard pads its table to
    winner_count the same way, so a frozen snapshot reproduces exactly what the
    public page displayed.

    Returns a list of (username, kick_name, discord_id, is_linked, wagered_usd).
    """
    rows = []
    for key, (username, total) in (totals or {}).items():
        baseline = (baselines or {}).get(key, total)
        wagered = round(max(0.0, total - baseline), 2)
        kick, discord_id = (identity or {}).get(key, (None, None))
        rows.append((username, kick, discord_id, discord_id is not None, wagered))
    rows.sort(key=lambda r: r[4], reverse=True)
    return rows[: winner_count or 10]
