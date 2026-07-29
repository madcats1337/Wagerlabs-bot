"""S3-compatible object storage (Railway Buckets) — hand-kept mirror of the
dashboard's utils/object_storage.py. Keep the two in sync.

The bot uploads finished clips here; the dashboard mints the presigned URLs that
serve them.

Why this exists rather than writing to the `/data` volume:

* Railway bills **service** egress but not **bucket** egress. Serving a clip
  through Flask charges us for every viewer, and Discord's media proxy refetches
  per viewer — so video served from the app is the worst case. A presigned
  redirect hands the bytes out bucket->viewer for free.
* The volume is block storage bound to one service instance in one region. It
  caps horizontal scaling and has a fixed ceiling; the bucket is elastic at
  $0.015/GB-month.
* Direct browser->bucket uploads keep large request bodies out of gunicorn
  entirely, so upload size stops being a web-tier concern.

Everything degrades safely: when the bucket is not configured, `is_enabled()`
returns False and callers fall back to the volume. That lets this deploy before
the bucket exists, and keeps local dev working with no credentials.

Railway Buckets are PRIVATE — public buckets are not supported — so every read
goes through a presigned URL. Presigned URLs last at most 90 days.
"""

import logging
import os
import threading

logger = logging.getLogger(__name__)

# Railway injects these when a bucket's credentials are attached to the service.
# `BUCKET` is the globally-unique S3 name (display name + hash), which is what
# the S3 API wants — NOT RAILWAY_BUCKET_NAME.
_BUCKET = os.getenv("BUCKET") or os.getenv("S3_BUCKET") or ""
_ENDPOINT = os.getenv("ENDPOINT") or os.getenv("S3_ENDPOINT") or ""
_ACCESS_KEY = os.getenv("ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID") or ""
_SECRET_KEY = os.getenv("SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY") or ""
_REGION = os.getenv("REGION") or os.getenv("AWS_REGION") or "auto"

# "virtual" (bucket.endpoint/key) is what Railway Buckets use today. Buckets
# created before that change need "path" (endpoint/bucket/key); the bucket's
# Credentials tab states which one applies.
_ADDRESSING_STYLE = os.getenv("S3_ADDRESSING_STYLE", "virtual")

# How long a playback URL stays valid.
#
# These URLs are embedded directly in a clip's OpenGraph tags, because Discord's
# media proxy will NOT follow a cross-origin redirect (it fetches our stable
# /clips/file/ URL, receives the 302 to the bucket, and gives up — the embed then
# renders with no player). Since the signed URL is what Discord caches, it wants
# to outlive the message. Tigris accepts up to 90 days, verified against the live
# bucket; note AWS S3 proper caps SigV4 presigning at 7 days.
GET_URL_TTL_SECONDS = int(os.getenv("CLIP_URL_TTL_SECONDS", str(90 * 24 * 3600)))

# Upload links are handed straight to a browser that is about to use them.
PUT_URL_TTL_SECONDS = 3600

_client = None
_client_lock = threading.Lock()


def is_enabled():
    """True when the bucket is fully configured. Callers fall back to the volume.

    The endpoint is sanity-checked rather than merely non-empty: these values
    come from Railway variable references (``${{bucket.ENDPOINT}}``), and a
    mistyped reference is stored as that literal string. Without this check a
    typo would look configured, so serving would presign against a nonsense
    endpoint and 502 instead of falling back to the volume — turning a config
    typo into an outage for every existing clip.
    """
    if not (_BUCKET and _ENDPOINT and _ACCESS_KEY and _SECRET_KEY):
        return False
    if not _ENDPOINT.startswith(("http://", "https://")):
        logger.error(f"[ObjectStorage] ENDPOINT is not a URL ({_ENDPOINT!r}) - falling back to volume storage")
        return False
    return True


def bucket_name():
    return _BUCKET


def get_client():
    """Lazily build a boto3 S3 client. Thread-safe: gevent workers share this."""
    global _client
    if _client is not None:
        return _client
    if not is_enabled():
        return None
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config

            _client = boto3.client(
                "s3",
                endpoint_url=_ENDPOINT,
                aws_access_key_id=_ACCESS_KEY,
                aws_secret_access_key=_SECRET_KEY,
                region_name=_REGION,
                config=Config(
                    signature_version="s3v4",
                    # MUST be set explicitly. Against a custom endpoint_url boto3
                    # defaults to path-style, but Railway Buckets serve
                    # virtual-hosted-style (bucket as a subdomain of the
                    # endpoint), so the default silently produces URLs that do
                    # not resolve. Buckets created before Railway's switch want
                    # "path" — the bucket's Credentials tab says which.
                    s3={"addressing_style": _ADDRESSING_STYLE},
                    # Retries stay low so a bucket hiccup surfaces as an error
                    # instead of pinning a worker.
                    retries={"max_attempts": 3, "mode": "standard"},
                    connect_timeout=5,
                    read_timeout=20,
                ),
            )
    return _client


