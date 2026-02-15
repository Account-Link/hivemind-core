from typing import Any

from pydantic import BaseModel, Field, StrictStr, model_validator
from datetime import datetime


# ── Store ──


class StoreRequest(BaseModel):
    data: str = Field(..., min_length=1)  # content (encrypted at rest)
    metadata: dict = Field(default_factory=dict)  # app-defined, stored as JSON
    index_text: str | None = None  # pre-computed FTS text (skip index agent)
    index_agent_id: str | None = None  # run Docker agent to produce index_text + metadata


class StoreResponse(BaseModel):
    record_id: str
    created_at: datetime
    metadata: dict


class RecordPatchRequest(BaseModel):
    metadata: dict[str, Any] | None = None
    index_text: StrictStr | None = None


# ── Query ──


class QueryRequest(BaseModel):
    query: str = Field(
        ...,
        min_length=1,
    )  # advisory: passed as QUERY_PROMPT env var; custom agents may ignore
    prompt: str | None = None  # deprecated alias for query
    scope: list[str] | None = None  # record_id whitelist, None = all
    query_agent_id: str | None = None  # uses default if omitted
    scope_agent_id: str | None = None  # optional dynamic scoping
    mediator_agent_id: str | None = None  # optional output filtering
    max_tokens: int | None = Field(default=None, ge=1)  # per-query budget cap

    @model_validator(mode="before")
    @classmethod
    def _resolve_query(cls, data):
        if not isinstance(data, dict):
            return data

        query = data.get("query")
        prompt = data.get("prompt")
        query_text = query.strip() if isinstance(query, str) else ""
        prompt_text = prompt.strip() if isinstance(prompt, str) else ""

        if query_text:
            return data
        if prompt_text:
            payload = dict(data)
            payload["query"] = prompt_text
            return payload
        if query is None:
            raise ValueError("'query' (or 'prompt') is required")
        return data

    @model_validator(mode="after")
    def _validate_query(self):
        if not self.query.strip():
            raise ValueError("'query' (or 'prompt') is required")
        return self


class QueryResponse(BaseModel):
    output: str
    records_accessed: list[str]
    mediated: bool
    usage: dict | None = None


# ── Health ──


class HealthResponse(BaseModel):
    status: str = "ok"
    record_count: int
    version: str
