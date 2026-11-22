"""Retired legacy corpus (``trades/``, ``assets/``, ``reviews/``) — byte-exact access.

The Owner approved on 2026-10-12 (see ``canonical/legacy/approval-2026-10-12.json``)
retiring the legacy directories after every file was preserved byte-for-byte in
the single content-addressed EvidenceStore. The append-only ledger
``canonical/legacy/legacy-path-ledger.jsonl`` maps each original repository
path to its SHA-256 and EvidenceStore locator, so historical references that
name a legacy path (adjudications, B1 v3 sources, provenance strings) remain
resolvable without rewriting any historical record.

``read_legacy_bytes`` is the only reader code should use for a legacy path: it
returns the working-tree file while it still exists, otherwise the preserved
bytes from the EvidenceStore, verified against the ledger SHA-256.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from .evidence_store import EvidenceStore

LEGACY_DIRS = ("trades", "assets", "reviews")
# Wave 2 (Owner-approved 2026-10-12): engineering/migration provenance records.
ENGINEERING_DIRS = (
    "_reports", "exports/phase4c", "exports/phase4d", "exports/phase4e",
    "ingestion/pilot", "ingestion/reports",
)
RETIRED_ROOTS = LEGACY_DIRS + ENGINEERING_DIRS
LEDGER = Path("canonical/legacy/legacy-path-ledger.jsonl")
LEDGER_SCHEMA = "legacy-path-ledger.v1"


class LegacySourceError(FileNotFoundError):
    """A legacy path is neither on disk nor preserved in the ledger."""


def _retired_root(rel: str) -> str | None:
    for root in sorted(RETIRED_ROOTS, key=len, reverse=True):
        if rel == root or rel.startswith(root + "/"):
            return root
    return None


def is_legacy_path(rel: str | Path) -> bool:
    return _retired_root(_norm(rel)) is not None


def _norm(rel: str | Path) -> str:
    return Path(str(rel).replace("\\", "/")).as_posix()


@lru_cache(maxsize=8)
def _ledger_index(ledger_path: str, mtime_ns: int) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    with open(ledger_path, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("schema_version") != LEDGER_SCHEMA:
                raise LegacySourceError(f"unexpected ledger row schema: {row.get('schema_version')}")
            # Append-only: a later row for the same path would be a correction.
            index[row["legacy_path"]] = row
    return index


def ledger_index(root: Path) -> dict[str, dict[str, Any]]:
    path = Path(root) / LEDGER
    if not path.is_file():
        return {}
    return _ledger_index(str(path.resolve()), path.stat().st_mtime_ns)


def legacy_exists(root: Path, rel: str | Path) -> bool:
    rel = _norm(rel)
    return (Path(root) / rel).is_file() or rel in ledger_index(root)


def read_legacy_bytes(root: Path, rel: str | Path) -> bytes:
    """Exact original bytes of a legacy repository path."""
    root = Path(root)
    rel = _norm(rel)
    path = root / rel
    if path.is_file():
        return path.read_bytes()
    row = ledger_index(root).get(rel)
    if row is None:
        raise LegacySourceError(f"legacy path is neither on disk nor in the ledger: {rel}")
    data = EvidenceStore(root).read(row["locator"], expected_sha256=row["sha256"])
    if hashlib.sha256(data).hexdigest() != row["sha256"] or len(data) != row["byte_size"]:
        raise LegacySourceError(f"preserved bytes differ from the ledger for {rel}")
    return data


def list_legacy_files(root: Path, prefix: str = "trades") -> list[str]:
    """Repository-relative legacy files under ``prefix``, from disk or the ledger."""
    root = Path(root)
    prefix = _norm(prefix).rstrip("/")
    base = root / prefix
    if base.is_dir():
        return sorted(p.relative_to(root).as_posix() for p in base.rglob("*") if p.is_file())
    return sorted(rel for rel in ledger_index(root) if rel == prefix or rel.startswith(prefix + "/"))


def legacy_dir_exists(root: Path, prefix: str) -> bool:
    return bool(list_legacy_files(root, prefix))


def read_legacy_text(root: Path, rel: str | Path, encoding: str = "utf-8") -> str:
    return read_legacy_bytes(root, rel).decode(encoding)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout


def migrate_legacy_corpus(root: Path, *, recorded_at: str, approval_ref: str,
                          dirs: tuple[str, ...] = LEGACY_DIRS) -> dict[str, Any]:
    """Preserve every tracked legacy file in the EvidenceStore and append ledger rows.

    Idempotent: paths already in the ledger with identical bytes are skipped.
    Bytes come from the committed Git blob and must equal the working tree, so
    nothing is ever taken from an uncommitted edit.
    """
    root = Path(root)
    source_commit = _git(root, "rev-parse", "HEAD").strip()
    tracked = sorted(p for p in _git(root, "ls-files", "-z", "--", *dirs).split("\0") if p)
    existing = ledger_index(root)
    store = EvidenceStore(root)
    tmp_dir = root / ".git" / "legacy-migration-tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    rows, skipped = [], 0
    try:
        for rel in tracked:
            data = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"HEAD:{rel}"],
                                  check=True, capture_output=True).stdout
            if (root / rel).read_bytes() != data:
                raise LegacySourceError(f"working tree differs from committed bytes: {rel}")
            sha = hashlib.sha256(data).hexdigest()
            prior = existing.get(rel)
            if prior is not None:
                if prior["sha256"] != sha:
                    raise LegacySourceError(f"ledger already binds different bytes to {rel}")
                skipped += 1
                continue
            if not data:
                raise LegacySourceError(f"empty legacy file cannot be preserved as evidence: {rel}")
            staged = tmp_dir / ("blob" + Path(rel).suffix.lower())
            staged.write_bytes(data)
            label = "".join(c if c.isascii() and c not in '<>:"/\\|?*' else "_" for c in Path(rel).name).strip(" .") or "legacy"
            details = store.ingest(staged, original_filename=label, expected_sha256=sha)
            if store.read(details["locator"], expected_sha256=sha) != data:
                raise LegacySourceError(f"EvidenceStore cannot read back {rel}")
            blob = _git(root, "rev-parse", f"HEAD:{rel}").strip()
            rows.append({
                "schema_version": LEDGER_SCHEMA,
                "legacy_path": rel,
                "kind": _retired_root(rel) or rel.split("/", 1)[0],
                "sha256": sha,
                "byte_size": len(data),
                "locator": details["locator"],
                "git_blob_sha": blob,
                "source_commit": source_commit,
                "recorded_at": recorded_at,
                "approval_ref": approval_ref,
            })
    finally:
        for leftover in tmp_dir.glob("*"):
            leftover.unlink()
        tmp_dir.rmdir()
    ledger = root / LEDGER
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with open(ledger, "a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return {"tracked": len(tracked), "appended": len(rows), "already_preserved": skipped,
            "source_commit": source_commit}


def verify_ledger(root: Path, paths: Iterable[str] | None = None) -> dict[str, Any]:
    """Re-read every preserved legacy file from the EvidenceStore and re-hash it."""
    root = Path(root)
    index = ledger_index(root)
    targets = sorted(index) if paths is None else sorted(_norm(p) for p in paths)
    store = EvidenceStore(root)
    bad = []
    for rel in targets:
        row = index.get(rel)
        if row is None:
            bad.append((rel, "missing from ledger")); continue
        data = store.read(row["locator"], expected_sha256=row["sha256"])
        if hashlib.sha256(data).hexdigest() != row["sha256"] or len(data) != row["byte_size"]:
            bad.append((rel, "bytes differ"))
    return {"checked": len(targets), "failures": bad}
