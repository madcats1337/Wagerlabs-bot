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
                       entry_prompt, min_account_age_days, min_server_days,
                       require_captcha
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
                       entry_prompt, min_account_age_days, min_server_days,
                       require_captcha
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

    guild_id = interaction.guild_id

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
                   entry_prompt, min_account_age_days, min_server_days,
                   require_captcha, started_at, ends_at)
                VALUES
                  (:sid, :title, :description, :entry_method, :keyword,
                   :messages_required, :time_window_minutes, :allow_multiple,
                   :max_per_user, 'active', :created_by, :channel_id,
                   :duration, :max_winners, :required_role_id, CAST(:bonus AS JSONB),
                   :entry_prompt, :min_account_age_days, :min_server_days,
                   :require_captcha, CURRENT_TIMESTAMP,
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
                "entry_prompt": tpl.get("entry_prompt"),
                # Alt/bot gates carried over from the template, so a giveaway
                # started from Discord enforces the same rules as one started
                # from the dashboard.
                "min_account_age_days": tpl.get("min_account_age_days"),
                "min_server_days": tpl.get("min_server_days"),
                "require_captcha": bool(tpl.get("require_captcha")),
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
    if tpl.get("entry_prompt"):
        bits.append(f'asks "{tpl["entry_prompt"]}"')
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
# Same settings as the dashboard's create form, without a saved template.
# `entry_method` is always 'discord' — the command exists to post a panel with
# a Join button, so there is nothing to choose.
#
# A View allows five action rows and each select consumes a whole row, so the
# settings are split across TWO pages of the same ephemeral message rather than
# crammed into one. Free-text values (title, description, duration) go in the
# modal, which is capped at five components.

# Preset day counts. Free-text numbers would need modal slots that the title,
# description and duration fields already occupy.
_AGE_PRESETS = [("Off", "0"), ("7 days", "7"), ("14 days", "14"), ("30 days", "30"), ("90 days", "90")]
_TENURE_PRESETS = [("Off", "0"), ("1 day", "1"), ("3 days", "3"), ("7 days", "7"), ("30 days", "30")]


def _int_select(label_value_pairs, placeholder, current, row):
    """A single-choice Select over (label, value) pairs, with `current` marked."""
    return discord.ui.Select(
        placeholder=placeholder,
        row=row,
        options=[
            discord.SelectOption(label=label, value=value, default=(value == str(current)))
            for label, value in label_value_pairs
        ],
    )


class GiveawayCreateView(discord.ui.View):
    """Ephemeral two-page settings panel for `/giveaway create`.

    Holds the chosen settings in memory; nothing is written until the modal is
    submitted, so abandoning the panel leaves no partial giveaway behind.
    """

    def __init__(self, bot, engine, author_id):
        super().__init__(timeout=600)
        self._bot = bot
        self._engine = engine
        self._author_id = author_id
        self._page = 1

        # Settings, mirroring the dashboard's defaults.
        self.channel_id = None
        self.required_role_id = None
        self.max_winners = 1
        self.max_entries_per_user = 1
        self.min_account_age_days = 0
        self.min_server_days = 0
        self.require_captcha = False
        self.bonus_roles = {}
        self.bonus_role_id = None

        self._build()

    # ── page construction ────────────────────────────────────────────────
    def _build(self):
        self.clear_items()
        if self._page == 1:
            self._build_page_one()
        else:
            self._build_page_two()

    def _build_page_one(self):
        # default_values keeps the picked channel/role visible after the message
        # is edited — the components are rebuilt from scratch on every refresh,
        # so without it the select would snap back to its placeholder and
        # disagree with the summary embed.
        channel = discord.ui.ChannelSelect(
            placeholder="Channel to post the giveaway in…",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            row=0,
            default_values=([discord.Object(id=self.channel_id)] if self.channel_id else []),
        )
        channel.callback = self._on_channel
        self.add_item(channel)

        role = discord.ui.RoleSelect(
            placeholder="Required role (optional) — leave unset for everyone",
            row=1,
            min_values=0,
            default_values=([discord.Object(id=self.required_role_id)] if self.required_role_id else []),
        )
        role.callback = self._on_role
        self.add_item(role)

        winners = _int_select(
            [(f"{n} winner{'s' if n != 1 else ''}", str(n)) for n in (1, 2, 3, 5, 10, 25)],
            "Number of winners…",
            self.max_winners,
            row=2,
        )
        winners.callback = self._on_winners
        self.add_item(winners)

        per_user = _int_select(
            [("1 entry per person", "1")] + [(f"Up to {n} entries each", str(n)) for n in (2, 3, 5, 10)],
            "Entries per person…",
            self.max_entries_per_user,
            row=3,
        )
        per_user.callback = self._on_per_user
        self.add_item(per_user)

        nxt = discord.ui.Button(label="Next: alt checks & bonuses", style=discord.ButtonStyle.secondary, row=4)
        nxt.callback = self._on_next
        self.add_item(nxt)

        start = discord.ui.Button(
            label="Set title & duration",
            style=discord.ButtonStyle.success,
            row=4,
            disabled=self.channel_id is None,
        )
        start.callback = self._on_configure
        self.add_item(start)

    def _build_page_two(self):
        age = _int_select(_AGE_PRESETS, "Minimum account age…", self.min_account_age_days, row=0)
        age.callback = self._on_age
        self.add_item(age)

        tenure = _int_select(_TENURE_PRESETS, "Minimum time in server…", self.min_server_days, row=1)
        tenure.callback = self._on_tenure
        self.add_item(tenure)

        bonus_role = discord.ui.RoleSelect(
            placeholder="Bonus entries: pick a role (optional)",
            row=2,
            min_values=0,
            default_values=([discord.Object(id=self.bonus_role_id)] if self.bonus_role_id else []),
        )
        bonus_role.callback = self._on_bonus_role
        self.add_item(bonus_role)

        if self.bonus_role_id:
            bonus_amount = _int_select(
                [(f"+{n} extra entries", str(n)) for n in (1, 2, 3, 5, 10)],
                "…and how many extra entries",
                # Mark the amount already chosen for THIS role, so re-selecting
                # a role shows what it is currently worth.
                self.bonus_roles.get(str(self.bonus_role_id), 0),
                row=3,
            )
            bonus_amount.callback = self._on_bonus_amount
            self.add_item(bonus_amount)

        captcha = discord.ui.Button(
            label=f"Bot check: {'On' if self.require_captcha else 'Off'}",
            style=discord.ButtonStyle.success if self.require_captcha else discord.ButtonStyle.secondary,
            row=4,
        )
        captcha.callback = self._on_captcha
        self.add_item(captcha)

        back = discord.ui.Button(
            # The channel lives on page one, so say where to go when it's unset
            # rather than presenting a dead disabled button on this page.
            label="Back" if self.channel_id else "Back — pick a channel",
            style=discord.ButtonStyle.secondary if self.channel_id else discord.ButtonStyle.primary,
            row=4,
        )
        back.callback = self._on_back
        self.add_item(back)

        start = discord.ui.Button(
            label="Set title & duration",
            style=discord.ButtonStyle.success,
            row=4,
            disabled=self.channel_id is None,
        )
        start.callback = self._on_configure
        self.add_item(start)

    # ── summary ──────────────────────────────────────────────────────────
    def embed(self):
        e = discord.Embed(
            title="Create a giveaway",
            description=(
                "Choose where it posts and who can enter, then set the title and duration."
                if self._page == 1
                else "Optional checks that keep alt accounts and bots out."
            ),
            color=WAGERLABS_YELLOW,
        )
        e.add_field(
            name="Channel",
            value=f"<#{self.channel_id}>" if self.channel_id else "*required*",
            inline=True,
        )
        e.add_field(name="Winners", value=str(self.max_winners), inline=True)
        e.add_field(
            name="Entries each",
            value=str(self.max_entries_per_user),
            inline=True,
        )
        e.add_field(
            name="Required role",
            value=f"<@&{self.required_role_id}>" if self.required_role_id else "Everyone",
            inline=True,
        )
        e.add_field(
            name="Account age",
            value=f"{self.min_account_age_days}d+" if self.min_account_age_days else "Any",
            inline=True,
        )
        e.add_field(
            name="In server",
            value=f"{self.min_server_days}d+" if self.min_server_days else "Any",
            inline=True,
        )
        if self.bonus_roles:
            e.add_field(
                name="Bonus entries",
                value=", ".join(f"<@&{rid}> +{n}" for rid, n in self.bonus_roles.items()),
                inline=False,
            )
        if self.require_captcha:
            e.add_field(
                name="Bot check",
                value="Entrants verify once on the web before joining.",
                inline=False,
            )
        e.set_footer(text=f"Page {self._page} of 2")
        return e

    async def _refresh(self, interaction):
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ── callbacks ────────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("This panel isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        # Nothing is written until the modal is submitted, so an expired panel
        # has no giveaway behind it — disable the controls rather than leave
        # them live and silently dropping the settings.
        for item in self.children:
            item.disabled = True

    async def _on_channel(self, interaction):
        values = interaction.data.get("values") or []
        self.channel_id = int(values[0]) if values else None
        await self._refresh(interaction)

    async def _on_role(self, interaction):
        values = interaction.data.get("values") or []
        self.required_role_id = int(values[0]) if values else None
        await self._refresh(interaction)

    async def _on_winners(self, interaction):
        self.max_winners = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_per_user(self, interaction):
        self.max_entries_per_user = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_age(self, interaction):
        self.min_account_age_days = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_tenure(self, interaction):
        self.min_server_days = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_bonus_role(self, interaction):
        values = interaction.data.get("values") or []
        self.bonus_role_id = int(values[0]) if values else None
        # Switching roles must not strand the previous one: a role left in the
        # dict would still grant its bonus in the live giveaway, because the
        # award path takes the max across every entry in bonus_roles.
        if self.bonus_role_id:
            self.bonus_roles = {rid: n for rid, n in self.bonus_roles.items() if rid == str(self.bonus_role_id)}
        else:
            self.bonus_roles = {}
        await self._refresh(interaction)

    async def _on_bonus_amount(self, interaction):
        if self.bonus_role_id:
            self.bonus_roles = {str(self.bonus_role_id): int(interaction.data["values"][0])}
        await self._refresh(interaction)

    async def _on_captcha(self, interaction):
        self.require_captcha = not self.require_captcha
        await self._refresh(interaction)

    async def _on_next(self, interaction):
        self._page = 2
        await self._refresh(interaction)

    async def _on_back(self, interaction):
        self._page = 1
        await self._refresh(interaction)

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
            "max_entries_per_user": self.max_entries_per_user,
            # The dashboard models "more than one entry" as a flag plus a cap;
            # the panel collects only the cap, so derive the flag from it.
            "allow_multiple_entries": self.max_entries_per_user > 1,
            "required_role_id": self.required_role_id,
            "bonus_roles": self.bonus_roles or None,
            "min_account_age_days": self.min_account_age_days or None,
            "min_server_days": self.min_server_days or None,
            "require_captcha": self.require_captcha,
            "keyword": None,
            "messages_required": None,
            "time_window_minutes": None,
            "entry_prompt": None,
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

            view = GiveawayCreateView(bot, engine, interaction.user.id)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

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
                       required_role_id, entry_prompt, bonus_roles
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
