"""Tests for the wager leaderboard's period math (utils.wager_leaderboard).

Covers the two bugs these helpers were extracted for:
  • a monthly board's start/end drifting a day or three every auto-renewal
  • the top-N rows the bot freezes disagreeing with what the dashboard displayed
"""

from datetime import datetime

from utils.wager_leaderboard import (
    add_months,
    is_http_url,
    is_month_end,
    next_period_window,
    period_leaderboard_rows,
    resolve_shuffle_stats_url,
)

# ── add_months ──────────────────────────────────────────────────────────────


def test_add_months_keeps_day_and_time():
    assert add_months(datetime(2026, 8, 1), 1) == datetime(2026, 9, 1)
    assert add_months(datetime(2026, 8, 15, 13, 45, 30), 3) == datetime(2026, 11, 15, 13, 45, 30)


def test_add_months_rolls_the_year():
    assert add_months(datetime(2026, 12, 1), 1) == datetime(2027, 1, 1)
    assert add_months(datetime(2026, 3, 1), 12) == datetime(2027, 3, 1)


def test_add_months_clamps_to_the_shorter_month():
    assert add_months(datetime(2026, 1, 31), 1) == datetime(2026, 2, 28)
    assert add_months(datetime(2028, 1, 31), 1) == datetime(2028, 2, 29)  # leap year
    assert add_months(datetime(2026, 8, 31), 1) == datetime(2026, 9, 30)


def test_add_months_can_hold_a_month_end_anchor():
    # Clamping alone ratchets 31 -> 30 -> 30; keep_month_end recovers the 31st.
    assert add_months(datetime(2026, 9, 30), 1) == datetime(2026, 10, 30)
    assert add_months(datetime(2026, 9, 30), 1, keep_month_end=True) == datetime(2026, 10, 31)
    # A date that is not a month end is unaffected by the flag.
    assert add_months(datetime(2026, 9, 15), 1, keep_month_end=True) == datetime(2026, 10, 15)


def test_is_month_end():
    assert is_month_end(datetime(2026, 8, 31))
    assert is_month_end(datetime(2027, 2, 28))
    assert is_month_end(datetime(2028, 2, 29))  # leap year
    assert not is_month_end(datetime(2027, 2, 27))
    assert not is_month_end(datetime(2026, 8, 1))


# ── next_period_window ──────────────────────────────────────────────────────


def test_monthly_window_stays_pinned_to_the_first():
    """The regression: adding the raw duration walked a monthly board off the 1st."""
    start, end = datetime(2026, 8, 1), datetime(2026, 9, 1)
    for expected_start, expected_end in [
        (datetime(2026, 9, 1), datetime(2026, 10, 1)),
        (datetime(2026, 10, 1), datetime(2026, 11, 1)),
        (datetime(2026, 11, 1), datetime(2026, 12, 1)),
        (datetime(2026, 12, 1), datetime(2027, 1, 1)),
        (datetime(2027, 1, 1), datetime(2027, 2, 1)),
    ]:
        start, end = next_period_window(start, end)
        assert (start, end) == (expected_start, expected_end)


def test_monthly_window_survives_february():
    assert next_period_window(datetime(2027, 1, 1), datetime(2027, 2, 1)) == (
        datetime(2027, 2, 1),
        datetime(2027, 3, 1),
    )
    assert next_period_window(datetime(2027, 2, 1), datetime(2027, 3, 1)) == (
        datetime(2027, 3, 1),
        datetime(2027, 4, 1),
    )


def test_inclusive_end_monthly_window():
    """Aug 1 00:00:00 -> Aug 31 23:59:59 rolls to Sep 1 -> Sep 30 23:59:59."""
    assert next_period_window(datetime(2026, 8, 1), datetime(2026, 8, 31, 23, 59, 59)) == (
        datetime(2026, 9, 1),
        datetime(2026, 9, 30, 23, 59, 59),
    )


