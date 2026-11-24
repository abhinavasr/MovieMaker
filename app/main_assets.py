"""A fingerprint of app/static, stamped onto asset URLs as ?v=... by every template set.

Cloudflare caches /static for 4 hours, so without it a deploy kept serving the
old editor script to browsers (2026-11-24).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

BASE = Path(__file__).parent


def _asset_version() -> str:
    """A fingerprint of /static, stamped onto asset URLs (?v=...). Cloudflare caches
    /static for 4 hours, so without it a deploy kept serving the old editor."""
    h = hashlib.sha1()
    for f in sorted((BASE / "static").glob("*")):
        if f.is_file():
            h.update(f.name.encode()); h.update(f.read_bytes())
    return h.hexdigest()[:10]


ASSET_V = _asset_version()

