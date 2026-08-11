"""Workspace-scoped source preview payloads for the WebUI."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from nanobot.security.workspace_access import WorkspaceScope
from nanobot.security.workspace_policy import WorkspaceBoundaryError, resolve_allowed_path

MAX_FILE_PREVIEW_BYTES = 384 * 1024


class WebUIFilePreviewError(ValueError):
    """Raised when a file cannot be previewed through the WebUI."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def file_preview_payload(
    raw_path: str | None,
    *,
    scope: WorkspaceScope,
    trusted_files: Mapping[str, str] | None = None,
    max_bytes: int = MAX_FILE_PREVIEW_BYTES,
) -> dict[str, Any]:
    """Return a text preview for a workspace file or an exact session-bound artifact."""

    path = _clean_preview_path(raw_path)
    if not path:
        raise WebUIFilePreviewError(400, "missing path")
    if len(path) > 4096:
        raise WebUIFilePreviewError(400, "path is too long")

    trusted_checksum: str | None = None
    try:
        resolved = resolve_allowed_path(
            path,
            workspace=scope.project_path,
            allowed_root=scope.project_path,
            strict=True,
        )
    except FileNotFoundError as e:
        raise WebUIFilePreviewError(404, "file not found") from e
    except WorkspaceBoundaryError as e:
        trusted = _resolve_trusted_file(path, trusted_files)
        if trusted is None:
            raise WebUIFilePreviewError(403, "file is outside the current workspace") from e
        resolved, trusted_checksum = trusted
    except OSError as e:
        raise WebUIFilePreviewError(400, "invalid path") from e

    if not resolved.is_file():
        raise WebUIFilePreviewError(404, "file not found")

    if trusted_checksum is not None and _sha256_file(resolved) != trusted_checksum:
        raise WebUIFilePreviewError(409, "file checksum validation failed")

    try:
        with open(resolved, "rb") as f:
            raw = f.read(max_bytes + 1)
    except OSError as e:
        raise WebUIFilePreviewError(500, "failed to read file") from e

    if b"\0" in raw[:4096]:
        raise WebUIFilePreviewError(415, "binary files cannot be previewed")

    truncated = len(raw) > max_bytes
    preview_bytes = raw[:max_bytes]
    try:
        content = preview_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = preview_bytes.decode("utf-8", errors="replace")

    display_path = _display_path(resolved, scope.project_path)
    return {
        "path": str(resolved),
        "display_path": display_path,
        "project_path": str(scope.project_path),
        "language": _language_for_path(resolved),
        "content": content,
        "size": resolved.stat().st_size,
        "truncated": truncated,
    }


def session_plan_preview_files(session_data: Any) -> dict[str, str]:
    """Return the exact current plan file trusted by the current session state."""

    if not isinstance(session_data, dict):
        return {}
    metadata = session_data.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    plan = metadata.get("plan_state")
    if not isinstance(plan, dict):
        return {}

    task_id = plan.get("task_id")
    revision = plan.get("revision")
    plan_hash = plan.get("plan_hash")
    markdown = plan.get("plan_markdown")
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", task_id)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or not isinstance(plan_hash, str)
        or not plan_hash
        or not isinstance(markdown, dict)
    ):
        return {}

    path = markdown.get("path")
    checksum = markdown.get("checksum")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or markdown.get("artifact_id") != "plan_markdown"
        or markdown.get("revision") != revision
        or markdown.get("plan_hash") != plan_hash
        or not isinstance(checksum, str)
        or not re.fullmatch(r"[0-9a-f]{64}", checksum)
    ):
        return {}

    candidate = Path(path).expanduser()
    expected_tail = Path(".nanobot-runtime") / "artifacts" / task_id / "plan.md"
    if tuple(candidate.parts[-len(expected_tail.parts):]) != expected_tail.parts:
        return {}
    return {str(candidate): checksum}


def _resolve_trusted_file(
    path: str,
    trusted_files: Mapping[str, str] | None,
) -> tuple[Path, str] | None:
    if not trusted_files:
        return None
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    for trusted_path, checksum in trusted_files.items():
        if not isinstance(trusted_path, str) or not isinstance(checksum, str):
            continue
        try:
            allowed = Path(trusted_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate == allowed and re.fullmatch(r"[0-9a-f]{64}", checksum):
            return candidate, checksum
    return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_preview_path(raw_path: str | None) -> str:
    if raw_path is None:
        return ""
    value = raw_path.strip()
    if not value:
        return ""
    if value.startswith("file://"):
        parsed = urlparse(value)
        value = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:[\\/]", value):
            value = value[1:]
    else:
        value = unquote(value)
    value = value.split("?", 1)[0].split("#", 1)[0].strip()
    if not re.match(r"^[A-Za-z]:[\\/]", value):
        value = re.sub(r":\d+(?::\d+)?$", "", value)
    return value


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _language_for_path(path: Path) -> str:
    name = path.name.lower()
    ext = path.suffix.lower().lstrip(".")
    if name == "dockerfile":
        return "dockerfile"
    return {
        "cjs": "javascript",
        "css": "css",
        "cts": "typescript",
        "html": "html",
        "js": "javascript",
        "json": "json",
        "jsonl": "json",
        "jsx": "jsx",
        "md": "markdown",
        "mdx": "markdown",
        "mjs": "javascript",
        "mts": "typescript",
        "py": "python",
        "pyi": "python",
        "scss": "scss",
        "sh": "bash",
        "toml": "toml",
        "ts": "typescript",
        "tsx": "tsx",
        "yaml": "yaml",
        "yml": "yaml",
    }.get(ext, ext or "text")
