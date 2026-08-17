"""
Raffle System Configuration
All configurable parameters for the raffle system
"""

import os

# Ticket conversion rates
WATCHTIME_TICKETS_PER_HOUR = 10  # 1 hour of watchtime = 10 tickets
GIFTED_SUB_TICKETS = 15  # 1 gifted sub = 15 tickets

# Wager tracking configuration (environment variable based)
# Each streamer sets their own affiliate URL and campaign code
# Primary: WAGER_AFFILIATE_URL, Fallback: SHUFFLE_AFFILIATE_URL (legacy)
WAGER_PLATFORM_NAME = os.getenv("WAGER_PLATFORM_NAME", "shuffle").lower()
WAGER_AFFILIATE_URL = os.getenv("WAGER_AFFILIATE_URL") or os.getenv("SHUFFLE_AFFILIATE_URL", "")
# No hardcoded default — an unset code must read as "unset", not as one
# specific tenant's Shuffle code (this used to default to "lele").
WAGER_CAMPAIGN_CODE = os.getenv("WAGER_CAMPAIGN_CODE") or os.getenv("SHUFFLE_CAMPAIGN_CODE", "")
WAGER_TICKETS_PER_1000_USD = int(os.getenv("WAGER_TICKETS_PER_1000_USD", "20"))

# Backwards compatibility aliases
SHUFFLE_AFFILIATE_URL = WAGER_AFFILIATE_URL
SHUFFLE_CAMPAIGN_CODE = WAGER_CAMPAIGN_CODE
SHUFFLE_TICKETS_PER_1000_USD = WAGER_TICKETS_PER_1000_USD

# Update intervals (in seconds)
WATCHTIME_CONVERSION_INTERVAL = 3600  # 1 hour
WAGER_CHECK_INTERVAL = int(os.getenv("WAGER_CHECK_INTERVAL", "900"))  # 15 minutes default
SHUFFLE_CHECK_INTERVAL = WAGER_CHECK_INTERVAL  # Backwards compatibility

# Monthly reset configuration
RESET_DAY_OF_MONTH = 1  # 1st day of the month
RESET_HOUR_UTC = 0  # 00:00 UTC

# Raffle settings
MINIMUM_TICKETS_TO_ENTER = 1
AUTO_DRAW_ON_RESET = False  # Manual draw by admin (set True for automatic)

# Notifications
TICKET_NOTIFICATION_THRESHOLD = 20  # Only notify for 20+ tickets earned at once
ENABLE_TICKET_NOTIFICATIONS = True

# Security & Anti-cheat
LINK_SHUFFLE_COOLDOWN = 86400  # 24 hours between Shuffle link attempts
MAX_SHUFFLE_LINKS_PER_USER = 1  # One Shuffle account per Discord user
REQUIRE_SHUFFLE_VERIFICATION = True  # Require admin verification for Shuffle links

# Leaderboard settings
DEFAULT_LEADERBOARD_SIZE = 10
MAX_LEADERBOARD_SIZE = 25
AUTO_LEADERBOARD_UPDATE_INTERVAL = 300  # 5 minutes in seconds (was 3600 = 1 hour)
