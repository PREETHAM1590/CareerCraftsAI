"""Document storage on local disk.

Previously backed by Supabase Storage. The managed service is unreachable from
the deployment VM — its hosts resolve IPv6-only and the VM has no IPv6 — so
files now live on a mounted volume shared by the backend and the agent worker.

The public surface (upload_file / download_file / delete_file) and the object
key layout "{user_id}/{uuid}.{ext}" are unchanged, so storage_path values
already recorded in user_documents keep resolving.
"""

import logging
import uuid
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)


def _root() -> Path:
    """Storage root, created on demand.

    Resolved per call rather than at import so tests can point
    DOCUMENT_STORAGE_DIR at a temp directory.
    """
    root = Path(settings.DOCUMENT_STORAGE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_extension(filename: str) -> str:
    """Restrict the storage suffix to a short alphanumeric token.

    The original filename is attacker-controlled. Carrying it into the storage
    key would allow path separators or traversal sequences in the object key,
    so only a conservative extension is preserved; everything else is dropped.
    """
    raw = filename.rsplit(".", 1)[-1] if "." in filename else ""
    cleaned = "".join(ch for ch in raw.lower() if ch.isascii() and ch.isalnum())
    return cleaned[:10] or "bin"


def _assert_owns_path(owner_id: str, storage_path: str) -> str:
    """Ensure storage_path belongs to owner_id.

    Paths are laid out as "{user_id}/{uuid}.{ext}", so the first segment must
    equal owner_id. Defense-in-depth against IDOR even when a caller already
    checked the DB row.
    """
    if not owner_id:
        raise PermissionError("owner_id is required for storage access")
    # Normalize and reject traversal. Backslashes are folded first so a
    # Windows-style "..\\.." cannot slip past a separator check that only
    # knows about forward slashes.
    normalized = storage_path.replace("\\", "/").lstrip("/")
    if ".." in normalized.split("/"):
        raise PermissionError(f"Illegal storage path: {storage_path}")
    prefix = normalized.split("/", 1)[0]
    if prefix != owner_id:
        raise PermissionError(
            f"Storage path {storage_path} does not belong to user {owner_id}"
        )
    return normalized


def _resolve(storage_path: str) -> Path:
    """Map a validated storage key to an absolute path inside the root.

    _assert_owns_path already rejects traversal, but this is the boundary where
    a key becomes a filesystem path, so the containment check is repeated
    against the resolved result — symlinks and unicode separators do not
    survive it.
    """
    root = _root()
    target = (root / storage_path).resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"Illegal storage path: {storage_path}")
    return target


def upload_file(user_id: str, filename: str, content: bytes, content_type: str) -> str:
    """Write file to local storage. Returns the storage path."""
    ext = _safe_extension(filename)
    path = f"{user_id}/{uuid.uuid4()}.{ext}"
    target = _resolve(_assert_owns_path(user_id, path))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file then rename, so a crash mid-write can
        # never leave a truncated document behind a valid-looking DB row.
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(target)
    except Exception as exc:
        logger.warning("Storage upload failed for %s: %s", path, exc)
        raise RuntimeError("Storage upload failed") from exc
    logger.info("Stored %d bytes at %s (%s)", len(content), path, content_type)
    return path


def download_file(storage_path: str, owner_id: str) -> bytes:
    """Read file bytes from local storage. Verifies ownership first."""
    storage_path = _assert_owns_path(owner_id, storage_path)
    try:
        return _resolve(storage_path).read_bytes()
    except PermissionError:
        raise
    except FileNotFoundError as exc:
        logger.warning("Storage download missing for %s", storage_path)
        raise RuntimeError("Storage download failed") from exc
    except Exception as exc:
        logger.warning("Storage download failed for %s: %s", storage_path, exc)
        raise RuntimeError("Storage download failed") from exc


def delete_file(storage_path: str, owner_id: str) -> None:
    """Delete a file from local storage. Verifies ownership first."""
    storage_path = _assert_owns_path(owner_id, storage_path)
    try:
        # missing_ok: deleting an already-absent document is the desired end
        # state, and callers treat delete as idempotent.
        _resolve(storage_path).unlink(missing_ok=True)
    except PermissionError:
        raise
    except Exception as exc:
        logger.warning("Storage delete failed for %s: %s", storage_path, exc)
        raise RuntimeError("Storage delete failed") from exc
