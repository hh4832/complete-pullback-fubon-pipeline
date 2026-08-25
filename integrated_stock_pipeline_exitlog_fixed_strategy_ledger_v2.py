"""
integrated_stock_pipeline.py

整合流程：
1. 使用富邦 API 取得目前庫存（整股 + 零股）與成交紀錄
2. 依 target_date 整理「當日買入 / 當日賣出」
3. 建立市場廣度記錄持股：目前庫存 - target_date 買入 + target_date 賣出
4. holdings_entry_conditions.csv 可維護「突破買進」與「排除」ticker；其餘自動歸類為「回檔」
5. 嘗試抓 TWSE/TPEX 市場廣度並寫入 Google Sheet；若失敗，只警告，不中斷 MFE
6. 對目前庫存做 FIFO、MFE、浮盈回吐分析；MFE end date 使用 target_date/as_of_date
7. 輸出 Excel / parquet，並回傳 result dict 方便 Notebook 檢查

重要限制：
- 富邦 accounting.inventories(account) 通常只能取得「目前」庫存，不是歷史庫存。
- 若 target_date 不是今天，本程式的「目前庫存」仍是執行當下庫存；市場廣度回推只用 target_date 買賣調整。
- 若要嚴格補歷史日期庫存，需要每天保存庫存快照，或從完整成交紀錄重建該日庫存。

必要檔案：
- .env：富邦登入資訊
- service_account.json：Google service account 憑證，不要上傳 GitHub
- holdings_entry_conditions.csv：可填「突破買進」或「排除」股票清單

.env 範例：
FUBON_USER_ID=你的身分證字號
FUBON_PASSWORD=你的富邦密碼
FUBON_CERT_PATH=D:\\path\\to\\certificate.pfx
FUBON_CERT_PASSWORD=憑證密碼  # 若不需要可省略

holdings_entry_conditions.csv 範例：
stock_no,note
2330,突破買進
2454,突破買進
00632R,排除
"""

from __future__ import annotations

import os
import re
import time
import pickle
import traceback
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from tqdm import tqdm

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception:  # 允許只跑富邦/MFE，不寫 Google Sheet
    gspread = None
    Credentials = None

try:
    from fubon_neo.sdk import FubonSDK
except Exception:  # 允許在非富邦環境先檢查語法
    FubonSDK = None

try:
    from fubon_neo.fugle_marketdata.rest.base_rest import FugleAPIError
except Exception:
    FugleAPIError = Exception


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
}


@dataclass
class PipelineConfig:
    # 日期；None = 今天
    target_date: Optional[str] = None  # YYYY-MM-DD

    # Google Sheet
    google_credentials_file: Path = Path("service_account.json")
    spreadsheet_id: str = "1-bs4-2mYutvQcUYY-np5zp-QqEJfab--TAjmfQi8RQY"
    sheet_name: str = "市場廣度"
    enable_google_sheet: bool = True

    # 入場條件：CSV 只放「突破買進」清單，其餘自動視為回檔
    entry_condition_file: Path = Path("holdings_entry_conditions.csv")
    breakthrough_condition: str = "突破"
    default_entry_condition: str = "回檔"
    excluded_condition: str = "排除"
    entry_conditions: tuple[str, ...] = ("突破", "回檔")

    # cache：預設每次更新
    use_cache: bool = False
    cache_dir: Path = Path("./cache_integrated_stock")
    output_dir: Path = Path("./output_integrated_stock")

    # 富邦成交紀錄與 MFE
    lookback_days: int = 365 * 3
    filled_history_chunk_days: int = 29
    trading_api_sleep_sec: float = 2.0
    market_api_sleep_sec: float = 0.2
    adjusted_price: str = "true"
    preserve_fifo_when_trim_excess: bool = True

    # 出場觀察條件
    # 條件 A：MFE > mfe_threshold 且浮盈回吐 > pullback_threshold（百分點）
    # 條件 B：持有交易日 >= min_holding_trading_days
    # 條件 D：收盤未實現虧損 > close_loss_threshold
    mfe_threshold: float = 0.40
    min_holding_trading_days: int = 35
    pullback_threshold: float = 0.25
    close_loss_threshold: float = 0.15

    # 策略帳本：將富邦實際庫存與策略歸屬分開管理
    enable_strategy_ledger: bool = True
    strategy_ledger_dir: Path = Path("./strategy_ledger")
    strategy_registry_file: Path = Path("strategy_registry.parquet")
    strategy_position_lots_file: Path = Path("strategy_position_lots.parquet")
    strategy_execution_log_file: Path = Path("strategy_execution_log.parquet")
    strategy_order_intent_file: Path = Path("order_intent.parquet")
    strategy_reconciliation_file: Path = Path("strategy_reconciliation.parquet")
    legacy_strategy_id: str = "legacy"
    unassigned_strategy_id: str = "unassigned"
    default_strategy_id: str = "pullback_macd_day35_v1"

    # 失敗隔離：TWSE/TPEX 或 Google Sheet 失敗時，不中斷 MFE
    continue_on_market_error: bool = True
    continue_on_gsheet_error: bool = True
    continue_on_mfe_error: bool = True

    # debug
    debug: bool = True

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.google_credentials_file = Path(self.google_credentials_file)
        self.entry_condition_file = Path(self.entry_condition_file)
        self.strategy_ledger_dir = Path(self.strategy_ledger_dir)
        self.strategy_ledger_dir.mkdir(parents=True, exist_ok=True)

    @property
    def as_of_date(self) -> date:
        return datetime.strptime(self.target_date, "%Y-%m-%d").date() if self.target_date else date.today()


# Backward-compatible aliases，避免 Notebook import 名稱不同造成錯誤
IntegratedConfig = PipelineConfig


def run_integrated_pipeline(config: Optional[PipelineConfig] = None) -> dict[str, Any]:
    return run_pipeline(config)


# ============================================================
# 小工具
# ============================================================

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_get(obj: Any, attr: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _cache_path(config: PipelineConfig, name: str) -> Path:
    return config.cache_dir / name


def _save_pickle(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(obj, f)


def _load_pickle(path: Path) -> Any:
    with open(path, "rb") as f:
        return pickle.load(f)


def normalize_stock_no(x: Any) -> str:
    if pd.isna(x):
        return ""
    s = str(x).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def run_required_step(name: str, func: Callable, config: PipelineConfig, *args, **kwargs):
    print(f"\n[STEP] {name}")
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"[ERROR] {name} 失敗：{e}")
        if config.debug:
            traceback.print_exc()
        raise


def run_optional_step(name: str, func: Callable, config: PipelineConfig, *args, default=None, **kwargs):
    print(f"\n[STEP] {name}")
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"[WARNING] {name} 失敗，但流程繼續：{e}")
        if config.debug:
            traceback.print_exc()
        return default


# ============================================================
# 入場條件：只維護突破買進清單
# ============================================================

def ensure_entry_condition_file(config: PipelineConfig) -> pd.DataFrame:
    """
    CSV 支援兩種格式：
    1) 新格式：stock_no,entry_condition,note
       entry_condition 可填「突破」或「排除」。
    2) 相容格式：stock_no,note
       若 note 填「突破」或「排除」，會依 note 判斷；若 note 沒有填條件，沿用舊版邏輯：列出的 stock_no 視為「突破」。

    「排除」用途：避險或非策略持股，例如 00632R。
    排除標的不列入市場廣度，也不列入 MFE / 持有天數計算。
    """
    if not config.entry_condition_file.exists():
        template = pd.DataFrame(columns=["stock_no", "entry_condition", "note"])
        template.to_csv(config.entry_condition_file, index=False, encoding="utf-8-sig")
        print(f"已建立入場條件清單範本：{config.entry_condition_file}")
        print("entry_condition 可填：突破 / 排除；其餘持股會自動歸類為回檔。")
        return template

    df = pd.read_csv(config.entry_condition_file, dtype={"stock_no": str}, encoding="utf-8-sig")
    if "stock_no" not in df.columns:
        raise ValueError(f"{config.entry_condition_file} 缺少 stock_no 欄位。")

    df["stock_no"] = df["stock_no"].map(normalize_stock_no)
    df = df[df["stock_no"].ne("")].drop_duplicates(subset=["stock_no"], keep="last").copy()

    if "note" not in df.columns:
        df["note"] = ""

    valid_conditions = {config.breakthrough_condition, config.excluded_condition}

    if "entry_condition" in df.columns:
        df["entry_condition"] = df["entry_condition"].astype(str).str.strip()
    else:
        # 相容舊 CSV：stock_no,note。若 note 是「突破」或「排除」就採用；否則沿用舊版，視為「突破」。
        note_condition = df["note"].astype(str).str.strip()
        df["entry_condition"] = np.where(
            note_condition.isin(valid_conditions),
            note_condition,
            config.breakthrough_condition,
        )

    df = df[df["entry_condition"].isin(valid_conditions)].copy()
    return df[["stock_no", "entry_condition", "note"]]

def classify_symbols(record_symbols: set[str], entry_df: pd.DataFrame, config: PipelineConfig) -> tuple[dict[str, list[str]], pd.DataFrame]:
    clean_symbols = {normalize_stock_no(s) for s in record_symbols if normalize_stock_no(s)}

    if entry_df.empty:
        breakthrough_set: set[str] = set()
        excluded_set: set[str] = set()
    else:
        tmp = entry_df.copy()
        tmp["stock_no"] = tmp["stock_no"].map(normalize_stock_no)
        tmp["entry_condition"] = tmp["entry_condition"].astype(str).str.strip()
        breakthrough_set = set(tmp.loc[tmp["entry_condition"].eq(config.breakthrough_condition), "stock_no"])
        excluded_set = set(tmp.loc[tmp["entry_condition"].eq(config.excluded_condition), "stock_no"])

    excluded_symbols = clean_symbols & excluded_set
    active_symbols = clean_symbols - excluded_symbols

    classified = {
        config.breakthrough_condition: sorted(active_symbols & breakthrough_set),
        config.default_entry_condition: sorted(active_symbols - breakthrough_set),
        config.excluded_condition: sorted(excluded_symbols),
    }

    rows = []
    for cond, symbols in classified.items():
        for s in symbols:
            rows.append({"stock_no": s, "entry_condition": cond})
    classified_df = pd.DataFrame(rows, columns=["stock_no", "entry_condition"])
    return classified, classified_df


