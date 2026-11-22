"""Cleanup safety regression: no deletion, no missing-evidence shortcuts."""
from pathlib import Path

from scripts.repo_cleanup_audit import build_cleanup_census


def test_census_preserves_missing_chart_trades_and_reports_exact_duplicate_bytes(tmp_path: Path):
    paths = [
        "trades/2023/a.md",
        "trades/2023/a_copy.md",
        "trades/2023/no_image.md",
        "assets/entry.png",
        "assets/entry_copy.png",
        "canonical/evidence/artifacts/entry.png",
        "notes.md",
    ]
    for path in paths:
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        if path.endswith("a.md") or path.endswith("a_copy.md"):
            p.write_text("setup information only\n", encoding="utf-8")
        elif path.endswith("no_image.md"):
            p.write_text("this text has no chart\n", encoding="utf-8")
        elif path.endswith(".png"):
            p.write_bytes(b"original-image")
        else:
            p.write_text("![[entry.png]]\n", encoding="utf-8")
    report = build_cleanup_census(tmp_path, paths)
    assert report["status"] == "REVIEW_REQUIRED_NO_MUTATION"
    assert report["automatically_deletable_paths"] == []
    groups = report["identical_legacy_duplicate_groups"]
    assert len(groups) == 2
    assert {g["folder"] for g in groups} == {"trades", "assets"}
    assert report["exact_legacy_bytes_already_in_canonical_evidence"][0]["legacy_paths"] == [
        "assets/entry.png", "assets/entry_copy.png"
    ]
    assert "notes.md" in next(g for g in groups if g["folder"] == "assets")["found_inbound_references"]["assets/entry.png"]
    assert (tmp_path / "trades/2023/no_image.md").exists()


def test_same_names_different_bytes_are_not_deletable(tmp_path: Path):
    paths = ["trades/a.md", "trades/b.md", "assets/chart.png", "canonical/evidence/artifacts/chart.png"]
    for n, path in enumerate(paths):
        f = tmp_path / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(f"unique content {n}".encode())
    report = build_cleanup_census(tmp_path, paths)
    assert report["identical_legacy_duplicate_groups"] == []
    assert report["exact_legacy_bytes_already_in_canonical_evidence"] == []
    assert report["automatically_deletable_paths"] == []
