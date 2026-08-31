# -*- coding: utf-8 -*-
"""Validate FinLab-only market breadth before production replacement."""
from __future__ import annotations

import argparse
import pandas as pd
from finlab import data

import integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2 as legacy
from finlab_market_breadth import (
    EVENT_REFERENCE_DATASETS,
    add_limit_prices_legacy_compatible,
    calc_breadth,
    load_finlab_market_breadth_data,
    ordinary_stock_symbols,
)


def classify_rows(df: pd.DataFrame, *, new_schema: bool) -> pd.Series:
    """Assign each ticker to the Google-Sheet breadth bucket."""
    base = add_limit_prices_legacy_compatible(df) if new_schema else df.copy()
    chg = pd.to_numeric(base["漲跌幅"], errors="coerce")
    close = pd.to_numeric(base["收盤價"], errors="coerce")
    limit_up = pd.to_numeric(base["漲停價"], errors="coerce")
    limit_down = pd.to_numeric(base["跌停價"], errors="coerce")

    labels = pd.Series("", index=base.index, dtype="object")
    is_up = close.ge(limit_up - 1e-12)
    is_down = close.le(limit_down + 1e-12)

    labels.loc[chg.eq(0)] = "平盤"
    labels.loc[(chg > 0) & (chg < 3) & ~is_up] = "小漲"
    labels.loc[(chg >= 3) & ~is_up] = "大漲"
    labels.loc[is_up & chg.gt(0)] = "漲停"
    labels.loc[(chg < 0) & (chg > -3) & ~is_down] = "小跌"
    labels.loc[(chg <= -3) & ~is_down] = "大跌"
    labels.loc[is_down & chg.lt(0)] = "跌停"
    return labels


