"""
Shared stream go-live notification sender (platform-agnostic).

Both the Kick webhook (livestream.status.updated) and the Twitch EventSub webhook
(stream.online/offline) call send_stream_notification() so the "X is now LIVE"
Discord post, the Redis publish, and the clip-buffer trigger behave identically
across platforms. Only the stream URL, the embed-proxy URL, and the platform
label differ per platform.

Extracted from the original Kick on_stream_status handler in core/kick_webhooks.py
so there is a single source of truth for go-live behavior.
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


# Per-platform URL builders. Kick uses clkick.com as a Discord-unfurl video proxy;
# Twitch HLS is locked down so we fall back to the channel page (the dashboard
# /stream/live embed for Twitch is deferred — see the plan §D).
def _stream_url(platform: str, streamer: str) -> str:
    if platform == "twitch":
        return f"https://twitch.tv/{streamer}"
    return f"https://kick.com/{streamer}"


def _embed_url(platform: str, streamer: str) -> str:
    if platform == "twitch":
        # No video-unfurl proxy yet for Twitch; link the channel directly.
        return f"https://twitch.tv/{streamer}"
    return f"https://clkick.com/{streamer}"


def _platform_label(platform: str) -> str:
    return "Twitch" if platform == "twitch" else "Kick"


def _sanitize_button_url(url: str) -> str:
    """Return a Discord-valid link-button URL, or "" if it can't be salvaged.

    Discord rejects the whole message (400 / URL_TYPE_INVALID_SCHEME) if any
    link button has a bad scheme, so one malformed saved URL must not break the
    go-live alert. Repairs the common `https:/host` → `https://host` typo (a
    single slash, the actual bug seen in the wild) and missing scheme
    (`twitch.tv/x` → `https://twitch.tv/x`); anything that still isn't an
    http(s) URL is dropped so the caller falls back to the stream URL.
    """
    import re

    u = (url or "").strip()
    if not u:
        return ""
    # `https:/host` or `http:/host` (one slash after the colon) → two slashes.
    u = re.sub(r"^(https?):/(?!/)", r"\1://", u, flags=re.IGNORECASE)
    low = u.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return u
    # Scheme-less but otherwise URL-ish (has a dot before any slash) → assume https.
    if "://" not in u and "." in u.split("/", 1)[0]:
        return "https://" + u
    return ""


# Per-platform live-alert settings live under these prefixes. Kick falls back to
# the legacy shared `stream_notification_*` keys so existing servers keep working.
_ALERT_SUFFIXES = ("enabled", "channel_id", "title", "description", "link_text", "link_small", "footer", "buttons")

# Discord caps an action row at 5 buttons and a button label at 80 chars.
_MAX_ALERT_BUTTONS = 5
_BUTTON_LABEL_MAX = 80

# Fallback when a server has never configured buttons: a single Watch Stream
# button that auto-links the live stream (blank url ⇒ stream_url).
_DEFAULT_ALERT_BUTTON = {"label": "Watch Stream", "emoji": "🔴", "url": ""}


def _alert_columns(platform: str) -> list:
    """The bot_settings keys to SELECT for a platform's live alert (new + legacy)."""
    prefix = "twitch_live_alert_" if platform == "twitch" else "kick_live_alert_"
    keys = [f"{prefix}{s}" for s in _ALERT_SUFFIXES]
    if platform != "twitch":
        keys += [f"stream_notification_{s}" for s in _ALERT_SUFFIXES]
    return keys


def alert_setting(settings: dict, platform: str, suffix: str):
    """Resolve a live-alert setting for a platform: the per-platform key, falling
    back to the legacy `stream_notification_*` key for Kick."""
    prefix = "twitch_live_alert_" if platform == "twitch" else "kick_live_alert_"
    val = settings.get(f"{prefix}{suffix}")
    if (val is None or val == "") and platform != "twitch":
        val = settings.get(f"stream_notification_{suffix}")
    return val


