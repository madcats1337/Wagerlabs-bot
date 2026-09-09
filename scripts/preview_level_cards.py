"""Preview harness for the levels cards (/rank, level-up, rank-up, leaderboard).

Renders a fixture matrix through the REAL functions in features/levels/cards.py
and writes an index.html that shows every result together, so card design can be
iterated without running the bot or crossing a level threshold in a live guild.

Deliberately drives the real renderer rather than mocking the cards in HTML/CSS:
a mock drifts from what the bot actually posts, which is the one thing a preview
is supposed to tell you.

    python scripts/preview_level_cards.py            # -> tmp/level-card-preview/
    python scripts/preview_level_cards.py --out DIR

Edit cards.py, re-run, refresh the browser. Output lands in tmp/ (gitignored).

Avatars use Discord's public default-avatar CDN so the real fetch ->
RGBA -> resize -> circle-mask path is exercised. With no network the renderer
falls back to placeholder circles; the index header says which one you're
looking at, so an offline run can't be mistaken for a styling result.
"""

import argparse
import asyncio
import html
import os
import sys
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import aiohttp  # noqa: E402

from features.levels.cards import (  # noqa: E402
    BannerTheme,
    render_banner_png,
    render_competition_winners_card,
    render_levelup_card,
    render_rank_card,
    render_rankup_card,
)
from features.levels.curve import cumulative_xp_for_level, xp_to_next  # noqa: E402

AVATARS = [f"https://cdn.discordapp.com/embed/avatars/{i}.png" for i in range(6)]

SHORT_NAME = "Lele"
TYPICAL_NAME = "MaikeleleTV"
LONG_NAME = "ThisIsAVeryLongDiscordName32Char"  # 32 chars = Discord's maximum


def xp_at(level: int, fraction: float = 0.0) -> int:
    """Total XP placing a member at `level`, `fraction` of the way to the next."""
    return cumulative_xp_for_level(level) + int(xp_to_next(level) * fraction)


def rows(count: int):
    """A leaderboard page spanning every rank tier, newest names first."""
    specs = [
        (TYPICAL_NAME, 80, "platinum", 48213),
        ("xX_SlotGrinder_Xx", 52, "platinum", 22140),
        (LONG_NAME, 38, "gold", 12044),
        ("bonusbuyer", 27, "gold", 8320),
        ("hunt_enjoyer", 21, "gold", 5110),
        ("chatterbox", 18, "silver", 3120),
        ("Lele", 14, "silver", 1890),
        ("newcomer99", 11, "silver", 940),
        ("lurker", 6, "bronze", 410),
        (None, 1, "bronze", 22),
    ]
    out = []
    for i, (name, level, rank, msgs) in enumerate(specs[:count]):
        out.append(
            {
                "position": i + 1,
                "discord_id": 1000 + i,
                "username": name,
                "avatar_url": AVATARS[i % len(AVATARS)],
                "total_xp": xp_at(level, 0.4),
                "messages_sent": msgs,
                "current_level": level,
                "current_rank": rank,
            }
        )
    return out


def _in(**delta):
    """An instant `delta` from now (negative values are in the past)."""
    return datetime.now(timezone.utc) + timedelta(**delta)


async def _banner(title, subtitle, stats, theme=None):
    """The banner as a file-like, matching what the other factories return.

    render_banner_png is synchronous (Pillow) and returns bytes; the harness
    awaits every factory and reads `.fp`, so wrap it to match rather than
    special-casing the caller.
    """
    import io as _io

    png = render_banner_png(title, subtitle, stats, theme=theme)
    return type("_Rendered", (), {"fp": _io.BytesIO(png)})()


def _comp_rows(count: int):
    """Competition board rows — note `xp` is per-period, not lifetime."""
    specs = [
        (TYPICAL_NAME, 18240, 912),
        ("xX_SlotGrinder_Xx", 15110, 780),
        (LONG_NAME, 12980, 664),
        ("bonusbuyer", 8120, 420),
        (None, 340, 19),
    ]
    return [
        {
            "position": i + 1,
            "discord_id": 2000 + i,
            "username": name,
            "avatar_url": AVATARS[i % len(AVATARS)],
            "xp": xp,
            "messages_sent": msgs,
        }
        for i, (name, xp, msgs) in enumerate(specs[:count])
    ]


def _winners(count: int):
    """Frozen winner rows, one per prize type."""
    specs = [
        (TYPICAL_NAME, 18240, "$50.00"),
        ("xX_SlotGrinder_Xx", 15110, "2,500 points"),
        (LONG_NAME, 12980, "Steam key"),
    ]
    return [
        {
            "place": i + 1,
            "discord_id": 2000 + i,
            "username": name,
            "avatar_url": AVATARS[i % len(AVATARS)],
            "xp": xp,
            "prize": prize,
        }
        for i, (name, xp, prize) in enumerate(specs[:count])
    ]


