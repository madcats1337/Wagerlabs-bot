"""Provably fair engine for the gambling games (commit-reveal).

This mirrors the raffle draw's guarantee (``raffle_system/draw.py``) rather than
the older "generate a seed at play time and reveal it in the same breath" scheme
it replaces. That older scheme proved nothing: a seed minted after the bet was
known could have been ground for a losing outcome, and revealing it afterwards
was unfalsifiable.

The commit-reveal model, as the raffle uses it:

  * the server seed is generated UP FRONT and only its SHA-256 **commitment** is
    published, before any outcome is known;
  * the outcome hash is ``SHA256(server_seed:client_seed:nonce)``, exactly the
    concatenation the raffle uses;
  * the seed is revealed later, and anyone can check ``SHA256(seed) ==
    commitment`` to prove the operator never swapped it.

Gambling needs a per-user shape of that, because bets are continuous while a
raffle period is a single event. So each player holds an **active seed pair**:

  * ``server_seed``      — secret until rotated, committed the moment it is issued
  * ``server_seed_commitment`` — SHA256(server_seed), shown to the player BEFORE
    they bet
  * ``client_seed``      — player-chosen and editable, so the player contributes
    entropy the operator cannot predict
  * ``nonce``            — increments once per bet, making every bet a distinct hash

Rotating a seed reveals the retired server seed, at which point every bet played
under it becomes independently verifiable. This is the standard construction used
by the major casinos, and it is the only arrangement where the pre-published
commitment actually constrains the operator.
"""

import hashlib
import logging
import secrets
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Default client seed handed to a player who has never set one. Players may
# replace it at will; the default only has to be non-empty and per-player.
DEFAULT_CLIENT_SEED_BYTES = 8


def commit_seed(server_seed: str) -> str:
    """SHA256 of a server seed - the value published before any bet is placed."""
    return hashlib.sha256(server_seed.encode()).hexdigest()


def generate_server_seed() -> str:
    """A fresh 64-char hex server seed (same width as the raffle's)."""
    return secrets.token_hex(32)


def build_proof_hash(server_seed: str, client_seed: str, nonce) -> str:
    """The canonical outcome hash: ``SHA256(server_seed:client_seed:nonce)``.

    Identical construction to ``raffle_system/draw.py`` so a single verifier -
    including the in-browser one on the public provably-fair page - covers both.
    """
    return hashlib.sha256(f"{server_seed}:{client_seed}:{nonce}".encode()).hexdigest()


def hash_to_random_value(proof_hash: str) -> float:
    """Reduce a proof hash to a 0.00-99.99 roll value.

    Takes the first 8 hex chars as an integer and scales it, matching the
    slot-reward helper in ``utils/provably_fair.py``.
    """
    random_int = int(proof_hash[:8], 16)
    return round((random_int % 10000) / 100.0, 2)


def hash_to_int(proof_hash: str, offset: int = 0, width: int = 8) -> int:
    """An unbiased integer drawn from a slice of the proof hash.

    ``offset`` lets one hash yield several independent integers, and mirrors how
    the raffle takes ``proof_hash[:16]``.
    """
    chunk = proof_hash[offset : offset + width]
    return int(chunk, 16)


# ---------------------------------------------------------------------------
# Seed-pair lifecycle (per player, per server)
# ---------------------------------------------------------------------------


def get_or_create_active_seed(engine, guild_id: int, discord_id: int) -> Dict[str, Any]:
    """Return this player's active seed pair, minting one if they have none.

    The row is created with its commitment already stored, so a seed can never
    exist without a published commitment - the property the whole scheme rests
    on. Returns the full row including ``server_seed``; callers that show data to
    the player must send only the commitment, never the live seed.
    """
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, server_seed, server_seed_commitment, client_seed, nonce
                FROM gambling_seeds
                WHERE discord_server_id = :sid AND discord_id = :did AND is_active = TRUE
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"sid": guild_id, "did": discord_id},
        ).fetchone()

        if row:
            return {
                "id": row[0],
                "server_seed": row[1],
                "server_seed_commitment": row[2],
                "client_seed": row[3],
                "nonce": int(row[4]),
            }

        server_seed = generate_server_seed()
        commitment = commit_seed(server_seed)
        client_seed = secrets.token_hex(DEFAULT_CLIENT_SEED_BYTES)

        new_id = conn.execute(
            text(
                """
                INSERT INTO gambling_seeds
                    (discord_server_id, discord_id, server_seed, server_seed_commitment,
                     client_seed, nonce, is_active)
                VALUES (:sid, :did, :ss, :commit, :cs, 0, TRUE)
                RETURNING id
                """
            ),
            {
                "sid": guild_id,
                "did": discord_id,
                "ss": server_seed,
                "commit": commitment,
                "cs": client_seed,
            },
        ).scalar()

        logger.info(
            "Issued gambling seed #%s for user %s (commitment %s...)",
            new_id,
            discord_id,
            commitment[:16],
        )
        return {
            "id": new_id,
            "server_seed": server_seed,
            "server_seed_commitment": commitment,
            "client_seed": client_seed,
            "nonce": 0,
        }


def next_nonce(engine, seed_id: int) -> int:
    """Atomically claim the next nonce for a seed pair.

    Incrementing inside the UPDATE (rather than read-then-write) keeps two
    concurrent bets from sharing a nonce, which would make two different bets
    hash identically and break verification for both.
    """
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    """
                    UPDATE gambling_seeds
                    SET nonce = nonce + 1
                    WHERE id = :id
                    RETURNING nonce
                    """
                ),
                {"id": seed_id},
            ).scalar()
        )


