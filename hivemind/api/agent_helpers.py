"""Shared helpers for agent uploads, image metadata, and inspection policy."""

from __future__ import annotations

import asyncio
import logging
import os
import tarfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile

logger = logging.getLogger(__name__)

IGNORED_TAR_TYPES = {
    tarfile.XHDTYPE,         # PAX extended header
    tarfile.XGLTYPE,         # PAX global header
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
}

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB compressed archive bytes
MAX_UPLOAD_TAR_MEMBERS = 2_000
MAX_UPLOAD_TAR_MEMBER_BYTES = 15 * 1024 * 1024  # 15 MB per file
MAX_UPLOAD_TAR_TOTAL_BYTES = 150 * 1024 * 1024  # 150 MB total extracted size


def image_digest(image: str) -> dict:
    """Return ``{id, repo_digests}`` for a tagged Docker image."""
    try:
        import docker

        client = docker.from_env()
        attrs = client.images.get(image).attrs
        return {
            "id": attrs.get("Id", "") or "",
            "repo_digests": list(attrs.get("RepoDigests") or []),
        }
    except Exception as e:
        logger.debug("image digest lookup failed for %r: %s", image, e)
        return {"id": "", "repo_digests": []}


def tenant_image_tag(tenant_id: str | None, agent_id: str) -> str:
    """Scope docker image tags by tenant so shared daemons do not collide."""
    if tenant_id:
        return f"hivemind-agent-{tenant_id}-{agent_id}:latest"
    return f"hivemind-agent-{agent_id}:latest"


async def read_upload_bytes_limited(
    upload: UploadFile,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> bytes:
    """Read upload content in chunks and stop once the byte cap is exceeded."""
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(
                f"Archive too large ({total} bytes). Max: {max_bytes} bytes."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def safe_extract_tar(
    archive_bytes: bytes,
    extract_to: str,
    *,
    max_members: int = MAX_UPLOAD_TAR_MEMBERS,
    max_member_bytes: int = MAX_UPLOAD_TAR_MEMBER_BYTES,
    max_total_bytes: int = MAX_UPLOAD_TAR_TOTAL_BYTES,
) -> None:
    """Extract a tar archive while rejecting path traversal and link entries."""
    import io

    base = Path(extract_to).resolve()
    member_count = 0
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.type in IGNORED_TAR_TYPES:
                continue

            member_count += 1
            if member_count > max_members:
                raise ValueError(
                    f"Archive has too many entries ({member_count} > {max_members})"
                )

            target = (base / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"Invalid archive member path: {member.name}")

            if member.issym() or member.islnk():
                raise ValueError(f"Symlink entries are not allowed: {member.name}")

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                raise ValueError(f"Unsupported archive member: {member.name}")

            member_size = int(member.size or 0)
            if member_size < 0:
                raise ValueError(f"Invalid archive member size: {member.name}")
            if member_size > max_member_bytes:
                raise ValueError(
                    f"Archive member too large ({member.name}: {member_size} bytes)"
                )
            total_bytes += member_size
            if total_bytes > max_total_bytes:
                raise ValueError(
                    f"Archive expands beyond limit ({total_bytes} > {max_total_bytes})"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise ValueError(f"Invalid archive member: {member.name}")
            with src, open(target, "wb") as dst:
                remaining = member_size
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError(
                            f"Unexpected end of archive while extracting {member.name}"
                        )
                    dst.write(chunk)
                    remaining -= len(chunk)

            file_mode = member.mode & 0o777
            os.chmod(target, file_mode or 0o644)


def validate_inspection_mode(mode: str, *, require_kms: bool = True) -> str:
    """Coerce/validate an owner-side ``inspection_mode`` form field."""
    m = (mode or "full").strip().lower() or "full"
    if m not in {"full", "sealed"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"inspection_mode must be one of 'full', 'sealed' "
                f"(got {mode!r})"
            ),
        )
    return m


def read_extracted_files(tmpdir: str) -> dict[str, str]:
    """Read all extracted source files from a directory as {path: content}."""
    files: dict[str, str] = {}
    base = Path(tmpdir)
    for fpath in sorted(base.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(base))
        if any(part.startswith(".") or part == "__pycache__" for part in rel.split("/")):
            continue
        try:
            files[rel] = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return files


def spawn_bg(app: FastAPI, coro) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine and pin a strong ref."""
    task = asyncio.create_task(coro)
    bg = getattr(app.state, "background_tasks", None)
    if isinstance(bg, set):
        bg.add(task)
        task.add_done_callback(bg.discard)
    return task
