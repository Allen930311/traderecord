"""Durable, content-addressed binary evidence store.

Evidence attached through :mod:`trading_os.operational_lifecycle` has always
been metadata-only: ``source_ref`` is a path string the caller must already
know, with no binary store and no integrity re-hash. This module is the
missing binary layer. It deliberately does not replace or duplicate the
existing evidence *metadata* contract (``LifecycleStore.attach_evidence`` /
``CanonicalWriter.attach_evidence``) -- it only gives those callers a durable
place to put the actual bytes and a stable locator to reference them by.

Bytes live at ``<root>/canonical/evidence/artifacts/<sha256><ext>`` and are
immutable once written: the store is content-addressed, so the same bytes
ingested twice always resolve to the same file and are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import ntpath
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class EvidenceStoreError(ValueError):
    """Raised when the evidence store cannot fail closed."""


EVIDENCE_IDENTITY_REQUIRED_FIELDS = ("sha256", "locator", "type", "timing", "category")
EVIDENCE_IDENTITY_OPTIONAL_FIELDS = (
    "captured_at",
    "provenance",
    "represented_market_time",
    "role",
    "timing_relative_to_trade",
)
EVIDENCE_IDENTITY_FIELDS = EVIDENCE_IDENTITY_REQUIRED_FIELDS + EVIDENCE_IDENTITY_OPTIONAL_FIELDS
EVIDENCE_BINDING_REQUIRED_FIELDS = ("source_ref", "category", "sha256", "locator", "type", "timing")
EVIDENCE_BINDING_OPTIONAL_FIELDS = EVIDENCE_IDENTITY_OPTIONAL_FIELDS
EVIDENCE_BINDING_FIELDS = EVIDENCE_BINDING_REQUIRED_FIELDS + EVIDENCE_BINDING_OPTIONAL_FIELDS


def evidence_identity(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the immutable identity portion of one evidence projection.

    ``source_ref`` is deliberately not part of this tuple: uniqueness is
    scoped by ``(draft, category, source_ref)`` and the same durable artifact
    may therefore be linked to more than one logical category.
    """

    fields = EVIDENCE_IDENTITY_REQUIRED_FIELDS + tuple(
        field for field in EVIDENCE_IDENTITY_OPTIONAL_FIELDS if field in evidence
    )
    return tuple(
        json.dumps(
            evidence.get(field),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        for field in fields
    )


def evidence_binding(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact logical identity persisted by a verification event.

    ``source_ref`` remains a compatibility locator, while the remaining fields
    bind the verification to the immutable evidence projection that was
    resolved at verification time.  Category is intentionally included: one
    durable artifact may be linked to multiple categories, but a verification
    may not be reinterpreted through a later category link.
    """

    fields = EVIDENCE_BINDING_REQUIRED_FIELDS + tuple(
        field for field in EVIDENCE_BINDING_OPTIONAL_FIELDS if field in evidence
    )
    return {field: evidence.get(field) for field in fields}


# Media types this store can positively identify from magic bytes. Extend
# here (not by trusting the caller's extension) if more binary types are
# needed later.
_MAGIC_SNIFFERS: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
_MEDIA_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
_SAFE_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_ARTIFACT_LOCATOR_RE = re.compile(r"^artifact:(?P<sha256>[0-9a-f]{64})(?P<extension>\.[a-z0-9]{1,10})$")
_SAFE_FILENAME_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")
_WINDOWS_RESERVED_DEVICE_BASE_RE = re.compile(r"^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE)


def validate_original_filename(value: Any) -> str:
    """Validate an evidence filename label without treating it as a path.

    Executor callers use a runtime-controlled temporary basename and pass the
    Owner's original filename separately. Keep that identity exact while
    rejecting path syntax and Windows device/alternate-stream names.
    """

    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise EvidenceStoreError("original_filename must be a non-empty basename")
    if value != value.strip() or value.endswith((" ", ".")):
        raise EvidenceStoreError("original_filename contains unsafe leading/trailing whitespace or dot")
    if any(ord(char) < 32 for char in value):
        raise EvidenceStoreError("original_filename contains a control character")
    if any(char in value for char in '<>:"/\\|?*') or ntpath.splitdrive(value)[0]:
        raise EvidenceStoreError("original_filename must not contain path or alternate-stream syntax")
    device_base = value.split(".", 1)[0].rstrip(" .")
    if _WINDOWS_RESERVED_DEVICE_BASE_RE.fullmatch(device_base):
        raise EvidenceStoreError("original_filename must not be a reserved Windows device name")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize_filename(name: str) -> str:
    """Return a filename safe to store as metadata.

    Never trusted for identity or as a filesystem path -- only the basename
    survives, and every character outside a conservative allow-list is
    replaced. This is what stops a path-traversal-shaped name (``../../etc/
    passwd``, an absolute path, backslash-separated components) from ever
    reaching the filesystem as anything but an opaque label.
    """

    if not isinstance(name, str) or not name.strip():
        raise EvidenceStoreError("original filename is required")
    # Strip any path components from either separator style, then collapse
    # ``.``/``..`` specifically so a bare traversal segment cannot survive
    # as the "sanitized" name either.
    candidate = name.replace("\\", "/").split("/")[-1].strip()
    if candidate in {"", ".", ".."}:
        raise EvidenceStoreError("original filename is invalid")
    candidate = _SAFE_FILENAME_CHARS_RE.sub("_", candidate)
    candidate = candidate.lstrip(".") or "file"
    return candidate[:200]


def _detect_media_type(name: str, head: bytes) -> str:
    for magic, media_type in _MAGIC_SNIFFERS:
        if head.startswith(magic):
            return media_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def _artifact_identity(data: bytes, original_filename: str) -> tuple[str, str, str, str]:
    """Derive the exact content address without writing to the EvidenceStore."""

    if not data:
        raise EvidenceStoreError("file is empty")
    filename = validate_original_filename(original_filename)
    sha256 = hashlib.sha256(data).hexdigest()
    media_type = _detect_media_type(filename, data[:16])
    ext = _MEDIA_TYPE_EXTENSIONS.get(media_type)
    if ext is None:
        suffix = Path(filename).suffix.lower()
        ext = suffix if _SAFE_EXTENSION_RE.fullmatch(suffix) else ""
    return sha256, media_type, ext, f"artifact:{sha256}{ext}"


def evidence_locator_for_bytes(data: bytes, original_filename: str) -> str:
    """Return the locator EvidenceStore would assign, without persisting bytes."""

    return _artifact_identity(data, original_filename)[3]


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    for _ in range(200):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    raise TimeoutError(f"timed out replacing {path}")


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    for _ in range(200):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    raise TimeoutError(f"timed out replacing {path}")


class EvidenceStore:
    """Content-addressed, immutable binary evidence store."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.directory = self.root / "canonical" / "evidence" / "artifacts"

    def _artifact_path(self, sha256: str, ext: str) -> Path:
        return self.directory / f"{sha256}{ext}"

    def _metadata_path(self, sha256: str) -> Path:
        # Keep metadata separate from an application/json artifact, whose
        # content-addressed path is also ``<sha256>.json``.
        return self.directory / f"{sha256}.metadata.json"

    def describe(
        self,
        file_path: str | Path,
        *,
        original_filename: str | None = None,
    ) -> dict[str, Any]:
        """Describe the exact EvidenceStore identity without persisting bytes.

        This lets a review preview bind an image to the locator and metadata
        that ``ingest`` will produce, while keeping evidence and lifecycle
        writes behind the Owner confirmation gate.
        """
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise EvidenceStoreError(f"file does not exist: {file_path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EvidenceStoreError(f"file is unreadable: {file_path}") from exc
        if not data:
            raise EvidenceStoreError("file is empty")
        filename = (
            validate_original_filename(original_filename)
            if original_filename is not None
            else _sanitize_filename(path.name)
        )
        sha256, media_type, _ext, locator = _artifact_identity(data, filename)
        return {
            "sha256": sha256,
            "locator": locator,
            "media_type": media_type,
            "byte_size": len(data),
            "original_filename": filename,
        }

    def ingest(
        self,
        file_path: str | Path,
        *,
        original_filename: str | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Persist the exact bytes of ``file_path`` and return its locator.

        Fails closed: a missing path, an unreadable file, or a zero-byte
        file all raise :class:`EvidenceStoreError` rather than inventing
        bytes or silently skipping the ingest.
        """

        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise EvidenceStoreError(f"file does not exist: {file_path}")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise EvidenceStoreError(f"file is unreadable: {file_path}") from exc
        if len(data) == 0:
            raise EvidenceStoreError(f"file is empty: {file_path}")

        original_filename = (
            validate_original_filename(original_filename)
            if original_filename is not None
            else _sanitize_filename(path.name)
        )
        sha256, media_type, ext, locator = _artifact_identity(data, original_filename)
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise EvidenceStoreError("file SHA256 differs from the confirmed preview")

        self.directory.mkdir(parents=True, exist_ok=True)
        artifact_path = self._artifact_path(sha256, ext)
        metadata_path = self._metadata_path(sha256)
        deduped = artifact_path.exists()
        if deduped:
            # Immutable once stored: verify the bytes on disk still match
            # the hash in their own name rather than trusting the filename.
            existing = artifact_path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != sha256:
                raise EvidenceStoreError(
                    f"stored artifact does not match its own content hash: {sha256}"
                )
        else:
            _atomic_write_bytes(artifact_path, data)

        ingested_at = _now()
        legacy_metadata_path = self.directory / f"{sha256}.json"
        metadata_source = metadata_path if metadata_path.exists() else legacy_metadata_path
        existing_meta: dict[str, Any] = {}
        if metadata_source.exists() and metadata_source != artifact_path:
            try:
                candidate_meta = json.loads(metadata_source.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                candidate_meta = {}
            if (
                isinstance(candidate_meta, dict)
                and candidate_meta.get("schema_version") == "evidence-artifact.v1"
                and candidate_meta.get("sha256") == sha256
            ):
                existing_meta = candidate_meta
        if existing_meta:
            filenames = set(existing_meta.get("original_filenames", []))
            first_seen = existing_meta.get("original_filename") or original_filename
            filenames.add(first_seen)
            filenames.add(original_filename)
            merged = {
                **existing_meta,
                "schema_version": "evidence-artifact.v1",
                "sha256": sha256,
                "locator": locator,
                "media_type": existing_meta.get("media_type", media_type),
                "byte_size": len(data),
                "original_filename": first_seen,
                "original_filenames": sorted(filenames),
                "ingested_at": existing_meta.get("ingested_at", ingested_at),
            }
            _atomic_write_json(metadata_path, merged)
        else:
            _atomic_write_json(
                metadata_path,
                {
                    "schema_version": "evidence-artifact.v1",
                    "sha256": sha256,
                    "locator": locator,
                    "media_type": media_type,
                    "byte_size": len(data),
                    "original_filename": original_filename,
                    "original_filenames": [original_filename],
                    "ingested_at": ingested_at,
                },
            )

        return {
            "sha256": sha256,
            "locator": locator,
            "media_type": media_type,
            "byte_size": len(data),
            "original_filename": original_filename,
            "deduped": deduped,
        }

    def metadata(self, sha256: str) -> dict[str, Any] | None:
        path = self._metadata_path(sha256)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        # Read metadata written by the previous layout when it did not collide
        # with a JSON artifact. A raw JSON artifact is not metadata.
        legacy_path = self.directory / f"{sha256}.json"
        if not legacy_path.exists():
            return None
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(legacy, dict)
            or legacy.get("schema_version") != "evidence-artifact.v1"
            or legacy.get("sha256") != sha256
        ):
            return None
        return legacy

    def read(self, locator: str, *, expected_sha256: str | None = None) -> bytes:
        """Read an artifact after validating its address, containment, and bytes.

        Only canonical lower-case SHA-256 EvidenceStore locators resolve; legacy
        sentinels such as ``artifact:full`` are deliberately not resolvable.
        """

        if not isinstance(locator, str):
            raise EvidenceStoreError("artifact locator must be a string")
        match = _ARTIFACT_LOCATOR_RE.fullmatch(locator)
        if match is None:
            raise EvidenceStoreError("artifact locator is not a canonical content address")
        sha256 = match.group("sha256")
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise EvidenceStoreError("expected sha256 must be a lower-case 64-character digest")
            if expected_sha256 != sha256:
                raise EvidenceStoreError("artifact locator digest does not match expected sha256")

        store_dir = self.directory.resolve()
        artifact_path = (self.directory / f"{sha256}{match.group('extension')}").resolve()
        if not artifact_path.is_relative_to(store_dir):
            raise EvidenceStoreError("artifact path escapes EvidenceStore")
        if not artifact_path.is_file():
            raise EvidenceStoreError("artifact bytes are missing from EvidenceStore")
        try:
            data = artifact_path.read_bytes()
        except OSError as exc:
            raise EvidenceStoreError("artifact bytes are unreadable") from exc
        if hashlib.sha256(data).hexdigest() != sha256:
            raise EvidenceStoreError("artifact bytes do not match their content address")

        try:
            metadata = self.metadata(sha256)
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidenceStoreError("artifact metadata is unreadable") from exc
        if metadata is not None and (
            not isinstance(metadata, Mapping)
            or metadata.get("locator") != locator
            or metadata.get("byte_size") != len(data)
        ):
            raise EvidenceStoreError("artifact metadata does not match the stored bytes")
        return data