# ============================================================
# 富邦 API：登入、庫存、成交
# ============================================================

def normalize_side(x: Any) -> str:
    s = str(x)
    s_lower = s.lower()
    if "buy" in s_lower or "買" in s:
        return "buy"
    if "sell" in s_lower or "賣" in s:
        return "sell"
    return "unknown"


def login_fubon_from_env() -> tuple[Any, Any]:
    if FubonSDK is None:
        raise RuntimeError("找不到 fubon_neo SDK。請先確認目前 Python 環境可以 import fubon_neo。")

    load_dotenv()
    user_id = os.getenv("FUBON_USER_ID")
    password = os.getenv("FUBON_PASSWORD")
    cert_path = os.getenv("FUBON_CERT_PATH")
    cert_password = os.getenv("FUBON_CERT_PASSWORD")

    missing = [
        name for name, value in {
            "FUBON_USER_ID": user_id,
            "FUBON_PASSWORD": password,
            "FUBON_CERT_PATH": cert_path,
        }.items() if not value
    ]
    if missing:
        raise ValueError(f".env 缺少必要欄位：{', '.join(missing)}")

    sdk = FubonSDK()
    accounts = sdk.login(user_id, password, cert_path, cert_password) if cert_password else sdk.login(user_id, password, cert_path)
    if not accounts.is_success:
        raise RuntimeError(f"登入失敗：{accounts.message}")
    if not accounts.data:
        raise RuntimeError("登入成功但沒有回傳帳號資料。")

    try:
        sdk.init_realtime()
    except Exception:
        pass

    return sdk, accounts.data[0]


