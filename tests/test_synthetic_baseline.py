"""The committed synthetic baseline must equal what the generator produces."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import make_synthetic_baseline as gen  # noqa: E402


def test_committed_baseline_matches_generator(tmp_path):
    import shutil
    shutil.copytree(ROOT / "registry", tmp_path / "registry")
    gen.build(tmp_path)
    for path in sorted(tmp_path.rglob("*")):
        if path.is_file() and "registry" not in path.relative_to(tmp_path).parts:
            rel = path.relative_to(tmp_path)
            assert (ROOT / rel).read_bytes() == path.read_bytes(), f"{rel} is stale: rerun examples/make_synthetic_baseline.py"
