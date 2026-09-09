"""Tests for the paginated Levels leaderboard (features.levels.pagination).

The page maths is the part worth pinning down: an off-by-one in the page count
or a stale page index from a button clicked minutes ago both surface as a reader
staring at an empty board, and neither is obvious from reading the code.
"""

import pytest

from features.levels import pagination as P

# ── Page counting ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "total,expected",
    [
        (0, 1),  # an empty board still has a page to show the empty text on
        (1, 1),
        (9, 1),
        (10, 1),  # exactly one full page — NOT two
        (11, 2),
        (20, 2),
        (21, 3),
        (195, 20),
        (10_000, P.MAX_PAGES),  # capped
    ],
)
def test_page_count(total, expected):
    assert P._page_count(total) == expected


@pytest.mark.parametrize(
    "page,total,expected",
    [
        (0, 0, 0),
        (5, 0, 0),  # empty board clamps to the only page
        (-3, 100, 0),  # a negative index cannot escape downwards
        (0, 100, 0),
        (9, 100, 9),
        (50, 100, 9),  # past the end clamps to the last page
        (99, 25, 2),
    ],
)
def test_clamp_page(page, total, expected):
    """A button rendered minutes ago can name a page that no longer exists."""
    assert P._clamp_page(page, total) == expected


def test_page_label_is_one_based():
    """Readers count from 1; the internal index counts from 0."""
    assert P._page_label(0, 55) == "Page 1/6"
    assert P._page_label(5, 55) == "Page 6/6"


# ── Row formatting ──────────────────────────────────────────────────────────


def _member(**over):
    row = {
        "position": 1,
        "discord_id": 42,
        "username": "streamerkid",
        "total_xp": 48000,
        "messages_sent": 3200,
        "current_level": 42,
        "current_rank": "platinum",
    }
    row.update(over)
    return row


def _competitor(**over):
    row = {"position": 1, "discord_id": 42, "username": "streamerkid", "xp": 5400, "messages_sent": 420}
    row.update(over)
    return row


def test_board_is_three_inline_columns_in_order():
    """PLACEMENT/USER, MESSAGES, XP — and inline, or they stack instead of
    sitting side by side."""
    cols = P.leaderboard_columns([_member()])
    assert [name for name, _, _ in cols] == ["# USER", "MESSAGES", "XP"]
    assert all(inline for _, _, inline in cols)


def test_competition_uses_the_same_columns():
    cols = P.competition_columns([_competitor()])
    assert [name for name, _, _ in cols] == ["# USER", "MESSAGES", "XP"]
    assert all(inline for _, _, inline in cols)


def test_every_column_has_one_entry_per_row():
    """A short column silently misaligns the whole board — row 7 of MESSAGES
    would sit beside row 8 of USER."""
    rows = [_member(position=i + 1, discord_id=1000 + i) for i in range(10)]
    for _, value, _ in P.leaderboard_columns(rows):
        assert len(value.split("\n")) == 10


def test_users_are_real_mentions():
    """A mention renders with the reader's own view of that member and stays
    correct when they rename — the denormalised username column does not."""
    _, value, _ = P.leaderboard_columns([_member(discord_id=987654321)])[0]
    assert "<@987654321>" in value


def test_missing_id_falls_back_to_an_escaped_name():
    _, value, _ = P.leaderboard_columns([_member(discord_id=None, username="**bold**")])[0]
    assert "<@" not in value
    assert r"\*\*bold" in value


def test_numbers_are_code_chips():
    """The chip forces a monospace face, which is what lines the digits up down
    each column."""
    cols = P.leaderboard_columns([_member(messages_sent=3200, total_xp=48000)])
    assert cols[1][1] == "`3,200`"
    assert cols[2][1] == "`48,000`"


def test_numbers_are_thousands_separated():
    cols = P.leaderboard_columns([_member(total_xp=1234567)])
    assert "1,234,567" in cols[2][1]


def test_position_and_rank_share_the_user_column():
    """The badge, number and name read as one unit, which frees the other two
    fields to be pure numbers."""
    _, value, _ = P.leaderboard_columns([_member(position=7, current_rank="gold")])[0]
    assert "`#7`" in value
    assert P._rank_badge("gold") in value


def test_rank_badges_differ_per_tier():
    """Discord cannot colour inline text, so the tier is carried by a glyph."""
    badges = {P._rank_badge(t) for t in ("platinum", "gold", "silver", "bronze")}
    assert len(badges) == 4


def test_unknown_rank_still_renders_a_badge():
    assert P._rank_badge(None)
    assert P._rank_badge("not-a-tier")


def test_competition_board_has_no_rank_badge():
    """A competition ranks on period activity, not lifetime tier."""
    _, value, _ = P.competition_columns([_competitor()])[0]
    assert P._rank_badge("platinum") not in value


def test_empty_board_renders_no_columns():
    assert P.leaderboard_columns([]) == []
    assert P.competition_columns([]) == []


def test_column_values_stay_within_discord_field_limit():
    """A field over 1024 chars is rejected by the API, taking the whole panel
    with it."""
    rows = [_member(position=i + 1, discord_id=10**17 + i, total_xp=999_999_999) for i in range(P.PAGE_SIZE)]
    for _, value, _ in P.leaderboard_columns(rows):
        assert len(value) <= P._FIELD_LIMIT


# ── Banner chips ────────────────────────────────────────────────────────────


