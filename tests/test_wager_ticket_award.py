"""Ticket arithmetic for the wager tracker.

These cover the two rules that were wrong in production for months and that no
test caught: the sub-ticket remainder was discarded on every poll, and a
reporting-window rollover stranded the paid-through watermark.

Reference numbers come from the live Howl server: 5 tickets per $1,000 (so one
ticket costs $200) polled every 2 minutes, where the average observed delta was
$33.62 and only 1.32% of polls ever reached $200 on their own.
"""

from raffle_system.shuffle_tracker import compute_wager_award

RATE = 5  # tickets per $1,000 -> $200 per ticket
POLL = 33.62  # average per-poll wager delta observed in production


def _replay(deltas, rate=RATE):
    """Feed a sequence of per-poll wager increments through the award rules.

    Mirrors what the tracker does across polls: `paid_through` only advances by
    what was converted, while `prev_total` tracks the platform's reported figure.
    Returns (total_tickets, paid_through, reported_total).
    """
    paid_through = 0.0
    reported = 0.0
    tickets = 0
    for d in deltas:
        reported = round(reported + d, 2)
        award = compute_wager_award(paid_through, reported - d, reported, rate)
        if award.action == "rollover":
            paid_through = award.paid_through
            continue
        if award.action == "skip":
            continue
        tickets += award.tickets
        paid_through = award.paid_through
    return tickets, paid_through, reported


def test_single_poll_below_one_ticket_awards_nothing_but_keeps_the_remainder():
    """The old code advanced the baseline here, discarding the $90."""
    award = compute_wager_award(0.0, 0.0, 90.42, RATE)
    assert award.action == "update"
    assert award.tickets == 0
    # Nothing converted, so the paid-through mark must not move.
    assert award.paid_through == 0.0
    assert award.observed_delta == 90.42


def test_remainder_accumulates_across_polls_into_whole_tickets():
    """$200 of wagering arriving in small polls must still pay one ticket."""
    tickets, paid_through, reported = _replay([90.42, 68.0, 58.0])
    assert reported == 216.42
    assert tickets == 1
    assert paid_through == 200.0  # $16.42 stays pending


def test_realistic_poll_stream_pays_what_was_actually_wagered():
    """A user wagering $2,883.83 at 5/$1,000 is owed 14 tickets.

    Production showed exactly this case paying 1 ticket, because each ~$34 poll
    floored to zero and the remainder was dropped.
    """
    polls = [POLL] * 86  # 86 * 33.62 = $2,891.32
    tickets, _, reported = _replay(polls)
    assert reported == 2891.32
    assert tickets == int(reported / 200)  # 14
    assert tickets == 14


def test_no_movement_is_skipped():
    award = compute_wager_award(200.0, 216.42, 216.42, RATE)
    assert award.action == "skip"
    assert award.tickets == 0


def test_window_rollover_reanchors_instead_of_stranding_the_watermark():
    """Howl's date-windowed total drops when the window moves.

    The old code hit `wager_delta <= 0` and skipped WITHOUT writing, leaving
    paid_through at the pre-rollover high-water mark — which silently killed
    every award for the rest of the raffle period.
    """
    award = compute_wager_award(96377.08, 96377.08, 0.0, RATE)
    assert award.action == "rollover"
    assert award.paid_through == 0.0
    assert award.tickets == 0


def test_awards_resume_immediately_after_a_rollover():
    """Post-rollover wagering must earn from the new baseline, not from zero
    relative to the old high-water mark."""
    # Pre-rollover the viewer is paid through $96,377.
    rolled = compute_wager_award(96377.08, 96377.08, 0.0, RATE)
    paid_through = rolled.paid_through

    # New window: they wager $600 across three polls.
    tickets, paid_through, reported = 0, paid_through, 0.0
    for d in (200.0, 200.0, 200.0):
        reported = round(reported + d, 2)
        award = compute_wager_award(paid_through, reported - d, reported, RATE)
        tickets += award.tickets
        paid_through = award.paid_through

    assert tickets == 3
    assert reported == 600.0


def test_a_zero_rate_setting_cannot_divide_by_zero():
    award = compute_wager_award(0.0, 0.0, 5000.0, 0)
    assert award.action == "update"
    assert award.tickets == 100  # falls back to the 20/$1,000 default


def test_shuffle_style_rate_still_works():
    """Rate 20/$1,000 -> $50 a ticket; the Shuffle servers run this."""
    award = compute_wager_award(0.0, 0.0, 175.0, 20)
    assert award.tickets == 3
    assert award.paid_through == 150.0  # $25 pending