def _build_live_message(settings: dict, platform: str, streamer: str, title: str, category: str):
    """Build the (content, footer) for a go-live message from a platform's alert
    settings. Kick includes the clkick video-unfurl link; Twitch has no video
    proxy, so it omits the hidden link (the Watch Stream button links the channel).
    """

    def repl(t):
        if not t:
            return t
        return (
            t.replace("{streamer}", streamer)
            .replace("{channel}", streamer)
            .replace("{title}", title or "")
            .replace("{category}", category or "")
        )

    custom_title = alert_setting(settings, platform, "title")
    title_text = (
        repl(custom_title) if custom_title else f"🔴 **{streamer}** is now LIVE on {_platform_label(platform)}!"
    )

    custom_description = alert_setting(settings, platform, "description")
    parts = [title_text]
    if custom_description:
        parts.append(repl(custom_description))

    if platform == "twitch":
        # Twitch has no video-unfurl proxy — include a plain clickable channel link
        # (auto-built from the connected handle) so the alert body links the stream.
        link_text = alert_setting(settings, platform, "link_text") or "Watch on Twitch"
        parts.append(f"[{link_text}]({_embed_url(platform, streamer)})")
    else:
        # Kick: the clkick unfurl link that Discord turns into the video preview.
        link_text = alert_setting(settings, platform, "link_text") or "Watch Preview"
        hidden_link = f"[{link_text}]({_embed_url(platform, streamer)})"
        if alert_setting(settings, platform, "link_small") == "true":
            hidden_link = f"-# {hidden_link}"
        parts.append(hidden_link)

    message_content = "\n".join(parts)
    footer_text = repl(alert_setting(settings, platform, "footer"))
    return message_content, footer_text


# Emoji values prefixed with this reference an uploaded application emoji (by
# name) rather than a unicode glyph — see the dashboard's APP_EMOJIS catalog.
_APP_EMOJI_PREFIX = "app:"

# Cache of {name: {"id", "name", "animated"}} application emojis so we don't hit
# the Discord REST API on every go-live. Refreshed lazily on the first alert that
# needs a custom emoji; app emojis change rarely (only when assets are re-uploaded).
_app_emoji_cache: Optional[dict] = None


async def _fetch_application_emojis(session, bot_token: str) -> dict:
    """Return {name: {"id", "name", "animated"}} for the bot's application emojis,
    via raw REST (no discord.py client needed). Cached after the first success;
    returns {} on any failure so callers fall back to unicode."""
    global _app_emoji_cache
    if _app_emoji_cache is not None:
        return _app_emoji_cache
    headers = {"Authorization": f"Bot {bot_token}"}
    try:
        # The bot's application id; for a bot it equals the bot user id.
        async with session.get("https://discord.com/api/v10/applications/@me", headers=headers) as r:
            if r.status != 200:
                return {}
            app_id = (await r.json()).get("id")
        if not app_id:
            return {}
        async with session.get(f"https://discord.com/api/v10/applications/{app_id}/emojis", headers=headers) as r:
            if r.status != 200:
                return {}
            payload = await r.json()
        items = payload.get("items", payload) if isinstance(payload, dict) else payload
        cache = {}
        for e in items or []:
            name = e.get("name")
            if name:
                cache[name] = {"id": e["id"], "name": name, "animated": bool(e.get("animated"))}
        _app_emoji_cache = cache
        return cache
    except Exception as e:  # noqa: BLE001 — emoji is cosmetic; never block the alert
        logger.info(f"[StreamNotify] ⚠️ Could not fetch application emojis (using unicode fallback): {e}")
        return {}


async def _resolve_emoji(raw_emoji: str, session, bot_token: str):
    """Turn a stored emoji value into a Discord button `emoji` dict, or None.

    - "app:<name>"  → custom emoji {"id", "name", "animated"} (None if unknown).
    - unicode glyph → {"name": "<glyph>"}.
    - blank         → None.
    """
    emoji = (raw_emoji or "").strip()
    if not emoji:
        return None
    if emoji.startswith(_APP_EMOJI_PREFIX):
        name = emoji[len(_APP_EMOJI_PREFIX) :]
        resolved = (await _fetch_application_emojis(session, bot_token)).get(name)
        return resolved  # None ⇒ button renders with no emoji (graceful)
    return {"name": emoji}