def test_quarterly_and_yearly_windows_roll_by_calendar():
    assert next_period_window(datetime(2026, 1, 1), datetime(2026, 4, 1)) == (
        datetime(2026, 4, 1),
        datetime(2026, 7, 1),
    )
    assert next_period_window(datetime(2026, 1, 1), datetime(2027, 1, 1)) == (
        datetime(2027, 1, 1),
        datetime(2028, 1, 1),
    )


def test_month_end_anchored_window_does_not_ratchet_down():
    """A month-end board must keep a monthly cadence instead of walking
    31 -> 30 -> 30 -> 28 through the short months."""
    start, end = datetime(2026, 7, 31), datetime(2026, 8, 31)
    for expected in [
        (datetime(2026, 8, 31), datetime(2026, 9, 30)),
        (datetime(2026, 9, 30), datetime(2026, 10, 31)),
        (datetime(2026, 10, 31), datetime(2026, 11, 30)),
        (datetime(2026, 11, 30), datetime(2026, 12, 31)),
        (datetime(2026, 12, 31), datetime(2027, 1, 31)),
        (datetime(2027, 1, 31), datetime(2027, 2, 28)),
        (datetime(2027, 2, 28), datetime(2027, 3, 31)),
    ]:
        start, end = next_period_window(start, end)
        assert (start, end) == expected


def test_legacy_offset_window_heals_to_midnight():
    """A board stored with a time-of-day — what a UTC+2/+3 admin picking
    Aug 1 -> Sep 1 used to be saved as, before the date pickers round-tripped in
    UTC — must not carry that offset forward forever.

    Period bounds are configured with DATE inputs, so a time component is always
    an artifact. The first rollover snaps it to midnight and every later window
    stays there, with no overlap against the period that just closed.
    """
    start, end = datetime(2026, 7, 31, 21), datetime(2026, 8, 31, 21)
    previous_end = end

    for _ in range(6):
        start, end = next_period_window(start, end)
        assert start.time() == datetime.min.time(), f"start {start} is not midnight"
        assert end.time() == datetime.min.time(), f"end {end} is not midnight"
        # Snapping must never reach BACK across the window that just ended.
        assert start >= previous_end, f"{start} overlaps previous end {previous_end}"
        assert end > start
        previous_end = end

    # Settles onto a clean 1st-of-month cadence rather than drifting.
    assert (start, end) == (datetime(2027, 2, 1), datetime(2027, 3, 1))


def test_already_midnight_window_is_untouched_by_snapping():
    """The normalization is a repair for legacy rows, not a behaviour change for
    boards that are already configured correctly."""
    assert next_period_window(datetime(2026, 8, 1), datetime(2026, 9, 1)) == (
        datetime(2026, 9, 1),
        datetime(2026, 10, 1),
    )
    assert next_period_window(datetime(2026, 7, 31), datetime(2026, 8, 31)) == (
        datetime(2026, 8, 31),
        datetime(2026, 9, 30),
    )


def test_a_genuine_short_window_is_not_read_as_month_end():
    """Feb 28 -> Mar 28 is a plain calendar month, not an end-of-month anchor."""
    assert next_period_window(datetime(2027, 2, 28), datetime(2027, 3, 28)) == (
        datetime(2027, 3, 28),
        datetime(2027, 4, 28),
    )


def test_non_calendar_window_keeps_its_exact_duration():
    # A 7-day board is not calendar-aligned; back-to-back by duration is correct.
    assert next_period_window(datetime(2026, 8, 3), datetime(2026, 8, 10)) == (
        datetime(2026, 8, 10),
        datetime(2026, 8, 17),
    )


def test_windows_are_always_back_to_back():
    start, end = datetime(2026, 8, 1), datetime(2026, 9, 1)
    for _ in range(6):
        new_start, new_end = next_period_window(start, end)
        assert new_start == end
        assert new_end > new_start
        start, end = new_start, new_end


