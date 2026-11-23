# Third-party notices

Vendored in `chart_planner/vendor/` (unmodified):

| Component | Version | Licence | Notes |
|---|---|---|---|
| TradingView Lightweight Charts | 4.2.0 | Apache-2.0 | Copyright (c) 2024 TradingView, Inc. https://github.com/tradingview/lightweight-charts . Apache-2.0 requires keeping the licence and notices; TradingView also asks products to credit them, so the page footer links to https://www.tradingview.com/ . |
| xterm.js | 5.5.0 | MIT | Copyright (c) 2014 The xterm.js authors; (c) 2012-2013 Christopher Jeffrey. The licence text is in the header of `xterm.css`. |
| xterm addon-fit | 0.10.0 | MIT | Copyright (c) The xterm.js authors. |

Runtime Python packages (installed by the user, not vendored): `pywinpty` (MIT), `websockets` (BSD-3-Clause).

Market data: BingX public klines endpoint and, with the user's own read-only key, BingX history endpoints.
Use is subject to BingX's API terms; this tool does not redistribute BingX data.