# (slug, caption, zero-arg factory returning the render coroutine)
GROUPS = [
    (
        "Rank card",
        "What /rank replies with. Progress bar is XP into the current level over the cost of the next.",
        [
            (
                "rank-bronze",
                "Bronze · level 6 · 40% · #42",
                lambda: render_rank_card(TYPICAL_NAME, AVATARS[0], xp_at(6, 0.4), 42),
            ),
            (
                "rank-silver",
                "Silver · level 14 · 70% · #12",
                lambda: render_rank_card("chatterbox", AVATARS[1], xp_at(14, 0.7), 12),
            ),
            (
                "rank-gold",
                "Gold · level 21 · 49% · #3",
                lambda: render_rank_card("hunt_enjoyer", AVATARS[2], xp_at(21, 0.49), 3),
            ),
            (
                "rank-platinum",
                "Platinum · level 80 · 25% · #1",
                lambda: render_rank_card(TYPICAL_NAME, AVATARS[3], xp_at(80, 0.25), 1),
            ),
            (
                "rank-longname",
                "Longest possible name (32 chars) — must not collide with the position block",
                lambda: render_rank_card(LONG_NAME, AVATARS[4], xp_at(21, 0.49), 3),
            ),
            ("rank-shortname", "Very short name", lambda: render_rank_card(SHORT_NAME, AVATARS[5], xp_at(33, 0.8), 7)),
            (
                "rank-level0",
                "Brand-new member: level 0, bar nearly empty",
                lambda: render_rank_card("firsttimer", AVATARS[0], 12, 640),
            ),
            (
                "rank-threshold",
                "Exactly on a threshold — bar legitimately reads 0 / next",
                lambda: render_rank_card(TYPICAL_NAME, AVATARS[1], xp_at(21, 0.0), 3),
            ),
            (
                "rank-nearly-full",
                "99% of the way to the next level",
                lambda: render_rank_card(TYPICAL_NAME, AVATARS[2], xp_at(40, 0.99), 2),
            ),
            (
                "rank-unranked",
                "No leaderboard position passed → 'Unranked'",
                lambda: render_rank_card(TYPICAL_NAME, AVATARS[3], xp_at(9, 0.3), None),
            ),
            (
                "rank-bigposition",
                "4-digit leaderboard position",
                lambda: render_rank_card("lurker", AVATARS[4], xp_at(2, 0.5), 1284),
            ),
            (
                "rank-noavatar",
                "Avatar missing → placeholder circle",
                lambda: render_rank_card(TYPICAL_NAME, None, xp_at(45, 0.6), 4),
            ),
        ],
    ),
    (
        "Level-up announcement",
        "Posted when a member gains a level WITHOUT changing rank tier.",
        [
            (
                "levelup-bronze",
                "Bronze, single-digit level",
                lambda: render_levelup_card("firsttimer", AVATARS[0], 4, "bronze"),
            ),
            (
                "levelup-silver",
                "Silver, double-digit level",
                lambda: render_levelup_card("chatterbox", AVATARS[1], 17, "silver"),
            ),
            ("levelup-gold", "Gold", lambda: render_levelup_card("hunt_enjoyer", AVATARS[2], 34, "gold")),
            (
                "levelup-platinum",
                "Platinum, 3-digit level (levels are uncapped)",
                lambda: render_levelup_card(TYPICAL_NAME, AVATARS[3], 104, "platinum"),
            ),
            (
                "levelup-longname",
                "Long name — the name truncates, 'reached Level N' must survive",
                lambda: render_levelup_card(LONG_NAME, AVATARS[4], 12, "silver"),
            ),
            (
                "levelup-noavatar",
                "Avatar missing → placeholder circle",
                lambda: render_levelup_card(SHORT_NAME, None, 8, "bronze"),
            ),
        ],
    ),
    (
        "Rank-up announcement",
        "Posted instead of the level-up card when the member crosses into a new tier. "
        "Only three transitions exist: 11 = Silver, 21 = Gold, 41 = Platinum.",
        [
            (
                "rankup-silver",
                "Bronze → Silver at level 11",
                lambda: render_rankup_card("newcomer99", AVATARS[0], "bronze", "silver", 11),
            ),
            (
                "rankup-gold",
                "Silver → Gold at level 21",
                lambda: render_rankup_card("hunt_enjoyer", AVATARS[1], "silver", "gold", 21),
            ),
            (
                "rankup-platinum",
                "Gold → Platinum at level 41 (final tier)",
                lambda: render_rankup_card(TYPICAL_NAME, AVATARS[2], "gold", "platinum", 41),
            ),
            (
                "rankup-longname",
                "Long name — must not swallow 'is now'",
                lambda: render_rankup_card(LONG_NAME, AVATARS[3], "silver", "gold", 21),
            ),
            (
                "rankup-noavatar",
                "Avatar missing → placeholder circle",
                lambda: render_rankup_card(SHORT_NAME, None, "bronze", "silver", 11),
            ),
        ],
    ),
    (
        "Competition banner",
        "Header image for the competition panel. The entries themselves are TEXT beneath it "
        "(paginated, 10 a page), so the banner carries identity and the three prize slots and "
        "stays valid across every page instead of re-rendering per page turn. The countdown is "
        "NOT drawn here — it lives in the Components V2 message as a client-ticked timestamp.",
        [
            (
                "banner-competition",
                "Weekly, all three prize types",
                lambda: _banner(
                    "Weekly Competition",
                    "Top 3 most active members win prizes",
                    [("#1 Prize", "$50.00"), ("#2 Prize", "5,000 pts"), ("#3 Prize", "Steam key")],
                ),
            ),
            (
                "banner-competition-partial",
                "Only a first prize set — the other two still render as empty slots",
                lambda: _banner(
                    "Monthly Competition",
                    "Top 3 most active members win prizes",
                    [("#1 Prize", "$250.00"), ("#2 Prize", "—"), ("#3 Prize", "—")],
                ),
            ),
            (
                "banner-competition-long",
                "Overflow: long prize strings truncate to the chip width",
                lambda: _banner(
                    "Bi-Weekly Competition",
                    "Top 3 most active members win prizes",
                    [
                        ("#1 Prize", "$1,250.00"),
                        ("#2 Prize", "1,000,000 pts"),
                        ("#3 Prize", "Gaming chair + headset"),
                    ],
                ),
            ),
        ],
    ),
    (
        "Competition results",
        "Posted once when the period closes. Covers all three prize types.",
        [
            (
                "competition-winners",
                "Full podium: USD, points and a custom prize",
                lambda: render_competition_winners_card(_winners(3), "Weekly"),
            ),
            (
                "competition-winners-two",
                "Only two members qualified — the podium centres on what was won",
                lambda: render_competition_winners_card(_winners(2), "Weekly"),
            ),
            (
                "competition-winners-one",
                "Only one member qualified",
                lambda: render_competition_winners_card(_winners(1), "Monthly"),
            ),
            (
                "competition-winners-none",
                "Period closed with no qualifying activity",
                lambda: render_competition_winners_card([], "Weekly"),
            ),
        ],
    ),
    (
        "Community banner",
        "Header image for the community leaderboard panel, edited in place in the configured "
        "channel. Members + Top Rank only — a busy server's total XP runs to eight digits and "
        "would be truncated inside a chip.",
        [
            (
                "banner-community",
                "Populated server",
                lambda: _banner(
                    "Community Leaderboard",
                    "Every member's lifetime XP",
                    [("Members", "1,204"), ("Top Rank", "Platinum")],
                ),
            ),
            (
                "banner-community-empty",
                "No activity yet",
                lambda: _banner(
                    "Community Leaderboard",
                    "Every member's lifetime XP",
                    [("Members", "0"), ("Top Rank", "—")],
                ),
            ),
        ],
    ),
    (
        "Banner theming",
        "Configured from the dashboard (Levels -> Appearance): title font, title colour and "
        "background (solid or gradient). Fonts are a closed set bundled in assets/fonts/ — "
        "Pillow can only draw a face that exists on the host, so the dropdown offers exactly "
        "what renders.",
        [
            (
                "banner-theme-anton",
                "Anton, purple title, vertical gradient",
                lambda: _banner(
                    "Weekly Competition",
                    "Top 3 most active members win prizes",
                    [("#1 Prize", "$50.00"), ("#2 Prize", "5,000 pts"), ("#3 Prize", "Steam key")],
                    BannerTheme(
                        font="anton",
                        title_color="#a855f7",
                        background="linear-gradient(180deg, #2e1065 0%, #0b0614 100%)",
                    ),
                ),
            ),
            (
                "banner-theme-bebas",
                "Bebas Neue, cyan title, 3-stop horizontal gradient",
                lambda: _banner(
                    "Community Leaderboard",
                    "Every member's lifetime XP",
                    [("Members", "128"), ("Top Rank", "Platinum")],
                    BannerTheme(
                        font="bebas",
                        title_color="rgb(34, 211, 238)",
                        background="linear-gradient(90deg, #082f49 0%, #0c4a6e 50%, #075985 100%)",
                    ),
                ),
            ),
            (
                "banner-theme-orbitron",
                "Orbitron, orange title, solid background",
                lambda: _banner(
                    "Monthly Competition",
                    "Top 3 most active members win prizes",
                    [("#1 Prize", "$250.00"), ("#2 Prize", "25,000 pts"), ("#3 Prize", "Merch")],
                    BannerTheme(font="orbitron", title_color="#f97316", background="#1c1917"),
                ),
            ),
            (
                "banner-theme-broken",
                "Malformed theme falls back to the stock look rather than failing the render",
                lambda: _banner(
                    "Community Leaderboard",
                    "Every member's lifetime XP",
                    [("Members", "42"), ("Top Rank", "Gold")],
                    BannerTheme(font="nope", title_color="zzz", background="linear-gradient(junk)"),
                ),
            ),
        ],
    ),
]

