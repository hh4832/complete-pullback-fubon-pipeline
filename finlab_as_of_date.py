# -*- coding: utf-8 -*-
"""Resolve one reproducible FinLab as-of date for the production pipeline.

Rules
-----
1. requested_date is None: use the latest date on which every required FinLab
   production input is available.
2. requested_date is explicit: require that exact date to be complete; never
   silently fall back to a prior trading day.

The resolver intentionally runs before Fubon reconciliation / Google Sheet
writes so a partial or unavailable FinLab date fails early.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
from finlab import data


FOREIGN_BUY_CANDIDATES = [
    "institutional_investors_trading_summary:外陸資買賣超股數(不含外資自營商)",
    "institutional_investors_trading_summary:外資買賣超股數",
    "institutional_investors_trading_summary:外資買賣超股數(不含外資自營商)",
    "institutional_investors_trading_summary:外資買賣超",
]
INDEX_DATASET = "market_transaction_info:收盤指數"
INDEX_SYMBOLS = ("TAIEX", "OTC")


@dataclass(frozen=True)
class AsOfDateResolution:
    requested_date: Optional[pd.Timestamp]
    effective_date: pd.Timestamp
    foreign_dataset: str
    latest_by_source: dict[str, str]

    @property
    def effective_date_str(self) -> str:
        return self.effective_date.strftime("%Y-%m-%d")


def _normalize_date(value: str | pd.Timestamp) -> pd.Timestamp:
    try:
        return pd.Timestamp(value).normalize()
    except Exception as exc:
        raise ValueError(f"無法解析 as_of_date: {value!r}") from exc


def _valid_rows(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        raise RuntimeError("FinLab dataset 為空。")
    return df.notna().any(axis=1)


def _load_foreign_buy() -> tuple[pd.DataFrame, str]:
    errors: list[str] = []
    for field in FOREIGN_BUY_CANDIDATES:
        try:
            df = data.get(field)
            if df is not None and not df.empty:
                return df.sort_index(), field
        except Exception as exc:
            errors.append(f"{field}: {exc}")
    raise RuntimeError(
        "找不到可用的外資買賣超 FinLab dataset。"
        + ("\n" + "\n".join(errors) if errors else "")
    )


def resolve_finlab_as_of_date(
    requested_date: str | pd.Timestamp | None = None,
) -> AsOfDateResolution:
    """Resolve the exact production data date shared by required FinLab inputs."""
    close = data.get("price:收盤價").sort_index()
    volume = data.get("price:成交股數").sort_index()
    foreign, foreign_field = _load_foreign_buy()
    index_close = data.get(INDEX_DATASET).sort_index()

    missing_index = set(INDEX_SYMBOLS) - set(index_close.columns)
    if missing_index:
        raise RuntimeError(f"{INDEX_DATASET} 缺少欄位: {sorted(missing_index)}")
    index_close = index_close.loc[:, list(INDEX_SYMBOLS)]

    common_index = close.index.intersection(volume.index)
    common_index = common_index.intersection(foreign.index)
    common_index = common_index.intersection(index_close.index)
    if len(common_index) == 0:
        raise RuntimeError("必要 FinLab datasets 沒有共同日期。")

    complete = (
        _valid_rows(close.reindex(common_index))
        & _valid_rows(volume.reindex(common_index))
        & _valid_rows(foreign.reindex(common_index))
        & index_close.reindex(common_index).notna().all(axis=1)
    )
    complete_dates = pd.DatetimeIndex(common_index[complete.to_numpy()])
    if len(complete_dates) == 0:
        raise RuntimeError("必要 FinLab datasets 找不到共同完整交易日。")

    requested = _normalize_date(requested_date) if requested_date is not None else None
    if requested is None:
        effective = pd.Timestamp(complete_dates[-1]).normalize()
    else:
        if requested not in complete_dates:
            latest = pd.Timestamp(complete_dates[-1]).date()
            raise ValueError(
                f"指定 as_of_date {requested.date()} 不是 FinLab 完整共同交易日；"
                f"目前最新完整共同交易日為 {latest}。不自動回退日期。"
            )
        effective = requested

    def latest_valid(df: pd.DataFrame, require_all: bool = False) -> str:
        valid = df.notna().all(axis=1) if require_all else df.notna().any(axis=1)
        if not valid.any():
            return ""
        return str(pd.Timestamp(df.index[valid][-1]).date())

    latest_by_source = {
        "price:收盤價": latest_valid(close),
        "price:成交股數": latest_valid(volume),
        foreign_field: latest_valid(foreign),
        INDEX_DATASET: latest_valid(index_close, require_all=True),
    }

    return AsOfDateResolution(
        requested_date=requested,
        effective_date=effective,
        foreign_dataset=foreign_field,
        latest_by_source=latest_by_source,
    )
