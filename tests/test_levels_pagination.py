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


def test_one_line_entry_per_row():
    rows = [_member(position=i + 1, discord_id=1000 + i) for i in range(10)]
    assert len(P.leaderboard_lines(rows)) == 10
    assert len(P.competition_lines([_competitor()])) == 1


def test_users_are_real_mentions():
    """A mention renders with the reader's own view of that member and stays
    correct when they rename — the denormalised username column does not."""
    line = P.leaderboard_lines([_member(discord_id=987654321)])[0]
    assert "<@987654321>" in line


def test_missing_id_falls_back_to_an_escaped_name():
    line = P.leaderboard_lines([_member(discord_id=None, username="**bold**")])[0]
    assert "<@" not in line
    assert r"\*\*bold" in line


def test_numbers_are_code_chips():
    """The chip marks the figure as data AND forces a monospace face, which is
    what lines equal-length numbers up down the board.

    V2 has no column primitive, so this is the alignment mechanism.
    """
    line = P.leaderboard_lines([_member(messages_sent=3200, total_xp=48000)])[0]
    assert "`" in line
    assert "3,200" in line and "48,000" in line


def test_a_row_is_one_line():
    """Member, message count and XP all sit on the SAME line.

    An earlier version wrapped the figures onto a second indented line, which
    read as two loose rows per member rather than one board row.
    """
    for line in P.leaderboard_lines([_member()]) + P.competition_lines([_competitor()]):
        assert chr(10) not in line


def test_numeric_chips_are_a_fixed_width():
    """Right-padded so the chips hold their width whatever the magnitude."""
    small = P.leaderboard_lines([_member(total_xp=5, messages_sent=1)])[0]
    large = P.leaderboard_lines([_member(total_xp=1_234_567, messages_sent=999_999)])[0]
    # Compare the chip run that follows the mention on each row.
    assert len(small.split(">")[-1]) == len(large.split(">")[-1])


def test_rank_badges_differ_per_tier():
    """Discord cannot colour inline text, so the tier is carried by a glyph."""
    badges = {P._rank_badge(t) for t in ("platinum", "gold", "silver", "bronze")}
    assert len(badges) == 4


def test_unknown_rank_still_renders_a_badge():
    assert P._rank_badge(None)
    assert P._rank_badge("not-a-tier")


def test_competition_rows_have_no_rank_badge():
    """A competition ranks on period activity, not lifetime tier."""
    line = P.competition_lines([_competitor()])[0]
    assert P._rank_badge("platinum") not in line


def test_position_leads_the_row():
    line = P.leaderboard_lines([_member(position=7)])[0]
    assert line.lstrip("`").lstrip().startswith("#7")


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


# ── Components V2 view ──────────────────────────────────────────────────────


def _walk(view):
    """Every item in the view's container tree, depth first."""
    found = []

    def visit(item):
        found.append(item)
        for child in getattr(item, "children", ()) or ():
            visit(child)

    for child in view.children:
        visit(child)
    return found


def _kinds(view):
    return [type(i).__name__ for i in _walk(view)]


def test_banner_is_a_media_gallery_above_the_rows():
    """The whole point of the V2 layout.

    An embed cannot do this — set_image always renders at the BOTTOM, and no
    embed slot puts a full-width image on top. A MediaGallery inside the
    Container can, and it sits inside the bordered surface rather than floating
    above it as a bare attachment.
    """
    view = P.build_board_view(
        prefix=P.LEADERBOARD_PAGE_ID,
        lines=["row"],
        page=0,
        total=1,
        banner_filename="leaderboard-banner.png",
        header="Community Leaderboard",
        empty_text="none",
    )
    kinds = _kinds(view)
    assert "MediaGallery" in kinds, "banner is not in the container"
    assert kinds.index("MediaGallery") < kinds.index("TextDisplay"), "banner must precede the rows"


def test_everything_lives_in_one_container():
    """One bordered surface, so the banner and the board read as one panel."""
    view = P.build_board_view(
        prefix=P.LEADERBOARD_PAGE_ID,
        lines=["row"],
        page=0,
        total=25,
        banner_filename="b.png",
        header="h",
        empty_text="none",
    )
    assert _kinds(view).count("Container") == 1


def test_falls_back_to_a_heading_without_a_banner():
    view = P.build_board_view(
        prefix=P.LEADERBOARD_PAGE_ID,
        lines=["row"],
        page=0,
        total=1,
        header="Community Leaderboard",
        empty_text="none",
    )
    texts = [i.content for i in _walk(view) if type(i).__name__ == "TextDisplay"]
    assert any("Community Leaderboard" in t for t in texts)


def test_pager_is_omitted_on_a_single_page():
    """Prev/Next on a one-page board is noise."""
    view = P.build_board_view(
        prefix=P.LEADERBOARD_PAGE_ID,
        lines=["a"],
        page=0,
        total=2,
        banner_filename="b.png",
        header="h",
        empty_text="none",
    )
    assert not [i for i in _walk(view) if type(i).__name__ == "PageButton"]


def test_prev_is_disabled_on_the_first_page_and_next_on_the_last():
    def buttons(page):
        view = P.build_board_view(
            prefix=P.LEADERBOARD_PAGE_ID,
            lines=["a"],
            page=page,
            total=25,
            banner_filename="b.png",
            header="h",
            empty_text="none",
        )
        return [i for i in _walk(view) if type(i).__name__ == "PageButton"]

    first = buttons(0)
    assert first[0].item.disabled is True
    assert first[1].item.disabled is False

    last = buttons(2)
    assert last[0].item.disabled is False
    assert last[1].item.disabled is True


def test_empty_board_shows_its_empty_text():
    view = P.build_board_view(
        prefix=P.LEADERBOARD_PAGE_ID,
        lines=[],
        page=0,
        total=0,
        banner_filename="b.png",
        header="h",
        empty_text="No activity yet.",
    )
    texts = [i.content for i in _walk(view) if type(i).__name__ == "TextDisplay"]
    assert any("No activity yet." in t for t in texts)


def test_view_is_dispatchable_across_restarts():
    """Buttons on a panel posted before a redeploy must still work."""
    view = P.build_board_view(
        prefix=P.LEADERBOARD_PAGE_ID,
        lines=["a"],
        page=0,
        total=25,
        banner_filename="b.png",
        header="h",
        empty_text="none",
    )
    assert view.is_dispatchable()


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
