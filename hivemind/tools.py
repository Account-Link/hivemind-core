import json
from dataclasses import dataclass
from typing import Callable

from .store import RecordStore

DEFAULT_READ_CHUNK = 20_000
MAX_TOOL_SEARCH_LIMIT = 200
MAX_TOOL_LIST_LIMIT = 200
MAX_TOOL_LIST_OFFSET = 5_000_000
MAX_TOOL_READ_LIMIT = 50_000
MAX_TOOL_READ_OFFSET = 5_000_000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., str]

    def to_openai_def(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_agent_file_tools(agent_store, query_agent_id: str) -> list[Tool]:
    """Build tools for scoping agents to inspect a query agent's source code."""

    def list_query_agent_files() -> str:
        files = agent_store.list_file_paths(query_agent_id)
        if not files:
            return json.dumps({
                "files": [],
                "note": "No source files extracted for this agent. "
                "The image may contain only compiled binaries.",
            })
        return json.dumps({"files": files})

    def read_query_agent_file(file_path: str) -> str:
        content = agent_store.read_file(query_agent_id, file_path)
        if content is None:
            return "File not found. Use list_query_agent_files to see available files."
        return content

    return [
        Tool(
            name="list_query_agent_files",
            description=(
                "List all source files extracted from the query agent's Docker image. "
                "Returns file paths and sizes."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            handler=list_query_agent_files,
        ),
        Tool(
            name="read_query_agent_file",
            description=(
                "Read the contents of a specific source file from the query agent's "
                "Docker image."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path of the file to read (from list_query_agent_files)",
                    },
                },
                "required": ["file_path"],
            },
            handler=read_query_agent_file,
        ),
    ]


def build_tools(
    store: RecordStore, scope: list[str] | None = None
) -> list[Tool]:
    """Build the 3 scoped storage tools: search, read, list."""

    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _clamp_int(value, default: int, min_value: int, max_value: int) -> int:
        parsed = _safe_int(value, default)
        if parsed < min_value:
            return min_value
        if parsed > max_value:
            return max_value
        return parsed

    def search(query: str, limit: int = 20) -> str:
        safe_limit = _clamp_int(limit, 20, 1, MAX_TOOL_SEARCH_LIMIT)
        results = store.search(query, scope=scope, limit=safe_limit)
        return json.dumps(results, default=str)

    def read(record_id: str, offset: int = 0, limit: int = DEFAULT_READ_CHUNK) -> str:
        record = store.read(record_id, scope=scope)
        if record is None:
            return "Record not found"
        safe_offset = _clamp_int(offset, 0, 0, MAX_TOOL_READ_OFFSET)
        safe_limit = _clamp_int(limit, DEFAULT_READ_CHUNK, 1, MAX_TOOL_READ_LIMIT)
        data = record["data"]
        total = len(data)
        chunk = data[safe_offset : safe_offset + safe_limit]

        header = ""
        if safe_offset == 0:
            meta_parts = [f"record_id: {record['id']}"]
            meta = record.get("metadata", {})
            if meta:
                for k, v in list(meta.items())[:5]:
                    meta_parts.append(f"{k}: {v}")
            header = "[" + ", ".join(str(p) for p in meta_parts) + "]\n\n"

        if safe_offset + safe_limit < total:
            remaining = total - safe_offset - safe_limit
            return (
                f"{header}{chunk}\n\n--- offset {safe_offset}, showing {len(chunk)} of "
                f"{total} chars, {remaining} remaining. "
                f"Call read again with offset={safe_offset + safe_limit} to continue. ---"
            )
        return f"{header}{chunk}" if header else chunk

    def list_records(limit: int = 20, offset: int = 0) -> str:
        safe_limit = _clamp_int(limit, 20, 1, MAX_TOOL_LIST_LIMIT)
        safe_offset = _clamp_int(offset, 0, 0, MAX_TOOL_LIST_OFFSET)
        results = store.list_records(scope=scope, limit=safe_limit, offset=safe_offset)
        return json.dumps(results, default=str)

    return [
        Tool(
            name="search",
            description=(
                "Search the knowledge base using a text query. "
                "Returns matching records with IDs, metadata, index_text, and score."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            handler=search,
        ),
        Tool(
            name="read",
            description=(
                "Read the data of a record by ID. Returns metadata and the data content. "
                "For large records, returns a chunk and tells you how to fetch more."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "record_id": {
                        "type": "string",
                        "description": "The record ID to read",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from (default 0)",
                        "default": 0,
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"Max characters to return (default {DEFAULT_READ_CHUNK})",
                        "default": DEFAULT_READ_CHUNK,
                    },
                },
                "required": ["record_id"],
            },
            handler=read,
        ),
        Tool(
            name="list",
            description=(
                "Browse recent records. Returns metadata and index_text, sorted by most recent."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20)",
                        "default": 20,
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip first N results (default 0)",
                        "default": 0,
                    },
                },
                "required": [],
            },
            handler=list_records,
        ),
    ]
