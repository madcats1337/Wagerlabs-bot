"""`/giveaway start` — launch a giveaway from a saved template.

Templates hold the reusable settings (entry method, winners, channel, role
gate, bonus roles); each run supplies only the title, an optional description,
and the duration (or no duration at all, for a manual draw).

The flow is two steps, and the split is forced by Discord's rules:

  1. The command replies with an EPHEMERAL setup panel — a template dropdown
     plus a summary of what the selected template will do, so the operator can
     see the channel, winner count, role gate and bonuses before committing.
  2. "Configure and start" opens a MODAL for the per-run fields.

Why not one modal for everything? A modal is capped at five components, and it
cannot be pre-filled from a previous selection — so a template dropdown inside
the modal would leave no room for separate day/hour/minute fields, and the
operator would be choosing a template blind. Keeping the dropdown on the panel
buys the summary *and* the five fields.

Registered once on the local command tree in on_ready, alongside the other
application-only commands (see features/discord_app_commands.py).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import text

from utils.log_context import server_context

logger = logging.getLogger(__name__)

WAGERLABS_YELLOW = 0xFACC15

# A Discord select menu holds at most 25 options.
_MAX_CHOICES = 25


def _fetch_templates_full(engine, guild_id):
    """Full template rows for the setup panel, which summarises each one."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, name, entry_method, max_winners, allow_multiple_entries,
                       max_entries_per_user, required_role_id, discord_channel_id,
                       keyword, messages_required, time_window_minutes, bonus_roles,
                       entry_prompt, entry_prompts
                FROM giveaway_templates
                WHERE discord_server_id = :sid
                ORDER BY name ASC
                LIMIT :limit
                """
            ),
            {"sid": str(guild_id), "limit": _MAX_CHOICES},
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _fetch_template(engine, template_id, guild_id):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, name, entry_method, max_winners, allow_multiple_entries,
                       max_entries_per_user, required_role_id, discord_channel_id,
                       keyword, messages_required, time_window_minutes, bonus_roles,
                       entry_prompt, entry_prompts
                FROM giveaway_templates
                WHERE id = :tid AND discord_server_id = :sid
                """
            ),
            {"tid": template_id, "sid": str(guild_id)},
        ).fetchone()
    return dict(row._mapping) if row else None


