# -*- coding: utf-8 -*-
"""Validate FinLab-only market breadth before production replacement."""
from __future__ import annotations

import argparse
import pandas as pd

from finlab_market_breadth import (
    calc_breadth,
    load_finlab_market_breadth_data,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-08-31")
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


if __name__ == "__main__":
    main()
