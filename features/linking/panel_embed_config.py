"""Dashboard-configurable embed styling for the link / shuffle / howl panels.

Each panel stores a JSON blob in bot_settings (one key per panel, scoped by
discord_server_id) holding {title, description, footer, accentColor, bannerUrl}.
Every field is optional — an absent or malformed blob leaves the panel rendering
its hardcoded defaults, so a bad config can never stop a panel from posting.

Panel VIEWS never load this themselves: they receive no guild_id. The manager
classes hold self.guild_id, load the config and pass it in as a kwarg — the same
way HowlPanelView already receives campaign_code.
"""

import json
import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

LINK_EMBED_CONFIG_KEY = "link_panel_embed_config"
SHUFFLE_EMBED_CONFIG_KEY = "shuffle_panel_embed_config"
HOWL_EMBED_CONFIG_KEY = "howl_panel_embed_config"


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


def resolve_banner_url(cfg) -> str:
    """The configured banner URL, or '' to use the panel's bundled logo file."""
    return (str((cfg or {}).get("bannerUrl") or "")).strip()


def resolve_title(cfg, default_heading: str) -> str:
    """Panel heading. Configured titles are stored without markdown, so wrap them
    to match the hardcoded '## ' headings."""
    title = (str((cfg or {}).get("title") or "")).strip()
    return f"## {title}" if title else default_heading


def resolve_footer(cfg) -> str:
    """Trailing footer text. Empty by default — these panels have no footer today."""
    return (str((cfg or {}).get("footer") or "")).strip()


def resolve_description(cfg) -> str:
    """Custom body copy, or '' to keep the panel's computed default."""
    return (str((cfg or {}).get("description") or "")).strip()
