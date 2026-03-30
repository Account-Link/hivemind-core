"""Test that S3 uploads are deferred until after mediator runs.

Covers three layers:
  - Bridge: buffering, validation, auth
  - Backend: pending_uploads in return tuples
  - Pipeline (run_query_agent_tracked): post-mediator upload, placeholder
    replacement, failure handling
"""
import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

import hivemind.pipeline as pipeline_module
from hivemind.sandbox.bridge import BridgeServer
from hivemind.sandbox.budget import Budget
from hivemind.sandbox.models import AgentConfig, SandboxSettings
from hivemind.tools import Tool


# ── Shared helpers ──


def _make_tools():
    return [
        Tool(
            name="get_schema",
            description="Get schema",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda: "[]",
        ),
    ]


async def _mock_llm_caller(messages, max_tokens, **kwargs):
    return {
        "content": "LLM response",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "finish_reason": "stop",
    }


async def _mock_on_tool_call(name, args):
    return "[]"


def _bridge_with_s3(run_id="test-run-123", **overrides):
    """Create a BridgeServer pre-configured with S3 uploader and run_store."""
    defaults = dict(
        session_token="test-token",
        tools=_make_tools(),
        on_tool_call=_mock_on_tool_call,
        llm_caller=_mock_llm_caller,
        budget=Budget(max_calls=10, max_tokens=100_000),
        host="127.0.0.1",
        s3_uploader=MagicMock(),
        run_id=run_id,
        run_store=MagicMock(),
    )
    defaults.update(overrides)
    return BridgeServer(**defaults)


# ═══════════════════════════════════════════════════════════════════════
# 1. Bridge-layer tests
# ═══════════════════════════════════════════════════════════════════════