PAGE_CSS = """
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 40px 32px 80px;
    background: #0a0a0c;
    color: #e8e8ea;
    font: 14px/1.55 ui-sans-serif, system-ui, "Segoe UI", Roboto, sans-serif;
  }
  header { max-width: 980px; margin: 0 auto 40px; }
  h1 { margin: 0 0 6px; font-size: 26px; letter-spacing: -0.01em; }
  .sub { color: #8b8b95; font-size: 13px; }
  .meta {
    margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px;
  }
  .pill {
    border: 1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.04);
    border-radius: 999px; padding: 4px 12px; font-size: 12px; color: #b9b9c2;
  }
  .pill.warn { border-color: rgba(250,204,21,.35); background: rgba(250,204,21,.08); color: #facc15; }
  .pill.ok { border-color: rgba(52,211,153,.3); background: rgba(52,211,153,.08); color: #6ee7b7; }
  section { max-width: 980px; margin: 0 auto 52px; }
  h2 {
    margin: 0 0 4px; font-size: 17px; color: #facc15;
    border-bottom: 1px solid rgba(255,255,255,.1); padding-bottom: 10px;
  }
  .group-note { color: #8b8b95; font-size: 13px; margin: 10px 0 22px; }
  figure {
    margin: 0 0 26px; padding: 16px;
    border: 1px solid rgba(255,255,255,.08); border-radius: 12px;
    background: rgba(255,255,255,.02);
  }
  figure img { display: block; max-width: 100%; height: auto; border-radius: 8px; }
  figcaption { margin-top: 12px; font-size: 12.5px; color: #9a9aa4; }
  figcaption b { color: #d6d6dc; font-weight: 600; }
  code { color: #c9c9d1; background: rgba(255,255,255,.06); padding: 1px 5px; border-radius: 4px; }
"""


