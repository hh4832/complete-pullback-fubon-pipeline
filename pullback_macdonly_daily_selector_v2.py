# -*- coding: utf-8 -*-

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from tqdm.auto import tqdm

from finlab import data
from finlab.markets.tw import TWMarket


# ============================================================
# CONFIG
# ============================================================

CONFIG = {
    # ---------- Data ----------
    "USE_ADJUSTED_PRICE": True,

    "FIELD_VOLUME_CANDIDATES": [
        "price:成交股數",
        "price:成交量",
    ],

    "FIELD_FOREIGN_BUY_CANDIDATES": [
        "institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)",
        "institutional_investors_trading_summary:外資買賣超股數",
        "institutional_investors_trading_summary:外資買賣超股數(不含外資自營商)",
        "institutional_investors_trading_summary:外資買賣超",
    ],

    # ---------- Period ----------
    "DATA_START": "2014-01-01",
    "DATA_END": None,

    "TRAIN_START": "2016-01-01",
    "TRAIN_END": "2020-12-31",

    "VALID_START": "2021-01-01",
    "VALID_END": "2025-12-31",

    "STRESS_YEARS": [2015, 2018, 2022],

    # ---------- Stock regime ----------
    "MA_FAST": 20,
    "MA_MID": 60,
    "MA_SLOW": 120,

    # 個股：不是 MA20 < MA60 < MA120
    "STOCK_REGIME_MODE": "not_bear_stack",

    # ---------- Pullback ----------
    "BIAS_N": 6,
    "BIAS_THRESHOLD": -0.05,

    # ---------- Mean reversion trigger ----------
    # RSV 保留用於計算 K 值；RSV cross 不參與本版入場條件。
    "RSV_N": 9,
    "RSV_CROSS_LEVEL": 20,

    "K_ALPHA": 1 / 3,

    "MTM_N": 5,
    "REQUIRE_MTM_NEGATIVE": False,

    # MACD oscillator = MACD line - signal line
    "MACD_FAST": 12,
    "MACD_SLOW": 26,
    "MACD_SIGNAL": 9,

    # ---------- Foreign confirmation ----------
    "FOREIGN_RATIO_LOOKBACK": 3,

    # ---------- Market filter using 0050 ----------
    "USE_MARKET_FILTER": True,

    # 6/17 實驗核心：
    # 0050 drawdown <= -4%
    # AND (ADX > 25 OR ADX < 25 AND DI+ > DI-)
    "MARKET_REGIME_MODE": "etf_drawdown_adx_or_di_plus",

    "MARKET_PROXY_STOCK_ID": "0050",

    # 0050 距 200 日最高收盤價下跌 X 才允許進場
    "MARKET_HIGH_LOOKBACK": 200,
    "MARKET_DRAWDOWN_THRESHOLD": -0.04,

    # 0050 ADX / DI
    "MARKET_ADX_N": 14,
    "MARKET_ADX_THRESHOLD": 25,

    # ---------- Exit ----------
    "STOP_LOSS": -0.15,
    "MAX_HOLD_DAYS": 35,

    # 本版不使用 MFE 回吐出場，只保留停損與持有天數出場。
    "USE_MFE_FULL_GIVEBACK_EXIT": False,
    "MFE_FULL_GIVEBACK_TRIGGER": 0.10,
    "MFE_FULL_GIVEBACK_EXIT_LEVEL": 0.0,

    # ---------- Cost ----------
    "ROUND_TRIP_COST": 0.006,

    # ---------- Analysis ----------
    "POST_STOP_RETURN_DAYS": 20,

    # ---------- Portfolio simulation ----------
    "RUN_PORTFOLIO_SIM": True,
    "INITIAL_CAPITAL": 1_000_000,
    "MAX_POSITIONS": 20,
    "POSITION_PCT": 0.05,

    # ---------- Output ----------
    "OUTPUT_PREFIX": "pullback_macdonly_day35_exit15_daily_selector",
}


# ============================================================
# Data loading
# ============================================================

def get_adjusted_price_from_twmarket(price_name, adj=True):
    """
    price_name: open / close / high / low
    """

    mkt = TWMarket()

    if hasattr(mkt, "get_trading_price"):
        return mkt.get_trading_price(price_name, adj=adj).sort_index()

    if hasattr(mkt, "get_price"):
        return mkt.get_price(price_name, adj=adj).sort_index()

    raise AttributeError(
        "TWMarket 物件沒有 get_trading_price 或 get_price。"
        "請確認 finlab 版本，建議 pip install finlab==2.0.11 後重啟 kernel。"
    )


