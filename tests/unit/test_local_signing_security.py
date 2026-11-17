"""Regression tests for the credential boundary around local proof signing."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.waa import daemon
from aistudio_api.infrastructure.waa.signer import (
    DEFAULT_PROOF_SERVER_URL,
    RemoteProofSigner,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://waa.spinsnow.fun",
        "http://127.0.0.1:8081.example.com",
        "http://127.0.0.1:8081@evil.example",
        "http://evil.example/127.0.0.1:8081",
        "http://localhost:8081",
        "http://127.0.0.1:8082",
        "https://127.0.0.1:8081",
        "http://127.0.0.1:8081/path",
        "http://127.0.0.1:8081?redirect=evil",
        "http://127.0.0.1:8081#fragment",
    ],
)
async def test_unsafe_target_is_rejected_even_with_cached_client(url: str):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "proof": "!unexpected"})

    signer = RemoteProofSigner()
    signer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # A live config edit must not reuse a cached client to bypass validation.
    signer.server_url = url
    try:
        with pytest.raises(TransportError, match="只允许本地签名"):
            await signer.mint_proof(cookies={"SID": "test-secret"}, prompt="private")
        assert requests == []
    finally:
        await signer.close()


@pytest.mark.asyncio
async def test_local_signer_does_not_follow_redirect(monkeypatch):
    requests: list[httpx.Request] = []
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://evil.example/mint"})

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setenv("HTTP_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://evil.example:3128")
    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    signer = RemoteProofSigner()
    try:
        with pytest.raises(TransportError, match="HTTP 307"):
            await signer.mint_proof(cookies={"SID": "test-secret"}, prompt="private")
        assert len(requests) == 1
        assert str(requests[0].url) == f"{DEFAULT_PROOF_SERVER_URL}/v1/proof/mint"
    finally:
        await signer.close()


def test_missing_local_binary_stops_startup_without_changing_config(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("waa:\n  active_server: local_spidermonkey\n")
    before = config_path.read_text()
    monkeypatch.setattr(daemon, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        daemon, "get_active_waa_server", lambda: {"url": DEFAULT_PROOF_SERVER_URL}
    )
    monkeypatch.setattr(daemon, "is_service_healthy", lambda _url: False)

    with pytest.raises(RuntimeError, match="已禁止远程回退"):
        daemon.ensure_signer_ready()
    assert config_path.read_text() == before


def test_remote_configuration_is_rejected_before_health_probe(monkeypatch):
    probe = MagicMock()
    monkeypatch.setattr(
        daemon, "get_active_waa_server", lambda: {"url": "https://waa.spinsnow.fun"}
    )
    monkeypatch.setattr(daemon, "is_service_healthy", probe)
    with pytest.raises(TransportError, match="只允许本地签名"):
        daemon.ensure_signer_ready()
    probe.assert_not_called()


def test_failed_local_process_stops_startup(monkeypatch, tmp_path):
    binary = tmp_path / "waa-signer" / "bin" / "waa-signer"
    binary.parent.mkdir(parents=True)
    binary.write_text("not executed")
    binary.chmod(0o700)
    monkeypatch.setattr(daemon, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(
        daemon, "get_active_waa_server", lambda: {"url": DEFAULT_PROOF_SERVER_URL}
    )
    monkeypatch.setattr(daemon, "is_service_healthy", lambda _url: False)
    monkeypatch.setattr(daemon.sys, "platform", "linux")
    launch = MagicMock(side_effect=OSError("cannot start signer"))
    monkeypatch.setattr(daemon.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="已禁止远程回退"):
        daemon.ensure_signer_ready()
    launch.assert_called_once()