def test_competition_stats_always_returns_three_slots():
    """An unset place still gets a chip, so the three read as a set."""
    stats = P.competition_stats(
        {
            "prize1_type": "usd",
            "prize1_amount": 50,
            "prize1_text": None,
            "prize2_type": None,
            "prize2_amount": None,
            "prize2_text": None,
            "prize3_type": None,
            "prize3_amount": None,
            "prize3_text": None,
        }
    )
    assert len(stats) == 3
    assert stats[0] == ("#1 Prize", "$50.00")
    assert stats[1][1] == "—"
    assert stats[2][1] == "—"


def test_competition_stats_shortens_points_for_the_chip():
    """ "5,000 points" overflows a chip; the label already says Prize."""
    stats = P.competition_stats(
        {
            "prize1_type": "points",
            "prize1_amount": 5000,
            "prize1_text": None,
            "prize2_type": "custom",
            "prize2_amount": None,
            "prize2_text": "Steam key",
            "prize3_type": None,
            "prize3_amount": None,
            "prize3_text": None,
        }
    )
    assert stats[0] == ("#1 Prize", "5,000 pts")
    assert stats[1] == ("#2 Prize", "Steam key")


# ── Embed + pager construction ──────────────────────────────────────────────


def test_attached_banner_is_not_pulled_into_the_embed():
    """The banner must stay a plain attachment, which Discord renders ABOVE the
    embed.

    An embed's own `set_image` always renders at the BOTTOM, under the fields —
    referencing the attachment there put the banner below the leaderboard rows.
    No embed slot places a full-width image above them, so the panel attaches
    the file and leaves the embed imageless.
    """
    embed = P.build_board_embed(
        columns=P.leaderboard_columns([_member()]),
        banner_filename="leaderboard-banner.png",
        header="Community Leaderboard",
        empty_text="none",
    )
    assert embed.image.url is None, "banner was pulled into the embed and renders below the rows"
    assert embed.title is None, "an attached banner carries the identity; the title would duplicate it"
    assert [f.name for f in embed.fields] == ["# USER", "MESSAGES", "XP"]
    assert all(f.inline for f in embed.fields)


def test_ephemeral_pager_may_embed_a_banner_by_url():
    """The one case where the banner does ride inside the embed."""
    embed = P.build_board_embed(
        columns=P.leaderboard_columns([_member()]),
        banner_url="https://cdn.discordapp.com/x/leaderboard-banner.png",
        header="Community Leaderboard",
        empty_text="none",
    )
    assert embed.image.url.endswith("leaderboard-banner.png")


def test_embed_falls_back_to_a_title_when_the_banner_failed():
    """An untitled embed with no image reads as a broken message."""
    embed = P.build_board_embed(
        columns=P.leaderboard_columns([_member()]),
        header="Community Leaderboard",
        empty_text="none",
    )
    assert embed.image.url is None
    assert embed.title == "Community Leaderboard"


def test_empty_board_describes_itself_instead_of_rendering_columns():
    embed = P.build_board_embed(
        columns=[],
        banner_filename="b.png",
        header="Community Leaderboard",
        empty_text="No activity yet.",
    )
    assert not embed.fields
    assert "No activity yet." in (embed.description or "")


def test_subheader_and_empty_text_both_survive():
    """The competition countdown must not be swallowed by the empty state."""
    embed = P.build_board_embed(
        columns=[],
        banner_filename="b.png",
        header="Weekly Competition",
        subheader="Ends <t:123:R>.",
        empty_text="No activity yet this period.",
    )
    assert "Ends <t:123:R>." in embed.description
    assert "No activity yet this period." in embed.description


def test_pager_has_prev_indicator_and_next():
    view = P.BoardPager(P.LEADERBOARD_PAGE_ID, page=1, total=55)
    labels = [c.label if hasattr(c, "label") else c.item.label for c in view.children]
    assert len(view.children) == 3
    assert "Page 2/6" in labels


def test_prev_is_disabled_on_the_first_page_and_next_on_the_last():
    first = P.BoardPager(P.LEADERBOARD_PAGE_ID, page=0, total=25).children
    assert first[0].item.disabled is True  # Prev
    assert first[2].item.disabled is False  # Next

    last = P.BoardPager(P.LEADERBOARD_PAGE_ID, page=2, total=25).children
    assert last[0].item.disabled is False
    assert last[2].item.disabled is True


def test_pager_is_dispatchable_across_restarts():
    """Buttons on a panel posted before a redeploy must still work."""
    assert P.BoardPager(P.LEADERBOARD_PAGE_ID, page=0, total=25).is_dispatchable()


def test_page_indicator_is_not_matched_by_the_button_template():
    """The disabled indicator must not dispatch — it has no callback."""
    view = P.BoardPager(P.LEADERBOARD_PAGE_ID, page=1, total=55)
    indicator = view.children[1]
    assert indicator.disabled is True
    assert P.PageButton.__discord_ui_compiled_template__.fullmatch(indicator.custom_id) is None


# ── Dynamic custom_id round-trip ────────────────────────────────────────────


@pytest.mark.parametrize("board", ["lb", "comp"])
@pytest.mark.parametrize("page", [0, 1, 7, 19])
def test_page_button_custom_id_round_trips(board, page):
    """The template must match what the button actually emits.

    This is the seam that breaks silently: a custom_id the template does not
    match produces a button that simply does nothing when clicked, with no error
    anywhere.
    """
    button = P.PageButton(board, page, "Next")
    match = P.PageButton.__discord_ui_compiled_template__.fullmatch(button.item.custom_id)
    assert match is not None, f"template does not match {button.item.custom_id!r}"
    assert match["board"] == board
    assert int(match["page"]) == page