def get_first_available_dataset(candidates, required_name):
    last_error = None

    for field in candidates:
        try:
            print(f"Trying data.get('{field}') ...")
            df = data.get(field)

            if df is not None and not df.empty:
                print(f"Loaded {required_name}: {field}, shape={df.shape}")
                return df.sort_index(), field

        except Exception as e:
            last_error = e
            print(f"Failed: {field}")
            print(f"  error: {e}")

    raise RuntimeError(
        f"找不到可用的 {required_name} 欄位。\n"
        f"請到 FinLab database 複製正確 data.get() 名稱後，放進 CONFIG。\n"
        f"最後錯誤：{last_error}"
    )


def get_price_data(cfg):
    print("Loading FinLab price data...")

    if cfg["USE_ADJUSTED_PRICE"]:
        adj_open = get_adjusted_price_from_twmarket("open", adj=True)
        adj_close = get_adjusted_price_from_twmarket("close", adj=True)
    else:
        adj_open = data.get("price:開盤價").sort_index()
        adj_close = data.get("price:收盤價").sort_index()

    if adj_open.empty or adj_close.empty:
        raise RuntimeError("開盤價或收盤價資料為空，請確認 FinLab 登入、版本與資料權限。")

    volume, volume_field = get_first_available_dataset(
        cfg["FIELD_VOLUME_CANDIDATES"],
        required_name="成交量"
    )

    foreign_buy, foreign_field = get_first_available_dataset(
        cfg["FIELD_FOREIGN_BUY_CANDIDATES"],
        required_name="外資買賣超"
    )

    common_index = adj_close.index
    common_columns = adj_close.columns

    adj_open = adj_open.reindex(index=common_index, columns=common_columns)
    volume = volume.reindex(index=common_index, columns=common_columns)
    foreign_buy = foreign_buy.reindex(index=common_index, columns=common_columns)

    print("Data loaded.")
    print(f"adj_open shape:  {adj_open.shape}")
    print(f"adj_close shape: {adj_close.shape}")
    print(f"volume field:    {volume_field}")
    print(f"foreign field:   {foreign_field}")

    return adj_open, adj_close, volume, foreign_buy


# ============================================================
# Indicators
# ============================================================

def rolling_mean(df, n):
    return df.rolling(n, min_periods=n).mean()


def calc_rsv(close, n):
    rolling_low = close.rolling(n, min_periods=n).min()
    rolling_high = close.rolling(n, min_periods=n).max()

    rsv = (close - rolling_low) / (rolling_high - rolling_low) * 100
    rsv = rsv.replace([np.inf, -np.inf], np.nan)

    return rsv


def calc_k_from_rsv(rsv, alpha=1 / 3):
    return rsv.ewm(alpha=alpha, adjust=False).mean()


def calc_macd_osc(close, fast=12, slow=26, signal=9):
    """
    MACD oscillator = EMA(fast) - EMA(slow) - signal line.
    回傳 macd_line, macd_signal, macd_osc。
    """
    ema_fast = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_slow = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_fast - ema_slow
    macd_signal = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    macd_osc = macd_line - macd_signal
    return macd_line, macd_signal, macd_osc


def calc_adx(high, low, close, n=14):
    """
    Wilder ADX.
    輸入為 pd.Series。
    """

    high = pd.Series(high).astype(float)
    low = pd.Series(low).astype(float)
    close = pd.Series(close).astype(float)

    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0),
        index=high.index
    )

    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0),
        index=high.index
    )

    atr = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()

    plus_di = 100 * plus_dm_smooth / atr
    minus_di = 100 * minus_dm_smooth / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx = dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()

    adx = adx.replace([np.inf, -np.inf], np.nan)

    return adx, plus_di, minus_di


def build_stock_regime_filter(adj_close, cfg):
    ma_fast = rolling_mean(adj_close, cfg["MA_FAST"])
    ma_mid = rolling_mean(adj_close, cfg["MA_MID"])
    ma_slow = rolling_mean(adj_close, cfg["MA_SLOW"])

    bear_stack = (ma_fast < ma_mid) & (ma_mid < ma_slow)
    bull_stack = (ma_fast > ma_mid) & (ma_mid > ma_slow)

    mode = cfg["STOCK_REGIME_MODE"]

    if mode == "not_bear_stack":
        regime_filter = ~bear_stack

    elif mode == "close_above_ma120":
        regime_filter = adj_close > ma_slow

    elif mode == "ma60_above_ma120":
        regime_filter = ma_mid > ma_slow

    elif mode == "strong_bull_stack":
        regime_filter = bull_stack

    else:
        raise ValueError(f"Unknown STOCK_REGIME_MODE: {mode}")

    regime_info = {
        "ma_fast": ma_fast,
        "ma_mid": ma_mid,
        "ma_slow": ma_slow,
        "bear_stack": bear_stack,
        "bull_stack": bull_stack,
        "regime_filter": regime_filter,
    }

    return regime_filter, regime_info


