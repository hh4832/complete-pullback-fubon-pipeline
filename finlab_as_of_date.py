# -*- coding: utf-8 -*-
"""Resolve one reproducible FinLab as-of date for the production pipeline.

Rules
-----
1. requested_date is None: use the latest date on which every required FinLab
   production input is available and has normal row coverage.
2. requested_date is explicit: require that exact date to be complete; never
   silently fall back to a prior trading day.
3. A date is not considered complete merely because one value has appeared.
   Broad stock datasets must have at least 90% of the recent normal non-null
   row coverage, which protects the pipeline from FinLab partial updates.

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
COVERAGE_LOOKBACK = 5
MIN_COVERAGE_RATIO = 0.90
MIN_ABSOLUTE_NON_NULL = 100


@dataclass(frozen=True)
class AsOfDateResolution:
    requested_date: Optional[pd.Timestamp]
    effective_date: pd.Timestamp
    latest_complete_date: pd.Timestamp
    foreign_dataset: str
    latest_by_source: dict[str, str]
    coverage_by_source: dict[str, dict[str, float | int | str]]

    @property
    def effective_date_str(self) -> str:
        return self.effective_date.strftime("%Y-%m-%d")

    @property
    def latest_complete_date_str(self) -> str:
        return self.latest_complete_date.strftime("%Y-%m-%d")


def _normalize_date(value: str | pd.Timestamp) -> pd.Timestamp:
    try:
        return pd.Timestamp(value).normalize()
    except Exception as exc:
        raise ValueError(f"無法解析 as_of_date: {value!r}") from exc


def _coverage_complete_rows(
    df: pd.DataFrame,
    *,
    lookback: int = COVERAGE_LOOKBACK,
    min_ratio: float = MIN_COVERAGE_RATIO,
    min_absolute: int = MIN_ABSOLUTE_NON_NULL,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return completeness mask, row counts, and historical coverage baseline.

    The baseline for date d is the median non-null count of the previous
    ``lookback`` rows, so a partially populated current row cannot lower its own
    threshold. This is a data-readiness gate only; it does not alter strategy
    data or fill missing values.
    """
    if df is None or df.empty:
        raise RuntimeError("FinLab dataset 為空。")

    counts = df.notna().sum(axis=1).astype(float)
    baseline = counts.shift(1).rolling(lookback, min_periods=min(3, lookback)).median()
    threshold = (baseline * min_ratio).clip(lower=float(min_absolute))

    complete = counts.ge(threshold) & baseline.notna()
    # Preserve historical usability near the very beginning of a dataset while
    # keeping the production/latest-date check conservative.
    early = baseline.isna() & counts.ge(float(min_absolute))
    complete = complete | early
    return complete, counts, baseline


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

    close_ok, close_counts, close_baseline = _coverage_complete_rows(close)
    volume_ok, volume_counts, volume_baseline = _coverage_complete_rows(volume)
    foreign_ok, foreign_counts, foreign_baseline = _coverage_complete_rows(foreign)
    index_ok = index_close.notna().all(axis=1)

    common_index = close.index.intersection(volume.index)
    common_index = common_index.intersection(foreign.index)
    common_index = common_index.intersection(index_close.index)
    if len(common_index) == 0:
        raise RuntimeError("必要 FinLab datasets 沒有共同日期。")

    complete = (
        close_ok.reindex(common_index, fill_value=False)
        & volume_ok.reindex(common_index, fill_value=False)
        & foreign_ok.reindex(common_index, fill_value=False)
        & index_ok.reindex(common_index, fill_value=False)
    )
    complete_dates = pd.DatetimeIndex(common_index[complete.to_numpy()])
    if len(complete_dates) == 0:
        raise RuntimeError("必要 FinLab datasets 找不到共同完整交易日。")

    latest_complete = pd.Timestamp(complete_dates[-1]).normalize()
    requested = _normalize_date(requested_date) if requested_date is not None else None
    if requested is None:
        effective = latest_complete
    else:
        if requested not in complete_dates:
            raise ValueError(
                f"指定 as_of_date {requested.date()} 不是 FinLab 完整共同交易日；"
                f"目前最新完整共同交易日為 {latest_complete.date()}。不自動回退日期。"
            )
        effective = requested

    def latest_true(mask: pd.Series) -> str:
        mask = mask.fillna(False)
        if not mask.any():
            return ""
        return str(pd.Timestamp(mask.index[mask][-1]).date())

    latest_by_source = {
        "price:收盤價": latest_true(close_ok),
        "price:成交股數": latest_true(volume_ok),
        foreign_field: latest_true(foreign_ok),
        INDEX_DATASET: latest_true(index_ok),
    }

    def coverage_snapshot(
        name: str,
        counts: pd.Series,
        baseline: pd.Series,
    ) -> dict[str, float | int | str]:
        d = effective
        count = float(counts.get(d, 0.0))
        base = float(baseline.get(d, float("nan")))
        ratio = count / base if pd.notna(base) and base > 0 else float("nan")
        return {
            "date": str(d.date()),
            "non_null_count": int(count),
            "recent_baseline": round(base, 2) if pd.notna(base) else "nan",
            "coverage_ratio": round(ratio, 4) if pd.notna(ratio) else "nan",
            "minimum_ratio": MIN_COVERAGE_RATIO,
        }

    coverage_by_source = {
        "price:收盤價": coverage_snapshot("price:收盤價", close_counts, close_baseline),
        "price:成交股數": coverage_snapshot("price:成交股數", volume_counts, volume_baseline),
        foreign_field: coverage_snapshot(foreign_field, foreign_counts, foreign_baseline),
        INDEX_DATASET: {
            "date": str(effective.date()),
            "non_null_count": int(index_close.loc[effective].notna().sum()),
            "recent_baseline": len(INDEX_SYMBOLS),
            "coverage_ratio": 1.0,
            "minimum_ratio": 1.0,
        },
    }

    return AsOfDateResolution(
        requested_date=requested,
        effective_date=effective,
        latest_complete_date=latest_complete,
        foreign_dataset=foreign_field,
        latest_by_source=latest_by_source,
        coverage_by_source=coverage_by_source,
    )
