"""Read-only repository cleanup census.

This is deliberately *not* a deletion runner. A historical source is not safe
to remove just because it lacks an image or appears as a Trade View performance
episode. Every physical deletion still requires exact-path approval, references,
original evidence preservation and an independently reviewable Git rollback.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess

LEGACY_FOLDERS = ("trades/", "assets/")
CANONICAL_EVIDENCE = "canonical/evidence/artifacts/"
TEXT_EXTENSIONS = {".md", ".json", ".jsonl", ".yaml", ".yml", ".py", ".js", ".html", ".txt"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".svg"}


def git_tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root, check=True, capture_output=True,
    )
    return sorted(part.decode("utf-8") for part in result.stdout.split(b"\x00") if part)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _origin(path: str) -> str | None:
    if path.startswith("trades/"):
        return "trades"
    if path.startswith("assets/"):
        return "assets"
    if path.startswith(CANONICAL_EVIDENCE):
        return "canonical_evidence"
    return None


def _incoming_refs(root: Path, text_paths: list[str], targets: list[str]) -> dict[str, list[str]]:
    """A conservative *positive-only* reference scan; empty != proven unreferenced."""
    result: dict[str, list[str]] = {p: [] for p in targets}
    by_basename: dict[str, list[str]] = defaultdict(list)
    for p in targets:
        by_basename[Path(p).name].append(p)
    for source in text_paths:
        path = root / source
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for base, paths in by_basename.items():
            if base not in text:
                continue
            for target in paths:
                if source != target and source not in result[target]:
                    result[target].append(source)
    return {p: sorted(items) for p, items in result.items()}


def build_cleanup_census(root: Path, tracked_paths: list[str]) -> dict:
    """Compute identity/evidence inventory without writing any repository file."""
    root = root.resolve()
    tracked = sorted(p for p in tracked_paths if (root / p).is_file())
    corpus = {kind: {"files": 0, "bytes": 0, "images": 0}
              for kind in ("trades", "assets", "canonical_evidence")}
    by_digest: dict[str, list[str]] = defaultdict(list)
    for p in tracked:
        kind = _origin(p)
        if kind is None:
            continue
        file_path = root / p
        corpus[kind]["files"] += 1
        corpus[kind]["bytes"] += file_path.stat().st_size
        if file_path.suffix.lower() in IMAGE_EXTENSIONS:
            corpus[kind]["images"] += 1
        by_digest[_digest(file_path)].append(p)

    repeated: list[dict] = []
    exact_in_evidence: list[dict] = []
    for sha, paths in sorted(by_digest.items()):
        for kind in ("trades", "assets"):
            same = sorted(p for p in paths if _origin(p) == kind)
            if len(same) > 1:
                repeated.append({
                    "sha256": sha, "folder": kind, "paths": same,
                    "redundant_copies": len(same) - 1,
                })
        canonical = sorted(p for p in paths if _origin(p) == "canonical_evidence")
        legacy = sorted(p for p in paths if _origin(p) in ("trades", "assets"))
        if canonical and legacy:
            exact_in_evidence.append({
                "sha256": sha, "legacy_paths": legacy,
                "canonical_evidence_paths": canonical,
            })

    target_paths = sorted(set(
        p for group in repeated for p in group["paths"]
    ) | set(
        p for group in exact_in_evidence for p in group["legacy_paths"]
    ))
    references = _incoming_refs(
        root,
        [p for p in tracked if Path(p).suffix.lower() in TEXT_EXTENSIONS],
        target_paths,
    )
    for group in repeated:
        group["found_inbound_references"] = {
            p: references.get(p, [])[:12] for p in group["paths"]
        }
    for group in exact_in_evidence:
        group["found_inbound_references"] = {
            p: references.get(p, [])[:12] for p in group["legacy_paths"]
        }

    protected_trades = None
    finalizations = root / "canonical/sequences/finalizations.json"
    if finalizations.exists():
        protected_trades = len(json.loads(finalizations.read_text(encoding="utf-8")).get("records", {}))

    view = root / "exports/operational/trade-view/trade-view-data.json"
    historical_rows = None
    historical_with_images = None
    if view.exists():
        rows = json.loads(view.read_text(encoding="utf-8")).get("rows", [])
        historical = [r for r in rows if r.get("status") == "historical_seed"]
        historical_rows = len(historical)
        historical_with_images = sum(bool(r.get("evidence_gallery")) for r in historical)

    return {
        "schema_version": "cleanup-read-only-census.v1",
        "corpus": corpus,
        "identical_legacy_duplicate_groups": repeated,
        "exact_legacy_bytes_already_in_canonical_evidence": exact_in_evidence,
        "protected_finalized_trade_count": protected_trades,
        "trade_view_historical_seed_rows": historical_rows,
        "trade_view_historical_seed_rows_with_gallery": historical_with_images,
        "automatically_deletable_paths": [],
        "status": "REVIEW_REQUIRED_NO_MUTATION",
        "limitations": [
            "git tracked tree only; no local untracked files",
            "textual reference scan finds positive links but cannot prove absence",
            "equal bytes do not imply equivalent transaction identity or source role",
            "Trade View is a derived projection, not preservation of original narrative",
            "physical deletion requires exact-path authority and proof of rollback",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_cleanup_census(args.root, git_tracked_paths(args.root))
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("Cleanup census: REVIEW_REQUIRED_NO_MUTATION")
        for folder, values in report["corpus"].items():
            print(f"  {folder}: {values['files']} files, {values['images']} images")
        print(f"  identical duplicate groups: {len(report['identical_legacy_duplicate_groups'])}")
        print(f"  exact canonical byte matches: {len(report['exact_legacy_bytes_already_in_canonical_evidence'])}")
        print("  deleted: 0 (no deletion functionality)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
