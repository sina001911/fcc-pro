"""Filesystem-backed artifact store with manifest and lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from free_claude_code.application.errors import InvalidRequestError

from .links import SignedLink, mint_link

_MAX_ARTIFACT_ID_HEX = 32
_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One persisted artifact and its metadata."""

    artifact_id: str
    path: Path
    filename: str
    content_type: str
    size_bytes: int
    created_at: float
    expires_at: float
    kind: str = "raw"
    source_format: str | None = None
    metadata: dict[str, object] | None = field(default=None)


class ArtifactStore:
    """Persists artifacts on disk under a root directory with a manifest."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 10 * 1024 * 1024,
        configured: bool = False,
    ) -> None:
        self._root = root
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = asyncio.Lock()
        self._root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._root / "manifest.json"
        self._records = self._load_manifest()
        self._purge_expired(time.time())
        # I-4: only validate explicitly-configured roots; the default
        # ~/.fcc/artifacts inherits its parent's ACLs and test temp dirs are
        # never flagged.
        self._root_is_safe = self._check_root_safe() if configured else True

    @property
    def root(self) -> Path:
        return self._root

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def _safe_artifact_dir(self, artifact_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F]{32}", artifact_id):
            raise InvalidRequestError("Unknown artifact id.")
        path = (self._root / artifact_id).resolve()
        root = self._root.resolve()
        if not path.is_relative_to(root):
            raise InvalidRequestError("Artifact path escapes storage root.")
        return path

    def _check_root_safe(self) -> bool:
        """Refuse to persist artifacts in a world-writable location.

        Read-only: inspects POSIX mode bits and a cross-platform denylist of
        well-known world-writable locations. Never changes ACLs, never runs
        icacls. Returns True when the root is safe or cannot be inspected.

        On Windows, mode bits are meaningless (every directory reports 0o777;
        ACLs, not mode bits, control access), so only the denylist applies
        there. That is a documented limitation: for full ACL assurance on
        Windows, point ARTIFACT_DIR at a directory whose ACLs you control
        (e.g. ``~/.fcc/artifacts`` is the safe default).
        """
        try:
            root = self._root.resolve()
        except OSError:
            return True
        # POSIX only: on Windows st_mode reports 0o777 for every directory
        # regardless of actual ACLs, so the bit check would reject everything.
        if os.name == "posix":
            try:
                mode = root.stat().st_mode
            except OSError:
                return True
            if mode & 0o022:  # group or other write bits
                return False
        # Cross-platform denylist of well-known world-writable locations.
        s = str(root).lower()
        for bad in ("/tmp/", "/var/tmp/", "/dev/shm", "\\temp\\", "\\users\\public\\"):
            if bad in s:
                return False
        return True

    def _load_manifest(self) -> dict[str, ArtifactRecord]:
        if not self._manifest_path.exists():
            return {}
        try:
            data = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        records: dict[str, ArtifactRecord] = {}
        for artifact_id, entry in data.get("artifacts", {}).items():
            try:
                records[artifact_id] = ArtifactRecord(
                    artifact_id=artifact_id,
                    path=Path(entry["path"]),
                    filename=entry["filename"],
                    content_type=entry.get("content_type") or "application/octet-stream",
                    size_bytes=int(entry["size_bytes"]),
                    created_at=float(entry["created_at"]),
                    expires_at=float(entry["expires_at"]),
                    kind=entry.get("kind", "raw"),
                    source_format=entry.get("source_format"),
                    metadata=entry.get("metadata"),
                )
            except (KeyError, TypeError, ValueError):
                continue
        return records

    def _write_manifest(self, records: dict[str, ArtifactRecord]) -> None:
        payload = {
            "version": 1,
            "artifacts": {
                artifact_id: {
                    "path": str(record.path),
                    "filename": record.filename,
                    "content_type": record.content_type,
                    "size_bytes": record.size_bytes,
                    "created_at": record.created_at,
                    "expires_at": record.expires_at,
                    "kind": record.kind,
                    "source_format": record.source_format,
                    "metadata": record.metadata,
                }
                for artifact_id, record in records.items()
            },
        }
        tmp = self._manifest_path.with_name("manifest.json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, self._manifest_path)

    def _purge_expired(self, now: float) -> int:
        removed = 0
        for artifact_id, record in list(self._records.items()):
            if now < record.expires_at:
                continue
            try:
                path = record.path.resolve()
                if path.is_file():
                    path.unlink()
                path.parent.rmdir()
            except OSError:
                pass
            self._records.pop(artifact_id, None)
            removed += 1
        if removed:
            self._write_manifest(self._records)
        return removed

    async def store(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        kind: str = "raw",
        source_format: str | None = None,
        metadata: dict[str, object] | None = None,
        ttl_seconds: int,
        signing_key: bytes,
        now: float | None = None,
    ) -> tuple[ArtifactRecord, SignedLink]:
        """Persist one artifact atomically and mint its signed download link."""
        if not isinstance(data, (bytes, bytearray)):
            raise InvalidRequestError("Artifact content must be bytes.")
        if len(data) > self._max_bytes:
            raise InvalidRequestError(
                f"Artifact exceeds maximum size of {self._max_bytes} bytes."
            )
        if not self._root_is_safe:
            raise InvalidRequestError(
                "Artifact storage directory must not be writable by group or others."
            )
        # I-2: reject non-serializable metadata BEFORE touching the filesystem,
        # so a bad manifest cannot orphan an artifact file on disk.
        _ensure_json_safe_metadata(metadata)
        safe_filename = _sanitize_filename(filename)
        content_type = _sanitize_content_type(content_type)
        current = time.time() if now is None else now
        async with self._lock:
            artifact_id = secrets.token_hex(_MAX_ARTIFACT_ID_HEX // 2)
            while artifact_id in self._records:
                artifact_id = secrets.token_hex(_MAX_ARTIFACT_ID_HEX // 2)
            artifact_dir = self._safe_artifact_dir(artifact_id)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            file_path = artifact_dir / safe_filename
            tmp_path = artifact_dir / (safe_filename + ".part")
            try:
                tmp_path.write_bytes(bytes(data))
                os.replace(tmp_path, file_path)
                expires_at = current + max(1, int(ttl_seconds))
                record = ArtifactRecord(
                    artifact_id=artifact_id,
                    path=file_path,
                    filename=safe_filename,
                    content_type=content_type,
                    size_bytes=len(data),
                    created_at=current,
                    expires_at=expires_at,
                    kind=kind,
                    source_format=source_format,
                    metadata=metadata,
                )
                self._records[artifact_id] = record
                self._write_manifest(self._records)
            except BaseException:
                # I-2: roll back ONLY the artifact we just created. Remove the
                # partial file, the temp file and the per-artifact directory,
                # and drop the in-memory record. Pre-existing artifacts and
                # manifest entries are untouched; the atomic manifest replace is
                # preserved (it either happened or did not). Cleanup failures
                # are swallowed so the original exception is never masked.
                for p in (tmp_path, file_path):
                    try:
                        if p.exists():
                            p.unlink()
                    except OSError:
                        pass
                try:
                    artifact_dir.rmdir()
                except OSError:
                    pass
                self._records.pop(artifact_id, None)
                raise
        link = mint_link(signing_key, artifact_id, ttl_seconds=ttl_seconds, now=current)
        return record, link

    async def retrieve(
        self,
        artifact_id: str,
        token: str,
        *,
        signing_key: bytes,
        now: float | None = None,
    ) -> ArtifactRecord | None:
        """Return the record if its signed link is authentic and unexpired."""
        if not self._root_is_safe:
            raise InvalidRequestError(
                "Artifact storage directory must not be writable by group or others."
            )
        from .links import validate_link

        check = validate_link(signing_key, token, artifact_id, now=now)
        if check.link is None:
            return None
        async with self._lock:
            record = self._records.get(artifact_id)
        if record is None:
            return None
        try:
            path = record.path.resolve()
        except OSError:
            return None
        root = self._root.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return record

    async def cleanup(self, *, now: float | None = None) -> int:
        """Remove expired artifacts and rewrite the manifest."""
        current = time.time() if now is None else now
        async with self._lock:
            return self._purge_expired(current)

    def get(self, artifact_id: str) -> ArtifactRecord | None:
        return self._records.get(artifact_id)

    def list_records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(self._records.values())


def _ensure_json_safe_metadata(metadata: object) -> None:
    """Reject metadata that cannot be serialized to the manifest.

    Called BEFORE any filesystem write so a serialization failure cannot
    leave an orphaned artifact file behind (I-2).
    """
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        raise InvalidRequestError("Artifact metadata must be a JSON object.")
    try:
        json.dumps(metadata, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise InvalidRequestError(
            "Artifact metadata must be JSON-serializable."
        ) from exc


def _sanitize_filename(filename: object) -> str:
    if not isinstance(filename, str) or not filename.strip():
        return "artifact.bin"
    cleaned = _FILENAME_RE.sub("_", os.path.basename(filename)).strip("._")
    if not cleaned:
        return "artifact.bin"
    return cleaned[:200]


def _sanitize_content_type(content_type: object) -> str:
    if not isinstance(content_type, str) or not content_type.strip():
        return "application/octet-stream"
    cleaned = content_type.strip().split(";")[0].strip().lower()
    if not cleaned or " " in cleaned or len(cleaned) > 128:
        return "application/octet-stream"
    return cleaned


__all__ = ["ArtifactRecord", "ArtifactStore"]