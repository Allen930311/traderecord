"""BACKTEST publish must take the focused test lane, like LIVE.

The publish path reads intake originals from the source checkout and writes only
canonical records/evidence into the isolated worktree. It must never commit
``registration-intake/`` files, which are not in the data-only path rules.
"""
from __future__ import annotations

import ast
from pathlib import Path

from trading_os.registration_test_lane import build_plan, load_policy

ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "trading_os" / "backtest_publish.py"
SHA = "a" * 64
DRAFT = "rec_0123456789abcdef0123"
TRADE = "trd_01a1c13e-d61a-7f75-8f2b-41be0f4f2958"


def test_backtest_sample_diff_selects_focused_lane() -> None:
    lane_policy, routine_allowlist = load_policy(ROOT)
    paths = [
        f"canonical/evidence/artifacts/{SHA}.png",
        f"canonical/evidence/artifacts/{SHA}.metadata.json",
        f"canonical/drafts/{DRAFT}.json",
        f"canonical/records/trades/{TRADE}.json",
        "canonical/lifecycle/current.json",
        "canonical/lifecycle/events.jsonl",
        "canonical/sequences/numbers.json",
    ]
    plan = build_plan(paths, lane_policy, routine_allowlist)
    assert plan["test_lane"] == "focused", plan
    assert plan["classification"] == "DATA_ONLY_REGISTRATION"


def test_intake_original_in_diff_still_forces_full_suite() -> None:
    lane_policy, routine_allowlist = load_policy(ROOT)
    plan = build_plan(
        ["registration-intake/case/images/shot.png"],
        lane_policy,
        routine_allowlist,
    )
    assert plan["test_lane"] == "full_suite"


def test_publish_reads_intake_from_source_and_never_copies_it() -> None:
    tree = ast.parse(PUBLISH.read_text(encoding="utf-8"))
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "_copy_images" not in names
    publish = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "publish_backtest_registration"
    )
    calls = [node for node in ast.walk(publish) if isinstance(node, ast.Call)]
    commit_calls = [c for c in calls if getattr(c.func, "id", "") == "commit_backtest_registration"]
    assert len(commit_calls) == 1
    image_base = {kw.arg: kw.value for kw in commit_calls[0].keywords}["image_base"]
    assert isinstance(image_base, ast.Name) and image_base.id == "source_root"


def test_remote_readback_requires_timing_event(monkeypatch) -> None:
    from trading_os.backtest_publish import _remote_backtest_readback
    from trading_os.registration_executor import RegistrationExecutorError
    import json
    import pytest

    record = {"trade_no": "BT000001", "trade_type": "backtest"}
    monkeypatch.setattr("trading_os.backtest_publish._git_bytes",
                        lambda root, *args: json.dumps(record).encode() if "trades/" in args[-1] else b"")
    result = {"trade_id": TRADE, "trade_no": "BT000001",
              "replay_timing": {"entry_at": "2026-09-08T13:40:00+08:00",
                                "exit_at": "2026-09-08T13:55:00+08:00"}}
    with pytest.raises(RegistrationExecutorError, match="missing.*timing event"):
        _remote_backtest_readback(ROOT, result)


def test_remote_readback_accepts_matching_timing_event(monkeypatch) -> None:
    from trading_os.backtest_publish import _remote_backtest_readback
    import json

    record = {"trade_no": "BT000001", "trade_type": "backtest"}
    timing = {"entry_at": "2026-09-08T13:40:00+08:00", "exit_at": "2026-09-08T13:55:00+08:00"}
    event = {"event_type": "backtest_timing_recorded", "payload": {"trade_id": TRADE, **timing}}
    def read(_root, *_args):
        return json.dumps(record).encode() if "trades/" in _args[-1] else (json.dumps(event) + "\n").encode()
    monkeypatch.setattr("trading_os.backtest_publish._git_bytes", read)
    result = {"trade_id": TRADE, "trade_no": "BT000001", "replay_timing": timing}
    assert _remote_backtest_readback(ROOT, result)["timing_recorded"] is True


def test_remote_readback_requires_exit_verification_when_counted(monkeypatch) -> None:
    from trading_os.backtest_publish import _remote_backtest_readback
    from trading_os.registration_executor import RegistrationExecutorError
    import json
    import pytest

    record = {"trade_no": "BT000001", "trade_type": "backtest"}
    verified = {"event_type": "exit_verified", "payload": {"trade_id": TRADE}}

    def reader(events):
        body = "".join(json.dumps(e) + "\n" for e in events).encode()
        return lambda _root, *args: json.dumps(record).encode() if "trades/" in args[-1] else body

    result = {"trade_id": TRADE, "trade_no": "BT000001", "result_counted": True}
    monkeypatch.setattr("trading_os.backtest_publish._git_bytes", reader([]))
    with pytest.raises(RegistrationExecutorError, match="exit verification"):
        _remote_backtest_readback(ROOT, result)
    monkeypatch.setattr("trading_os.backtest_publish._git_bytes", reader([verified]))
    assert _remote_backtest_readback(ROOT, result)["exit_verified_on_origin_main"] is True