class TestBridgeS3Buffering:
    """Verify that the bridge /sandbox/s3-upload endpoint buffers correctly."""

    @pytest.mark.asyncio
    async def test_upload_buffered_not_executed(self):
        """S3 upload should be buffered, NOT actually uploaded."""
        mock_s3 = MagicMock()
        mock_store = MagicMock()
        server = _bridge_with_s3(s3_uploader=mock_s3, run_store=mock_store)
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer test-token"}
            payload = b"hello world report data"
            resp = await client.post(
                "/sandbox/s3-upload",
                headers=headers,
                json={
                    "filename": "report.json",
                    "content_base64": base64.b64encode(payload).decode(),
                    "content_type": "application/json",
                },
            )
            assert resp.status_code == 200
            data = resp.json()

            # Placeholder URL returned
            assert data["s3_url"].startswith("s3://pending/")
            assert "test-run-123/report.json" in data["s3_url"]

            # Real S3 was NOT called
            mock_s3.upload_bytes.assert_not_called()
            # run_store was NOT updated
            mock_store.update_status.assert_not_called()

            # Buffered correctly
            assert len(server.pending_s3_uploads) == 1
            upload = server.pending_s3_uploads[0]
            assert upload["key"] == "test-run-123/report.json"
            assert upload["data"] == payload
            assert upload["content_type"] == "application/json"
            assert upload["placeholder_url"] == data["s3_url"]
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_multiple_uploads_all_buffered(self):
        """Multiple S3 upload calls should all be buffered in order."""
        server = _bridge_with_s3(run_id="run-multi")
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer test-token"}
            for i in range(3):
                resp = await client.post(
                    "/sandbox/s3-upload",
                    headers=headers,
                    json={
                        "filename": f"file{i}.txt",
                        "content_base64": base64.b64encode(
                            f"content{i}".encode()
                        ).decode(),
                    },
                )
                assert resp.status_code == 200

            assert len(server.pending_s3_uploads) == 3
            keys = [u["key"] for u in server.pending_s3_uploads]
            assert keys == [
                "run-multi/file0.txt",
                "run-multi/file1.txt",
                "run-multi/file2.txt",
            ]
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_endpoint_not_registered_without_uploader(self):
        """Bridge without s3_uploader should NOT expose the endpoint."""
        server = BridgeServer(
            session_token="tok",
            tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
            llm_caller=_mock_llm_caller,
            budget=Budget(max_calls=10, max_tokens=100_000),
            host="127.0.0.1",
        )
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer tok"}
            resp = await client.post(
                "/sandbox/s3-upload",
                headers=headers,
                json={
                    "filename": "test.txt",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code in (404, 405)
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_invalid_base64_returns_400(self):
        """Invalid base64 content should be rejected with 400."""
        server = _bridge_with_s3()
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer test-token"}
            resp = await client.post(
                "/sandbox/s3-upload",
                headers=headers,
                json={
                    "filename": "bad.bin",
                    "content_base64": "!!!not-valid-base64!!!",
                },
            )
            assert resp.status_code == 400
            assert len(server.pending_s3_uploads) == 0
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_auth_required(self):
        """S3 upload endpoint should require authentication."""
        server = _bridge_with_s3()
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            resp = await client.post(
                "/sandbox/s3-upload",
                json={
                    "filename": "test.txt",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code == 401

            resp = await client.post(
                "/sandbox/s3-upload",
                headers={"Authorization": "Bearer wrong-token"},
                json={
                    "filename": "test.txt",
                    "content_base64": base64.b64encode(b"data").decode(),
                },
            )
            assert resp.status_code == 401

            assert len(server.pending_s3_uploads) == 0
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_binary_content_buffered_correctly(self):
        """Binary (non-text) content should be buffered byte-for-byte."""
        server = _bridge_with_s3()
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer test-token"}
            binary_data = bytes(range(256)) * 10  # 2560 bytes of all byte values
            resp = await client.post(
                "/sandbox/s3-upload",
                headers=headers,
                json={
                    "filename": "image.png",
                    "content_base64": base64.b64encode(binary_data).decode(),
                    "content_type": "image/png",
                },
            )
            assert resp.status_code == 200
            upload = server.pending_s3_uploads[0]
            assert upload["data"] == binary_data
            assert upload["content_type"] == "image/png"
        finally:
            await client.aclose()
            await server.stop()

    @pytest.mark.asyncio
    async def test_each_upload_gets_unique_placeholder(self):
        """Each upload should get a unique placeholder URL based on filename."""
        server = _bridge_with_s3(run_id="run-x")
        port = await server.start()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")

        try:
            headers = {"Authorization": "Bearer test-token"}
            urls = []
            for name in ["a.json", "b.csv", "sub/c.txt"]:
                resp = await client.post(
                    "/sandbox/s3-upload",
                    headers=headers,
                    json={
                        "filename": name,
                        "content_base64": base64.b64encode(b"x").decode(),
                    },
                )
                urls.append(resp.json()["s3_url"])

            # All unique
            assert len(set(urls)) == 3
            assert "run-x/a.json" in urls[0]
            assert "run-x/b.csv" in urls[1]
            assert "run-x/sub/c.txt" in urls[2]
        finally:
            await client.aclose()
            await server.stop()


# ═══════════════════════════════════════════════════════════════════════
# 2. Backend-layer tests
# ═══════════════════════════════════════════════════════════════════════


class TestBackendPendingUploads:
    """Verify backend.run() returns pending_s3_uploads in all return shapes."""

    @pytest.fixture(autouse=True)
    def _patch(self, monkeypatch):
        """Patch runner and bridge for all tests in this class."""
        import hivemind.sandbox.backend as backend_module

        class _Runner:
            def __init__(self, settings):
                pass

            async def run_agent(self, **kwargs):
                from hivemind.sandbox.docker_runner import ContainerResult

                return ContainerResult(
                    stdout="agent output\n",
                    stderr="",
                    exit_code=0,
                    timed_out=False,
                )

        self._bridge_instance = None

        class _Bridge:
            def __init__(self_, *args, **kwargs):
                self_.pending_s3_uploads = [
                    {"key": "run/file.json", "data": b"data",
                     "content_type": "application/json",
                     "placeholder_url": "s3://pending/run/file.json"},
                ]
                self._bridge_instance = self_

            async def start(self) -> int:
                return 9999

            async def stop(self):
                pass

            def get_recorded_tape(self):
                return [{"entry": "tape"}]

        monkeypatch.setattr(backend_module, "_create_runner", lambda s: _Runner(s))
        monkeypatch.setattr(backend_module, "BridgeServer", _Bridge)

        self._backend = backend_module.SandboxBackend(
            llm_client=AsyncMock(),
            llm_model="model",
            settings=SandboxSettings(
                bridge_host="127.0.0.1",
                docker_network_name="test",
                container_memory_mb=256,
                container_cpu_quota=1.0,
                global_max_llm_calls=50,
                global_max_tokens=200_000,
                global_timeout_seconds=300,
            ),
            agent=AgentConfig(
                agent_id="qa-1", name="Q", image="img:test",
            ),
        )

    @pytest.mark.asyncio
    async def test_default_return(self):
        """No flags: (output, pending_uploads)."""
        output, uploads = await self._backend.run(
            role="query", env={}, tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
        )
        assert output == "agent output"
        assert len(uploads) == 1
        assert uploads[0]["key"] == "run/file.json"

    @pytest.mark.asyncio
    async def test_with_budget_summary(self):
        """return_budget_summary: (output, usage, pending_uploads)."""
        output, usage, uploads = await self._backend.run(
            role="query", env={}, tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
            return_budget_summary=True,
        )
        assert output == "agent output"
        assert "total_tokens" in usage
        assert len(uploads) == 1

    @pytest.mark.asyncio
    async def test_with_tape(self):
        """return_tape: (output, tape, pending_uploads)."""
        output, tape, uploads = await self._backend.run(
            role="query", env={}, tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
            return_tape=True,
        )
        assert output == "agent output"
        assert tape == [{"entry": "tape"}]
        assert len(uploads) == 1

    @pytest.mark.asyncio
    async def test_with_budget_and_tape(self):
        """Both flags: (output, usage, tape, pending_uploads)."""
        output, usage, tape, uploads = await self._backend.run(
            role="query", env={}, tools=_make_tools(),
            on_tool_call=_mock_on_tool_call,
            return_budget_summary=True,
            return_tape=True,
        )
        assert output == "agent output"
        assert "total_tokens" in usage
        assert tape == [{"entry": "tape"}]
        assert len(uploads) == 1


# ═══════════════════════════════════════════════════════════════════════
# 3. Pipeline-layer tests (run_query_agent_tracked)
# ═══════════════════════════════════════════════════════════════════════


def _make_mock_run_store():
    """Create a mock run_store with a simple in-memory state."""
    store = MagicMock()
    state = {"status": "pending", "s3_url": None, "output": None}

    def update_status(run_id, status, **kwargs):
        state["status"] = status
        state.update(kwargs)

    def get(run_id):
        return dict(state)

    store.update_status = MagicMock(side_effect=update_status)
    store.update_stage = MagicMock()
    store.get = MagicMock(side_effect=lambda rid: dict(state))
    store.create = MagicMock()
    store._state = state
    return store


def _make_test_pipeline(agents: dict[str, AgentConfig] | None = None):
    """Create a Pipeline with a mock DB and a fake in-memory AgentStore."""
    from hivemind.config import Settings
    from hivemind.pipeline import Pipeline

    mock_db = MagicMock()

    # Build a lightweight AgentStore that works without Postgres
    agent_registry = dict(agents or {})
    mock_agent_store = MagicMock()
    mock_agent_store.get = MagicMock(side_effect=lambda aid: agent_registry.get(aid))
    mock_agent_store.create = MagicMock(
        side_effect=lambda cfg: agent_registry.__setitem__(cfg.agent_id, cfg)
    )

    settings = Settings(database_url="unused", llm_api_key="test")
    return Pipeline(settings, mock_db, mock_agent_store)


class TestPipelineS3DeferredUpload:
    """Pipeline-level tests for deferred S3 upload in run_query_agent_tracked."""

    @pytest.mark.asyncio
    async def test_placeholder_replaced_with_real_url(self):
        """Output should have placeholder URLs replaced with real S3 URLs."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-s3", name="Q", image="img:test",
        ))

        placeholder = "s3://pending/run-1/report.json"
        pending = [{
            "key": "run-1/report.json",
            "data": b'{"result": "ok"}',
            "content_type": "application/json",
            "placeholder_url": placeholder,
        }]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                # Agent output references the placeholder URL
                output = f"Report uploaded to {placeholder}"
                return output, {"total_tokens": 50}, pending

        pipeline_module_ref = pipeline_module
        with patch.object(pipeline_module_ref, "SandboxBackend", FakeBackend):
            mock_s3 = MagicMock()
            mock_s3.upload_bytes = MagicMock(
                return_value="s3://bucket/real/run-1/report.json"
            )
            run_store = _make_mock_run_store()

            await pipeline.run_query_agent_tracked(
                agent_id="q-s3",
                run_id="run-1",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test query",
            )

            # Real S3 upload was called
            mock_s3.upload_bytes.assert_called_once_with(
                "run-1/report.json", b'{"result": "ok"}', "application/json",
            )
            # run_store final state has real URL
            final_call = run_store.update_status.call_args_list[-1]
            assert final_call.kwargs.get("s3_url") == "s3://bucket/real/run-1/report.json"
            # Output has placeholder replaced
            assert "s3://bucket/real/run-1/report.json" in (
                final_call.kwargs.get("output", "")
            )
            assert placeholder not in final_call.kwargs.get("output", "")

    @pytest.mark.asyncio
    async def test_s3_upload_after_mediator(self):
        """S3 upload should happen AFTER mediator, not during query agent."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-med", name="Q", image="img:test",
        ))
        pipeline.agent_store.create(AgentConfig(
            agent_id="med-1", name="Mediator", image="img:med",
        ))

        call_order = []
        pending = [{
            "key": "run-m/data.json",
            "data": b"sensitive data",
            "content_type": "application/json",
            "placeholder_url": "s3://pending/run-m/data.json",
        }]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, role="query", **kwargs):
                if role == "query":
                    call_order.append("query")
                    return "raw output with s3://pending/run-m/data.json", {"total_tokens": 10}, pending
                elif role == "mediator":
                    call_order.append("mediator")
                    return "mediated output with s3://pending/run-m/data.json", {"total_tokens": 5}, []

        mock_s3 = MagicMock()
        def track_upload(*args, **kwargs):
            call_order.append("s3_upload")
            return "s3://bucket/run-m/data.json"
        mock_s3.upload_bytes = MagicMock(side_effect=track_upload)

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-med",
                run_id="run-m",
                run_store=_make_mock_run_store(),
                s3_uploader=mock_s3,
                prompt="test",
                mediator_agent_id="med-1",
            )

        # S3 upload must come AFTER mediator
        assert call_order == ["query", "mediator", "s3_upload"]

    @pytest.mark.asyncio
    async def test_s3_upload_failure_does_not_crash_pipeline(self):
        """If S3 upload fails, pipeline should still complete (not raise)."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-fail", name="Q", image="img:test",
        ))

        pending = [{
            "key": "run-f/report.json",
            "data": b"data",
            "content_type": "application/json",
            "placeholder_url": "s3://pending/run-f/report.json",
        }]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "output text", {"total_tokens": 10}, pending

        mock_s3 = MagicMock()
        mock_s3.upload_bytes = MagicMock(side_effect=RuntimeError("S3 down"))

        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            # Should NOT raise
            await pipeline.run_query_agent_tracked(
                agent_id="q-fail",
                run_id="run-f",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test",
            )

        # Pipeline still marked completed (no s3_url)
        final_call = run_store.update_status.call_args_list[-1]
        assert final_call.args[0] == "run-f"
        assert final_call.args[1] == "completed"
        assert "s3_url" not in final_call.kwargs

    @pytest.mark.asyncio
    async def test_no_pending_uploads_no_s3_call(self):
        """When agent doesn't use S3, no upload should happen."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-plain", name="Q", image="img:test",
        ))

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "just text output", {"total_tokens": 10}, []  # No uploads

        mock_s3 = MagicMock()
        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-plain",
                run_id="run-p",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test",
            )

        mock_s3.upload_bytes.assert_not_called()
        final_call = run_store.update_status.call_args_list[-1]
        assert final_call.args[1] == "completed"
        assert "s3_url" not in final_call.kwargs

    @pytest.mark.asyncio
    async def test_no_s3_uploader_skips_upload(self):
        """When s3_uploader is None, pending uploads are ignored."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-no-s3", name="Q", image="img:test",
        ))

        pending = [{
            "key": "run-n/file.json",
            "data": b"data",
            "content_type": "application/json",
            "placeholder_url": "s3://pending/run-n/file.json",
        }]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "output", {"total_tokens": 10}, pending

        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-no-s3",
                run_id="run-n",
                run_store=run_store,
                s3_uploader=None,  # No uploader
                prompt="test",
            )

        final_call = run_store.update_status.call_args_list[-1]
        assert final_call.args[1] == "completed"
        assert "s3_url" not in final_call.kwargs

    @pytest.mark.asyncio
    async def test_multiple_uploads_last_url_wins(self):
        """With multiple uploads, the last successful s3_url is stored."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-multi", name="Q", image="img:test",
        ))

        pending = [
            {
                "key": "run-mm/a.json",
                "data": b"aaa",
                "content_type": "application/json",
                "placeholder_url": "s3://pending/run-mm/a.json",
            },
            {
                "key": "run-mm/b.json",
                "data": b"bbb",
                "content_type": "application/json",
                "placeholder_url": "s3://pending/run-mm/b.json",
            },
        ]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "see s3://pending/run-mm/a.json and s3://pending/run-mm/b.json", {
                    "total_tokens": 10,
                }, pending

        urls = iter(["s3://bucket/run-mm/a.json", "s3://bucket/run-mm/b.json"])
        mock_s3 = MagicMock()
        mock_s3.upload_bytes = MagicMock(side_effect=lambda *a, **kw: next(urls))

        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-multi",
                run_id="run-mm",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test",
            )

        assert mock_s3.upload_bytes.call_count == 2
        final_call = run_store.update_status.call_args_list[-1]
        # Last upload URL is stored
        assert final_call.kwargs["s3_url"] == "s3://bucket/run-mm/b.json"
        # Both placeholders replaced in output
        output = final_call.kwargs["output"]
        assert "s3://bucket/run-mm/a.json" in output
        assert "s3://bucket/run-mm/b.json" in output
        assert "s3://pending/" not in output

    @pytest.mark.asyncio
    async def test_partial_upload_failure_continues(self):
        """If one upload fails, the rest still proceed."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-partial", name="Q", image="img:test",
        ))

        pending = [
            {
                "key": "run-pf/fail.json",
                "data": b"bad",
                "content_type": "application/json",
                "placeholder_url": "s3://pending/run-pf/fail.json",
            },
            {
                "key": "run-pf/ok.json",
                "data": b"good",
                "content_type": "application/json",
                "placeholder_url": "s3://pending/run-pf/ok.json",
            },
        ]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "output", {"total_tokens": 10}, pending

        call_count = 0

        def side_effect(key, data, content_type):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first upload failed")
            return "s3://bucket/run-pf/ok.json"

        mock_s3 = MagicMock()
        mock_s3.upload_bytes = MagicMock(side_effect=side_effect)
        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-partial",
                run_id="run-pf",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test",
            )

        # Both uploads attempted
        assert mock_s3.upload_bytes.call_count == 2
        # Second one succeeded
        final_call = run_store.update_status.call_args_list[-1]
        assert final_call.kwargs["s3_url"] == "s3://bucket/run-pf/ok.json"

    @pytest.mark.asyncio
    async def test_mediator_failure_still_uploads(self):
        """If mediator fails (non-fatal), S3 upload should still proceed."""
        pipeline = _make_test_pipeline()
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-mf", name="Q", image="img:test",
        ))
        pipeline.agent_store.create(AgentConfig(
            agent_id="med-fail", name="Mediator", image="img:med",
        ))

        pending = [{
            "key": "run-mf/report.json",
            "data": b"data",
            "content_type": "application/json",
            "placeholder_url": "s3://pending/run-mf/report.json",
        }]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, role="query", **kwargs):
                if role == "query":
                    return "raw output", {"total_tokens": 10}, pending
                elif role == "mediator":
                    raise ValueError("mediator crashed")

        mock_s3 = MagicMock()
        mock_s3.upload_bytes = MagicMock(
            return_value="s3://bucket/run-mf/report.json"
        )
        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-mf",
                run_id="run-mf",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test",
                mediator_agent_id="med-fail",
            )

        # S3 upload still happened despite mediator failure
        mock_s3.upload_bytes.assert_called_once()
        final_call = run_store.update_status.call_args_list[-1]
        assert final_call.args[1] == "completed"
        assert final_call.kwargs["s3_url"] == "s3://bucket/run-mf/report.json"

    @pytest.mark.asyncio
    async def test_without_mediator_uploads_still_work(self):
        """S3 uploads work even when no mediator is configured."""
        pipeline = _make_test_pipeline()
        # Override settings to ensure no default mediator
        pipeline.settings.default_mediator_agent = ""
        pipeline.agent_store.create(AgentConfig(
            agent_id="q-nomed", name="Q", image="img:test",
        ))

        pending = [{
            "key": "run-nm/file.json",
            "data": b"data",
            "content_type": "application/json",
            "placeholder_url": "s3://pending/run-nm/file.json",
        }]

        class FakeBackend:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, **kwargs):
                return "output", {"total_tokens": 10}, pending

        mock_s3 = MagicMock()
        mock_s3.upload_bytes = MagicMock(
            return_value="s3://bucket/run-nm/file.json"
        )
        run_store = _make_mock_run_store()

        with patch.object(pipeline_module, "SandboxBackend", FakeBackend):
            await pipeline.run_query_agent_tracked(
                agent_id="q-nomed",
                run_id="run-nm",
                run_store=run_store,
                s3_uploader=mock_s3,
                prompt="test",
                mediator_agent_id=None,  # explicitly no mediator
            )

        mock_s3.upload_bytes.assert_called_once()
        final_call = run_store.update_status.call_args_list[-1]
        assert final_call.kwargs["s3_url"] == "s3://bucket/run-nm/file.json"
