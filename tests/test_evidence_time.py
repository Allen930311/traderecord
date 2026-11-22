from __future__ import annotations

import pytest

from trading_os.evidence_time import (
    EvidenceTimeError,
    normalize_evidence_time_metadata,
    represented_market_time_contains,
)


def test_retrospective_replay_keeps_capture_time_separate_from_market_time():
    evidence = {
        "category": "review",
        "timing": "post_trade",
        "captured_at": "2026-10-08T04:00:00Z",
        "provenance": {
            "capture_mode": "retrospective_replay",
            "captured_at": "2026-10-08T04:00:00Z",
        },
        "represented_market_time": {
            "start": "2026-09-22T09:30:00Z",
            "end": "2026-09-22T09:45:00Z",
        },
        "role": "historical_market_reconstruction",
    }

    normalized = normalize_evidence_time_metadata(evidence)

    assert normalized["provenance"]["capture_mode"] == "retrospective_replay"
    assert normalized["provenance"]["capture_time_status"] == "known"
    assert normalized["provenance"]["captured_at"] == "2026-10-08T04:00:00Z"
    assert normalized["represented_market_time"]["status"] == "known"
    assert normalized["timing_relative_to_trade"] == "post_trade"
    assert represented_market_time_contains(evidence, "2026-09-22T09:33:35Z") is True
    assert represented_market_time_contains(evidence, "2026-10-08T04:00:00Z") is False


def test_unknown_capture_or_market_time_stays_unknown_instead_of_becoming_a_conflict():
    evidence = {
        "category": "review",
        "timing": "post_trade",
        "provenance": {"capture_mode": "retrospective_replay"},
        "role": "historical_market_reconstruction",
    }

    normalized = normalize_evidence_time_metadata(evidence)

    assert normalized["provenance"]["capture_time_status"] == "not_observable"
    assert "represented_market_time" not in normalized
    assert represented_market_time_contains(evidence, "2026-09-22T09:33:35Z") is None


@pytest.mark.parametrize(
    "evidence",
    [
        {
            "category": "pre_trade_context",
            "timing": "pre_trade",
            "provenance": {"capture_mode": "retrospective_replay"},
            "role": "historical_market_reconstruction",
        },
        {
            "category": "review",
            "timing": "post_trade",
            "provenance": {"capture_mode": "retrospective_replay"},
            "role": "chart_snapshot",
        },
        {
            "category": "review",
            "timing": "post_trade",
            "captured_at": "2026-10-08T04:00:00Z",
            "provenance": {
                "capture_mode": "retrospective_replay",
                "captured_at": "2026-10-08T04:01:00Z",
            },
            "role": "historical_market_reconstruction",
        },
    ],
)
def test_retrospective_replay_rejects_false_or_inconsistent_provenance(evidence):
    with pytest.raises(EvidenceTimeError):
        normalize_evidence_time_metadata(evidence)


def test_retrospective_capture_cannot_be_earlier_than_represented_market_end():
    with pytest.raises(EvidenceTimeError, match="later than its represented market time"):
        normalize_evidence_time_metadata(
            {
                "category": "review",
                "timing": "post_trade",
                "provenance": {
                    "capture_mode": "retrospective_replay",
                    "captured_at": "2026-09-22T09:40:00Z",
                },
                "represented_market_time": {
                    "start": "2026-09-22T09:30:00Z",
                    "end": "2026-09-22T09:45:00Z",
                },
                "role": "historical_market_reconstruction",
            }
        )
