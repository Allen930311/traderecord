"""One underlying is one statistics bucket, however a source spelled the symbol."""
import pytest

from trading_os.instruments import base_asset
from trading_os.operational_analytics import _metrics


@pytest.mark.parametrize("raw, expected", [
    ("BTC", "BTC"),
    ("BTCUSDT", "BTC"),
    ("BTCUSDCP", "BTC"),
    ("btc/usdt", "BTC"),
    ("ETH-USDC", "ETH"),
    ("BTCUSDT.P", "BTC"),
    ("SOLUSDTPERP", "SOL"),
    ("BTC/USDT PERPETUAL", "BTC"),
    ("BTC/USDT Perpetual", "BTC"),
    ("BTCUSDT.PERP", "BTC"),
    ("BTCUSD", "BTCUSD"),
    ("1000SHIB", "1000SHIB"),
    ("XAUUSDT", "XAU"),
    ("EURUSD", "EURUSD"),
    ("NAS100", "NAS100"),
    ("USDT", "USDT"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_base_asset(raw, expected):
    assert base_asset(raw) == expected


def test_concentration_counts_spellings_of_one_underlying_together():
    rows = [
        {"counted": True, "realized_r": 1.0, "instrument": symbol, "event_date": f"2026-01-0{day}"}
        for day, symbol in enumerate(["BTC", "BTCUSDT", "BTCUSDCP", "ETH", "ETHUSDT", "SOLUSDT"], start=1)
    ]
    concentration = _metrics(rows, {})["instrument_concentration"]
    assert concentration["top_1_share"] == 0.5
    assert concentration["top_3_share"] == 1.0


def test_projections_written_before_base_asset_grouping_read_as_stale(tmp_path):
    """Merging the generator change alone must not leave old statistics looking current."""
    import json
    import shutil
    from pathlib import Path

    import pytest as _pytest

    from trading_os.projections import ProjectionStaleError, verify_projections

    repo = Path(__file__).resolve().parents[1]
    for rel in ("canonical/lifecycle/current.json", "exports/operational", "provider-ledgers/propw"):
        source, target = repo / rel, tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copytree if source.is_dir() else shutil.copy2)(source, target)
    current = tmp_path / "exports/operational/analytics/current.json"
    payload = json.loads(current.read_text(encoding="utf-8"))
    payload["source_version_pins"].pop("analytics_generator", None)
    current.write_text(json.dumps(payload), encoding="utf-8")
    with _pytest.raises(ProjectionStaleError, match="analytics_generator"):
        verify_projections(tmp_path)
