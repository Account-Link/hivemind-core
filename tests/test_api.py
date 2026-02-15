import io
import tarfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from hivemind.config import Settings
from hivemind.server import create_app
from hivemind.version import APP_VERSION


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed_client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="secret",
        llm_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_health(client):
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["record_count"] == 0
    assert data["version"] == APP_VERSION


def test_store_with_index_text(client):
    resp = client.post(
        "/v1/store",
        json={
            "data": "The team decided to migrate to Stripe.",
            "metadata": {"author": "alice", "team": "payments"},
            "index_text": "Payment Migration Stripe processing decision",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "record_id" in data
    assert data["metadata"]["author"] == "alice"


def test_store_and_health_count(client):
    client.post(
        "/v1/store",
        json={
            "data": "Some text",
            "index_text": "some index",
        },
    )
    resp = client.get("/v1/health")
    assert resp.json()["record_count"] == 1


def test_get_record_metadata(client):
    resp = client.post(
        "/v1/store",
        json={
            "data": "Content here",
            "metadata": {"title": "Test Record"},
            "index_text": "test content",
        },
    )
    record_id = resp.json()["record_id"]

    resp = client.get(f"/v1/admin/records/{record_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metadata"]["title"] == "Test Record"
    assert data["index_text"] == "test content"
    assert "data" not in data  # should NOT expose raw data


def test_update_record_metadata(client):
    resp = client.post(
        "/v1/store",
        json={"data": "Content", "metadata": {"old": True}},
    )
    record_id = resp.json()["record_id"]

    resp = client.patch(
        f"/v1/admin/records/{record_id}",
        json={"metadata": {"new": True}},
    )
    assert resp.status_code == 200

    resp = client.get(f"/v1/admin/records/{record_id}")
    assert resp.json()["metadata"] == {"new": True}


def test_update_record_index_text(client):
    resp = client.post(
        "/v1/store",
        json={"data": "Content", "index_text": "old text"},
    )
    record_id = resp.json()["record_id"]

    resp = client.patch(
        f"/v1/admin/records/{record_id}",
        json={"index_text": "new searchable text"},
    )
    assert resp.status_code == 200


def test_update_record_metadata_and_index_text_together(client):
    resp = client.post(
        "/v1/store",
        json={
            "data": "Content",
            "metadata": {"old": True},
            "index_text": "old text",
        },
    )
    record_id = resp.json()["record_id"]

    resp = client.patch(
        f"/v1/admin/records/{record_id}",
        json={
            "metadata": {"new": True},
            "index_text": "new text",
        },
    )
    assert resp.status_code == 200

    resp = client.get(f"/v1/admin/records/{record_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"] == {"new": True}
    assert body["index_text"] == "new text"


def test_delete_record(client):
    resp = client.post(
        "/v1/store",
        json={"data": "To be deleted"},
    )
    record_id = resp.json()["record_id"]

    resp = client.delete(f"/v1/admin/records/{record_id}")
    assert resp.status_code == 200

    resp = client.delete(f"/v1/admin/records/{record_id}")
    assert resp.status_code == 404


def test_auth_required(authed_client):
    resp = authed_client.post(
        "/v1/store",
        json={"data": "test"},
    )
    assert resp.status_code == 401

    resp = authed_client.post(
        "/v1/store",
        json={"data": "test"},
        headers={"Authorization": "Bearer secret"},
    )
    assert resp.status_code == 200


def test_health_no_auth_required(authed_client):
    resp = authed_client.get("/v1/health")
    assert resp.status_code == 200


def test_register_agent_memory_is_capped(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
        container_memory_mb=256,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.image_exists.return_value = True
            instance.extract_image_files_async = AsyncMock(return_value={})

            resp = client.post(
                "/v1/agents",
                json={
                    "name": "mem-test",
                    "image": "myorg/agent:v1",
                    "memory_mb": 2048,
                },
            )
            assert resp.status_code == 200
            agent_id = resp.json()["agent_id"]

            resp = client.get(f"/v1/agents/{agent_id}")
            assert resp.status_code == 200
            assert resp.json()["memory_mb"] == 256


def test_agent_crud(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.image_exists.return_value = True
            instance.extract_image_files_async = AsyncMock(return_value={})

            # Create
            resp = client.post(
                "/v1/agents",
                json={"name": "test-agent", "image": "myorg/agent:v1"},
            )
            assert resp.status_code == 200
            agent_id = resp.json()["agent_id"]

            # List
            resp = client.get("/v1/agents")
            assert resp.status_code == 200
            assert len(resp.json()) == 1

            # Get
            resp = client.get(f"/v1/agents/{agent_id}")
            assert resp.status_code == 200
            assert resp.json()["name"] == "test-agent"

            # Delete
            resp = client.delete(f"/v1/agents/{agent_id}")
            assert resp.status_code == 200

            resp = client.delete(f"/v1/agents/{agent_id}")
            assert resp.status_code == 404


def test_register_agent_rejects_missing_local_image(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.image_exists.return_value = False

            resp = client.post(
                "/v1/agents",
                json={"name": "missing-image", "image": "missing:latest"},
            )

    assert resp.status_code == 400
    assert "not found locally" in resp.json()["detail"]


def test_register_agent_handles_docker_preflight_failure(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.image_exists.side_effect = RuntimeError("docker daemon down")

            resp = client.post(
                "/v1/agents",
                json={"name": "preflight-error", "image": "example:v1"},
            )

    assert resp.status_code == 503
    assert "Docker daemon unavailable" in resp.json()["detail"]


def test_get_nonexistent_record(client):
    resp = client.get("/v1/admin/records/nonexistent")
    assert resp.status_code == 404


def test_patch_requires_fields(client):
    resp = client.post(
        "/v1/store",
        json={"data": "Content"},
    )
    record_id = resp.json()["record_id"]

    resp = client.patch(
        f"/v1/admin/records/{record_id}",
        json={},
    )
    assert resp.status_code == 400


def test_patch_rejects_null_metadata(client):
    resp = client.post("/v1/store", json={"data": "Content"})
    record_id = resp.json()["record_id"]

    resp = client.patch(f"/v1/admin/records/{record_id}", json={"metadata": None})
    assert resp.status_code == 400
    assert "metadata" in resp.json()["detail"]


def test_patch_rejects_null_index_text(client):
    resp = client.post("/v1/store", json={"data": "Content"})
    record_id = resp.json()["record_id"]

    resp = client.patch(f"/v1/admin/records/{record_id}", json={"index_text": None})
    assert resp.status_code == 400
    assert "index_text" in resp.json()["detail"]


def test_patch_rejects_non_string_index_text(client):
    resp = client.post("/v1/store", json={"data": "Content"})
    record_id = resp.json()["record_id"]

    resp = client.patch(f"/v1/admin/records/{record_id}", json={"index_text": 123})
    assert resp.status_code == 422


def test_patch_rejects_non_object_metadata(client):
    resp = client.post("/v1/store", json={"data": "Content"})
    record_id = resp.json()["record_id"]

    resp = client.patch(f"/v1/admin/records/{record_id}", json={"metadata": ["not", "object"]})
    assert resp.status_code == 422


# ── Upload endpoint tests ──


def _make_upload_tar(files: dict[str, str]) -> bytes:
    """Create an in-memory gzipped tarball from a dict of path -> content."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    return buf.read()


def test_upload_agent(tmp_path):
    """Upload a tarball with Dockerfile, verify agent is registered."""
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    tar_bytes = _make_upload_tar({
        "Dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
        "agent.py": "print('hello')\n",
    })

    with TestClient(app) as client:
        with patch(
            "hivemind.sandbox.docker_runner.DockerRunner"
        ) as MockRunner:
            instance = MockRunner.return_value
            instance.build_image_async = AsyncMock(
                return_value="hivemind-agent-test:latest"
            )
            instance.extract_image_files_async = AsyncMock(
                return_value={"agent.py": "print('hello')\n"}
            )

            resp = client.post(
                "/v1/agents/upload",
                data={"name": "my-agent", "description": "Test agent"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-agent"
        assert "agent_id" in data
        assert data["files_extracted"] == 1

        # Verify agent was registered
        resp = client.get(f"/v1/agents/{data['agent_id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "my-agent"


def test_upload_rejects_missing_dockerfile(tmp_path):
    """Upload without Dockerfile should return 400."""
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    tar_bytes = _make_upload_tar({
        "agent.py": "print('hello')\n",
    })

    with TestClient(app) as client:
        with patch(
            "hivemind.sandbox.docker_runner.DockerRunner"
        ) as MockRunner:
            instance = MockRunner.return_value
            instance.build_image_async = AsyncMock(
                side_effect=ValueError("No Dockerfile found in upload. A Dockerfile is required.")
            )

            resp = client.post(
                "/v1/agents/upload",
                data={"name": "bad-agent"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

        assert resp.status_code == 400
        assert "Dockerfile" in resp.json()["detail"]


def test_upload_rejects_invalid_archive(tmp_path):
    """Non-tarball upload should return 400."""
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/agents/upload",
            data={"name": "bad-agent"},
            files={"archive": ("agent.tar.gz", b"not a tarball", "application/gzip")},
        )

    assert resp.status_code == 400
    assert "Invalid archive" in resp.json()["detail"]


def test_upload_unexpected_extract_error_returns_500(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    tar_bytes = _make_upload_tar({
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "print('hello')\n",
    })

    with TestClient(app) as client:
        with patch(
            "hivemind.server._safe_extract_tar",
            side_effect=RuntimeError("filesystem unavailable"),
        ), patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            resp = client.post(
                "/v1/agents/upload",
                data={"name": "bad-agent"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Archive extraction failed"
    assert "filesystem unavailable" not in resp.json()["detail"]
    MockRunner.assert_not_called()


def test_upload_docker_build_error_is_redacted(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    tar_bytes = _make_upload_tar({
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "print('hello')\n",
    })

    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            instance = MockRunner.return_value
            instance.build_image_async = AsyncMock(
                side_effect=RuntimeError("sensitive host path /tmp/private")
            )

            resp = client.post(
                "/v1/agents/upload",
                data={"name": "bad-agent"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

    assert resp.status_code == 500
    assert resp.json()["detail"] == "Docker build failed"
    assert "sensitive" not in resp.json()["detail"]


def test_upload_rejects_path_traversal_archive(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    tar_bytes = _make_upload_tar({
        "../escape.py": "print('escape')\n",
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "print('hello')\n",
    })

    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            resp = client.post(
                "/v1/agents/upload",
                data={"name": "bad-agent"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

    assert resp.status_code == 400
    assert "Invalid archive" in resp.json()["detail"]
    MockRunner.assert_not_called()


def test_upload_rejects_oversized_member(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    # Exceeds MAX_UPLOAD_TAR_MEMBER_BYTES (15MB).
    tar_bytes = _make_upload_tar({
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "x" * (16 * 1024 * 1024),
    })

    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            resp = client.post(
                "/v1/agents/upload",
                data={"name": "too-big-member"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

    assert resp.status_code == 400
    assert "Invalid archive" in resp.json()["detail"]
    assert "too large" in resp.json()["detail"].lower()
    MockRunner.assert_not_called()


def test_upload_rejects_invalid_limits(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    tar_bytes = _make_upload_tar({
        "Dockerfile": "FROM python:3.12-slim\n",
        "agent.py": "print('hello')\n",
    })

    bad_payloads = (
        {"name": "bad-1", "memory_mb": "0"},
        {"name": "bad-2", "max_llm_calls": "0"},
        {"name": "bad-3", "max_tokens": "0"},
        {"name": "bad-4", "timeout_seconds": "0"},
    )

    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            for payload in bad_payloads:
                resp = client.post(
                    "/v1/agents/upload",
                    data=payload,
                    files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
                )
                assert resp.status_code == 422

    MockRunner.assert_not_called()


def test_query_accepts_query_field(client):
    """POST /v1/query should accept 'query' as the canonical field name."""
    resp = client.post(
        "/v1/query",
        json={"query": "What happened?", "query_agent_id": "qa-1"},
    )
    # Will fail with 400 (agent not found), but NOT 422 (validation error)
    assert resp.status_code == 400


def test_query_accepts_prompt_backward_compat(client):
    """POST /v1/query should still accept 'prompt' for backward compatibility."""
    resp = client.post(
        "/v1/query",
        json={"prompt": "What happened?", "query_agent_id": "qa-1"},
    )
    assert resp.status_code == 400  # agent not found, not 422


def test_query_openapi_marks_query_required(client):
    schema = client.get("/openapi.json").json()
    query_request = schema["components"]["schemas"]["QueryRequest"]
    assert "query" in query_request.get("required", [])


def test_query_accepts_max_tokens(client):
    """POST /v1/query should accept 'max_tokens' in the request body."""
    resp = client.post(
        "/v1/query",
        json={"query": "What?", "query_agent_id": "qa-1", "max_tokens": 50000},
    )
    assert resp.status_code == 400  # agent not found, not 422


def test_query_rejects_zero_max_tokens(client):
    """POST /v1/query should reject max_tokens=0."""
    resp = client.post(
        "/v1/query",
        json={"query": "What?", "query_agent_id": "qa-1", "max_tokens": 0},
    )
    assert resp.status_code == 422


def test_upload_rejects_too_many_members(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    files = {"Dockerfile": "FROM python:3.12-slim\n"}
    for i in range(2001):  # exceeds MAX_UPLOAD_TAR_MEMBERS=2000
        files[f"src/file_{i}.txt"] = "x"
    tar_bytes = _make_upload_tar(files)

    with TestClient(app) as client:
        with patch("hivemind.sandbox.docker_runner.DockerRunner") as MockRunner:
            resp = client.post(
                "/v1/agents/upload",
                data={"name": "too-many-members"},
                files={"archive": ("agent.tar.gz", tar_bytes, "application/gzip")},
            )

    assert resp.status_code == 400
    assert "Invalid archive" in resp.json()["detail"]
    assert "too many entries" in resp.json()["detail"].lower()
    MockRunner.assert_not_called()


def test_upload_rejects_oversized_archive_before_extract(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "test.db"),
        api_key="",
        llm_api_key="test",
    )
    app = create_app(settings)

    with TestClient(app) as client:
        with patch("hivemind.server.MAX_UPLOAD_SIZE", 1024), patch(
            "hivemind.server._safe_extract_tar"
        ) as mock_extract:
            resp = client.post(
                "/v1/agents/upload",
                data={"name": "too-big"},
                files={"archive": ("agent.tar.gz", b"x" * 1025, "application/gzip")},
            )

    assert resp.status_code == 400
    assert "Archive too large" in resp.json()["detail"]
    mock_extract.assert_not_called()