async def _create_giveaway_from_template(bot, engine, interaction, tpl, title, description, duration_minutes):
    """Insert + start a giveaway from a settings dict, post its panel, describe it.

    `tpl` is a saved template row for `/giveaway start`, or an equivalent dict
    assembled in memory by `/giveaway create`. Both go through here so there is
    exactly one place that knows how settings become a running giveaway — and
    so a new column only has to be added to this INSERT once.

    Only `tpl["name"]` is template-specific; it is optional, and the summary
    embed omits the Template field when it is absent.

    Returns (embed, giveaway_id) — or (embed, None) when it could not start.
    """
    import json as _json

    from .giveaway_panel import entry_prompts_of

    guild_id = interaction.guild_id
    prompts = entry_prompts_of(tpl)

    # Several giveaways may run at once. Each Discord-hosted one owns its own
    # panel message and `_active_discord_giveaway` resolves entries by
    # discord_message_id, so their entry pools stay separate; the manager cache
    # and the expiry loop both iterate all active rows.

    bonus_roles = tpl.get("bonus_roles")
    with engine.begin() as conn:
        # Created ACTIVE with its deadline already resolved — the bot's expiry
        # loop compares ends_at against the DB clock, so both use the same clock.
        row = conn.execute(
            text(
                """
                INSERT INTO giveaways
                  (discord_server_id, title, description, entry_method, keyword,
                   messages_required, time_window_minutes, allow_multiple_entries,
                   max_entries_per_user, status, created_by, discord_channel_id,
                   duration_minutes, max_winners, required_role_id, bonus_roles,
                   entry_prompt, entry_prompts, started_at, ends_at)
                VALUES
                  (:sid, :title, :description, :entry_method, :keyword,
                   :messages_required, :time_window_minutes, :allow_multiple,
                   :max_per_user, 'active', :created_by, :channel_id,
                   :duration, :max_winners, :required_role_id, CAST(:bonus AS JSONB),
                   :entry_prompt, CAST(:entry_prompts AS JSONB), CURRENT_TIMESTAMP,
                   -- NULL duration = no timer: ends_at stays NULL and the
                   -- expiry loop ignores it, so it waits for a manual draw.
                   CASE WHEN :duration IS NULL THEN NULL
                        ELSE CURRENT_TIMESTAMP + (:duration * INTERVAL '1 minute') END)
                RETURNING id
                """
            ),
            {
                "sid": guild_id,
                "title": title[:255],
                "description": description,
                "entry_method": tpl.get("entry_method") or "discord",
                "keyword": tpl.get("keyword"),
                "messages_required": tpl.get("messages_required"),
                "time_window_minutes": tpl.get("time_window_minutes"),
                "allow_multiple": bool(tpl.get("allow_multiple_entries")),
                "max_per_user": int(tpl.get("max_entries_per_user") or 1),
                "created_by": str(interaction.user),
                "channel_id": tpl.get("discord_channel_id"),
                "duration": duration_minutes,
                "max_winners": max(1, int(tpl.get("max_winners") or 1)),
                "required_role_id": tpl.get("required_role_id"),
                "bonus": _json.dumps(bonus_roles) if bonus_roles else None,
                # Both shapes are written: the array is authoritative, the
                # scalar carries question #1 for a service that predates it.
                "entry_prompt": prompts[0][:45] if prompts else None,
                "entry_prompts": _json.dumps(prompts) if prompts else None,
                # Alt/bot gates are NOT per-giveaway: they are a server-wide
                # policy in bot_settings, read fresh on every Join click by
                # giveaway_panel._entry_rules().
            },
        ).fetchone()
        giveaway_id = int(row[0])

    # Refresh the manager cache so chat-based entry methods see it too.
    try:
        manager = getattr(bot, "giveaway_managers", {}).get(guild_id)
        if manager:
            await manager.load_active_giveaway()
    except Exception as e:
        logger.warning(f"[giveaway] manager reload failed: {e}")

    # Post the interactive panel for Discord-hosted giveaways.
    posted = False
    is_discord = (tpl.get("entry_method") or "discord") == "discord"
    if is_discord:
        try:
            from .giveaway_panel import post_panel

            giveaway = _fetch_started_giveaway(engine, giveaway_id, guild_id)
            if giveaway:
                posted = await post_panel(bot, engine, guild_id, giveaway)
        except Exception as e:
            logger.error(f"[giveaway] panel post failed: {e}", exc_info=True)

    embed = discord.Embed(title="Giveaway started", description=f"**{title}**", color=WAGERLABS_YELLOW)
    # Absent for /giveaway create, which assembles its settings on the panel
    # rather than loading a saved template.
    if tpl.get("name"):
        embed.add_field(name="Template", value=tpl["name"], inline=True)
    ends_epoch = _ends_at_epoch(engine, giveaway_id)
    embed.add_field(
        name="Ends",
        value=f"<t:{ends_epoch}:R>" if ends_epoch else "No timer — draw manually",
        inline=True,
    )
    if is_discord and tpl.get("discord_channel_id"):
        embed.add_field(name="Channel", value=f"<#{int(tpl['discord_channel_id'])}>", inline=True)
    if is_discord and prompts:
        embed.add_field(
            name="Entry question" if len(prompts) == 1 else "Entry questions",
            value=("\n".join(f"{i}. {p}" for i, p in enumerate(prompts, 1)) if len(prompts) > 1 else prompts[0]),
            inline=True,
        )
    if is_discord and not posted:
        embed.add_field(
            name="Note",
            value="The panel could not be posted — check the template's channel.",
            inline=False,
        )
    logger.info(f"[giveaway] started id={giveaway_id} via {tpl.get('name') or 'ad-hoc create'}")
    return embed, giveaway_id


