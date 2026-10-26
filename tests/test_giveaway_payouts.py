"""Giveaway prize payouts: asking winners where to send their prize.

A giveaway with a prize amount owes each winner that much, and the streamer tips it on
the casino the server names (`giveaway_payout_casino`). Viewers save their username
there with the Payout details button (link panel or winner DM); once the winners are
announced, each one nobody asked yet gets one DM (features/giveaway/payouts.py).
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from sqlalchemy import create_engine, text

from features.giveaway import payouts as p
from features.linking.combined_link_panel import CombinedLinkPanelView

SERVER = 42
OTHER_SERVER = 43


@pytest.fixture
def engine():
    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        for ddl in (
            "CREATE TABLE bot_settings (key TEXT, value TEXT, discord_server_id BIGINT, UNIQUE (key, discord_server_id))",
            """CREATE TABLE viewer_payout_details (
                discord_server_id BIGINT NOT NULL, discord_id BIGINT NOT NULL, casino TEXT NOT NULL,
                username TEXT NOT NULL, updated_at TIMESTAMP, PRIMARY KEY (discord_server_id, discord_id, casino))""",
            """CREATE TABLE raffle_shuffle_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, shuffle_username TEXT, discord_id BIGINT,
                verified BOOLEAN DEFAULT 0, platform TEXT)""",
            """CREATE TABLE giveaways (
                id INTEGER, discord_server_id BIGINT, title TEXT, prize_amount NUMERIC, entry_method TEXT)""",
            """CREATE TABLE giveaway_winners (
                id INTEGER PRIMARY KEY AUTOINCREMENT, giveaway_id INTEGER, discord_server_id TEXT,
                kick_username TEXT, discord_id BIGINT, drawn_at TIMESTAMP, paid_at TIMESTAMP,
                payout_notice TEXT, payout_notice_at TIMESTAMP)""",
            """CREATE TABLE giveaway_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, giveaway_id INTEGER, discord_server_id BIGINT,
                kick_username TEXT, discord_id BIGINT, discord_username TEXT, platform TEXT, platform_user_id TEXT)""",
            """CREATE TABLE links (
                discord_id BIGINT, kick_name TEXT, discord_server_id BIGINT, platform TEXT,
                platform_user_id TEXT, linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""",
        ):
            conn.execute(text(ddl))
    return eng


def run(engine, sql, **params):
    with engine.begin() as conn:
        return conn.execute(text(sql), params)


def tip_on(engine, casino, server=SERVER):
    run(
        engine,
        "INSERT INTO bot_settings (key, value, discord_server_id) VALUES ('giveaway_payout_casino', :v, :g)",
        v=casino,
        g=server,
    )


def giveaway(engine, gid=1, amount=25, method="keyword", title="Friday drop", server=SERVER):
    run(
        engine,
        "INSERT INTO giveaways (id, discord_server_id, title, prize_amount, entry_method) VALUES (:i, :g, :t, :a, :m)",
        i=gid,
        g=server,
        t=title,
        a=amount,
        m=method,
    )


def winner(engine, name, gid=1, discord_id=None, server=SERVER, **cols):
    sets = {
        "giveaway_id": gid,
        "discord_server_id": str(server),
        "kick_username": name,
        "discord_id": discord_id,
        **cols,
    }
    run(
        engine,
        f"INSERT INTO giveaway_winners ({', '.join(sets)}) VALUES ({', '.join(':' + k for k in sets)})",
        **sets,
    )


def entry(engine, name, gid=1, platform="kick", uid=None, discord_id=None, server=SERVER):
    run(
        engine,
        """INSERT INTO giveaway_entries (giveaway_id, discord_server_id, kick_username, discord_id, platform, platform_user_id)
           VALUES (:gid, :g, :n, :d, :p, :u)""",
        gid=gid,
        g=server,
        n=name,
        d=discord_id,
        p=platform,
        u=uid,
    )


def link(engine, discord_id, name, platform="kick", uid=None, server=SERVER):
    run(
        engine,
        "INSERT INTO links (discord_id, kick_name, discord_server_id, platform, platform_user_id) VALUES (:d, :n, :g, :p, :u)",
        d=discord_id,
        n=name,
        g=server,
        p=platform,
        u=uid,
    )


def notices(engine, gid=1):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT kick_username, payout_notice FROM giveaway_winners WHERE giveaway_id = :g"), {"g": gid}
            ).fetchall()
        )


# ---------------------------------------------------------------------------
# Small rules
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("amount, shown", [(25, "$25"), (12.5, "$12.50"), ("10.50", "$10.50"), (1000, "$1,000")])
def test_money(amount, shown):
    assert p.money(amount) == shown


@pytest.mark.parametrize(
    "name, ok",
    [
        ("alpha", True),
        ("a_b.c-9", True),
        ("ab", True),
        ("a", False),
        ("has space", False),
        ("<@123>", False),
        ("x" * 33, False),
        ("", False),
    ],
)
def test_a_casino_username(name, ok):
    assert p.valid_username(name) is ok


def test_the_winner_dm_says_where_the_prize_goes_or_how_to_say_where():
    content, label = p.winner_dm_text(25, "Friday *drop*", "Lele's", "stake", "alpha_1")
    assert content == (
        "You won **$25** in **Friday \\*drop\\*** on **Lele's**. It will be tipped to **alpha\\_1** on Stake. "
        "Wrong name? Change it below."
    )
    assert label == "Change payout details"
    content, label = p.winner_dm_text(10.5, "Friday drop", None, "shuffle", None)
    assert (
        content == "You won **$10.50** in **Friday drop**. Add your Shuffle username below so it can be tipped to you."
    )
    assert label == "Add payout details"


def test_the_chat_note_fits_in_one_kick_message():
    note = p.not_linked_chat_text(["alpha", "beta"], "Friday drop", "stake")
    assert note.startswith("@alpha @beta you won Friday drop! To get your prize, link your account")
    assert note.endswith("add your Stake username.")
    many = p.not_linked_chat_text([f"viewer{i:03d}" for i in range(200)], "Friday drop", "stake")
    assert len(many) <= p.CHAT_LIMIT and many.startswith("@viewer000 @viewer001")


# ---------------------------------------------------------------------------
# Where a prize goes
# ---------------------------------------------------------------------------
def test_payouts_are_off_until_a_known_casino_is_picked(engine):
    assert p.payout_casino(engine, SERVER) is None
    tip_on(engine, "nowhere", server=OTHER_SERVER)
    assert p.payout_casino(engine, OTHER_SERVER) is None
    tip_on(engine, " Stake ")
    assert p.payout_casino(engine, SERVER) == "stake"


def test_a_saved_name_wins_over_a_verified_link_and_belongs_to_one_server(engine):
    run(
        engine,
        "INSERT INTO raffle_shuffle_links (shuffle_username, discord_id, verified, platform) VALUES ('BetaShuf', 9002, 1, 'shuffle')",
    )
    run(
        engine,
        "INSERT INTO raffle_shuffle_links (shuffle_username, discord_id, verified, platform) VALUES ('maybe', 9003, 0, 'shuffle')",
    )
    assert p.payout_destination(engine, SERVER, 9002, "shuffle") == ("BetaShuf", True)
    assert p.payout_destination(engine, SERVER, 9003, "shuffle") == (None, False)  # not verified
    p.save_username(engine, SERVER, 9002, "shuffle", "betashuf")
    assert p.payout_destination(engine, SERVER, 9002, "shuffle") == ("betashuf", True)
    p.save_username(engine, SERVER, 9002, "shuffle", "newBeta")
    assert p.payout_destination(engine, SERVER, 9002, "shuffle") == ("newBeta", False)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM viewer_payout_details")).scalar() == 1
    assert p.payout_destination(engine, OTHER_SERVER, 9001, "shuffle") == (None, False)
    p.save_username(engine, SERVER, 9001, "stake", "alpha")
    assert p.payout_destination(engine, OTHER_SERVER, 9001, "stake") == (None, False)
    assert p.payout_destination(engine, SERVER, 9001, "shuffle") == (None, False)


def test_who_a_winner_is_on_discord(engine):
    giveaway(engine)
    entry(engine, "alpha", uid="501")
    link(engine, 9001, "renamed", uid="501")  # by the chatter's id, whatever their handle now
    entry(engine, "beta")
    link(engine, 9002, "Beta")  # by handle on that platform
    entry(engine, "gated", discord_id=9004)  # a gated entry holds it
    link(engine, 9003, "gamma")  # no entry left: the handle names one person
    link(engine, 9005, "twins")
    link(engine, 9006, "twins")
    entry(engine, "delta", platform="twitch")
    link(engine, 9007, "delta", platform="kick")  # another platform's link doesn't count
    g = p._priced_giveaway(engine, SERVER, 1)
    who = {
        name: p.winner_discord_id(engine, SERVER, g, {"kick_username": name})
        for name in ("alpha", "beta", "gated", "gamma", "twins", "delta")
    }
    assert who == {"alpha": 9001, "beta": 9002, "gated": 9004, "gamma": 9003, "twins": None, "delta": None}
    assert p.winner_discord_id(engine, SERVER, g, {"kick_username": "x", "discord_id": 9010}) == 9010


# ---------------------------------------------------------------------------
# Asking winners
# ---------------------------------------------------------------------------
class FakeUser:
    def __init__(self, closed=False):
        self.closed = closed
        self.sent = []

    async def send(self, content=None, view=None):
        if self.closed:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user"
            )
        self.sent.append((content, view))


class FakeBot:
    def __init__(self, users):
        self.users = users

    def get_guild(self, gid):
        return SimpleNamespace(name="Lele's") if gid == SERVER else None

    def get_user(self, uid):
        return self.users.get(uid)

    async def fetch_user(self, uid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown User")


def ask(engine, bot, gid=1, chat=None):
    async def go():
        return await p.notify_prize_winners(bot, engine, SERVER, gid, send_chat=chat)

    return asyncio.run(go())


@pytest.fixture
def drawn(engine):
    tip_on(engine, "stake")
    giveaway(engine)
    entry(engine, "alpha", uid="501")
    link(engine, 9001, "alpha", uid="501")
    p.save_username(engine, SERVER, 9001, "stake", "alphaStake")
    winner(engine, "alpha")
    entry(engine, "beta")
    link(engine, 9002, "beta")
    winner(engine, "beta")
    entry(engine, "delta")
    winner(engine, "delta")
    return engine


def test_each_winner_is_asked_once_and_how_it_went_is_kept(drawn):
    users = {9001: FakeUser(), 9002: FakeUser(closed=True)}
    said = []

    async def chat(message, guild_id=None):
        said.append((message, guild_id))

    bot = FakeBot(users)
    assert ask(drawn, bot, chat=chat) == 3
    assert notices(drawn) == {"alpha": "dm_sent", "beta": "dm_closed", "delta": "not_linked"}
    content, view = users[9001].sent[0]
    assert "**$25**" in content and "**alphaStake** on Stake" in content and "**Lele's**" in content
    (button,) = view.children
    assert isinstance(button, p.PayoutDetailsButton) and button.custom_id == f"wl_payout:{SERVER}"
    assert said == [(p.not_linked_chat_text(["delta"], "Friday drop", "stake"), SERVER)]
    # Announcing again (a reroll's re-announcement, the stop after the last draw) asks nobody twice.
    assert ask(drawn, bot, chat=chat) == 0
    assert len(users[9001].sent) == 1 and len(said) == 1


def test_a_rerolled_in_winner_is_the_only_one_asked(drawn):
    users = {9001: FakeUser(), 9002: FakeUser(), 9003: FakeUser()}
    bot = FakeBot(users)
    ask(drawn, bot)
    link(drawn, 9003, "gamma")
    winner(drawn, "gamma")
    assert ask(drawn, bot) == 1
    assert users[9003].sent and "Add your Stake username" in users[9003].sent[0][0]


def test_a_claim_left_by_a_dead_bot_is_taken_again_once_stale(engine):
    tip_on(engine, "stake")
    giveaway(engine)
    winner(engine, "alpha", payout_notice="pending", payout_notice_at=p._utcnow() - timedelta(minutes=2))
    assert p._claim_winners(engine, SERVER, 1) == []
    run(engine, "UPDATE giveaway_winners SET payout_notice_at = :t", t=p._utcnow() - timedelta(minutes=11))
    assert [w["kick_username"] for w in p._claim_winners(engine, SERVER, 1)] == ["alpha"]


def test_a_paid_winner_is_never_asked(engine):
    tip_on(engine, "stake")
    giveaway(engine)
    winner(engine, "alpha", discord_id=9001, paid_at=p._utcnow())
    assert ask(engine, FakeBot({9001: FakeUser()})) == 0


@pytest.mark.parametrize("casino, amount", [(None, 25), ("stake", None)])
def test_nobody_is_asked_without_a_casino_or_a_prize_amount(engine, casino, amount):
    if casino:
        tip_on(engine, casino)
    giveaway(engine, amount=amount)
    winner(engine, "alpha", discord_id=9001)
    user = FakeUser()
    assert ask(engine, FakeBot({9001: user})) == 0
    assert notices(engine) == {"alpha": None} and user.sent == []


def test_a_discord_giveaway_never_posts_in_chat(engine):
    tip_on(engine, "stake")
    giveaway(engine, method="discord")
    winner(engine, "eps")  # a Discord winner whose id was lost
    said = []

    async def chat(message, guild_id=None):
        said.append(message)

    assert ask(engine, FakeBot({}), chat=chat) == 1
    assert notices(engine) == {"eps": "not_linked"} and said == []


def test_asking_in_the_background_does_the_same(drawn):
    users = {9001: FakeUser(), 9002: FakeUser()}

    async def go():
        await p.ask_prize_winners_soon(FakeBot(users), drawn, SERVER, 1)

    asyncio.run(go())
    assert notices(drawn) == {"alpha": "dm_sent", "beta": "dm_sent", "delta": "not_linked"}


def test_a_failed_background_ask_is_logged_not_raised(caplog):
    async def go():
        # No tables at all: the very first read fails.
        task = p.ask_prize_winners_soon(FakeBot({}), create_engine("sqlite://"), SERVER, 1)
        await task
        return task

    task = asyncio.run(go())
    assert task.exception() is None
    assert "asking the prize winners of 1 failed" in caplog.text


# ---------------------------------------------------------------------------
# The button and the form
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self):
        self.messages = []
        self.modals = []

    async def send_message(self, content=None, ephemeral=False, **kw):
        self.messages.append(content)

    async def send_modal(self, modal):
        self.modals.append(modal)

    def is_done(self):
        return bool(self.messages or self.modals)


def interaction(user_id=9001):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=FakeResponse(),
        client=SimpleNamespace(get_guild=lambda gid: SimpleNamespace(name="Lele's") if gid == SERVER else None),
    )


def test_the_button_opens_the_form_with_the_name_they_have(engine):
    async def go():
        button = p.PayoutDetailsButton(SERVER, engine=engine)
        off = interaction()
        await button.callback(off)
        tip_on(engine, "stake")
        p.save_username(engine, SERVER, 9001, "stake", "alphaStake")
        on = interaction()
        await button.callback(on)
        return off, on

    off, on = asyncio.run(go())
    assert off.response.messages == ["This server isn't collecting payout details right now."]
    (modal,) = on.response.modals
    field = modal.username.to_component_dict()
    assert field["label"] == "Your Stake username" and field["value"] == "alphaStake"


def test_the_form_saves_a_good_name_for_the_casino_it_asked_about(engine):
    tip_on(engine, "stake")

    async def submit(value, switch_to=None):
        modal = p.PayoutDetailsModal(engine, SERVER, "Lele's", "stake")
        modal.username._value = value
        if switch_to:
            run(engine, "UPDATE bot_settings SET value = :v", v=switch_to)
        i = interaction()
        await modal.on_submit(i)
        return i.response.messages

    assert asyncio.run(submit("has space")) == [p.USERNAME_RULE]
    assert p.payout_destination(engine, SERVER, 9001, "stake") == (None, False)
    assert asyncio.run(submit(" alpha_1 ")) == ["Saved. Prizes you win in **Lele's** go to **alpha\\_1** on Stake."]
    assert p.payout_destination(engine, SERVER, 9001, "stake") == ("alpha_1", False)
    assert asyncio.run(submit("beta", switch_to="shuffle")) == [
        "This server changed where it sends prizes. Press the button again."
    ]
    assert p.payout_destination(engine, SERVER, 9001, "stake") == ("alpha_1", False)


def _custom_ids(view):
    return [getattr(item, "custom_id", None) for item in view.walk_children()]


def test_the_link_panel_shows_payout_details_where_prizes_are_tipped(engine):
    run(
        engine,
        "INSERT INTO bot_settings (key, value, discord_server_id) VALUES ('stream_platforms', 'kick', :g)",
        g=SERVER,
    )

    def panel(**kw):
        async def build():
            return CombinedLinkPanelView(None, engine, lambda *a: "", lambda *a: "", show_logo=False, **kw)

        return asyncio.run(build())

    assert f"wl_payout:{SERVER}" not in _custom_ids(panel(platforms=["kick"], guild_id=SERVER))
    tip_on(engine, "stake")
    posted = panel(platforms=["kick"], guild_id=SERVER)
    assert _custom_ids(posted).count(f"wl_payout:{SERVER}") == 1
    assert any("add your Stake username" in getattr(item, "content", "") for item in posted.walk_children())
    # The global persistent view carries no copy: the button answers as a dynamic item.
    assert not any((cid or "").startswith("wl_payout:") for cid in _custom_ids(panel()))
