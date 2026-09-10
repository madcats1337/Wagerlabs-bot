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
import os
import re

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


def _new_card(width: int, height: int, background=None, pad=None, radius=None, shadow=True):
    """Transparent canvas holding a rounded, shadowed, gradient-filled card.

    `width`/`height` are RENDER px. Returns (canvas, draw, ox, oy, light) —
    content is positioned relative to the card's top-left corner at (ox, oy),
    so callers never deal with the margin. `light` collects emissive shapes for
    _apply_bloom at the end.

    `background` is an optional RGB Image of exactly (width, height) used as the
    card's fill — how the themable banner paints a custom colour or gradient.
    When None the stock CARD_TOP -> CARD_BOTTOM gradient is used, so every
    existing caller renders bit-for-bit what it always did.

    Three knobs exist for the BANNER, which is not a free-floating card but the
    top element of a Components V2 container that clips and rounds the image
    itself:

      `pad`    — the transparent margin holding the drop shadow. 0 for the
                 banner: Discord fits the image edge-to-edge, so the margin is
                 not a shadow but dead space beside the rows beneath it.
      `radius` — 0 for the banner. Rounded corners on an image the container
                 ALREADY rounds leave transparent notches, and the container's
                 own surface shows through them as a square behind the banner.
      `shadow` — off for the banner, for the same reason: with no margin to
                 fall into, the blur just darkens the bottom edge.

    The standalone cards keep all three: their shadow falls on Discord's own
    background and reads as depth.
    """
    pad = PAD if pad is None else pad
    radius = RADIUS if radius is None else radius
    total = (width + pad * 2, height + pad * 2)
    canvas = Image.new("RGBA", total, (0, 0, 0, 0))

    body_mask = _rounded_mask((width, height), radius)

    # Drop shadow: the body silhouette, blurred, pushed down, laid underneath.
    if shadow:
        shadow_mask = Image.new("L", total, 0)
        shadow_mask.paste(body_mask, (pad, pad))
        blurred = _tint(shadow_mask.filter(ImageFilter.GaussianBlur(SHADOW_BLUR)), (0, 0, 0), SHADOW_ALPHA)
        canvas.alpha_composite(blurred, dest=(0, SHADOW_OFFSET))

    if background is not None:
        body = background.convert("RGBA")
        if body.size != (width, height):
            body = body.resize((width, height), Image.Resampling.BILINEAR)
    else:
        body = _vertical_gradient((width, height), CARD_TOP, CARD_BOTTOM).convert("RGBA")
    body.putalpha(body_mask)
    canvas.alpha_composite(body, dest=(pad, pad))

    border = _tint(
        _rounded_outline_mask((width, height), radius, max(1, SCALE // 2)),
        CARD_BORDER,
        CARD_BORDER_ALPHA,
    )
    canvas.alpha_composite(border, dest=(pad, pad))

    highlight = _tint(_top_highlight((width, height), radius), (255, 255, 255), 46)
    canvas.alpha_composite(highlight, dest=(pad, pad))

    # Bloom is clipped to the card body so light lands on the surface instead
    # of haloing out into the transparent margin and over the drop shadow.
    clip = Image.new("L", total, 0)
    clip.paste(body_mask, (pad, pad))

    return canvas, ImageDraw.Draw(canvas), pad, pad, _Light(total, clip)


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


async def render_competition_card(rows, period_label: str) -> discord.File:
    """Standing competition panel: the period board.

    `rows` carry per-period `xp`, NOT lifetime total_xp — the whole point of the
    competition board is that it is scoped to the period.

    Deliberately renders NO countdown. The panel re-renders every 10 minutes, so
    a time baked into the pixels is stale for almost its whole life. The
    remaining time is put in the embed instead as a Discord relative timestamp
    (<t:...:R>), which every client ticks live.
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


# Podium, matching the public /leaderboards design language
# (frontend/src/pages/public/leaderboard/LeaderboardPieces.tsx): a per-place
# gradient panel whose fill fades IN from the top, a circular rank badge above a
# ringed avatar, uppercase micro-labels over each value, and 1st raised in the
# centre with render order 2 - 1 - 3.
MUTED_LABEL = (118, 118, 130)

_PODIUM_ORDER = (2, 1, 3)
_PODIUM_RAISE = {1: 24, 2: 0, 3: 0}

# (panel tint, border, badge/label accent) per place. Same yellow / gray /
# amber-700 families the web podium uses.
_PODIUM_STYLE = {
    1: {"tint": (234, 179, 8), "accent": (250, 204, 21), "border_alpha": 77},
    2: {"tint": (156, 163, 175), "accent": (209, 213, 219), "border_alpha": 51},
    3: {"tint": (180, 83, 9), "accent": (217, 119, 6), "border_alpha": 51},
}


def _podium_panel(size, tint, border_alpha: int) -> Image.Image:
    """A rounded panel whose gradient fill fades in from the top.

    The web version does this with a CSS mask-image; here the same effect is a
    vertical alpha ramp multiplied into the rounded mask, so the panel dissolves
    into the card instead of sitting on it as a hard rectangle.
    """
    w, h = size
    radius = _s(16)

    ramp = Image.new("L", (1, h))
    for y in range(h):
        # 0.04 -> 0.12 alpha over the panel height, matching the web gradient.
        t = y / max(1, h - 1)
        ramp.putpixel((0, y), int(255 * (0.04 + 0.08 * t)))
    ramp = ramp.resize((w, h), Image.Resampling.BILINEAR)

    panel = _tint(ImageChops.multiply(_rounded_mask((w, h), radius), ramp), tint)
    panel.alpha_composite(_tint(_rounded_outline_mask((w, h), radius, max(1, SCALE // 2)), tint, border_alpha))
    return panel


async def render_competition_winners_card(winners, period_label: str) -> discord.File:
    """End-of-competition podium, styled after the public leaderboard podium.

    `winners` are the FROZEN rows, each with place, username, avatar_url, xp and
    a pre-formatted `prize` string. Fewer than three is normal - a quiet period
    renders only the places actually won, and the group stays centred.
    """
    W = _s(900)
    column_w = _s(268)
    gap = _s(16)
    panel_top = _s(96)

    # Vertical rhythm inside a panel, in design units from the panel's top. The
    # panel HEIGHT is DERIVED from these rather than guessed: a fixed height left
    # ~70 units of dead space under the last line, and moving the content down
    # only shifted that gap instead of closing it.
    _BADGE_TOP, _BADGE = 22, 42
    _AVATAR_GAP, _AVATAR = 18, 74
    _NAME_GAP, _NAME_H = 16, 30
    _LABEL_H, _SECTION_GAP = 20, 24
    _VALUE_H, _PRIZE_H = 28, 34
    _PANEL_BOTTOM = 18  # room under the last value, mirroring the top inset

    name_y_rel = _BADGE_TOP + _BADGE + _AVATAR_GAP + _AVATAR + _NAME_GAP
    score_label_rel = name_y_rel + _NAME_H + _SECTION_GAP
    reward_label_rel = score_label_rel + _LABEL_H + _VALUE_H + _SECTION_GAP
    panel_h = _s(reward_label_rel + _LABEL_H + _PRIZE_H + _PANEL_BOTTOM)
    # Card ends just below the panel floor. The panels share a floor at
    # panel_top + panel_h (1st is raised but grown to match), so anything beyond
    # a small margin here is dead space under all three at once.
    # The un-raised panels sit lowest, so they set the card's bottom edge.
    H = panel_top + panel_h + _s(22)

    canvas, draw, ox, oy, light = _new_card(W, H)

    title_font = _load_font(34, bold=True)
    badge_font = _load_font(20, bold=True)
    name_font = _load_font(23, bold=True)
    label_font = _load_font(14, bold=True)
    value_font = _load_font(19)
    prize_font = _load_font(24, bold=True)

    draw.text((ox + _s(30), oy + _s(20)), f"{period_label} Competition Results", fill=ACCENT, font=title_font)

    by_place = {w.get("place", i + 1): w for i, w in enumerate(winners)}
    if not by_place:
        draw.text(
            (ox + _s(30), oy + _s(200)),
            "No qualifying activity this period.",
            fill=SUBTEXT_COLOR,
            font=name_font,
        )
        return _to_file(_apply_bloom(canvas, light), "competition-winners.png")

    present = [p for p in _PODIUM_ORDER if p in by_place]
    avatar_size = _s(_AVATAR)
    avatars = await _fetch_avatars([by_place[p].get("avatar_url") for p in present], avatar_size)
    avatar_by_place = dict(zip(present, avatars))

    total_w = len(present) * column_w + (len(present) - 1) * gap
    x = ox + (W - total_w) // 2
    panel_divider = _tint(Image.new("L", (column_w - _s(56), max(1, SCALE // 2)), 255), DIVIDER, DIVIDER_ALPHA)

    for place in present:
        winner = by_place[place]
        style = _PODIUM_STYLE.get(place, _PODIUM_STYLE[1])
        accent = style["accent"]
        centre_x = x + column_w // 2
        # 1st is LIFTED as a whole, the way the web podium does with
        # -translate-y-6 — every panel keeps the same height. Growing it instead
        # (to land on a shared floor) turned the entire raise into dead space
        # under the reward, since the content does not stretch with the panel.
        raise_by = _s(_PODIUM_RAISE.get(place, 0))
        top = oy + panel_top - raise_by

        canvas.alpha_composite(
            _podium_panel((column_w, panel_h), style["tint"], style["border_alpha"]),
            dest=(x, top),
        )

        # Circular rank badge.
        badge_size = _s(_BADGE)
        badge_x = centre_x - badge_size // 2
        badge_y = top + _s(_BADGE_TOP)
        canvas.alpha_composite(_tint(_circle_mask(badge_size), style["tint"], 51), dest=(badge_x, badge_y))
        canvas.alpha_composite(_tint(_ring_mask(badge_size, max(1, SCALE)), accent, 102), dest=(badge_x, badge_y))
        badge_text = f"#{place}"
        draw.text(
            (centre_x - draw.textlength(badge_text, font=badge_font) / 2, badge_y + _s(9)),
            badge_text,
            fill=accent,
            font=badge_font,
        )

        avatar_y = badge_y + badge_size + _s(_AVATAR_GAP)
        _draw_avatar(
            canvas,
            light,
            avatar_by_place[place],
            centre_x - avatar_size // 2,
            avatar_y,
            avatar_size,
            accent,
            ring_width=3,
        )

        name = _truncate(
            draw, winner.get("username") or f"User {winner.get('discord_id')}", name_font, column_w - _s(24)
        )
        name_y = top + _s(name_y_rel)
        draw.text(
            (centre_x - draw.textlength(name, font=name_font) / 2, name_y),
            name,
            fill=TEXT_COLOR,
            font=name_font,
        )

        # Uppercase micro-label over each value, as on the web podium.
        def _labelled(label, value, value_font, value_fill, top):
            draw.text(
                (centre_x - draw.textlength(label, font=label_font) / 2, top),
                label,
                fill=MUTED_LABEL,
                font=label_font,
            )
            draw.text(
                (centre_x - draw.textlength(value, font=value_font) / 2, top + _s(20)),
                value,
                fill=value_fill,
                font=value_font,
            )

        def _divider(top):
            """Hairline between sections, inset from the panel edge.

            Same DIVIDER/DIVIDER_ALPHA the leaderboard rows use, so every
            separator in the card set reads at one weight.
            """
            canvas.alpha_composite(panel_divider, dest=(x + _s(28), top))

        # The three sections are spread down the panel rather than stacked at
        # the top: the reward is the payoff line, so it sits low with the dead
        # space distributed between the groups instead of below them.
        score_top = top + _s(score_label_rel)
        _divider(score_top - _s(14))
        _labelled("SCORE", f"{winner.get('xp', 0):,}", value_font, TEXT_COLOR, score_top)

        prize = _truncate(draw, winner.get("prize") or "", prize_font, column_w - _s(24))
        if prize:
            reward_top = top + _s(reward_label_rel)
            _divider(reward_top - _s(14))
            _labelled("REWARD", prize, prize_font, accent, reward_top)

        x += column_w + gap

    return _to_file(_apply_bloom(canvas, light), "competition-winners.png")


# -- Leaderboard banners -----------------------------------------------------
#
# The banner is the standing panel's header image. It replaces the old
# full-board render: the ROWS are now text in the message body (paginated, 10
# to a page), so the image only carries identity and a few headline values.
#
# That split is deliberate. A rendered row costs an image re-render and a
# re-upload on every page turn and every refresh, and none of it is selectable
# or copyable in Discord. A banner is rendered once per refresh and stays valid
# across every page, so paging is a pure message edit.
#
# Appearance is themable from the dashboard (Levels -> Appearance): title font,
# title colour and background. See BannerTheme below.

# DISPLAY size. 520 matches the width Discord gives an EMBED, so the banner
# attached above the board lines up with it instead of overhanging by the ~30px
# an image gets from the wider message content area. The height keeps the
# original 4.1:1 proportion.
BANNER_W = 520
BANNER_H = 127

# The banner's interior is still authored against the ORIGINAL 820x200 canvas —
# every type size and offset below is in those units. _bs() folds in the ratio
# to the display size, so the whole design scales as one piece and the numbers
# stay comparable to the cards' own.
_BANNER_DESIGN_W = 820

# Rasterise at 3x the display size rather than the cards' 2x: at 520 display px
# a 2x render is only 1040px, which Discord upscales on a HiDPI display and
# softens. 3x keeps the type crisp at the smaller size.
BANNER_SCALE = 3

_BANNER_UNIT = BANNER_SCALE * BANNER_W / _BANNER_DESIGN_W


def _bs(value) -> int:
    """Banner design units (820-wide canvas) -> render pixels."""
    return int(round(value * _BANNER_UNIT))


_BANNER_CHIP_TINT = (255, 255, 255)
_BANNER_CHIP_ALPHA = 10
_BANNER_CHIP_BORDER_ALPHA = 20

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "assets", "fonts")

# Fonts the RENDERER can actually use, keyed by the value the dashboard stores.
#
# Deliberately a closed set of faces bundled in assets/fonts/ rather than the
# widget editor's ~30 webfonts: those are loaded by the BROWSER, and PIL can
# only draw a face that exists on the bot host. Offering a font we cannot
# rasterise would silently fall back to the default and make the dashboard
# preview a lie. Keep in sync with BANNER_FONTS in the dashboard's
# utils/levels_banner.py -- the dropdown is built from that mirror.
#
# `weight` is the variable-font weight axis to pin for the bold/title cut;
# None means the file is already the weight we want.
_BANNER_FONTS = {
    # "default" points at the BUNDLED DejaVu rather than falling through to
    # _load_font's host search. That search resolves DejaVu on Railway and Arial
    # on a Windows dev box — two different faces, so the dashboard's preview and
    # the posted banner would disagree depending on which host drew them.
    # Bundling makes the default deterministic on every host.
    "default": {"regular": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf", "weight": None},
    "anton": {"regular": "Anton.ttf", "bold": "Anton.ttf", "weight": None},
    "bebas": {"regular": "BebasNeue.ttf", "bold": "BebasNeue.ttf", "weight": None},
    "oswald": {"regular": "Oswald.ttf", "bold": "Oswald.ttf", "weight": 700},
    "russo": {"regular": "RussoOne.ttf", "bold": "RussoOne.ttf", "weight": None},
    "orbitron": {"regular": "Orbitron.ttf", "bold": "Orbitron.ttf", "weight": 700},
    "poppins": {"regular": "Poppins-Regular.ttf", "bold": "Poppins-Bold.ttf", "weight": None},
    "playfair": {"regular": "PlayfairDisplay.ttf", "bold": "PlayfairDisplay.ttf", "weight": 700},
    "inter": {"regular": "Inter.ttf", "bold": "Inter.ttf", "weight": 700},
}

DEFAULT_BANNER_FONT = "default"


def _banner_fallback_font(size: int, bold: bool):
    """The stock face at BANNER_SCALE.

    _load_font scales by the CARDS' SCALE, which would render banner type at the
    wrong size the moment a font key is unknown or its file is missing — so the
    fallback resolves the same paths itself at the banner's scale.
    """
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"]
        if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"]
    )
    for path in candidates:
        font = _open_font(path, _bs(size))
        if font is not None:
            return font
    return ImageFont.load_default()


def _load_banner_font(font_key: str, size: int, bold: bool = False):
    """A bundled face at `size` banner-design units, or the built-in default.

    Falls back to the stock face for an unknown key or a missing/corrupt file,
    so a stale setting degrades to the stock look instead of failing the render.
    """
    spec = _BANNER_FONTS.get(font_key or DEFAULT_BANNER_FONT)
    if not spec or not spec.get("bold" if bold else "regular"):
        return _banner_fallback_font(size, bold)

    path = os.path.join(_FONT_DIR, spec["bold"] if bold else spec["regular"])
    font = _open_font(path, _bs(size))
    if font is None:
        return _banner_fallback_font(size, bold)

    # Variable faces default to Regular; pin the weight axis for the bold cut.
    weight = spec.get("weight")
    if bold and weight:
        try:
            font.set_variation_by_axes([float(weight)])
        except Exception:
            # A static instance of a face we expected to be variable: the file
            # is already at its only weight, so it is fine as-is.
            pass
    return font


def _parse_color(value, fallback):
    """A CSS-ish colour string -> an RGBA tuple, or `fallback`.

    Accepts "#rgb", "#rrggbb", "#rrggbbaa" and "rgb()/rgba()".
    Alpha is parsed so gradients can fade to transparent (revealing the
    default card background behind them).
    """
    if not value or not isinstance(value, str):
        return fallback

    text_value = value.strip().lower()
    if text_value.startswith("#"):
        hex_digits = text_value[1:]
        if len(hex_digits) in (3, 4):
            hex_digits = "".join(c * 2 for c in hex_digits)
        if len(hex_digits) in (6, 8):
            try:
                r, g, b = (int(hex_digits[i : i + 2], 16) for i in (0, 2, 4))
                a = int(hex_digits[6:8], 16) if len(hex_digits) == 8 else 255
                return (r, g, b, a)
            except ValueError:
                return fallback
        return fallback

    if text_value.startswith("rgb"):
        inner = text_value[text_value.find("(") + 1 : text_value.rfind(")")]
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) not in (3, 4):
            return fallback
        try:
            r, g, b = (max(0, min(255, int(round(float(p))))) for p in parts[:3])
            a = max(0, min(255, int(round(float(parts[3]) * 255)))) if len(parts) == 4 else 255
            return (r, g, b, a)
        except (ValueError, IndexError):
            return fallback

    return fallback


# linear-gradient(<angle>deg, <color> <pos>%, ...) -- the picker output form.
# Only the colour stops are read; see _banner_background for the angle handling.
#
# IGNORECASE: the dashboard's colour picker upper-cases the stop being edited
# ("RGBA(...)"). The dashboard now strips that before storing, but rows saved
# before that fix still carry it, and a case-sensitive match would drop the
# stop and render an N-stop gradient with N-1 stops. MIRRORS the dashboard's
# utils/levels_banner_render.py -- keep the two in sync.
_GRADIENT_STOP = re.compile(r"(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))(?:\s+(\d+(?:\.\d+)?)%)?", re.IGNORECASE)


def _parse_gradient(value):
    """(kind, stops, angle) for a background string."""
    if not value or not isinstance(value, str):
        return "solid", [(None, 0.0)], 0

    text_value = value.strip()
    if not text_value.lower().startswith("linear-gradient"):
        return "solid", [(text_value, 0.0)], 0

    stops_matches = _GRADIENT_STOP.findall(text_value)

    parsed_stops = []
    for c, p in stops_matches:
        parsed_stops.append((c, float(p) / 100.0 if p else None))

    if not parsed_stops:
        return "solid", [(None, 0.0)], 0

    if parsed_stops[0][1] is None:
        parsed_stops[0] = (parsed_stops[0][0], 0.0)
    if parsed_stops[-1][1] is None:
        parsed_stops[-1] = (parsed_stops[-1][0], 1.0)

    for i in range(1, len(parsed_stops) - 1):
        if parsed_stops[i][1] is None:
            next_defined = i + 1
            while next_defined < len(parsed_stops) and parsed_stops[next_defined][1] is None:
                next_defined += 1
            prev_pos = parsed_stops[i - 1][1]
            next_pos = parsed_stops[next_defined][1]
            step = (next_pos - prev_pos) / (next_defined - i + 1)
            parsed_stops[i] = (parsed_stops[i][0], prev_pos + step)

    angle = 180.0
    import re as regex

    angle_match = regex.match(r"linear-gradient\(\s*(-?\d+(?:\.\d+)?)(?:deg)?\s*,", text_value, regex.IGNORECASE)
    if angle_match:
        angle = float(angle_match.group(1))

    return "linear", parsed_stops, angle


def _multi_gradient(size, stops, angle_deg: float) -> Image.Image:
    """An N-stop linear gradient at an arbitrary angle."""
    import math

    width, height = size
    theta = math.radians(90 - angle_deg)

    span = int(math.ceil(abs(width * math.cos(theta)) + abs(height * math.sin(theta))))
    if span < 1:
        span = 1

    strip = Image.new("RGBA", (span, 1))
    pixels = strip.load()

    stops = sorted(stops, key=lambda x: x[1])

    for i in range(span):
        t = i / max(1, span - 1)

        start_idx = 0
        for idx in range(len(stops) - 1):
            if t <= stops[idx + 1][1]:
                start_idx = idx
                break
        else:
            start_idx = len(stops) - 2

        start_c, start_pos = stops[start_idx]
        end_c, end_pos = stops[start_idx + 1]

        segment_len = end_pos - start_pos
        if segment_len <= 0:
            local = 1.0
        else:
            local = max(0.0, min(1.0, (t - start_pos) / segment_len))

        s = start_c if len(start_c) == 4 else start_c + (255,)
        e = end_c if len(end_c) == 4 else end_c + (255,)

        blended = tuple(int(round(s[c] + (e[c] - s[c]) * local)) for c in range(4))
        pixels[i, 0] = blended

    diagonal = int(math.ceil(math.hypot(width, height)))
    rect = strip.resize((span, diagonal), Image.Resampling.BILINEAR)
    rot = rect.rotate(90 - angle_deg, resample=Image.Resampling.BILINEAR, expand=True)

    rx, ry = rot.size
    left = (rx - width) // 2
    top = (ry - height) // 2
    return rot.crop((left, top, left + width, top + height))


def _banner_background(size, background):
    """The banner body fill: a solid colour, a gradient, or the stock card."""
    if not background:
        return None

    kind, raw_stops, angle = _parse_gradient(background)
    if kind == "solid":
        solid = _parse_color(raw_stops[0][0], None)
        if solid:
            solid = solid if len(solid) == 4 else solid + (255,)
            return Image.new("RGBA", size, solid)
        return None

    parsed_stops = []
    for c_str, pos in raw_stops:
        c = _parse_color(c_str, None)
        if c is not None:
            parsed_stops.append((c, pos))

    if len(parsed_stops) < 2:
        if parsed_stops:
            solid = parsed_stops[0][0]
            solid = solid if len(solid) == 4 else solid + (255,)
            return Image.new("RGBA", size, solid)
        return None

    return _multi_gradient(size, parsed_stops, angle)


class BannerTheme:
    """Dashboard-controlled banner appearance.

    Any field left None keeps the stock look, so an unconfigured server renders
    exactly what it rendered before theming existed.
    """

    __slots__ = ("font", "title_color", "background")

    def __init__(self, font=None, title_color=None, background=None):
        self.font = font or DEFAULT_BANNER_FONT
        self.title_color = title_color
        self.background = background

    @classmethod
    def from_settings(cls, settings):
        """Read the theme off a BotSettingsManager, tolerating a missing one."""
        if settings is None:
            return cls()
        return cls(
            font=getattr(settings, "levels_banner_font", None),
            title_color=getattr(settings, "levels_banner_title_color", None),
            background=getattr(settings, "levels_banner_background", None),
        )

    @property
    def accent(self):
        return _parse_color(self.title_color, ACCENT)


def _banner_chip(size, radius: int) -> Image.Image:
    """A soft rounded plate for one headline value."""
    plate = _tint(_rounded_mask(size, radius), _BANNER_CHIP_TINT, _BANNER_CHIP_ALPHA)
    plate.alpha_composite(
        _tint(_rounded_outline_mask(size, radius, max(1, SCALE // 2)), _BANNER_CHIP_TINT, _BANNER_CHIP_BORDER_ALPHA)
    )
    return plate


def _ink_box(draw, text_value: str, font):
    """(left, top, right, bottom) of a string's VISIBLE ink, relative to an
    anchor-less draw.text at the origin.

    PIL positions text by the font's ascent box, not by the glyphs, and the
    empty space that box reserves above the cap height differs per face — 21%
    of the em on DejaVu, far more on a display face like Bebas. Centring by
    that box makes text sit visibly low inside a chip, and by a DIFFERENT
    amount for every font the operator can pick. textbbox reports the real ink
    extent, so callers can centre on what the eye actually sees.
    """
    return draw.textbbox((0, 0), text_value, font=font)


def _draw_centered_ink(draw, text_value: str, font, center_x: int, center_y: int, fill):
    """Draw `text_value` with its INK centred on (center_x, center_y)."""
    left, top, right, bottom = _ink_box(draw, text_value, font)
    x = center_x - (left + right) / 2
    y = center_y - (top + bottom) / 2
    draw.text((x, y), text_value, fill=fill, font=font)


def _draw_banner_stats(canvas, draw, ox, oy, stats, font_key, H_render):
    """Row of chips along the bottom of a banner.

    `stats` is a list of (label, value) pairs laid out on an even grid across
    the card's inner width, so two, three or four chips all stay balanced
    without per-count special-casing.

    Both lines are placed by their INK rather than their text box (see
    _ink_box): the label and value are two different faces/sizes, and the
    unequal ascent space each reserves is what made the pair read as sitting
    low and off-centre in the chip. The two are laid out as one block —
    measured, stacked with a fixed optical gap, then centred as a unit — so the
    result stays balanced across every bundled font.
    """
    if not stats:
        return

    label_font = _load_banner_font(font_key, 17)
    value_font = _load_banner_font(font_key, 26, bold=True)

    inner_x = ox + _bs(30)
    inner_w = (canvas.width - 2 * ox) - _bs(60)
    gap = _bs(12)
    chip_w = (inner_w - gap * (len(stats) - 1)) // len(stats)
    chip_h = _bs(64)
    chip_y = oy + H_render - _bs(24) - chip_h
    plate = _banner_chip((chip_w, chip_h), _bs(14))
    max_text_w = chip_w - _bs(16)

    # Optical gap between the label's baseline row and the value's cap row.
    line_gap = _bs(7)

    for index, (label, value) in enumerate(stats):
        x = inner_x + index * (chip_w + gap)
        canvas.alpha_composite(plate, dest=(x, chip_y))

        label_text = _truncate(draw, str(label).upper(), label_font, max_text_w)
        value_text = _truncate(draw, str(value), value_font, max_text_w)

        # Measure both inks, stack them, and centre the whole block. Using each
        # string's own ink height (not the font's line height) keeps a chip
        # whose value has no descender from sitting differently to one that does.
        _, l_top, _, l_bottom = _ink_box(draw, label_text, label_font)
        _, v_top, _, v_bottom = _ink_box(draw, value_text, value_font)
        label_h = l_bottom - l_top
        value_h = v_bottom - v_top
        block_h = label_h + line_gap + value_h

        center_x = x + chip_w // 2
        block_top = chip_y + (chip_h - block_h) / 2

        _draw_centered_ink(draw, label_text, label_font, center_x, block_top + label_h / 2, MUTED_LABEL)
        _draw_centered_ink(
            draw, value_text, value_font, center_x, block_top + label_h + line_gap + value_h / 2, TEXT_COLOR
        )


def render_banner_png(title: str, subtitle: str, stats, theme=None) -> bytes:
    """The banner as raw PNG bytes.

    Split out from render_leaderboard_banner so the DASHBOARD can render a
    byte-identical preview: it holds no discord.py dependency, and its twin in
    Admin-Dashboard/utils/levels_banner_render.py is a copy of this same code.
    A preview that merely approximated the real renderer would drift from it,
    and the operator would only find out after posting.
    """
    theme = theme or BannerTheme()
    # BANNER_W/H are DISPLAY dimensions, so they scale by BANNER_SCALE alone —
    # _bs() is for the 820-canvas design units used inside.
    W, H = BANNER_W * BANNER_SCALE, BANNER_H * BANNER_SCALE

    # pad=0: the banner is the top element of a Components V2 container, which
    # fits an image edge-to-edge. The usual transparent shadow margin would
    # render as ~16px of dead space on each side and push the banner visibly out
    # of line with the text rows beneath it.
    canvas, draw, ox, oy, light = _new_card(
        W, H, background=_banner_background((W, H), theme.background), pad=0, radius=0, shadow=False
    )

    title_font = _load_banner_font(theme.font, 40, bold=True)
    subtitle_font = _load_banner_font(theme.font, 21)

    draw.text(
        (ox + _bs(30), oy + _bs(26)),
        _truncate(draw, title, title_font, W - _bs(60)),
        fill=theme.accent,
        font=title_font,
    )
    if subtitle:
        draw.text(
            (ox + _bs(30), oy + _bs(76)),
            _truncate(draw, subtitle, subtitle_font, W - _bs(60)),
            fill=SUBTEXT_COLOR,
            font=subtitle_font,
        )

    _draw_banner_stats(canvas, draw, ox, oy, stats, theme.font, H)

    buf = io.BytesIO()
    _apply_bloom(canvas, light).save(buf, format="PNG")
    return buf.getvalue()


async def render_leaderboard_banner(title: str, subtitle: str, stats, theme=None) -> discord.File:
    """Header image for a paginated leaderboard panel, as a discord.File."""
    png = render_banner_png(title, subtitle, stats, theme=theme)
    return discord.File(io.BytesIO(png), filename="leaderboard-banner.png")