def set_client_seed(engine, guild_id: int, discord_id: int, client_seed: str) -> Dict[str, Any]:
    """Point a player's active seed pair at a new client seed.

    Changing the client seed does NOT reset the nonce: the (seed, client, nonce)
    triple stays unique either way, and resetting would let a player replay a
    nonce against the same server seed.
    """
    seed = get_or_create_active_seed(engine, guild_id, discord_id)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE gambling_seeds SET client_seed = :cs WHERE id = :id"),
            {"cs": client_seed, "id": seed["id"]},
        )
    seed["client_seed"] = client_seed
    return seed


def rotate_seed(engine, guild_id: int, discord_id: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Retire the active seed pair (revealing its seed) and issue a fresh one.

    This is the reveal half of commit-reveal. Until rotation the server seed is
    withheld, so outcomes are unpredictable to the player; after rotation the
    retired seed is public and every bet recorded against it can be recomputed and
    checked against the commitment that was published before those bets ran.

    Returns ``(retired_seed, new_active_seed)``. The retired pair's ``nonce`` is
    the number of bets played under it, so a caller can tell the player whether
    the revealed seed actually covers anything.
    """
    current = get_or_create_active_seed(engine, guild_id, discord_id)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE gambling_seeds
                SET is_active = FALSE, revealed_at = CURRENT_TIMESTAMP
                WHERE id = :id
                """
            ),
            {"id": current["id"]},
        )

    fresh = get_or_create_active_seed(engine, guild_id, discord_id)
    logger.info(
        "Rotated gambling seed for user %s: revealed #%s, issued #%s",
        discord_id,
        current["id"],
        fresh["id"],
    )
    return current, fresh


def get_revealed_seed(engine, guild_id: int, discord_id: int, seed_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a retired (revealed) seed pair. Returns None while it is still active."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT id, server_seed, server_seed_commitment, client_seed, nonce, revealed_at
                FROM gambling_seeds
                WHERE id = :id AND discord_server_id = :sid AND discord_id = :did
                  AND is_active = FALSE
                """
            ),
            {"id": seed_id, "sid": guild_id, "did": discord_id},
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "server_seed": row[1],
        "server_seed_commitment": row[2],
        "client_seed": row[3],
        "nonce": int(row[4]),
        "revealed_at": row[5],
    }


# ---------------------------------------------------------------------------
# Per-bet outcome derivation
# ---------------------------------------------------------------------------


def roll_bet(engine, guild_id: int, discord_id: int) -> Dict[str, Any]:
    """Claim the next nonce on the player's active pair and derive its outcome.

    Returns everything a game needs to resolve itself and everything the history
    row needs to be verifiable later - but note ``server_seed`` is included for
    the resolver's use only and must not be shown to the player before rotation.
    """
    seed = get_or_create_active_seed(engine, guild_id, discord_id)
    nonce = next_nonce(engine, seed["id"])

    proof_hash = build_proof_hash(seed["server_seed"], seed["client_seed"], nonce)

    return {
        "seed_id": seed["id"],
        "server_seed": seed["server_seed"],
        "server_seed_commitment": seed["server_seed_commitment"],
        "client_seed": seed["client_seed"],
        "nonce": str(nonce),
        "proof_hash": proof_hash,
        "random_value": hash_to_random_value(proof_hash),
    }


def generate_deck_shuffle(server_seed: str, client_seed: str, nonce) -> List[int]:
    """Deterministic 52-card shuffle bound to one specific bet.

    Uses a Fisher-Yates shuffle driven by a hash chain rooted at this bet's
    ``(server_seed, client_seed, nonce)``. Fisher-Yates replaces the old
    "sort 52 cards by their hash" approach, which was biased - sorting hex strings
    is not a uniform permutation - and which ignored the nonce entirely, so every
    bet on one seed pair dealt the SAME deck.

    Card mapping is unchanged: ``index // 4`` = rank (0=A ... 12=K), ``index % 4``
    = suit.
    """
    deck = list(range(52))

    # Hash chain: each block yields 16 four-hex-char draws. Deriving the stream
    # from the bet's own nonce is what makes each bet's deck distinct.
    stream: List[int] = []

    def _extend(counter_label) -> None:
        block = hashlib.sha256(f"{server_seed}:{client_seed}:{nonce}:{counter_label}".encode()).hexdigest()
        for i in range(0, 64, 4):
            stream.append(int(block[i : i + 4], 16))

    counter = 0
    while len(stream) < 64:
        _extend(counter)
        counter += 1

    # Fisher-Yates from the top down, rejecting draws in the ragged tail of the
    # 16-bit range so the modulo stays uniform.
    cursor = 0
    for i in range(51, 0, -1):
        limit = i + 1
        bound = (65536 // limit) * limit
        while True:
            if cursor >= len(stream):
                _extend(counter)
                counter += 1
            draw = stream[cursor]
            cursor += 1
            if draw < bound:
                break
        j = draw % limit
        deck[i], deck[j] = deck[j], deck[i]

    return deck


# ---------------------------------------------------------------------------
# Verification (used by tests and by the bot's own explainers)
# ---------------------------------------------------------------------------


def verify_commitment(server_seed: str, commitment: str) -> bool:
    """Check a revealed seed against the commitment published before the bets."""
    return commit_seed(server_seed) == (commitment or "").lower()


def verify_bet(server_seed: str, client_seed: str, nonce, expected_hash: str) -> bool:
    """Recompute a bet's proof hash and compare it with the recorded one."""
    return build_proof_hash(server_seed, client_seed, nonce) == expected_hash


def verify_deck_shuffle(server_seed: str, client_seed: str, nonce, expected_deck: List[int]) -> bool:
    """Recompute a bet's deck and compare it with the recorded one."""
    return generate_deck_shuffle(server_seed, client_seed, nonce) == expected_deck
