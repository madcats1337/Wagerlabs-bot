import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from discord import app_commands
from discord.ext import commands

from features.discord_app_commands import register_wagerlabs_slash_commands, sync_global_slash_commands


def _make_bot():
    return commands.Bot(command_prefix="!", intents=discord.Intents.none())


def test_registers_global_chat_input_commands_once():
    bot = _make_bot()

    register_wagerlabs_slash_commands(bot, engine=None)
    register_wagerlabs_slash_commands(bot, engine=None)

    commands_by_name = {command.name: command for command in bot.tree.get_commands()}
    # /fair moved to the raffle cog as a hybrid command; this module now
    # registers only the application-only commands.
    assert set(commands_by_name) == {"wagerlabs"}
    assert isinstance(commands_by_name["wagerlabs"], app_commands.Command)


def test_wagerlabs_command_uses_safe_public_links():
    bot = _make_bot()
    register_wagerlabs_slash_commands(bot, engine=None)
    command = bot.tree.get_command("wagerlabs")
    response = SimpleNamespace(send_message=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=123,
        guild=SimpleNamespace(name="Test Server"),
        response=response,
    )

    asyncio.run(command.callback(interaction))

    response.send_message.assert_awaited_once()
    kwargs = response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Wagerlabs"
    assert [item.url for item in kwargs["view"].children] == [
        "https://wagerlabs.app/",
        "https://wagerlabs.app/provably-fair",
    ]


def test_syncs_once_and_sets_guard_only_after_success():
    synced_commands = [
        SimpleNamespace(name="wagerlabs"),
    ]
    bot = SimpleNamespace(tree=SimpleNamespace(sync=AsyncMock(return_value=synced_commands)))

    assert asyncio.run(sync_global_slash_commands(bot)) is True
    assert asyncio.run(sync_global_slash_commands(bot)) is True

    bot.tree.sync.assert_awaited_once()
    assert bot._wagerlabs_slash_commands_synced is True


def test_failed_sync_remains_retryable():
    bot = SimpleNamespace(tree=SimpleNamespace(sync=AsyncMock(side_effect=[RuntimeError("offline"), []])))

    assert asyncio.run(sync_global_slash_commands(bot)) is False
    assert asyncio.run(sync_global_slash_commands(bot)) is True

    assert bot.tree.sync.await_count == 2
