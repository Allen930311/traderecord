"""Read the retired legacy corpus (trades/, assets/, reviews/) in tests.

Those directories now live byte-for-byte in the EvidenceStore behind
canonical/legacy/legacy-path-ledger.jsonl; tests read them the same way the
runtime does, and can materialize them into a temporary fixture repository.
"""
from __future__ import annotations

from pathlib import Path

from trading_os.legacy_source import list_legacy_files, read_legacy_bytes

REPO_ROOT = Path(__file__).resolve().parents[1]


def legacy_bytes(relative: str) -> bytes:
    return read_legacy_bytes(REPO_ROOT, relative)


def legacy_text(relative: str) -> str:
    return legacy_bytes(relative).decode("utf-8")


def materialize_legacy(destination: Path, prefix: str = "trades") -> list[str]:
    """Write the preserved legacy files under ``prefix`` into a fixture root."""
    written = []
    for rel in list_legacy_files(REPO_ROOT, prefix):
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(read_legacy_bytes(REPO_ROOT, rel))
        written.append(rel)
    return written


def unpreserved_legacy_changes(base: str, changed: list[str]) -> list[str]:
    """Changed trades/ paths that are NOT an exact, ledger-preserved retirement.

    The Owner-approved retirement (canonical/legacy/approval-2026-10-12.json)
    removed trades/ from the working tree. A diff entry is acceptable only if
    the file is gone at HEAD and the ledger binds exactly the bytes it had at
    ``base``; any edit, addition or byte mismatch is still a mutation.
    """
    import hashlib
    import subprocess

    from trading_os.legacy_source import ledger_index

    index = ledger_index(REPO_ROOT)
    bad = []
    for path in changed:
        if not path.startswith("trades/"):
            continue
        if (REPO_ROOT / path).exists():
            bad.append(path)  # still present yet changed: a real edit
            continue
        before = subprocess.run(["git", "show", f"{base}:{path}"], cwd=REPO_ROOT, capture_output=True)
        row = index.get(path)
        if before.returncode != 0 or row is None or hashlib.sha256(before.stdout).hexdigest() != row["sha256"]:
            bad.append(path)
    return bad