async def build_alert_components(settings: dict, platform: str, stream_url: str, session, bot_token: str) -> list:
    """Build the Discord message `components` (one action row of link buttons) for
    a go-live alert.

    Buttons come from the per-platform `*_live_alert_buttons` setting (JSON array
    of {label, emoji, url}). The FIRST button is the primary "Watch Stream"
    button: a blank url auto-links `stream_url`. Extra buttons need both a label
    and a url or they're dropped. Falls back to a single auto Watch Stream button
    when nothing is configured. Discord limits: 5 buttons/row, 80-char labels.

    `emoji` may be a unicode glyph or an "app:<name>" application-emoji token,
    resolved to the custom-emoji dict via the bot token (`session` is the open
    aiohttp session the caller is already using to post).
    """
    import json

    raw = alert_setting(settings, platform, "buttons")
    buttons = None
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                buttons = parsed
        except (ValueError, TypeError):
            buttons = None
    if buttons is None:
        buttons = [dict(_DEFAULT_ALERT_BUTTON)]

    components = []
    for i, btn in enumerate(buttons[:_MAX_ALERT_BUTTONS]):
        if not isinstance(btn, dict):
            continue
        label = str(btn.get("label") or "").strip()
        emoji = str(btn.get("emoji") or "").strip()
        # Sanitize so a malformed saved URL (e.g. `https:/twitch.tv/x`) can't
        # 400 the whole alert; unsalvageable URLs become "" and are handled
        # below (primary → stream_url, extra → dropped).
        url = _sanitize_button_url(str(btn.get("url") or ""))
        is_primary = i == 0
        # Primary button: blank url ⇒ the live stream; blank label ⇒ "Watch Stream".
        if is_primary:
            if not url:
                url = stream_url
            if not label:
                label = "Watch Stream"
        else:
            # Extra buttons require both a label and a url; skip incomplete ones.
            if not label or not url:
                continue
        button = {"type": 2, "style": 5, "label": label[:_BUTTON_LABEL_MAX], "url": url}
        emoji_obj = await _resolve_emoji(emoji, session, bot_token)
        if emoji_obj:
            button["emoji"] = emoji_obj
        components.append(button)

    if not components:
        # Defensive: never post a live alert with zero buttons.
        components.append({"type": 2, "style": 5, "label": "Watch Stream", "url": stream_url, "emoji": {"name": "🔴"}})

    return [{"type": 1, "components": components}]


async def send_stream_notification(
    discord_server_id,
    streamer: str,
    is_live: bool,
    title: str = "",
    category: str = "",
    platform: str = "kick",
):
    """
    Post the go-live notification to Discord (if enabled for the server), publish
    the stream status to Redis, and trigger the clip buffer.

    Mirrors the original Kick behavior exactly; `platform` selects the URLs/label.
    """
    if not discord_server_id:
        return

    # ---- 1. Discord notification (only on go-live) ----
    if is_live:
        await _post_discord_notification(discord_server_id, streamer, title, category, platform)

    # ---- 2. Redis publish (dashboard) ----
    try:
        from utils.redis_publisher import bot_redis_publisher

        if is_live:
            bot_redis_publisher.publish_stream_live(
                discord_server_id=str(discord_server_id),
                streamer=streamer,
                stream_url=_stream_url(platform, streamer),
            )
        else:
            bot_redis_publisher.publish_stream_offline(discord_server_id=str(discord_server_id), streamer=streamer)
        logger.info(f"[StreamNotify] 📤 Published stream_{'live' if is_live else 'offline'} ({platform})")
    except Exception as e:
        logger.info(f"[StreamNotify] ⚠️ Failed to publish stream status to Redis: {e}")

    # ---- 3. Clip buffer start/stop (reuses dashboard clip API) ----
    await _control_clip_buffer(discord_server_id, streamer, is_live, platform)


