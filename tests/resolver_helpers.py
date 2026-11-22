"""Fixtures that feed the mandatory strategy resolver (tests must not assert strategy_id)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading_os.strategy_resolver import active_strategies

ROOT = Path(__file__).resolve().parents[1]


def matching_observations(
    strategy_id: str = "stg-vegas-tunnel",
    *,
    timing: str = "pre_trade",
    evidence_ref: str = "fixture://chart",
) -> dict[str, Any]:
    """PASS observations for every required rule of ``strategy_id`` (a clean MATCH)."""

    strategy = next(s for s in active_strategies(ROOT) if s["strategy_id"] == strategy_id)
    return {
        "observations": [
            {"rule_id": r["rule_id"], "state": "PASS", "timing": timing, "evidence_refs": [evidence_ref]}
            for r in strategy["rules"]
            if r.get("required")
        ]
    }


def no_match_observations(reason: str = "no recognized setup at entry") -> dict[str, Any]:
    return {
        "owner_no_match": {
            "reviewed_strategy_ids": [s["strategy_id"] for s in active_strategies(ROOT)],
            "reason": reason,
            "evidence_refs": ["fixture://owner-review"],
        }
    }


def write_observations(path: Path, observations: dict[str, Any] | None = None) -> str:
    path.write_text(json.dumps(observations or matching_observations()), encoding="utf-8")
    return str(path)


def _module_observations_file() -> str:
    import tempfile

    return write_observations(Path(tempfile.mkdtemp(prefix="resolver-obs-")) / "matching.json")


MATCHING_OBSERVATIONS_FILE = _module_observations_file()


def live_resolution(
    strategy_id: str = "stg-vegas-tunnel",
    *,
    contemporaneous: bool = True,
    evidence_ref: str = "fixture://chart",
) -> dict[str, Any]:
    """A resolver-produced ``strategy_resolution`` for tests that drive ``create_draft`` directly."""

    from trading_os.strategy_resolver import resolve_strategy, to_lifecycle_resolution

    observations = no_match_observations() if strategy_id == "unknown" else matching_observations(strategy_id, evidence_ref=evidence_ref)
    observations["plan"] = {"stop_loss_price": 1.0, "target_price": 2.0}
    return to_lifecycle_resolution(resolve_strategy(ROOT, observations), contemporaneous=contemporaneous)
