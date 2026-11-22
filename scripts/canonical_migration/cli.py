"""Command-line entry point for the Issue #3 fixture pilot."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .pipeline import run_pilot


def main(argv: list[str] | None = None) -> int:
    package_dir = Path(__file__).resolve().parent
    project_root = package_dir.parents[1]
    parser = argparse.ArgumentParser(description="Run the deterministic Issue #3 15-fixture canonical migration pilot.")
    parser.add_argument("--project-root", type=Path, default=project_root, help="tradingdiary project root")
    parser.add_argument("--manifest", type=Path, default=package_dir / "fixtures" / "representative-fixtures.json")
    parser.add_argument("--output-dir", type=Path, required=True, help="disposable output directory")
    parser.add_argument("--report", type=Path, help="human-readable report path (defaults inside output-dir)")
    args = parser.parse_args(argv)
    try:
        run = run_pilot(args.project_root, args.manifest, args.output_dir, args.report)
    except Exception as exc:
        print(f"pilot failed: {exc}", file=sys.stderr)
        return 2
    checks_ok = all(item["checks_passed"] for item in run["fixtures"])
    summary = {
        "validation_ok": run["validation"]["ok"],
        "fixture_checks_ok": checks_ok,
        "fixture_count": len(run["fixtures"]),
        "candidate_count": len(run["normalized_candidates"]),
        "derived_count": len(run["derived_canonical"]),
        "report_path": run["report_path"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["validation_ok"] and checks_ok else 1