# ============================================================
# 0050 market proxy
# ============================================================

def get_market_ohlc_0050_proxy(adj_close, cfg):
    """
    用 0050 OHLC 當大盤 proxy。
    好處：0050 有 high / low / close，可以穩定計算 ADX / DI。
    """

    stock_id = cfg.get("MARKET_PROXY_STOCK_ID", "0050")

    print(f"Loading market proxy OHLC: {stock_id}")

    high_df = get_adjusted_price_from_twmarket("high", adj=True)
    low_df = get_adjusted_price_from_twmarket("low", adj=True)
    close_df = get_adjusted_price_from_twmarket("close", adj=True)

    for name, df in [("high", high_df), ("low", low_df), ("close", close_df)]:
        if stock_id not in df.columns:
            raise RuntimeError(
                f"找不到 {stock_id} 的 {name} 資料。"
                f"目前欄位前 20 個：{list(df.columns[:20])}"
            )

    market_high = high_df[stock_id].reindex(adj_close.index).ffill()
    market_low = low_df[stock_id].reindex(adj_close.index).ffill()
    market_close = close_df[stock_id].reindex(adj_close.index).ffill()

    print(f"Loaded market proxy: {stock_id}")
    print(f"  valid high count:  {market_high.notna().sum()}")
    print(f"  valid low count:   {market_low.notna().sum()}")
    print(f"  valid close count: {market_close.notna().sum()}")

    return market_high, market_low, market_close


def build_market_regime_filter(adj_close, cfg):
    """
    大盤濾網使用 0050 proxy：

    mode = etf_drawdown_adx_or_di_plus

    條件：
    0050 drawdown <= threshold
    AND
    (
        ADX > ADX_THRESHOLD
        OR
        ADX < ADX_THRESHOLD AND DI+ > DI-
    )
    """

    market_high, market_low, market_close = get_market_ohlc_0050_proxy(adj_close, cfg)

    high_lookback = cfg.get("MARKET_HIGH_LOOKBACK", 200)
    drawdown_threshold = cfg.get("MARKET_DRAWDOWN_THRESHOLD", -0.04)

    adx_n = cfg.get("MARKET_ADX_N", 14)
    adx_threshold = cfg.get("MARKET_ADX_THRESHOLD", 25)

    market_rolling_high_close = market_close.rolling(
        high_lookback,
        min_periods=high_lookback
    ).max()

    market_drawdown_from_high = market_close / market_rolling_high_close - 1
    drawdown_hit = market_drawdown_from_high <= drawdown_threshold

    market_adx, market_plus_di, market_minus_di = calc_adx(
        high=market_high,
        low=market_low,
        close=market_close,
        n=adx_n
    )

    # 6/17 實驗：
    # - ADX > 25：視為趨勢強，允許進場
    # - ADX < 25：要求 DI+ > DI-
    adx_strong = market_adx > adx_threshold
    market_plus_di_rising = market_plus_di > market_plus_di.shift(1)
    adx_weak_but_di_plus_rising = (market_adx < adx_threshold) & market_plus_di_rising

    mode = cfg["MARKET_REGIME_MODE"]

    if mode == "etf_drawdown_from_high":
        market_filter = drawdown_hit

    elif mode == "etf_drawdown_from_high_and_adx":
        market_filter = drawdown_hit & adx_strong

    elif mode == "etf_drawdown_adx_or_di_plus":
        # 舊版保留：ADX 強，或 ADX 弱但 DI+ > DI-
        adx_weak_but_di_plus = (market_adx < adx_threshold) & (market_plus_di > market_minus_di)
        market_filter = drawdown_hit & (adx_strong | adx_weak_but_di_plus)

    elif mode == "etf_drawdown_adx_or_di_plus_rising":
        market_filter = drawdown_hit & (adx_strong | adx_weak_but_di_plus_rising)

    else:
        raise ValueError(
            f"Unknown MARKET_REGIME_MODE: {mode}. "
            "This version supports etf_drawdown_from_high, "
            "etf_drawdown_from_high_and_adx, etf_drawdown_adx_or_di_plus, "
            "and etf_drawdown_adx_or_di_plus_rising."
        )

    market_filter = market_filter.reindex(adj_close.index).fillna(False)

    print("Market filter summary:")
    print(f"  proxy: {cfg.get('MARKET_PROXY_STOCK_ID', '0050')}")
    print(f"  mode: {mode}")
    print(f"  high lookback: {high_lookback}")
    print(f"  drawdown threshold: {drawdown_threshold:.2%}")
    print(f"  ADX n: {adx_n}")
    print(f"  ADX threshold: {adx_threshold}")
    print(f"  drawdown hit days: {int(drawdown_hit.sum())}")
    print(f"  ADX strong days: {int(adx_strong.sum())}")
    print(f"  DI+ rising days: {int(market_plus_di_rising.sum())}")
    print(f"  ADX weak but DI+ rising days: {int(adx_weak_but_di_plus_rising.sum())}")
    print(f"  pass days: {int(market_filter.sum())}")
    print(f"  total days: {len(market_filter)}")
    print(f"  pass ratio: {market_filter.mean():.2%}")

    print("  0050 drawdown quantiles:")
    print(market_drawdown_from_high.quantile([0.1, 0.25, 0.5, 0.75, 0.9]))

    print("  0050 ADX quantiles:")
    print(market_adx.quantile([0.1, 0.25, 0.5, 0.75, 0.9]))

    print("  0050 DI+ quantiles:")
    print(market_plus_di.quantile([0.1, 0.25, 0.5, 0.75, 0.9]))

    print("  0050 DI- quantiles:")
    print(market_minus_di.quantile([0.1, 0.25, 0.5, 0.75, 0.9]))

    return market_filter, {
        "market_proxy_high": market_high,
        "market_proxy_low": market_low,
        "market_proxy_close": market_close,
        "market_proxy_rolling_high_close": market_rolling_high_close,
        "market_drawdown_from_high": market_drawdown_from_high,
        "market_adx": market_adx,
        "market_plus_di": market_plus_di,
        "market_minus_di": market_minus_di,
        "market_drawdown_hit": drawdown_hit,
        "market_adx_strong": adx_strong,
        "market_plus_di_rising": market_plus_di_rising,
        "market_adx_weak_but_di_plus_rising": adx_weak_but_di_plus_rising,
        "market_filter_series": market_filter,
    }


