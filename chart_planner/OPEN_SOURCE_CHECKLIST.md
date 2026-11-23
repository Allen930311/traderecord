# Status and known gaps (chart_planner)

Released inside the public edition under **Apache-2.0** (decision 2026-11-23). Third-party licences:
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Verified
- No secrets in the code: the BingX key comes only from the environment and is never stored or logged
  (offline tests check that it does not leak into URLs, results or error messages).
- Plans are git-ignored; the folder holds no personal data or trade records.
- Terminal bridge: localhost only, Origin check, allow-list (`claude`, `codex`); `agy` is excluded.
- Licences of vendored libraries are permissive and compatible; the page footer credits TradingView.

## Not yet verified (please report what you find)
- "成交紀錄" and "核對成交" against a real BingX account with a closed trade: the fills field mapping
  in `fills.normalize` was written without live data.
- The Codex button and "把計畫貼給 CLI" were never exercised end to end.
- Only Chrome was tried.

## Known weaknesses
- The terminal pane is Windows-only (pywinpty); no macOS/Linux path yet.
- A local terminal bridge is powerful: if someone exposes the port, it is remote code execution as the
  user. Only localhost binding and the Origin check protect it; there is no login.
- Three symbols, BingX swap candles only.
- `match.py` depends on `trading_os.live_chart_plan`, so the app is not standalone.
