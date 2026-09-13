"""Unit tests for Discord Bot Profile Synchronization (features.discord_profile.profile_sync)."""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

# Ensure Kick-dicord-bot is in sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord
import pytest

from features.discord_profile.profile_sync import (
    apply_guild_bot_profile,
    apply_guild_bot_profile_safe,
    fetch_or_read_data_uri,
)


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    guild = MagicMock()
    guild.id = 123456789
    guild.name = "Test Guild"
    bot.get_guild.return_value = guild
    bot.http = MagicMock()
    bot.http.request = AsyncMock(return_value={"nick": "TestNick"})
    return bot


def test_apply_guild_bot_profile_payload(mock_bot):
    """Test that custom nickname, bio, avatar, and banner are formatted into the Discord API payload."""
    settings = {
        "discord_bot_nickname": "Custom Wagerlabs",
        "discord_bot_bio": "Custom Bot Bio Description",
        "discord_bot_avatar": "data:image/png;base64,testavatar123",
        "discord_bot_banner": "data:image/png;base64,testbanner456",
    }

    success, err = asyncio.run(apply_guild_bot_profile(mock_bot, 123456789, settings=settings))
    assert success is True
    assert err is None

    mock_bot.http.request.assert_awaited_once()
    call_args = mock_bot.http.request.call_args
    route = call_args[0][0]
    kwargs = call_args[1]

    assert route.method == "PATCH"
    assert "123456789" in route.url
    payload = kwargs["json"]
    assert payload["nick"] == "Custom Wagerlabs"
    assert payload["bio"] == "Custom Bot Bio Description"
    assert payload["avatar"] == "data:image/png;base64,testavatar123"
    assert payload["banner"] == "data:image/png;base64,testbanner456"


def test_apply_guild_bot_profile_reset_to_default(mock_bot):
    """Test that empty string values are sent as None/null to reset them to default."""
    settings = {
        "discord_bot_nickname": "",
        "discord_bot_bio": "",
        "discord_bot_avatar": "",
        "discord_bot_banner": "",
    }

    success, err = asyncio.run(apply_guild_bot_profile(mock_bot, 123456789, settings=settings))
    assert success is True
    assert err is None

    mock_bot.http.request.assert_awaited_once()
    payload = mock_bot.http.request.call_args[1]["json"]
    assert payload["nick"] is None
    assert payload["bio"] is None
    assert payload["avatar"] is None
    assert payload["banner"] is None


def test_apply_guild_bot_profile_missing_permissions(mock_bot):
    """Test that 403 Forbidden with code 50013 gives a descriptive error message about Change Nickname."""
    mock_response = MagicMock()
    mock_response.status = 403
    mock_response.reason = "Forbidden"

    forbidden_exc = discord.Forbidden(mock_response, "Missing Permissions")
    forbidden_exc.code = 50013
    mock_bot.http.request.side_effect = forbidden_exc

    settings = {"discord_bot_nickname": "New Name"}
    success, err = asyncio.run(apply_guild_bot_profile(mock_bot, 123456789, settings=settings))

    assert success is False
    assert "Change Nickname" in err


def test_apply_guild_bot_profile_safe_never_raises(mock_bot):
    """Test that the safe wrapper catches all exceptions and returns False."""
    mock_bot.http.request.side_effect = RuntimeError("Fatal crash")
    settings = {"discord_bot_nickname": "Boom"}

    success = asyncio.run(apply_guild_bot_profile_safe(mock_bot, 123456789, settings=settings))
    assert success is False


def test_fetch_or_read_data_uri():
    """Test data URI pass-through and null handling."""
    data_uri = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA"
    result = asyncio.run(fetch_or_read_data_uri(data_uri))
    assert result == data_uri

    assert asyncio.run(fetch_or_read_data_uri(None)) is None
    assert asyncio.run(fetch_or_read_data_uri("")) is None
    assert asyncio.run(fetch_or_read_data_uri("   ")) is None
