from pathlib import Path
from trading_os.dryrun import run_dry_run

def test_dry_run_is_non_numbering_and_covers_sources(tmp_path: Path):
    root=Path.cwd()/"20_Project/tradingdiary" if (Path.cwd()/"20_Project/tradingdiary").is_dir() else Path.cwd()
    result=run_dry_run(root, Path(".test-tmp/dry-run.json"))
    assert result["source_count"] > 0
    assert result["canonical_count"] == 0
    assert result["trade_numbers_allocated"] is False
    assert result["coverage"]["exactly_once"] is True
    assert "adjudicated_canonical" in set(result["coverage"]["dispositions"].values())
    assert "duplicate_source" in set(result["coverage"]["dispositions"].values())
