"""IPC boundary and restart regressions using synthetic credentials only."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistudio_api.domain.errors import TransportError
from aistudio_api.infrastructure.waa import daemon
from aistudio_api.infrastructure.waa.signer import LocalIpcProofSigner


class MemoryChild:
    """Controllable pipe peer for process exit and concurrent restart scenarios."""

    def __init__(self, *, hold_replies=False, wait_gate=None):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdout.feed_data(b'{"id":0,"event":"ready","ok":true}\n')
        self.pid = 12345
        self.returncode = None
        self.requests = []
        self.request_received = asyncio.Event()
        self.wait_started = asyncio.Event()
        self.wait_gate = wait_gate
        self.hold_replies = hold_replies
        self.stdin = MagicMock()
        self.stdin.write.side_effect = self.write
        self.stdin.drain = AsyncMock()

    def write(self, payload):
        req = json.loads(payload)
        self.requests.append(req)
        if req["method"] != "shutdown":
            self.request_received.set()
            if not self.hold_replies:
                self.reply(req["id"])

    def reply(self, req_id):
        self.stdout.feed_data(
            (
                json.dumps({"id": req_id, "ok": True, "proof": "!fixture"}) + "\n"
            ).encode()
        )

    async def wait(self):
        self.wait_started.set()
        if self.wait_gate is not None:
            await self.wait_gate.wait()
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        return 0


@pytest.fixture
def signer_binary(tmp_path):
    binary = tmp_path / "waa-signer"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o700)
    return binary


@pytest.mark.asyncio
async def test_old_pipe_reader_cannot_fail_new_generation(monkeypatch, signer_binary):
    first = MemoryChild()
    second = MemoryChild(hold_replies=True)
    launch = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    manager = daemon.LocalSignerProcess(signer_binary)
    try:
        await manager.health()
        # The OS may report exit before buffered stdout has reached EOF.
        first.returncode = 1
        restarted = asyncio.create_task(manager.health())
        await asyncio.wait_for(second.request_received.wait(), 1)
        # Deliver EOF late, while the replacement already has a pending request.
        first.stdout.feed_eof()
        await asyncio.sleep(0)
        second.reply(second.requests[0]["id"])
        assert (await restarted)["ok"] is True
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stop_finishes_before_new_process_starts(monkeypatch, signer_binary):
    gate = asyncio.Event()
    first = MemoryChild(wait_gate=gate)
    second = MemoryChild()
    launch = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    manager = daemon.LocalSignerProcess(signer_binary)
    await manager.health()
    stopped = asyncio.create_task(manager.stop())
    await first.wait_started.wait()
    restarted = asyncio.create_task(manager.health())
    try:
        # Give the competing coroutine a turn while old process.wait is blocked.
        await asyncio.sleep(0)
        assert launch.await_count == 1
        gate.set()
        await stopped
        assert (await restarted)["ok"] is True
    finally:
        gate.set()
        await asyncio.gather(stopped, restarted, return_exceptions=True)
        await manager.stop()


@pytest.mark.asyncio
async def test_cancelled_and_timed_out_requests_do_not_break_health(
    monkeypatch, signer_binary
):
    child = MemoryChild(hold_replies=True)
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=child)
    )
    manager = daemon.LocalSignerProcess(signer_binary)
    try:
        pending = asyncio.create_task(
            manager.request("proof.mint", {"cookies": "SID=fixture"})
        )
        await child.request_received.wait()
        cancelled_id = child.requests[-1]["id"]
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        with pytest.raises(TransportError, match="超时"):
            await manager.request("proof.mint", timeout=0.01)
        # Stale replies must never satisfy a later request.
        child.reply(cancelled_id)
        child.reply(child.requests[-1]["id"])
        child.hold_replies = False
        assert (await manager.health())["ok"] is True
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_timeout_also_bounds_a_blocked_pipe_write(monkeypatch, signer_binary):
    child = MemoryChild(hold_replies=True)
    blocked = asyncio.Event()
    child.stdin.drain = blocked.wait
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", AsyncMock(return_value=child)
    )
    manager = daemon.LocalSignerProcess(signer_binary)
    try:
        with pytest.raises(TransportError, match="超时"):
            await asyncio.wait_for(manager.request("proof.mint", timeout=0.01), 0.5)
        blocked.set()
        child.hold_replies = False
        assert (await manager.health())["ok"] is True
    finally:
        blocked.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_missing_local_binary_stops_startup_without_changing_config(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_keys: [fixture]\nwaa:\n  active_server: old_node\n")
    before = config_path.read_bytes()
    monkeypatch.setattr(daemon, "_repo_root", lambda: tmp_path)
    with pytest.raises(TransportError, match="未检测到本地"):
        await daemon.ensure_signer_ready()
    assert config_path.read_bytes() == before


@pytest.mark.asyncio
async def test_failed_local_process_stops_startup(monkeypatch, signer_binary):
    manager = daemon.LocalSignerProcess(signer_binary)
    monkeypatch.setattr(daemon, "is_signer_binary_ready", lambda: True)
    monkeypatch.setattr(daemon, "get_signer_process", lambda: manager)
    launch = AsyncMock(side_effect=OSError("fixture exec failure"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    with pytest.raises(TransportError, match="启动失败"):
        await daemon.ensure_signer_ready()
    launch.assert_awaited_once()
    assert not manager.is_running


@pytest.mark.asyncio
async def test_legacy_remote_url_and_proxy_cannot_redirect_signing(
    monkeypatch, signer_binary
):
    import httpx

    monkeypatch.setenv("AISTUDIO_PROOF_SERVER_URL", "https://untrusted.example")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    http_client = MagicMock(side_effect=AssertionError("signing must use child pipes"))
    monkeypatch.setattr(httpx, "AsyncClient", http_client)
    child = MemoryChild()
    launch = AsyncMock(return_value=child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    manager = daemon.LocalSignerProcess(signer_binary)
    signer = LocalIpcProofSigner(process=manager)
    try:
        assert await signer.mint_proof(cookies={"SAPISID": "fixture"}) == "!fixture"
        assert child.requests[0]["method"] == "proof.mint"
        assert "https://untrusted.example" not in launch.call_args.args
        http_client.assert_not_called()
    finally:
        await manager.stop()
