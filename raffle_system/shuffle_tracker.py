"""
Multi-Platform Wager Tracker
Polls gambling affiliate APIs (Shuffle, Stake, etc.) and awards raffle tickets based on wager amounts

Settings loaded from database (bot_settings table) with env var fallbacks:
- wager_affiliate_url / WAGER_AFFILIATE_URL: Your affiliate stats API URL
- wager_campaign_code / WAGER_CAMPAIGN_CODE: Your affiliate/referral code to track
- wager_tickets_per_1000 / WAGER_TICKETS_PER_1000_USD: Tickets to award per $1000 wagered
"""

import asyncio
import logging
import os
from datetime import datetime

import aiohttp
from sqlalchemy import text

from utils.wager_leaderboard import is_http_url, resolve_shuffle_stats_url

from .tickets import TicketManager

logger = logging.getLogger(__name__)


class ShuffleWagerTracker:
    """
    Tracks gambling platform wagers and awards raffle tickets

    Despite the class name, this now supports multiple platforms (Shuffle, Stake, Stake.us, etc.)
    Settings are loaded from database (bot_settings) with env var fallbacks.
    """

    def __init__(self, engine, bot_settings=None, server_id=None):
        self.engine = engine
        self.server_id = server_id
        self.ticket_manager = TicketManager(engine, server_id=server_id)
        self.bot_settings = bot_settings

        # True after warning about a Cloudflare challenge on the affiliate API,
        # so a multi-hour challenge spell logs ONE warning (then debug) instead
        # of one per 2-minute poll. Reset on the next successful fetch.
        self._cf_challenge_warned = False

        # Load settings from bot_settings (database) or fall back to env vars
        self._load_settings()

        # Per-guild init detail — DEBUG (startup is summarized elsewhere).
        logger.debug(f"[Shuffle Tracker] 🎰 Initialized for platform: {self.platform_name}")
        codes = [c.strip() for c in self.campaign_code.split(",")]
        if len(codes) > 1:
            logger.debug(f"[Shuffle Tracker] 📊 Campaign codes ({len(codes)}): {', '.join(codes)}")
        else:
            logger.debug(f"[Shuffle Tracker] 📊 Campaign code: {self.campaign_code}")
        logger.debug(f"[Shuffle Tracker] 🎫 Rate: {self.tickets_per_1000} tickets per $1000 wagered")
        if self.affiliate_url:
            logger.debug(f"[Shuffle Tracker] 🔗 Affiliate URL configured: {self.affiliate_url[:50]}...")
        else:
            logger.debug(f"[Shuffle Tracker] No affiliate URL configured")

    # Default howl affiliate leaderboard endpoint (overridable per-server).
    HOWL_DEFAULT_LB_URL = "https://howl.gg/api/user/affiliate/lb"

    @staticmethod
    def _shuffle_wager_url(affiliate_url):
        """Derive Shuffle's `/wager/<id>` endpoint from a configured `/stats/<id>` URL.

        Servers store the affiliate URL as the classic public stats endpoint
        (https://affiliate.shuffle.com/stats/<id>). Shuffle added a newer public
        endpoint at the same host — /wager/<id> — that returns, per referee,
        BOTH the raw `wagerAmount` and the RTP-weighted `weightedWagerAmount`
        (the 100/50/10% tiers already applied server-side). Same public id, no
        auth. We derive /wager/ from the stored /stats/ URL so existing configs
        keep working without anyone re-entering anything. If the URL doesn't look
        like the stats endpoint, it's returned unchanged (defensive; the caller
        falls back to the stored URL's own shape).
        """
        if not affiliate_url:
            return affiliate_url
        return affiliate_url.replace("/stats/", "/wager/", 1)

    def _leaderboard_stats_url(self):
        """The active leaderboard period's "Stats URL" for this server, or "".

        Read fresh on every poll (this runs from refresh_settings) so editing the
        period's Stats URL takes effect without a bot restart, exactly like the
        other wager settings. A DB failure returns "" so the caller falls back to
        the Profile Settings affiliate URL rather than losing wager tracking.
        """
        if not self.server_id or self.engine is None:
            return ""
        try:
            with self.engine.begin() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT stats_url FROM wager_leaderboard_periods
                        WHERE discord_server_id = :sid
                          AND status = 'active'
                          AND site = 'shuffle'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ),
                    {"sid": self.server_id},
                ).fetchone()
        except Exception as e:
            logger.info(f"[Shuffle Tracker] leaderboard stats_url lookup failed: {type(e).__name__}: {e}")
            return ""
        return (row[0] or "").strip() if row else ""

    def _resolve_shuffle_url(self, affiliate_url):
        """Stats URL (active leaderboard period) with the Affiliate URL as fallback.

        The leaderboard's own "Stats URL" field is what the streamer wants tracked
        for that board; blank means "use the Affiliate URL from Profile Settings".
        A value that isn't an http(s) URL is ignored (with a warning) so a typo
        degrades to the server's affiliate URL instead of taking wager tracking
        offline.
        """
        stats_url = self._leaderboard_stats_url()
        if stats_url and not is_http_url(stats_url):
            logger.warning(
                f"[Shuffle Tracker] Ignoring leaderboard Stats URL {stats_url[:60]!r} "
                f"— not an http(s) URL; using the Profile Settings affiliate URL"
            )
        return resolve_shuffle_stats_url(stats_url, affiliate_url)

    def _load_settings(self):
        """Load wager settings from bot_settings (database) or environment variables.

        The active affiliate is the server's `wager_platform_name` (single-select
        in Profile Settings). Because this runs before EVERY poll (refresh_settings)
        and on every dashboard settings save, switching the platform hot-swaps the
        SAME tracker object to the other API — no restart, no second tracker.

        For shuffle the URL is resolved per-poll as the active leaderboard period's
        "Stats URL", falling back to the Profile Settings "Affiliate URL".
        """
        if self.bot_settings:
            # Refresh to get latest values
            self.bot_settings.refresh()

            platform = (self.bot_settings.get("wager_platform_name") or "shuffle").strip().lower()
            self.platform_name = platform

            if platform == "howl":
                # Read fresh + decrypted (never from the settings cache), so a key
                # rotated on the dashboard applies without a bot restart.
                self.howl_api_key = (self.bot_settings.get_secret("howl_api_key") or "").strip()
                # Strip first so a blank-but-not-empty stored value falls back to
                # the default endpoint instead of fetching a whitespace URL.
                self.affiliate_url = (
                    self.bot_settings.get("howl_affiliate_url") or ""
                ).strip() or self.HOWL_DEFAULT_LB_URL
                self.campaign_code = self.bot_settings.get("howl_campaign_code") or ""
                # Reuse the shared ticket rate (no separate howl rate setting yet).
                self.tickets_per_1000 = self.bot_settings.shuffle_tickets_per_1000 or 20
            else:
                self.howl_api_key = ""
                # Leaderboard "Stats URL" first, else Profile Settings' "Affiliate
                # URL". Blank/whitespace-only values (a cleared dashboard field)
                # resolve to "" — the task's empty-URL guard then skips this server
                # instead of fetching "   ".
                self.affiliate_url = self._resolve_shuffle_url(self.bot_settings.shuffle_affiliate_url)
                # No "lele" fallback: that is one tenant's code and silently
                # tracked THEIR referrals on any server that hadn't set one.
                # "" means "unset" and is handled by the campaign-filter guard
                # in _fetch_and_process (it skips rather than matching ['']).
                self.campaign_code = self.bot_settings.shuffle_campaign_code or ""
                self.tickets_per_1000 = self.bot_settings.shuffle_tickets_per_1000 or 20
        else:
            # Fallback to environment variables
            self.platform_name = os.getenv("WAGER_PLATFORM_NAME", "shuffle").lower()
            if self.platform_name == "howl":
                self.howl_api_key = os.getenv("HOWL_API_KEY", "").strip()
                self.affiliate_url = (os.getenv("HOWL_AFFILIATE_URL") or "").strip() or self.HOWL_DEFAULT_LB_URL
                self.campaign_code = os.getenv("HOWL_CAMPAIGN_CODE", "")
                self.tickets_per_1000 = int(os.getenv("WAGER_TICKETS_PER_1000_USD", "20"))
            else:
                self.howl_api_key = ""
                # Same precedence as the DB path: the leaderboard's Stats URL wins.
                self.affiliate_url = self._resolve_shuffle_url(
                    os.getenv("WAGER_AFFILIATE_URL") or os.getenv("SHUFFLE_AFFILIATE_URL", "")
                )
                # No hardcoded default (was "lele" — one tenant's code).
                self.campaign_code = os.getenv("WAGER_CAMPAIGN_CODE") or os.getenv("SHUFFLE_CAMPAIGN_CODE", "")
                self.tickets_per_1000 = int(os.getenv("WAGER_TICKETS_PER_1000_USD", "20"))

    def refresh_settings(self):
        """Reload settings from database"""
        self._load_settings()
        codes = [c.strip() for c in self.campaign_code.split(",")]
        codes_str = f"{len(codes)} codes" if len(codes) > 1 else self.campaign_code
        logger.debug(f"[Shuffle Tracker] 🔄 Settings refreshed - URL: {bool(self.affiliate_url)}, Codes: {codes_str}")

    def _record_wager_history(self, conn, shuffle_username, total_wager_usd, wager_delta):
        """Append a row to shuffle_wager_history for the dashboard leaderboard.

        Only called when the tracker has detected a positive wager increase
        for a *known* user. The new-user onboarding path does NOT write
        history (those users haven't actually wagered anything on our watch
        yet — they came in already at some lifetime total).

        Each row stores:
          - total_wager_usd : the user's running cumulative total after this
                              update (useful for debugging / future audits)
          - wager_delta     : how much they wagered since the previous
                              observation. The leaderboard sums this column
                              across rows in [start, end) for each user.

        Wrapped in a nested transaction (SAVEPOINT) so an error here doesn't
        poison the surrounding wager-update transaction.
        """
        # Guard on the cent-rounded delta: a sub-cent residue (from comparing a
        # full-precision API value against the DECIMAL(15,2) stored wager) is > 0
        # but rounds to 0.00 in the wager_delta column, which would write a
        # meaningless $0.00 history row. Only record a genuine cent-or-more change.
        if not self.server_id or round(wager_delta, 2) <= 0:
            return

        try:
            with conn.begin_nested():  # per-user SAVEPOINT
                conn.execute(
                    text(
                        """
                        INSERT INTO shuffle_wager_history
                            (discord_server_id, shuffle_username, total_wager_usd, wager_delta)
                        VALUES
                            (:server_id, :username, :total, :delta)
                        """
                    ),
                    {
                        "server_id": self.server_id,
                        "username": shuffle_username,
                        "total": total_wager_usd,
                        "delta": wager_delta,
                    },
                )
        except Exception as e:
            logger.info(
                f"[Shuffle Tracker] ❌ shuffle_wager_history append failed "
                f"for server={self.server_id}, user={shuffle_username}: "
                f"{type(e).__name__}: {e}"
            )
            return

        # Publish to the dashboard's live "recent wagers" feed. This is the single
        # chokepoint for a real (cent-or-more) committed wager delta, so it covers
        # every update branch. Best-effort: a publish failure never affects ingestion.
        try:
            from utils.redis_publisher import bot_redis_publisher

            bot_redis_publisher.publish_wager(
                discord_server_id=str(self.server_id),
                shuffle_username=shuffle_username,
                kick_name=None,
                platform=self.platform_name,
                wager_delta=round(wager_delta, 2),
                total_wager_usd=round(total_wager_usd, 2) if total_wager_usd is not None else None,
            )
        except Exception as pub_err:
            logger.info(f"[Shuffle Tracker] wager publish failed: {pub_err}")

    def _store_lifetime_totals(self, filtered_data):
        """Upsert current lifetime wager totals into shuffle_wager_totals.

        Period-INDEPENDENT: called on every poll (even with no active raffle
        period) so the dashboard's Tier-4 wager leaderboard always has fresh
        totals to read without hitting shuffle's rate-limited API itself. One row
        per (server, platform, username); we also carry the verified kick/discord
        link (if any) for display badges. Best-effort: a failure here never blocks
        the ticket-awarding path that follows.
        """
        if not self.server_id or not filtered_data:
            return

        try:
            with self.engine.begin() as conn:
                # Verified links for this platform → identity badges on the board.
                link_rows = conn.execute(
                    text(
                        """
                        SELECT shuffle_username, kick_name, discord_id
                        FROM raffle_shuffle_links
                        WHERE verified = TRUE AND platform = :platform
                        """
                    ),
                    {"platform": self.platform_name},
                ).fetchall()
                links = {str(r[0]).lower(): (r[1], r[2]) for r in link_rows}

                for user_data in filtered_data:
                    username = user_data.get("username")
                    if not username:
                        continue
                    # Leaderboard totals use the RTP-WEIGHTED wager. Shuffle's
                    # /wager/ endpoint supplies weightedWagerAmount; Howl (and any
                    # legacy /stats/ response) fall back to the raw amount via
                    # _normalize_rows, so this reads correctly for every platform.
                    try:
                        total = round(float(user_data.get("weightedWagerAmount", 0) or 0), 2)
                    except (TypeError, ValueError):
                        total = 0.0
                    kick_name, discord_id = links.get(str(username).lower(), (None, None))

                    # Upsert keyed on (server, platform, username). COALESCE keeps a
                    # previously-stored link if this poll has no fresh one, so a
                    # later unlink doesn't silently wipe the badge mid-period.
                    conn.execute(
                        text(
                            """
                            INSERT INTO shuffle_wager_totals
                                (discord_server_id, platform, shuffle_username,
                                 kick_name, discord_id, total_wager_usd, last_updated)
                            VALUES
                                (:server_id, :platform, :username,
                                 :kick_name, :discord_id, :total, CURRENT_TIMESTAMP)
                            ON CONFLICT (discord_server_id, platform, shuffle_username)
                            DO UPDATE SET
                                total_wager_usd = EXCLUDED.total_wager_usd,
                                kick_name = COALESCE(EXCLUDED.kick_name, shuffle_wager_totals.kick_name),
                                discord_id = COALESCE(EXCLUDED.discord_id, shuffle_wager_totals.discord_id),
                                last_updated = CURRENT_TIMESTAMP
                            """
                        ),
                        {
                            "server_id": self.server_id,
                            "platform": self.platform_name,
                            "username": username,
                            "kick_name": kick_name,
                            "discord_id": discord_id,
                            "total": total,
                        },
                    )
        except Exception as e:
            logger.info(
                f"[Shuffle Tracker] shuffle_wager_totals upsert failed for "
                f"server={self.server_id}: {type(e).__name__}: {e}"
            )
            return

        # One-time cutover, in its OWN transaction so a failure here never rolls
        # back the weighted totals written above. The totals just stored are now
        # RTP-WEIGHTED (previously raw); any leaderboard period active across this
        # switch has baselines snapshotted from the OLD raw totals, so
        # `weighted_now - raw_baseline` would be wrong. Re-anchor those baselines
        # to the fresh weighted totals, exactly once per server. Shuffle only
        # (Howl weighted == raw, so there's nothing to re-anchor).
        if self.platform_name == "shuffle":
            self._rebaseline_active_periods_for_weighted_cutover()

    def _rebaseline_active_periods_for_weighted_cutover(self):
        """Re-anchor active leaderboard baselines to weighted totals, once.

        Guarded by a marker row in `shuffle_weighted_cutover` so it fires exactly
        once per server, the first time weighted totals are stored. For every
        ACTIVE leaderboard period on this server it overwrites each existing
        baseline with the user's current (now weighted) total from
        shuffle_wager_totals, so the period's displayed wager continues from ~0 at
        the cutover instant rather than showing a broken weighted-minus-raw value.

        Runs in its OWN transaction (the marker claim + the UPDATE commit
        together) so it's atomic and safe to retry: if anything fails, the
        transaction rolls back the marker too and the next poll tries again. Only
        baselines that already exist are re-anchored; a period's own new-user
        snapshotting keeps handling users first seen after the cutover.
        """
        try:
            with self.engine.begin() as conn:
                # Marker table: presence of a row means the cutover already ran.
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS shuffle_weighted_cutover (
                            discord_server_id BIGINT PRIMARY KEY,
                            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                )
                claimed = conn.execute(
                    text(
                        """
                        INSERT INTO shuffle_weighted_cutover (discord_server_id)
                        VALUES (:sid)
                        ON CONFLICT (discord_server_id) DO NOTHING
                        RETURNING discord_server_id
                        """
                    ),
                    {"sid": self.server_id},
                ).fetchone()

                # Row already existed → cutover done before; nothing to do.
                if not claimed:
                    return

                # Re-baseline every active period's EXISTING baselines to the
                # current weighted totals. Join baselines → totals by username.
                result = conn.execute(
                    text(
                        """
                        UPDATE wager_leaderboard_baselines b
                        SET baseline_amount = t.total_wager_usd
                        FROM wager_leaderboard_periods p, shuffle_wager_totals t
                        WHERE b.period_id = p.id
                          AND p.discord_server_id = :sid
                          AND p.status = 'active'
                          AND t.discord_server_id = :sid
                          AND t.platform = 'shuffle'
                          AND LOWER(t.shuffle_username) = LOWER(b.shuffle_username)
                        """
                    ),
                    {"sid": self.server_id},
                )
                logger.info(
                    f"[Shuffle Tracker] weighted-wager cutover: re-baselined "
                    f"{result.rowcount} active-period baseline(s) for server={self.server_id}"
                )
        except Exception as e:
            # Non-fatal and self-retrying: the marker and UPDATE share one
            # transaction, so a failure rolls back both and the next poll retries.
            logger.info(
                f"[Shuffle Tracker] weighted-wager cutover re-baseline failed for "
                f"server={self.server_id}: {type(e).__name__}: {e}"
            )

    async def update_shuffle_wagers(self):
        """
        Poll gambling platform affiliate API and update wager tracking

        Fetches the latest wager data from the configured affiliate page,
        compares with previous values, and awards tickets for new wagers.

        Returns:
            dict: Summary of updates performed
        """
        try:
            # Fetch current wager data from the platform FIRST. The lifetime
            # totals feed the dashboard's Tier-4 wager leaderboard, which is
            # independent of raffles — so we store totals on every poll even when
            # no raffle period is active. (The period gate below only governs
            # raffle TICKET awarding, which genuinely needs a period.)
            wager_data = await self._fetch_shuffle_data()

            if not wager_data:
                # _fetch_shuffle_data already logged the specific reason (rate
                # limit, timeout, HTTP status, bad body) at the appropriate level,
                # so avoid a duplicate generic line here. The caller decides how to
                # surface `fetch_failed`.
                logger.debug(f"No {self.platform_name} affiliate data this poll (see fetch log above)")
                return {"status": "fetch_failed", "updates": 0}

            # Filter for campaign code(s) - supports multiple codes separated by comma.
            # Howl's affiliate API already returns ONLY your affiliates and has no
            # per-row campaign code to match on, so the campaign filter is both
            # meaningless and (with an empty howl_campaign_code) would drop every
            # row. Bypass it for howl; shuffle keeps the existing filter.
            if self.platform_name == "howl":
                filtered_data = wager_data
            else:
                # An UNSET shuffle code must not silently match nothing: "".split(",")
                # is [''], which no row's campaignCode equals, so every wager would be
                # dropped with only a DEBUG line. Skip the poll loudly instead - the
                # server needs a code configured before Shuffle wagers can be tracked.
                campaign_codes = [c.strip().lower() for c in self.campaign_code.split(",") if c.strip()]
                if not campaign_codes:
                    logger.warning(
                        f"[Shuffle Tracker] No campaign code configured for server "
                        f"{self.server_id} - skipping wager poll (set one in "
                        f"Profile Settings -> Affiliate)"
                    )
                    return {"status": "no_campaign_code", "updates": 0}
                filtered_data = [user for user in wager_data if user.get("campaignCode", "").lower() in campaign_codes]

            if not filtered_data:
                # A valid, benign state (an affiliate with no qualifying referees
                # yet), so DEBUG — not a warning. With the /wager/ endpoint the
                # campaign code is stamped, so this means genuinely-empty data.
                logger.debug(f"No qualifying users in {self.platform_name} affiliate data this poll")
                return {"status": "no_users", "updates": 0}

            logger.debug(
                f"Found {len(filtered_data)} users with campaign codes '{self.campaign_code}' on {self.platform_name}"
            )

            # Always refresh the period-independent leaderboard totals.
            self._store_lifetime_totals(filtered_data)

            # Raffle ticket awarding requires an active raffle period. With no
            # period we've still stored the leaderboard totals above, so just stop
            # here rather than treating it as a failure.
            period_id = self._get_active_period_id()
            if not period_id:
                logger.debug(f"No active raffle period - leaderboard totals stored, skipping ticket awards")
                return {"status": "no_active_period", "updates": 0}

            updates = []
            tickets_to_award = []  # Store ticket awards to process after wager updates

            with self.engine.begin() as conn:
                for user_data in filtered_data:
                    shuffle_username = user_data.get("username")
                    current_wager = float(user_data.get("wagerAmount", 0))

                    if not shuffle_username:
                        continue

                    # Get previous wager amount for this period
                    prev_result = conn.execute(
                        text(
                            """
                        SELECT
                            last_known_wager,
                            tickets_awarded,
                            discord_id,
                            kick_name
                        FROM raffle_shuffle_wagers
                        WHERE period_id = :period_id AND shuffle_username = :username
                          AND platform = :platform
                    """
                        ),
                        {"period_id": period_id, "username": shuffle_username, "platform": self.platform_name},
                    )

                    prev_row = prev_result.fetchone()

                    if prev_row:
                        # Existing user - check for wager increase
                        last_known_wager = float(prev_row[0])
                        total_tickets_awarded = prev_row[1]
                        discord_id = prev_row[2]
                        kick_name = prev_row[3]

                        # Calculate new wager since last check.
                        #
                        # The Shuffle API returns wagerAmount as a high-precision
                        # float (e.g. 116301.04286432), but raffle_shuffle_wagers
                        # stores last_known_wager as DECIMAL(15,2), so it reads back
                        # rounded to cents (116301.04). Subtracting the rounded stored
                        # value from the full-precision API value yields a tiny
                        # positive residue (~0.0028) on EVERY poll even when the user
                        # has not wagered. That residue is > 0 (so it slips past a
                        # naive guard) but rounds to 0.00 in the NUMERIC(15,2)
                        # wager_delta column — producing one phantom $0.00 history row
                        # per user per poll. Round the delta to cents and require a
                        # real change so only genuine wagering is recorded.
                        wager_delta = round(current_wager - last_known_wager, 2)

                        if wager_delta <= 0:
                            # No increase (or a sub-cent/decrease change, which we ignore)
                            continue

                        # Calculate tickets for the new wager amount
                        # $1000 = configured tickets per 1000 USD
                        new_tickets = int((wager_delta / 1000.0) * self.tickets_per_1000)

                        if new_tickets == 0:
                            # Less than threshold, update wager but no tickets
                            conn.execute(
                                text(
                                    """
                                UPDATE raffle_shuffle_wagers
                                SET
                                    last_known_wager = :current_wager,
                                    total_wager_usd = :current_wager,
                                    last_checked = CURRENT_TIMESTAMP,
                                    last_updated = CURRENT_TIMESTAMP
                                WHERE period_id = :period_id AND shuffle_username = :username
                                  AND platform = :platform
                            """
                                ),
                                {
                                    "period_id": period_id,
                                    "username": shuffle_username,
                                    "current_wager": current_wager,
                                    "platform": self.platform_name,
                                },
                            )
                            self._record_wager_history(conn, shuffle_username, current_wager, wager_delta)
                            continue

                        # Award tickets if user is linked
                        if discord_id and kick_name:
                            # Queue ticket award to happen after wager update transaction
                            tickets_to_award.append(
                                {
                                    "discord_id": discord_id,
                                    "kick_name": kick_name,
                                    "tickets": new_tickets,
                                    "description": f"{self.platform_name.capitalize()} wager: ${wager_delta:.2f} (${last_known_wager:.2f} → ${current_wager:.2f})",
                                    "period_id": period_id,
                                    "shuffle_username": shuffle_username,
                                    "wager_delta": wager_delta,
                                    "current_wager": current_wager,
                                }
                            )

                            # Update wager tracking (will commit with transaction)
                            conn.execute(
                                text(
                                    """
                                UPDATE raffle_shuffle_wagers
                                SET
                                    last_known_wager = :current_wager,
                                    total_wager_usd = :current_wager,
                                    tickets_awarded = tickets_awarded + :new_tickets,
                                    last_checked = CURRENT_TIMESTAMP,
                                    last_updated = CURRENT_TIMESTAMP
                                WHERE period_id = :period_id AND shuffle_username = :username
                                  AND platform = :platform
                            """
                                ),
                                {
                                    "period_id": period_id,
                                    "username": shuffle_username,
                                    "current_wager": current_wager,
                                    "new_tickets": new_tickets,
                                    "platform": self.platform_name,
                                },
                            )
                            self._record_wager_history(conn, shuffle_username, current_wager, wager_delta)
                        else:
                            # User exists but not linked - just update wager
                            conn.execute(
                                text(
                                    """
                                UPDATE raffle_shuffle_wagers
                                SET
                                    last_known_wager = :current_wager,
                                    total_wager_usd = :current_wager,
                                    last_checked = CURRENT_TIMESTAMP,
                                    last_updated = CURRENT_TIMESTAMP
                                WHERE period_id = :period_id AND shuffle_username = :username
                                  AND platform = :platform
                            """
                                ),
                                {
                                    "period_id": period_id,
                                    "username": shuffle_username,
                                    "current_wager": current_wager,
                                    "platform": self.platform_name,
                                },
                            )
                            self._record_wager_history(conn, shuffle_username, current_wager, wager_delta)
                    else:
                        # New user - check if they're linked (scoped to THIS platform
                        # so a shuffle link can't match a howl wager username or vice-versa)
                        link_result = conn.execute(
                            text(
                                """
                            SELECT discord_id, kick_name FROM raffle_shuffle_links
                            WHERE shuffle_username = :username AND verified = TRUE
                              AND platform = :platform
                        """
                            ),
                            {"username": shuffle_username, "platform": self.platform_name},
                        )

                        link_row = link_result.fetchone()
                        discord_id = link_row[0] if link_row else None
                        kick_name = link_row[1] if link_row else None

                        # Create wager tracking entry
                        # Set last_known_wager = total_wager so only FUTURE wagers earn tickets
                        conn.execute(
                            text(
                                """
                            INSERT INTO raffle_shuffle_wagers
                                (period_id, discord_server_id, shuffle_username, kick_name,
                                 discord_id, total_wager_usd, last_known_wager,
                                 tickets_awarded, platform)
                            VALUES
                                (:period_id, :server_id, :username, :kick_name,
                                 :discord_id, :wager, :wager, 0, :platform)
                        """
                            ),
                            {
                                "period_id": period_id,
                                # MUST be supplied explicitly. This column used to be
                                # left out, and the live schema carried a hardcoded
                                # DEFAULT of one specific guild's id — so every
                                # tenant's rows were stamped with THAT server and the
                                # dashboard's per-server /wagers query returned
                                # nothing for everyone else (howl servers showed an
                                # empty table). The default is dropped in
                                # migrate_fix_wager_server_id_default().
                                "server_id": self.server_id,
                                "username": shuffle_username,
                                "kick_name": kick_name,
                                "discord_id": discord_id,
                                "wager": current_wager,
                                "platform": self.platform_name,
                            },
                        )
                        # No history row for new-user onboarding: this user
                        # came in already at $current_wager from before we
                        # were tracking them. Their next observed change is
                        # what counts toward the leaderboard.

                        # No tickets awarded for initial/existing wagers
                        logger.info(
                            f"📊 Tracking new Shuffle user: {shuffle_username} (${current_wager:.2f}) - "
                            f"{'Linked to ' + kick_name if kick_name else 'Not linked'}"
                        )

            # Wager transaction is complete, now award tickets in separate transactions
            for award in tickets_to_award:
                try:
                    success = self.ticket_manager.award_tickets(
                        discord_id=award["discord_id"],
                        kick_name=award["kick_name"],
                        tickets=award["tickets"],
                        source="shuffle_wager",
                        description=award["description"],
                        period_id=award["period_id"],
                    )

                    if success:
                        updates.append(
                            {
                                "shuffle_username": award["shuffle_username"],
                                "kick_name": award["kick_name"],
                                "wager_delta": award["wager_delta"],
                                "tickets_awarded": award["tickets"],
                                "total_wager": award["current_wager"],
                            }
                        )
                        logger.info(
                            f"💰 {award['kick_name']} ({award['shuffle_username']}): "
                            f"${award['wager_delta']:.2f} wagered → {award['tickets']} tickets"
                        )
                    else:
                        logger.error(f"Failed to award {award['tickets']} tickets to {award['kick_name']}")
                except Exception as e:
                    logger.error(f"Error awarding tickets to {award['kick_name']}: {e}")

            return {"status": "success", "updates": len(updates), "details": updates}

        except Exception as e:
            logger.error(f"Failed to update Shuffle wagers: {e}")
            import traceback

            traceback.print_exc()
            return {"status": "error", "error": str(e), "updates": 0}

    def _get_active_period_id(self):
        """Get the ID of the currently active raffle period"""
        try:
            with self.engine.begin() as conn:
                if self.server_id:
                    # Multiserver: filter by discord_server_id
                    result = conn.execute(
                        text(
                            """
                        SELECT id FROM raffle_periods
                        WHERE status = 'active' AND discord_server_id = :sid
                        ORDER BY start_date DESC
                        LIMIT 1
                    """
                        ),
                        {"sid": self.server_id},
                    )
                else:
                    # Backwards compatible: no server filter
                    result = conn.execute(
                        text(
                            """
                        SELECT id FROM raffle_periods
                        WHERE status = 'active'
                        ORDER BY start_date DESC
                        LIMIT 1
                    """
                        )
                    )
                row = result.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get active period: {e}")
            return None

    def _normalize_rows(self, raw):
        """Normalize a platform's raw affiliate response into the row shape the
        update loop consumes: dicts with `username`, `wagerAmount`,
        `weightedWagerAmount`, `campaignCode`.

        `wagerAmount` (raw) drives raffle TICKET awards; `weightedWagerAmount`
        (RTP-weighted) drives the dashboard leaderboard totals. See
        _store_lifetime_totals / the ticket loop for how each is used.

        - Shuffle: a bare JSON list from the /wager/<id> endpoint, already
          carrying both `wagerAmount` and `weightedWagerAmount`. It has NO
          per-row `campaignCode` (the /wager/ endpoint drops it; the <id> in the
          URL already scopes to one affiliate), so we stamp the configured code
          onto each row to keep the campaign-code filter a no-op pass-through.
        - Howl: `{success, data:[{name, wageredUSD, userId, ...}]}` (see
          leaderboard-howl-data.md). `name`→username, `wageredUSD`→wagerAmount.
          Howl has no RTP weighting, so weighted == raw. Howl rows carry no
          per-row campaign code either, so we stamp the configured one (or "*").
        """
        if self.platform_name == "howl":
            if not isinstance(raw, dict) or not raw.get("success"):
                logger.error(f"Unexpected howl API response: {str(raw)[:200]}")
                return None
            rows = raw.get("data") or []
            stamp = self.campaign_code.split(",")[0].strip().lower() if self.campaign_code else "*"
            normalized = []
            for r in rows:
                name = r.get("name")
                if not name:
                    continue
                wagered = r.get("wageredUSD", 0)
                normalized.append(
                    {
                        "username": name,
                        "wagerAmount": wagered,
                        # Howl doesn't weight by RTP — leaderboard uses the raw total.
                        "weightedWagerAmount": wagered,
                        "campaignCode": stamp,
                    }
                )
            return normalized

        # Shuffle (and other bare-list platforms): the /wager/<id> endpoint
        # returns [{username, wagerAmount, weightedWagerAmount}]. Stamp the
        # configured campaign code so the (now redundant) campaign filter passes,
        # and default weightedWagerAmount to the raw amount if a row is missing it
        # (e.g. a legacy /stats/ response served during a transition).
        if not isinstance(raw, list):
            logger.error(f"Unexpected {self.platform_name} API response format: {type(raw)}")
            return None
        stamp = self.campaign_code.split(",")[0].strip().lower() if self.campaign_code else "*"
        normalized = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            row = dict(r)
            if "weightedWagerAmount" not in row:
                row["weightedWagerAmount"] = row.get("wagerAmount", 0)
            # Preserve a real campaignCode if present (legacy /stats/), else stamp.
            if not row.get("campaignCode"):
                row["campaignCode"] = stamp
            normalized.append(row)
        return normalized

    async def _fetch_shuffle_data(self):
        """
        Fetch wager data from the active platform's affiliate API and normalize it.

        Returns:
            list: Array of normalized user wager objects, or None on failure.
        """
        headers = {}
        params = None
        fetch_url = self.affiliate_url
        if self.platform_name == "howl":
            if not self.howl_api_key:
                logger.error("Howl selected but no howl_api_key configured")
                return None
            # Howl requires an Authorization header (raw key) and a date window.
            # We query the current calendar month: howl returns a *period total*
            # for [from, to). Within a month this grows monotonically, so the
            # tracker's `current - last_known` delta works exactly like Shuffle's
            # lifetime total. At a month rollover the period total drops, making
            # the delta <= 0 → the row is skipped (no negative tickets) — safe.
            headers["Authorization"] = self.howl_api_key
            # howl.gg fronts its API with Cloudflare bot protection that scores
            # requests; aiohttp's default "Python/x aiohttp/y" UA scores worst.
            # Identify honestly but not as a bare python client.
            headers["Accept"] = "application/json"
            headers["User-Agent"] = "Mozilla/5.0 (compatible; WagerlabsBot/1.0; +https://wagerlabs.app)"
            now = datetime.utcnow()
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            params = {
                "from": month_start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "to": now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "limit": "1000",
            }
        else:
            # Shuffle: use the newer /wager/<id> endpoint (derived from the stored
            # /stats/<id> URL). It returns both raw `wagerAmount` (drives tickets)
            # and `weightedWagerAmount` (RTP-weighted, drives the leaderboard).
            fetch_url = self._shuffle_wager_url(self.affiliate_url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(fetch_url, headers=headers, params=params, timeout=30) as response:
                    try:
                        data = await response.json(content_type=None)
                    except Exception:
                        # Non-JSON body (an HTML error page). Distinguish a
                        # Cloudflare challenge (howl.gg's zone serves a managed
                        # "Just a moment..." challenge on its own API when bot
                        # protection is dialed up — Cf-Mitigated header) from a
                        # transient 5xx page. Missed polls are harmless: totals
                        # are cumulative, so the next successful poll's delta
                        # includes everything wagered during the outage.
                        if response.headers.get("Cf-Mitigated"):
                            msg = (
                                f"{self.platform_name.capitalize()} affiliate API is behind a "
                                f"Cloudflare challenge (HTTP {response.status}, Cf-Mitigated: "
                                f"{response.headers.get('Cf-Mitigated')}) — {self.platform_name}'s "
                                f"bot protection is blocking server-to-server API calls. Polls keep "
                                f"retrying and recover on their own once the challenge lifts; if it "
                                f"persists, ask {self.platform_name} to exempt the affiliate API "
                                f"(or this server's IP) from their WAF challenge. Skipping this poll"
                            )
                            if self._cf_challenge_warned:
                                logger.debug(msg)
                            else:
                                self._cf_challenge_warned = True
                                logger.warning(msg)
                        else:
                            # Only a genuine problem is worth surfacing; a transient
                            # 5xx/timeout page will simply be retried next poll.
                            logger.warning(
                                f"{self.platform_name.capitalize()} affiliate API returned "
                                f"HTTP {response.status} with a non-JSON body; skipping this poll"
                            )
                        return None
                    if self._cf_challenge_warned:
                        self._cf_challenge_warned = False
                        logger.info(
                            f"{self.platform_name.capitalize()} affiliate API recovered from the "
                            f"Cloudflare challenge; polling resumed normally"
                        )

                    # Shuffle-ONLY error handling. Shuffle's affiliate endpoints
                    # serve rate-limit errors INCONSISTENTLY: sometimes as a proper
                    # HTTP 400/429, and sometimes as an HTTP 200 whose body is a
                    # JSON error OBJECT {"statusCode":400,"message":"TOO_MANY_REQUEST"}.
                    # Shuffle's SUCCESS body is a JSON array, so a dict from Shuffle
                    # is always an error envelope. Classify and skip cleanly.
                    #
                    # Howl is DIFFERENT: its success body is a dict
                    # ({"success": true, "data": [...]}), so we must NOT treat a
                    # Howl dict as an error — _normalize_rows validates Howl's shape
                    # (and logs if `success` is false). Only apply the array check
                    # to Shuffle-style (bare-list) platforms.
                    if self.platform_name != "howl" and not isinstance(data, list):
                        msg = "unknown error"
                        code = None
                        if isinstance(data, dict):
                            msg = data.get("message") or data.get("error") or "unknown error"
                            code = data.get("statusCode")
                        is_rate_limit = (
                            str(msg).upper() == "TOO_MANY_REQUEST"
                            or code in (400, 429)
                            or response.status in (400, 429)
                        )
                        if is_rate_limit:
                            # Transient and self-recovering (the next 2-min poll
                            # usually succeeds), so DEBUG — not worth a warning
                            # every poll during a rate-limit spell.
                            logger.debug(
                                f"{self.platform_name.capitalize()} affiliate API rate-limited "
                                f"(status {response.status}, body: {msg}); skipping this poll"
                            )
                        else:
                            # A real error (bad URL, server error, unexpected
                            # shape) IS worth surfacing — it may need attention.
                            logger.warning(
                                f"{self.platform_name.capitalize()} affiliate API error "
                                f"(HTTP {response.status}): {str(data)[:200]}"
                            )
                        return None

                    # For a non-200 status that still returned parseable JSON on a
                    # platform we didn't special-case above, let _normalize_rows do
                    # the shape validation (and its own error logging).
                    return self._normalize_rows(data)

        except asyncio.TimeoutError:
            logger.error(f"Timeout fetching {self.platform_name} affiliate data")
            return None
        except Exception as e:
            logger.error(f"Error fetching {self.platform_name} data: {e}")
            return None

    def link_shuffle_account(self, shuffle_username, kick_name, discord_id, verified=False, verified_by=None):
        """
        Link a Shuffle username to a Kick/Discord account

        Args:
            shuffle_username: Shuffle.com username
            kick_name: Kick username
            discord_id: Discord user ID
            verified: Whether admin has verified this link
            verified_by: Discord ID of admin who verified

        Returns:
            dict: Result of linking operation
        """
        try:
            with self.engine.begin() as conn:
                # Check if shuffle username already linked.
                # Scoped to platform='shuffle' so a user's howl link (now allowed
                # to coexist via UNIQUE(shuffle_username, platform)) doesn't make
                # this report a false "already linked".
                existing = conn.execute(
                    text(
                        """
                    SELECT discord_id, kick_name FROM raffle_shuffle_links
                    WHERE shuffle_username = :username AND platform = 'shuffle'
                """
                    ),
                    {"username": shuffle_username},
                )

                existing_row = existing.fetchone()

                if existing_row:
                    return {
                        "status": "already_linked",
                        "existing_discord_id": existing_row[0],
                        "existing_kick_name": existing_row[1],
                    }

                # Check if discord_id already has a shuffle link (scoped to
                # platform='shuffle' — a user may also hold a howl link under
                # UNIQUE(discord_id, platform); that must not block this).
                discord_check = conn.execute(
                    text(
                        """
                    SELECT shuffle_username FROM raffle_shuffle_links
                    WHERE discord_id = :discord_id AND platform = 'shuffle'
                """
                    ),
                    {"discord_id": discord_id},
                )

                discord_row = discord_check.fetchone()

                if discord_row:
                    return {"status": "discord_already_linked", "existing_shuffle_username": discord_row[0]}

                # Create the link (platform explicit for clarity, though the
                # column also defaults to 'shuffle')
                conn.execute(
                    text(
                        """
                    INSERT INTO raffle_shuffle_links
                        (shuffle_username, kick_name, discord_id, platform, verified, verified_by_discord_id, verified_at)
                    VALUES
                        (:shuffle_username, :kick_name, :discord_id, 'shuffle', :verified, :verified_by,
                         CASE WHEN :verified THEN CURRENT_TIMESTAMP ELSE NULL END)
                """
                    ),
                    {
                        "shuffle_username": shuffle_username,
                        "kick_name": kick_name,
                        "discord_id": discord_id,
                        "verified": verified,
                        "verified_by": verified_by,
                    },
                )

            logger.info(
                f"🔗 Linked Shuffle account: {shuffle_username} → {kick_name} (Discord: {discord_id}, verified: {verified})"
            )

            return {
                "status": "success",
                "shuffle_username": shuffle_username,
                "kick_name": kick_name,
                "discord_id": discord_id,
                "verified": verified,
            }

        except Exception as e:
            logger.error(f"Failed to link Shuffle account: {e}")
            return {"status": "error", "error": str(e)}

    def _get_active_period_id(self):
        """Get the ID of the currently active raffle period"""
        try:
            with self.engine.begin() as conn:
                if self.server_id is not None:
                    result = conn.execute(
                        text(
                            """
                        SELECT id FROM raffle_periods
                        WHERE status = 'active' AND discord_server_id = :server_id
                        ORDER BY start_date DESC
                        LIMIT 1
                    """
                        ),
                        {"server_id": self.server_id},
                    )
                else:
                    result = conn.execute(
                        text(
                            """
                        SELECT id FROM raffle_periods
                        WHERE status = 'active'
                        ORDER BY start_date DESC
                        LIMIT 1
                    """
                        )
                    )
                row = result.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get active period: {e}")
            return None


async def setup_shuffle_tracker(bot, engine, server_id=None, bot_settings=None, server_name=None):
    """
    Setup the Shuffle wager tracker as a Discord bot task

    Args:
        bot: Discord bot instance
        engine: SQLAlchemy engine
        server_id: Discord server/guild ID for multi-server support (optional)
        bot_settings: Guild-specific bot settings manager (optional)
        server_name: Discord guild name, for per-server log tagging (optional)
    """
    from discord.ext import tasks

    from utils.log_context import set_server

    tracker = ShuffleWagerTracker(engine, bot_settings=bot_settings, server_id=server_id)

    @tasks.loop(minutes=2)  # Run every 2 minutes
    async def update_shuffle_task():
        """Periodic task to update Shuffle wagers and award tickets"""
        # Tag every log line from this per-guild task with the server, so a
        # warning shows "[<server name>]" instead of "[-]" (faster debugging).
        set_server(server_id, server_name)
        try:
            # Refresh settings before each update to get latest from database
            tracker.refresh_settings()

            # Skip (and stay silent) when this server has no wager source
            # configured for its ACTIVE platform. `affiliate_url` already resolves
            # per-platform in _load_settings (the shuffle stats/wager URL when
            # shuffle is active, the howl leaderboard URL when howl is active), so
            # an empty value means "wager tracking isn't set up for this server" —
            # nothing to fetch, and any fetch warning below would be pure noise.
            # This is DEBUG only, so a server without wager tracking never emits a
            # warning about a missing/failing affiliate URL.
            if not tracker.affiliate_url:
                logger.debug("[Shuffle Tracker] No affiliate URL for active platform - skipping update")
                return

            logger.debug("[Shuffle Tracker] 🔄 Checking wagers...")
            result = await tracker.update_shuffle_wagers()

            # Only log at INFO when wagers were actually awarded or something is wrong.
            if result["status"] == "success" and result["updates"] > 0:
                logger.info(f"[Shuffle Tracker] ✅ Updated {result['updates']} wager(s)")
                # Print details of each update
                for detail in result.get("details", []):
                    logger.info(
                        f"  💰 {detail['kick_name']} ({detail['shuffle_username']}): "
                        f"${detail['wager_delta']:.2f} → +{detail['tickets_awarded']} tickets"
                    )
            elif result["status"] == "success":
                logger.debug(f"[Shuffle Tracker] No new wagers found")
            elif result["status"] == "no_active_period":
                logger.debug("[Shuffle Tracker] No active raffle period")
            elif result["status"] == "no_users":
                logger.debug(f"[Shuffle Tracker] No users found with campaign code(s): {tracker.campaign_code}")
            elif result["status"] == "fetch_failed":
                # The specific cause (rate-limit / timeout / HTTP status / bad
                # body) was already logged at the right level inside the fetch, so
                # keep this summary at DEBUG to avoid a duplicate warning per poll.
                logger.debug("[Shuffle Tracker] No data fetched this poll (see fetch log for cause)")
            elif result["status"] == "error":
                logger.warning(f"[Shuffle Tracker] ❌ Update failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[Shuffle Tracker] ❌ Task error: {e}", exc_info=True)

    @update_shuffle_task.before_loop
    async def before_shuffle_task():
        """Wait for bot to be ready before starting the task"""
        await bot.wait_until_ready()
        logger.debug("[Shuffle Tracker] ✅ Started (runs every 2 minutes)")

    # Start the task
    update_shuffle_task.start()

    return tracker
