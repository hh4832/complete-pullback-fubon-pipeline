# Pullback + Fubon Pipeline v2 修改說明

更新日期：2026-08-18

## 一、修改目的

本次更新處理兩項需求：

1. 判斷近期完全沒有選股訊號，究竟是市場條件未成立，還是程式／資料日期問題。
2. 將 pipeline 的持有天數出場條件改成第 35 個交易日觸發，同時保留原本的市場漲跌家數與相關紀錄功能。

## 二、選股零訊號問題

### 發現的問題

舊版在未指定 `signal_date` 時，直接採用收盤價資料的最新日期。FinLab 的外資買賣超資料可能比收盤價晚更新；因此最新交易日可能已經有收盤價，但尚未有外資資料。

入場規則包含：

- 外資買超必須大於 0。
- 外資買超占成交量比例必須為近 3 日新高。

當最新日期的外資資料為空值時，所有股票的外資條件都會變成 `False`，最後出現 0 檔候選股。這種情況看起來像市場沒有訊號，但其實可能是資料尚未更新完整。

### v2 修正

新增「最新共同有效資料日」判斷。自動選擇同時具備以下三種資料的最新日期：

- 收盤價
- 成交量
- 外資買賣超

若最新收盤價日期晚於共同有效資料日期，程式會顯示提示，並退回共同有效日期計算訊號，避免產生假性零訊號。

### 新增個別條件計數與累積交集漏斗

每日執行時首先顯示指定訊號日各條件「單獨」通過的股票數量：

- `stock_regime_filter`：個股趨勢結構條件
- `bias_filter`：BIAS 跌深條件
- `macd_osc_rising`：MACD 柱狀體回升
- `foreign_buy_positive`：外資買超為正
- `foreign_ratio_new_high`：外資買超占量近 3 日新高
- `foreign_filter`：完整外資條件
- `signal_before_market`：套用大盤濾網前的訊號
- `market_filter_pass`：當日大盤濾網是否通過
- `signal_close_day`：最終選股訊號

個別條件各自有股票通過，不代表這些股票必然重疊。例如 BIAS 有 130 檔、MACD 有 546 檔、外資條件有 425 檔，同時符合全部條件的交集仍可能是 0。

因此後續再加入「累積 AND 交集漏斗」，按照實際入場公式逐項疊加條件：

```text
Signal-date cumulative funnel:
  stock regime
  + BIAS filter
  + MACD osc rising
  + foreign buy positive
  + foreign ratio 3D high
  + market filter
```

每一列均代表「上一列留下的股票，再加入本列條件」後的剩餘數量，因此數量原則上只能持平或下降。這能直接判斷訊號在哪個條件加入後歸零。

判讀方式：

- `stock regime` 後歸零：個股趨勢結構無標的通過。
- `+ BIAS filter` 後大幅下降：當日跌深股票較少，或跌深股票多處於空頭排列。
- `+ MACD osc rising` 後歸零：跌深股票尚未出現 MACD 柱狀體回升。
- `+ foreign buy positive` 後歸零：前述標的當日沒有外資買超。
- `+ foreign ratio 3D high` 後歸零：雖有外資買超，但買超占量未達近 3 日新高。
- `+ market filter` 後歸零：個股條件成立，但大盤環境濾網未通過。

程式也會核對累積漏斗最後的個股條件交集是否等於 `signal_before_market`。若不一致，會顯示警告，提示入場公式與診斷漏斗可能沒有同步。

此項修改只增加診斷輸出與一致性檢查，不改變任何選股布林條件、候選股名單或 order intent。

## 三、持有 35 天出場

### 舊版

- pipeline 執行設定仍是 `min_holding_trading_days=60`。
- 判斷式使用 `holding_trading_days > threshold`。
- 即使 threshold 設為 35，也會等到第 36 個交易日才觸發。

### v2

- 預設門檻改為 `min_holding_trading_days=35`。
- 判斷式改為：

```python
holding_trading_days >= 35
```

- 第 35 個交易日即列入出場觀察。
- Excel 工作表名稱由「持有超過60天」改為「持有達35天」。
- 完整 pipeline 與單獨選股入口均強制使用相同的 Day-35 設定，避免從不同入口執行時套用不同參數。

## 四、維持不變的功能

以下功能未改動：

- TWSE／TPEX 市場快照取得
- 上漲、下跌與平盤家數統計
- 持股市場廣度統計
- Google Sheet「市場廣度」寫入
- 富邦成交紀錄與庫存取得
- FIFO 持股批次重建
- MFE 與浮盈回吐紀錄
- 策略帳本、order intent 與庫存核對
- 原有 MFE 出場觀察條件
- 停損設定仍為 -15%
- 最大持股数仍為 20 檔，每檔配置仍為 5%

本次只更改「持有天數出場門檻」這一個核心策略變數；MFE 條件仍作為原有的額外出場觀察條件。

## 五、驗證結果

- 所有 Python 檔案均通過 `py_compile` 語法檢查。
- Notebook 通過 JSON 結構檢查。
- 使用合成資料驗證：當 8/18 有收盤價但外資資料為空時，程式會正確退回外資資料完整的 8/17。
- 使用合成布林條件驗證累積漏斗，各層數量依序由 5 → 3 → 2 → 1 → 1，且最後結果與 `signal_before_market`、`signal_close_day` 一致。
- 出場比較使用 `>= 35`，避免原本的 off-by-one 問題。

目前工作環境未安裝完整的 FinLab／Fubon 執行相依套件，因此尚未在此環境完成即時市場與券商 API 的端到端測試。請在原本的 Anaconda／FubonAPI 環境執行 Notebook；v2 的逐層診斷輸出可用來確認最新一天是否真的沒有訊號。

## 六、v2 檔案

- `Complete_Pullback_Fubon_Pipeline_v2.ipynb`
- `complete_pullback_fubon_pipeline_v2.py`
- `integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2.py`
- `integrated_stock_pipeline_strategy_complete_v2.py`
- `pullback_macdonly_daily_selector_v2.py`
- `run_complete_pullback_fubon_pipeline_v2.py`

Python 檔案彼此的 `import` 已同步改成 `_v2` 模組名稱，因此六個 v2 檔案必須放在同一個資料夾中執行。
