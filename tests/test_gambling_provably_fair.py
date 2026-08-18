"""Tests for the gambling commit-reveal provably-fair engine.

These pin the properties the scheme's guarantee actually rests on:
  * the outcome hash is the SAME construction the raffle uses, so one verifier
    covers both and the published formula is honest;
  * a commitment identifies exactly one seed;
  * every bet on a seed pair gets a distinct deck (the bug in the scheme this
    replaced, where one seed dealt one deck forever);
  * the shuffle is a uniform permutation, not merely a deterministic one.
"""

import hashlib
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from features.games.gambling.provably_fair_gambling import (  # noqa: E402
    build_proof_hash,
    commit_seed,
    generate_deck_shuffle,
    generate_server_seed,
    hash_to_random_value,
    verify_bet,
    verify_commitment,
    verify_deck_shuffle,
)
from features.games.gambling.roll import ROLL_BRACKETS, ROLL_TABLE, get_roll_multiplier  # noqa: E402

SEED = "a" * 64
CLIENT = "client-seed"


def test_proof_hash_matches_raffle_construction():
    """The bet hash must be byte-identical to raffle_system/draw.py's."""
    expected = hashlib.sha256(f"{SEED}:{CLIENT}:7".encode()).hexdigest()
    assert build_proof_hash(SEED, CLIENT, 7) == expected
    assert build_proof_hash(SEED, CLIENT, "7") == expected


def test_commitment_round_trip():
    seed = generate_server_seed()
    assert len(seed) == 64
    assert verify_commitment(seed, commit_seed(seed))


def test_commitment_rejects_a_different_seed():
    """A commitment must not validate any seed but its own - otherwise the
    operator could swap the seed after the fact and still 'prove' fairness."""
    a, b = generate_server_seed(), generate_server_seed()
    assert not verify_commitment(a, commit_seed(b))


def test_verify_bet_detects_tampering():
    real = build_proof_hash(SEED, CLIENT, 3)
    assert verify_bet(SEED, CLIENT, 3, real)
    # Any change to any input must break verification.
    assert not verify_bet(SEED, CLIENT, 4, real)
    assert not verify_bet(SEED, "other", 3, real)
    assert not verify_bet("b" * 64, CLIENT, 3, real)


def test_random_value_in_range():
    for nonce in range(200):
        value = hash_to_random_value(build_proof_hash(SEED, CLIENT, nonce))
        assert 0.0 <= value <= 99.99


def test_deck_is_a_permutation():
    deck = generate_deck_shuffle(SEED, CLIENT, 0)
    assert sorted(deck) == list(range(52))


def test_deck_differs_per_nonce():
    """Each bet must deal its own deck.

    The previous implementation derived the deck from (server, client) only, so
    every hand on one seed pair dealt identical cards - a player could see their
    next hand by playing one.
    """
    decks = {tuple(generate_deck_shuffle(SEED, CLIENT, n)) for n in range(50)}
    assert len(decks) == 50


def test_deck_verification_round_trip():
    deck = generate_deck_shuffle(SEED, CLIENT, 11)
    assert verify_deck_shuffle(SEED, CLIENT, 11, deck)
    assert not verify_deck_shuffle(SEED, CLIENT, 12, deck)


def test_deck_shuffle_is_uniform():
    """Every card must reach every position about equally often.

    Guards the modulo-rejection in the Fisher-Yates loop: dropping it biases
    low positions, which a determinism-only test would not catch.
    """
    trials = 5200
    positions = Counter(generate_deck_shuffle(SEED, CLIENT, n).index(0) for n in range(trials))
    expected = trials / 52
    assert len(positions) == 52
    # Generous band: this asserts "not biased", not an exact distribution.
    for count in positions.values():
        assert 0.55 * expected < count < 1.45 * expected


def test_roll_table_matches_brackets():
    """`/roll info` must describe the payouts the game actually pays.

    ROLL_TABLE is hand-written prose; ROLL_BRACKETS is the logic. This walks all
    100 rolls and asserts every one falls in the table row claiming it.
    """

    def row_for(roll: int):
        if roll in (1, 100):
            return ROLL_TABLE[0]
        if roll <= 5 or roll >= 96:
            return ROLL_TABLE[1]
        if roll <= 15 or roll >= 86:
            return ROLL_TABLE[2]
        if roll <= 25 or roll >= 76:
            return ROLL_TABLE[3]
        if roll <= 39 or roll >= 62:
            return ROLL_TABLE[4]
        return ROLL_TABLE[5]

    for roll in range(1, 101):
        multiplier, _label = get_roll_multiplier(roll)
        assert multiplier == row_for(roll)[1], f"roll {roll} pays {multiplier}, table says {row_for(roll)[1]}"

    # And the table must not describe payouts the brackets never produce.
    assert {m for _, m, _ in ROLL_TABLE} == {m for _, m, _ in ROLL_BRACKETS}
