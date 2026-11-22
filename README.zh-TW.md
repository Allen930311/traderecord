# traderecord

**以證據為核心的交易紀錄引擎，給人類與 AI Agent 一起使用。**

traderecord 為每筆交易保留一份不可悄悄改寫的紀錄：發生了什麼、當時為什麼進場、圖表證據，以及事後的獨立檢討。AI Agent 可以協助維護，但永遠不是「什麼是真的」的權威。

> **狀態：早期公開版（v0.1）。** 引擎取自一個已實際運作數月的私有 repo。公開版不含任何真實交易資料，因此部分原始測試與示範資料尚未納入（見下方路線圖）。

## 四個獨立操作

| 操作 | 用途 |
|---|---|
| **Sync** | 唯讀取得券商／平台資料，產生確定性的帳本。 |
| **Register** | 經 Owner 確認事實、理由與圖表證據後，才建立交易紀錄。 |
| **Review** | 對單筆已登錄交易做獨立檢討，只保存 Owner 確認的結論。 |
| **Analyze** | 描述性統計，以及明確要求時的跨交易診斷。 |

## 設計原則

- Agent 提案、Owner 確認一次、凍結後的登錄封包才會被執行。
- 證據以 SHA-256 內容定址，無法被悄悄替換。
- 紀錄只增不改；修正是新紀錄取代舊紀錄。
- 依能力路由，不依 Agent 名稱。
- 每筆交易綁定登錄當時凍結的策略規則版本。

## 快速開始

需要 Python 3.10 以上，從 checkout 直接執行：

```bash
git clone https://github.com/Allen930311/traderecord.git
cd traderecord
pip install pyyaml pytest
python -m pytest
python -m trading_os --help
```

[`registry/`](registry/) 內的策略都是**合成範例**，請換成你自己的策略定義。

## 路線圖

- 合成示範資料，讓 register、review、analyze 能完整跑通。
- 用合成 fixture 還原依賴既有紀錄的其餘測試。
- 可 `pip install` 的套件。

## 免責聲明

本專案是紀錄工具，不是投資建議，也不是下單或訊號系統。

## 授權

[Apache-2.0](LICENSE)
