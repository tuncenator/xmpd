"""Proxy URL builder."""

from __future__ import annotations


def build_proxy_url(
    provider: str,
    track_id: str,
    host: str = "localhost",
    port: int = 8080,
) -> str:
    """Return ``http://{host}:{port}/proxy/{provider}/{track_id}``."""
    return f"http://{host}:{port}/proxy/{provider}/{track_id}"
