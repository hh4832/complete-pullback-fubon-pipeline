# -*- coding: utf-8 -*-
"""FinLab-only Taiwan market breadth utilities.

Universe definition:
- Listed / OTC securities from FinLab security_categories (market sii / otc)
- Ordinary-share proxy: 4-digit numeric symbol, excluding depositary receipts
- Traded on target date: close is present and volume > 0

Return basis:
- Normal day: previous valid raw close for that security
- Corporate-action day: overwrite with FinLab official event reference prices
  for dividends/ex-rights, capital reduction, and par value changes.

Market index basis:
- market_transaction_info:收盤指數
- TAIEX and OTC use previous valid index close to calculate daily percent change.

This module is intentionally independent from the production pipeline so it can
be validated before replacing the legacy TWSE/TPEX API implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd
from finlab import data


EVENT_REFERENCE_DATASETS = [
    "dividend_tse:除權息參考價",
    "dividend_otc:除權息參考價",
    "capital_reduction_tse:恢復買賣參考價",
    "capital_reduction_otc:減資恢復買賣開始日參考價格",
    "par_value_change_tse:恢復買賣參考價",
    "par_value_change_otc:恢復買賣開始日參考價",
]

INDEX_DATASET = "market_transaction_info:收盤指數"
INDEX_SYMBOLS = ("TAIEX", "OTC")


@dataclass
class BreadthData:
    target_date: pd.Timestamp
    stock_df: pd.DataFrame
    reference_price: pd.Series
    security_categories: pd.DataFrame
    data_sources: dict[str, Any]


def _normalize_date(value: str | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def load_security_categories() -> pd.DataFrame:
    cats = data.get("security_categories").copy()
    required = {"symbol", "category", "market", "stock_id"}
    missing = required - set(cats.columns)
    if missing:
        raise RuntimeError(f"security_categories 缺少欄位: {sorted(missing)}")
    for c in ["symbol", "category", "market", "stock_id"]:
        cats[c] = cats[c].astype(str).str.strip()
    return cats


def ordinary_stock_symbols(cats: pd.DataFrame) -> pd.Index:
    """Return a reproducible ordinary-share universe proxy.

    Four-digit numeric codes cover domestic/KY ordinary shares but also include
    some DRs such as 9105, so depositary receipts are explicitly excluded using
    security_categories. Letter-suffixed preferred shares and other structured
    products are excluded by the four-digit rule.
    """
    market_mask = cats["market"].isin(["sii", "otc"])
    four_digit_mask = cats["stock_id"].str.fullmatch(r"\d{4}", na=False)
    depositary_receipt_mask = cats["category"].str.contains("存託憑證", na=False)
    selected = cats.loc[market_mask & four_digit_mask & ~depositary_receipt_mask, "stock_id"]
    return pd.Index(selected.drop_duplicates().sort_values())


def _overlay_event_reference_prices(
    reference: pd.DataFrame,
    datasets: Iterable[str] = EVENT_REFERENCE_DATASETS,
) -> tuple[pd.DataFrame, dict[str, str]]:
    loaded: dict[str, str] = {}
    out = reference.copy()
    for dataset in datasets:
        try:
            event_ref = data.get(dataset)
            event_ref = event_ref.reindex(index=out.index, columns=out.columns)
            out = out.where(event_ref.isna(), event_ref)
            loaded[dataset] = str(event_ref.index.max()) if len(event_ref.index) else ""
        except Exception as exc:
            raise RuntimeError(f"必要公司行動參考價資料載入失敗: {dataset}: {exc}") from exc
    return out, loaded


def build_reference_price_matrix(close: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str]]:
    # Reference price is security-specific. If a stock did not trade on the
    # immediately preceding market day, use its last valid raw close.
    # Corporate-action reference prices still override this baseline.
    reference = close.ffill().shift(1)
    return _overlay_event_reference_prices(reference)


def load_finlab_market_breadth_data(target_date: str | pd.Timestamp) -> BreadthData:
    d = _normalize_date(target_date)

    close = data.get("price:收盤價").sort_index()
    volume = data.get("price:成交股數").sort_index()
    cats = load_security_categories()
    ordinary = ordinary_stock_symbols(cats)

    close = close.reindex(columns=close.columns.intersection(ordinary))
    volume = volume.reindex(index=close.index, columns=close.columns)

    if d not in close.index:
        raise ValueError(f"FinLab price:收盤價 沒有指定交易日 {d.date()}")

    traded = close.loc[d].notna() & volume.loc[d].fillna(0).gt(0)
    symbols = close.columns[traded]

    reference_matrix, event_sources = build_reference_price_matrix(close)
    reference = reference_matrix.loc[d, symbols]
    today_close = close.loc[d, symbols]
    today_volume = volume.loc[d, symbols]

    frame = pd.DataFrame({
        "收盤價": today_close,
        "成交股數": today_volume,
        "參考價": reference,
    }).dropna(subset=["收盤價", "參考價"])

    frame["漲跌幅"] = (frame["收盤價"] / frame["參考價"] - 1) * 100

    sources = {
        "price:收盤價_latest": str(close.index.max()),
        "price:成交股數_latest": str(volume.index.max()),
        "security_categories_rows": int(len(cats)),
        "ordinary_universe_count": int(len(ordinary)),
        "traded_ordinary_count": int(len(frame)),
        "event_reference_datasets": event_sources,
    }

    return BreadthData(
        target_date=d,
        stock_df=frame,
        reference_price=reference,
        security_categories=cats,
        data_sources=sources,
    )


def load_market_index_returns(target_date: str | pd.Timestamp) -> dict[str, float]:
    """Return daily TAIEX / OTC percent changes from FinLab only."""
    d = _normalize_date(target_date)
    index_close = data.get(INDEX_DATASET).sort_index()

    missing_cols = set(INDEX_SYMBOLS) - set(index_close.columns)
    if missing_cols:
        raise RuntimeError(f"{INDEX_DATASET} 缺少指數欄位: {sorted(missing_cols)}")
    if d not in index_close.index:
        raise ValueError(f"{INDEX_DATASET} 沒有指定交易日 {d.date()}")

    selected = index_close.loc[:, list(INDEX_SYMBOLS)].apply(pd.to_numeric, errors="coerce")
    previous_valid = selected.ffill().shift(1)

    out: dict[str, float] = {}
    for symbol in INDEX_SYMBOLS:
        current = selected.loc[d, symbol]
        previous = previous_valid.loc[d, symbol]
        if pd.isna(current) or pd.isna(previous) or float(previous) == 0:
            raise RuntimeError(
                f"無法計算 {symbol} {d.date()} 日報酬：current={current}, previous={previous}"
            )
        out[symbol] = (float(current) / float(previous) - 1.0) * 100.0
    return out


def get_tick_size_series(reference: pd.Series) -> pd.Series:
    """Match the existing production tick-size rule exactly."""
    return pd.cut(
        reference,
        bins=[0, 10, 50, 100, 500, 1000, float("inf")],
        labels=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0],
    ).astype(float)


def add_limit_prices_legacy_compatible(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate Taiwan +/-10% limit prices using the production rule."""
    out = df.copy()
    reference = out["參考價"]
    tick = get_tick_size_series(reference)
    out["漲停價"] = (np.floor(reference * 1.1 / tick) * tick).round(2)
    out["跌停價"] = (np.ceil(reference * 0.9 / tick) * tick).round(2)
    return out


