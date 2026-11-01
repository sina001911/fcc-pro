"""HMAC-SHA256 signed artifact download links with expiry.

Tokens bind an ``artifact_id`` to an expiry time and are verified with a
constant-time comparison so that tampering cannot be distinguished from an
unknown artifact (no existence oracle).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SignedLink:
    """A minted, single-purpose download link."""

    token: str
    artifact_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class LinkCheck:
    """Result of validating a signed link."""

    link: SignedLink | None
    expired: bool = False


def mint_link(
    signing_key: bytes,
    artifact_id: str,
    *,
    ttl_seconds: int,
    now: float | None = None,
) -> SignedLink:
    """Mint a signed download link for one artifact."""
    current = time.time() if now is None else now
    expires_at = current + max(1, int(ttl_seconds))
    payload = f"{artifact_id}:{int(expires_at)}".encode("utf-8")
    signature = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(
        f"{signature}:{artifact_id}:{int(expires_at)}".encode("ascii")
    ).decode("ascii").rstrip("=")
    return SignedLink(token=token, artifact_id=artifact_id, expires_at=expires_at)


def validate_link(
    signing_key: bytes,
    token: str,
    artifact_id: str,
    *,
    now: float | None = None,
) -> LinkCheck:
    """Return the link if authentic and unexpired, otherwise a failed check.

    ``expired`` is set only when the token was authentic but past its expiry,
    so callers can distinguish a 410 Gone from a 404 Not Found.
    """
    if not token or not artifact_id:
        return LinkCheck(link=None)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except ValueError:
        # base64 errors (including binascii.Error) are ValueErrors.
        return LinkCheck(link=None)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return LinkCheck(link=None)
    parts = text.split(":")
    if len(parts) != 3:
        return LinkCheck(link=None)
    signature, link_id, expires_text = parts
    if link_id != artifact_id:
        return LinkCheck(link=None)
    try:
        expires_at = float(expires_text)
    except ValueError:
        return LinkCheck(link=None)
    expected = hmac.new(
        signing_key,
        f"{artifact_id}:{int(expires_at)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature.encode("ascii"), expected.encode("ascii")):
        return LinkCheck(link=None)
    current = time.time() if now is None else now
    if current >= expires_at:
        return LinkCheck(link=None, expired=True)
    return LinkCheck(link=SignedLink(token=token, artifact_id=artifact_id, expires_at=expires_at))


__all__ = ["SignedLink", "LinkCheck", "mint_link", "validate_link"]