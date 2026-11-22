from __future__ import annotations

import json
import hashlib
from datetime import datetime as RealDateTime, timezone
from pathlib import Path

import pytest

import trading_os.registration_census as census_module
from trading_os.registration_census import build_census, lookup, select_candidates, select_next, write_census


def _root(tmp_path: Path, lifecycles, links, finalized) -> Path:
    led = tmp_path / "provider-ledgers" / "propw" / "normalized"
    led.mkdir(parents=True)
    led.joinpath("trade-lifecycles.jsonl").write_text(
        "".join(json.dumps({"open_id": o, "lifecycle": {"open_id": o, "normalization_status": n, "provider_lifecycle_phase": "CLOSED" if n == "ok" else "UNKNOWN", "lifecycle_reason": None if n == "ok" else "BAD_NORMALIZATION_STATUS", "is_closed": 1, "is_partial": 0, "instrument": "BTCUSDT", "direction": "long", "entry_time": "2026-01-01T00:00:00Z", "entry_price": "100", "exit_time": "2026-01-01T01:00:00Z", "weighted_exit_price": "101", "open_size": "1", "closed_size": "1"}}) + "\n" for o, n in lifecycles),
        encoding="utf-8",
    )
    (tmp_path / "canonical" / "lifecycle").mkdir(parents=True)
    (tmp_path / "canonical" / "lifecycle" / "identity-links.jsonl").write_text("".join(json.dumps(l) + "\n" for l in links), encoding="utf-8")
    (tmp_path / "canonical" / "sequences").mkdir(parents=True)
    (tmp_path / "canonical" / "sequences" / "finalizations.json").write_text(json.dumps({"records": finalized}), encoding="utf-8")
    return tmp_path


def _link(draft, provider, oid, disp="MATCHED"):
    return {"draft_id": draft, "provider": provider, "provider_lifecycle_id": oid, "match_disposition": disp}


