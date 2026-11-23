"""chart_planner.fills: read-only BingX fills (Allen, 2026-11-23). No network, no real key."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chart_planner"))
import fills as F  # noqa: E402


def test_no_key_returns_not_configured_without_calling_network(monkeypatch):
    monkeypatch.delenv("BINGX_API_KEY", raising=False)
    monkeypatch.delenv("BINGX_API_SECRET", raising=False)
    r = F.fills(0, 1000, http=lambda *a: pytest.fail("must not call network"))
    assert r["configured"] is False and r["fills"] == []


def test_signature_is_hmac_sha256_over_sorted_params():
    import hashlib, hmac
    p = {"b": 2, "a": 1}
    assert F.sign("s", p) == hmac.new(b"s", b"a=1&b=2", hashlib.sha256).hexdigest()


def test_fills_normalised_windowed_and_secret_never_leaks(monkeypatch):
    monkeypatch.setenv("BINGX_API_KEY", "KEY123")
    monkeypatch.setenv("BINGX_API_SECRET", "SECRET456")
    calls = []

    def fake(url, key):
        calls.append((url, key))
        return {"code": 0, "data": {"fill_orders": [
            {"symbol": "BTC-USDT", "orderId": 9, "side": "BUY", "positionSide": "LONG",
             "price": "100.5", "qty": "2", "commission": "-0.1", "filledTm": "1700000000000"}]}}

    ten_days = 10 * 24 * 3600 * 1000
    r = F.fills(0, ten_days, http=fake, now=lambda: 1.0)
    assert len(calls) == 2  # 7-day max window
    assert r["fills"][0] | {} == r["fills"][0] and r["fills"][0]["price"] == 100.5
    assert r["fills"][0]["side"] == "buy" and r["fills"][0]["filled_at_ms"] == 1700000000000
    assert all("SECRET456" not in u for u, _ in calls) and calls[0][1] == "KEY123"
    assert "SECRET456" not in repr(r)


def test_transport_error_does_not_echo_signed_url(monkeypatch):
    monkeypatch.setenv("BINGX_API_KEY", "K")
    monkeypatch.setenv("BINGX_API_SECRET", "S")

    def boom(url, key):
        raise OSError("failed " + url)

    with pytest.raises(RuntimeError) as e:
        F.fills(0, 1000, http=boom)
    assert "signature" not in str(e.value) and "http" not in str(e.value)
