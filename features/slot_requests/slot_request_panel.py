"""
Slot Request Panel - Interactive Discord panel for managing slot requests
Shows statistics and allows picking random slots via buttons
"""

import logging
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Button, Modal, TextInput, View
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Emojis
EMOJI_RANDOM = "🎲"  # Pick random slot
EMOJI_REFRESH = "♻️"  # Refresh panel


class SetMaxRequestsModal(Modal, title="Set Max Requests Per User"):
    """Modal for setting maximum requests per user"""

    max_requests = TextInput(
        label="Maximum Requests (0 = unlimited)",
        placeholder="Enter a number from 0 to 10",
        required=True,
        min_length=1,
        max_length=2,
        default="0",
    )

    def __init__(self, panel):
        super().__init__()
        self.panel = panel
        # Set current value as default
        current = panel.tracker.max_requests_per_user
        self.max_requests.default = str(current)

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        try:
            # Validate input
            value = int(self.max_requests.value)

            if value < 0 or value > 10:
                await interaction.response.send_message("❌ Please enter a number between 0 and 10.", ephemeral=True)
                return

            # Set the limit
            success = self.panel.tracker.set_max_requests(value)

            if success:
                limit_text = f"{value} request(s)" if value > 0 else "unlimited"
                await interaction.response.send_message(
                    f"✅ Max requests per user set to: **{limit_text}**", ephemeral=True
                )

                # Update the panel
                await self.panel.update_panel(force=True)
            else:
                await interaction.response.send_message(
                    "❌ Failed to update the limit. Check logs for details.", ephemeral=True
                )

        except ValueError:
            await interaction.response.send_message("❌ Please enter a valid number.", ephemeral=True)


class SlotPanelView(View):
    """Button view for slot request panel - looks up panel by guild_id for multi-server support"""

    def __init__(self, bot=None):
        super().__init__(timeout=None)  # Persistent view
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Check if user has permission to use buttons"""
        # Check if user is admin
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Only administrators can use this panel.", ephemeral=True)
            return False
        return True

    def _get_panel(self, interaction: discord.Interaction):
        """Get the panel for the interaction's guild"""
        if not self.bot or not hasattr(self.bot, "slot_panels_by_guild"):
            return None
        return self.bot.slot_panels_by_guild.get(interaction.guild_id)

    @discord.ui.button(style=discord.ButtonStyle.primary, label="Pick Random", emoji="🎲", custom_id="slot_pick_random")
    async def pick_random_button(self, interaction: discord.Interaction, button: Button):
        """Handle pick random button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.pick_random_slot_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Refresh", emoji="♻️", custom_id="slot_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        """Handle refresh button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.refresh_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.success, label="Enable Requests", emoji="✅", custom_id="slot_enable")
    async def enable_button(self, interaction: discord.Interaction, button: Button):
        """Handle enable requests button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.enable_requests_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.danger, label="Disable Requests", emoji="❌", custom_id="slot_disable")
    async def disable_button(self, interaction: discord.Interaction, button: Button):
        """Handle disable requests button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.disable_requests_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )

    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Set Limit", emoji="🔢", custom_id="slot_set_limit")
    async def set_limit_button(self, interaction: discord.Interaction, button: Button):
        """Handle set limit button click"""
        panel = self._get_panel(interaction)
        if panel:
            await panel.set_limit_interaction(interaction)
        else:
            await interaction.response.send_message(
                "❌ Panel not initialized for this server. Please recreate the panel.", ephemeral=True
            )


