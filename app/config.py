"""Deployment facts read from the environment, in one place.

Only what more than one module needs. The portal origin was read in two
places in app/web.py for the Auth0 redirect and logout, and the MCP had no
way to name it at all — so a minted link came back as
"https://<this service>/t/<token>", which an agent could neither open nor
hand to anyone.
"""

from __future__ import annotations

import os


def portal_base_url() -> str:
    """The portal's https origin, no trailing slash; empty when unset.

    ``PORTAL_BASE_URL`` is set by scripts/deploy.sh and is the same origin
    the Auth0 callback is registered against, so it is exactly the host that
    ``/t/<token>`` lives on. Empty locally unless exported.
    """
    return (os.environ.get("PORTAL_BASE_URL") or "").strip().rstrip("/")


def portal_link(token: str) -> str:
    """The full bookmarkable link for a token, or a placeholder that says
    what is missing rather than a string that looks like a URL."""
    base = portal_base_url()
    if base:
        return f"{base}/t/{token}"
    return f"https://<PORTAL_BASE_URL is not set on this service>/t/{token}"
