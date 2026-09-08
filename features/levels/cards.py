"""Pillow rendering for the /rank card, the community-leaderboard panel image,
and the level-up / rank-up announcement cards.

The font-fallback chain and the aiohttp-fetch -> BytesIO -> discord.File
pipeline follow bot.py's create_shop_mosaic_image.

Resolution: geometry and type sizes below are written in DESIGN units and
multiplied by SCALE on the way out, so the PNG is a 2x asset. Discord lays an
embedded image out at roughly 550 CSS px; a 1x render is then upscaled on any
HiDPI display and goes visibly soft, which no amount of edge smoothing fixes.

Edges: Pillow has NO antialiased drawing primitives — every ellipse and
rounded-rectangle edge it draws is hard-stepped. Shapes are therefore built as
masks at _SS times the render size and downsampled with LANCZOS. Combined with
SCALE that is 4x effective supersampling, which is what makes the avatar
circle, the card corners and the bar caps read as smooth.

Light: bloom is accumulated from the emissive shapes and SCREENED over the
finished card as a post-process. Light is additive, so screening is what makes
it brighten the surface the way real light does; alpha-compositing a
translucent coloured blob under the shape instead lays a dull film on the card,
which is what makes a "glow" look painted on. Only the avatar ring emits —
see _BLOOM_SCALES for why the XP bar deliberately does not.

Type is never bloomed: blurred copies behind glyphs just fringe the letterforms.

Each card sits on a transparent margin so its drop shadow falls outside the
card body onto Discord's own background. Output is therefore RGBA — flattening
to RGB would replace that soft edge with a black rectangle.

Avatars go through ONE aiohttp session per card and, for the leaderboard, all
in flight together: the panel re-renders every 10 minutes for every guild, so a
session-per-row plus a serial round trip per row is real repeated churn against
Discord's CDN, not a micro-optimisation.
"""

import asyncio
import io
import logging

import aiohttp
import discord
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .curve import RANK_LABELS, xp_progress

logger = logging.getLogger(__name__)

# Output resolution multiplier. Everything below is authored at 1x and scaled
# through _s()/_load_font, so this is the only knob for asset resolution.
SCALE = 2

# Supersample factor for shape masks, on top of SCALE. 2 x SCALE=2 gives 4x
# effective coverage sampling; going higher is invisible here and squares the
# memory of the full-card masks (the 10-row leaderboard is already ~1600x1900).
_SS = 2


def _s(value) -> int:
    """Design units -> render pixels."""
    return int(value * SCALE)


PAD = _s(26)  # transparent margin around the card, holding the drop shadow
RADIUS = _s(30)

CARD_TOP = (32, 32, 40)  # card gradient, lighter at the top for a lit look
CARD_BOTTOM = (15, 15, 19)
CARD_BORDER = (255, 255, 255)
CARD_BORDER_ALPHA = 30

SHADOW_BLUR = _s(16)
SHADOW_OFFSET = _s(10)
SHADOW_ALPHA = 150

TEXT_COLOR = (255, 255, 255)
SUBTEXT_COLOR = (170, 170, 182)
ACCENT = (250, 204, 21)  # Wagerlabs yellow
BAR_TRACK = (44, 44, 54)
DIVIDER = (255, 255, 255)
DIVIDER_ALPHA = 18
AVATAR_PLACEHOLDER = (58, 58, 70, 255)

RANK_COLORS = {
    "bronze": (205, 127, 50),
    "silver": (198, 202, 214),
    "gold": (250, 204, 21),
    "platinum": (137, 220, 235),
}

# Bloom is layered at several scales — a tight bright core plus wider, fainter
# tails. One radius alone reads as a flat smear. Radii are design units.
#
# The avatar ring is the ONLY emitter. Blooming the XP bar was tried and pulled
# back out: emitted light scales with the lit AREA, so a long fill throws far
# more of it than a thin ring, and every tail wide enough to read as light
# washed over the type sitting above and below the bar and softened the
# fill/track boundary — the one thing the bar exists to communicate. Tightening
# it far enough to stop that left nothing worth rendering.
# Each scale is screened on SEPARATELY rather than combined into one layer.
# Taking a max (the obvious way to merge them) leaves a kink where the dominant
# term switches over, and the eye reads that kink as a ring edge in the falloff.
_BLOOM_SCALES = ((4, 0.55), (12, 0.38), (30, 0.26), (60, 0.16))

