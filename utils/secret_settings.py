"""Authenticated encryption for credentials at rest.

MIRRORED FILE — an identical copy lives at
`Kick-dicord-bot/utils/secret_settings.py`. Both services read and write the same
rows, so the envelope format and key handling must stay byte-compatible. Any
change here must be applied to BOTH copies in the same deploy.

Envelope format
---------------
    wlsec:v1:<fernet-token>

The version segment exists so a future format (different KDF, different cipher)
can be introduced without ambiguity about how to read old rows. Fernet gives us
authenticated encryption (AES-128-CBC + HMAC-SHA256), so a tampered ciphertext
fails loudly instead of decrypting to garbage.

Configuration
-------------
    SETTINGS_ENCRYPTION_KEY           primary Fernet key — used for ALL writes
    SETTINGS_ENCRYPTION_KEY_PREVIOUS  optional, comma-separated older keys,
                                      tried on read only (rotation window)
    SECRET_ENCRYPTION_WRITE_ENABLED   'true' to start writing ciphertext

Migration posture
-----------------
Reads accept plaintext for now, because the rows are plaintext until the
Release 3 migration runs. That tolerance is deliberately one-directional and
temporary:

  - A plaintext read logs a warning naming the KEY and row identifier — never the
    value — so the remaining plaintext rows are discoverable from logs.
  - Once `SECRET_ENCRYPTION_WRITE_ENABLED` is true, every write is ciphertext.
  - After the migration reports zero remaining plaintext rows, set
    `SECRET_ENCRYPTION_ALLOW_PLAINTEXT_READ=false` to make plaintext a hard
    error.

Failure posture — fails CLOSED
------------------------------
  - A value that IS encrypted but cannot be decrypted raises. It is never
    silently treated as "not configured", and we never fall back to a legacy
    plaintext source after a decryption failure: doing either would let a
    key mix-up look like an unconfigured integration and silently re-provision
    credentials.
  - A missing key while encrypted rows exist raises.
"""

import logging
import os

logger = logging.getLogger(__name__)

SECRET_PREFIX = "wlsec:"
SECRET_PREFIX_V1 = "wlsec:v1:"


class SecretConfigError(RuntimeError):
    """Encryption is required but not usable (missing/invalid key material)."""


class SecretDecryptError(RuntimeError):
    """A value is encrypted but could not be authenticated/decrypted."""


def _env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def write_enabled():
    """True when new writes should be encrypted.

    Defaults FALSE so deploying this code changes nothing until the key is
    configured on every service that reads these rows.
    """
    return _env_flag("SECRET_ENCRYPTION_WRITE_ENABLED", False)


def plaintext_read_allowed():
    """True while un-migrated plaintext rows may still be read."""
    return _env_flag("SECRET_ENCRYPTION_ALLOW_PLAINTEXT_READ", True)


def _load_keys():
    """Return [primary, *previous] Fernet keys. Empty list when unconfigured."""
    primary = (os.getenv("SETTINGS_ENCRYPTION_KEY") or "").strip()
    previous_raw = (os.getenv("SETTINGS_ENCRYPTION_KEY_PREVIOUS") or "").strip()
    keys = []
    if primary:
        keys.append(primary)
    for part in previous_raw.split(","):
        part = part.strip()
        if part:
            keys.append(part)
    return keys


def _fernets():
    """Build Fernet instances for every configured key, primary first."""
    from cryptography.fernet import Fernet

    out = []
    for raw in _load_keys():
        try:
            out.append(Fernet(raw.encode("utf-8") if isinstance(raw, str) else raw))
        except Exception as e:
            # Never log the key material itself.
            raise SecretConfigError(f"invalid Fernet key in encryption config: {type(e).__name__}") from e
    return out


def encryption_available():
    """True when at least one usable key is configured."""
    try:
        return bool(_fernets())
    except SecretConfigError:
        return False


def is_encrypted_secret(value):
    """True when `value` carries the encrypted envelope."""
    return isinstance(value, str) and value.startswith(SECRET_PREFIX)


def encrypt_secret(value):
    """Encrypt `value` with the PRIMARY key, returning the versioned envelope.

    Idempotent: an already-encrypted value is returned unchanged, so callers and
    migrations can run repeatedly without double-wrapping. When writes are
    disabled the plaintext is returned as-is (pre-migration behavior).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if is_encrypted_secret(value):
        return value
    if value == "":
        # Blank means "not configured" — keep it blank rather than storing a
        # ciphertext that decrypts to an empty string.
        return value
    if not write_enabled():
        return value

    fernets = _fernets()
    if not fernets:
        raise SecretConfigError("SECRET_ENCRYPTION_WRITE_ENABLED is true but SETTINGS_ENCRYPTION_KEY is not set")
    token = fernets[0].encrypt(value.encode("utf-8")).decode("ascii")
    return f"{SECRET_PREFIX_V1}{token}"


def decrypt_secret(value, key_name=None, row_id=None):
    """Decrypt an envelope, or pass plaintext through during migration.

    `key_name` / `row_id` are for diagnostics only and are safe to log; the
    VALUE is never logged, at any level, on any path.

    Raises SecretDecryptError when an encrypted value cannot be authenticated —
    callers must not interpret that as "no credential configured".
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if value == "":
        return value

    where = f"key={key_name or '?'} row={row_id if row_id is not None else '?'}"

    if not is_encrypted_secret(value):
        if not plaintext_read_allowed():
            raise SecretDecryptError(f"plaintext secret encountered after migration ({where})")
        # Value-free warning so remaining plaintext rows are discoverable.
        logger.warning(f"[secrets] reading UNENCRYPTED value ({where}); run the encryption migration")
        return value

    if not value.startswith(SECRET_PREFIX_V1):
        raise SecretDecryptError(f"unsupported secret envelope version ({where})")

    token = value[len(SECRET_PREFIX_V1) :].encode("ascii", errors="strict")

    fernets = _fernets()
    if not fernets:
        # Fail closed: encrypted data exists but this process cannot read it.
        raise SecretConfigError(f"encrypted secret found but no SETTINGS_ENCRYPTION_KEY configured ({where})")

    from cryptography.fernet import InvalidToken

    for idx, fernet in enumerate(fernets):
        try:
            return fernet.decrypt(token).decode("utf-8")
        except InvalidToken:
            continue  # try the next rotation key
        except Exception as e:
            raise SecretDecryptError(f"secret decryption error ({where}): {type(e).__name__}") from e

    # Wrong key, or tampered ciphertext. Never degrade to "not configured".
    raise SecretDecryptError(f"secret could not be decrypted with any configured key ({where})")


def assert_startup_readiness(has_encrypted_rows):
    """Fail fast at boot when encrypted rows exist but no key is configured.

    `has_encrypted_rows` is a callable returning bool, so the caller controls the
    (possibly expensive) query and we don't import a DB layer here.
    """
    if encryption_available():
        return
    try:
        present = bool(has_encrypted_rows())
    except Exception as e:
        logger.warning(f"[secrets] readiness check could not run: {type(e).__name__}: {e}")
        return
    if present:
        raise SecretConfigError(
            "encrypted credential rows exist but SETTINGS_ENCRYPTION_KEY is not configured on this service"
        )