# ============================================================
# Entry signal
# ============================================================

def build_entry_signal(adj_close, volume, foreign_buy, cfg):
    print("Building vectorized indicators...")

    # 1. 個股非空頭
    stock_regime_filter, regime_info = build_stock_regime_filter(adj_close, cfg)

    ma_fast = regime_info["ma_fast"]
    ma_mid = regime_info["ma_mid"]
    ma_slow = regime_info["ma_slow"]
    bear_stack = regime_info["bear_stack"]
    bull_stack = regime_info["bull_stack"]

    # 2. BIAS 負乖離
    ma_bias = rolling_mean(adj_close, cfg["BIAS_N"])
    bias = adj_close / ma_bias - 1
    bias_filter = bias < cfg["BIAS_THRESHOLD"]

    # 3. RSV / K
    # RSV cross 保留在 indicators 裡方便觀察，但本版不參與入場條件。
    rsv = calc_rsv(adj_close, cfg["RSV_N"])
    rsv_cross_20 = (
        (rsv > cfg["RSV_CROSS_LEVEL"]) &
        (rsv.shift(1) <= cfg["RSV_CROSS_LEVEL"])
    )

    # 今 K > 昨 K
    k = calc_k_from_rsv(rsv, cfg["K_ALPHA"])
    k_up = k > k.shift(1)

    # 4. MTM 轉強
    mtm = adj_close / adj_close.shift(cfg["MTM_N"]) - 1
    mtm_strengthen = mtm > mtm.shift(1)

    if cfg["REQUIRE_MTM_NEGATIVE"]:
        mtm_strengthen = mtm_strengthen & (mtm < 0)

    # 5. MACD oscillator 柱狀體上升
    macd_line, macd_signal, macd_osc = calc_macd_osc(
        adj_close,
        fast=cfg["MACD_FAST"],
        slow=cfg["MACD_SLOW"],
        signal=cfg["MACD_SIGNAL"],
    )
    macd_osc_rising = macd_osc > macd_osc.shift(1)

    # 6/17 MACD osc 版本：MTM 轉強 / 今K>昨K / MACD-osc 上升，三選一
    mean_reversion_trigger = macd_osc_rising

    # 6. 外資買超佔成交量 3 日新高
    foreign_ratio = foreign_buy / volume
    foreign_ratio = foreign_ratio.replace([np.inf, -np.inf], np.nan)

    foreign_buy_positive = foreign_buy > 0

    foreign_ratio_new_high = (
        foreign_ratio ==
        foreign_ratio.rolling(
            cfg["FOREIGN_RATIO_LOOKBACK"],
            min_periods=cfg["FOREIGN_RATIO_LOOKBACK"]
        ).max()
    )

    foreign_filter = foreign_buy_positive & foreign_ratio_new_high

    signal_before_market = (
        stock_regime_filter &
        bias_filter &
        mean_reversion_trigger &
        foreign_filter
    )

    # 7. 大盤濾網：0050 drawdown + ADX/DI
    if cfg["USE_MARKET_FILTER"]:
        market_filter_series, market_info = build_market_regime_filter(adj_close, cfg)

        market_filter = pd.DataFrame(
            np.repeat(market_filter_series.values.reshape(-1, 1), adj_close.shape[1], axis=1),
            index=adj_close.index,
            columns=adj_close.columns
        )
    else:
        print("Market filter is OFF. All True.")
        market_filter = pd.DataFrame(
            True,
            index=adj_close.index,
            columns=adj_close.columns
        )
        market_info = {}

    signal_close_day = signal_before_market & market_filter

    before_n = int(signal_before_market.shift(1).fillna(False).sum().sum())
    after_n = int(signal_close_day.shift(1).fillna(False).sum().sum())
    filtered_n = int((signal_before_market & ~market_filter).shift(1).fillna(False).sum().sum())

    print("Signal debug:")
    print(f"  signals before market filter: {before_n}")
    print(f"  signals after market filter:  {after_n}")
    print(f"  filtered out signals:         {filtered_n}")
    print(f"  MTM strengthen count:         {int(mtm_strengthen.sum().sum())}")
    print(f"  K up count:                   {int(k_up.sum().sum())}")
    print(f"  MACD osc rising count:        {int(macd_osc_rising.sum().sum())}")

    # 前一天收盤成立，隔天開盤進場
    entry_next_open = signal_close_day.shift(1).fillna(False)

    indicators = {
        "ma_fast": ma_fast,
        "ma_mid": ma_mid,
        "ma_slow": ma_slow,
        "bear_stack": bear_stack,
        "bull_stack": bull_stack,
        "stock_regime_filter": stock_regime_filter,

        "bias": bias,
        "bias_filter": bias_filter,

        "rsv": rsv,
        "rsv_cross_20": rsv_cross_20,

        "k": k,
        "k_up": k_up,

        "mtm": mtm,
        "mtm_strengthen": mtm_strengthen,

        "macd_line": macd_line,
        "macd_signal": macd_signal,
        "macd_osc": macd_osc,
        "macd_osc_rising": macd_osc_rising,

        "mean_reversion_trigger": mean_reversion_trigger,

        "foreign_ratio": foreign_ratio,
        "foreign_buy_positive": foreign_buy_positive,
        "foreign_ratio_new_high": foreign_ratio_new_high,
        "foreign_filter": foreign_filter,

        "market_filter": market_filter,

        "signal_before_market": signal_before_market,
        "signal_close_day": signal_close_day,
        "entry_next_open": entry_next_open,
    }

    indicators.update(market_info)

    print("Entry signal built.")
    print(f"Raw entry signals: {int(entry_next_open.sum().sum())}")

    return entry_next_open, indicators