def presigned_get_url(key, ttl=None, filename=None):
    """A temporary direct-download URL for `key`, or None if unavailable.

    Bytes travel bucket -> client, bypassing this service, so this costs no
    service egress. `filename` sets a download name via response headers.
    """
    client = get_client()
    if client is None:
        return None
    params = {"Bucket": _BUCKET, "Key": key}
    if filename:
        params["ResponseContentDisposition"] = f'inline; filename="{filename}"'
    try:
        return client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=ttl or GET_URL_TTL_SECONDS,
        )
    except Exception as e:
        logger.error(f"[ObjectStorage] presign GET failed for {key}: {e}")
        return None


def presigned_upload(key, content_type, max_bytes, min_bytes=1024):
    """Presigned POST letting a browser upload straight to the bucket.

    The conditions are the security boundary — the browser can only write the
    exact key we chose (so one tenant cannot overwrite another's object), only
    the content type we allow, and only within the size range. Returns
    ``{"url": ..., "fields": {...}}`` or None.
    """
    client = get_client()
    if client is None:
        return None
    try:
        return client.generate_presigned_post(
            Bucket=_BUCKET,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                # boto3 adds the bucket condition itself; only the rest is ours.
                ["eq", "$key", key],
                ["eq", "$Content-Type", content_type],
                ["content-length-range", min_bytes, max_bytes],
            ],
            ExpiresIn=PUT_URL_TTL_SECONDS,
        )
    except Exception as e:
        logger.error(f"[ObjectStorage] presign POST failed for {key}: {e}")
        return None


def head_object(key):
    """Size/content-type for an object, or None when it does not exist.

    Used to confirm a browser upload actually landed before a database row is
    written — the client could otherwise claim an upload it never performed.
    """
    client = get_client()
    if client is None:
        return None
    try:
        resp = client.head_object(Bucket=_BUCKET, Key=key)
        return {
            "size": resp.get("ContentLength", 0),
            "content_type": resp.get("ContentType", ""),
        }
    except Exception:
        return None


def read_range(key, length=16):
    """First `length` bytes of an object, or b"" on failure.

    The server never sees a direct-to-bucket upload, so container sniffing has to
    happen after the fact. A ranged GET is a free API operation and a few bytes
    of egress.
    """
    client = get_client()
    if client is None:
        return b""
    try:
        resp = client.get_object(Bucket=_BUCKET, Key=key, Range=f"bytes=0-{length - 1}")
        return resp["Body"].read()
    except Exception as e:
        logger.warning(f"[ObjectStorage] range read failed for {key}: {e}")
        return b""


def put_bytes(key, data, content_type):
    """Upload bytes from this service. Incurs service egress — use sparingly.

    Fine for small artifacts (poster frames, migrated files); large media should
    go through `presigned_upload` so the bytes never touch this process.
    """
    client = get_client()
    if client is None:
        return False
    try:
        client.put_object(Bucket=_BUCKET, Key=key, Body=data, ContentType=content_type)
        return True
    except Exception as e:
        logger.error(f"[ObjectStorage] put failed for {key}: {e}")
        return False


def put_file(key, path, content_type):
    """Upload a file from disk, streamed rather than read into memory.

    Used for finished clips, which can be hundreds of megabytes — `put_bytes`
    would hold the whole thing in the process at once.
    """
    client = get_client()
    if client is None:
        return False
    try:
        client.upload_file(path, _BUCKET, key, ExtraArgs={"ContentType": content_type})
        return True
    except Exception as e:
        logger.error(f"[ObjectStorage] upload failed for {key}: {e}")
        return False


def download_file(key, path):
    """Stream an object to a local scratch path for background processing."""
    client = get_client()
    if client is None:
        return False
    try:
        client.download_file(_BUCKET, key, path)
        return True
    except Exception as e:
        logger.error(f"[ObjectStorage] download failed for {key}: {e}")
        return False


def delete_object(key):
    client = get_client()
    if client is None:
        return False
    try:
        client.delete_object(Bucket=_BUCKET, Key=key)
        return True
    except Exception as e:
        logger.warning(f"[ObjectStorage] delete failed for {key}: {e}")
        return False


def clip_key(filename):
    """Object key for a clip. Kept flat and prefix-scoped for easy lifecycle rules."""
    return f"clips/{filename}"