async def _avatars_reachable(url: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                return resp.status == 200
    except Exception:
        return False


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=os.path.join(REPO_ROOT, "tmp", "level-card-preview"),
        help="output directory (default: tmp/level-card-preview, gitignored)",
    )
    args = parser.parse_args()
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    live_avatars = await _avatars_reachable(AVATARS[0])

    parts = []
    total = 0
    for group_title, group_note, items in GROUPS:
        parts.append(f"<section>\n<h2>{html.escape(group_title)}</h2>")
        parts.append(f'<p class="group-note">{html.escape(group_note)}</p>')
        for slug, caption, factory in items:
            file = await factory()
            data = file.fp.read()
            filename = f"{slug}.png"
            with open(os.path.join(out_dir, filename), "wb") as fh:
                fh.write(data)
            total += 1
            parts.append(
                "<figure>"
                f'<img src="{filename}" alt="{html.escape(caption)}">'
                f"<figcaption><b>{html.escape(slug)}</b> — {html.escape(caption)}</figcaption>"
                "</figure>"
            )
            print(f"  {filename:<26} {len(data):>8,} bytes")
        parts.append("</section>")

    avatar_pill = (
        '<span class="pill ok">avatars: fetched live from Discord CDN</span>'
        if live_avatars
        else '<span class="pill warn">avatars: OFFLINE — every circle below is a placeholder, '
        "not a styling result</span>"
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Levels card preview</title>
<style>{PAGE_CSS}</style>
</head>
<body>
<header>
  <h1>Levels card preview</h1>
  <div class="sub">
    Rendered by <code>features/levels/cards.py</code> — the same code path the bot posts from.
    Edit that file, re-run <code>scripts/preview_level_cards.py</code>, refresh.
  </div>
  <div class="meta">
    <span class="pill">{total} cards</span>
    {avatar_pill}
    <span class="pill">generated {datetime.now().strftime("%Y-%m-%d %H:%M")}</span>
  </div>
</header>
{"".join(parts)}
</body>
</html>
"""
    index_path = os.path.join(out_dir, "index.html")
    with open(index_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    print(f"\n{total} cards -> {out_dir}")
    print(f"open: {index_path}")
    if not live_avatars:
        print("WARNING: Discord CDN unreachable — avatars rendered as placeholders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
