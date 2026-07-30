"""Resolve the bot -> dashboard clip API key.

This is a single system secret the operator controls, NOT per-server data. It is
read from the ENVIRONMENT only.

The legacy `bot_settings.bot_api_key` fallback has been removed. That row was
writable through the dashboard's generic settings endpoints, so a delegated
dashboard user could rewrite the credential guarding the machine-to-machine clip
API — and it gave the secret a second home in a table the dashboard exposes as
ordinary configuration.

Set CLIPS_API_KEY on the bot service to the SAME value as the dashboard's
CLIPS_API_KEY; the /api/clips/* endpoints authenticate the X-API-Key header
against it.

Rotation: the dashboard also accepts CLIPS_API_KEY_PREVIOUS for inbound
verification during a rollout window. Outbound callers (here) always send the
current key only.
"""

import logging
import os

logger = logging.getLogger(__name__)


def get_clip_api_key(db_fallback: str = "") -> str:
    """Return the clip API key from the environment.

    `db_fallback` is accepted but IGNORED. The parameter is retained so existing
    call sites keep working without a coordinated cross-repo edit; passing a value
    logs a warning so the remaining callers are easy to find and clean up.
    """
    if db_fallback:
        logger.warning(
            "[Clip API] get_clip_api_key() was passed a database fallback; it is ignored. "
            "Set CLIPS_API_KEY in the environment instead."
        )
    key = os.getenv("CLIPS_API_KEY") or os.getenv("BOT_API_KEY") or ""
    if not key:
        logger.error("[Clip API] CLIPS_API_KEY is not set — clip API calls will fail to authenticate")
    return key
