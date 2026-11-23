"""Local chart planner prototype (read-only market data, no order surface).

Serves the page, proxies BingX PUBLIC candles (no key), and saves Owner-drawn plans.
R is computed by the existing trading_os.live_chart_plan.measure_r, never re-implemented.
Plans are PRE-TRADE DRAFTS in chart_planner/plans/ (git-ignored); they are not trades and
mint no T-ID. Registration still goes through workflows/trade-registration.yaml.
Source: Allen, 2026-11-23 (chart app prototype request).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from trading_os.live_chart_plan import LiveChartPlanError, measure_r  # noqa: E402

PLANS = HERE / "plans"
BINGX = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
INTERVALS = {"5m", "15m", "30m", "1h", "4h", "1d"}


def candles(symbol: str, interval: str, limit: int = 500) -> list[dict]:
    if interval not in INTERVALS or not symbol.replace("-", "").isalnum():
        raise ValueError("bad symbol or interval")
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval, "limit": limit})
    with urllib.request.urlopen(f"{BINGX}?{q}", timeout=15) as r:
        rows = json.loads(r.read())["data"]
    rows.sort(key=lambda x: x["time"])
    return [{"time": x["time"] // 1000, **{k: float(x[k]) for k in ("open", "high", "low", "close")}} for x in rows]


def save_plan(body: dict) -> dict:
    direction = "long" if body["target"] > body["entry"] else "short"
    m = measure_r(direction, body["entry"], body["stop"], body["target"])
    if m["risk"] <= 0 or m["reward"] <= 0:
        raise LiveChartPlanError("entry/stop/target contradict each other")
    plan = {
        "kind": "pre_trade_plan_draft", "created_at": int(time.time()),
        "symbol": body["symbol"], "interval": body["interval"], "direction": direction,
        "entry": body["entry"], "stop": body["stop"], "target": body["target"],
        "planned_rr": float(m["planned_rr"]), "risk_distance": float(m["risk"]),
        "provenance": "app_drawn", "note": body.get("note", ""),
    }
    PLANS.mkdir(exist_ok=True)
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    (PLANS / f"{plan['created_at']}-{plan['symbol']}.json").write_text(text, encoding="utf-8")
    (PLANS / "latest.json").write_text(text, encoding="utf-8")
    return plan


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/candles":
                q = urllib.parse.parse_qs(u.query)
                return self._send(200, candles(q.get("symbol", ["BTC-USDT"])[0], q.get("interval", ["1h"])[0]))
            if u.path == "/api/fills":
                import fills
                q = urllib.parse.parse_qs(u.query)
                end = int(q.get("end", [int(time.time() * 1000)])[0])
                start = int(q.get("start", [end - 7 * 24 * 3600 * 1000])[0])
                return self._send(200, fills.fills(start, end, q.get("symbol", [None])[0]))
            if u.path == "/api/match":  # plan id = saved file stem, e.g. 1700000000-BTC-USDT
                import fills, match, re
                pid = urllib.parse.parse_qs(u.query).get("plan", [""])[0]
                if pid == "latest":
                    pid = f"{json.loads((PLANS / 'latest.json').read_text('utf-8'))['created_at']}-" +                           json.loads((PLANS / "latest.json").read_text("utf-8"))["symbol"]
                if not re.fullmatch(r"\d+-[A-Za-z0-9-]+", pid) or not (PLANS / f"{pid}.json").exists():
                    return self._send(400, {"error": "unknown plan id"})
                plan = json.loads((PLANS / f"{pid}.json").read_text("utf-8"))
                got = fills.fills(plan["created_at"] * 1000, int(time.time() * 1000), plan["symbol"])
                if not got["configured"]:
                    return self._send(200, {"status": "fills_not_configured", "hint": got["hint"]})
                return self._send(200, match.match_plan(plan, pid, got["fills"]))
            if u.path == "/":
                return self._send(200, (HERE / "index.html").read_bytes(), "text/html")
            assets = {"/lw.js": ("lightweight-charts.js", "application/javascript"),
                      "/xterm.js": ("xterm.js", "application/javascript"),
                      "/addon-fit.js": ("addon-fit.js", "application/javascript"),
                      "/xterm.css": ("xterm.css", "text/css")}
            if u.path in assets:
                name, ctype = assets[u.path]
                return self._send(200, (HERE / "vendor" / name).read_bytes(), ctype)
        except Exception as e:  # prototype: report, don't crash
            return self._send(502, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/plan":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._send(200, save_plan(body))
        except (LiveChartPlanError, KeyError, ValueError) as e:
            self._send(400, {"error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    try:
        import terminal
        terminal.start(port)
    except ImportError as e:  # pywinpty/websockets missing: chart still works, no terminal pane
        print(f"terminal pane disabled: {e}")
    print(f"chart planner on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