# ============================================================
# Daily selector
# ============================================================

from pathlib import Path
from datetime import datetime


def _latest_valid_date(df: pd.DataFrame) -> pd.Timestamp:
    """Return the latest date that contains at least one non-null value."""
    valid = df.notna().any(axis=1)
    if not valid.any():
        raise RuntimeError("找不到有效資料日期。")
    return pd.Timestamp(df.index[valid][-1])


def _latest_common_data_date(
    adj_close: pd.DataFrame,
    volume: pd.DataFrame,
    foreign_buy: pd.DataFrame,
) -> pd.Timestamp:
    """Latest date on which every input required by the entry rule is available."""
    usable = (
        adj_close.notna().any(axis=1)
        & volume.notna().any(axis=1)
        & foreign_buy.notna().any(axis=1)
    )
    if not usable.any():
        raise RuntimeError("收盤價、成交量與外資買賣超找不到共同有效日期。")
    return pd.Timestamp(usable.index[usable][-1])


def _print_signal_date_diagnostics(
    d: pd.Timestamp,
    indicators: dict,
    adj_close: pd.DataFrame,
) -> tuple[dict[str, int | bool], dict[str, pd.DataFrame]]:
    """Print and return every cumulative screening layer for the signal date."""
    individual_names = [
        "stock_regime_filter",
        "bias_filter",
        "macd_osc_rising",
        "foreign_buy_positive",
        "foreign_ratio_new_high",
        "foreign_filter",
        "signal_before_market",
        "signal_close_day",
    ]
    stats: dict[str, int | bool] = {}
    rows = {
        name: indicators[name].loc[d].fillna(False).astype(bool)
        for name in individual_names
    }

    print(f"\nSignal-date individual counts ({d.date()}):")
    for name in individual_names:
        count = int(rows[name].sum())
        stats[f"individual_{name}"] = count
        print(f"  {name:26s}: {count}")

    market = indicators.get("market_filter_series")
    market_pass = bool(market.loc[d]) if market is not None and d in market.index else True
    stats["market_filter_pass"] = market_pass
    print(f"  {'market_filter_pass':26s}: {market_pass}")

    # These are the actual AND operations used by signal_before_market.
    funnel_steps = [
        ("stock regime", "stock_regime_filter"),
        ("+ BIAS filter", "bias_filter"),
        ("+ MACD osc rising", "macd_osc_rising"),
        ("+ foreign buy positive", "foreign_buy_positive"),
        ("+ foreign ratio 3D high", "foreign_ratio_new_high"),
    ]
    cumulative = None
    funnel_frames: dict[str, pd.DataFrame] = {}
    macd_change = indicators["macd_osc"] - indicators["macd_osc"].shift(1)

    def make_stage_frame(
        mask: pd.Series,
        stage_order: int,
        stage_key: str,
        stage_label: str,
    ) -> pd.DataFrame:
        selected = mask.index[mask.fillna(False)]
        stock_ids = [
            str(stock_id).zfill(4) if str(stock_id).isdigit() else str(stock_id)
            for stock_id in selected
        ]
        frame = pd.DataFrame({
            "signal_date": d,
            "stage_order": stage_order,
            "stage_key": stage_key,
            "stage_label": stage_label,
            "stage_count": len(selected),
            "stock_id": stock_ids,
            "bias_at_signal": indicators["bias"].loc[d, selected].values,
            "macd_osc_at_signal": indicators["macd_osc"].loc[d, selected].values,
            "macd_osc_change_at_signal": macd_change.loc[d, selected].values,
            "foreign_buy_positive": indicators["foreign_buy_positive"].loc[d, selected].values,
            "foreign_ratio_new_high": indicators["foreign_ratio_new_high"].loc[d, selected].values,
            "foreign_ratio_at_signal": indicators["foreign_ratio"].loc[d, selected].values,
            "close_at_signal": adj_close.loc[d, selected].values,
        })
        return frame.sort_values("stock_id").reset_index(drop=True)

    print(f"\nSignal-date cumulative funnel ({d.date()}):")
    for stage_order, (label, name) in enumerate(funnel_steps, start=1):
        cumulative = rows[name].copy() if cumulative is None else cumulative & rows[name]
        count = int(cumulative.sum())
        stats[f"funnel_{name}"] = count
        funnel_frames[f"{stage_order}_{name}"] = make_stage_frame(
            cumulative,
            stage_order,
            name,
            label,
        )
        print(f"  {label:28s}: {count}")
        if 0 < count <= 20:
            passing = funnel_frames[f"{stage_order}_{name}"]["stock_id"].tolist()
            print(f"    passing stocks: {', '.join(passing)}")

    before_market_count = int(rows["signal_before_market"].sum())
    stats["signal_before_market"] = before_market_count
    if int(cumulative.sum()) != before_market_count:
        print("  [WARNING] 診斷漏斗與 signal_before_market 不一致，請檢查入場公式。")

    final_count = int(rows["signal_close_day"].sum())
    stats["signal_close_day"] = final_count
    funnel_frames["6_market_filter"] = make_stage_frame(
        rows["signal_close_day"],
        6,
        "market_filter",
        "+ market filter",
    )
    print(f"  {'+ market filter':28s}: {final_count}")
    if 0 < final_count <= 20:
        passing = funnel_frames["6_market_filter"]["stock_id"].tolist()
        print(f"    passing stocks: {', '.join(passing)}")
    return stats, funnel_frames


