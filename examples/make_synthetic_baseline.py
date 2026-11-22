"""Generate the synthetic baseline the operational flow reads.

Everything here is invented: 12 fake historical trades, fake pins, a zero commit id.
It exists so the register -> review -> analyze flow and its tests can run in a fresh
clone without anyone's real records.

    python examples/make_synthetic_baseline.py [--root <dir>]     # default: repo root

Writes (all deterministic, safe to re-run):
    exports/method-validation/b1-v3/   historical seed (the "legacy" trades)
    exports/method-validation/b2/      performance summary pins
    exports/method-validation/b3/      frozen rule versions
    exports/trader-dna/v1/manifest.json
    canonical/adjudications/method-validation/episode-identity-resolution-20260911.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

GENERATED_AT = "2026-09-11T00:00:00Z"
ANALYSIS_DATE = "2026-09-11"
BASELINE_SHA = "0" * 40
ACTIVE = ["stg-vegas-tunnel", "stg-ict-liquidity", "stg-po3", "stg-triple-tap"]

# (strategy, instrument, direction, origin, outcome, R)
TRADES = [
    ("stg-vegas-tunnel", "BTCUSDT", "long", "live", "win", 2.0),
    ("stg-vegas-tunnel", "BTCUSDT", "short", "live", "loss", -1.0),
    ("stg-vegas-tunnel", "ETHUSDT", "long", "backtest", "win", 1.5),
    ("stg-vegas-tunnel", "ETHUSDT", "long", "backtest", "loss", -1.0),
    ("stg-vegas-tunnel", "BTCUSDT", "long", "backtest", "win", 3.0),
    ("stg-po3", "NAS100", "long", "live", "win", 1.8),
    ("stg-po3", "NAS100", "short", "backtest", "loss", -1.0),
    ("stg-po3", "XAUUSD", "long", "backtest", "breakeven", 0.0),
    ("stg-ict-liquidity", "EURUSD", "short", "live", "loss", -1.0),
    ("stg-ict-liquidity", "EURUSD", "long", "backtest", "win", 2.2),
    ("stg-triple-tap", "SOLUSDT", "long", "backtest", "win", 2.5),
    ("stg-triple-tap", "SOLUSDT", "short", "live", "loss", -1.0),
]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _digest(payload: dict) -> str:
    body = {k: v for k, v in payload.items() if k != "manifest_digest"}
    return _sha(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return _sha(data)


def build(root: Path) -> None:
    rows = []
    for index, (strategy, instrument, direction, origin, outcome, r) in enumerate(TRADES, start=1):
        episode = f"episode_{index:024x}"
        day = f"2025-{(index - 1) // 4 + 1:02d}-{(index - 1) % 4 * 7 + 3:02d}"
        rows.append({
            "analysis_eligibility": "eligible",
            "canonical_episode_id": episode,
            "canonical_realized_r": r,
            "direction": direction,
            "entry_price": 100.0 + index,
            "episode_id": episode,
            "episode_role": "primary",
            "event_date": day,
            "instrument": instrument,
            "outcome": outcome,
            "performance_counting_eligibility": True,
            "sample_origin": origin,
            "schema_version": "method-validation-b1.v3",
            "source_path": f"synthetic/trades/{day}_{instrument}_{index}.md",
            "stop_loss_price": 99.0 + index,
            "strategy_id": strategy,
            "strategy_status": "active",
            "take_profit_price": 103.0 + index,
            "time_entry": f"{8 + index:02d}:30",
            "timeframe_context": "1h",
            "timeframe_trigger": "15m",
        })
    dataset = b"".join(
        (json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8") for row in rows
    )

    resolution_rel = "canonical/adjudications/method-validation/episode-identity-resolution-20260911.jsonl"
    resolution_sha = _write(root / resolution_rel, b"")

    b1 = root / "exports/method-validation/b1-v3"
    dataset_sha = _write(b1 / "canonical-trade-dataset.jsonl", dataset)
    b1_manifest = {
        "schema_version": "method-validation-b1.v3",
        "dataset_version": "method-validation-b1.v3",
        "generated_at": GENERATED_AT,
        "baseline_sha": BASELINE_SHA,
        "synthetic": True,
        "artifacts": {
            "dataset": {"path": "exports/method-validation/b1-v3/canonical-trade-dataset.jsonl", "sha256": dataset_sha},
            "episode_resolution": {"path": resolution_rel, "sha256": resolution_sha},
        },
        "counts": {"source_rows": len(rows), "performance_countable_episode_count": len(rows)},
    }
    b1_manifest["manifest_digest"] = _digest(b1_manifest)
    _write(b1 / "manifest.json", _dump(b1_manifest))

    versions = []
    for strategy in ACTIVE:
        version_file = root / "registry/versions" / f"{strategy}-v1.yaml"
        file_sha = _sha(version_file.read_bytes()) if version_file.is_file() else _sha(strategy.encode())
        versions.append({
            "strategy_id": strategy,
            "rule_version": f"{strategy}:v1",
            "rule_definition_sha256": _sha(f"rules:{strategy}".encode()),
            "version_file_sha256": file_sha,
            "freeze_status": "frozen",
            "lifecycle_at_freeze": "active",
        })
    b3_manifest = {
        "schema_version": "method-validation-b3.v1",
        "synthetic": True,
        "freeze_date": ANALYSIS_DATE,
        "freeze_baseline_sha": BASELINE_SHA,
        "active_strategy_ids": ACTIVE,
        "strategy_versions": versions,
    }
    b3_manifest["manifest_digest"] = _digest(b3_manifest)
    _write(root / "exports/method-validation/b3/manifest.json", _dump(b3_manifest))

    performance = {
        "schema_version": "method-validation-b2.v1",
        "synthetic": True,
        "strategies": [
            {"strategy_id": strategy, "rule_observability": []} for strategy in ACTIVE
        ],
    }
    performance_sha = _write(root / "exports/method-validation/b2/strategy-performance.json", _dump(performance))
    b2_manifest = {
        "schema_version": "method-validation-b2.v1",
        "synthetic": True,
        "analysis_date": ANALYSIS_DATE,
        "baseline_sha": BASELINE_SHA,
        "active_strategy_ids": ACTIVE,
        "output_artifacts": {
            "strategy-performance.json": {
                "path": "exports/method-validation/b2/strategy-performance.json",
                "sha256": performance_sha,
            }
        },
    }
    b2_manifest["manifest_digest"] = _digest(b2_manifest)
    _write(root / "exports/method-validation/b2/manifest.json", _dump(b2_manifest))

    dna_manifest = {
        "schema_version": "trader-dna.v1",
        "synthetic": True,
        "analysis_date": ANALYSIS_DATE,
        "baseline_sha": BASELINE_SHA,
    }
    dna_manifest["manifest_digest"] = _digest(dna_manifest)
    _write(root / "exports/trader-dna/v1/manifest.json", _dump(dna_manifest))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    build(args.root.resolve())
    print(f"synthetic baseline written under {args.root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
