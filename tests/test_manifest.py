import json
import os
import shutil
from pathlib import Path

from trading_os.manifest import build_manifest, write_manifest


def _sandbox(name: str) -> Path:
    root = Path(".test-tmp") / f"trading-os-test-{name}"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    return root


def test_manifest_is_deterministic():
    tmp_path = _sandbox("deterministic")
    (tmp_path / "trades").mkdir()
    (tmp_path / "assets").mkdir()
    (tmp_path / "trades" / "a.md").write_text("---\npair: BTCUSDT\n---\n![[x.png]]", encoding="utf-8")
    (tmp_path / "assets" / "x.png").write_bytes(b"image")
    first = build_manifest(tmp_path)
    second = build_manifest(tmp_path)
    assert first == second
    markdown = next(item for item in first["entries"] if item["relative_path"] == "trades/a.md")
    assert markdown["image_references"][0]["status"] == "found"


def test_manifest_write_is_utf8_and_sorted():
    tmp_path = _sandbox("write")
    out = tmp_path / "canonical" / "manifests" / "source.json"
    write_manifest(tmp_path, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["manifest_version"] == "evidence-manifest.v1"
    assert out.read_bytes().endswith(b"\n")


def test_manifest_ignores_mtime_for_unchanged_source(tmp_path: Path):
    source = tmp_path / "trade.md"
    source.write_text("# unchanged\n", encoding="utf-8")
    first_out = tmp_path / "canonical" / "manifests" / "first.json"
    second_out = tmp_path / "canonical" / "manifests" / "second.json"
    first = write_manifest(tmp_path, first_out)
    os.utime(source, (source.stat().st_atime + 10, source.stat().st_mtime + 10))
    second = write_manifest(tmp_path, second_out)
    assert first == second
    assert first_out.read_bytes() == second_out.read_bytes()