def load_current_holdings(path: str | None) -> pd.DataFrame:
    """
    Optional holdings file. Supported formats: CSV / XLSX / Parquet.
    Required stock-id column can be one of: stock_id, stock_no, symbol.
    """
    if not path:
        return pd.DataFrame(columns=["stock_id"])

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"持股檔不存在：{p}")

    suffix = p.suffix.lower()
    if suffix == ".csv":
        df = pd.read_csv(p)
    elif suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(p)
    elif suffix == ".parquet":
        df = pd.read_parquet(p)
    else:
        raise ValueError("持股檔只支援 CSV / XLSX / Parquet。")

    stock_col = next((c for c in ["stock_id", "stock_no", "symbol"] if c in df.columns), None)
    if stock_col is None:
        raise ValueError("持股檔需包含 stock_id、stock_no 或 symbol 欄位。")

    out = pd.DataFrame({"stock_id": df[stock_col].astype(str).str.zfill(4)})
    return out.drop_duplicates().reset_index(drop=True)


def build_daily_candidates(
    cfg: dict,
    holdings_path: str | None = None,
    total_equity: float | None = None,
    signal_date: str | None = None,
):
    """
    Daily selection logic:
    1. Use the close-day signal; intended entry is next trading day's open.
    2. MACD oscillator rising only.
    3. BIAS < threshold.
    4. Foreign-buy ratio is a 3-day high and foreign buying is positive.
    5. 0050 market filter passes.
    6. Rank by BIAS ascending (deeper pullback first).
    7. Exclude currently held names.
    8. Keep only remaining slots under MAX_POSITIONS.
    """
    adj_open, adj_close, volume, foreign_buy = get_price_data(cfg)

    start = cfg.get("DATA_START")
    end = cfg.get("DATA_END")
    if start:
        adj_open = adj_open.loc[start:]
        adj_close = adj_close.loc[start:]
        volume = volume.loc[start:]
        foreign_buy = foreign_buy.loc[start:]
    if end:
        adj_open = adj_open.loc[:end]
        adj_close = adj_close.loc[:end]
        volume = volume.loc[:end]
        foreign_buy = foreign_buy.loc[:end]

    _, indicators = build_entry_signal(
        adj_close=adj_close,
        volume=volume,
        foreign_buy=foreign_buy,
        cfg=cfg,
    )

    latest_price_date = _latest_valid_date(adj_close)
    latest_common_date = _latest_common_data_date(adj_close, volume, foreign_buy)
    print(f"Latest price date:       {latest_price_date.date()}")
    print(f"Latest common data date: {latest_common_date.date()}")
    if latest_common_date < latest_price_date:
        print("[INFO] 最新價格日的成交量或外資資料尚未齊全，改用共同有效日期，避免假性零訊號。")

    if signal_date is None:
        d = latest_common_date
    else:
        d = pd.Timestamp(signal_date)
        if d not in adj_close.index:
            prior = adj_close.index[adj_close.index <= d]
            if len(prior) == 0:
                raise ValueError(f"指定日期 {d.date()} 之前沒有交易資料。")
            d = pd.Timestamp(prior[-1])

    if not volume.loc[d].notna().any() or not foreign_buy.loc[d].notna().any():
        raise ValueError(f"指定訊號日 {d.date()} 的成交量或外資資料尚未齊全。")

    condition_counts, funnel_candidates = _print_signal_date_diagnostics(
        d,
        indicators,
        adj_close,
    )
    signal_row = indicators["signal_close_day"].loc[d].fillna(False)
    stock_ids = signal_row[signal_row].index.astype(str)

    if len(stock_ids) == 0:
        candidates = pd.DataFrame(columns=[
            "rank", "signal_date", "stock_id", "bias_at_signal",
            "macd_osc_at_signal", "macd_osc_change_at_signal",
            "foreign_ratio_at_signal", "close_at_signal",
            "market_drawdown", "market_adx", "market_plus_di", "market_minus_di",
            "already_held", "suggested_position_value",
        ])
    else:
        macd_change = indicators["macd_osc"] - indicators["macd_osc"].shift(1)
        candidates = pd.DataFrame({
            "signal_date": d,
            "stock_id": stock_ids,
            "bias_at_signal": indicators["bias"].loc[d, stock_ids].values,
            "macd_osc_at_signal": indicators["macd_osc"].loc[d, stock_ids].values,
            "macd_osc_change_at_signal": macd_change.loc[d, stock_ids].values,
            "foreign_ratio_at_signal": indicators["foreign_ratio"].loc[d, stock_ids].values,
            "close_at_signal": adj_close.loc[d, stock_ids].values,
        })

        market_drawdown = indicators.get("market_drawdown_from_high", pd.Series(dtype=float)).get(d, np.nan)
        market_adx = indicators.get("market_adx", pd.Series(dtype=float)).get(d, np.nan)
        market_plus_di = indicators.get("market_plus_di", pd.Series(dtype=float)).get(d, np.nan)
        market_minus_di = indicators.get("market_minus_di", pd.Series(dtype=float)).get(d, np.nan)

        candidates["market_drawdown"] = market_drawdown
        candidates["market_adx"] = market_adx
        candidates["market_plus_di"] = market_plus_di
        candidates["market_minus_di"] = market_minus_di

        holdings = load_current_holdings(holdings_path)
        held_set = set(holdings["stock_id"])
        candidates["stock_id"] = candidates["stock_id"].astype(str).str.zfill(4)
        candidates["already_held"] = candidates["stock_id"].isin(held_set)

        candidates = candidates.sort_values(
            ["bias_at_signal", "stock_id"],
            ascending=[True, True],
            na_position="last",
        ).reset_index(drop=True)
        candidates.insert(0, "rank", np.arange(1, len(candidates) + 1))

        if total_equity is not None:
            candidates["suggested_position_value"] = float(total_equity) * cfg["POSITION_PCT"]
        else:
            candidates["suggested_position_value"] = np.nan

    current_positions = len(load_current_holdings(holdings_path))
    available_slots = max(0, int(cfg["MAX_POSITIONS"]) - current_positions)

    buy_list = candidates.loc[~candidates.get("already_held", False)].head(available_slots).copy()
    buy_list["selected_for_next_open"] = True

    return {
        "signal_date": d,
        "current_positions": current_positions,
        "available_slots": available_slots,
        "all_candidates": candidates,
        "buy_list": buy_list,
        "condition_counts": condition_counts,
        "funnel_candidates": funnel_candidates,
        "config": cfg.copy(),
    }


