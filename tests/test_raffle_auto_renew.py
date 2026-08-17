"""Auto-renew must not undo a manually ended raffle period.

Renewal keys off the period's SCHEDULED `end_date`, not off when it actually
stopped. So a period ended early from the dashboard ("End Current Period") or
with `!raffleend` used to be silently re-created the moment its original end
date passed — hours or days later, with the operator having explicitly stopped
the raffle. `raffle_periods.ended_manually` records the human decision and keeps
the raffle dormant until someone starts the next period.
"""

from datetime import datetime, timedelta

import pytest

from raffle_system.scheduler import RaffleScheduler


class FakeConn:
    def __init__(self, row):
        self._row = row
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((str(query), params))
        return self

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeEngine:
    def __init__(self, row):
        self.conn = FakeConn(row)

    def begin(self):
        return self.conn


def _scheduler(row, has_column=True):
    s = RaffleScheduler.__new__(RaffleScheduler)
    s.engine = FakeEngine(row)
    s.discord_server_id = 123
    s._created = []
    # Record instead of writing; the renewal decision is what's under test.
    s._create_renewal_period = lambda: s._created.append("renewed") or {"new_period_id": 1}
    s._has_ended_manually_column = lambda: has_column
    return s


PAST = datetime.now() - timedelta(days=3)
FUTURE = datetime.now() + timedelta(days=3)


def test_a_manually_ended_period_is_never_auto_renewed():
    """The reported bug: the bot re-created the period about a minute later."""
    s = _scheduler((PAST, True))
    assert s._maybe_renew_after_end() is None
    assert s._created == []


def test_a_manually_ended_period_stays_dormant_even_long_after_its_scheduled_end():
    """Ending early must not merely DELAY the renewal until end_date passes."""
    s = _scheduler((datetime.now() - timedelta(days=90), True))
    assert s._maybe_renew_after_end() is None
    assert s._created == []


def test_a_lapsed_period_still_auto_renews():
    """The feature itself must keep working for periods that just ran out."""
    s = _scheduler((PAST, False))
    result = s._maybe_renew_after_end()
    assert result is not None
    assert s._created == ["renewed"]


def test_a_period_drawn_early_waits_for_its_scheduled_end():
    """Drawn early but not manually ended: renew on schedule, not immediately."""
    s = _scheduler((FUTURE, False))
    assert s._maybe_renew_after_end() is None
    assert s._created == []


def test_no_periods_at_all_stays_dormant():
    s = _scheduler(None)
    assert s._maybe_renew_after_end() is None
    assert s._created == []


def test_a_null_ended_manually_is_treated_as_not_manual():
    """Rows predating the column read as NULL and must keep auto-renewing."""
    s = _scheduler((PAST, None))
    result = s._maybe_renew_after_end()
    assert result is not None
    assert s._created == ["renewed"]


@pytest.mark.parametrize("flag", [True, False])
def test_the_renewal_query_reads_the_manual_end_flag(flag):
    """Guard against the column being dropped from the query in a refactor."""
    s = _scheduler((PAST, flag))
    s._maybe_renew_after_end()
    sql = s.engine.conn.queries[0][0].lower()
    assert "ended_manually" in sql


# ---------------------------------------------------------------------------
# Deploy window: the bot may run before the dashboard's migration lands.
# ---------------------------------------------------------------------------


def test_a_missing_column_does_not_break_renewal():
    """Pre-migration the bot must keep auto-renewing, not error out.

    The two services deploy independently. Selecting a column that doesn't
    exist yet would raise, and check_period_transition would swallow it as
    "no renewal" for every server until the dashboard caught up.
    """
    s = _scheduler((PAST, False), has_column=False)
    result = s._maybe_renew_after_end()

    assert result is not None
    assert s._created == ["renewed"]
    # The flag is never referenced while the column is absent.
    assert "ended_manually" not in s.engine.conn.queries[0][0].lower()


def test_a_missing_column_still_respects_the_scheduled_end():
    """Degrading must not renew a period that hasn't reached its end date."""
    s = _scheduler((FUTURE, False), has_column=False)
    assert s._maybe_renew_after_end() is None
    assert s._created == []
