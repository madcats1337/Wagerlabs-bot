"""Tests for the Levels leaderboard banner renderer (features.levels.cards).

Two things are guarded here:

  • Chip text is centred on its INK, not on the font's ascent box. PIL positions
    text by that box, and the empty space it reserves above the cap height
    differs per typeface — so padding measured from the box put the label/value
    pair visibly low, by a different amount for every font the operator can
    pick. This shipped once and was caught by eye.

  • The dashboard's preview renderer stays in step with this one. The two repos
    deploy independently and cannot import each other, so
    Admin-Dashboard/utils/levels_banner_render.py is a mechanical copy of the
    banner half of cards.py. If they drift, the Appearance tab's preview lies
    about what actually gets posted to Discord.
"""

import importlib.util
import io
import os

import pytest
from PIL import Image, ImageChops

from features.levels import cards

DASH_RENDERER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Admin-Dashboard",
    "utils",
    "levels_banner_render.py",
)

# _dither() adds a sub-LSB noise floor through Image.effect_noise, which is
# randomly seeded — the SAME renderer never produces identical bytes twice, so
# comparisons are per-pixel with this tolerance. A real divergence (different
# font, colour or geometry) moves whole glyphs and exceeds it by a wide margin.
DITHER_TOLERANCE = 8

COMMUNITY_STATS = [("Members", "128"), ("Top Rank", "Platinum")]
COMPETITION_STATS = [("#1 Prize", "$50.00"), ("#2 Prize", "5,000 pts"), ("#3 Prize", "Steam key")]


def _open(png_bytes):
    return Image.open(io.BytesIO(png_bytes)).convert("RGBA")


def _chip_geometry(chip_count):
    """(x, y, w, h) of the FIRST stat chip, mirroring _draw_banner_stats.

    The banner renders with pad=0 — it sits at the top of a Components V2
    container, which fits an image edge-to-edge, so the usual transparent
    shadow margin would just be dead space beside the text rows below it.
    """
    s = cards._s
    ox = oy = 0
    inner_x = ox + s(30)
    inner_w = s(cards.BANNER_W) - s(60)
    gap = s(12)
    chip_w = (inner_w - gap * (chip_count - 1)) // chip_count
    chip_h = s(64)
    chip_y = oy + s(cards.BANNER_H) - s(24) - chip_h
    return inner_x, chip_y, chip_w, chip_h


def _ink_rows(img, x0, x1, y0, y1):
    """Row indices in [y0, y1) that contain bright text ink."""
    px = img.load()
    rows = []
    for y in range(y0, y1):
        for x in range(x0, x1, 2):
            r, g, b, a = px[x, y]
            if a > 8 and (r + g + b) / 3 > 95:
                rows.append(y)
                break
    return rows


# ── Chip ink centring ───────────────────────────────────────────────────────


@pytest.mark.parametrize("font_key", sorted(cards._BANNER_FONTS))
def test_chip_text_is_optically_centred(font_key):
    """The label+value block sits centred in the chip for EVERY bundled font.

    Parameterised per font on purpose: the original bug was font-dependent, so a
    single-font assertion would not have caught it.
    """
    png = cards.render_banner_png(
        "Weekly Competition",
        "Top 3 most active members win prizes",
        COMPETITION_STATS,
        theme=cards.BannerTheme(font=font_key),
    )
    img = _open(png)
    chip_x, chip_y, chip_w, chip_h = _chip_geometry(len(COMPETITION_STATS))

    rows = _ink_rows(img, chip_x + 4, chip_x + chip_w - 4, chip_y - 8, chip_y + chip_h + 8)
    assert rows, f"no text ink found in the chip for font {font_key!r}"

    ink_centre = (min(rows) + max(rows)) / 2
    chip_centre = chip_y + chip_h / 2
    offset = ink_centre - chip_centre

    # 3px at 2x scale is well under one pixel of the displayed banner. The old
    # box-centred code was +8.5px here.
    assert abs(offset) <= 3, f"font {font_key!r}: chip text off-centre by {offset:+.1f}px"


@pytest.mark.parametrize("font_key", sorted(cards._BANNER_FONTS))
def test_chip_text_stays_inside_the_chip(font_key):
    """Ink never spills past the chip plate."""
    png = cards.render_banner_png(
        "Weekly Competition", "sub", COMPETITION_STATS, theme=cards.BannerTheme(font=font_key)
    )
    img = _open(png)
    chip_x, chip_y, chip_w, chip_h = _chip_geometry(len(COMPETITION_STATS))
    rows = _ink_rows(img, chip_x + 4, chip_x + chip_w - 4, chip_y - 20, chip_y + chip_h + 20)
    assert rows
    assert min(rows) >= chip_y, f"font {font_key!r}: ink above the chip"
    assert max(rows) <= chip_y + chip_h, f"font {font_key!r}: ink below the chip"


