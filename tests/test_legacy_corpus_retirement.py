"""The retired corpus stays complete, exact and Owner-approved.

Two Owner-approved waves on 2026-10-12 removed directories from the working
tree after every file was preserved byte-for-byte in the EvidenceStore behind
the append-only ledger canonical/legacy/legacy-path-ledger.jsonl:

* wave 1 — trades/, assets/, reviews/ (451 files);
* wave 2 — _reports/, exports/phase4c|4d|4e, ingestion/pilot, ingestion/reports
  (139 files). exports/strategy-discovery, method-validation and trader-dna are
  runtime inputs and stay.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from trading_os.legacy_source import (
    ENGINEERING_DIRS, LEDGER, LEGACY_DIRS, RETIRED_ROOTS, ledger_index, list_legacy_files,
    read_legacy_bytes, verify_ledger,
)

ROOT = Path(__file__).resolve().parents[1]
WAVES = {
    "wave1": ("canonical/legacy/approval-2026-10-12.json",
              "canonical/legacy/approved-manifest-2026-10-12.json", LEGACY_DIRS, 451),
    "wave2": ("canonical/legacy/approval-2026-10-12-wave2.json",
              "canonical/legacy/approved-manifest-2026-10-12-wave2.json", ENGINEERING_DIRS, 139),
}


def test_retired_directories_are_gone_and_runtime_datasets_stay():
    for name in RETIRED_ROOTS:
        assert not (ROOT / name).exists(), name
    for kept in ("exports/strategy-discovery", "exports/method-validation", "exports/trader-dna",
                 "ingestion/intake", "ingestion/propw"):
        assert (ROOT / kept).is_dir(), kept


@pytest.mark.parametrize("wave", sorted(WAVES))
def test_each_wave_matches_its_exact_owner_approved_manifest(wave):
    approval_path, manifest_path, dirs, count = WAVES[wave]
    approval = json.loads((ROOT / approval_path).read_text(encoding="utf-8"))
    assert approval["decision"] == "approved" and approval["actor"] == "example-owner"
    assert approval["owner_answer_verbatim"].strip()
    assert approval["scope"]["directories"] == list(dirs)
    manifest = json.loads((ROOT / manifest_path).read_text(encoding="utf-8"))
    compact = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(compact).hexdigest() == approval["approved_manifest_sha256"]
    assert len(manifest) == approval["scope"]["file_count"] == count
    rows = [row for row in ledger_index(ROOT).values() if row["approval_ref"].startswith(approval_path)]
    assert {row["legacy_path"]: row["sha256"] for row in rows} == {m["legacy_path"]: m["sha256"] for m in manifest}
    assert all(row["source_commit"] == approval["scope"]["source_commit"] for row in rows)


def test_ledger_is_append_only_one_row_per_path():
    lines = [line for line in (ROOT / LEDGER).read_text(encoding="utf-8").splitlines() if line.strip()]
    paths = [json.loads(line)["legacy_path"] for line in lines]
    assert len(paths) == len(set(paths)) == sum(w[3] for w in WAVES.values()) == 590


def test_every_preserved_file_reads_back_byte_exact():
    result = verify_ledger(ROOT)
    assert result["checked"] == 590 and result["failures"] == []


def test_listing_and_reading_work_without_the_directories():
    assert len(list_legacy_files(ROOT, "trades")) == 267
    assert len(list_legacy_files(ROOT, "assets")) == 175
    assert len(list_legacy_files(ROOT, "reviews")) == 9
    assert list_legacy_files(ROOT, "ingestion/pilot")
    sample = read_legacy_bytes(ROOT, "trades/2026/08/2026-08-24-BTC.md")
    assert hashlib.sha256(sample).hexdigest() == ledger_index(ROOT)["trades/2026/08/2026-08-24-BTC.md"]["sha256"]


def test_trades_guard_still_rejects_anything_but_an_exact_retirement():
    from legacy_test_support import unpreserved_legacy_changes
    base = "origin/main"
    # A path the ledger never preserved, or one whose base bytes differ, is a mutation.
    assert unpreserved_legacy_changes(base, ["trades/never-existed.md"]) == ["trades/never-existed.md"]
    assert unpreserved_legacy_changes("HEAD", ["trades/2026/08/2026-08-24-BTC.md"]) == ["trades/2026/08/2026-08-24-BTC.md"]
    # Non-trades paths are outside this guard.
    assert unpreserved_legacy_changes(base, ["README.md"]) == []
