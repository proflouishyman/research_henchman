"""Helpers for robust Playwright CDP endpoint handling."""

from __future__ import annotations

import socket
import urllib.parse


def effective_cdp_url(cdp_url: str) -> str:
    """Return a CDP URL variant that is reachable from Dockerized runtime.

    Non-obvious behavior:
    - Chrome DevTools can reject requests when the Host header is a hostname
      (for example `host.docker.internal`) instead of `localhost`/IP.
    - When configured with that hostname, resolve it to an IP so probe/connect
      requests from the container use an accepted Host header.
    """

    raw = str(cdp_url or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    host = str(parsed.hostname or "").strip().lower()
    if host != "host.docker.internal":
        return raw
    try:
        resolved_host = socket.gethostbyname(host)
    except OSError:
        return raw
    if not resolved_host:
        return raw

    auth_prefix = ""
    if parsed.username:
        auth_prefix = parsed.username
        if parsed.password:
            auth_prefix = f"{auth_prefix}:{parsed.password}"
        auth_prefix = f"{auth_prefix}@"
    netloc = f"{auth_prefix}{resolved_host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
