import hashlib
import json
from pathlib import Path

from trading_os.pilot import run_pilot


def _protected_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for directory in ("trades", "assets", "reviews")
        for path in sorted((root / directory).rglob("*"))
        if path.is_file()
    }


def _root() -> Path:
    cwd = Path.cwd()
    return cwd / "20_Project/tradingdiary" if (cwd / "20_Project/tradingdiary").is_dir() else cwd


def test_pilot_passes_all_normative_fixtures_without_mutating_legacy():
    out = Path(".test-tmp/pilot/pilot.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    root = _root()
    before = _protected_hashes(root)
    result = run_pilot(root, out)
    assert len(result["fixtures"]) == 15
    assert result["validation"]["ok"] is True
    assert all(item["checks_passed"] for item in result["fixtures"])
    assert all(item["trade_no"] is None for item in result["normalized_candidates"])
    assert before == _protected_hashes(root)


def test_pilot_is_byte_stable():
    root = _root()
    first = Path(".test-tmp/pilot-first/pilot.json")
    second = Path(".test-tmp/pilot-second/pilot.json")
    run_pilot(root, first)
    run_pilot(root, second)
    assert json.loads(first.read_text(encoding="utf-8")) == json.loads(second.read_text(encoding="utf-8"))
