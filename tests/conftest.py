import os
import tempfile

import pytest

from hivemind.store import RecordStore


@pytest.fixture(autouse=True)
def _clear_default_agent_env():
    """Keep tests independent from local .env default agent IDs."""
    keys = (
        "HIVEMIND_LLM_API_KEY",
        "HIVEMIND_LLM_BASE_URL",
        "HIVEMIND_LLM_MODEL",
        "HIVEMIND_CORS_ALLOW_ORIGINS",
        "HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS",
        "HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS_FAIL_CLOSED",
        "HIVEMIND_AUTOLOAD_DEFAULT_AGENTS",
        "HIVEMIND_DEFAULT_INDEX_AGENT",
        "HIVEMIND_DEFAULT_SCOPE_AGENT",
        "HIVEMIND_DEFAULT_QUERY_AGENT",
        "HIVEMIND_DEFAULT_MEDIATOR_AGENT",
        "HIVEMIND_DEFAULT_INDEX_IMAGE",
        "HIVEMIND_DEFAULT_SCOPE_IMAGE",
        "HIVEMIND_DEFAULT_QUERY_IMAGE",
        "HIVEMIND_DEFAULT_MEDIATOR_IMAGE",
    )
    before = {k: os.environ.get(k) for k in keys}
    for key in keys:
        if key == "HIVEMIND_AUTOLOAD_DEFAULT_AGENTS":
            os.environ[key] = "false"
        elif key == "HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS":
            os.environ[key] = "false"
        elif key == "HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS_FAIL_CLOSED":
            os.environ[key] = "true"
        elif key == "HIVEMIND_LLM_BASE_URL":
            os.environ[key] = "https://openrouter.ai/api/v1"
        elif key == "HIVEMIND_LLM_MODEL":
            os.environ[key] = "anthropic/claude-sonnet-4.5"
        elif key == "HIVEMIND_CORS_ALLOW_ORIGINS":
            os.environ[key] = ""
        else:
            os.environ[key] = ""
    yield
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def tmp_db():
    """Temporary RecordStore backed by a throwaway SQLite file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = RecordStore(path)
    yield store
    store.close()
    os.unlink(path)