# ── period_leaderboard_rows ─────────────────────────────────────────────────


def _totals(**users):
    return {name: (name, total) for name, total in users.items()}


def test_rows_report_wagering_since_the_baseline():
    """The baseline itself is never shown — only wagering inside the period."""
    rows = period_leaderboard_rows(
        _totals(alice=1500.0, bob=900.0),
        {"alice": ("alice_kick", 42)},
        {"alice": 1000.0, "bob": 600.0},
        winner_count=10,
    )
    assert rows == [
        ("alice", "alice_kick", 42, True, 500.0),
        ("bob", None, None, False, 300.0),
    ]


def test_rows_are_sorted_by_period_wager_and_capped():
    rows = period_leaderboard_rows(
        _totals(low=110.0, high=900.0, mid=300.0),
        {},
        {"low": 100.0, "high": 100.0, "mid": 100.0},
        winner_count=2,
    )
    assert [(r[0], r[4]) for r in rows] == [("high", 800.0), ("mid", 200.0)]


def test_a_user_with_no_baseline_contributes_zero():
    """Never show a newcomer's whole lifetime total as period wagering."""
    rows = period_leaderboard_rows(_totals(newcomer=50_000.0), {}, {}, winner_count=10)
    assert rows == [("newcomer", None, None, False, 0.0)]


def test_a_total_below_its_baseline_clamps_to_zero():
    rows = period_leaderboard_rows(_totals(alice=900.0), {}, {"alice": 1000.0}, winner_count=10)
    assert rows == [("alice", None, None, False, 0.0)]


def test_zero_rows_are_kept_so_the_freeze_matches_the_public_page():
    """The dashboard pads its table with $0.00 rows; a freeze must do the same."""
    rows = period_leaderboard_rows(
        _totals(alice=1500.0, bob=400.0),
        {},
        {"alice": 1000.0, "bob": 400.0},
        winner_count=10,
    )
    assert [(r[0], r[4]) for r in rows] == [("alice", 500.0), ("bob", 0.0)]


def test_empty_totals_produce_no_rows():
    assert period_leaderboard_rows({}, {}, {}, winner_count=10) == []
    assert period_leaderboard_rows(None, None, {}, winner_count=10) == []


# ── resolve_shuffle_stats_url ───────────────────────────────────────────────

AFFILIATE = "https://affiliate.shuffle.com/stats/affiliate-default"
PERIOD_STATS = "https://affiliate.shuffle.com/stats/leaderboard-specific"


def test_the_periods_stats_url_wins():
    assert resolve_shuffle_stats_url(PERIOD_STATS, AFFILIATE) == PERIOD_STATS


def test_a_blank_stats_url_falls_back_to_the_affiliate_url():
    for blank in ("", "   ", None):
        assert resolve_shuffle_stats_url(blank, AFFILIATE) == AFFILIATE


def test_a_malformed_stats_url_falls_back_instead_of_going_dark():
    """A typo must degrade to the Profile Settings URL, not stop tracking."""
    for bad in ("affiliate.shuffle.com/stats/x", "ftp://example.com", "not a url"):
        assert resolve_shuffle_stats_url(bad, AFFILIATE) == AFFILIATE


def test_surrounding_whitespace_is_trimmed():
    assert resolve_shuffle_stats_url(f"  {PERIOD_STATS}  ", AFFILIATE) == PERIOD_STATS
    assert resolve_shuffle_stats_url(None, f"  {AFFILIATE}  ") == AFFILIATE


def test_no_source_at_all_is_empty_so_the_poll_is_skipped():
    assert resolve_shuffle_stats_url(None, None) == ""
    assert resolve_shuffle_stats_url("", "   ") == ""


def test_is_http_url():
    assert is_http_url("http://x.test")
    assert is_http_url("HTTPS://x.test")
    assert not is_http_url("")
    assert not is_http_url(None)
    assert not is_http_url("x.test")
