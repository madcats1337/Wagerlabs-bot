"""Dashboard-configurable embed styling for the link / shuffle / howl panels.

Each panel stores a JSON blob in bot_settings (one key per panel, scoped by
discord_server_id) holding {title, description, footer, accentColor, bannerUrl}.
Every field is optional — an absent or malformed blob leaves the panel rendering
its hardcoded defaults, so a bad config can never stop a panel from posting.

Panel VIEWS never load this themselves: they receive no guild_id. The manager
classes hold self.guild_id, load the config and pass it in as a kwarg — the same
way HowlPanelView already receives campaign_code.
"""

import io
import json
import logging
import re
from typing import Optional, Tuple

import aiohttp
import discord
from sqlalchemy import text

logger = logging.getLogger(__name__)

LINK_EMBED_CONFIG_KEY = "link_panel_embed_config"
SHUFFLE_EMBED_CONFIG_KEY = "shuffle_panel_embed_config"
HOWL_EMBED_CONFIG_KEY = "howl_panel_embed_config"
ROOBET_EMBED_CONFIG_KEY = "roobet_panel_embed_config"


def load_panel_embed_config(engine, guild_id, key) -> dict:
    """The guild's saved embed styling for one panel, or {} when unset/unreadable."""
    if engine is None or guild_id is None:
        return {}
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM bot_settings WHERE key = :k AND discord_server_id = :g LIMIT 1"),
                {"k": key, "g": guild_id},
            ).fetchone()
        if row and row[0]:
            cfg = json.loads(row[0])
            return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.debug(f"[panel-embed] Could not load {key} for guild {guild_id}: {e}")
    return {}


def resolve_accent(cfg, default: int) -> int:
    """accentColor ('#RRGGBB' or 'RRGGBB') as an int, falling back to the panel default."""
    raw = (cfg or {}).get("accentColor")
    if not raw:
        return default
    try:
        return int(str(raw).lstrip("#"), 16)
    except Exception:
        return default


def resolve_banner_url(cfg, engine=None, guild_id=None) -> str:
    """The configured banner URL, or '' to use the panel's bundled logo file."""
    raw = (str((cfg or {}).get("bannerUrl") or "")).strip()
    if not raw or "/branding/" in raw:
        return ""
    if raw.startswith("/") and engine is not None and guild_id is not None:
        try:
            from utils.server_urls import get_server_base_url

            base = get_server_base_url(engine, guild_id)
            if base:
                return f"{base}{raw}"
        except Exception:
            pass
    return raw


async def resolve_banner_attachment(
    banner_url: str,
    engine=None,
    filename: str = "banner.png",
) -> Tuple[str, Optional[discord.File]]:
    """Resolves a banner URL into a direct discord.File attachment when possible,
    returning ('attachment://banner.<ext>', discord.File) so Discord serves it at
    100% original resolution without external proxy compression.

    Checks the shared PostgreSQL `uploaded_files` table first for dashboard uploads,
    otherwise fetches via aiohttp if it's an HTTP(S) URL.
    Falls back gracefully to (banner_url, None) on any failure.
    """
    if not banner_url:
        return "", None

    # 1. Dashboard upload path: check PostgreSQL uploaded_files table directly
    if "/static/uploads/" in banner_url and engine is not None:
        try:
            idx = banner_url.find("/static/uploads/")
            path = banner_url[idx:]
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
            if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
                ext = "png"
            out_filename = f"banner.{ext}"

            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT file_bytes FROM uploaded_files WHERE file_path = :p OR file_path = :p_clean LIMIT 1"),
                    {"p": path, "p_clean": path.lstrip("/")},
                ).fetchone()
                if row and row[0]:
                    raw_bytes = bytes(row[0])
                    if len(raw_bytes) > 0:
                        file_obj = discord.File(io.BytesIO(raw_bytes), filename=out_filename)
                        return f"attachment://{out_filename}", file_obj
        except Exception as e:
            logger.debug(f"[panel-embed] DB fetch for banner {banner_url} failed: {e}")

    # 2. HTTP/HTTPS URL: fetch bytes via aiohttp
    if banner_url.startswith(("http://", "https://")):
        try:
            clean_url = banner_url.split("?")[0]
            ext = clean_url.rsplit(".", 1)[-1].lower() if "." in clean_url else "png"
            if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
                ext = "png"
            out_filename = f"banner.{ext}"

            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(banner_url, headers={"User-Agent": "WagerlabsBot/1.0"}) as resp:
                    if resp.status == 200:
                        raw_bytes = await resp.read()
                        if raw_bytes and len(raw_bytes) <= 15 * 1024 * 1024:  # 15MB limit
                            file_obj = discord.File(io.BytesIO(raw_bytes), filename=out_filename)
                            return f"attachment://{out_filename}", file_obj
        except Exception as e:
            logger.debug(f"[panel-embed] HTTP fetch for banner {banner_url} failed: {e}")

    # 3. Fallback to raw URL directly (previous behavior)
    return banner_url, None


def resolve_title(cfg, default_heading: str) -> str:
    """Panel heading without hardcoded formatting syntax."""
    title = (str((cfg or {}).get("title") or "")).strip()
    if not title:
        return default_heading
    return title


def resolve_footer(cfg) -> str:
    """Trailing footer text. Empty by default — these panels have no footer today."""
    return (str((cfg or {}).get("footer") or "")).strip()


def resolve_description(cfg) -> str:
    """Custom body copy, or '' to keep the panel's computed default."""
    return (str((cfg or {}).get("description") or "")).strip()


HR_REGEX = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")


def add_container_body(container, text: str, text_display_cls, separator_cls):
    """Add text to a Components V2 container, converting markdown horizontal rules
    (e.g. '---', '***') into Discord Separator components."""
    if not text:
        return
    lines = text.split("\n")
    current_chunk = []
    for line in lines:
        if HR_REGEX.match(line):
            chunk_str = "\n".join(current_chunk).strip()
            if chunk_str:
                container.add_item(text_display_cls(chunk_str))
            container.add_item(separator_cls())
            current_chunk = []
        else:
            current_chunk.append(line)
    chunk_str = "\n".join(current_chunk).strip()
    if chunk_str:
        container.add_item(text_display_cls(chunk_str))
