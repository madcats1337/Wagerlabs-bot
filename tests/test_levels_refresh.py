"""Tests for the event-driven panel refresh (features.levels.panel).

The panels used to repaint on a ten-minute tick; they now repaint when a
level-up or rank-up happens. The thing worth pinning down is the DEBOUNCE: a
busy server produces bursts of level-ups, and one panel edit per award would
hit Discord's per-channel edit rate limit and show a board flickering through a
dozen near-identical states.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from features.levels import panel as PANEL


class FakePanel:
    """Counts repaints so a test can assert how many edits a burst produced."""

    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1


class FakeBot:
    def __init__(self, guild_id):
        self.levels_panels = {guild_id: FakePanel()}
        self.levels_competition_panels = {guild_id: FakePanel()}


@pytest.fixture(autouse=True)
def _clear_pending():
    """The pending-task map is module-level; don't leak between tests."""
    PANEL._pending_refreshes.clear()
    yield
    for task in list(PANEL._pending_refreshes.values()):
        task.cancel()
    PANEL._pending_refreshes.clear()


def test_a_burst_of_requests_collapses_into_one_refresh():
    """20 level-ups in the same instant must cost ONE edit, not 20."""

    async def _test():
        bot = FakeBot(1)

        for _ in range(20):
            PANEL.request_panel_refresh(bot, 1, delay=0.05)

        assert len(PANEL._pending_refreshes) == 1, "burst should share a single pending task"
        await asyncio.sleep(0.2)

        assert bot.levels_panels[1].refreshes == 1
        assert bot.levels_competition_panels[1].refreshes == 1

    asyncio.run(_test())


def test_a_later_request_refreshes_again():
    """Debounce must not permanently swallow subsequent activity."""

    async def _test():
        bot = FakeBot(1)

        PANEL.request_panel_refresh(bot, 1, delay=0.05)
        await asyncio.sleep(0.2)
        assert bot.levels_panels[1].refreshes == 1

        PANEL.request_panel_refresh(bot, 1, delay=0.05)
        await asyncio.sleep(0.2)
        assert bot.levels_panels[1].refreshes == 2

    asyncio.run(_test())


def test_guilds_debounce_independently():
    """One busy server must not starve another's refresh."""

    async def _test():
        bot = FakeBot(1)
        bot.levels_panels[2] = FakePanel()
        bot.levels_competition_panels[2] = FakePanel()

        PANEL.request_panel_refresh(bot, 1, delay=0.05)
        PANEL.request_panel_refresh(bot, 2, delay=0.05)
        assert len(PANEL._pending_refreshes) == 2

        await asyncio.sleep(0.2)
        assert bot.levels_panels[1].refreshes == 1
        assert bot.levels_panels[2].refreshes == 1

    asyncio.run(_test())


def test_the_pending_entry_is_released_after_running():
    """A leaked entry would block every future refresh for that guild."""

    async def _test():
        bot = FakeBot(1)
        PANEL.request_panel_refresh(bot, 1, delay=0.05)
        await asyncio.sleep(0.2)
        assert 1 not in PANEL._pending_refreshes

    asyncio.run(_test())


def test_a_failing_panel_does_not_block_later_refreshes():
    """One bad guild must not wedge its own future repaints."""

    async def _test():

        class Boom(FakePanel):
            async def refresh(self):
                self.refreshes += 1
                raise RuntimeError("channel gone")

        bot = FakeBot(1)
        bot.levels_panels[1] = Boom()

        PANEL.request_panel_refresh(bot, 1, delay=0.05)
        await asyncio.sleep(0.2)
        assert bot.levels_panels[1].refreshes == 1
        assert 1 not in PANEL._pending_refreshes, "a raised refresh must still release the slot"

        PANEL.request_panel_refresh(bot, 1, delay=0.05)
        await asyncio.sleep(0.2)
        assert bot.levels_panels[1].refreshes == 2

    asyncio.run(_test())


def test_refresh_skips_a_guild_with_no_panel():
    """Most guilds have never placed a panel; that is not an error."""

    async def _test():
        bot = FakeBot(1)
        await PANEL.refresh_guild_panels(bot, 999)  # must not raise

    asyncio.run(_test())


def test_a_bad_guild_id_is_ignored_rather_than_raising():
    """award_xp must never fail because a repaint could not be scheduled."""
    bot = FakeBot(1)
    PANEL.request_panel_refresh(bot, None)
    PANEL.request_panel_refresh(bot, "not-an-id")
    assert not PANEL._pending_refreshes


def test_request_outside_an_event_loop_is_survivable():
    """A sync context (CLI/test) has no loop; the safety-net catches up."""
    bot = FakeBot(1)
    PANEL.request_panel_refresh(bot, 1)  # no running loop — must not raise
    assert not PANEL._pending_refreshes


# ── Components V2 -> embed migration ────────────────────────────────────────


def _panel_over(message):
    """A LevelsPanel wired to `message`, with the DB and render stubbed out."""
    panel = PANEL.LevelsPanel.__new__(PANEL.LevelsPanel)
    panel.bot = MagicMock()
    panel.engine = MagicMock()
    panel.guild_id = 1
    panel.panel_type = PANEL.PANEL_TYPE
    panel.panel_channel_id = 10
    panel.panel_message_id = 20
    panel._enabled = lambda: True
    panel._render = AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    panel.create_panel = AsyncMock(return_value=True)

    channel = MagicMock()
    channel.fetch_message = AsyncMock(return_value=message)
    panel.bot.get_channel.return_value = channel
    return panel, channel


def _message(components_v2):
    message = MagicMock()
    message.flags.components_v2 = components_v2
    message.edit = AsyncMock()
    message.delete = AsyncMock()
    return message


def test_a_components_v2_panel_is_reposted_not_edited():
    """The IS_COMPONENTS_V2 flag is set at creation and cannot be cleared.

    Editing an embed onto such a message silently does nothing, so a panel
    posted by the older V2 layout would render the old design forever and every
    fix would look like it had no effect. This shipped exactly that way.
    """

    async def _test():
        message = _message(components_v2=True)
        panel, _ = _panel_over(message)

        await panel.refresh()

        panel.create_panel.assert_awaited_once()
        message.delete.assert_awaited_once()
        message.edit.assert_not_awaited()

    asyncio.run(_test())


def test_an_embed_panel_is_edited_in_place():
    """The normal path: no repost, so the message keeps its place in channel."""

    async def _test():
        message = _message(components_v2=False)
        panel, _ = _panel_over(message)

        await panel.refresh()

        message.edit.assert_awaited_once()
        panel.create_panel.assert_not_awaited()
        message.delete.assert_not_awaited()

    asyncio.run(_test())


def test_the_stale_panel_survives_a_failed_repost():
    """Post before delete: a failure must leave the old board, not nothing."""

    async def _test():
        message = _message(components_v2=True)
        panel, _ = _panel_over(message)
        panel.create_panel = AsyncMock(return_value=False)

        await panel.refresh()

        message.delete.assert_not_awaited()

    asyncio.run(_test())