# ── Theme parsing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("#fff", (255, 255, 255)),
        ("#facc15", (250, 204, 21)),
        ("#facc15ff", (250, 204, 21)),  # alpha parsed then dropped
        ("rgb(34, 211, 238)", (34, 211, 238)),
        ("rgba(34, 211, 238, 0.5)", (34, 211, 238)),
        ("  #A855F7  ", (168, 85, 247)),
    ],
)
def test_parse_color_accepts_picker_output(value, expected):
    assert cards._parse_color(value, None) == expected


@pytest.mark.parametrize("value", ["", None, "not-a-color", "#12", "rgb(1,2)", 42, "#gggggg"])
def test_parse_color_falls_back_on_junk(value):
    sentinel = ("fallback",)
    assert cards._parse_color(value, sentinel) is sentinel


def test_parse_gradient_reads_stops_and_angle():
    kind, stops, angle = cards._parse_gradient("linear-gradient(90deg, #082f49 0%, #075985 100%)")
    assert kind == "linear"
    assert stops == ["#082f49", "#075985"]
    assert angle == 90


def test_parse_gradient_treats_a_bare_colour_as_solid():
    kind, stops, _ = cards._parse_gradient("#1c1917")
    assert kind == "solid"
    assert stops == ["#1c1917"]


def test_malformed_theme_still_renders():
    """A stale or hand-edited setting must degrade to the stock look, not 500."""
    png = cards.render_banner_png(
        "Community Leaderboard",
        "sub",
        COMMUNITY_STATS,
        theme=cards.BannerTheme(font="no-such-font", title_color="zzz", background="linear-gradient(nonsense)"),
    )
    assert _open(png).size == (cards._s(cards.BANNER_W), cards._s(cards.BANNER_H))


def test_banner_has_no_transparent_margin():
    """The banner must fill its own image, edge to edge.

    Components V2 fits a MediaGallery image to the container width, so any
    transparent margin baked into the PNG renders as dead space beside the text
    rows below it — ~16px a side at Discord's display width, which is what made
    the banner look inset from the rows it heads.
    """
    png = cards.render_banner_png("Community Leaderboard", "sub", COMMUNITY_STATS)
    img = _open(png)
    assert img.size == (cards._s(cards.BANNER_W), cards._s(cards.BANNER_H))

    # Corners are rounded, so probe the edge MIDPOINTS: opaque there means the
    # card body reaches every edge.
    px = img.load()
    w, h = img.size
    for name, (x, y) in {
        "left": (0, h // 2),
        "right": (w - 1, h // 2),
        "top": (w // 2, 0),
        "bottom": (w // 2, h - 1),
    }.items():
        assert px[x, y][3] > 250, f"{name} edge is transparent — the banner is inset"


def test_unset_theme_uses_the_stock_accent():
    assert cards.BannerTheme().accent == cards.ACCENT
    assert cards.BannerTheme(title_color="#a855f7").accent == (168, 85, 247)


# ── Dashboard preview parity ────────────────────────────────────────────────


@pytest.mark.skipif(not os.path.exists(DASH_RENDERER), reason="Admin-Dashboard repo not checked out alongside")
@pytest.mark.parametrize(
    "title,subtitle,stats,theme_kwargs",
    [
        ("Community Leaderboard", "Every member's lifetime XP", COMMUNITY_STATS, {}),
        ("Weekly Competition", "Top 3 most active members win prizes", COMPETITION_STATS, {}),
        (
            "Weekly Competition",
            "Themed",
            COMPETITION_STATS,
            {"font": "anton", "title_color": "#a855f7", "background": "linear-gradient(180deg, #2e1065, #0b0614)"},
        ),
        (
            "Community Leaderboard",
            "Horizontal",
            COMMUNITY_STATS,
            {
                "font": "bebas",
                "title_color": "rgb(34, 211, 238)",
                "background": "linear-gradient(90deg, #082f49, #075985)",
            },
        ),
        ("Monthly Competition", "Solid", [("#1 Prize", "$250.00")], {"font": "orbitron", "background": "#1c1917"}),
        ("Community Leaderboard", "Serif", COMMUNITY_STATS, {"font": "playfair"}),
    ],
)
def test_dashboard_preview_matches_this_renderer(title, subtitle, stats, theme_kwargs):
    """The dashboard's copy renders the same pixels this one does.

    Guards the duplication in Admin-Dashboard/utils/levels_banner_render.py:
    change the banner here without regenerating that file and this fails.
    """
    spec = importlib.util.spec_from_file_location("levels_banner_render", DASH_RENDERER)
    dash = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dash)

    ours = _open(cards.render_banner_png(title, subtitle, stats, theme=cards.BannerTheme(**theme_kwargs)))
    theirs = _open(dash.render_banner_png(title, subtitle, stats, theme=dash.BannerTheme(**theme_kwargs)))

    assert ours.size == theirs.size, "dashboard preview renders a different SIZE than the bot"
    delta = ImageChops.difference(ours, theirs).convert("L").getextrema()[1]
    assert delta <= DITHER_TOLERANCE, (
        f"dashboard preview diverged from the bot (max channel delta {delta}). "
        "Regenerate Admin-Dashboard/utils/levels_banner_render.py from features/levels/cards.py."
    )
