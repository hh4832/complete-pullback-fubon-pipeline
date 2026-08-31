# -*- coding: utf-8 -*-
"""Validate FinLab-only market breadth before production replacement."""
from __future__ import annotations

import argparse
import pandas as pd
from finlab import data

import integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2 as legacy
from finlab_market_breadth import (
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


def build_universe_diagnostic(result, old_df: pd.DataFrame) -> pd.DataFrame:
    """Explain why legacy and FinLab universes differ for the target date."""
    d = result.target_date
    old_symbols = set(old_df.index.astype(str))
    new_symbols = set(result.stock_df.index.astype(str))
    symbols = sorted(old_symbols ^ new_symbols)

    raw_close = data.get("price:收盤價")
    raw_volume = data.get("price:成交股數")
    cats = result.security_categories.copy()
    ordinary = set(ordinary_stock_symbols(cats).astype(str))

    cat_lookup = (
        cats.drop_duplicates(subset=["stock_id"], keep="last")
        .set_index("stock_id")
    )

    rows = []
    for s in symbols:
        cat = cat_lookup.loc[s] if s in cat_lookup.index else None
        close = raw_close.loc[d, s] if d in raw_close.index and s in raw_close.columns else pd.NA
        volume = raw_volume.loc[d, s] if d in raw_volume.index and s in raw_volume.columns else pd.NA
        category = cat.get("category", "") if cat is not None else ""
        market = cat.get("market", "") if cat is not None else ""
        name = cat.get("name", "") if cat is not None and "name" in cat.index else ""

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

        rows.append({
            "stock_id": s,
            "name": name,
            "category": category,
            "market": market,
            "legacy_in": s in old_symbols,
            "finlab_in": s in new_symbols,
            "finlab_ordinary_rule": s in ordinary,
            "close": close,
            "volume": volume,
            "diagnosis": ";".join(reasons),
        })

    return pd.DataFrame(rows).set_index("stock_id") if rows else pd.DataFrame()


def print_legacy_comparison(result, stats) -> None:
    target = result.target_date.date()
    print("\n=== Legacy TWSE/TPEX comparison ===")
    old_df, old_taiex, old_otc = legacy.fetch_market_snapshot(target)
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
    if bucket_diff.empty:
        print("  none")
    else:
        print(bucket_diff.sort_values(["legacy_bucket", "finlab_bucket", "abs_pct_diff"], ascending=[True, True, False]).to_string())

    print("\nLargest raw percentage differences on common universe:")
    print(row_diff.sort_values("abs_pct_diff", ascending=False).head(30).to_string())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-08-31")
    parser.add_argument("--skip-legacy", action="store_true", help="Skip TWSE/TPEX comparison.")
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

    print("\nLargest absolute moves:")
    print(
        result.stock_df.assign(abs_pct=result.stock_df["漲跌幅"].abs())
        .sort_values("abs_pct", ascending=False)
        .head(30)[["收盤價", "參考價", "漲跌幅", "成交股數"]]
        .to_string()
    )

    print("\nPotential special-security checks:")
    cats = result.security_categories
    for s in ["2330", "6415", "9105", "911608"]:
        row = cats[cats["stock_id"].astype(str).eq(s)]
        print(f"\n{s}")
        print(row.to_string(index=False) if not row.empty else "not found")

    if not args.skip_legacy:
        print_legacy_comparison(result, stats)


if __name__ == "__main__":
    main()
