"""Tests for CDP endpoint normalization helpers."""

from __future__ import annotations

from adapters.cdp_utils import effective_cdp_url


def test_effective_cdp_url_rewrites_host_docker_internal_to_ip(monkeypatch) -> None:
    monkeypatch.setattr("adapters.cdp_utils.socket.gethostbyname", lambda _host: "192.168.65.1")

    out = effective_cdp_url("http://host.docker.internal:9222")

    assert out == "http://192.168.65.1:9222"


def test_effective_cdp_url_keeps_non_docker_hostname() -> None:
    out = effective_cdp_url("http://127.0.0.1:9222")

    assert out == "http://127.0.0.1:9222"
