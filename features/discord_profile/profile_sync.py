"""Discord Guild Bot Profile Synchronization.

Applies server-specific bot nickname, avatar, bio, and banner to Discord
via PATCH /guilds/{guild_id}/members/@me using discord.py's internal HTTP client.
"""

import asyncio
import base64
import logging
import mimetypes
import os
from typing import Any, Dict, Optional, Tuple

import aiohttp
import discord
from discord.http import Route
from sqlalchemy import text

from utils.log_context import server_context
from utils.server_urls import get_server_base_url

logger = logging.getLogger(__name__)


async def fetch_or_read_data_uri(
    path_or_url: Optional[str], engine=None, guild_id: Optional[int] = None
) -> Optional[str]:
    """Convert an image path, URL, or data URI into a Discord-compatible base64 Data URI."""
    if not path_or_url or not isinstance(path_or_url, str):
        return None
    path_or_url = path_or_url.strip()
    if not path_or_url:
        return None

    if path_or_url.startswith("data:image/"):
        return path_or_url

    # 1. Check persistent volume /data/uploads directly (if mounted)
    if path_or_url.startswith("/static/uploads/"):
        volume_path = os.path.join("/data/uploads", path_or_url[len("/static/uploads/") :])
        if os.path.isfile(volume_path):
            try:
                mime_type, _ = mimetypes.guess_type(volume_path)
                if not mime_type or not mime_type.startswith("image/"):
                    mime_type = "image/png"
                with open(volume_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")
                return f"data:{mime_type};base64,{encoded}"
            except Exception as e:
                logger.warning(f"Failed to read volume image file {volume_path}: {e}")

    # 2. Check local filesystem (in dev or shared disk setups)
    if path_or_url.startswith("/static/"):
        # Check standard relative path to Admin-Dashboard
        current_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.abspath(os.path.join(current_dir, "..", "..", ".."))
        local_path = os.path.join(repo_root, "Admin-Dashboard", path_or_url.lstrip("/"))

        if os.path.isfile(local_path):
            try:
                mime_type, _ = mimetypes.guess_type(local_path)
                if not mime_type or not mime_type.startswith("image/"):
                    mime_type = "image/png"
                with open(local_path, "rb") as f:
                    encoded = base64.b64encode(f.read()).decode("utf-8")
                return f"data:{mime_type};base64,{encoded}"
            except Exception as e:
                logger.warning(f"Failed to read local image file {local_path}: {e}")

    # 3. Check database uploaded_files table directly if engine is provided
    if engine and (path_or_url.startswith("/static/uploads/") or path_or_url.startswith("/uploads/")):
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT file_bytes, content_type FROM uploaded_files
                        WHERE file_path = :path OR file_path = :alt_path
                        LIMIT 1
                        """
                    ),
                    {"path": path_or_url, "alt_path": "/" + path_or_url.lstrip("/")},
                ).fetchone()
                if row and row[0]:
                    file_bytes = bytes(row[0])
                    mime_type = row[1] or "image/png"
                    encoded = base64.b64encode(file_bytes).decode("utf-8")
                    return f"data:{mime_type};base64,{encoded}"
        except Exception as e:
            logger.debug(f"DB lookup for uploaded profile asset failed: {e}")

    # 4. Fallback to fetching via HTTP/HTTPS (for remote or Railway container environments)
    target_url = path_or_url
    if path_or_url.startswith("/static/"):
        base_url = None
        if engine and guild_id:
            try:
                base_url = get_server_base_url(engine, guild_id)
            except Exception:
                base_url = None
        if not base_url:
            base_url = "https://wagerlabs.app"
        target_url = f"{base_url.rstrip('/')}{path_or_url}"

    if target_url.startswith(("http://", "https://")):
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(target_url) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        mime_type = resp.headers.get("Content-Type", "").split(";")[0].strip()
                        if not mime_type or not mime_type.startswith("image/"):
                            mime_type = mimetypes.guess_type(target_url)[0] or "image/png"
                        encoded = base64.b64encode(data).decode("utf-8")
                        return f"data:{mime_type};base64,{encoded}"
                    else:
                        logger.warning(f"Failed to fetch image from {target_url}: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Error fetching image from {target_url}: {e}")

    return None


async def apply_guild_bot_profile(
    bot: discord.Client,
    guild_id: int,
    settings: Optional[Dict[str, Any]] = None,
    engine=None,
) -> Tuple[bool, Optional[str]]:
    """Apply server-specific bot profile (nickname, avatar, bio, banner) to Discord for a guild.

    Args:
        bot: Discord Bot or Client instance
        guild_id: The Discord guild ID
        settings: Optional dictionary containing settings keys. If omitted, loads from bot_settings table.
        engine: Optional SQLAlchemy engine for DB operations.

    Returns:
        (success, error_message)
    """
    if not guild_id or int(guild_id) <= 0:
        return False, "Invalid guild ID"

    guild_id = int(guild_id)
    guild = bot.get_guild(guild_id)
    if not guild:
        return False, f"Bot is not in guild {guild_id}"

    guild_name = guild.name if guild else str(guild_id)

    with server_context(guild_id, guild_name):
        # 1. Load settings from database if not explicitly provided
        if settings is None:
            if engine is None:
                try:
                    from redis_subscriber import get_engine

                    engine = get_engine()
                except Exception:
                    pass

            if not engine:
                return False, "No database engine available to load bot profile settings"

            settings = {}
            try:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            """
                            SELECT key, value FROM bot_settings
                            WHERE discord_server_id = :gid
                              AND key IN ('discord_bot_nickname', 'discord_bot_avatar', 'discord_bot_bio', 'discord_bot_banner')
                        """
                        ),
                        {"gid": guild_id},
                    ).fetchall()
                    for r in rows:
                        settings[r[0]] = r[1]
            except Exception as e:
                logger.error(f"Error loading bot profile settings from DB for guild {guild_id}: {e}")
                return False, f"Database error loading settings: {e}"

        # If settings dict is empty (and none of the 4 keys are set), nothing to do
        keys_present = {
            k
            for k in ("discord_bot_nickname", "discord_bot_avatar", "discord_bot_bio", "discord_bot_banner")
            if k in settings
        }
        if not keys_present:
            logger.debug("No custom bot profile keys configured; skipping sync")
            return True, None

        nickname = settings.get("discord_bot_nickname")
        avatar = settings.get("discord_bot_avatar")
        bio = settings.get("discord_bot_bio")
        banner = settings.get("discord_bot_banner")

        # 2. Build Discord payload
        payload: Dict[str, Any] = {}

        if "discord_bot_nickname" in settings:
            if nickname and isinstance(nickname, str) and nickname.strip():
                payload["nick"] = nickname.strip()[:32]
            else:
                payload["nick"] = None

        if "discord_bot_bio" in settings:
            if bio and isinstance(bio, str) and bio.strip():
                payload["bio"] = bio.strip()[:190]
            else:
                payload["bio"] = None

        if "discord_bot_avatar" in settings:
            if avatar and isinstance(avatar, str) and avatar.strip():
                avatar_data_uri = await fetch_or_read_data_uri(avatar, engine=engine, guild_id=guild_id)
                if avatar_data_uri:
                    payload["avatar"] = avatar_data_uri
                else:
                    logger.warning(f"Could not convert avatar '{avatar}' to data URI")
            else:
                payload["avatar"] = None

        if "discord_bot_banner" in settings:
            if banner and isinstance(banner, str) and banner.strip():
                banner_data_uri = await fetch_or_read_data_uri(banner, engine=engine, guild_id=guild_id)
                if banner_data_uri:
                    payload["banner"] = banner_data_uri
                else:
                    logger.warning(f"Could not convert banner '{banner}' to data URI")
            else:
                payload["banner"] = None

        if not payload:
            return True, None

        # 3. Dispatch PATCH request to Discord
        route = Route("PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id)
        try:
            await bot.http.request(
                route,
                json=payload,
                reason="Customized via Wagerlabs Dashboard",
            )
            logger.info(f"✅ Successfully updated Discord bot profile for guild {guild_id}")
            return True, None
        except discord.Forbidden as e:
            if e.code == 50013:
                msg = (
                    "Bot lacks permission to change nickname in this server. "
                    "Please ensure the bot has the 'Change Nickname' permission in Discord Server Settings -> Roles."
                )
            else:
                msg = f"Discord returned 403 Forbidden: {e.text}"
            logger.warning(f"⚠️ {msg} (guild={guild_id})")
            return False, msg
        except discord.HTTPException as e:
            msg = f"Discord API error ({e.status}): {e.text}"
            logger.warning(f"⚠️ {msg} (guild={guild_id})")
            return False, msg
        except Exception as e:
            msg = f"Unexpected error updating Discord bot profile: {e}"
            logger.error(f"⚠️ {msg} (guild={guild_id})", exc_info=True)
            return False, msg


async def apply_guild_bot_profile_safe(
    bot: discord.Client,
    guild_id: int,
    settings: Optional[Dict[str, Any]] = None,
    engine=None,
) -> bool:
    """Safe wrapper around apply_guild_bot_profile that catches all exceptions and returns a bool."""
    try:
        success, _ = await apply_guild_bot_profile(bot, guild_id, settings=settings, engine=engine)
        return success
    except Exception as e:
        logger.debug(f"Exception in apply_guild_bot_profile_safe for guild {guild_id}: {e}")
        return False