def save_daily_selection(result: dict, output_dir: str = "."):
    outdir = Path(output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    d = pd.Timestamp(result["signal_date"]).strftime("%Y%m%d")
    run_cfg = result.get("config", CONFIG)
    prefix = run_cfg["OUTPUT_PREFIX"]

    csv_all = outdir / f"{prefix}_{d}_all_candidates.csv"
    csv_buy = outdir / f"{prefix}_{d}_buy_list.csv"
    csv_funnel = outdir / f"{prefix}_{d}_funnel_candidates.csv"
    xlsx = outdir / f"{prefix}_{d}.xlsx"

    result["all_candidates"].to_csv(csv_all, index=False, encoding="utf-8-sig")
    result["buy_list"].to_csv(csv_buy, index=False, encoding="utf-8-sig")
    funnel_frames = result.get("funnel_candidates", {})
    funnel_long = (
        pd.concat(funnel_frames.values(), ignore_index=True)
        if funnel_frames else pd.DataFrame()
    )
    funnel_long.to_csv(csv_funnel, index=False, encoding="utf-8-sig")

    with pd.ExcelWriter(xlsx) as writer:
        result["buy_list"].to_excel(writer, sheet_name="buy_list", index=False)
        result["all_candidates"].to_excel(writer, sheet_name="all_candidates", index=False)
        funnel_long.to_excel(writer, sheet_name="funnel_all", index=False)
        funnel_sheet_names = {
            "1_stock_regime_filter": "F1_stock_regime",
            "2_bias_filter": "F2_plus_BIAS",
            "3_macd_osc_rising": "F3_plus_MACD",
            "4_foreign_buy_positive": "F4_plus_foreign_buy",
            "5_foreign_ratio_new_high": "F5_plus_foreign_3Dhigh",
            "6_market_filter": "F6_plus_market",
        }
        for stage_key, frame in funnel_frames.items():
            frame.to_excel(
                writer,
                sheet_name=funnel_sheet_names.get(stage_key, stage_key[:31]),
                index=False,
            )
        pd.DataFrame([{
            "signal_date": result["signal_date"],
            "current_positions": result["current_positions"],
            "available_slots": result["available_slots"],
            "position_pct": run_cfg["POSITION_PCT"],
            "max_positions": run_cfg["MAX_POSITIONS"],
            "stop_loss": run_cfg["STOP_LOSS"],
            "max_hold_days": run_cfg["MAX_HOLD_DAYS"],
        }]).to_excel(writer, sheet_name="run_info", index=False)

    print("\nDaily selector result")
    print(f"  signal date:       {result['signal_date'].date()}")
    print(f"  candidates:        {len(result['all_candidates'])}")
    print(f"  current positions: {result['current_positions']}")
    print(f"  available slots:   {result['available_slots']}")
    print(f"  selected buys:     {len(result['buy_list'])}")
    print(f"  Excel:             {xlsx}")
    print(f"  Buy list CSV:      {csv_buy}")
    print(f"  Funnel CSV:        {csv_funnel}")

    if not result["buy_list"].empty:
        display_cols = [
            "rank", "stock_id", "bias_at_signal", "macd_osc_change_at_signal",
            "foreign_ratio_at_signal", "close_at_signal", "suggested_position_value",
        ]
        print("\nSuggested buy list (next open):")
        print(result["buy_list"][display_cols].to_string(index=False))
    else:
        print("\nNo buy candidates for next open.")

    return xlsx, csv_buy, csv_all, csv_funnel


if __name__ == "__main__":
    # Optional: set these values before running.
    HOLDINGS_FILE = None       # e.g. r"D:\\HAW\\current_holdings.csv"
    TOTAL_EQUITY = 1_000_000   # used only to calculate suggested 5% position value
    OUTPUT_DIR = "."
    SIGNAL_DATE = None         # None = latest available trading date

    result = build_daily_candidates(
        cfg=CONFIG,
        holdings_path=HOLDINGS_FILE,
        total_equity=TOTAL_EQUITY,
        signal_date=SIGNAL_DATE,
    )
    save_daily_selection(result, output_dir=OUTPUT_DIR)