class SlotRequestPanel:
    """Manages the slot request panel message"""

    def __init__(self, bot, engine, slot_call_tracker, kick_send_callback=None):
        self.bot = bot
        self.engine = engine
        self.tracker = slot_call_tracker
        self.kick_send_callback = kick_send_callback
        self.guild_id = slot_call_tracker.server_id if slot_call_tracker else None
        self.panel_message_id = None
        self.panel_channel_id = None
        self.last_update_time = None  # Track last update time for rate limiting
        self.update_cooldown = 30  # Minimum 30 seconds between updates
        self._load_panel_info()

    def _load_panel_info(self):
        """Load panel message ID and channel from database"""
        if not self.engine or not self.guild_id:
            return

        try:
            with self.engine.connect() as conn:
                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'slot_panel_message_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": str(self.guild_id)},
                ).fetchone()

                if result:
                    self.panel_message_id = int(result[0])

                result = conn.execute(
                    text(
                        """
                    SELECT value FROM bot_settings
                    WHERE key = 'slot_panel_channel_id' AND discord_server_id = :guild_id
                """
                    ),
                    {"guild_id": str(self.guild_id)},
                ).fetchone()

                if result:
                    self.panel_channel_id = int(result[0])

        except Exception as e:
            logger.error(f"Failed to load panel info: {e}")

    def _save_panel_info(self):
        """Save panel message ID and channel to database"""
        if not self.engine or not self.guild_id:
            return

        try:
            with self.engine.begin() as conn:
                # Save message ID
                if self.panel_message_id:
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                        VALUES ('slot_panel_message_id', :value, :guild_id, CURRENT_TIMESTAMP)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = :value, updated_at = CURRENT_TIMESTAMP
                    """
                        ),
                        {"value": str(self.panel_message_id), "guild_id": str(self.guild_id)},
                    )

                # Save channel ID
                if self.panel_channel_id:
                    conn.execute(
                        text(
                            """
                        INSERT INTO bot_settings (key, value, discord_server_id, updated_at)
                        VALUES ('slot_panel_channel_id', :value, :guild_id, CURRENT_TIMESTAMP)
                        ON CONFLICT (key, discord_server_id)
                        DO UPDATE SET value = :value, updated_at = CURRENT_TIMESTAMP
                    """
                        ),
                        {"value": str(self.panel_channel_id), "guild_id": str(self.guild_id)},
                    )

        except Exception as e:
            logger.error(f"Failed to save panel info: {e}")

    def _get_slot_stats(self):
        """Get slot request statistics from database"""
        if not self.engine:
            return None

        # Get server_id from tracker
        server_id = self.tracker.server_id if self.tracker else None
        if not server_id:
            logger.warning("No server_id available for stats, showing all servers")
        else:
            logger.debug(f"Getting slot stats for server_id: {server_id} (type: {type(server_id).__name__})")

        try:
            with self.engine.connect() as conn:
                # Get counts - filter by server_id if available
                if server_id:
                    # Ensure server_id is int for BIGINT column comparison
                    server_id = int(server_id)

                    total = conn.execute(
                        text("SELECT COUNT(*) FROM slot_requests WHERE discord_server_id = :server_id"),
                        {"server_id": server_id},
                    ).fetchone()[0]
                    unpicked = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM slot_requests WHERE picked = FALSE AND discord_server_id = :server_id"
                        ),
                        {"server_id": server_id},
                    ).fetchone()[0]
                    picked = conn.execute(
                        text(
                            "SELECT COUNT(*) FROM slot_requests WHERE picked = TRUE AND discord_server_id = :server_id"
                        ),
                        {"server_id": server_id},
                    ).fetchone()[0]

                    # Get last picked slot for this server
                    last_picked = conn.execute(
                        text(
                            """
                        SELECT kick_username, slot_call, picked_at
                        FROM slot_requests
                        WHERE picked = TRUE AND discord_server_id = :server_id
                        ORDER BY picked_at DESC
                        LIMIT 1
                    """
                        ),
                        {"server_id": server_id},
                    ).fetchone()

                    logger.debug(
                        f"Stats for server {server_id}: total={total}, unpicked={unpicked}, picked={picked}, last_picked={last_picked}"
                    )
                else:
                    # Fallback: show all servers
                    total = conn.execute(text("SELECT COUNT(*) FROM slot_requests")).fetchone()[0]
                    unpicked = conn.execute(text("SELECT COUNT(*) FROM slot_requests WHERE picked = FALSE")).fetchone()[
                        0
                    ]
                    picked = conn.execute(text("SELECT COUNT(*) FROM slot_requests WHERE picked = TRUE")).fetchone()[0]

                    # Get last picked slot
                    last_picked = conn.execute(
                        text(
                            """
                        SELECT kick_username, slot_call, picked_at
                        FROM slot_requests
                        WHERE picked = TRUE
                        ORDER BY picked_at DESC
                        LIMIT 1
                    """
                        )
                    ).fetchone()

                return {"total": total, "unpicked": unpicked, "picked": picked, "last_picked": last_picked}
        except Exception as e:
            logger.error(f"Failed to get slot stats: {e}")
            return None

    def _create_panel_embed(self):
        """Create the panel embed with current stats"""
        stats = self._get_slot_stats()

        if not stats:
            embed = discord.Embed(
                title="🎰 Slot Request Panel", description="❌ Could not load statistics", color=discord.Color.red()
            )
            return embed

        # Build description
        desc_lines = []
        desc_lines.append(f"**Total Requests:** {stats['total']}")
        desc_lines.append(f"**Available:** {stats['unpicked']}")
        desc_lines.append(f"**Already Picked:** {stats['picked']}")

        if stats["last_picked"]:
            username, slot_call, picked_at = stats["last_picked"]
            time_str = picked_at.strftime("%H:%M:%S") if picked_at else "Unknown"
            desc_lines.append(f"\n**Last Picked:**")
            desc_lines.append(f"• {slot_call}")
            desc_lines.append(f"• by {username} at {time_str}")
        else:
            desc_lines.append(f"\n**Last Picked:** None yet")

        embed = discord.Embed(
            title="🎰 Slot Request Panel", description="\n".join(desc_lines), color=discord.Color.gold()
        )

        # Status indicator
        status = "✅ Open" if self.tracker.is_enabled() else "❌ Closed"
        embed.add_field(name="Status", value=status, inline=True)

        # Max requests per user
        max_req = self.tracker.max_requests_per_user
        limit_text = f"{max_req} request(s)" if max_req > 0 else "Unlimited"
        embed.add_field(name="Max Per User", value=limit_text, inline=True)

        # Instructions
        embed.add_field(name="How to use", value="Use the buttons below to manage slot requests", inline=False)

        embed.set_footer(text=f"Last updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")

        return embed

    async def create_panel(self, channel: discord.TextChannel):
        """Create a new panel in the specified channel"""
        try:
            embed = self._create_panel_embed()
            view = SlotPanelView(self.bot)  # Pass bot for guild lookup

            message = await channel.send(embed=embed, view=view)

            # Save panel info
            self.panel_message_id = message.id
            self.panel_channel_id = channel.id
            self._save_panel_info()

            logger.info(f"Created slot request panel in {channel.name}")
            return True

        except Exception as e:
            logger.error(f"Failed to create panel: {e}")
            return False

    async def update_panel(self, force=False):
        """
        Update the existing panel

        Args:
            force: If True, bypass rate limiting (for user actions like button clicks)
        """
        if not self.panel_message_id or not self.panel_channel_id:
            return False

        # Rate limiting - prevent Discord API spam
        if not force and self.last_update_time:
            time_since_last = (datetime.utcnow() - self.last_update_time).total_seconds()
            if time_since_last < self.update_cooldown:
                logger.debug(
                    f"Skipping panel update (cooldown: {self.update_cooldown - time_since_last:.1f}s remaining)"
                )
                return False

        try:
            channel = self.bot.get_channel(self.panel_channel_id)
            if not channel:
                logger.error(f"Panel channel {self.panel_channel_id} not found")
                return False

            message = await channel.fetch_message(self.panel_message_id)
            if not message:
                logger.error(f"Panel message {self.panel_message_id} not found")
                return False

            embed = self._create_panel_embed()
            view = SlotPanelView(self.bot)  # Pass bot for guild lookup

            await message.edit(embed=embed, view=view)
            self.last_update_time = datetime.utcnow()  # Update timestamp

            return True

        except discord.NotFound:
            logger.warning("Panel message not found, needs to be recreated")
            self.panel_message_id = None
            self.panel_channel_id = None
            return False
        except Exception as e:
            logger.error(f"Failed to update panel: {e}")
            return False

    async def pick_random_slot_interaction(self, interaction: discord.Interaction):
        """Handle pick random button click"""
        await interaction.response.defer(ephemeral=True)

        result = await self._pick_random_slot(interaction.channel)
        if result:
            await interaction.followup.send("✅ Random slot picked!", ephemeral=True)
        else:
            await interaction.followup.send("❌ No slot requests available.", ephemeral=True)

    async def refresh_interaction(self, interaction: discord.Interaction):
        """Handle refresh button click"""
        await interaction.response.defer(ephemeral=True)

        success = await self.update_panel(force=True)  # Force update for user action
        if success:
            await interaction.followup.send("✅ Panel refreshed!", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to refresh panel.", ephemeral=True)

    async def enable_requests_interaction(self, interaction: discord.Interaction):
        """Handle enable requests button click"""
        await interaction.response.defer(ephemeral=True)

        if self.tracker.is_enabled():
            await interaction.followup.send("ℹ️ Slot requests are already enabled.", ephemeral=True)
        else:
            await self.tracker.set_enabled(True)
            await interaction.followup.send("✅ Slot requests **enabled**!", ephemeral=True)

    async def disable_requests_interaction(self, interaction: discord.Interaction):
        """Handle disable requests button click"""
        await interaction.response.defer(ephemeral=True)

        if not self.tracker.is_enabled():
            await interaction.followup.send("ℹ️ Slot requests are already disabled.", ephemeral=True)
        else:
            await self.tracker.set_enabled(False)
            await interaction.followup.send("✅ Slot requests **disabled**!", ephemeral=True)

    async def set_limit_interaction(self, interaction: discord.Interaction):
        """Handle set limit button click - show modal"""
        modal = SetMaxRequestsModal(self)
        await interaction.response.send_modal(modal)

    async def handle_reaction(self, payload):
        """Handle reaction on the panel"""
        # Ignore if not the panel message
        if payload.message_id != self.panel_message_id:
            return

        # Ignore bot's own reactions
        if payload.user_id == self.bot.user.id:
            return

        # Get the user
        user = self.bot.get_user(payload.user_id)
        if not user:
            return

        # Get the channel and message
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        try:
            message = await channel.fetch_message(payload.message_id)

            # Remove user's reaction
            await message.remove_reaction(payload.emoji, user)

            # Check if user has admin permissions
            member = channel.guild.get_member(user.id)
            if not member or not member.guild_permissions.administrator:
                # Silently ignore non-admins
                return

            # Handle the reaction
            if str(payload.emoji) == EMOJI_RANDOM:
                await self._pick_random_slot(channel)
            elif str(payload.emoji) == EMOJI_REFRESH:
                await self.update_panel()

        except Exception as e:
            logger.error(f"Error handling panel reaction: {e}")

    async def _pick_random_slot(self, channel):
        """Pick a random slot request using provably fair algorithm - returns True if successful, False if no slots available"""
        import secrets

        from utils.provably_fair import generate_provably_fair_result

        if not self.engine:
            return False

        # Get guild_id from channel
        guild_id = channel.guild.id if hasattr(channel, "guild") else None
        if not guild_id:
            logger.error("Cannot pick slot: no guild_id available")
            return False

        try:
            with self.engine.connect() as conn:
                # Get ALL unpicked slot requests for this server (for provably fair selection)
                results = conn.execute(
                    text(
                        """
                    SELECT id, kick_username, slot_call, requested_at
                    FROM slot_requests
                    WHERE picked = FALSE AND discord_server_id = :server_id
                    ORDER BY id ASC
                """
                    ),
                    {"server_id": guild_id},
                ).fetchall()

                if not results:
                    # No slots available - just update panel
                    await self.update_panel()
                    return False

                # Use provably fair to select winner
                total_requests = len(results)

                # Generate server seed
                server_seed = secrets.token_hex(32)

                # Use first request as base for client seed
                first_request = results[0]
                client_seed = f"slot_picker:{guild_id}:{total_requests}"

                # Generate provably fair result
                result = generate_provably_fair_result(
                    kick_username=client_seed,
                    slot_request_id=first_request[0],  # Use first request ID as nonce base
                    slot_call="random_pick",
                    chance_percent=100.0,  # Always "wins" to generate random value
                )

                # Use random_value to select from requests
                winner_index = int((result["random_value"] / 100.0) * total_requests)
                winner_index = min(winner_index, total_requests - 1)  # Ensure within bounds

                selected_request = results[winner_index]
                request_id, username, slot_call, requested_at = selected_request

                logger.info(
                    f"[Server {guild_id}] Provably fair pick - Random: {result['random_value']}, Index: {winner_index}/{total_requests}, Winner: {username}"
                )

                # Mark as picked and save provably fair data
                with self.engine.begin() as update_conn:
                    update_conn.execute(
                        text(
                            """
                        UPDATE slot_requests
                        SET picked = TRUE,
                            picked_at = CURRENT_TIMESTAMP,
                            server_seed = :server_seed,
                            client_seed = :client_seed,
                            nonce = :nonce,
                            proof_hash = :proof_hash,
                            random_value = :random_value
                        WHERE id = :id
                    """
                        ),
                        {
                            "id": request_id,
                            "server_seed": result["server_seed"],
                            "client_seed": result["client_seed"],
                            "nonce": result["nonce"],
                            "proof_hash": result["proof_hash"],
                            "random_value": result["random_value"],
                        },
                    )

                logger.info(f"[Server {guild_id}] Panel picked random slot: {slot_call} by {username} (provably fair)")

                # Check for rewards from slot_rewards table
                won_reward = False
                reward_type = None
                reward_amount = None
                reward_id = None
                reward_chance = 0

                try:
                    with self.engine.connect() as reward_conn:
                        # Get ALL enabled rewards, ordered by amount DESC (highest reward checked first)
                        all_rewards = reward_conn.execute(
                            text(
                                """
                            SELECT id, reward_type, reward_amount, reward_chance_percent
                            FROM slot_rewards
                            WHERE discord_server_id = :server_id
                              AND enabled = TRUE
                            ORDER BY reward_amount DESC
                        """
                            ),
                            {"server_id": guild_id},
                        ).fetchall()

                        if all_rewards:
                            # Check if user is linked (verified)
                            link_check = reward_conn.execute(
                                text(
                                    """
                                SELECT 1 FROM links
                                WHERE kick_name = :username AND discord_server_id = :server_id
                                LIMIT 1
                            """
                                ),
                                {"username": username, "server_id": guild_id},
                            ).fetchone()
                            is_linked = link_check is not None

                            if is_linked:
                                # Check each reward in order (highest amount first)
                                for reward_row in all_rewards:
                                    current_chance = float(reward_row[3])

                                    if current_chance > 0 and result["random_value"] < current_chance:
                                        # Won this reward!
                                        won_reward = True
                                        reward_id = reward_row[0]
                                        reward_type = reward_row[1]
                                        reward_amount = float(reward_row[2])
                                        reward_chance = current_chance

                                        logger.info(
                                            f"[Server {guild_id}] WON! Random: {result['random_value']:.2f} < Chance: {current_chance}% = ${reward_amount} {reward_type}"
                                        )
                                        break
                                    else:
                                        logger.info(
                                            f"[Server {guild_id}] No win - Random: {result['random_value']:.2f} >= Chance: {current_chance}%"
                                        )

                                # If no reward won, use the first reward's chance for recording
                                if not won_reward and all_rewards:
                                    reward_chance = float(all_rewards[0][3])

                                # Record the pick in slot_picks table
                                with self.engine.begin() as pick_conn:
                                    pick_conn.execute(
                                        text(
                                            """
                                        INSERT INTO slot_picks
                                            (slot_request_id, discord_server_id, kick_username, slot_call,
                                             reward_won, reward_type, reward_amount, reward_id,
                                             server_seed, client_seed, nonce, proof_hash, random_value, chance_percent)
                                        VALUES (:slot_request_id, :server_id, :username, :slot_call,
                                                :reward_won, :reward_type, :reward_amount, :reward_id,
                                                :server_seed, :client_seed, :nonce, :proof_hash, :random_value, :chance_percent)
                                    """
                                        ),
                                        {
                                            "slot_request_id": request_id,
                                            "server_id": guild_id,
                                            "username": username,
                                            "slot_call": slot_call,
                                            "reward_won": won_reward,
                                            "reward_type": reward_type if won_reward else None,
                                            "reward_amount": reward_amount if won_reward else None,
                                            "reward_id": reward_id if won_reward else None,
                                            "server_seed": result["server_seed"],
                                            "client_seed": result["client_seed"],
                                            "nonce": result["nonce"],
                                            "proof_hash": result["proof_hash"],
                                            "random_value": result["random_value"],
                                            "chance_percent": reward_chance,
                                        },
                                    )
                            else:
                                logger.info(f"[Server {guild_id}] User {username} not linked - no reward eligibility")
                except Exception as reward_error:
                    logger.error(f"[Server {guild_id}] Error checking rewards: {reward_error}")

                # Check if slot overlay is enabled in dashboard settings
                overlay_delay_needed = False
                if self.engine:
                    try:
                        with self.engine.connect() as check_conn:
                            # Check if overlay is explicitly enabled
                            overlay_setting = check_conn.execute(
                                text(
                                    """
                                SELECT value FROM bot_settings
                                WHERE key = 'slot_overlay_enabled' AND discord_server_id = :server_id
                            """
                                ),
                                {"server_id": guild_id},
                            ).fetchone()

                            if overlay_setting:
                                overlay_delay_needed = overlay_setting[0] == "true"
                                logger.info(f"[Server {guild_id}] Slot overlay setting: {overlay_setting[0]}")
                            else:
                                # Fallback: check if there are any unpicked slots (indicates overlay usage)
                                remaining_slots = check_conn.execute(
                                    text(
                                        """
                                    SELECT COUNT(*) FROM slot_requests
                                    WHERE discord_server_id = :server_id AND picked = FALSE
                                """
                                    ),
                                    {"server_id": guild_id},
                                ).fetchone()[0]
                                overlay_delay_needed = remaining_slots > 0
                                logger.info(
                                    f"[Server {guild_id}] Fallback overlay check - {remaining_slots} unpicked slots"
                                )
                    except Exception as delay_check_error:
                        logger.warning(f"Could not check overlay status: {delay_check_error}")

                # Apply 9-second delay if overlay is being used (syncs with slot picker animation)
                if overlay_delay_needed:
                    import asyncio

                    await asyncio.sleep(9)

                # Send message to Kick chat
                if self.kick_send_callback:
                    try:
                        if won_reward and reward_type and reward_amount:
                            reward_type_display = (
                                "Bonus Buy" if reward_type == "bonus_buy" else reward_type.capitalize()
                            )
                            kick_message = f"🎰 PICKED: {slot_call} (requested by @{username}) 💰 WON ${reward_amount:.2f} {reward_type_display}!"
                        else:
                            kick_message = f"🎰 Random slot picked: {slot_call} (requested by @{username})"
                        await self.kick_send_callback(kick_message, guild_id=guild_id)
                    except Exception as kick_error:
                        logger.error(f"Failed to send pick notification to Kick: {kick_error}")

                # Update panel
                await self.update_panel()
                return True

        except Exception as e:
            logger.error(f"Failed to pick random slot from panel: {e}")
            return False


class SlotRequestPanelCommands(commands.Cog):
    """Commands for managing the slot request panel"""

    def __init__(self, bot, panel: SlotRequestPanel = None):
        self.bot = bot
        self.default_panel = panel  # Fallback for backwards compatibility
        self.auto_update_task.start()

    def _get_panel_for_guild(self, guild_id: int) -> Optional[SlotRequestPanel]:
        """Get the correct panel for a guild"""
        # First try per-guild panels
        if hasattr(self.bot, "slot_panels_by_guild"):
            panel = self.bot.slot_panels_by_guild.get(guild_id)
            if panel:
                logger.info(f"Found panel for guild {guild_id}")
                return panel
            else:
                # Debug: show what keys exist
                keys = list(self.bot.slot_panels_by_guild.keys())
                logger.warning(f"No panel found for guild {guild_id}. Available guild IDs: {keys}")
        # DO NOT fall back to default panel - that causes cross-server issues
        logger.error(f"No panel available for guild {guild_id}")
        return None

    def cog_unload(self):
        """Clean up when cog is unloaded"""
        self.auto_update_task.cancel()

    @tasks.loop(minutes=3)
    async def auto_update_task(self):
        """Auto-update panels every 3 minutes"""
        # Update all guild panels
        if hasattr(self.bot, "slot_panels_by_guild"):
            for guild_id, panel in self.bot.slot_panels_by_guild.items():
                try:
                    await panel.update_panel()
                except Exception as e:
                    logger.error(f"Failed to auto-update panel for guild {guild_id}: {e}")
        elif self.default_panel:
            await self.default_panel.update_panel()

    @auto_update_task.before_loop
    async def before_auto_update(self):
        """Wait for bot to be ready"""
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="slotpanel")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def create_slot_panel(self, ctx):
        """
        [ADMIN] Create a slot request panel in this channel
        Usage: /slotpanel
        """
        panel = self._get_panel_for_guild(ctx.guild.id)
        if not panel:
            await ctx.send("❌ Slot panel not initialized for this server")
            return

        success = await panel.create_panel(ctx.channel)
        if success:
            await ctx.send("✅ Slot request panel created! React with 🎲 to pick a random slot.")
        else:
            await ctx.send("❌ Failed to create panel. Check logs for details.")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Handle reactions on the panel"""
        if not payload.guild_id:
            return
        panel = self._get_panel_for_guild(payload.guild_id)
        if panel:
            await panel.handle_reaction(payload)


async def setup_slot_panel(bot, engine, slot_call_tracker, kick_send_callback=None):
    """Setup the slot request panel system"""
    panel = SlotRequestPanel(bot, engine, slot_call_tracker, kick_send_callback)

    # Set panel reference in tracker so it can trigger updates
    slot_call_tracker.panel = panel

    await bot.add_cog(SlotRequestPanelCommands(bot, panel))
    logger.info("Slot request panel system initialized")
    return panel
