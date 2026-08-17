"""
Multi-Platform Wager Tracking Configuration
Supports Shuffle.com, Stake.com, Stake.us, and extensible for more platforms
"""

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class WagerPlatformConfig:
    """Configuration for a single wager tracking platform"""

    platform_name: str
    campaign_code: str
    affiliate_api_url: str
    tickets_per_1000_usd: int = 20
    enabled: bool = True

    def __post_init__(self):
        """Validate configuration"""
        if not self.platform_name:
            raise ValueError("platform_name cannot be empty")
        if not self.campaign_code:
            raise ValueError("campaign_code cannot be empty")
        if not self.affiliate_api_url:
            raise ValueError("affiliate_api_url cannot be empty")
        if self.tickets_per_1000_usd <= 0:
            raise ValueError("tickets_per_1000_usd must be positive")


class MultiPlatformWagerConfig:
    """Manages multiple wager tracking platform configurations"""

    def __init__(self):
        self.platforms: Dict[str, WagerPlatformConfig] = {}
        self._load_from_environment()

    def _load_from_environment(self):
        """
        Load platform configurations from environment variables

        Environment Variable Format:
        WAGER_PLATFORM_1_NAME=shuffle
        WAGER_PLATFORM_1_CODE=lele
        WAGER_PLATFORM_1_URL=https://affiliate.shuffle.com/stats/UUID
        WAGER_PLATFORM_1_TICKETS=20
        WAGER_PLATFORM_1_ENABLED=true

        WAGER_PLATFORM_2_NAME=stake
        WAGER_PLATFORM_2_CODE=trainwrecks
        WAGER_PLATFORM_2_URL=https://affiliate.stake.com/stats/UUID
        WAGER_PLATFORM_2_TICKETS=20
        WAGER_PLATFORM_2_ENABLED=true

        And so on...
        """
        # Check for WAGER_AFFILIATE_URL first (modern), then legacy SHUFFLE_AFFILIATE_URL
        wager_url = os.getenv("WAGER_AFFILIATE_URL") or os.getenv("SHUFFLE_AFFILIATE_URL")
        # No hardcoded default (was "lele" — one tenant's code).
        wager_code = os.getenv("WAGER_CAMPAIGN_CODE") or os.getenv("SHUFFLE_CAMPAIGN_CODE", "")
        wager_platform = os.getenv("WAGER_PLATFORM_NAME", "shuffle").lower()
        wager_tickets = int(os.getenv("WAGER_TICKETS_PER_1000_USD", "20"))

        if wager_url:
            logger.info(f"✅ Loading wager platform config: {wager_platform}")
            logger.info(f"📊 Campaign code: {wager_code}")

            try:
                self.platforms[wager_platform] = WagerPlatformConfig(
                    platform_name=wager_platform,
                    campaign_code=wager_code,
                    affiliate_api_url=wager_url,
                    tickets_per_1000_usd=wager_tickets,
                    enabled=True,
                )
            except Exception as e:
                logger.error(f"Failed to load wager platform config: {e}")

        # Load modern multi-platform configs
        platform_index = 1
        while True:
            prefix = f"WAGER_PLATFORM_{platform_index}_"

            name = os.getenv(f"{prefix}NAME")
            if not name:
                # No more platforms configured
                break

            code = os.getenv(f"{prefix}CODE")
            url = os.getenv(f"{prefix}URL")
            tickets = int(os.getenv(f"{prefix}TICKETS", "20"))
            enabled = os.getenv(f"{prefix}ENABLED", "true").lower() == "true"

            if not code or not url:
                logger.warning(f"⚠️ Platform {name} is missing CODE or URL, skipping")
                platform_index += 1
                continue

            try:
                normalized_name = name.lower().strip()
                self.platforms[normalized_name] = WagerPlatformConfig(
                    platform_name=normalized_name,
                    campaign_code=code,
                    affiliate_api_url=url,
                    tickets_per_1000_usd=tickets,
                    enabled=enabled,
                )
                logger.info(f"✅ Loaded wager platform: {normalized_name} (code: {code}, tickets: {tickets}/1k)")
            except Exception as e:
                logger.error(f"Failed to load platform {name}: {e}")

            platform_index += 1

        if not self.platforms:
            logger.warning("⚠️ No wager tracking platforms configured!")
            logger.info("💡 Set WAGER_PLATFORM_1_NAME, CODE, and URL to enable wager tracking")

    def get_platform(self, platform_name: str) -> Optional[WagerPlatformConfig]:
        """Get configuration for a specific platform"""
        return self.platforms.get(platform_name.lower())

    def get_enabled_platforms(self) -> List[WagerPlatformConfig]:
        """Get list of all enabled platforms"""
        return [p for p in self.platforms.values() if p.enabled]

    def get_all_platforms(self) -> List[WagerPlatformConfig]:
        """Get list of all configured platforms"""
        return list(self.platforms.values())

    def is_platform_enabled(self, platform_name: str) -> bool:
        """Check if a platform is enabled"""
        platform = self.get_platform(platform_name)
        return platform is not None and platform.enabled

    def get_platform_count(self) -> int:
        """Get total number of configured platforms"""
        return len(self.platforms)

    def get_enabled_count(self) -> int:
        """Get number of enabled platforms"""
        return len(self.get_enabled_platforms())

    def add_platform(self, config: WagerPlatformConfig):
        """Add a new platform configuration (runtime only, not persisted)"""
        self.platforms[config.platform_name.lower()] = config
        logger.info(f"Added platform: {config.platform_name}")

    def disable_platform(self, platform_name: str) -> bool:
        """Disable a platform"""
        platform = self.get_platform(platform_name)
        if platform:
            platform.enabled = False
            logger.info(f"Disabled platform: {platform_name}")
            return True
        return False

    def enable_platform(self, platform_name: str) -> bool:
        """Enable a platform"""
        platform = self.get_platform(platform_name)
        if platform:
            platform.enabled = True
            logger.info(f"Enabled platform: {platform_name}")
            return True
        return False


# Global instance
_wager_config = None


def get_wager_config() -> MultiPlatformWagerConfig:
    """Get the global wager configuration instance"""
    global _wager_config
    if _wager_config is None:
        _wager_config = MultiPlatformWagerConfig()
    return _wager_config


# For backwards compatibility with existing code
def get_shuffle_config() -> Optional[WagerPlatformConfig]:
    """Get Shuffle platform config (backwards compatibility)"""
    config = get_wager_config()
    return config.get_platform("shuffle")
