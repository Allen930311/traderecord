# Chart planner

Plan a trade on a BingX candle chart, with Claude or Codex in a terminal pane on the same page, then check
the plan against your real fills. Local only; read-only market data; no order surface.
(Source: Allen, 2026-11-23. Status and known gaps: [OPEN_SOURCE_CHECKLIST.md](OPEN_SOURCE_CHECKLIST.md).)

## What it does
1. **Chart**: BingX public candles (no key needed), symbols BTC/ETH/SOL-USDT, 5m to 1d.
2. **Plan**: "放置計畫" puts Entry / SL / TP lines on the chart; drag them. The footer shows direction and
   planned RR (preview). "儲存計畫" saves a draft in `chart_planner/plans/` (git-ignored); the server
   recomputes RR with `trading_os.live_chart_plan.measure_r`. A plan is a draft, not a trade, and mints no T-ID.
3. **Ask an agent**: "Claude" / "Codex" open a terminal pane in this page. "把計畫貼給 CLI" pastes a short
   request (it does not press Enter).
4. **Fills (optional)**: "成交紀錄" marks your last 7 days of BingX fills on the chart.
5. **Check**: "核對成交" matches the latest plan to fills made *after* it was saved and runs the
   registration R check (`app_drawn` provenance). It reports; it never registers. Registering a trade
   stays the Trading Record chat flow with the Owner's confirmation.

## Run
Requires Python 3.11+ and, for the terminal pane only, `pywinpty` and `websockets` (**Windows only**;
without them the chart still works and the pane is disabled).

    python chart_planner/server.py 8765      # then open http://127.0.0.1:8765

Use `claude` and/or `codex` only if they are installed and logged in on your machine.

## BingX key (optional, for fills and check)
Create a **read-only** API key (no trade, no withdraw). Put it in the environment that starts the server:

    BINGX_API_KEY=...   BINGX_API_SECRET=...

The key is read at call time; it is never written to disk, logged, returned to the browser, or shown in errors.
Without a key, everything except fills/check still works.

## Safety model
- Servers bind 127.0.0.1 only. The terminal WebSocket rejects any Origin other than this page.
- Only `claude` and `codex` can be started (never a free shell). `agy` (Antigravity) is deliberately
  unsupported: its terms make third-party driving a ban risk.
- The terminal runs *as you*, in this repo: anything an agent CLI can do, it can do here. Do not expose
  the port (no tunnel, no `0.0.0.0`).
- Not financial advice; planning/analysis aid only.

## Known limits
- BingX fills field mapping was written without a live account: **unverified against real data**.
- Match rule is simple on purpose: scale-ins, reversals and overlapping positions are reported
  `ambiguous`, never guessed.
- `match.py` uses `trading_os.live_chart_plan` from this repo, so the app is not standalone yet.

## 中文摘要
在 BingX K 線上畫 Entry / SL / TP，同頁開 Claude / Codex；存檔後用「核對成交」對照真實成交算 R。
只在本機運作、只讀行情、沒有下單功能。API key 只放環境變數、請用唯讀 key，程式不會寫入或記錄。
終端機窗格僅限 Windows。不要把連接埠開到外網。