def _event_reference_values(d: pd.Timestamp, symbols: list[str]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {s: {} for s in symbols}
    for ds in EVENT_REFERENCE_DATASETS:
        try:
            x = data.get(ds)
        except Exception:
            continue
        if d not in x.index:
            continue
        for s in symbols:
            if s in x.columns:
                v = x.loc[d, s]
                if pd.notna(v):
                    out[s][ds] = v
    return out


def build_universe_diagnostic(result, old_df: pd.DataFrame) -> pd.DataFrame:
    d = result.target_date
    old_symbols = set(old_df.index.astype(str))
    new_symbols = set(result.stock_df.index.astype(str))
    symbols = sorted(old_symbols ^ new_symbols)

    raw_close = data.get("price:收盤價").sort_index()
    raw_volume = data.get("price:成交股數").sort_index()
    prev_close_matrix = raw_close.shift(1)
    cats = result.security_categories.copy()
    ordinary = set(ordinary_stock_symbols(cats).astype(str))
    event_refs = _event_reference_values(d, symbols)

    try:
        legacy_twse_company = legacy.fetch_common_stock_set(2)
    except Exception:
        legacy_twse_company = set()
    try:
        legacy_tpex_company = legacy.fetch_common_stock_set(4)
    except Exception:
        legacy_tpex_company = set()
    legacy_company_set = legacy_twse_company | legacy_tpex_company

    cat_lookup = cats.drop_duplicates(subset=["stock_id"], keep="last").set_index("stock_id")

    rows = []
    for s in symbols:
        cat = cat_lookup.loc[s] if s in cat_lookup.index else None
        close = raw_close.loc[d, s] if d in raw_close.index and s in raw_close.columns else pd.NA
        volume = raw_volume.loc[d, s] if d in raw_volume.index and s in raw_volume.columns else pd.NA
        prev_close = prev_close_matrix.loc[d, s] if d in prev_close_matrix.index and s in prev_close_matrix.columns else pd.NA
        category = cat.get("category", "") if cat is not None else ""
        market = cat.get("market", "") if cat is not None else ""
        name = cat.get("name", "") if cat is not None and "name" in cat.index else ""
        refs = event_refs.get(s, {})
        event_ref_value = next(iter(refs.values())) if refs else pd.NA
        event_ref_source = next(iter(refs.keys())) if refs else ""

        reasons = []
        if s not in ordinary:
            if market not in {"sii", "otc"}:
                reasons.append(f"market={market or 'missing'}")
            if not pd.Series([s]).str.fullmatch(r"\d{4}", na=False).iloc[0]:
                reasons.append("not_4_digit")
            if "存託憑證" in str(category):
                reasons.append("depositary_receipt")
            if not reasons:
                reasons.append("excluded_by_ordinary_rule")
        else:
            reasons.append("passes_finlab_ordinary_rule")

        try:
            vol_num = float(volume)
        except Exception:
            vol_num = float("nan")
        if pd.isna(close):
            reasons.append("close_missing")
        if pd.isna(vol_num) or vol_num <= 0:
            reasons.append("not_traded_or_volume_missing")
        if s in ordinary and pd.notna(close) and vol_num > 0 and pd.isna(prev_close) and not refs:
            reasons.append("reference_missing")
        if legacy_company_set and s not in legacy_company_set:
            reasons.append("not_in_legacy_company_openapi")

        rows.append({
            "stock_id": s,
            "name": name,
            "category": category,
            "market": market,
            "legacy_in": s in old_symbols,
            "finlab_in": s in new_symbols,
            "legacy_company_openapi": (s in legacy_company_set) if legacy_company_set else pd.NA,
            "finlab_ordinary_rule": s in ordinary,
            "close": close,
            "volume": volume,
            "prev_raw_close": prev_close,
            "event_ref": event_ref_value,
            "event_ref_source": event_ref_source,
            "diagnosis": ";".join(reasons),
        })

    return pd.DataFrame(rows).set_index("stock_id") if rows else pd.DataFrame()


def print_index_dataset_diagnostics(d: pd.Timestamp) -> None:
    """Inspect FinLab index datasets needed for 加權% / 櫃買%."""
    candidates = [
        "taiex_total_index:收盤指數",
        "market_transaction_info:收盤指數",
        "stock_index_price:收盤指數",
        "benchmark_return:發行量加權股價報酬指數",
    ]
    print("\n=== FinLab index dataset diagnostics ===")
    for ds in candidates:
        print(f"\n--- {ds} ---")
        try:
            x = data.get(ds)
        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")
            continue

        print("type:", type(x))
        print("shape:", getattr(x, "shape", None))
        if hasattr(x, "index") and len(x.index):
            print("index_min:", x.index.min())
            print("index_max:", x.index.max())
        if isinstance(x, pd.DataFrame):
            print("columns_sample:", list(map(str, x.columns[:30])))
            if d in x.index:
                row = x.loc[d]
                if isinstance(row, pd.Series):
                    nonnull = row.dropna()
                    print("target_date_nonnull_count:", len(nonnull))
                    print(nonnull.head(50).to_string())
        elif isinstance(x, pd.Series):
            print("name:", x.name)
            if d in x.index:
                print("target_date_value:", x.loc[d])
            around = x.loc[(x.index >= d - pd.Timedelta(days=5)) & (x.index <= d)]
            print("around_target:")
            print(around.tail(5).to_string())


def print_legacy_comparison(result, stats) -> None:
    target = result.target_date.date()
    print("\n=== Legacy TWSE/TPEX comparison ===")
    try:
        old_df, old_taiex, old_otc = legacy.fetch_market_snapshot(target)
    except Exception as exc:
        print("[WARNING] Legacy TWSE/TPEX comparison unavailable; FinLab validation remains valid.")
        print(f"reason: {type(exc).__name__}: {exc}")
        return

    old_stats = legacy.calc_breadth(old_df, taiex_pct=old_taiex, include_vs_market=False)
    keys = ["漲停", "大漲", "小漲", "平盤", "小跌", "大跌", "跌停", "總上漲", "總下跌", "總家數"]
    comparison = pd.DataFrame({
        "TWSE_TPEX": [old_stats.get(k) for k in keys],
        "FinLab": [stats.get(k) for k in keys],
    }, index=keys)
    comparison["diff"] = comparison["FinLab"] - comparison["TWSE_TPEX"]
    print(f"legacy taiex_pct: {old_taiex}")
    print(f"legacy otc_pct:   {old_otc}")
    print(comparison.to_string())

    old_symbols = set(old_df.index.astype(str))
    new_symbols = set(result.stock_df.index.astype(str))
    print("\nUniverse difference:")
    print(f"  legacy only ({len(old_symbols - new_symbols)}): {sorted(old_symbols - new_symbols)}")
    print(f"  FinLab only ({len(new_symbols - old_symbols)}): {sorted(new_symbols - old_symbols)}")

    universe_diag = build_universe_diagnostic(result, old_df)
    print("\nUniverse difference diagnostics:")
    print(universe_diag.to_string() if not universe_diag.empty else "  none")

    common = sorted(old_symbols & new_symbols)
    old_labels = classify_rows(old_df.loc[common], new_schema=False)
    new_labels = classify_rows(result.stock_df.loc[common], new_schema=True)
    row_diff = pd.DataFrame({
        "legacy_pct": pd.to_numeric(old_df.loc[common, "漲跌幅"], errors="coerce"),
        "finlab_pct": pd.to_numeric(result.stock_df.loc[common, "漲跌幅"], errors="coerce"),
        "legacy_bucket": old_labels,
        "finlab_bucket": new_labels,
    })
    row_diff["abs_pct_diff"] = (row_diff["legacy_pct"] - row_diff["finlab_pct"]).abs()
    bucket_diff = row_diff[row_diff["legacy_bucket"].ne(row_diff["finlab_bucket"])].copy()

    print("\nPer-symbol bucket differences on common universe:")
    print("  none" if bucket_diff.empty else bucket_diff.to_string())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-08-31")
    parser.add_argument("--skip-legacy", action="store_true")
    parser.add_argument("--index-diagnostics", action="store_true")
    args = parser.parse_args()

    result = load_finlab_market_breadth_data(args.date)
    stats = calc_breadth(result.stock_df)

    print("=== FinLab market breadth validation ===")
    print("target_date:", result.target_date.date())
    print("rows:", len(result.stock_df))
    print("\nData sources:")
    for k, v in result.data_sources.items():
        print(f"  {k}: {v}")

    print("\nBreadth:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    if args.index_diagnostics:
        print_index_dataset_diagnostics(result.target_date)

    if not args.skip_legacy:
        print_legacy_comparison(result, stats)


if __name__ == "__main__":
    main()
