"""Paginated leaderboard rendering for the standing Levels panels.

The board used to be one rendered image of the top 10. It is now a BANNER image
(identity + headline values, see cards.render_banner_png) above the entries as
TEXT, ten to a page. Two reasons:

  • a rendered row costs an image re-render and a re-upload on every page turn
    and every refresh, where text is a message edit;
  • rendered text is not selectable, copyable, or searchable in Discord, and
    does not scale with the reader's accessibility settings.

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


def _page_count(total: int) -> int:
    if total <= 0:
        return 1
    return min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE)


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


def leaderboard_columns(rows):
    """[(field name, field value, inline)] for the community board.

    Three inline fields = three real columns. The position and the mention share
    the first field so the rank badge, number and name read as one unit.
    """
    if not rows:
        return []
    return [
        (
            "# USER",
            _clip([f"{_rank_badge(r.get('current_rank'))} `#{r['position']}` {_mention(r)}" for r in rows]),
            True,
        ),
        ("MESSAGES", _clip([_chip(r.get("messages_sent", 0)) for r in rows]), True),
        ("XP", _clip([_chip(r.get("total_xp", 0)) for r in rows]), True),
    ]


def competition_columns(rows):
    """[(field name, field value, inline)] for a competition board."""
    if not rows:
        return []
    return [
        ("# USER", _clip([f"`#{r['position']}` {_mention(r)}" for r in rows]), True),
        ("MESSAGES", _clip([_chip(r.get("messages_sent", 0)) for r in rows]), True),
        ("XP", _clip([_chip(r.get("xp", 0)) for r in rows]), True),
    ]


def _page_label(page, total):
    return f"Page {page + 1}/{_page_count(total)}"


# ── Views ───────────────────────────────────────────────────────────────────


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
    still works, which is the same statelessness the point-shop callbacks need.
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


class BoardPager(discord.ui.View):
    """Prev / page-indicator / Next beneath a leaderboard embed.

    A classic View rather than a LayoutView: a Components V2 message cannot
    carry an embed, and the embed is what gives the board real columns (three
    `inline=True` fields, laid out by Discord). Buttons alongside an embed is
    the ordinary pattern.
    """

    def __init__(self, prefix, page, total):
        super().__init__(timeout=None)
        pages = _page_count(total)
        board = "comp" if prefix == COMPETITION_PAGE_ID else "lb"

        self.add_item(PageButton(board, max(0, page - 1), "Prev", emoji="◀️", disabled=page <= 0))
        # The indicator is a plain disabled Button: never clickable, so it needs
        # no callback and no dynamic template.
        self.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=_page_label(page, total),
                disabled=True,
                custom_id=f"levels_{board}_label:{page}",
            )
        )
        self.add_item(PageButton(board, min(pages - 1, page + 1), "Next", emoji="▶️", disabled=page >= pages - 1))


def build_board_embed(
    *,
    columns,
    banner_filename=None,
    banner_url=None,
    header,
    subheader=None,
    empty_text,
    footer=None,
):
    """The board as a rich embed: banner image on top, then the columns.

    `columns` is the [(name, value, inline)] list from leaderboard_columns /
    competition_columns. Three inline fields render as three real columns —
    Discord aligns them, so a variable-width @mention in the first cannot push
    the other two out of line.

    `banner_filename` references an attachment on the SAME message (the standing
    panel, which uploads the PNG). `banner_url` points at an already-uploaded
    one — the ephemeral pager reuses the panel's image that way rather than
    re-uploading a copy per page turn.
    """
    embed = discord.Embed(colour=ACCENT_COLOR)

    image = banner_url or (f"attachment://{banner_filename}" if banner_filename else None)
    if image:
        embed.set_image(url=image)
    else:
        # No banner (the render failed) — the title carries the board instead,
        # so the panel still reads rather than posting an untitled embed.
        embed.title = header

    if subheader:
        embed.description = subheader

    if columns:
        for name, value, inline in columns:
            embed.add_field(name=name, value=value, inline=inline)
    else:
        embed.description = f"{subheader}\n\n{empty_text}" if subheader else empty_text

    if footer:
        embed.set_footer(text=footer)

    return embed


# ── Ephemeral pager ─────────────────────────────────────────────────────────


def _banner_url_from(message):
    """The banner already attached to the panel message, if it is still there.

    Reused by the ephemeral pager so paging never re-uploads the image: the
    banner is identical on every page, and a fresh upload per click would cost
    a render and a few hundred KB each time.
    """
    for attachment in getattr(message, "attachments", ()) or ():
        if attachment.filename.endswith(".png"):
            return attachment.url
    return None


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
    banner_url = _banner_url_from(interaction.message)

    try:
        if prefix == COMPETITION_PAGE_ID:
            competition = get_active_competition(engine, guild_id)
            if not competition:
                await interaction.response.send_message("No competition is running.", ephemeral=True)
                return
            rows, total, page = fetch_competition_page(engine, competition["id"], page)
            label = PERIOD_LABELS.get(competition["period_type"], "Activity")
            embed = build_board_embed(
                columns=competition_columns(rows),
                banner_url=banner_url,
                header=f"{label} Competition",
                empty_text="No activity yet this period.",
            )
        else:
            rows, total, page = fetch_leaderboard_page(engine, guild_id, page)
            embed = build_board_embed(
                columns=leaderboard_columns(rows),
                banner_url=banner_url,
                header="Community Leaderboard",
                empty_text="No activity yet.",
            )
        view = BoardPager(prefix, page, total) if total > PAGE_SIZE else None
    except Exception as e:
        logger.error(f"[levels] page click failed for guild {guild_id}: {e}", exc_info=True)
        await interaction.response.send_message("Could not load that page.", ephemeral=True)
        return

    # Edit the ephemeral in place when the click came from one, so a reader
    # paging through does not accumulate a message per page.
    if interaction.message and interaction.message.flags.ephemeral:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
