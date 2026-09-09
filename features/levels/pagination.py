"""Paginated leaderboard rendering for the standing Levels panels.

The panel is ONE Components V2 message: a Container holding the banner
(MediaGallery), the entries (TextDisplay) and the pager (ActionRow), ten to a
page. The banner sits INSIDE the bordered surface and ABOVE the rows, which is
the layout an embed cannot produce — `set_image` always renders at the bottom,
and no embed slot puts a full-width image on top. The container is also full
width by construction, so the banner and the board agree without any spacer
trickery.

The rows are text rather than an image for two reasons:

  • a rendered row costs an image re-render and a re-upload on every page turn
    and every refresh, where text is a message edit;
  • rendered text is not selectable, copyable, or searchable in Discord, and
    does not scale with the reader's accessibility settings.

V2 has no column primitive (a Section stacks its Text Displays vertically and
its accessory takes only a Button or Thumbnail), so figures are wrapped in
inline-code chips: that marks them as data AND forces a monospace face, so
equal-length numbers line up down the board.

Paging is PRIVATE to the clicker. The panel is one standing message shared by
the whole channel, so editing it on a Next click would drag every other reader's
view along and fight anyone else browsing at the same time. Instead the buttons
open an ephemeral pager that only the clicker sees, and the standing message
stays on page 1 — which is also what the periodic/event refresh repaints,
without yanking the page out from under someone mid-browse.

The page buttons are STATELESS: the page number lives in the custom_id, and the
rows are re-read from the database on each click. Panel views are registered at
startup and serve every guild across restarts, so a view that closed over its
rows would break on the first redeploy (the same rule the point-shop callbacks
follow).
"""

import asyncio
import io
import logging

import discord
from sqlalchemy import text

from .cards import BannerTheme, render_banner_png
from .competition import PERIOD_LABELS, get_active_competition
from .curve import RANK_LABELS
from .prizes import describe_prize

logger = logging.getLogger(__name__)

PAGE_SIZE = 10
ACCENT_COLOR = 0xFACC15  # Wagerlabs yellow

# custom_id prefixes. The page index rides along so the handler is stateless.
LEADERBOARD_PAGE_ID = "levels_lb_page"
COMPETITION_PAGE_ID = "levels_comp_page"

# Hard cap on pages. 200 rows is far past what anyone scrolls to, and it bounds
# both the OFFSET the database sees and the page count in the label.
MAX_PAGES = 20

# Rows are joined into one TextDisplay.
NEWLINE = chr(10)

# Attachment name the panel uploads the banner under. Lives here because both
# the panel and the ephemeral pager attach it, and panel.py imports this module.
BANNER_FILENAME = "leaderboard-banner.png"


def _page_count(total: int) -> int:
    if total <= 0:
        return 1
    return min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def _page_label(page, total):
    """Reader-facing "Page 2/6" — readers count from 1, the index from 0."""
    return f"Page {page + 1}/{_page_count(total)}"


def _clamp_page(page: int, total: int) -> int:
    """Keep `page` inside the board's real bounds.

    Rows are deleted and members leave between the render that drew a button and
    the click that uses it, so a stale page index is normal rather than
    exceptional — clamp instead of erroring at the reader.
    """
    return max(0, min(page, _page_count(total) - 1))


# ── Queries ─────────────────────────────────────────────────────────────────