def calc_breadth(stock_df: pd.DataFrame) -> dict[str, int]:
    if stock_df is None or stock_df.empty:
        return {
            "漲停": 0, "大漲": 0, "小漲": 0, "平盤": 0,
            "小跌": 0, "大跌": 0, "跌停": 0,
            "總上漲": 0, "總下跌": 0, "總家數": 0,
        }

    base = add_limit_prices_legacy_compatible(stock_df)
    nonflat = base[base["漲跌幅"].ne(0)].copy()
    chg = nonflat["漲跌幅"]
    close = nonflat["收盤價"]
    is_limit_up = close.ge(nonflat["漲停價"])
    is_limit_down = close.le(nonflat["跌停價"])

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
    # Preserve current Google Sheet semantics: total movers excludes flat names.
    stats["總家數"] = stats["總上漲"] + stats["總下跌"]
    return stats


def fetch_finlab_market_snapshot(
    target_date: str | pd.Timestamp,
) -> tuple[pd.DataFrame, float, float]:
    """Production-compatible FinLab-only market snapshot.

    Returns (stock_df, taiex_pct, otc_pct) using the same tuple contract as the
    legacy TWSE/TPEX fetch_market_snapshot function.
    """
    result = load_finlab_market_breadth_data(target_date)
    stock_df = add_limit_prices_legacy_compatible(result.stock_df).rename(
        columns={"參考價": "前收"}
    )
    stock_df = stock_df[["漲跌幅", "收盤價", "漲停價", "跌停價"]]

    index_returns = load_market_index_returns(target_date)
    return stock_df, index_returns["TAIEX"], index_returns["OTC"]