def _root_with_rows(tmp_path: Path, rows, links=(), finalized=None) -> Path:
    led = tmp_path / "provider-ledgers" / "propw" / "normalized"
    led.mkdir(parents=True)
    led.joinpath("trade-lifecycles.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    (tmp_path / "canonical" / "lifecycle").mkdir(parents=True)
    (tmp_path / "canonical" / "lifecycle" / "identity-links.jsonl").write_text(
        "".join(json.dumps(link) + "\n" for link in links), encoding="utf-8"
    )
    (tmp_path / "canonical" / "sequences").mkdir(parents=True)
    (tmp_path / "canonical" / "sequences" / "finalizations.json").write_text(
        json.dumps({"records": finalized or {}}), encoding="utf-8"
    )
    return tmp_path


def _phase_row(open_id, phase, entry_time, *, status=None, closed=False):
    status = status or {"OPEN": "open_only", "PARTIALLY_CLOSED": "ok", "CLOSED": "ok", "UNKNOWN": "insufficient_exit_data"}[phase]
    return {
        "open_id": open_id,
        "lifecycle": {
            "open_id": open_id,
            "instrument": "BTCUSDT",
            "direction": "1",
            "entry_time": entry_time,
            "entry_price": "100",
            "exit_time": "2026-04-01T00:00:00Z" if phase in {"PARTIALLY_CLOSED", "CLOSED"} else None,
            "weighted_exit_price": "101" if phase in {"PARTIALLY_CLOSED", "CLOSED"} else None,
            "open_size": "5",
            "closed_size": "2" if phase == "PARTIALLY_CLOSED" else ("5" if closed else None),
            "is_closed": int(closed),
            "is_partial": int(phase == "PARTIALLY_CLOSED"),
            "normalization_status": status,
            "provider_lifecycle_phase": phase,
            "lifecycle_reason": "TEST_UNKNOWN" if phase == "UNKNOWN" else None,
        },
    }


def test_all_statuses_and_provider_casing(tmp_path):
    root = _root(
        tmp_path,
        [("1", "ok"), ("2", "ok"), ("3", "ok"), ("4", "ok"), ("5", "ok"), ("6", "bad")],
        [_link("d1", "PropW", "1"), _link("d2", "propw", "2"), _link("d3", "propw", "3", "AMBIGUOUS"), _link("dA", "propw", "4"), _link("dB", "PropW", "4")],
        {"d1": {"trade_no": "T000001"}},
    )
    census = build_census(root, "PropW")
    by = {r["open_id"]: r["status"] for r in census["lifecycles"]}
    assert by == {"1": "REGISTERED_INCOMPLETE", "2": "IN_PROGRESS", "3": "AMBIGUOUS", "4": "AMBIGUOUS", "5": "UNREGISTERED", "6": "INVALID_PROVIDER_LIFECYCLE"}
    assert lookup(census, "PROPW", "1")["trade_no"] == "T000001"  # re-register returns the original T-ID
    assert lookup(census, "propw", "999")["status"] == "NOT_IN_LEDGER"
    assert sum(census["counts"].values()) == census["total"] == 6
    assert census["registered_identity_count"] == 1


def test_missing_ledger_fails_closed(tmp_path):
    with pytest.raises(FileNotFoundError):
        build_census(tmp_path, "propw")


def test_open_partial_and_closed_are_distinct_ordered_candidates(tmp_path):
    root = _root_with_rows(
        tmp_path,
        [
            _phase_row("closed", "CLOSED", "2026-03-01T00:00:00Z", closed=True),
            _phase_row("partial", "PARTIALLY_CLOSED", "2026-02-01T00:00:00Z"),
            _phase_row("open", "OPEN", "2026-01-01T00:00:00Z"),
        ],
    )
    census = build_census(root)
    assert census["counts"]["UNREGISTERED"] == 3
    assert {row["open_id"]: row["provider_phase"] for row in census["lifecycles"]} == {
        "open": "OPEN", "partial": "PARTIALLY_CLOSED", "closed": "CLOSED"
    }
    assert [row["open_id"] for row in select_candidates(census)] == ["open", "partial", "closed"]
    assert select_next(census)["open_id"] == "closed"  # legacy default remains closed-only
    assert select_next(census, phase="open")["open_id"] == "open"
    assert select_next(census, phase="partially_closed")["open_id"] == "partial"
    assert select_next(census, phase="all")["open_id"] == "open"
    assert all(row["trade_no"] is None for row in select_candidates(census))
    assert census["freshness"]["status"] == "unknown"


def test_open_only_requires_verified_opener_facts(tmp_path):
    invalid = _phase_row("bad-open", "UNKNOWN", "2026-01-01T00:00:00Z")
    invalid["lifecycle"]["lifecycle_reason"] = "INCOMPLETE_OPEN_FACTS"
    invalid["lifecycle"]["open_size"] = None
    root = _root_with_rows(tmp_path, [invalid])
    census = build_census(root)
    row = census["lifecycles"][0]
    assert row["status"] == "INVALID_PROVIDER_LIFECYCLE"
    assert row["provider_phase"] == "UNKNOWN"
    assert select_candidates(census) == []


def test_new_and_legacy_conflicts_or_incomplete_facts_never_become_candidates(tmp_path):
    overclosed = _phase_row("overclosed", "CLOSED", "2026-01-01T00:00:00Z", closed=True)
    overclosed["lifecycle"]["closed_size"] = "50"
    legacy_missing = {
        "open_id": "legacy-missing",
        "lifecycle": {"normalization_status": "ok", "is_closed": 1, "entry_time": "2026-01-01T00:00:00Z"},
    }
    legacy_infinite = {
        "open_id": "legacy-infinite",
        "lifecycle": {
            "normalization_status": "open_only",
            "open_size": "Infinity",
            "entry_price": "Infinity",
            "entry_time": "2026-01-01T00:00:00Z",
            "instrument": "BTCUSDT",
            "direction": "long",
            "is_closed": 0,
            "is_partial": 0,
        },
    }
    census = build_census(_root_with_rows(tmp_path, [overclosed, legacy_missing, legacy_infinite]))
    assert all(row["provider_phase"] == "UNKNOWN" for row in census["lifecycles"])
    assert all(row["status"] == "INVALID_PROVIDER_LIFECYCLE" for row in census["lifecycles"])
    assert select_candidates(census) == []


def test_registration_identity_remains_registered_when_provider_phase_is_unknown(tmp_path):
    root = _root_with_rows(
        tmp_path,
        [_phase_row("1", "UNKNOWN", "2026-01-01T00:00:00Z")],
        [_link("d1", "propw", "1")],
        {"d1": {"trade_no": "T000007", "trade_id": "trd_7"}},
    )
    row = build_census(root)["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"
    assert row["trade_no"]  # the T-ID is kept; only the status label reflects missing inputs
    assert row["provider_phase"] == "UNKNOWN"
    assert row["trade_no"] == "T000007"
    assert select_candidates({"lifecycles": [row]}) == []


def test_canonical_only_finalization_marks_census_stale_then_rebuilds(tmp_path):
    root = _root(tmp_path, [("1", "ok")], [_link("d1", "propw", "1")], {})
    path = write_census(root)
    before = json.loads(path.read_text(encoding="utf-8"))
    ledger_bytes = (root / "provider-ledgers" / "propw" / "normalized" / "trade-lifecycles.jsonl").read_bytes()
    assert before["counts"]["IN_PROGRESS"] == 1

    finalizations_path = root / "canonical" / "sequences" / "finalizations.json"
    finalizations_path.write_text(json.dumps({"records": {"d1": {"trade_no": "T000001", "trade_id": "trd_1"}}}), encoding="utf-8")
    stale = build_census(root)
    assert stale["freshness"]["status"] == "stale"
    assert stale["freshness"]["detail"] == "SOURCE_DIGESTS_CHANGED"
    assert stale["lifecycles"][0]["status"] == "REGISTERED_INCOMPLETE"
    assert stale["source_digests"]["normalized_ledger_sha256"] == before["source_digests"]["normalized_ledger_sha256"]
    assert (root / "provider-ledgers" / "propw" / "normalized" / "trade-lifecycles.jsonl").read_bytes() == ledger_bytes

    write_census(root)
    rebuilt = json.loads(path.read_text(encoding="utf-8"))
    assert rebuilt["freshness"]["status"] == "fresh"
    assert rebuilt["counts"]["REGISTERED_INCOMPLETE"] == 1


def test_same_source_regeneration_is_byte_identical_including_metadata(tmp_path, monkeypatch):
    root = _root_with_rows(tmp_path, [_phase_row("1", "OPEN", "2026-01-01T00:00:00Z")])

    class Clock:
        value = "2026-10-10T01:02:03+00:00"

        @classmethod
        def now(cls, tz):
            assert tz is timezone.utc
            return RealDateTime.fromisoformat(cls.value)

    monkeypatch.setattr(census_module, "datetime", Clock)
    path = write_census(root)
    first = path.read_bytes()
    first_generated_at = json.loads(first)["generated_at"]
    Clock.value = "2026-10-10T02:03:04+00:00"
    write_census(root)
    assert path.read_bytes() == first
    assert json.loads(path.read_bytes())["generated_at"] == first_generated_at


_PACKET_SHA = "f" * 64


def _raw_image(name: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + name.encode("ascii")


def _registered_root(tmp_path: Path, record: dict, packet: dict | None) -> Path:
    from trading_os.registration_packet import packet_sha256
    frozen_sha = packet_sha256(packet) if packet is not None else _PACKET_SHA
    link = {**_link("rec_1", "propw", "1"), "registration_packet_sha256": frozen_sha}
    root = _root(tmp_path, [("1", "ok")], [link], {"rec_1": {"trade_no": "T000001", "trade_id": "trd_1"}})
    (root / "canonical" / "lifecycle" / "current.json").write_text(json.dumps({"records": {"trd_1": record}}), encoding="utf-8")
    for item in record.get("evidence", []):
        if not isinstance(item, dict) or not item.get("original_filename"):
            continue
        name = item["original_filename"].removesuffix(".png")
        sha = hashlib.sha256(_raw_image(name)).hexdigest()
        if item.get("sha256") != sha:
            continue
        artifacts = root / "canonical" / "evidence" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / f"{sha}.png").write_bytes(_raw_image(name))
    if packet is not None:
        folder = root / "canonical" / "registration" / "packets"
        folder.mkdir(parents=True)
        (folder / f"{frozen_sha}.json").write_text(json.dumps({**packet, "packet_sha256": frozen_sha}), encoding="utf-8")
    return root


def _image(name: str) -> dict:
    sha = hashlib.sha256(_raw_image(name)).hexdigest()
    ref = f"artifact:{sha}.png"
    return {"source_ref": ref, "locator": ref, "sha256": sha,
            "media_type": "image/png", "original_filename": f"{name}.png"}


def _role(*names: str, rationale: str = "Agent read the chart") -> dict:
    return {"evidence_refs": [_image(n)["locator"] for n in names], "rationale": rationale}


_WORDS = [{"authority": "owner_supplied_verbatim", "verbatim_text": "我當時的想法", "sha256": hashlib.sha256("我當時的想法".encode("utf-8")).hexdigest()}]
_RECORD = {"owner_statements": _WORDS, "evidence": [_image("a"), _image("b")]}
_PACKET = {"agent_assessment": {"chart_roles": {"entry_chart": _role("a"), "exit_chart": _role("b")}}}


def test_registered_means_owner_words_and_agent_declared_entry_and_exit_charts(tmp_path: Path) -> None:
    row = build_census(_registered_root(tmp_path, _RECORD, _PACKET))["lifecycles"][0]
    assert row["status"] == "REGISTERED"
    assert "missing" not in row


def test_one_combined_chart_may_be_both_roles(tmp_path: Path) -> None:
    packet = {"agent_assessment": {"chart_roles": {"entry_chart": _role("a"), "exit_chart": _role("a")}}}
    record = {"owner_statements": _WORDS, "evidence": [_image("a")]}
    assert build_census(_registered_root(tmp_path, record, packet))["lifecycles"][0]["status"] == "REGISTERED"


@pytest.mark.parametrize(
    "record, packet, missing",
    [
        ({**_RECORD, "owner_statements": []}, _PACKET, ["owner_words"]),
        ({**_RECORD, "owner_statements": [{**_WORDS[0], "sha256": "0" * 64}]}, _PACKET, ["owner_words"]),
        (_RECORD, None, ["entry_chart", "exit_chart"]),  # the Agent never judged the charts
        (_RECORD, {"agent_assessment": {}}, ["entry_chart", "exit_chart"]),
        (_RECORD, {"agent_assessment": {"chart_roles": {"entry_chart": _role("a")}}}, ["exit_chart"]),
        (_RECORD, {"agent_assessment": {"chart_roles": {"entry_chart": _role("a"), "exit_chart": _role("zzz")}}}, ["exit_chart"]),  # cites an image that is not saved
        (_RECORD, {"agent_assessment": {"chart_roles": {"entry_chart": _role("a", rationale="  "), "exit_chart": _role("b")}}}, ["entry_chart"]),
        ({"owner_statements": _WORDS, "evidence": []}, _PACKET, ["entry_chart", "exit_chart"]),
    ],
)
def test_incomplete_registration_lists_what_is_missing(tmp_path: Path, record: dict, packet: dict | None, missing: list) -> None:
    row = build_census(_registered_root(tmp_path, record, packet))["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"
    assert row["missing"] == missing
    assert row["trade_no"] == "T000001"


def test_the_program_never_guesses_roles_from_image_count(tmp_path: Path) -> None:
    # Two distinct saved images and the Owner's words are NOT enough without the Agent's judgment.
    row = build_census(_registered_root(tmp_path, _RECORD, None))["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"


def test_incomplete_registration_is_never_offered_as_a_new_candidate(tmp_path: Path) -> None:
    census = build_census(_registered_root(tmp_path, {"owner_statements": [], "evidence": []}, None))
    assert census["counts"]["REGISTERED_INCOMPLETE"] == 1
    assert census["unregistered_closed"] == 0
    assert select_candidates(census) == []


def test_existing_legacy_identity_never_reenters_registration_queue(tmp_path: Path) -> None:
    # New completeness requirements do not invalidate a previously allocated T-ID.
    root = _registered_root(tmp_path, {"owner_statements": [], "evidence": []}, None)
    snapshot = build_census(root)
    row = snapshot["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"
    assert row["trade_no"] == "T000001"
    assert row["trade_id"] == "trd_1"
    assert row["missing"] == ["owner_words", "entry_chart", "exit_chart"]
    assert snapshot["unregistered_closed"] == 0
    assert snapshot["counts"]["UNREGISTERED"] == 0
    assert select_candidates(snapshot) == []
    # The writer's identity-only lookup remains registered, and can reuse T000001.
    assert census_module.registration_state(root, "propw", "1")["trade_no"] == "T000001"
    assert census_module.registration_state(root, "propw", "1")["status"] == "REGISTERED"


def test_existing_legacy_chart_metadata_still_resolves_declared_roles(tmp_path: Path) -> None:
    # Historical records can have a chart type but no populated MIME metadata.
    record = {
        "owner_statements": _WORDS,
        "evidence": [
            {k: v for k, v in {**_image("a"), "type": "chart_snapshot"}.items() if k != "media_type"},
            {k: v for k, v in {**_image("b"), "type": "replay_chart"}.items() if k != "media_type"},
        ],
    }
    row = build_census(_registered_root(tmp_path, record, _PACKET))["lifecycles"][0]
    assert row["status"] == "REGISTERED"
    assert row["trade_no"] == "T000001"
    assert row["trade_id"] == "trd_1"


def test_real_existing_33_trade_identities_survive_completeness_reclassification() -> None:
    """The pre-v4 corpus is durable: no old T-ID can return to the candidate queue.

    This reads the repository's checked-in provider ledger and canonical links;
    it does not write, amend, or prompt for reconstruction of any historical trade.
    """
    root = Path(__file__).resolve().parents[1]
    persisted = json.loads(
        (root / "provider-ledgers" / "propw" / "registration-census.json").read_text(
            encoding="utf-8"
        )
    )
    historical = {
        row["trade_no"]: row["provider_lifecycle_key"]
        for row in persisted["lifecycles"]
        if row.get("trade_no") in {f"T{n:06d}" for n in range(1, 34)}
    }
    # All original T000001..T000033 were issued once. Later registrations may
    # increase the population but must never reuse or hide these identities.
    assert set(historical) == {f"T{n:06d}" for n in range(1, 34)}

    current = build_census(root)
    by_trade = {row["trade_no"]: row for row in current["lifecycles"] if row.get("trade_no")}
    pending = {row["provider_lifecycle_key"] for row in select_candidates(current)}
    for trade_no, provider_key in historical.items():
        row = by_trade[trade_no]
        assert row["provider_lifecycle_key"] == provider_key
        assert row["trade_id"].startswith("trd_")
        assert row["status"] in {"REGISTERED", "REGISTERED_INCOMPLETE"}
        assert provider_key not in pending
    assert len(by_trade) >= 33
    assert current["registered_identity_count"] == len(by_trade)
    assert current["counts"]["REGISTERED"] + current["counts"]["REGISTERED_INCOMPLETE"] >= 33


def test_image_metadata_without_original_bytes_is_incomplete(tmp_path: Path) -> None:
    root = _registered_root(tmp_path, _RECORD, _PACKET)
    a = _image("a")
    artifact = root / "canonical" / "evidence" / "artifacts" / a["locator"].removeprefix("artifact:")
    artifact.write_bytes(b"corrupted-original")
    row = build_census(root)["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"
    assert row["missing"] == ["entry_chart"]
    assert row["trade_no"] == "T000001"


def test_missing_canonical_image_locator_does_not_count_as_saved_original(tmp_path: Path) -> None:
    record = {**_RECORD, "evidence": [
        {**_image("a"), "sha256": "0" * 64},
        _image("b"),
    ]}
    row = build_census(_registered_root(tmp_path, record, _PACKET))["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"
    assert row["missing"] == ["entry_chart"]


def test_tampered_frozen_packet_cannot_claim_chart_roles(tmp_path: Path) -> None:
    root = _registered_root(tmp_path, _RECORD, _PACKET)
    from trading_os.registration_packet import packet_sha256
    sha = packet_sha256(_PACKET)
    packet_path = root / "canonical" / "registration" / "packets" / f"{sha}.json"
    persisted = json.loads(packet_path.read_text(encoding="utf-8"))
    persisted["agent_assessment"]["chart_roles"]["entry_chart"]["rationale"] = "tampered"
    packet_path.write_text(json.dumps(persisted), encoding="utf-8")
    row = build_census(root)["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"
    assert row["missing"] == ["entry_chart", "exit_chart"]
    assert row["trade_no"] == "T000001"


_UNRECOVERABLE = {"event_id": "lev_x", "reason": "Registered before the verbatim rule.", "confirmed_at": "2026-10-11T08:00:00Z"}


def test_acknowledged_missing_words_are_counted_separately_but_stay_incomplete(tmp_path: Path) -> None:
    record = {"owner_statements": [], "owner_words_unrecoverable": _UNRECOVERABLE, "evidence": [_image("a"), _image("b")]}
    census = build_census(_registered_root(tmp_path, record, _PACKET))
    row = census["lifecycles"][0]
    assert row["status"] == "REGISTERED_INCOMPLETE"  # an acknowledgement never completes a trade
    assert row["missing"] == ["owner_words"]
    assert row["acknowledged_gaps"] == ["owner_words"]
    assert census["legacy_acknowledged_count"] == 1
    assert census["counts"]["REGISTERED"] == 0


def test_unacknowledged_missing_words_are_not_counted_as_legacy(tmp_path: Path) -> None:
    record = {"owner_statements": [], "evidence": [_image("a"), _image("b")]}
    census = build_census(_registered_root(tmp_path, record, _PACKET))
    assert "acknowledged_gaps" not in census["lifecycles"][0]
    assert census["legacy_acknowledged_count"] == 0


def test_a_trade_with_other_gaps_is_not_legacy_only(tmp_path: Path) -> None:
    # Words acknowledged, but the exit chart is still missing: still work to do, so not legacy-only.
    record = {"owner_statements": [], "owner_words_unrecoverable": _UNRECOVERABLE, "evidence": [_image("a")]}
    packet = {"agent_assessment": {"chart_roles": {"entry_chart": _role("a")}}}
    census = build_census(_registered_root(tmp_path, record, packet))
    row = census["lifecycles"][0]
    assert set(row["missing"]) == {"owner_words", "exit_chart"}
    assert row["acknowledged_gaps"] == ["owner_words"]
    assert census["legacy_acknowledged_count"] == 0


def test_verbatim_words_override_an_old_acknowledgement(tmp_path: Path) -> None:
    record = {**_RECORD, "owner_words_unrecoverable": _UNRECOVERABLE}
    row = build_census(_registered_root(tmp_path, record, _PACKET))["lifecycles"][0]
    assert row["status"] == "REGISTERED"
    assert "acknowledged_gaps" not in row