def fetch_inventory(sdk: Any, account: Any, config: PipelineConfig) -> pd.DataFrame:
    cp = _cache_path(config, f"inventory_current_{date.today().strftime('%Y%m%d')}.pkl")
    if config.use_cache and cp.exists():
        return _load_pickle(cp)

    result = sdk.accounting.inventories(account)
    if not result.is_success:
        raise RuntimeError(f"庫存查詢失敗：{result.message}")

    rows: list[dict[str, Any]] = []
    for inv in result.data or []:
        odd = _safe_get(inv, "odd")
        today_qty = int(_safe_get(inv, "today_qty", 0) or 0)
        odd_today_qty = int(_safe_get(odd, "today_qty", 0) or 0)
        total_today_qty = today_qty + odd_today_qty
        rows.append({
            "date": _safe_get(inv, "date"),
            "account": _safe_get(inv, "account"),
            "branch_no": _safe_get(inv, "branch_no"),
            "stock_no": normalize_stock_no(_safe_get(inv, "stock_no", "")),
            "order_type": str(_safe_get(inv, "order_type", "")),
            "today_qty": today_qty,
            "odd_today_qty": odd_today_qty,
            "total_today_qty": total_today_qty,
            "tradable_qty": int(_safe_get(inv, "tradable_qty", 0) or 0),
            "odd_tradable_qty": int(_safe_get(odd, "tradable_qty", 0) or 0),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[df["total_today_qty"].gt(0)].copy()
        df = df.sort_values("stock_no").reset_index(drop=True)

    _save_pickle(df, cp)
    return df


def iter_backward_chunks(end: date, lookback_days: int, chunk_days: int) -> list[tuple[date, date]]:
    start = end - timedelta(days=lookback_days)
    chunks: list[tuple[date, date]] = []
    chunk_end = end
    while chunk_end >= start:
        chunk_start = max(start, chunk_end - timedelta(days=chunk_days))
        chunks.append((chunk_start, chunk_end))
        chunk_end = chunk_start - timedelta(days=1)
    return list(reversed(chunks))


def fetch_filled_history(sdk: Any, account: Any, config: PipelineConfig, end_date: date) -> pd.DataFrame:
    chunks = iter_backward_chunks(end=end_date, lookback_days=config.lookback_days, chunk_days=config.filled_history_chunk_days)
    all_rows: list[dict[str, Any]] = []

    for chunk_start, chunk_end in tqdm(chunks, desc="取得成交紀錄"):
        start_ymd = chunk_start.strftime("%Y%m%d")
        end_ymd = chunk_end.strftime("%Y%m%d")
        cp = _cache_path(config, f"filled_history_{start_ymd}_{end_ymd}.pkl")

        if config.use_cache and cp.exists():
            all_rows.extend(_load_pickle(cp))
            continue

        try:
            result = sdk.stock.filled_history(account, start_ymd, end_ymd)
            rows: list[dict[str, Any]] = []
            if result.is_success and result.data is not None:
                for f in result.data:
                    rows.append({
                        "date": _safe_get(f, "date"),
                        "stock_no": normalize_stock_no(_safe_get(f, "stock_no", "")),
                        "buy_sell": str(_safe_get(f, "buy_sell", "")),
                        "qty": _safe_get(f, "filled_qty", 0),
                        "price": _safe_get(f, "filled_price", 0),
                        "time": _safe_get(f, "filled_time", ""),
                        "order_no": _safe_get(f, "order_no", ""),
                        "filled_no": _safe_get(f, "filled_no", ""),
                    })
                print(f"{start_ymd} ~ {end_ymd} OK {len(rows)}")
            else:
                print(f"{start_ymd} ~ {end_ymd} FAIL {getattr(result, 'message', '')}")

            _save_pickle(rows, cp)
            all_rows.extend(rows)
            time.sleep(config.trading_api_sleep_sec)
        except Exception as e:
            print(f"[警告] 成交紀錄抓取失敗：{start_ymd} ~ {end_ymd}: {e}")
            if config.debug:
                traceback.print_exc()
            time.sleep(config.trading_api_sleep_sec)

    df = pd.DataFrame(all_rows)
    cols = ["date", "stock_no", "buy_sell", "qty", "price", "time", "order_no", "filled_no", "side"]
    if df.empty:
        return pd.DataFrame(columns=cols)

    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["stock_no"] = df["stock_no"].map(normalize_stock_no)
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["side"] = df["buy_sell"].apply(normalize_side)
    dedup_cols = ["date", "stock_no", "side", "qty", "price", "time", "order_no", "filled_no"]
    df = df.drop_duplicates(subset=dedup_cols)
    df = df.sort_values(["date", "time", "stock_no"]).reset_index(drop=True)
    return df[cols]


def get_target_date_trades(trades: pd.DataFrame, target: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    if trades.empty:
        return trades.copy(), trades.copy()
    day_df = trades[trades["date"].eq(target)].copy()
    buy_df = day_df[day_df["side"].eq("buy")].copy()
    sell_df = day_df[day_df["side"].eq("sell")].copy()
    return buy_df, sell_df


def build_breadth_record_symbols(inventory: pd.DataFrame, target_buy: pd.DataFrame, target_sell: pd.DataFrame) -> set[str]:
    """今日廣度記錄持股 = 目前庫存 - target_date 買入 + target_date 賣出。"""
    current_symbols = set(inventory["stock_no"].map(normalize_stock_no)) if not inventory.empty else set()
    buy_symbols = set(target_buy["stock_no"].map(normalize_stock_no)) if not target_buy.empty else set()
    sell_symbols = set(target_sell["stock_no"].map(normalize_stock_no)) if not target_sell.empty else set()
    return {s for s in ((current_symbols - buy_symbols) | sell_symbols) if s}


# ============================================================
# 市場廣度：TWSE / TPEX
# ============================================================

def get_tick_size_series(prev: pd.Series) -> pd.Series:
    return pd.cut(
        prev,
        bins=[0, 10, 50, 100, 500, 1000, float("inf")],
        labels=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
    ).astype(float)


def add_limit_prices(df: pd.DataFrame, prev_close_col: str = "前收") -> pd.DataFrame:
    prev = df[prev_close_col]
    tick = get_tick_size_series(prev)
    df["漲停價"] = (np.floor(prev * 1.1 / tick) * tick).round(2)
    df["跌停價"] = (np.ceil(prev * 0.9 / tick) * tick).round(2)
    return df


def fetch_common_stock_set(mode: int) -> set[str]:
    """Return listed/OTC company tickers from official JSON OpenAPI.

    The former ISIN HTML page is not reliable from GitHub-hosted runners and
    its markup can change.  Company-profile OpenAPI datasets naturally exclude
    ETFs, warrants and other exchange-traded products, which is the universe
    needed for the ordinary-stock breadth calculation.
    """
    sources = {
        2: ("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", "公司代號"),
        4: ("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", "SecuritiesCompanyCode"),
    }
    if mode not in sources:
        raise ValueError(f"不支援的普通股清單 mode：{mode}")

    time.sleep(1)
    url, code_field = sources[mode]
    r = requests.get(url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"普通股清單格式異常：mode={mode}")

    tickers = {
        normalize_stock_no(row.get(code_field))
        for row in data
        if isinstance(row, dict) and row.get(code_field) is not None
    }
    # Taiwanese ordinary company tickers are four numeric digits.  This also
    # protects the breadth universe if an OpenAPI response later adds metadata.
    tickers = {ticker for ticker in tickers if re.fullmatch(r"\d{4}", ticker)}
    if not tickers:
        raise RuntimeError(f"普通股清單抓取失敗：mode={mode}")
    return tickers


def fetch_twse(date_str: str) -> dict[str, Any]:
    time.sleep(2)
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={date_str}&type=ALL&response=json"
    r = requests.get(url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        raise RuntimeError(f"證交所資料異常：{data.get('stat')}")
    return data


def fetch_tpex_highlight(target: date) -> dict[str, Any]:
    time.sleep(1)
    roc_year = target.year - 1911
    tpex_date = f"{roc_year}/{target.strftime('%m/%d')}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/market_highlight/highlight_result.php?l=zh-tw&d={tpex_date}"
    r = requests.get(url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def fetch_tpex_stocks(target: date) -> dict[str, Any]:
    time.sleep(1)
    roc_year = target.year - 1911
    tpex_date = f"{roc_year}/{target.strftime('%m/%d')}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={tpex_date}&se=AL&s=0,asc&o=json"
    r = requests.get(url, timeout=30, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def parse_taiex(twse_data: dict[str, Any]) -> Optional[float]:
    for row in twse_data["tables"][0]["data"]:
        if "發行量加權股價指數" in row[0]:
            return float(str(row[4]).replace(",", ""))
    return None


def parse_otc_pct(tpex_highlight: dict[str, Any]) -> Optional[float]:
    fields = tpex_highlight["tables"][0]["fields"]
    row = tpex_highlight["tables"][0]["data"][0]
    d = dict(zip(fields, row))
    close = float(str(d["收市指數"]).replace(",", ""))
    change = float(str(d["指數漲跌"]).replace(",", ""))
    prev = close - change
    return round(change / prev * 100, 2) if prev != 0 else None


def parse_twse_stocks(twse_data: dict[str, Any], common_set: set[str]) -> pd.DataFrame:
    table = twse_data["tables"][8]
    df = pd.DataFrame(table["data"], columns=table["fields"])
    df["證券代號"] = df["證券代號"].map(normalize_stock_no)
    df = df[df["證券代號"].isin(common_set)].copy()
    df = df[pd.to_numeric(df["成交股數"].str.replace(",", "", regex=False), errors="coerce").fillna(0).gt(0)]

    df["收盤價"] = pd.to_numeric(df["收盤價"].str.replace(",", "", regex=False), errors="coerce")
    df["漲跌價差"] = pd.to_numeric(df["漲跌價差"].str.replace(",", "", regex=False), errors="coerce").abs()
    df["方向"] = df["漲跌(+/-)"].apply(lambda x: -1 if "green" in str(x) else 1)
    df["前收"] = df["收盤價"] - df["方向"] * df["漲跌價差"]
    df["漲跌幅"] = df["方向"] * df["漲跌價差"] / df["前收"] * 100
    df.loc[df["漲跌價差"].eq(0), "漲跌幅"] = 0
    df = add_limit_prices(df, "前收")
    df = df.dropna(subset=["漲跌幅", "收盤價"])
    return df.set_index("證券代號")[["漲跌幅", "收盤價", "漲停價", "跌停價"]]


def parse_tpex_stocks(tpex_stocks_data: dict[str, Any], common_set: set[str]) -> pd.DataFrame:
    table = tpex_stocks_data["tables"][0]
    fields = [str(f).strip().replace("<br>", "") for f in table["fields"]]
    df = pd.DataFrame(table["data"], columns=fields)
    df["代號"] = df["代號"].map(normalize_stock_no)
    df = df[df["代號"].isin(common_set)].copy()
    df = df[pd.to_numeric(df["成交股數"].str.replace(",", "", regex=False), errors="coerce").fillna(0).gt(0)]

    df["收盤"] = pd.to_numeric(df["收盤"].str.replace(",", "", regex=False), errors="coerce")
    df["漲跌"] = pd.to_numeric(df["漲跌"].str.replace(",", "", regex=False), errors="coerce")
    df["前收"] = df["收盤"] - df["漲跌"]
    df["漲跌幅"] = df["漲跌"] / df["前收"] * 100
    df.loc[df["漲跌"].eq(0), "漲跌幅"] = 0
    df = add_limit_prices(df, "前收")
    df = df.dropna(subset=["漲跌幅", "收盤"])
    df = df.rename(columns={"收盤": "收盤價"})
    return df.set_index("代號")[["漲跌幅", "收盤價", "漲停價", "跌停價"]]


def fetch_market_snapshot(target: date) -> tuple[pd.DataFrame, Optional[float], Optional[float]]:
    api_date = target.strftime("%Y%m%d")
    twse_common = fetch_common_stock_set(2)
    tpex_common = fetch_common_stock_set(4)

    twse_data = fetch_twse(api_date)
    tpex_highlight = fetch_tpex_highlight(target)
    tpex_stocks_data = fetch_tpex_stocks(target)

    taiex_pct = parse_taiex(twse_data)
    otc_pct = parse_otc_pct(tpex_highlight)
    twse_df = parse_twse_stocks(twse_data, twse_common)
    tpex_df = parse_tpex_stocks(tpex_stocks_data, tpex_common)
    all_df = pd.concat([twse_df, tpex_df]).sort_index()
    return all_df, taiex_pct, otc_pct


def empty_breadth_stats(include_vs_market: bool = False) -> dict[str, int]:
    out = {
        "漲停": 0, "大漲": 0, "小漲": 0, "平盤": 0,
        "小跌": 0, "大跌": 0, "跌停": 0,
        "總上漲": 0, "總下跌": 0, "總家數": 0,
    }
    if include_vs_market:
        out["強於大盤"] = 0
        out["弱於大盤"] = 0
    return out


def calc_breadth(stock_df: pd.DataFrame, taiex_pct: Optional[float] = None, include_vs_market: bool = False) -> dict[str, int]:
    if stock_df is None or stock_df.empty:
        return empty_breadth_stats(include_vs_market)

    base = stock_df.copy()
    df = base[base["漲跌幅"].ne(0)].copy()
    if df.empty:
        return empty_breadth_stats(include_vs_market)

    chg = df["漲跌幅"]
    close = df["收盤價"]
    is_limit_up = close.ge(df["漲停價"])
    is_limit_down = close.le(df["跌停價"])

    stats = {
        "漲停": int(is_limit_up.sum()),
        "大漲": int(((chg >= 3) & ~is_limit_up).sum()),
        "小漲": int(((chg > 0) & (chg < 3) & ~is_limit_up).sum()),
        "平盤": int(base["漲跌幅"].eq(0).sum()),
        "小跌": int(((chg < 0) & (chg > -3) & ~is_limit_down).sum()),
        "大跌": int(((chg <= -3) & ~is_limit_down).sum()),
        "跌停": int(is_limit_down.sum()),
    }
    stats["總上漲"] = stats["漲停"] + stats["大漲"] + stats["小漲"]
    stats["總下跌"] = stats["跌停"] + stats["大跌"] + stats["小跌"]
    stats["總家數"] = int(len(df))

    if include_vs_market:
        if taiex_pct is None:
            stats["強於大盤"] = 0
            stats["弱於大盤"] = 0
        else:
            stats["強於大盤"] = int(chg.gt(taiex_pct).sum())
            stats["弱於大盤"] = int(chg.lt(taiex_pct).sum())
    return stats


# ============================================================
# Google Sheet
# ============================================================

def get_breadth_headers(config: PipelineConfig) -> list[str]:
    market_cols = ["總上漲", "漲停", "大漲(>3%)", "小漲", "平盤", "總下跌", "小跌", "大跌(<-3%)", "跌停", "總家數"]
    holding_cols = ["總上漲", "漲停", "大漲(>3%)", "小漲", "平盤", "總下跌", "小跌", "大跌(<-3%)", "跌停", "強於大盤", "弱於大盤", "總家數"]
    headers = [
        "Date", "加權%", "櫃買%",
        "今日買入檔數", "今日賣出檔數", "廣度記錄持股檔數",
        f"{config.breakthrough_condition}檔數", f"{config.default_entry_condition}檔數", f"{config.excluded_condition}檔數",
    ]
    headers += [f"全市場_{c}" for c in market_cols]
    for cond in config.entry_conditions:
        headers += [f"{cond}_{c}" for c in holding_cols]
    headers += [f"全持倉_{c}" for c in holding_cols]
    return headers


def breadth_market_values(stats: dict[str, int]) -> list[int]:
    return [stats.get("總上漲", 0), stats.get("漲停", 0), stats.get("大漲", 0), stats.get("小漲", 0), stats.get("平盤", 0),
            stats.get("總下跌", 0), stats.get("小跌", 0), stats.get("大跌", 0), stats.get("跌停", 0), stats.get("總家數", 0)]


def breadth_holding_values(stats: dict[str, int]) -> list[int]:
    return [stats.get("總上漲", 0), stats.get("漲停", 0), stats.get("大漲", 0), stats.get("小漲", 0), stats.get("平盤", 0),
            stats.get("總下跌", 0), stats.get("小跌", 0), stats.get("大跌", 0), stats.get("跌停", 0),
            stats.get("強於大盤", 0), stats.get("弱於大盤", 0), stats.get("總家數", 0)]


def build_breadth_row(
    target: date,
    taiex_pct: Optional[float],
    otc_pct: Optional[float],
    market_stats: dict[str, int],
    condition_stats: dict[str, dict[str, int]],
    target_buy: pd.DataFrame,
    target_sell: pd.DataFrame,
    record_symbols: set[str],
    classified: dict[str, list[str]],
    config: PipelineConfig,
) -> list[Any]:
    row: list[Any] = [
        target.strftime("%Y-%m-%d"),
        round(taiex_pct, 2) if taiex_pct is not None else "",
        round(otc_pct, 2) if otc_pct is not None else "",
        int(target_buy["stock_no"].nunique()) if not target_buy.empty else 0,
        int(target_sell["stock_no"].nunique()) if not target_sell.empty else 0,
        len(record_symbols),
        len(classified.get(config.breakthrough_condition, [])),
        len(classified.get(config.default_entry_condition, [])),
        len(classified.get(config.excluded_condition, [])),
    ]
    row += breadth_market_values(market_stats)
    for cond in config.entry_conditions:
        row += breadth_holding_values(condition_stats.get(cond, empty_breadth_stats(True)))
    row += breadth_holding_values(condition_stats.get("全持倉", empty_breadth_stats(True)))
    return row


def write_to_gsheet(row_data: list[Any], date_str: str, config: PipelineConfig) -> None:
    if not config.enable_google_sheet:
        print("已略過 Google Sheet 寫入：enable_google_sheet=False")
        return
    if gspread is None or Credentials is None:
        raise RuntimeError("找不到 gspread 或 google.oauth2.service_account，無法寫入 Google Sheet。")
    if not config.google_credentials_file.exists():
        raise FileNotFoundError(f"找不到 Google credentials：{config.google_credentials_file}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(str(config.google_credentials_file), scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.spreadsheet_id).worksheet(config.sheet_name)

    headers = get_breadth_headers(config)
    all_values = sheet.get_all_values()
    has_header = all_values and all_values[0] and all_values[0][0] == "Date"
    if not has_header:
        sheet.insert_row(headers, 1)
        all_values = sheet.get_all_values()
    elif all_values[0] != headers:
        # 欄位新版有變動時，直接更新第一列。避免舊欄位造成錯位。
        sheet.update("1:1", [headers])
        all_values = sheet.get_all_values()

    existing_dates = [r[0] for r in all_values[1:] if r]
    if date_str in existing_dates:
        idx = existing_dates.index(date_str) + 2
        sheet.delete_rows(idx)
        sheet.insert_row(row_data, idx)
        print(f"已更新 Google Sheet：{date_str}")
    else:
        sheet.append_row(row_data)
        print(f"已新增 Google Sheet：{date_str}")


# ============================================================
# FIFO / MFE
# ============================================================

def trim_lots_to_qty(lots: list[dict[str, Any]], target_qty: int, preserve_fifo: bool = True) -> list[dict[str, Any]]:
    if target_qty <= 0:
        return []
    source = lots if preserve_fifo else list(reversed(lots))
    kept: list[dict[str, Any]] = []
    need = target_qty
    for lot in source:
        if need <= 0:
            break
        take = min(int(lot["remaining_qty"]), need)
        new_lot = lot.copy()
        new_lot["remaining_qty"] = take
        kept.append(new_lot)
        need -= take
    if not preserve_fifo:
        kept = list(reversed(kept))
    return kept


def reconstruct_open_lots_fifo(trades: pd.DataFrame, inventory: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    cols = ["stock_no", "buy_date", "remaining_qty", "buy_price"]
    if trades.empty or inventory.empty:
        return pd.DataFrame(columns=cols)

    current_qty_map = inventory.groupby("stock_no")["total_today_qty"].sum().to_dict()
    holding_rows: list[dict[str, Any]] = []

    for stock in sorted(current_qty_map.keys()):
        stock_trades = trades[trades["stock_no"].eq(stock)].sort_values(["date", "time"])
        lots: list[dict[str, Any]] = []

        for _, row in stock_trades.iterrows():
            side = row["side"]
            qty = int(row["qty"])
            if qty <= 0:
                continue
            if side == "buy":
                lots.append({
                    "stock_no": stock,
                    "buy_date": row["date"],
                    "remaining_qty": qty,
                    "buy_price": float(row["price"]),
                })
            elif side == "sell":
                sell_qty = qty
                while sell_qty > 0 and lots:
                    if lots[0]["remaining_qty"] <= sell_qty:
                        sell_qty -= lots[0]["remaining_qty"]
                        lots.pop(0)
                    else:
                        lots[0]["remaining_qty"] -= sell_qty
                        sell_qty = 0

        reconstructed_qty = sum(int(lot["remaining_qty"]) for lot in lots)
        actual_qty = int(current_qty_map.get(stock, 0))

        if reconstructed_qty > actual_qty:
            lots = trim_lots_to_qty(lots, actual_qty, preserve_fifo=config.preserve_fifo_when_trim_excess)
        elif reconstructed_qty < actual_qty:
            print(
                f"[警告] {stock} 交易紀錄重建股數 {reconstructed_qty} < 目前庫存 {actual_qty}；"
                "可能是 lookback_days 不足、匯撥、申購、配股、減資或公司行為。"
            )

        holding_rows.extend([lot for lot in lots if int(lot["remaining_qty"]) > 0])

    df = pd.DataFrame(holding_rows, columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["buy_date"] = pd.to_datetime(df["buy_date"]).dt.date
    return df.sort_values(["stock_no", "buy_date"]).reset_index(drop=True)


def summarize_open_lots(holding_df: pd.DataFrame, as_of_date: date) -> pd.DataFrame:
    if holding_df.empty:
        return pd.DataFrame()
    df = holding_df.copy()
    df["cost_amount"] = df["remaining_qty"] * df["buy_price"]
    summary = (
        df.groupby("stock_no")
        .agg(
            qty=("remaining_qty", "sum"),
            cost_amount=("cost_amount", "sum"),
            first_buy=("buy_date", "min"),
            last_buy=("buy_date", "max"),
            lot_count=("buy_date", "count"),
        )
        .reset_index()
    )
    summary["avg_cost"] = summary["cost_amount"] / summary["qty"]
    summary["as_of_date"] = as_of_date
    summary["holding_trading_days"] = summary["first_buy"].apply(lambda d: max(len(pd.bdate_range(d, as_of_date)) - 1, 0))
    return summary.sort_values("holding_trading_days", ascending=False).reset_index(drop=True)


def fetch_price_history(reststock: Any, symbol: str, start_date: date, end_date: date, config: PipelineConfig) -> pd.DataFrame:
    cp = _cache_path(config, f"price_{symbol}_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}_adj{config.adjusted_price}.pkl")
    if config.use_cache and cp.exists():
        return _load_pickle(cp)

    all_rows: list[dict[str, Any]] = []
    cur = start_date
    while cur <= end_date:
        chunk_end = min(cur + timedelta(days=364), end_date)
        try:
            data = reststock.historical.candles(
                **{
                    "symbol": symbol,
                    "from": cur.strftime("%Y-%m-%d"),
                    "to": chunk_end.strftime("%Y-%m-%d"),
                    "timeframe": "D",
                    "fields": "open,high,low,close,volume,change",
                    "adjusted": config.adjusted_price,
                    "sort": "asc",
                }
            )
            rows = data.get("data", []) if isinstance(data, dict) else []
            all_rows.extend(rows)
            time.sleep(config.market_api_sleep_sec)
        except FugleAPIError as e:
            print(f"[警告] {symbol} 價格抓取失敗 {cur} ~ {chunk_end}: {e}")
            time.sleep(1)
        except Exception as e:
            print(f"[警告] {symbol} 價格抓取失敗 {cur} ~ {chunk_end}: {e}")
            if config.debug:
                traceback.print_exc()
            time.sleep(1)
        cur = chunk_end + timedelta(days=1)

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ["open", "high", "low", "close", "volume", "change"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "high", "close"]).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    _save_pickle(df, cp)
    return df


def calculate_mfe_pullback(position_summary: pd.DataFrame, reststock: Any, config: PipelineConfig, as_of_date: date) -> pd.DataFrame:
    if position_summary.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for _, row in tqdm(position_summary.iterrows(), total=len(position_summary), desc="計算 MFE / 回檔"):
        symbol = str(row["stock_no"])
        start = row["first_buy"]
        avg_cost = float(row["avg_cost"])
        try:
            px = fetch_price_history(reststock, symbol, start, as_of_date, config)
            if px.empty:
                print(f"[警告] {symbol} 無價格資料。")
                continue

            current_price = float(px.iloc[-1]["close"])
            current_date = px.iloc[-1]["date"]
            high_idx = px["high"].idxmax()
            high_since_buy = float(px.loc[high_idx, "high"])
            high_date = px.loc[high_idx, "date"]

            mfe_pct = high_since_buy / avg_cost - 1 if avg_cost > 0 else np.nan
            unrealized_pct = current_price / avg_cost - 1 if avg_cost > 0 else np.nan
            pullback_from_high_pct = 1 - current_price / high_since_buy if high_since_buy > 0 else np.nan
            # 浮盈回吐「百分點」：例如 MFE +40%、目前 +15%，回吐 = 25 個百分點。
            # 這不是 high_since_buy 到 current_price 的價格回落比例。
            profit_giveback_pct_point = mfe_pct - unrealized_pct if avg_cost > 0 else np.nan
            # 保留舊欄位供參考：回吐佔最大浮盈的比例，例如回吐 25 / MFE 40 = 62.5%。
            giveback_of_mfe_pct = profit_giveback_pct_point / mfe_pct if mfe_pct > 0 else np.nan

            rows.append({
                "stock_no": symbol,
                "qty": int(row["qty"]),
                "first_buy": start,
                "last_buy": row["last_buy"],
                "lot_count": int(row["lot_count"]),
                "as_of_date": as_of_date,
                "holding_trading_days": int(row["holding_trading_days"]),
                "avg_cost": avg_cost,
                "current_date": current_date,
                "current_price": current_price,
                "high_date": high_date,
                "high_since_buy": high_since_buy,
                "unrealized_pct": unrealized_pct,
                "mfe_pct": mfe_pct,
                "pullback_from_high_pct": pullback_from_high_pct,
                "profit_giveback_pct_point": profit_giveback_pct_point,
                "giveback_of_mfe_pct": giveback_of_mfe_pct,
            })
        except Exception as e:
            print(f"[警告] {symbol} MFE 計算失敗：{e}")
            if config.debug:
                traceback.print_exc()

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("profit_giveback_pct_point", ascending=False).reset_index(drop=True)


def format_percent_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in ["unrealized_pct", "mfe_pct", "pullback_from_high_pct", "profit_giveback_pct_point", "giveback_of_mfe_pct"]:
        if col in out.columns:
            out[col] = out[col].apply(lambda x: None if pd.isna(x) else round(float(x) * 100, 2))
    for col in ["avg_cost", "current_price", "high_since_buy"]:
        if col in out.columns:
            out[col] = out[col].apply(lambda x: None if pd.isna(x) else round(float(x), 4))
    return out


def build_mfe_alert(mfe: pd.DataFrame, config: PipelineConfig) -> pd.DataFrame:
    """
    出場觀察條件，四種任一成立即列入：
    A. MFE > mfe_threshold 且浮盈回吐 > pullback_threshold（百分點）
    B. 持有交易日 >= min_holding_trading_days
    C. MFE > 10% 且 giveback_of_mfe_pct > 100%，也就是曾經浮盈超過 10% 但已吐光轉虧
    D. 以目前收盤價計算的未實現虧損 > close_loss_threshold

    注意：format_percent_columns() 會把 0.40 轉成 40.00，
    因此這裡用 config.* * 100 比較。
    """
    formatted = format_percent_columns(mfe)
    if formatted.empty:
        return formatted

    # 向後相容：若讀到舊版 parquet / Excel，可能還沒有這個欄位。
    if "profit_giveback_pct_point" not in formatted.columns:
        formatted["profit_giveback_pct_point"] = formatted["mfe_pct"] - formatted["unrealized_pct"]
    if "giveback_of_mfe_pct" not in formatted.columns:
        formatted["giveback_of_mfe_pct"] = np.where(
            formatted["mfe_pct"].gt(0),
            formatted["profit_giveback_pct_point"] / formatted["mfe_pct"] * 100,
            np.nan,
        )

    exit_by_mfe_pullback = (
        formatted["mfe_pct"].gt(config.mfe_threshold * 100)
        & formatted["profit_giveback_pct_point"].gt(config.pullback_threshold * 100)
    )
    exit_by_mfe10_giveback100 = (
        formatted["mfe_pct"].gt(10)
        & formatted["giveback_of_mfe_pct"].gt(100)
    )
    exit_by_holding_days = formatted["holding_trading_days"].ge(config.min_holding_trading_days)
    exit_by_close_loss = formatted["unrealized_pct"].lt(-config.close_loss_threshold * 100)

    formatted["exit_by_mfe_pullback"] = exit_by_mfe_pullback
    formatted["exit_by_mfe10_giveback100"] = exit_by_mfe10_giveback100
    formatted["exit_by_holding_days"] = exit_by_holding_days
    formatted["exit_by_close_loss"] = exit_by_close_loss
    formatted["exit_signal"] = (
        exit_by_mfe_pullback
        | exit_by_mfe10_giveback100
        | exit_by_holding_days
        | exit_by_close_loss
    )

    def reason(row: pd.Series) -> str:
        reasons = []
        if bool(row.get("exit_by_mfe_pullback", False)):
            reasons.append(
                f"MFE>{int(config.mfe_threshold * 100)}% 且浮盈回吐>{int(config.pullback_threshold * 100)}個百分點"
            )
        if bool(row.get("exit_by_mfe10_giveback100", False)):
            reasons.append("MFE>10% 且最大浮盈已吐光轉虧")
        if bool(row.get("exit_by_holding_days", False)):
            reasons.append(f"持有>={config.min_holding_trading_days}個交易日")
        if bool(row.get("exit_by_close_loss", False)):
            reasons.append(f"收盤虧損>{int(config.close_loss_threshold * 100)}%")
        return "；".join(reasons)

    formatted["exit_reason"] = formatted.apply(reason, axis=1)

    return formatted[formatted["exit_signal"]].copy().sort_values(
        [
            "exit_by_holding_days",
            "exit_by_close_loss",
            "exit_by_mfe_pullback",
            "exit_by_mfe10_giveback100",
            "holding_trading_days",
            "profit_giveback_pct_point",
        ],
        ascending=[False, False, False, False, False, False],
    ).reset_index(drop=True)



def update_exit_review_log(
    alert: pd.DataFrame,
    target: date,
    config: PipelineConfig,
    target_sell: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, Path]:
    """
    將出場觀察事件累積寫入固定 parquet。

    注意：這是跨日 event log，不是每日快照。
    每日 Excel / timestamp parquet 仍照舊輸出；本 log 只記錄每個 event_key 第一次觸發。
    """
    log_path = _cache_path(config, "exit_review_log.parquet")

    if log_path.exists():
        old_log = pd.read_parquet(log_path)
    else:
        old_log = pd.DataFrame()

    events: list[pd.DataFrame] = []

    # 1) 實際賣出：只要 target_date 有賣出，就寫入 review log。
    #    這和 MFE alert 無關，避免「真的賣了但因為不在目前庫存/MFE 清單而沒被記錄」。
    if target_sell is not None and not target_sell.empty:
        real_exit = target_sell.copy()
        real_exit["stock_no"] = real_exit["stock_no"].map(normalize_stock_no)
        real_exit = real_exit[real_exit["stock_no"].ne("")].copy()
        if not real_exit.empty:
            # 同一檔同一天多筆成交彙總成一筆，方便 review。
            agg = (
                real_exit.groupby("stock_no", as_index=False)
                .agg(
                    sell_qty=("qty", "sum"),
                    trigger_price=("price", "mean"),
                    sell_time=("time", lambda x: ",".join(map(str, x.dropna().astype(str).unique()))),
                )
            )
            agg["trigger_date"] = target
            agg["rule_name"] = "real_exit"
            agg["is_real_exit"] = True
            agg["is_shadow_exit"] = False
            agg["exit_reason"] = "目標日實際賣出"
            events.append(agg)

    # 2) 出場觀察：MFE / 持有天數等 shadow exit，只記錄第一次觸發。
    if alert is not None and not alert.empty:
        rules = [
            ("mfe_pullback", "exit_by_mfe_pullback"),
            ("mfe10_giveback100", "exit_by_mfe10_giveback100"),
            ("holding_days", "exit_by_holding_days"),
            ("close_loss", "exit_by_close_loss"),
        ]

        for rule_name, flag_col in rules:
            if flag_col not in alert.columns:
                continue
            triggered = alert[alert[flag_col].fillna(False)].copy()
            if triggered.empty:
                continue
            triggered["rule_name"] = rule_name
            triggered["trigger_date"] = triggered.get("as_of_date", target)
            triggered["trigger_price"] = triggered.get("current_price", np.nan)
            triggered["trigger_return_pct"] = triggered.get("unrealized_pct", np.nan)
            triggered["is_real_exit"] = False
            triggered["is_shadow_exit"] = True
            events.append(triggered)

    if not events:
        return old_log, log_path

    today_events = pd.concat(events, ignore_index=True)

    # 只保留常用欄位；缺欄位時自動補 NA，避免未來欄位變動導致中斷。
    keep_cols = [
        "trigger_date",
        "stock_no",
        "rule_name",
        "is_real_exit",
        "is_shadow_exit",
        "first_buy",
        "last_buy",
        "avg_cost",
        "trigger_price",
        "trigger_return_pct",
        "sell_qty",
        "sell_time",
        "current_date",
        "holding_trading_days",
        "mfe_pct",
        "unrealized_pct",
        "profit_giveback_pct_point",
        "giveback_of_mfe_pct",
        "exit_reason",
    ]
    for col in keep_cols:
        if col not in today_events.columns:
            today_events[col] = np.nan
    today_events = today_events[keep_cols].copy()

    today_events["event_key"] = (
        today_events["stock_no"].astype(str)
        + "_"
        + today_events["trigger_date"].astype(str)
        + "_"
        + today_events["first_buy"].astype(str)
        + "_"
        + today_events["rule_name"].astype(str)
    )
    today_events = today_events.drop_duplicates(subset=["event_key"], keep="first").copy()

    if old_log.empty:
        updated_log = today_events.copy()
    else:
        existing_keys = set(old_log["event_key"].astype(str)) if "event_key" in old_log.columns else set()
        new_events = today_events[~today_events["event_key"].astype(str).isin(existing_keys)].copy()
        updated_log = pd.concat([old_log, new_events], ignore_index=True)

    updated_log.to_parquet(log_path, index=False)
    updated_log.to_csv(log_path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
    return updated_log, log_path



# ============================================================
# 策略帳本：券商庫存是真實來源；strategy lots 是策略歸屬來源
# ============================================================

STRATEGY_REGISTRY_COLUMNS = [
    "strategy_id", "strategy_name", "allocated_capital", "max_positions",
    "position_fraction", "is_active", "note",
]

STRATEGY_LOT_COLUMNS = [
    "trade_id", "strategy_id", "stock_no", "entry_date", "entry_price",
    "original_quantity", "remaining_quantity", "planned_exit_date", "stop_price",
    "status", "source", "created_at", "updated_at", "review_required",
]

STRATEGY_EXECUTION_COLUMNS = [
    "execution_key", "date", "stock_no", "side", "qty", "price", "time",
    "order_no", "filled_no", "trade_id", "strategy_id", "allocation_method",
    "review_required", "processed_at",
]


def _ledger_path(config: PipelineConfig, file_value: Path) -> Path:
    p = Path(file_value)
    return p if p.is_absolute() else config.strategy_ledger_dir / p


def _read_parquet_or_empty(path: Path, columns: list[str]) -> pd.DataFrame:
    if path.exists():
        df = pd.read_parquet(path)
        for c in columns:
            if c not in df.columns:
                df[c] = np.nan
        return df
    return pd.DataFrame(columns=columns)


def _write_parquet_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    df.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")


def ensure_strategy_registry(config: PipelineConfig) -> tuple[pd.DataFrame, Path]:
    path = _ledger_path(config, config.strategy_registry_file)
    registry = _read_parquet_or_empty(path, STRATEGY_REGISTRY_COLUMNS)
    defaults = pd.DataFrame([
        {
            "strategy_id": config.legacy_strategy_id,
            "strategy_name": "啟用帳本前既有持股",
            "allocated_capital": np.nan,
            "max_positions": np.nan,
            "position_fraction": np.nan,
            "is_active": True,
            "note": "首次啟用時由富邦現有庫存匯入，不自動套用新策略出場規則",
        },
        {
            "strategy_id": config.unassigned_strategy_id,
            "strategy_name": "待分類成交",
            "allocated_capital": np.nan,
            "max_positions": np.nan,
            "position_fraction": np.nan,
            "is_active": True,
            "note": "富邦成交找不到 order_intent 時暫存於此",
        },
        {
            "strategy_id": config.default_strategy_id,
            "strategy_name": "拉回 MACD Day35",
            "allocated_capital": np.nan,
            "max_positions": 20,
            "position_fraction": 0.05,
            "is_active": True,
            "note": "MACD rising、BIAS 跌深優先、Day35、停損15%、無MFE",
        },
    ])
    registry = pd.concat([registry, defaults], ignore_index=True)
    registry = registry.drop_duplicates(subset=["strategy_id"], keep="first").reset_index(drop=True)
    _write_parquet_csv(registry, path)
    return registry, path


def load_order_intents(config: PipelineConfig) -> pd.DataFrame:
    path = _ledger_path(config, config.strategy_order_intent_file)
    cols = [
        "trade_id", "strategy_id", "signal_date", "order_date", "stock_no",
        "side", "planned_quantity", "planned_price", "order_no", "status",
    ]
    intents = _read_parquet_or_empty(path, cols)
    if intents.empty:
        return intents
    intents["stock_no"] = intents["stock_no"].map(normalize_stock_no)
    intents["side"] = intents["side"].map(normalize_side)
    for col in ["signal_date", "order_date"]:
        intents[col] = pd.to_datetime(intents[col], errors="coerce").dt.date
    return intents


def _execution_key(row: pd.Series) -> str:
    filled_no = str(row.get("filled_no", "") or "").strip()
    if filled_no and filled_no.lower() != "nan":
        return f"filled:{filled_no}"
    return "|".join([
        str(row.get("date", "")), str(row.get("stock_no", "")),
        str(row.get("side", "")), str(row.get("qty", "")),
        str(row.get("price", "")), str(row.get("time", "")),
        str(row.get("order_no", "")),
    ])


def _match_intent(fill: pd.Series, intents: pd.DataFrame) -> Optional[pd.Series]:
    if intents.empty:
        return None
    order_no = str(fill.get("order_no", "") or "").strip()
    candidates = intents.copy()
    if order_no:
        exact = candidates[candidates["order_no"].astype(str).eq(order_no)]
        if not exact.empty:
            return exact.iloc[-1]
    candidates = candidates[
        candidates["stock_no"].eq(normalize_stock_no(fill.get("stock_no", "")))
        & candidates["side"].eq(normalize_side(fill.get("side", "")))
    ]
    fill_date = fill.get("date")
    if fill_date is not None and "order_date" in candidates.columns:
        date_match = candidates[candidates["order_date"].eq(fill_date)]
        if not date_match.empty:
            candidates = date_match
    return candidates.iloc[-1] if not candidates.empty else None


def bootstrap_strategy_lots(
    inventory: pd.DataFrame,
    config: PipelineConfig,
) -> pd.DataFrame:
    now = datetime.now()
    rows = []
    for _, inv in inventory.iterrows():
        stock_no = normalize_stock_no(inv.get("stock_no", ""))
        qty = int(inv.get("total_today_qty", 0) or 0)
        if not stock_no or qty <= 0:
            continue
        rows.append({
            "trade_id": f"legacy_{stock_no}_{now.strftime('%Y%m%d%H%M%S')}",
            "strategy_id": config.legacy_strategy_id,
            "stock_no": stock_no,
            "entry_date": pd.NaT,
            "entry_price": np.nan,
            "original_quantity": qty,
            "remaining_quantity": qty,
            "planned_exit_date": pd.NaT,
            "stop_price": np.nan,
            "status": "open",
            "source": "imported_inventory",
            "created_at": now,
            "updated_at": now,
            "review_required": False,
        })
    return pd.DataFrame(rows, columns=STRATEGY_LOT_COLUMNS)


def _allocate_sell_fifo(
    lots: pd.DataFrame,
    stock_no: str,
    qty: int,
    preferred_strategy_id: Optional[str],
    preferred_trade_id: Optional[str],
) -> tuple[pd.DataFrame, int, str, bool]:
    remaining = int(qty)
    allocation_method = "matched_trade_id" if preferred_trade_id else (
        "matched_strategy_fifo" if preferred_strategy_id else "unmatched_fifo_all_strategies"
    )
    review_required = not bool(preferred_trade_id or preferred_strategy_id)

    mask = lots["stock_no"].eq(stock_no) & pd.to_numeric(lots["remaining_quantity"], errors="coerce").fillna(0).gt(0)
    candidates = lots[mask].copy()
    if preferred_trade_id:
        candidates = candidates[candidates["trade_id"].astype(str).eq(str(preferred_trade_id))]
    elif preferred_strategy_id:
        candidates = candidates[candidates["strategy_id"].astype(str).eq(str(preferred_strategy_id))]

    if candidates.empty and (preferred_trade_id or preferred_strategy_id):
        candidates = lots[mask].copy()
        allocation_method = "fallback_fifo_all_strategies"
        review_required = True

    candidates["entry_date_sort"] = pd.to_datetime(candidates["entry_date"], errors="coerce")
    candidates = candidates.sort_values(["entry_date_sort", "created_at"], na_position="first")

    now = datetime.now()
    for idx in candidates.index:
        if remaining <= 0:
            break
        available = int(lots.at[idx, "remaining_quantity"] or 0)
        used = min(available, remaining)
        lots.at[idx, "remaining_quantity"] = available - used
        lots.at[idx, "updated_at"] = now
        if int(lots.at[idx, "remaining_quantity"]) <= 0:
            lots.at[idx, "status"] = "closed"
        if review_required:
            lots.at[idx, "review_required"] = True
        remaining -= used
    return lots, remaining, allocation_method, review_required


def update_strategy_ledger(
    inventory: pd.DataFrame,
    trades: pd.DataFrame,
    target: date,
    config: PipelineConfig,
) -> dict[str, Any]:
    registry, registry_path = ensure_strategy_registry(config)
    lots_path = _ledger_path(config, config.strategy_position_lots_file)
    execution_path = _ledger_path(config, config.strategy_execution_log_file)
    reconciliation_path = _ledger_path(config, config.strategy_reconciliation_file)

    lots_existed = lots_path.exists()
    lots = _read_parquet_or_empty(lots_path, STRATEGY_LOT_COLUMNS)
    execution_log = _read_parquet_or_empty(execution_path, STRATEGY_EXECUTION_COLUMNS)
    intents = load_order_intents(config)

    # 第一次啟用：現有庫存全部標為 legacy；歷史成交只標記為 bootstrap，避免重播改動庫存。
    if not lots_existed:
        lots = bootstrap_strategy_lots(inventory, config)
        bootstrap_exec = trades.copy()
        if not bootstrap_exec.empty:
            bootstrap_exec["execution_key"] = bootstrap_exec.apply(_execution_key, axis=1)
            bootstrap_exec["trade_id"] = ""
            bootstrap_exec["strategy_id"] = config.legacy_strategy_id
            bootstrap_exec["allocation_method"] = "historical_preledger_bootstrap"
            bootstrap_exec["review_required"] = False
            bootstrap_exec["processed_at"] = datetime.now()
            for c in STRATEGY_EXECUTION_COLUMNS:
                if c not in bootstrap_exec.columns:
                    bootstrap_exec[c] = np.nan
            execution_log = bootstrap_exec[STRATEGY_EXECUTION_COLUMNS].copy()
    else:
        known_keys = set(execution_log["execution_key"].astype(str)) if not execution_log.empty else set()
        fills = trades.copy()
        if not fills.empty:
            fills["execution_key"] = fills.apply(_execution_key, axis=1)
            fills = fills[~fills["execution_key"].astype(str).isin(known_keys)].copy()

        new_exec_rows = []
        for _, fill in fills.sort_values(["date", "time", "stock_no"]).iterrows():
            intent = _match_intent(fill, intents)
            strategy_id = str(intent.get("strategy_id")) if intent is not None and pd.notna(intent.get("strategy_id")) else config.unassigned_strategy_id
            trade_id = str(intent.get("trade_id")) if intent is not None and pd.notna(intent.get("trade_id")) else ""
            side = normalize_side(fill.get("side", ""))
            stock_no = normalize_stock_no(fill.get("stock_no", ""))
            qty = int(fill.get("qty", 0) or 0)
            price = pd.to_numeric(pd.Series([fill.get("price")]), errors="coerce").iloc[0]
            allocation_method = "matched_order_intent" if intent is not None else "unmatched"
            review_required = intent is None

            if side == "buy" and qty > 0:
                if not trade_id:
                    trade_id = f"{strategy_id}_{stock_no}_{fill.get('date')}_{fill.get('filled_no') or fill.get('order_no') or _timestamp()}"
                lot = {
                    "trade_id": trade_id,
                    "strategy_id": strategy_id,
                    "stock_no": stock_no,
                    "entry_date": fill.get("date"),
                    "entry_price": price,
                    "original_quantity": qty,
                    "remaining_quantity": qty,
                    "planned_exit_date": intent.get("planned_exit_date") if intent is not None and "planned_exit_date" in intent.index else pd.NaT,
                    "stop_price": intent.get("stop_price") if intent is not None and "stop_price" in intent.index else np.nan,
                    "status": "open",
                    "source": "matched_order_intent" if intent is not None else "manual_or_unknown_buy",
                    "created_at": datetime.now(),
                    "updated_at": datetime.now(),
                    "review_required": review_required,
                }
                lots = pd.concat([lots, pd.DataFrame([lot])], ignore_index=True)
            elif side == "sell" and qty > 0:
                lots, unallocated_qty, allocation_method, sell_review = _allocate_sell_fifo(
                    lots,
                    stock_no,
                    qty,
                    strategy_id if intent is not None else None,
                    trade_id if trade_id else None,
                )
                review_required = review_required or sell_review or unallocated_qty > 0
                if unallocated_qty > 0:
                    allocation_method += f"_unallocated_{unallocated_qty}"

            new_exec_rows.append({
                "execution_key": fill.get("execution_key"),
                "date": fill.get("date"),
                "stock_no": stock_no,
                "side": side,
                "qty": qty,
                "price": price,
                "time": fill.get("time", ""),
                "order_no": fill.get("order_no", ""),
                "filled_no": fill.get("filled_no", ""),
                "trade_id": trade_id,
                "strategy_id": strategy_id,
                "allocation_method": allocation_method,
                "review_required": review_required,
                "processed_at": datetime.now(),
            })

        if new_exec_rows:
            execution_log = pd.concat([execution_log, pd.DataFrame(new_exec_rows)], ignore_index=True)

    if not lots.empty:
        lots["stock_no"] = lots["stock_no"].map(normalize_stock_no)
        lots["original_quantity"] = pd.to_numeric(lots["original_quantity"], errors="coerce").fillna(0).astype(int)
        lots["remaining_quantity"] = pd.to_numeric(lots["remaining_quantity"], errors="coerce").fillna(0).astype(int)
        lots["status"] = np.where(lots["remaining_quantity"].gt(0), "open", "closed")
        lots = lots.drop_duplicates(subset=["trade_id"], keep="last").reset_index(drop=True)

    if not execution_log.empty:
        execution_log = execution_log.drop_duplicates(subset=["execution_key"], keep="last").reset_index(drop=True)

    broker_qty = (
        inventory.groupby("stock_no", as_index=False)["total_today_qty"].sum()
        if not inventory.empty else pd.DataFrame(columns=["stock_no", "total_today_qty"])
    )
    ledger_open = lots[pd.to_numeric(lots["remaining_quantity"], errors="coerce").fillna(0).gt(0)].copy()
    ledger_qty = (
        ledger_open.groupby("stock_no", as_index=False)["remaining_quantity"].sum()
        if not ledger_open.empty else pd.DataFrame(columns=["stock_no", "remaining_quantity"])
    )
    reconciliation = broker_qty.merge(ledger_qty, on="stock_no", how="outer")
    reconciliation["total_today_qty"] = pd.to_numeric(reconciliation["total_today_qty"], errors="coerce").fillna(0).astype(int)
    reconciliation["remaining_quantity"] = pd.to_numeric(reconciliation["remaining_quantity"], errors="coerce").fillna(0).astype(int)
    reconciliation["difference"] = reconciliation["total_today_qty"] - reconciliation["remaining_quantity"]
    reconciliation["reconciliation_date"] = target
    reconciliation["status"] = np.where(reconciliation["difference"].eq(0), "OK", "REVIEW")
    reconciliation = reconciliation.sort_values(["status", "stock_no"], ascending=[False, True]).reset_index(drop=True)

    _write_parquet_csv(lots, lots_path)
    _write_parquet_csv(execution_log, execution_path)
    # reconciliation 是每日快照，固定檔 append 並以 date+stock 去重
    old_recon = _read_parquet_or_empty(reconciliation_path, list(reconciliation.columns))
    recon_all = pd.concat([old_recon, reconciliation], ignore_index=True)
    recon_all = recon_all.drop_duplicates(subset=["reconciliation_date", "stock_no"], keep="last")
    _write_parquet_csv(recon_all, reconciliation_path)

    return {
        "registry": registry,
        "lots": lots,
        "execution_log": execution_log,
        "reconciliation": reconciliation,
        "paths": {
            "strategy_registry": registry_path,
            "strategy_position_lots": lots_path,
            "strategy_execution_log": execution_path,
            "strategy_reconciliation": reconciliation_path,
            "strategy_order_intent": _ledger_path(config, config.strategy_order_intent_file),
        },
    }

# ============================================================
# 輸出
# ============================================================

def export_results(
    target: date,
    inventory: pd.DataFrame,
    trades: pd.DataFrame,
    target_buy: pd.DataFrame,
    target_sell: pd.DataFrame,
    record_symbols: set[str],
    classified_df: pd.DataFrame,
    market_snapshot: Optional[pd.DataFrame],
    market_stats: Optional[dict[str, int]],
    condition_stats: Optional[dict[str, dict[str, int]]],
    holding_lots: pd.DataFrame,
    summary: pd.DataFrame,
    mfe: pd.DataFrame,
    alert: pd.DataFrame,
    strategy_ledger: Optional[dict[str, Any]],
    errors: list[dict[str, str]],
    config: PipelineConfig,
) -> dict[str, Path]:
    ts = _timestamp()
    excel_path = config.output_dir / f"integrated_stock_report_{target.strftime('%Y%m%d')}_{ts}.xlsx"
    mfe_path = config.output_dir / f"integrated_mfe_all_{target.strftime('%Y%m%d')}_{ts}.parquet"
    alert_path = config.output_dir / f"integrated_exit_watch_{target.strftime('%Y%m%d')}_{ts}.parquet"

    record_df = pd.DataFrame({"stock_no": sorted(record_symbols)})
    errors_df = pd.DataFrame(errors)
    market_stats_df = pd.DataFrame([market_stats]) if market_stats else pd.DataFrame()
    if condition_stats:
        condition_stats_df = pd.DataFrame([
            {"entry_condition": cond, **stats} for cond, stats in condition_stats.items()
        ])
    else:
        condition_stats_df = pd.DataFrame()

    exit_review_log, exit_review_log_path = update_exit_review_log(alert, target, config, target_sell=target_sell)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        inventory.to_excel(writer, sheet_name="目前庫存", index=False)
        trades.to_excel(writer, sheet_name="成交明細", index=False)
        target_buy.to_excel(writer, sheet_name="目標日買入", index=False)
        target_sell.to_excel(writer, sheet_name="目標日賣出", index=False)
        record_df.to_excel(writer, sheet_name="廣度記錄持股", index=False)
        classified_df.to_excel(writer, sheet_name="入場條件分類", index=False)
        if market_snapshot is not None and not market_snapshot.empty:
            market_snapshot.reset_index(names="stock_no").to_excel(writer, sheet_name="市場快照", index=False)
        market_stats_df.to_excel(writer, sheet_name="市場廣度統計", index=False)
        condition_stats_df.to_excel(writer, sheet_name="持股廣度統計", index=False)
        holding_lots.to_excel(writer, sheet_name="持股批次_FIFO", index=False)
        summary.to_excel(writer, sheet_name="持股彙總", index=False)
        format_percent_columns(mfe).to_excel(writer, sheet_name="MFE_全部", index=False)
        alert.to_excel(writer, sheet_name="出場觀察", index=False)
        exit_review_log.to_excel(writer, sheet_name="出場觀察_log", index=False)
        if not alert.empty and "exit_by_mfe_pullback" in alert.columns:
            alert[alert["exit_by_mfe_pullback"]].to_excel(writer, sheet_name="MFE回檔出場", index=False)
            alert[alert["exit_by_holding_days"]].to_excel(writer, sheet_name="持有達35天", index=False)
        else:
            pd.DataFrame().to_excel(writer, sheet_name="MFE回檔出場", index=False)
            pd.DataFrame().to_excel(writer, sheet_name="持有達35天", index=False)
        if not alert.empty and "exit_by_close_loss" in alert.columns:
            alert[alert["exit_by_close_loss"]].to_excel(writer, sheet_name="收盤虧損逾15pct", index=False)
        else:
            pd.DataFrame().to_excel(writer, sheet_name="收盤虧損逾15pct", index=False)
        # 相容舊版命名，也保留 MFE_警示，內容等同出場觀察
        alert.to_excel(writer, sheet_name="MFE_警示", index=False)
        if strategy_ledger:
            strategy_ledger.get("registry", pd.DataFrame()).to_excel(writer, sheet_name="策略_registry", index=False)
            strategy_ledger.get("lots", pd.DataFrame()).to_excel(writer, sheet_name="策略持倉_lots", index=False)
            strategy_ledger.get("execution_log", pd.DataFrame()).to_excel(writer, sheet_name="策略成交_log", index=False)
            strategy_ledger.get("reconciliation", pd.DataFrame()).to_excel(writer, sheet_name="策略庫存核對", index=False)
        errors_df.to_excel(writer, sheet_name="錯誤紀錄", index=False)

    if not mfe.empty:
        mfe.to_parquet(mfe_path, index=False)
    if not alert.empty:
        alert.to_parquet(alert_path, index=False)

    output_paths = {"excel": excel_path, "mfe_parquet": mfe_path, "alert_parquet": alert_path, "exit_review_log": exit_review_log_path}
    if strategy_ledger:
        output_paths.update(strategy_ledger.get("paths", {}))
    return output_paths


# ============================================================
# 主流程
# ============================================================

def run_pipeline(config: Optional[PipelineConfig] = None) -> dict[str, Any]:
    config = config or PipelineConfig()
    target = config.as_of_date
    date_str = target.strftime("%Y-%m-%d")
    today = date.today()
    errors: list[dict[str, str]] = []

    print("版本確認：integrated_stock_pipeline v2.1-strategy-ledger")
    print(f"目標日期 target/as_of_date：{date_str}")
    print(f"cache：{'使用 cache' if config.use_cache else '每次更新'}")
    if target != today:
        print("[注意] target_date 不是今天：富邦『目前庫存』仍是執行當下庫存，不是歷史庫存快照。")

    entry_df = run_required_step("讀取入場條件清單", ensure_entry_condition_file, config, config)
    sdk, account = run_required_step("登入富邦 API", login_fubon_from_env, config)

    # 預先建立空物件，確保即使某段失敗仍可輸出部分結果
    inventory = pd.DataFrame()
    trades = pd.DataFrame()
    target_buy = pd.DataFrame()
    target_sell = pd.DataFrame()
    record_symbols: set[str] = set()
    classified: dict[str, list[str]] = {config.breakthrough_condition: [], config.default_entry_condition: [], config.excluded_condition: []}
    classified_df = pd.DataFrame(columns=["stock_no", "entry_condition"])
    market_snapshot = None
    taiex_pct = None
    otc_pct = None
    market_stats = None
    condition_stats = None
    holding_lots = pd.DataFrame()
    summary = pd.DataFrame()
    mfe = pd.DataFrame()
    alert = pd.DataFrame()
    strategy_ledger: Optional[dict[str, Any]] = None
    paths: dict[str, Path] = {}

    try:
        # A. 富邦交易/庫存：必要。失敗就無法建立 universe 與 MFE。
        inventory = run_required_step("取得目前庫存（整股 + 零股）", fetch_inventory, config, sdk, account, config)
        trades = run_required_step("取得成交紀錄", fetch_filled_history, config, sdk, account, config, target)
        target_buy, target_sell = run_required_step("整理目標日買入 / 賣出", get_target_date_trades, config, trades, target)
        record_symbols = run_required_step("建立市場廣度記錄持股 universe", build_breadth_record_symbols, config, inventory, target_buy, target_sell)
        classified, classified_df = run_required_step("依條件分類：突破 / 回檔 / 排除", classify_symbols, config, record_symbols, entry_df, config)
        excluded_symbols = set(classified.get(config.excluded_condition, []))
        if excluded_symbols:
            record_symbols = {s for s in record_symbols if s not in excluded_symbols}

        print("\n[INFO] 目標日買入：", sorted(target_buy["stock_no"].unique()) if not target_buy.empty else [])
        print("[INFO] 目標日賣出：", sorted(target_sell["stock_no"].unique()) if not target_sell.empty else [])
        print("[INFO] 排除標的：", sorted(classified.get(config.excluded_condition, [])))
        print("[INFO] 廣度記錄持股（已排除）：", sorted(record_symbols))
        print("[INFO] 入場條件分類：", {k: len(v) for k, v in classified.items()})

        # B. 市場廣度 / Google Sheet：非必要。失敗不影響 MFE。
        market_result = run_optional_step(
            "取得 TWSE/TPEX 市場廣度快照",
            fetch_market_snapshot,
            config,
            target,
            default=None,
        ) if config.continue_on_market_error else run_required_step("取得 TWSE/TPEX 市場廣度快照", fetch_market_snapshot, config, target)

        if market_result is None:
            errors.append({"step": "market_snapshot", "message": "TWSE/TPEX 市場廣度快照失敗或無資料；已略過 Google Sheet 市場廣度寫入。"})
        else:
            market_snapshot, taiex_pct, otc_pct = market_result
            market_stats = calc_breadth(market_snapshot)
            condition_stats = {}
            for cond in config.entry_conditions:
                symbols = classified.get(cond, [])
                condition_stats[cond] = calc_breadth(market_snapshot[market_snapshot.index.isin(symbols)], taiex_pct, include_vs_market=True)
            condition_stats["全持倉"] = calc_breadth(market_snapshot[market_snapshot.index.isin(record_symbols)], taiex_pct, include_vs_market=True)

            row = build_breadth_row(target, taiex_pct, otc_pct, market_stats, condition_stats, target_buy, target_sell, record_symbols, classified, config)
            if config.continue_on_gsheet_error:
                gs_ok = run_optional_step("寫入 Google Sheet", write_to_gsheet, config, row, date_str, config, default=False)
                if gs_ok is False:
                    errors.append({"step": "google_sheet", "message": "Google Sheet 寫入失敗；已繼續執行 MFE。"})
            else:
                run_required_step("寫入 Google Sheet", write_to_gsheet, config, row, date_str, config)

        # C. MFE：獨立於市場廣度。MFE 與 holding days 都使用 target/as_of_date。
        try:
            reststock = sdk.marketdata.rest_client.stock
            excluded_symbols = set(classified.get(config.excluded_condition, []))
            mfe_inventory = inventory[~inventory["stock_no"].isin(excluded_symbols)].copy() if not inventory.empty else inventory
            holding_lots = run_required_step("FIFO 重建目前庫存批次（已排除）", reconstruct_open_lots_fifo, config, trades, mfe_inventory, config)
            summary = run_required_step("彙總持股（holding days 使用 target_date，已排除）", summarize_open_lots, config, holding_lots, target)
            mfe = run_required_step("計算 MFE / 回檔（價格抓到 target_date）", calculate_mfe_pullback, config, summary, reststock, config, target)
            alert = build_mfe_alert(mfe, config)
        except Exception as e:
            errors.append({"step": "mfe", "message": str(e)})
            if config.continue_on_mfe_error:
                print(f"[WARNING] MFE 失敗，但仍會輸出已完成資料：{e}")
                if config.debug:
                    traceback.print_exc()
            else:
                raise

        # D. 策略帳本：首次匯入舊庫存，之後追蹤新成交與策略歸屬。
        if config.enable_strategy_ledger:
            strategy_ledger = run_optional_step(
                "更新策略帳本 / 庫存核對",
                update_strategy_ledger,
                config,
                inventory,
                trades,
                target,
                config,
                default=None,
            )
            if strategy_ledger is None:
                errors.append({"step": "strategy_ledger", "message": "策略帳本更新失敗；主流程仍繼續。"})
            elif not strategy_ledger.get("reconciliation", pd.DataFrame()).empty:
                bad = strategy_ledger["reconciliation"]
                bad = bad[bad["difference"].ne(0)]
                if not bad.empty:
                    print("[WARNING] 策略帳本與富邦庫存不一致，請查看『策略庫存核對』：")
                    print(bad.to_string(index=False))

        # E. 輸出：盡量輸出所有成功部分。
        paths = run_required_step(
            "輸出 Excel / parquet",
            export_results,
            config,
            target,
            inventory,
            trades,
            target_buy,
            target_sell,
            record_symbols,
            classified_df,
            market_snapshot,
            market_stats,
            condition_stats,
            holding_lots,
            summary,
            mfe,
            alert,
            strategy_ledger,
            errors,
            config,
        )

        print("\n=== 出場觀察 ===")
        print(
            "條件 A：MFE > {}% 且浮盈回吐 > {} 個百分點；條件 B：持有 >= {} 個交易日；條件 C：MFE > 10% 且吐光轉虧；條件 D：收盤虧損 > {}%；任一成立即列入。".format(
                int(config.mfe_threshold * 100),
                int(config.pullback_threshold * 100),
                config.min_holding_trading_days,
                int(config.close_loss_threshold * 100),
            )
        )
        if alert.empty:
            print("目前沒有符合出場觀察條件的股票，或 MFE 未成功產生。")
        else:
            print(alert.to_string(index=False))

        print("\n輸出檔案：")
        for k, p in paths.items():
            print(f"{k}: {p}")

        if errors:
            print("\n流程中有非致命錯誤，詳見 Excel 的『錯誤紀錄』工作表：")
            for err in errors:
                print(f"- {err['step']}: {err['message']}")

        return {
            "target_date": target,
            "inventory": inventory,
            "trades": trades,
            "target_buy": target_buy,
            "target_sell": target_sell,
            # 相容前一版命名
            "today_buy": target_buy,
            "today_sell": target_sell,
            "record_symbols": record_symbols,
            "breadth_holdings": pd.DataFrame({"stock_no": sorted(record_symbols)}),
            "classified": classified,
            "excluded_symbols": set(classified.get(config.excluded_condition, [])),
            "classified_holdings": classified_df,
            "market_snapshot": market_snapshot,
            "taiex_pct": taiex_pct,
            "otc_pct": otc_pct,
            "market_stats": market_stats,
            "condition_stats": condition_stats,
            "holding_lots": holding_lots,
            "summary": summary,
            "mfe": mfe,
            "mfe_alert": alert,
            "alert": alert,
            "strategy_ledger": strategy_ledger,
            "errors": errors,
            "paths": paths,
        }
    finally:
        try:
            sdk.logout()
        except Exception:
            pass


if __name__ == "__main__":
    run_pipeline(PipelineConfig())
