"""Stable identifier helpers for canonical trading-system records."""

from __future__ import annotations

import re
import hashlib
import secrets
import time
import uuid


_UUID7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TRADE_NUMBER_RE = re.compile(r"^(?:T|BT)[0-9]{6}$")
_RECORD_ID_RE = re.compile(r"^rec_[0-9a-f]{20,24}$")


def uuid7() -> uuid.UUID:
    """Return an RFC 9562 UUIDv7 on Python versions without ``uuid.uuid7``."""

    builtin = getattr(uuid, "uuid7", None)
    if callable(builtin):
        return builtin()

    timestamp_ms = time.time_ns() // 1_000_000
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= random_a << 64
    value |= 0b10 << 62
    value |= random_b
    return uuid.UUID(int=value)


def new_identifier(prefix: str) -> str:
    """Create a prefixed canonical identifier such as ``trd_<uuid7>``."""

    if prefix not in {"trd", "obs", "adj", "rev", "draft"}:
        raise ValueError(f"unsupported identifier prefix: {prefix}")
    return f"{prefix}_{uuid7()}"


def is_uuid7_identifier(value: object, prefix: str) -> bool:
    """Return whether *value* is exactly a lower-case, prefixed UUIDv7."""

    if not isinstance(value, str) or not value.startswith(prefix + "_"):
        return False
    raw = value[len(prefix) + 1 :]
    return bool(_UUID7_RE.fullmatch(raw))


def is_trade_number(value: object, trade_type: str | None = None) -> bool:
    """Validate the permanent human-readable number and its mode prefix."""

    if not isinstance(value, str) or not _TRADE_NUMBER_RE.fullmatch(value):
        return False
    if trade_type == "live":
        return value.startswith("T")
    if trade_type == "backtest":
        return value.startswith("BT")
    return False


def is_record_identifier(value: object) -> bool:
    """Validate the opaque normalized-record identity.

    ``src_*`` belongs to the immutable source manifest.  ``rec_*`` is the
    separately governed normalized source-candidate record layer.
    """

    return isinstance(value, str) and bool(_RECORD_ID_RE.fullmatch(value))


def normalized_record_identifier(repository: str, relative_path: str, content_sha256: str) -> str:
    """Derive the one deterministic ``rec_*`` identity for a source snapshot."""

    if not all(isinstance(value, str) and value for value in (repository, relative_path, content_sha256)):
        raise ValueError("repository, relative_path, and content_sha256 are required")
    payload = f"{repository}\0{relative_path}\0{content_sha256}".encode("utf-8")
    return "rec_" + hashlib.sha256(payload).hexdigest()[:20]
