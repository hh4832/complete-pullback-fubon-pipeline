# -*- coding: utf-8 -*-
"""Complete daily workflow: Fubon reconciliation -> FinLab selection -> order intents.

This module does NOT place orders. It records strategy intent at signal time and
uses the next post-close Fubon filled history to populate actual quantity/price.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from pandas.tseries.offsets import BDay

from integrated_stock_pipeline_strategy_complete_v2 import (
    PipelineConfig as BrokerPipelineConfig,
    run_pipeline as run_broker_pipeline,
)
import pullback_macdonly_daily_selector_v2 as selector


ORDER_INTENT_COLUMNS = [
    "trade_id", "strategy_id", "signal_date", "expected_order_date",
    "expires_date", "stock_no", "side", "status", "created_at", "updated_at",
    "order_no", "filled_date", "filled_quantity", "filled_price",
    "signal_rank", "bias_at_signal", "note",
]


@dataclass
class CompletePipelineConfig:
    project_dir: Path = Path(".")
    strategy_id: str = "pullback_macd_day35_v1"
    trade_id_prefix: str = "PB"
    intent_valid_calendar_days: int = 5

    # Selector
    selector_output_dir: Path = Path("./output_selector")
    selector_signal_date: Optional[str] = None
    selector_total_equity: Optional[float] = None
    selector_config_overrides: dict[str, Any] = field(default_factory=dict)

    # Broker / ledger
    broker_config: BrokerPipelineConfig = field(default_factory=BrokerPipelineConfig)
    run_broker_first: bool = True
    create_order_intents: bool = True

    def __post_init__(self) -> None:
        self.project_dir = Path(self.project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.selector_output_dir = Path(self.selector_output_dir)
        if not self.selector_output_dir.is_absolute():
            self.selector_output_dir = self.project_dir / self.selector_output_dir
        self.selector_output_dir.mkdir(parents=True, exist_ok=True)

        ledger_dir = Path(self.broker_config.strategy_ledger_dir)
        if not ledger_dir.is_absolute():
            ledger_dir = self.project_dir / ledger_dir
        self.broker_config.strategy_ledger_dir = ledger_dir
        self.broker_config.strategy_ledger_dir.mkdir(parents=True, exist_ok=True)

        out_dir = Path(self.broker_config.output_dir)
        if not out_dir.is_absolute():
            out_dir = self.project_dir / out_dir
        self.broker_config.output_dir = out_dir
        self.broker_config.output_dir.mkdir(parents=True, exist_ok=True)

        cache_dir = Path(self.broker_config.cache_dir)
        if not cache_dir.is_absolute():
            cache_dir = self.project_dir / cache_dir
        self.broker_config.cache_dir = cache_dir
        self.broker_config.cache_dir.mkdir(parents=True, exist_ok=True)

        for attr in ["google_credentials_file", "entry_condition_file"]:
            p = Path(getattr(self.broker_config, attr))
            if not p.is_absolute():
                setattr(self.broker_config, attr, self.project_dir / p)


def _intent_path(config: CompletePipelineConfig) -> Path:
    p = Path(config.broker_config.strategy_order_intent_file)
    return p if p.is_absolute() else config.broker_config.strategy_ledger_dir / p


def _read_intents(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=ORDER_INTENT_COLUMNS)
    df = pd.read_parquet(path)
    for c in ORDER_INTENT_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA
    return df


def _write_intents(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    df.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")


def append_basic_order_intents(
    buy_list: pd.DataFrame,
    signal_date,
    config: CompletePipelineConfig,
) -> pd.DataFrame:
    """Append strategy + stock intent only. Quantity and price come from Fubon fills."""
    path = _intent_path(config)
    old = _read_intents(path)

    if buy_list is None or buy_list.empty:
        print("[INFO] 今日 buy_list 為空，不新增 order intent。")
        return pd.DataFrame(columns=ORDER_INTENT_COLUMNS)

    signal_ts = pd.Timestamp(signal_date)
    expected_order_date = (signal_ts + BDay(1)).date()
    expires_date = expected_order_date + timedelta(days=config.intent_valid_calendar_days)
    now = datetime.now()
    rows = []

    for _, row in buy_list.reset_index(drop=True).iterrows():
        stock_no = str(row.get("stock_id", row.get("stock_no", ""))).strip().zfill(4)
        if not stock_no:
            continue
        trade_id = f"{config.trade_id_prefix}_{signal_ts:%Y%m%d}_{stock_no}"
        rows.append({
            "trade_id": trade_id,
            "strategy_id": config.strategy_id,
            "signal_date": signal_ts.date(),
            "expected_order_date": expected_order_date,
            "expires_date": expires_date,
            "stock_no": stock_no,
            "side": "buy",
            "status": "planned",
            "created_at": now,
            "updated_at": now,
            "order_no": "",
            "filled_date": pd.NaT,
            "filled_quantity": pd.NA,
            "filled_price": pd.NA,
            "signal_rank": row.get("rank", pd.NA),
            "bias_at_signal": row.get("bias_at_signal", pd.NA),
            "note": "quantity/price filled automatically from Fubon post-close history",
        })

    new = pd.DataFrame(rows, columns=ORDER_INTENT_COLUMNS)
    if new.empty:
        return new

    combined = pd.concat([old, new], ignore_index=True)
    # Deterministic trade_id makes reruns idempotent; preserve already-filled records.
    combined["_status_priority"] = combined["status"].map({
        "filled": 5, "partially_filled": 4, "submitted": 3,
        "planned": 2, "expired": 1, "cancelled": 1,
    }).fillna(0)
    combined = combined.sort_values(["trade_id", "_status_priority", "updated_at"])
    combined = combined.drop_duplicates(subset=["trade_id"], keep="last")
    combined = combined.drop(columns=["_status_priority"]).sort_values(
        ["signal_date", "stock_no"]
    ).reset_index(drop=True)
    _write_intents(combined, path)

    added_ids = set(new["trade_id"]) & set(combined["trade_id"])
    print(f"[OK] order intent 已更新：{path}")
    print(f"[INFO] 本次訊號 {len(new)} 筆；目前帳本共 {len(combined)} 筆 intent。")
    return combined[combined["trade_id"].isin(added_ids)].copy()


def _inventory_as_holdings_file(
    inventory: pd.DataFrame,
    config: CompletePipelineConfig,
) -> Path:
    path = config.broker_config.strategy_ledger_dir / "current_broker_holdings_for_selector.parquet"
    if inventory is None or inventory.empty:
        pd.DataFrame(columns=["stock_no"]).to_parquet(path, index=False)
    else:
        inventory[["stock_no"]].drop_duplicates().to_parquet(path, index=False)
    return path


def run_complete_daily_pipeline(
    config: Optional[CompletePipelineConfig] = None,
) -> dict[str, Any]:
    """Recommended post-close sequence.

    1) Fubon: import fills, update lots, reconcile inventory.
    2) FinLab: select latest close signals, excluding actual broker holdings.
    3) Append basic order intents for the next session.
    """
    config = config or CompletePipelineConfig()
    # Keep the live broker/exit watcher aligned with the validated Day-35 strategy.
    config.broker_config.min_holding_trading_days = 35
    print("版本確認：complete_pullback_fubon_pipeline v1.0")

    broker_result = None
    inventory = pd.DataFrame()
    if config.run_broker_first:
        print("\n========== A. 富邦盤後 / 策略帳本 ==========")
        broker_result = run_broker_pipeline(config.broker_config)
        inventory = broker_result.get("inventory", pd.DataFrame())

    holdings_path = _inventory_as_holdings_file(inventory, config)

    selector_cfg = selector.CONFIG.copy()
    selector_cfg.update(config.selector_config_overrides)
    selector_cfg["MAX_HOLD_DAYS"] = 35
    selector_cfg["STOP_LOSS"] = -0.15
    selector_cfg["MAX_POSITIONS"] = 20
    selector_cfg["POSITION_PCT"] = 0.05

    print("\n========== B. FinLab 收盤選股 ==========")
    selection = selector.build_daily_candidates(
        cfg=selector_cfg,
        holdings_path=str(holdings_path),
        total_equity=config.selector_total_equity,
        signal_date=config.selector_signal_date,
    )
    selector.save_daily_selection(selection, output_dir=str(config.selector_output_dir))

    new_intents = pd.DataFrame(columns=ORDER_INTENT_COLUMNS)
    if config.create_order_intents:
        print("\n========== C. 建立基本 order intent ==========")
        new_intents = append_basic_order_intents(
            selection["buy_list"],
            selection["signal_date"],
            config,
        )

    return {
        "broker": broker_result,
        "selection": selection,
        "new_order_intents": new_intents,
        "order_intent_path": _intent_path(config),
        "holdings_path_used_by_selector": holdings_path,
    }


def run_selection_and_intent_only(
    config: Optional[CompletePipelineConfig] = None,
) -> dict[str, Any]:
    """Run selector without contacting Fubon; uses latest saved reconciliation/lots."""
    config = config or CompletePipelineConfig(run_broker_first=False)
    config.run_broker_first = False

    lots_path = config.broker_config.strategy_ledger_dir / config.broker_config.strategy_position_lots_file
    if lots_path.exists():
        lots = pd.read_parquet(lots_path)
        open_lots = lots[pd.to_numeric(lots.get("remaining_quantity", 0), errors="coerce").fillna(0).gt(0)]
        inventory = pd.DataFrame({"stock_no": open_lots["stock_no"].astype(str).drop_duplicates()})
    else:
        inventory = pd.DataFrame(columns=["stock_no"])
    holdings_path = _inventory_as_holdings_file(inventory, config)

    selector_cfg = selector.CONFIG.copy()
    selector_cfg.update(config.selector_config_overrides)
    selector_cfg["MAX_HOLD_DAYS"] = 35
    selector_cfg["STOP_LOSS"] = -0.15
    selector_cfg["MAX_POSITIONS"] = 20
    selector_cfg["POSITION_PCT"] = 0.05
    selection = selector.build_daily_candidates(
        cfg=selector_cfg,
        holdings_path=str(holdings_path),
        total_equity=config.selector_total_equity,
        signal_date=config.selector_signal_date,
    )
    selector.save_daily_selection(selection, output_dir=str(config.selector_output_dir))
    new_intents = append_basic_order_intents(selection["buy_list"], selection["signal_date"], config)
    return {
        "selection": selection,
        "new_order_intents": new_intents,
        "order_intent_path": _intent_path(config),
    }