def _describe_template(tpl) -> str:
    """One-glance summary of what a template will do, for the setup panel."""
    from .giveaway_panel import entry_prompts_of

    bits = []
    method = tpl.get("entry_method") or "discord"
    bits.append(
        {"discord": "Discord button", "keyword": "Keyword", "active_chatter": "Active chatter"}.get(method, method)
    )
    winners = int(tpl.get("max_winners") or 1)
    bits.append(f"{winners} winner{'s' if winners != 1 else ''}")
    if tpl.get("discord_channel_id"):
        bits.append(f"<#{int(tpl['discord_channel_id'])}>")
    if tpl.get("required_role_id"):
        bits.append(f"requires <@&{int(tpl['required_role_id'])}>")
    bonus = tpl.get("bonus_roles") or {}
    if isinstance(bonus, dict) and bonus:
        parts = [f"<@&{int(rid)}> +{int(extra)}" for rid, extra in list(bonus.items())[:3]]
        bits.append("bonus: " + ", ".join(parts))
    prompts = entry_prompts_of(tpl)
    if len(prompts) == 1:
        bits.append(f'asks "{prompts[0]}"')
    elif prompts:
        bits.append(f"asks {len(prompts)} questions")
    return " · ".join(bits)


class GiveawayDetailsModal(discord.ui.Modal):
    """Collects the per-run fields. The template is already chosen on the panel.

    Exactly five components — Discord's hard cap for a modal — which is why the
    template lives on the panel rather than in here.
    """

    def __init__(self, bot, engine, tpl):
        # `tpl` is a saved template row (from /giveaway start) or an in-memory
        # settings dict (from /giveaway create). Only the former has a name and
        # an id, and only the former is re-read on submit.
        super().__init__(title=(f"Start: {tpl['name']}" if tpl.get("name") else "New giveaway")[:45])
        self._bot = bot
        self._engine = engine
        self._tpl = tpl

        self.title_input = discord.ui.TextInput(placeholder="Summer Cash Drop", max_length=100, required=True)
        self.description_input = discord.ui.TextInput(
            placeholder="Optional",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        # Separate numeric fields rather than one parsed string: no ambiguity
        # about what "2 30" means, and each field stays short enough to validate.
        self.days_input = discord.ui.TextInput(placeholder="0", max_length=3, required=False)
        self.hours_input = discord.ui.TextInput(placeholder="0", max_length=3, required=False)
        self.minutes_input = discord.ui.TextInput(placeholder="30", max_length=4, required=False)

        # ui.Label (discord.py 2.6+) allows a description line under each field,
        # which plain TextInput labels cannot carry.
        self.add_item(discord.ui.Label(text="Title", component=self.title_input))
        self.add_item(
            discord.ui.Label(
                text="Description",
                description="Shown under the title on the panel.",
                component=self.description_input,
            )
        )
        self.add_item(
            discord.ui.Label(
                text="Days",
                description="Leave all three blank for no timer (draw manually).",
                component=self.days_input,
            )
        )
        self.add_item(discord.ui.Label(text="Hours", component=self.hours_input))
        self.add_item(discord.ui.Label(text="Minutes", component=self.minutes_input))

    @staticmethod
    def _as_int(field) -> int:
        raw = (field.value or "").strip()
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except ValueError:
            return -1  # signals "not a number"

    async def on_submit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else None
        with server_context(guild_id, guild_name):
            days = self._as_int(self.days_input)
            hours = self._as_int(self.hours_input)
            minutes = self._as_int(self.minutes_input)
            if -1 in (days, hours, minutes):
                await interaction.response.send_message(
                    "Duration must be whole numbers. Leave a field blank for zero.", ephemeral=True
                )
                return

            # All three blank = no timer: the giveaway runs until it is drawn
            # by hand from the dashboard console (ends_at stays NULL, so the
            # expiry loop never picks it up).
            duration_minutes = days * 1440 + hours * 60 + minutes or None
            if duration_minutes and duration_minutes > 527040:  # ~1 year
                await interaction.response.send_message("Duration must be under one year.", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)

            # Re-read a SAVED template: it may have been edited or deleted while
            # the modal sat open. An ad-hoc create has no stored row to re-read —
            # its settings live on the panel — so use them as given.
            if self._tpl.get("id"):
                tpl = _fetch_template(self._engine, self._tpl["id"], guild_id)
                if not tpl:
                    await interaction.followup.send("That template no longer exists.", ephemeral=True)
                    return
            else:
                tpl = self._tpl

            embed, giveaway_id = await _create_giveaway_from_template(
                self._bot,
                self._engine,
                interaction,
                tpl,
                self.title_input.value.strip(),
                (self.description_input.value or "").strip() or None,
                duration_minutes,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"[giveaway] setup modal error: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


# ── /giveaway create ─────────────────────────────────────────────────────────
#
# Same per-giveaway settings as the dashboard's create form, without a saved
# template. `entry_method` is always 'discord' — the command exists to post a
# panel with a Join button, so there is nothing to choose. `max_entries_per_user`
# is likewise omitted: a Discord giveaway is one click per member.
#
# Alt/bot checks are NOT here. They are a server-wide policy configured once on
# the dashboard (Giveaway Management -> Entry rules) and read fresh from
# bot_settings on every Join click.
#
# Built with Components V2 (LayoutView + Container), NOT a plain View, so each
# dropdown can carry a TextDisplay heading above it. A classic View has no way
# to label a select beyond its placeholder — which vanishes the moment a value
# is chosen — and is capped at five rows.


def _opts(label_value_pairs, current):
    """SelectOptions with `current` marked, so the choice survives a re-render."""
    return [
        discord.SelectOption(label=label, value=value, default=(value == str(current)))
        for label, value in label_value_pairs
    ]


class GiveawayPromptModal(discord.ui.Modal):
    """The entry questions for the `/giveaway create` panel.

    Its own modal rather than extra fields on GiveawayDetailsModal, which is
    already at Discord's five-component ceiling. Submitting edits the panel in
    place: a modal opened FROM a component may respond by editing the message it
    was opened from, so the operator lands back on the panel with the questions
    shown rather than on a separate confirmation.

    All five question slots are shown at once — the same ceiling that caps the
    questions themselves — so there is no add/remove dance inside a modal (which
    cannot re-render). Blank slots are dropped and the rest close up, so the
    operator can clear #1 and keep #2 without stranding an empty question.
    """

    def __init__(self, view):
        from .giveaway_panel import MAX_ENTRY_PROMPT_LEN, MAX_ENTRY_PROMPTS

        super().__init__(title="Entry questions")
        self._view = view
        self._inputs = []
        existing = list(view.entry_prompts)
        for index in range(MAX_ENTRY_PROMPTS):
            field = discord.ui.TextInput(
                placeholder="e.g. Your Steam trade link" if index == 0 else "Optional",
                # 45 is both the entry_prompt column width and Discord's cap on
                # a TextInput label — which is exactly what this text becomes
                # when a member joins — so it can never silently truncate.
                max_length=MAX_ENTRY_PROMPT_LEN,
                required=False,
                default=existing[index] if index < len(existing) else None,
            )
            self._inputs.append(field)
            self.add_item(
                discord.ui.Label(
                    text=f"Question {index + 1}",
                    description=(
                        "Shown as a field label when a member joins. Leave blank for no question."
                        if index == 0
                        else None
                    ),
                    component=field,
                )
            )

    async def on_submit(self, interaction: discord.Interaction):
        # Blanks are dropped, so clearing every box is the way back to "no
        # questions" — the panel has no separate remove control.
        self._view.entry_prompts = [(f.value or "").strip() for f in self._inputs if (f.value or "").strip()]
        self._view._rerender()
        await interaction.response.edit_message(view=self._view)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.error(f"[giveaway] entry question modal error: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


class GiveawayCreateView(discord.ui.LayoutView):
    """Ephemeral settings panel for `/giveaway create`.

    Holds the chosen settings in memory; nothing is written until the modal is
    submitted, so abandoning the panel leaves no partial giveaway behind.

    NOTE: the re-render method is `_rerender`, NOT `_refresh` — `View._refresh`
    is discord.py's own hook for syncing component state back from Discord, and
    overriding it with a coroutine breaks that sync and logs
    "coroutine ... was never awaited".
    """

    def __init__(self, bot, engine, author_id):
        super().__init__(timeout=600)
        self._bot = bot
        self._engine = engine
        self._author_id = author_id

        # Settings, mirroring the dashboard's defaults.
        self.channel_id = None
        self.required_role_id = None
        self.max_winners = 1
        # Ordered entry questions (max 5 — Discord's modal component cap).
        self.entry_prompts = []
        self.bonus_roles = {}
        self.bonus_role_id = None

        self._rerender()

    # ── layout ───────────────────────────────────────────────────────────
    def _rerender(self):
        """Rebuild the whole container from current state.

        Every component is recreated, so anything that should look "chosen"
        must be re-marked here — `default=` on options and `default_values=`
        on the entity selects.
        """
        self.clear_items()
        container = discord.ui.Container(accent_colour=WAGERLABS_YELLOW)

        container.add_item(
            discord.ui.TextDisplay("## Create a giveaway\nSet it up here, then add the title and how long it runs.")
        )
        container.add_item(discord.ui.Separator())

        # ── Channel ──
        channel_state = f"<#{self.channel_id}>" if self.channel_id else "*required*"
        container.add_item(
            discord.ui.TextDisplay(f"**Channel** · {channel_state}\n-# Where the giveaway panel is posted.")
        )
        channel = discord.ui.ChannelSelect(
            placeholder="Choose a channel…",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            default_values=([discord.Object(id=self.channel_id)] if self.channel_id else []),
        )
        channel.callback = self._on_channel
        container.add_item(discord.ui.ActionRow(channel))

        # ── Winners ──
        container.add_item(
            discord.ui.TextDisplay(f"**Winners** · {self.max_winners}\n-# How many people win the giveaway.")
        )
        winners = discord.ui.Select(
            placeholder="How many winners…",
            options=_opts(
                [(f"{n} winner{'s' if n != 1 else ''}", str(n)) for n in (1, 2, 3, 5, 10, 25)],
                self.max_winners,
            ),
        )
        winners.callback = self._on_winners
        container.add_item(discord.ui.ActionRow(winners))

        # ── Required role ──
        role_state = f"<@&{self.required_role_id}>" if self.required_role_id else "everyone can enter"
        container.add_item(
            discord.ui.TextDisplay(
                f"**Required role** · {role_state}\n-# Only members with this role can join. Optional."
            )
        )
        role = discord.ui.RoleSelect(
            placeholder="Choose a role (optional)…",
            min_values=0,
            default_values=([discord.Object(id=self.required_role_id)] if self.required_role_id else []),
        )
        role.callback = self._on_role
        container.add_item(discord.ui.ActionRow(role))

        # ── Entry question ──
        # A button rather than an inline field: free text cannot be typed into a
        # select, and the details modal is already at the five-component cap.
        if not self.entry_prompts:
            prompt_state = "no questions"
        elif len(self.entry_prompts) == 1:
            prompt_state = f'asks "{self.entry_prompts[0]}"'
        else:
            prompt_state = "asks " + ", ".join(f'"{p}"' for p in self.entry_prompts)
        container.add_item(
            discord.ui.TextDisplay(
                f"**Entry questions** · {prompt_state}"
                "\n-# Ask for text answers when a member joins. Up to 5, all in one popup. Optional."
            )
        )
        prompt = discord.ui.Button(
            label="Edit entry questions" if self.entry_prompts else "Add entry questions",
            style=discord.ButtonStyle.secondary,
        )
        prompt.callback = self._on_entry_prompt
        container.add_item(discord.ui.ActionRow(prompt))

        # Alt/bot checks are deliberately NOT here: they are a server-wide
        # policy set once on the dashboard (Giveaway Management -> Entry rules)
        # and applied to every Discord giveaway.

        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay("### Bonus entries\n-# Give a role extra tickets in the draw. Optional.")
        )

        # ── Bonus role ──
        bonus_state = (
            ", ".join(f"<@&{rid}> +{n}" for rid, n in self.bonus_roles.items()) if self.bonus_roles else "none"
        )
        container.add_item(
            discord.ui.TextDisplay(f"**Bonus role** · {bonus_state}\n-# This role's entry counts more than once.")
        )
        bonus_role = discord.ui.RoleSelect(
            placeholder="Choose a role to reward (optional)…",
            min_values=0,
            default_values=([discord.Object(id=self.bonus_role_id)] if self.bonus_role_id else []),
        )
        bonus_role.callback = self._on_bonus_role
        container.add_item(discord.ui.ActionRow(bonus_role))

        if self.bonus_role_id:
            container.add_item(
                discord.ui.TextDisplay("**Extra entries**\n-# How many tickets that role's single entry is worth.")
            )
            amount = discord.ui.Select(
                placeholder="How many extra entries…",
                options=_opts(
                    [(f"+{n} extra entries", str(n)) for n in (1, 2, 3, 5, 10)],
                    self.bonus_roles.get(str(self.bonus_role_id), 0),
                ),
            )
            amount.callback = self._on_bonus_amount
            container.add_item(discord.ui.ActionRow(amount))

        container.add_item(discord.ui.Separator())

        # ── Actions ──
        start = discord.ui.Button(
            label="Set title and duration" if self.channel_id else "Choose a channel first",
            style=discord.ButtonStyle.primary,
            disabled=self.channel_id is None,
        )
        start.callback = self._on_configure
        container.add_item(discord.ui.ActionRow(start))

        self.add_item(container)

    async def _apply(self, interaction):
        self._rerender()
        await interaction.response.edit_message(view=self)

    # ── callbacks ────────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # Nothing is written until the modal is submitted, so an expired panel
        # has no giveaway behind it.
        for item in self.walk_children():
            if hasattr(item, "disabled"):
                item.disabled = True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        logger.error(f"[giveaway] create panel error: {error}", exc_info=True)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)

    @staticmethod
    def _first_id(interaction):
        """The single chosen value as an int, or None when the select is cleared."""
        values = interaction.data.get("values") or []
        return int(values[0]) if values else None

    async def _on_channel(self, interaction):
        self.channel_id = self._first_id(interaction)
        await self._apply(interaction)

    async def _on_role(self, interaction):
        self.required_role_id = self._first_id(interaction)
        await self._apply(interaction)

    async def _on_winners(self, interaction):
        self.max_winners = self._first_id(interaction) or 1
        await self._apply(interaction)

    async def _on_entry_prompt(self, interaction):
        # A modal must be the FIRST response, so this cannot go through _apply.
        # The modal re-renders the panel itself on submit.
        await interaction.response.send_modal(GiveawayPromptModal(self))

    async def _on_bonus_role(self, interaction):
        self.bonus_role_id = self._first_id(interaction)
        # Switching roles must not strand the previous one: a role left in the
        # dict would still grant its bonus in the live giveaway, because the
        # award path takes the max across every entry in bonus_roles.
        if self.bonus_role_id:
            key = str(self.bonus_role_id)
            # Default to +1 so a role picked without touching the amount select
            # still counts — it is shown as chosen, so silently dropping it
            # would contradict the panel.
            self.bonus_roles = {key: self.bonus_roles.get(key, 1)}
        else:
            self.bonus_roles = {}
        await self._apply(interaction)

    async def _on_bonus_amount(self, interaction):
        if self.bonus_role_id:
            self.bonus_roles = {str(self.bonus_role_id): self._first_id(interaction) or 1}
        await self._apply(interaction)

    async def _on_configure(self, interaction):
        if not self.channel_id:
            await interaction.response.send_message("Pick a channel first.", ephemeral=True)
            return
        await interaction.response.send_modal(GiveawayDetailsModal(self._bot, self._engine, self.as_settings()))

    def as_settings(self):
        """The panel's state in the shape `_create_giveaway_from_template` reads.

        No `name` key — that is what marks this as an ad-hoc create rather than
        a saved template, and keeps the Template field off the summary embed.
        """
        return {
            "entry_method": "discord",
            "discord_channel_id": self.channel_id,
            "max_winners": self.max_winners,
            # One click per member: a Discord giveaway has no repeat-entry mode,
            # so the cap is 1 and multiple entries stay off. A role bonus is a
            # ticket WEIGHTING on that single entry, not extra clicks.
            "max_entries_per_user": 1,
            "allow_multiple_entries": False,
            "required_role_id": self.required_role_id,
            "bonus_roles": self.bonus_roles or None,
            "keyword": None,
            "messages_required": None,
            "time_window_minutes": None,
            # `_create_giveaway_from_template` reads this through
            # entry_prompts_of() and derives the legacy scalar itself.
            "entry_prompts": self.entry_prompts or None,
        }


class GiveawaySetupView(discord.ui.View):
    """Ephemeral setup panel: pick a template, see what it does, then configure.

    Short-lived and per-invocation (not a persistent view): it is only ever
    attached to one ephemeral message, so there is nothing to re-bind after a
    restart.
    """

    def __init__(self, bot, engine, templates, author_id):
        super().__init__(timeout=300)
        self._bot = bot
        self._engine = engine
        self._templates = {int(t["id"]): t for t in templates}
        self._author_id = author_id
        self.selected_id = None

        self.select = discord.ui.Select(
            placeholder="Choose a template…",
            options=[
                discord.SelectOption(
                    label=t["name"][:100],
                    value=str(t["id"]),
                    description=_describe_template(t)[:100] or None,
                )
                for t in templates[:25]
            ],
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.configure_button = discord.ui.Button(
            label="Configure and start", style=discord.ButtonStyle.success, disabled=True
        )
        self.configure_button.callback = self._on_configure
        self.add_item(self.configure_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # The message is ephemeral so only the invoker can see it, but guard the
        # components anyway.
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        self.selected_id = int(self.select.values[0])
        tpl = self._templates.get(self.selected_id)
        # Keep the choice visible after the message is edited.
        for option in self.select.options:
            option.default = option.value == str(self.selected_id)
        self.configure_button.disabled = False
        embed = discord.Embed(
            title="Start a giveaway",
            description=f"**{tpl['name']}**\n{_describe_template(tpl)}",
            color=WAGERLABS_YELLOW,
        )
        embed.set_footer(
            text="Continue to set the title, description and duration. Leave the duration blank to draw manually."
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_configure(self, interaction: discord.Interaction):
        tpl = self._templates.get(self.selected_id)
        if not tpl:
            await interaction.response.send_message("Pick a template first.", ephemeral=True)
            return
        await interaction.response.send_modal(GiveawayDetailsModal(self._bot, self._engine, tpl))


def register_giveaway_commands(bot: commands.Bot, engine) -> None:
    """Add the /giveaway group to the tree (idempotent)."""
    if bot.tree.get_command("giveaway") is not None:
        return

    giveaway_group = app_commands.Group(
        name="giveaway",
        description="Start and manage giveaways.",
        guild_only=True,
    )

    def _may_manage(interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_guild))

    @giveaway_group.command(name="start", description="Start a giveaway from a saved template.")
    async def giveaway_start(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else None

        with server_context(guild_id, guild_name):
            # Only admins may start a giveaway — the panel posts publicly and
            # the draw is real.
            if not _may_manage(interaction):
                await interaction.response.send_message(
                    "You need Manage Server permission to start a giveaway.", ephemeral=True
                )
                return

            templates = _fetch_templates_full(engine, guild_id)
            if not templates:
                await interaction.response.send_message(
                    "No giveaway templates yet. Create one on the dashboard first " "(Giveaways -> Templates).",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="Start a giveaway",
                description="Pick a template to see what it does.",
                color=WAGERLABS_YELLOW,
            )
            view = GiveawaySetupView(bot, engine, templates, interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @giveaway_group.command(
        name="create",
        description="Create and start a Discord giveaway without a saved template.",
    )
    async def giveaway_create(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        guild_name = interaction.guild.name if interaction.guild else None

        with server_context(guild_id, guild_name):
            if not _may_manage(interaction):
                await interaction.response.send_message(
                    "You need Manage Server permission to create a giveaway.", ephemeral=True
                )
                return

            # Components V2: the LayoutView carries its own headings and copy,
            # so there is no separate embed (and a V2 message cannot have one).
            view = GiveawayCreateView(bot, engine, interaction.user.id)
            await interaction.response.send_message(view=view, ephemeral=True)

    bot.tree.add_command(giveaway_group)
    logger.debug("Registered /giveaway command group")


def _fetch_started_giveaway(engine, giveaway_id, guild_id):
    """The row shape post_panel expects."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, title, description, status, started_at, ends_at, ended_at,
                       max_winners, discord_channel_id, discord_message_id,
                       required_role_id, entry_prompt, entry_prompts, bonus_roles
                FROM giveaways
                WHERE id = :gid AND discord_server_id = :sid
                """
            ),
            {"gid": giveaway_id, "sid": guild_id},
        ).fetchone()
    return dict(row._mapping) if row else None


def _ends_at_epoch(engine, giveaway_id) -> int:
    """Unix seconds for the giveaway's deadline, for a Discord timestamp."""
    from datetime import timezone

    with engine.connect() as conn:
        row = conn.execute(text("SELECT ends_at FROM giveaways WHERE id = :gid"), {"gid": giveaway_id}).fetchone()
    if not row or not row[0]:
        return 0
    value = row[0]
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())
