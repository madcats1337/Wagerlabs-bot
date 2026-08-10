from .giveaway_manager import GiveawayManager, setup_giveaway_managers
from .giveaway_panel import GiveawayPanelView, post_panel, refresh_panel

__all__ = [
    "GiveawayManager",
    "setup_giveaway_managers",
    "GiveawayPanelView",
    "post_panel",
    "refresh_panel",
    "register_giveaway_commands",
]


def register_giveaway_commands(bot, engine):
    """Lazy re-export: giveaway_commands imports discord.app_commands, so keep
    it out of this package's import-time graph (bot.py imports this package)."""
    from .giveaway_commands import register_giveaway_commands as _register

    return _register(bot, engine)