# Sub-LSB noise, added once at the end. The bloom falloff spans only ~20
# luminance levels across a couple of hundred pixels, and the card's background
# gradient only ~17 across its height, so at 8 bits each level occupies a
# 30-40px plateau and the steps between them read as hard rings. PIL cannot blur
# in floating point (ImageFilter refuses mode "F"), so the fix is to dither
# rather than to carry more precision.
#
# 0.6 is the knee, measured: it collapses the longest flat run from 106px to
# 7px, while raising it to 1.6 only reaches 4px and costs ~46% more PNG (noise
# is incompressible, and the leaderboard panel re-uploads every 10 minutes for
# every guild). Higher would also start reading as grain rather than dither.
_DITHER_SIGMA = 0.6

_AVATAR_TIMEOUT = aiohttp.ClientTimeout(total=10)


# ── Fonts and text ──────────────────────────────────────────────────────────


def _open_font(path: str, size: int):
    """One face, or None when it isn't installed on this host."""
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return None


def _load_font(size: int, bold: bool = False):
    """Load a face at `size` DESIGN units — rasterised at SCALE x that, so the
    glyphs are drawn at output resolution rather than upscaled.

    Linux (Railway) carries DejaVu, Windows carries Arial; whichever is missing
    simply returns None and the next candidate is tried.
    """
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"]
        if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"]
    )
    for path in candidates:
        font = _open_font(path, _s(size))
        if font is not None:
            return font
    return ImageFont.load_default()


def _truncate(draw, value: str, font, max_width: int) -> str:
    """Trim `value` to fit `max_width` RENDER px, ellipsising when it doesn't.

    Discord display names run to 32 characters; at the rank card's title size
    that overruns the canvas, so trim by MEASURED width rather than a character
    count (mirrors create_shop_mosaic_image's title handling).
    """
    if draw.textlength(value, font=font) <= max_width:
        return value
    trimmed = value
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return trimmed + "…"


def _fit_with_suffix(draw, name: str, suffix: str, font, max_width: int) -> str:
    """Truncate only `name` so `suffix` always survives.

    Truncating the whole sentence let a long display name swallow the half that
    carries the information — a level-up card reading "ThisIsAVeryLongName3…"
    with no level in it.
    """
    suffix_width = draw.textlength(suffix, font=font)
    return _truncate(draw, name, font, max(0, int(max_width - suffix_width))) + suffix


# ── Antialiased shape masks ─────────────────────────────────────────────────


def _circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size * _SS, size * _SS), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * _SS - 1, size * _SS - 1), fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _ring_mask(size: int, width: int) -> Image.Image:
    mask = Image.new("L", (size * _SS, size * _SS), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * _SS - 1, size * _SS - 1), outline=255, width=max(1, width * _SS))
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _rounded_mask(size, radius: int) -> Image.Image:
    w, h = size
    mask = Image.new("L", (w * _SS, h * _SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w * _SS - 1, h * _SS - 1), radius=radius * _SS, fill=255)
    return mask.resize((w, h), Image.Resampling.LANCZOS)


def _rounded_outline_mask(size, radius: int, width: int) -> Image.Image:
    w, h = size
    mask = Image.new("L", (w * _SS, h * _SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * _SS - 1, h * _SS - 1),
        radius=radius * _SS,
        outline=255,
        width=max(1, width * _SS),
    )
    return mask.resize((w, h), Image.Resampling.LANCZOS)


def _vertical_gradient(size, top, bottom) -> Image.Image:
    """A 1px-wide gradient stretched to `size` — cheaper than a per-pixel fill."""
    w, h = size
    strip = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        strip.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return strip.resize((w, h), Image.Resampling.BILINEAR)


def _lighten(color, amount: float):
    """Move `color` toward white by `amount` (0-1)."""
    return tuple(min(255, int(c + (255 - c) * amount)) for c in color)


