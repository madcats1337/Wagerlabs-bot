"""One-off: credit raffle tickets lost to the discarded-remainder bug.

Until the fix in `compute_wager_award`, the wager tracker paid
`int(poll_delta / 1000 * rate)` tickets per poll and then advanced the baseline
to the viewer's full current total — throwing away everything below one whole
ticket, on every poll. On a 2-minute loop at 5 tickets per $1,000 (one ticket =
$200) the average observed delta was $33.62 and only 1.32% of polls ever reached
$200 on their own, so essentially all wagering converted to nothing.

This re-derives what each linked viewer was owed for a raffle period from
`shuffle_wager_history` (which recorded every observed delta correctly), awards
the difference, and leaves both watermarks consistent with the fixed tracker:

    owed        = floor(wagered_during_period / dollars_per_ticket)
    shortfall   = owed - tickets_already_awarded
    pending     = wagered_during_period - owed * dollars_per_ticket
    last_known_wager = total_wager_usd - pending   # so `unpaid` == pending

DRY RUN BY DEFAULT. Pass --apply to write. Scoped to ONE period so it can't
touch history that has already been drawn.

  railway run -e production -- python scripts/fixes/backfill_wager_ticket_shortfall.py \
      --server <guild_id> [--period <id>] [--apply]
"""

import argparse
import os
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from raffle_system.tickets import TicketManager  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", type=int, required=True, help="discord_server_id")
    ap.add_argument("--period", type=int, help="raffle period id (default: the active one)")
    ap.add_argument("--platform", default="howl")
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    args = ap.parse_args()

    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL not set")
    engine = create_engine(url)

    with engine.connect() as conn:
        if args.period:
            period = conn.execute(
                text(
                    "SELECT id, start_date, end_date, status FROM raffle_periods "
                    "WHERE id = :pid AND discord_server_id = :sid"
                ),
                {"pid": args.period, "sid": args.server},
            ).fetchone()
        else:
            period = conn.execute(
                text(
                    "SELECT id, start_date, end_date, status FROM raffle_periods "
                    "WHERE discord_server_id = :sid AND status = 'active' "
                    "ORDER BY start_date DESC LIMIT 1"
                ),
                {"sid": args.server},
            ).fetchone()
        if not period:
            sys.exit("No matching raffle period")
        period_id, start_date, end_date, status = period

        rate = conn.execute(
            text(
                "SELECT value FROM bot_settings " "WHERE key = 'shuffle_tickets_per_1000' AND discord_server_id = :sid"
            ),
            {"sid": args.server},
        ).fetchone()
        rate = int(rate[0]) if rate and str(rate[0]).strip().isdigit() else 20
        dollars_per_ticket = 1000.0 / rate

        print(f"period {period_id} ({status})  {start_date} -> {end_date}")
        print(f"rate {rate} tickets/$1000  =>  ${dollars_per_ticket:.2f} per ticket")
        print(f"mode: {'APPLY' if args.apply else 'DRY RUN'}\n")

        # Verified links joined to their wager row and to the wagering that
        # history actually recorded inside the period window.
        rows = conn.execute(
            text(
                """
                SELECT l.shuffle_username,
                       l.discord_id,
                       l.kick_name,
                       COALESCE(w.tickets_awarded, 0)  AS already,
                       COALESCE(w.total_wager_usd, 0)  AS total_wager_usd,
                       COALESCE((
                           SELECT SUM(h.wager_delta)
                           FROM shuffle_wager_history h
                           WHERE h.discord_server_id = :sid
                             AND h.shuffle_username = l.shuffle_username
                             AND h.recorded_at >= :start_date
                             AND h.recorded_at < :end_date
                       ), 0) AS wagered
                FROM raffle_shuffle_links l
                LEFT JOIN raffle_shuffle_wagers w
                       ON w.period_id = :pid
                      AND w.platform = l.platform
                      AND w.shuffle_username = l.shuffle_username
                WHERE l.platform = :platform AND l.verified = TRUE
                ORDER BY 6 DESC
                """
            ),
            {
                "sid": args.server,
                "pid": period_id,
                "platform": args.platform,
                "start_date": start_date,
                "end_date": end_date,
            },
        ).fetchall()

    total_shortfall = 0
    plan = []
    for username, discord_id, kick_name, already, total_wager_usd, wagered in rows:
        wagered = float(wagered or 0)
        owed = int(wagered / dollars_per_ticket)
        shortfall = owed - int(already or 0)
        pending = round(wagered - owed * dollars_per_ticket, 2)
        new_paid_through = round(float(total_wager_usd or 0) - pending, 2)
        flag = "" if shortfall > 0 else "   (nothing owed)"
        print(
            f"  {username:<20} wagered=${wagered:>10,.2f}  owed={owed:>4}  "
            f"already={int(already or 0):>4}  shortfall={shortfall:>4}{flag}"
        )
        if shortfall > 0:
            total_shortfall += shortfall
            plan.append((username, discord_id, kick_name, shortfall, owed, new_paid_through))

    print(f"\ntotal shortfall: {total_shortfall} ticket(s) across {len(plan)} viewer(s)")
    if not plan:
        print("nothing to credit.")
        return
    if not args.apply:
        print("dry run — re-run with --apply to write.")
        return

    tm = TicketManager(engine, server_id=args.server)
    for username, discord_id, kick_name, shortfall, owed, new_paid_through in plan:
        ok = tm.award_tickets(
            discord_id=discord_id,
            # raffle_tickets.kick_name is NOT NULL; a Howl viewer who verified by
            # UID may not have a Kick account, so fall back to the wager handle.
            kick_name=kick_name or username,
            tickets=shortfall,
            source="shuffle_wager",
            description=f"Backfill: wager tickets lost to per-poll rounding (period {period_id})",
            period_id=period_id,
        )
        if not ok:
            print(f"  !! award failed for {username} — leaving its watermarks untouched")
            continue
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE raffle_shuffle_wagers
                    SET tickets_awarded = :owed,
                        last_known_wager = :paid_through,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE period_id = :pid AND shuffle_username = :username AND platform = :platform
                    """
                ),
                {
                    "owed": owed,
                    "paid_through": new_paid_through,
                    "pid": period_id,
                    "username": username,
                    "platform": args.platform,
                },
            )
        print(f"  credited {username}: +{shortfall} ticket(s)")

    print("\ndone.")


if __name__ == "__main__":
    main()
