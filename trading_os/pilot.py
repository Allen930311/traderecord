from __future__ import annotations

import json
import sys
from pathlib import Path


def _migration_package(root: Path):
    """Load the already-approved migration substrate without copying its logic."""

    scripts = root.resolve() / "scripts"
    if not scripts.is_dir():
        raise ValueError(f"canonical migration scripts not found under {root}")
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from canonical_migration.pipeline import run_pilot as established_run_pilot

    manifest = scripts / "canonical_migration" / "fixtures" / "representative-fixtures.json"
    return established_run_pilot, manifest


def run_pilot(root: Path, output: Path) -> dict:
    """Run the normative 15-fixture pilot and emit a deterministic aggregate.

    Gate C deliberately delegates to the previously reviewed migration substrate.
    A fixture is never considered successful merely because a source is missing.
    """

    root = root.resolve()
    output = output.resolve()
    established_run_pilot, manifest = _migration_package(root)
    result = established_run_pilot(root, manifest, output.parent)
    if len(result["fixtures"]) != 15:
        raise ValueError("Gate C requires exactly 15 normative fixtures")
    if not result["validation"]["ok"]:
        raise ValueError("Gate C fixture validation failed")
    if not all(item["checks_passed"] for item in result["fixtures"]):
        raise ValueError("Gate C golden assertions failed")
    if any(record.get("trade_no") is not None for record in result["normalized_candidates"]):
        raise ValueError("Gate C must not allocate permanent trade numbers")
    # Absolute output locations are execution metadata, not canonical pilot truth.
    # Normalize them so identical inputs produce identical JSON regardless of temp dir.
    if "report_path" in result:
        result["report_path"] = "pilot-report.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result