async def _post_discord_notification(discord_server_id, streamer, title, category, platform):
    try:
        import aiohttp
        from sqlalchemy import create_engine, text

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            return
        # Read this platform's live-alert settings (new keys; Kick falls back to
        # the legacy shared stream_notification_* keys).
        cols = _alert_columns(platform)
        placeholders = ", ".join(f":k{i}" for i in range(len(cols)))
        params = {"guild_id": discord_server_id}
        params.update({f"k{i}": c for i, c in enumerate(cols)})
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT key, value FROM bot_settings "
                    f"WHERE discord_server_id = :guild_id AND key IN ({placeholders})"
                ),
                params,
            ).fetchall()
        settings = {key: value for key, value in rows}

        # Gate on this platform's OWN enabled flag.
        if alert_setting(settings, platform, "enabled") != "true":
            return
        notification_channel_id = alert_setting(settings, platform, "channel_id")
        if not notification_channel_id:
            logger.info(f"[StreamNotify] {platform} live alert has no channel — skipping")
            return

        stream_url = _stream_url(platform, streamer)
        message_content, footer_text = _build_live_message(settings, platform, streamer, title, category)

        bot_token = os.getenv("DISCORD_TOKEN")
        if not bot_token:
            return
        # Discord's message-create endpoint can be slow during their bad windows;
        # 10s was tight enough that the response read timed out *after* Discord had
        # already created the message, which made us skip the footer follow-up.
        # Use 30s and post the footer INDEPENDENTLY of the main POST's outcome —
        # the two messages are unrelated Discord objects (the footer doesn't need
        # the main message's id), so a slow/timed-out main read must not drop it.
        # (Matches the already-fixed oauth_server go-live path.)
        headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
        channel_url = f"https://discord.com/api/v10/channels/{notification_channel_id}/messages"
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession() as session:
            # Built inside the session so app:<name> emoji tokens can be resolved
            # against the bot's application emojis over the same connection.
            components = await build_alert_components(settings, platform, stream_url, session, bot_token)
            try:
                async with session.post(
                    channel_url,
                    headers=headers,
                    json={"content": message_content, "components": components},
                    timeout=timeout,
                ) as resp:
                    if resp.status in (200, 201):
                        logger.info(f"[StreamNotify] ✅ Notification sent to channel {notification_channel_id}")
                    else:
                        error_text = await resp.text()
                        logger.info(f"[StreamNotify] ⚠️ Failed to send notification: {resp.status} - {error_text[:200]}")
            except asyncio.TimeoutError:
                # Discord likely accepted the POST but the response read timed out —
                # proceed to the footer anyway so we don't drop it when the alert is
                # actually visible in the channel.
                logger.info("[StreamNotify] ⚠️ Timed out reading Discord response for alert (may have posted)")

            # Footer goes as a separate follow-up message: a classic content +
            # components message can't place text below the buttons, and the video
            # unfurl needs the link in the main content. Sent regardless of the
            # main POST's read outcome (see above).
            if footer_text:
                await asyncio.sleep(0.5)
                try:
                    async with session.post(
                        channel_url,
                        headers=headers,
                        json={"content": f"-# {footer_text}"},
                        timeout=timeout,
                    ) as footer_resp:
                        if footer_resp.status not in (200, 201):
                            footer_err = (await footer_resp.text())[:200]
                            logger.info(f"[StreamNotify] ⚠️ Failed to send footer: {footer_resp.status} - {footer_err}")
                except asyncio.TimeoutError:
                    logger.info("[StreamNotify] ⚠️ Timed out reading Discord response for footer (may have posted)")
    except Exception as e:
        logger.info(f"[StreamNotify] ⚠️ Failed to send Discord stream notification: {e}")


async def _control_clip_buffer(discord_server_id, streamer, is_live, platform):
    """Start/stop the rolling clip buffer on go-live/offline.

    The buffer now lives in this process, so this drives it directly instead of
    POSTing to the dashboard's clip API. That round-trip was both redundant and
    fragile: it depended on resolving a per-server dashboard URL and on the
    clip API key, and any hiccup in either silently meant no recording. Clips
    target Kick HLS only; Twitch is a no-op until clip support lands.
    """
    if platform != "kick":
        return
    try:
        import os

        from sqlalchemy import create_engine, text

        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            return
        engine = create_engine(db_url, pool_pre_ping=True)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT key, value FROM bot_settings
                    WHERE discord_server_id = :guild_id
                    AND key IN ('kick_channel', 'clips_auto_start_on_live')
                    """
                ),
                {"guild_id": discord_server_id},
            ).fetchall()
        settings = {key: value for key, value in rows}
        kick_channel = (settings.get("kick_channel") or "").strip()
        if not kick_channel:
            return

        auto_start_buffer = str(settings.get("clips_auto_start_on_live", "true")).lower() != "false"

        from utils.clip_service import get_clip_manager

        manager = get_clip_manager()

        if is_live:
            if not auto_start_buffer:
                logger.info(f"[StreamNotify] ⏸️ Auto-start disabled — skipping clip buffer for {streamer}")
                return
            existing = manager.get_buffer(kick_channel)
            if existing and existing.is_recording:
                logger.info(f"[StreamNotify] 🎬 Clip buffer already running for {streamer}")
                return
            buffer = manager.create_buffer(kick_channel, buffer_minutes=4)
            if await buffer.start():
                logger.info(f"[StreamNotify] ✅ Clip buffer started for {streamer}")
            else:
                logger.info(f"[StreamNotify] ⚠️ Clip buffer failed to start for {streamer}")
        else:
            buffer = manager.get_buffer(kick_channel)
            if buffer:
                await buffer.stop()
                manager.remove_buffer(kick_channel)
                logger.info(f"[StreamNotify] ✅ Clip buffer stopped for {streamer}")
    except Exception as e:
        logger.info(f"[StreamNotify] ⚠️ Failed to control clip buffer: {e}")