def fetch_leaderboard_page(engine, guild_id, page=0):
    """(rows, total, page) for the community board.

    `total` drives the page count, so it is counted rather than inferred from a
    short page — a page that happens to hold exactly PAGE_SIZE rows would
    otherwise always advertise one more page than exists.
    """
    with engine.connect() as conn:
        total = (
            conn.execute(
                text("SELECT COUNT(*) FROM user_levels WHERE discord_server_id = :guild_id"),
                {"guild_id": guild_id},
            ).scalar()
            or 0
        )
        page = _clamp_page(page, total)
        rows = (
            conn.execute(
                text(
                    """
                    SELECT discord_id, username, total_xp, messages_sent,
                           current_level, current_rank
                    FROM user_levels
                    WHERE discord_server_id = :guild_id
                    ORDER BY total_xp DESC, discord_id ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"guild_id": guild_id, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            .mappings()
            .fetchall()
        )

    start = page * PAGE_SIZE
    return (
        [
            {
                "position": start + i + 1,
                "discord_id": r["discord_id"],
                "username": r["username"],
                "total_xp": r["total_xp"],
                "messages_sent": r["messages_sent"],
                "current_level": r["current_level"],
                "current_rank": r["current_rank"],
            }
            for i, r in enumerate(rows)
        ],
        total,
        page,
    )


def fetch_competition_page(engine, competition_id, page=0):
    """(rows, total, page) for one competition's board."""
    with engine.connect() as conn:
        total = (
            conn.execute(
                text("SELECT COUNT(*) FROM level_competition_scores WHERE competition_id = :cid"),
                {"cid": competition_id},
            ).scalar()
            or 0
        )
        page = _clamp_page(page, total)
        rows = (
            conn.execute(
                text(
                    """
                    SELECT discord_id, username, xp, messages_sent
                    FROM level_competition_scores
                    WHERE competition_id = :cid
                    ORDER BY xp DESC, discord_id ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"cid": competition_id, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            .mappings()
            .fetchall()
        )

    start = page * PAGE_SIZE
    return (
        [
            {
                "position": start + i + 1,
                "discord_id": r["discord_id"],
                "username": r["username"],
                "xp": r["xp"],
                "messages_sent": r["messages_sent"],
            }
            for i, r in enumerate(rows)
        ],
        total,
        page,
    )


def leaderboard_stats(engine, guild_id):
    """Banner chips for the community board: member count and the top rank.

    Deliberately no total-XP figure — a busy server's total runs to eight digits
    and would be truncated inside a chip.
    """
    with engine.connect() as conn:
        total = (
            conn.execute(
                text("SELECT COUNT(*) FROM user_levels WHERE discord_server_id = :guild_id"),
                {"guild_id": guild_id},
            ).scalar()
            or 0
        )
        top_rank = conn.execute(
            text(
                """
                SELECT current_rank FROM user_levels
                WHERE discord_server_id = :guild_id
                ORDER BY total_xp DESC LIMIT 1
                """
            ),
            {"guild_id": guild_id},
        ).scalar()

    return [("Members", f"{total:,}"), ("Top Rank", RANK_LABELS.get(top_rank, "—") if top_rank else "—")]


def competition_stats(competition):
    """Banner chips for a competition: the three prize slots.

    An unset place still gets a chip so the three always read as a set — a
    competition with only a first prize should show that second and third are
    empty, not silently render a one-chip banner.
    """
    stats = []
    for place in (1, 2, 3):
        described = describe_prize(
            competition.get(f"prize{place}_type"),
            competition.get(f"prize{place}_amount"),
            competition.get(f"prize{place}_text"),
        )
        # "5,000 points" is too wide for a chip at this size; the label already
        # says Prize, so the unit can shorten.
        stats.append((f"#{place} Prize", described.replace(" points", " pts") if described else "—"))
    return stats


# ── Text rendering ──────────────────────────────────────────────────────────


# Column rendering.
#
# The board is a RICH EMBED with three inline fields, which is Discord's own
# column mechanism: three `inline=True` fields render side by side and Discord
# lays them out as real columns. Nothing is padded into alignment — each column
# is a separate field, so a variable-width @mention in one cannot push the
# others out of line, which is exactly what defeated every text-only attempt.
#
# Columns: "# USER" | "MESSAGES" | "XP".
#
# Users are real <@id> mentions, so they are clickable and carry the reader's
# own nickname/avatar rendering. Messages and XP sit in inline-code chips, which
# also gives those two columns a monospace face so the digits line up down each
# column.

# Discord counts a mention against the 1024-char field limit at its RAW length
# ("<@123456789012345678>" ~ 21 chars), so ten of them plus positions stays far
# inside it — but cap anyway so a future PAGE_SIZE bump cannot silently truncate.
_FIELD_LIMIT = 1024

# Rank tier -> a coloured square. Discord cannot colour inline text, and the
# tier is the one categorical value on the row, so a glyph reads it at a glance
# without spending a column.
_RANK_BADGES = {
    "platinum": "🟦",  # blue
    "gold": "🟨",  # yellow
    "silver": "⬜",  # white
    "bronze": "🟫",  # brown
}


def _rank_badge(rank) -> str:
    return _RANK_BADGES.get(rank, "⬜")


def _mention(row) -> str:
    """The member as a real Discord mention, falling back to a plain name.

    A mention renders with the reader's own view of that member (nickname,
    avatar on hover, click-through to the profile) and stays correct when they
    change their display name — which the denormalised `username` column does
    not.
    """
    discord_id = row.get("discord_id")
    if discord_id:
        return f"<@{discord_id}>"
    name = row.get("username") or "Unknown member"
    return discord.utils.escape_markdown(str(name))


def _chip(value) -> str:
    """A number in an inline-code chip.

    The chip is not decoration: it forces a monospace face, which is what makes
    the digits line up down the column without any padding.
    """
    return f"`{value:,}`" if isinstance(value, int) else f"`{value}`"


def _clip(lines):
    """Join `lines` into one field value, staying inside Discord's limit."""
    out = []
    used = 0
    for line in lines:
        cost = len(line) + 1
        if used + cost > _FIELD_LIMIT:
            break
        out.append(line)
        used += cost
    return "\n".join(out)


def _row_line(position, mention, messages, xp, badge=None):
    """One board row.

    Layout is `#N  <badge>  @user  ·  msgs  ·  xp`, with the numbers in inline
    code chips. The chips do double duty: they mark the figures as data, and
    they force a monospace face so digits of the same length line up down the
    board even though the surrounding text is proportional.

    The mention sits BEFORE the numbers rather than after because a mention's
    rendered width is Discord's to decide — anything following it inherits that
    variance, so the columns that can align are kept clear of it.
    """
    lead = f"`{position:>3}`"
    if badge:
        lead = f"{lead} {badge}"
    return f"{lead} {mention}\n{_ROW_INDENT}`{messages:>9}` msgs  ·  `{xp:>11}` XP"


# Indents the second line of a row under the first, so a row reads as one block
# rather than as two loose lines. Figure spaces survive Discord's whitespace
# collapsing; ordinary spaces do not.
_ROW_INDENT = " " * 6


def leaderboard_lines(rows):
    """One entry per member, newest-format rows for the community board."""
    return [
        _row_line(
            f"#{r['position']}",
            _mention(r),
            f"{r.get('messages_sent', 0):,}",
            f"{r.get('total_xp', 0):,}",
            badge=_rank_badge(r.get("current_rank")),
        )
        for r in rows
    ]


def competition_lines(rows):
    """One entry per entrant. No rank badge — a competition ranks on period
    activity, so the lifetime tier would be misleading here."""
    return [
        _row_line(
            f"#{r['position']}",
            _mention(r),
            f"{r.get('messages_sent', 0):,}",
            f"{r.get('xp', 0):,}",
        )
        for r in rows
    ]


# ── Components V2 board ─────────────────────────────────────────────────────
#
# The whole panel is ONE Components V2 message: a Container holding the banner
# (MediaGallery), the rows (TextDisplay) and the pager (ActionRow). Two reasons
# this beats the embed it replaces:
#
#   • the banner sits INSIDE the bordered, accent-railed surface and ABOVE the
#     rows. An embed cannot do that — set_image always renders at the bottom,
#     and no embed slot puts a full-width image on top;
#   • the container is full width by construction, so the banner and the board
#     agree without the footer-spacer hack that tried to force an embed wider.
#
# The cost is real: V2 has no column primitive (Section stacks its Text Displays
# vertically and its accessory takes only a Button or Thumbnail), so rows are
# text rather than embed fields. Numbers are chipped to keep them monospaced.


class PageButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"levels_(?P<board>lb|comp)_page:(?P<page>\d+)",
):
    """One Prev/Next button, with the target page stored in its custom_id.

    A DynamicItem rather than a persistent View: the buttons are rebuilt on
    every refresh with different page numbers, so a registered View would have
    to enumerate every (board, page) pair to match them. The template matches
    any page with one registration, and the callback re-reads that page from
    the database — so a button on a message posted before the last restart
    still works.
    """

    def __init__(self, board: str, page: int, label: str, emoji=None, disabled: bool = False):
        self.board = board
        self.page = page
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=label,
                emoji=emoji,
                disabled=disabled,
                custom_id=f"levels_{board}_page:{page}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["board"], int(match["page"]), "Page")

    async def callback(self, interaction):
        prefix = COMPETITION_PAGE_ID if self.board == "comp" else LEADERBOARD_PAGE_ID
        await handle_page_click(interaction.client, interaction, prefix, self.page)


def _pager_row(prefix, page, total):
    """Prev / page indicator / Next, with the ends disabled at the bounds."""
    pages = _page_count(total)
    board = "comp" if prefix == COMPETITION_PAGE_ID else "lb"
    return discord.ui.ActionRow(
        PageButton(board, max(0, page - 1), "Prev", emoji="◀️", disabled=page <= 0),
        # The indicator is a plain disabled Button: never clickable, so it needs
        # no callback and no dynamic template.
        discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label=_page_label(page, total),
            disabled=True,
            custom_id=f"levels_{board}_label:{page}",
        ),
        PageButton(board, min(pages - 1, page + 1), "Next", emoji="▶️", disabled=page >= pages - 1),
    )


def build_board_view(
    *,
    prefix,
    lines,
    page,
    total,
    banner_filename=None,
    banner_url=None,
    header,
    subheader=None,
    empty_text,
    footer=None,
):
    """The panel as one Components V2 LayoutView.

    `banner_filename` references an attachment on the same message; `banner_url`
    points at an already-uploaded copy (the ephemeral pager's case, so paging
    does not re-upload a few hundred KB per click).
    """
    view = discord.ui.LayoutView(timeout=None)
    container = discord.ui.Container(accent_colour=ACCENT_COLOR)

    media = banner_url or (f"attachment://{banner_filename}" if banner_filename else None)
    if media:
        container.add_item(discord.ui.MediaGallery(discord.MediaGalleryItem(media)))
    else:
        # No banner (the render failed) — a heading keeps the panel readable.
        container.add_item(discord.ui.TextDisplay(f"## {header}"))

    if subheader:
        container.add_item(discord.ui.TextDisplay(subheader))

    container.add_item(discord.ui.Separator())
    container.add_item(discord.ui.TextDisplay(NEWLINE.join(lines) if lines else empty_text))

    if total > PAGE_SIZE:
        container.add_item(discord.ui.Separator())
        container.add_item(_pager_row(prefix, page, total))

    if footer:
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(f"-# {footer}"))

    view.add_item(container)
    return view


# ── Ephemeral pager ─────────────────────────────────────────────────────────


async def _render_banner_for(bot, engine, guild_id, prefix):
    """(file, header) — the banner for an ephemeral page, or (None, header).

    Re-rendered rather than reusing the panel's uploaded copy by URL. Pointing
    an embed at that URL was the cheaper option, but `set_image` renders at the
    BOTTOM of an embed: the pager would then show the banner UNDER the columns
    while the panel shows it above, and the two would not look like the same
    board. Rendering costs ~200ms on a thread and is only paid on a click.
    """
    from .cards import BannerTheme

    getter = getattr(bot, "get_guild_settings", None)
    theme = BannerTheme()
    if callable(getter):
        try:
            theme = BannerTheme.from_settings(getter(guild_id))
        except Exception:
            pass

    try:
        if prefix == COMPETITION_PAGE_ID:
            competition = get_active_competition(engine, guild_id)
            label = PERIOD_LABELS.get((competition or {}).get("period_type"), "Activity")
            header = f"{label} Competition"
            png = await asyncio.to_thread(
                render_banner_png,
                header,
                "Top 3 most active members win prizes",
                competition_stats(competition) if competition else [],
                theme,
            )
        else:
            header = "Community Leaderboard"
            png = await asyncio.to_thread(
                render_banner_png,
                header,
                "Every member's lifetime XP",
                leaderboard_stats(engine, guild_id),
                theme,
            )
        return discord.File(io.BytesIO(png), filename=BANNER_FILENAME), header
    except Exception as e:
        # A failed banner must not cost the reader their page — the embed falls
        # back to a plain title.
        logger.warning(f"[levels] banner render failed for guild {guild_id}: {e}")
        return None, "Community Leaderboard" if prefix != COMPETITION_PAGE_ID else "Competition"


async def handle_page_click(bot, interaction, prefix, page):
    """Serve one page privately to whoever clicked.

    Ephemeral on purpose: the panel is a single message shared by the channel,
    so editing it here would move every other reader's page and let two browsers
    fight over one view. This also means a refresh landing mid-browse cannot
    yank the page away.
    """
    engine = getattr(bot, "levels_engine", None)
    if engine is None:
        await interaction.response.send_message("Leaderboard is unavailable right now.", ephemeral=True)
        return

    guild_id = interaction.guild_id

    try:
        banner, header = await _render_banner_for(bot, engine, guild_id, prefix)

        if prefix == COMPETITION_PAGE_ID:
            competition = get_active_competition(engine, guild_id)
            if not competition:
                await interaction.response.send_message("No competition is running.", ephemeral=True)
                return
            rows, total, page = fetch_competition_page(engine, competition["id"], page)
            view = build_board_view(
                prefix=prefix,
                lines=competition_lines(rows),
                page=page,
                total=total,
                banner_filename=BANNER_FILENAME if banner else None,
                header=header,
                empty_text="No activity yet this period.",
            )
        else:
            rows, total, page = fetch_leaderboard_page(engine, guild_id, page)
            view = build_board_view(
                prefix=prefix,
                lines=leaderboard_lines(rows),
                page=page,
                total=total,
                banner_filename=BANNER_FILENAME if banner else None,
                header=header,
                empty_text="No activity yet.",
            )
    except Exception as e:
        logger.error(f"[levels] page click failed for guild {guild_id}: {e}", exc_info=True)
        await interaction.response.send_message("Could not load that page.", ephemeral=True)
        return

    files = [banner] if banner else []

    # Edit the ephemeral in place when the click came from one, so a reader
    # paging through does not accumulate a message per page.
    if interaction.message and interaction.message.flags.ephemeral:
        await interaction.response.edit_message(view=view, attachments=files)
    else:
        await interaction.response.send_message(view=view, files=files, ephemeral=True)