def _tint(mask: Image.Image, color, alpha: int = 255) -> Image.Image:
    """Colorize a mask into an RGBA layer ready to composite."""
    layer = Image.new("RGBA", mask.size, tuple(color) + (0,))
    layer.putalpha(mask if alpha >= 255 else mask.point(lambda v: v * alpha // 255))
    return layer


def _top_highlight(size, radius: int) -> Image.Image:
    """The card's top edge catching light.

    This is where the "premium" read comes from on a dark surface — a lit edge
    plus the drop shadow — rather than from blooming the type.
    """
    w, h = size
    edge = _rounded_outline_mask((w, h), radius, max(1, SCALE // 2))
    fade = Image.new("L", (1, h))
    for y in range(h):
        t = min(1.0, y / max(1.0, h * 0.45))
        fade.putpixel((0, y), int(255 * (1 - t) ** 2))
    return ImageChops.multiply(edge, fade.resize((w, h), Image.Resampling.BILINEAR))


# ── Light ───────────────────────────────────────────────────────────────────


class _Light:
    """Accumulates emissive shapes so bloom can be applied once, at the end.

    Bloom is a POST-PROCESS: light spreads over whatever surrounds it, the
    emitting object included. Compositing it under each shape as that shape is
    drawn (the obvious first attempt) hides the inner half behind the shape and
    stacks translucent films wherever two glows overlap.
    """

    def __init__(self, size, clip):
        self.size = size
        self.clip = clip
        self.layers = {}

    def emit(self, mask, color, dest=(0, 0)) -> None:
        key = tuple(color)
        layer = self.layers.get(key)
        if layer is None:
            layer = Image.new("L", self.size, 0)
            self.layers[key] = layer
        box = (dest[0], dest[1], dest[0] + mask.width, dest[1] + mask.height)
        layer.paste(ImageChops.lighter(layer.crop(box), mask), box)


def _dither(image: Image.Image, clip: Image.Image) -> Image.Image:
    """Break up 8-bit quantisation contours with a sub-LSB noise floor.

    This is dithering, not texture: the same noise goes into all three channels,
    so it perturbs luminance without tinting anything. `clip` holds it to the
    card body — noise in the transparent margin is invisible but still costs
    PNG size, since noise is the one thing the format cannot compress.
    """
    noise = Image.composite(
        Image.effect_noise(image.size, _DITHER_SIGMA),
        Image.new("L", image.size, 128),  # 128 is the neutral value for the add below
        clip,
    )
    return Image.merge("RGB", [ImageChops.add(ch, noise, 1.0, -128) for ch in image.split()])


def _apply_bloom(canvas, light):
    """Screen the accumulated light over the card, clipped to the card body."""
    if not light.layers:
        return _finish(canvas, light)

    rgb = canvas.convert("RGB")
    for colour, layer in light.layers.items():
        for radius, weight in _BLOOM_SCALES:
            blurred = ImageChops.multiply(layer.filter(ImageFilter.GaussianBlur(_s(radius))), light.clip)
            # Weight and tint fold into ONE rounding step; scaling the mask down
            # first and colouring it after quantised the faint tail twice.
            tinted = Image.merge(
                "RGB",
                [blurred.point(lambda v, c=c, w=weight: int(v * c * w) // 255) for c in colour],
            )
            rgb = ImageChops.screen(rgb, tinted)

    return _finish(canvas, light, rgb)


def _finish(canvas, light, rgb=None):
    """Dither and re-apply the card's alpha."""
    out = _dither(canvas.convert("RGB") if rgb is None else rgb, light.clip).convert("RGBA")
    out.putalpha(canvas.getchannel("A"))
    return out


# ── Card shell ──────────────────────────────────────────────────────────────


def _new_card(width: int, height: int):
    """Transparent canvas holding a rounded, shadowed, gradient-filled card.

    `width`/`height` are RENDER px. Returns (canvas, draw, ox, oy, light) —
    content is positioned relative to the card's top-left corner at (ox, oy),
    so callers never deal with the margin. `light` collects emissive shapes for
    _apply_bloom at the end.
    """
    total = (width + PAD * 2, height + PAD * 2)
    canvas = Image.new("RGBA", total, (0, 0, 0, 0))

    body_mask = _rounded_mask((width, height), RADIUS)

    # Drop shadow: the body silhouette, blurred, pushed down, laid underneath.
    shadow_mask = Image.new("L", total, 0)
    shadow_mask.paste(body_mask, (PAD, PAD))
    shadow = _tint(shadow_mask.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)), (0, 0, 0), SHADOW_ALPHA)
    canvas.alpha_composite(shadow, dest=(0, SHADOW_OFFSET))

    body = _vertical_gradient((width, height), CARD_TOP, CARD_BOTTOM).convert("RGBA")
    body.putalpha(body_mask)
    canvas.alpha_composite(body, dest=(PAD, PAD))

    border = _tint(
        _rounded_outline_mask((width, height), RADIUS, max(1, SCALE // 2)),
        CARD_BORDER,
        CARD_BORDER_ALPHA,
    )
    canvas.alpha_composite(border, dest=(PAD, PAD))

    highlight = _tint(_top_highlight((width, height), RADIUS), (255, 255, 255), 46)
    canvas.alpha_composite(highlight, dest=(PAD, PAD))

    # Bloom is clipped to the card body so light lands on the surface instead
    # of haloing out into the transparent margin and over the drop shadow.
    clip = Image.new("L", total, 0)
    clip.paste(body_mask, (PAD, PAD))

    return canvas, ImageDraw.Draw(canvas), PAD, PAD, _Light(total, clip)


def _draw_avatar(canvas, light, avatar, x, y, size: int, ring_color, ring_width: int = 5):
    """Avatar behind a sharp rank-coloured ring, registered as a light source."""
    canvas.alpha_composite(avatar, dest=(x, y))
    ring = _ring_mask(size, _s(ring_width))
    canvas.alpha_composite(_tint(ring, ring_color), dest=(x, y))
    light.emit(ring, ring_color, dest=(x, y))


def _draw_progress_bar(canvas, x, y, width: int, height: int, fraction: float, color):
    """Rounded track with a gradient fill, and no bloom — see _BLOOM_SCALES."""
    fraction = max(0.0, min(1.0, fraction))
    radius = height // 2

    canvas.alpha_composite(_tint(_rounded_mask((width, height), radius), BAR_TRACK), dest=(x, y))
    if fraction <= 0:
        return

    fill_size = (max(height, int(width * fraction)), height)
    fill = _rounded_mask(fill_size, radius)
    # Lit from above like the card itself, so the fill reads as a rounded
    # surface rather than a flat slab of colour.
    body = _vertical_gradient(fill_size, _lighten(color, 0.42), color).convert("RGBA")
    body.putalpha(fill)
    canvas.alpha_composite(body, dest=(x, y))


def _to_file(canvas: Image.Image, filename: str) -> discord.File:
    """PNG with alpha preserved — flattening would square off the soft edge."""
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return discord.File(buf, filename=filename)


# ── Avatars ─────────────────────────────────────────────────────────────────


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img.resize((size, size), Image.Resampling.LANCZOS), (0, 0), mask=_circle_mask(size))
    return out


def _placeholder_avatar(size: int) -> Image.Image:
    return _circle_crop(Image.new("RGBA", (size, size), AVATAR_PLACEHOLDER), size)


async def _download_avatar(session, url, size: int) -> Image.Image:
    if not url:
        return _placeholder_avatar(size)
    try:
        async with session.get(url, timeout=_AVATAR_TIMEOUT) as resp:
            if resp.status != 200:
                return _placeholder_avatar(size)
            data = await resp.read()
        return _circle_crop(Image.open(io.BytesIO(data)).convert("RGBA"), size)
    except Exception as e:
        logger.debug(f"[levels] avatar fetch failed ({url}): {e}")
        return _placeholder_avatar(size)


async def _fetch_avatars(urls, size: int):
    """Every avatar for one card: one session, all requests concurrent."""
    if not urls:
        return []
    async with aiohttp.ClientSession() as session:
        return await asyncio.gather(*(_download_avatar(session, url, size) for url in urls))


async def _fetch_avatar(url, size: int) -> Image.Image:
    return (await _fetch_avatars([url], size))[0]


# ── Cards ───────────────────────────────────────────────────────────────────


async def render_rank_card(username: str, avatar_url, total_xp: int, leaderboard_position) -> discord.File:
    level, xp_into_level, xp_needed, rank = xp_progress(total_xp)
    rank_color = RANK_COLORS.get(rank, ACCENT)

    W, H = _s(900), _s(300)
    canvas, draw, ox, oy, light = _new_card(W, H)

    avatar_size = _s(180)
    avatar = await _fetch_avatar(avatar_url, avatar_size)
    _draw_avatar(canvas, light, avatar, ox + _s(40), oy + (H - avatar_size) // 2, avatar_size, rank_color)

    name_font = _load_font(44, bold=True)
    rank_font = _load_font(28, bold=True)
    small_font = _load_font(24)

    text_x = ox + _s(260)
    right_edge = ox + W - _s(40)

    # Leave room for the right-aligned leaderboard-position block.
    draw.text(
        (text_x, oy + _s(40)),
        _truncate(draw, username, name_font, _s(900 - 260 - 190)),
        fill=TEXT_COLOR,
        font=name_font,
    )
    draw.text(
        (text_x, oy + _s(100)),
        f"{RANK_LABELS[rank]} • Level {level}",
        fill=rank_color,
        font=rank_font,
    )

    bar_y = oy + _s(170)
    _draw_progress_bar(
        canvas,
        text_x,
        bar_y,
        _s(900 - 260 - 60),
        _s(28),
        (xp_into_level / xp_needed) if xp_needed else 0,
        rank_color,
    )
    draw.text(
        (text_x, bar_y + _s(36)),
        f"{xp_into_level:,} / {xp_needed:,} XP",
        fill=SUBTEXT_COLOR,
        font=small_font,
    )

    # Right-align each line by its OWN measured width. Sharing one width here
    # pushed the wider "Leaderboard" label off the edge of the card.
    position_text = f"#{leaderboard_position}" if leaderboard_position else "Unranked"
    position_label = "Leaderboard"
    draw.text(
        (right_edge - draw.textlength(position_text, font=name_font), oy + _s(40)),
        position_text,
        fill=ACCENT,
        font=name_font,
    )
    draw.text(
        (right_edge - draw.textlength(position_label, font=small_font), oy + _s(100)),
        position_label,
        fill=SUBTEXT_COLOR,
        font=small_font,
    )

    return _to_file(_apply_bloom(canvas, light), "rank.png")


async def render_levelup_card(username: str, avatar_url, new_level: int, rank: str) -> discord.File:
    rank_color = RANK_COLORS.get(rank, ACCENT)
    W, H = _s(700), _s(260)
    canvas, draw, ox, oy, light = _new_card(W, H)

    avatar_size = _s(140)
    avatar = await _fetch_avatar(avatar_url, avatar_size)
    _draw_avatar(canvas, light, avatar, ox + _s(40), oy + (H - avatar_size) // 2, avatar_size, rank_color, ring_width=4)

    headline_font = _load_font(40, bold=True)
    body_font = _load_font(26)

    text_x = ox + _s(210)
    max_text = _s(700 - 210 - 40)

    draw.text((text_x, oy + _s(55)), "LEVEL UP!", fill=ACCENT, font=headline_font)
    draw.text(
        (text_x, oy + _s(115)),
        _fit_with_suffix(draw, username, f" reached Level {new_level}", body_font, max_text),
        fill=TEXT_COLOR,
        font=body_font,
    )
    draw.text((text_x, oy + _s(155)), f"{RANK_LABELS[rank]} rank", fill=rank_color, font=body_font)

    return _to_file(_apply_bloom(canvas, light), "levelup.png")


async def render_rankup_card(username: str, avatar_url, old_rank: str, new_rank: str, new_level: int) -> discord.File:
    rank_color = RANK_COLORS.get(new_rank, ACCENT)
    W, H = _s(700), _s(280)
    canvas, draw, ox, oy, light = _new_card(W, H)

    avatar_size = _s(150)
    avatar = await _fetch_avatar(avatar_url, avatar_size)
    _draw_avatar(canvas, light, avatar, ox + _s(40), oy + (H - avatar_size) // 2, avatar_size, rank_color, ring_width=5)

    headline_font = _load_font(40, bold=True)
    body_font = _load_font(26)
    rank_name_font = _load_font(48, bold=True)
    footer_font = _load_font(20)

    text_x = ox + _s(220)
    max_text = _s(700 - 220 - 40)

    draw.text((text_x, oy + _s(45)), "RANK UP!", fill=rank_color, font=headline_font)
    draw.text(
        (text_x, oy + _s(105)),
        _fit_with_suffix(draw, username, " is now", body_font, max_text),
        fill=TEXT_COLOR,
        font=body_font,
    )
    draw.text((text_x, oy + _s(145)), RANK_LABELS[new_rank], fill=rank_color, font=rank_name_font)
    draw.text(
        (text_x, oy + _s(210)),
        f"(from {RANK_LABELS[old_rank]}, Level {new_level})",
        fill=SUBTEXT_COLOR,
        font=footer_font,
    )

    return _to_file(_apply_bloom(canvas, light), "rankup.png")


async def render_leaderboard_card(rows) -> discord.File:
    """rows: ordered list of dicts with position, discord_id, username,
    avatar_url, total_xp, messages_sent, current_level, current_rank."""
    row_h = _s(84)
    header_h = _s(76)
    avatar_size = _s(56)
    W = _s(820)
    H = header_h + row_h * max(len(rows), 1) + _s(24)

    canvas, draw, ox, oy, light = _new_card(W, H)

    title_font = _load_font(36, bold=True)
    name_font = _load_font(26, bold=True)
    small_font = _load_font(20)

    draw.text((ox + _s(30), oy + _s(20)), "Community Leaderboard", fill=ACCENT, font=title_font)

    if not rows:
        draw.text((ox + _s(30), oy + header_h + _s(20)), "No activity yet.", fill=SUBTEXT_COLOR, font=name_font)
        return _to_file(_apply_bloom(canvas, light), "leaderboard.png")

    avatars = await _fetch_avatars([row.get("avatar_url") for row in rows], avatar_size)

    text_x = ox + _s(170)
    # Reserve the XP column by MEASURING the widest value actually on this
    # board. A fixed reservation is wrong at both ends: too wide for a new
    # server's 3-digit totals, too narrow for a 7-digit one — which ran the
    # truncated name straight into the XP figure.
    xp_texts = [f"{row.get('total_xp', 0):,} XP" for row in rows]
    xp_column = max(draw.textlength(t, font=name_font) for t in xp_texts)
    name_max = int(W - _s(30) - xp_column - _s(24)) - _s(170)
    divider = _tint(Image.new("L", (W - _s(60), max(1, SCALE // 2)), 255), DIVIDER, DIVIDER_ALPHA)

    y = oy + header_h
    for index, (row, avatar) in enumerate(zip(rows, avatars)):
        rank = row.get("current_rank", "bronze")
        rank_color = RANK_COLORS.get(rank, ACCENT)

        draw.text((ox + _s(30), y + row_h // 2 - _s(16)), f"#{row['position']}", fill=SUBTEXT_COLOR, font=name_font)
        _draw_avatar(
            canvas,
            light,
            avatar,
            ox + _s(100),
            y + (row_h - avatar_size) // 2,
            avatar_size,
            rank_color,
            ring_width=3,
        )

        display_name = row.get("username") or f"User {row.get('discord_id')}"
        draw.text(
            (text_x, y + _s(12)),
            _truncate(draw, display_name, name_font, name_max),
            fill=TEXT_COLOR,
            font=name_font,
        )
        draw.text(
            (text_x, y + _s(46)),
            _truncate(
                draw,
                f"Level {row.get('current_level', 0)} • {RANK_LABELS.get(rank, rank.title())} • "
                f"{row.get('messages_sent', 0):,} msgs",
                small_font,
                name_max,
            ),
            fill=SUBTEXT_COLOR,
            font=small_font,
        )

        xp_text = xp_texts[index]
        draw.text(
            (ox + W - _s(30) - draw.textlength(xp_text, font=name_font), y + row_h // 2 - _s(13)),
            xp_text,
            fill=rank_color,
            font=name_font,
        )

        y += row_h
        if index < len(rows) - 1:
            canvas.alpha_composite(divider, dest=(ox + _s(30), y))

    return _to_file(_apply_bloom(canvas, light), "leaderboard.png")


# ── Competition cards ───────────────────────────────────────────────────────

# Podium colours for places 1-3. Distinct from RANK_COLORS: a bronze-rank member
# can place first, so tying podium colour to rank tier would be misleading.
PLACE_COLORS = {
    1: (250, 204, 21),
    2: (198, 202, 214),
    3: (205, 127, 50),
}


def _format_remaining(ends_at, now=None) -> str:
    """Coarse 'time left' string. Deliberately not second-precision: the panel
    only re-renders every 10 minutes, so a ticking clock would be wrong for
    most of its life."""
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)

    seconds = int((ends_at - now).total_seconds())
    if seconds <= 0:
        return "Ending now"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h left"
    if hours:
        return f"{hours}h {minutes}m left"
    return f"{minutes}m left"


async def render_competition_card(rows, period_label: str, ends_at) -> discord.File:
    """Standing competition panel: the period board plus time remaining.

    `rows` carry per-period `xp`, NOT lifetime total_xp — the whole point of the
    competition board is that it is scoped to the period.
    """
    row_h = _s(84)
    header_h = _s(104)
    avatar_size = _s(56)
    W = _s(820)
    H = header_h + row_h * max(len(rows), 1) + _s(24)

    canvas, draw, ox, oy, light = _new_card(W, H)

    title_font = _load_font(36, bold=True)
    meta_font = _load_font(20)
    name_font = _load_font(26, bold=True)
    small_font = _load_font(20)

    draw.text((ox + _s(30), oy + _s(20)), f"{period_label} Competition", fill=ACCENT, font=title_font)

    remaining = _format_remaining(ends_at)
    draw.text(
        (ox + W - _s(30) - draw.textlength(remaining, font=meta_font), oy + _s(32)),
        remaining,
        fill=SUBTEXT_COLOR,
        font=meta_font,
    )
    draw.text((ox + _s(30), oy + _s(66)), "Top 3 win prizes", fill=SUBTEXT_COLOR, font=meta_font)

    if not rows:
        draw.text(
            (ox + _s(30), oy + header_h + _s(20)),
            "No activity yet this period.",
            fill=SUBTEXT_COLOR,
            font=name_font,
        )
        return _to_file(_apply_bloom(canvas, light), "competition.png")

    avatars = await _fetch_avatars([row.get("avatar_url") for row in rows], avatar_size)

    text_x = ox + _s(170)
    xp_texts = [f"{row.get('xp', 0):,} XP" for row in rows]
    xp_column = max(draw.textlength(t, font=name_font) for t in xp_texts)
    name_max = int(W - _s(30) - xp_column - _s(24)) - _s(170)
    divider = _tint(Image.new("L", (W - _s(60), max(1, SCALE // 2)), 255), DIVIDER, DIVIDER_ALPHA)

    y = oy + header_h
    for index, (row, avatar) in enumerate(zip(rows, avatars)):
        position = row.get("position", index + 1)
        # Only the podium places are coloured; the rest stay neutral so the
        # prize-winning cut-off is readable at a glance.
        accent = PLACE_COLORS.get(position, SUBTEXT_COLOR)

        draw.text((ox + _s(30), y + row_h // 2 - _s(16)), f"#{position}", fill=accent, font=name_font)
        _draw_avatar(
            canvas,
            light,
            avatar,
            ox + _s(100),
            y + (row_h - avatar_size) // 2,
            avatar_size,
            accent,
            ring_width=3,
        )

        display_name = row.get("username") or f"User {row.get('discord_id')}"
        draw.text(
            (text_x, y + _s(12)),
            _truncate(draw, display_name, name_font, name_max),
            fill=TEXT_COLOR,
            font=name_font,
        )
        draw.text(
            (text_x, y + _s(46)),
            _truncate(draw, f"{row.get('messages_sent', 0):,} msgs this period", small_font, name_max),
            fill=SUBTEXT_COLOR,
            font=small_font,
        )

        xp_text = xp_texts[index]
        draw.text(
            (ox + W - _s(30) - draw.textlength(xp_text, font=name_font), y + row_h // 2 - _s(13)),
            xp_text,
            fill=accent,
            font=name_font,
        )

        y += row_h
        if index < len(rows) - 1:
            canvas.alpha_composite(divider, dest=(ox + _s(30), y))

    return _to_file(_apply_bloom(canvas, light), "competition.png")


async def render_competition_winners_card(winners, period_label: str) -> discord.File:
    """End-of-competition podium: the top 3 and what each of them won.

    `winners` are the FROZEN rows, each with place, username, avatar_url, xp and
    a pre-formatted `prize` string.
    """
    row_h = _s(96)
    header_h = _s(104)
    avatar_size = _s(64)
    W = _s(820)
    H = header_h + row_h * max(len(winners), 1) + _s(24)

    canvas, draw, ox, oy, light = _new_card(W, H)

    title_font = _load_font(36, bold=True)
    meta_font = _load_font(20)
    name_font = _load_font(26, bold=True)
    prize_font = _load_font(22, bold=True)

    draw.text((ox + _s(30), oy + _s(20)), f"{period_label} Competition Results", fill=ACCENT, font=title_font)
    draw.text((ox + _s(30), oy + _s(66)), "Congratulations to the top 3", fill=SUBTEXT_COLOR, font=meta_font)

    if not winners:
        draw.text(
            (ox + _s(30), oy + header_h + _s(20)),
            "No qualifying activity this period.",
            fill=SUBTEXT_COLOR,
            font=name_font,
        )
        return _to_file(_apply_bloom(canvas, light), "competition-winners.png")

    avatars = await _fetch_avatars([w.get("avatar_url") for w in winners], avatar_size)

    text_x = ox + _s(180)
    prize_texts = [(w.get("prize") or "") for w in winners]
    prize_column = max([draw.textlength(t, font=prize_font) for t in prize_texts] + [0])
    name_max = int(W - _s(30) - prize_column - _s(24)) - _s(180)
    divider = _tint(Image.new("L", (W - _s(60), max(1, SCALE // 2)), 255), DIVIDER, DIVIDER_ALPHA)

    y = oy + header_h
    for index, (winner, avatar) in enumerate(zip(winners, avatars)):
        place = winner.get("place", index + 1)
        accent = PLACE_COLORS.get(place, ACCENT)

        draw.text((ox + _s(30), y + row_h // 2 - _s(16)), f"#{place}", fill=accent, font=name_font)
        _draw_avatar(
            canvas,
            light,
            avatar,
            ox + _s(100),
            y + (row_h - avatar_size) // 2,
            avatar_size,
            accent,
            ring_width=4,
        )

        display_name = winner.get("username") or f"User {winner.get('discord_id')}"
        draw.text(
            (text_x, y + _s(18)),
            _truncate(draw, display_name, name_font, name_max),
            fill=TEXT_COLOR,
            font=name_font,
        )
        draw.text(
            (text_x, y + _s(52)),
            f"{winner.get('xp', 0):,} XP this period",
            fill=SUBTEXT_COLOR,
            font=meta_font,
        )

        prize = prize_texts[index]
        if prize:
            draw.text(
                (ox + W - _s(30) - draw.textlength(prize, font=prize_font), y + row_h // 2 - _s(11)),
                prize,
                fill=accent,
                font=prize_font,
            )

        y += row_h
        if index < len(winners) - 1:
            canvas.alpha_composite(divider, dest=(ox + _s(30), y))

    return _to_file(_apply_bloom(canvas, light), "competition-winners.png")
