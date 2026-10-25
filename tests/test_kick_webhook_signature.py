"""Kick webhook signatures against a key Kick can rotate.

Kick signs `message_id.timestamp.body` with its RSA key and publishes the
public half. The bot verified against a copy bundled in the code; Kick rotated
the key on 2026-10-01, every webhook failed verification after that, and no
go-live alert was posted for weeks. These pin that the published key is used,
that a failure fetches it again (but not on every bad request), and that the
bundled copy only stands in when Kick can't be reached.
"""

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import core.kick_webhooks as kw

MESSAGE_ID = "01M5FY9DV8WSRX0HGF5NHHKS4F"
TIMESTAMP = "2026-10-21T19:16:27Z"
BODY = b'{"broadcaster":{"username":"nickcleveland"},"is_live":true}'


def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, pem


def sign(private, body=BODY):
    message = f"{MESSAGE_ID}.{TIMESTAMP}.{body.decode()}".encode()
    return base64.b64encode(private.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()


def verify(signature, body=BODY):
    return kw.verify_kick_signature(body, signature, MESSAGE_ID, TIMESTAMP)


class Kick:
    """Kick's public-key endpoint, as the module fetches it, on a fake clock."""

    def __init__(self, monkeypatch, pem):
        self.pem = pem
        self.fetches = 0
        self.now = 1_000.0
        monkeypatch.setattr(kw, "_fetch_kick_public_key_pem", self._fetch)
        monkeypatch.setattr(kw.time, "monotonic", lambda: self.now)

    def _fetch(self):
        self.fetches += 1
        return self.pem


@pytest.fixture(autouse=True)
def no_cached_keys(monkeypatch):
    for name, value in (
        ("_bundled_public_key", None),
        ("_fetched_public_key", None),
        ("_fetched_pem", None),
        ("_last_fetch_attempt", None),
    ):
        monkeypatch.setattr(kw, name, value)


def test_a_webhook_signed_with_kicks_published_key_verifies(monkeypatch):
    private, pem = keypair()
    kick = Kick(monkeypatch, pem)
    assert verify(sign(private))
    assert kick.fetches == 1
    assert verify(sign(private)), "the fetched key is kept"
    assert kick.fetches == 1


def test_a_rotated_key_is_fetched_when_the_one_held_fails(monkeypatch):
    old, old_pem = keypair()
    new, new_pem = keypair()
    kick = Kick(monkeypatch, old_pem)
    assert verify(sign(old))

    kick.pem = new_pem  # Kick rotates
    kick.now += kw.KEY_REFETCH_SECONDS + 1
    assert verify(sign(new)), "the failure fetched Kick's new key and retried"
    assert kick.fetches == 2


def test_bad_signatures_fetch_the_key_at_most_once_per_window(monkeypatch):
    private, pem = keypair()
    stranger, _ = keypair()
    kick = Kick(monkeypatch, pem)
    assert verify(sign(private))
    kick.now += kw.KEY_REFETCH_SECONDS + 1

    for _ in range(5):
        assert not verify(sign(stranger))
    assert kick.fetches == 2, "one refetch for the burst, not one per forged request"

    kick.now += kw.KEY_REFETCH_SECONDS + 1
    assert not verify(sign(stranger))
    assert kick.fetches == 3


def test_the_bundled_key_stands_in_when_kick_cannot_be_reached(monkeypatch):
    private, pem = keypair()
    Kick(monkeypatch, None)
    monkeypatch.setattr(kw, "KICK_PUBLIC_KEY", pem)
    assert verify(sign(private))


def test_a_tampered_body_is_refused(monkeypatch):
    private, pem = keypair()
    Kick(monkeypatch, pem)
    signature = sign(private)
    assert not verify(signature, body=BODY.replace(b"true", b"false"))


def test_a_missing_or_garbled_signature_is_refused_without_fetching(monkeypatch):
    _, pem = keypair()
    kick = Kick(monkeypatch, pem)
    assert not verify("")
    assert not kw.verify_kick_signature(BODY, "c2ln", None, TIMESTAMP)
    assert kick.fetches == 0


def test_the_bundled_key_parses():
    assert kw._load_pem(kw.KICK_PUBLIC_KEY).key_size == 2048
