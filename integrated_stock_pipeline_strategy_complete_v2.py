# -*- coding: utf-8 -*-
"""Enhanced strategy-ledger wrapper for the existing Fubon pipeline.

Keeps the original market breadth / FIFO / MFE / exit-log workflow, while
hardening order-intent matching and partial-fill accounting.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

import integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2 as _base

PipelineConfig = _base.PipelineConfig
IntegratedConfig = PipelineConfig


def _active_intents(config: PipelineConfig) -> pd.DataFrame:
    intents = _base.load_order_intents(config)
    if intents.empty:
        return intents
    for col in ["signal_date", "order_date", "expected_order_date", "expires_date"]:
        if col in intents.columns:
            intents[col] = pd.to_datetime(intents[col], errors="coerce").dt.date
    if "status" not in intents.columns:
        intents["status"] = "planned"
    active = {"planned", "submitted", "open", "partially_filled"}
    return intents[intents["status"].fillna("planned").astype(str).isin(active)].copy()


def _match_intent_safe(
    fill: pd.Series,
    intents: pd.DataFrame,
) -> tuple[Optional[pd.Series], str, bool]:
    """Return (intent, allocation_method, review_required).

    Never silently chooses between multiple same-day strategy intents.
    """
    if intents.empty:
        return None, "unmatched_no_intent", True

    stock_no = _base.normalize_stock_no(fill.get("stock_no", ""))
    side = _base.normalize_side(fill.get("side", ""))
    fill_date = fill.get("date")
    order_no = str(fill.get("order_no", "") or "").strip()

    candidates = intents[
        intents["stock_no"].eq(stock_no) & intents["side"].eq(side)
    ].copy()
    if candidates.empty:
        return None, "unmatched_no_stock_side_intent", True

    if order_no and "order_no" in candidates.columns:
        exact = candidates[candidates["order_no"].fillna("").astype(str).eq(order_no)]
        if len(exact) == 1:
            return exact.iloc[0], "matched_order_no", False
        if len(exact) > 1:
            return None, "ambiguous_duplicate_order_no", True

    date_col = "expected_order_date" if "expected_order_date" in candidates.columns else "order_date"
    if fill_date is not None and date_col in candidates.columns:
        exact_date = candidates[candidates[date_col].eq(fill_date)]
        if len(exact_date) == 1:
            return exact_date.iloc[0], "matched_stock_side_date", False
        if len(exact_date) > 1:
            return None, "ambiguous_multiple_intents_same_day", True

    # Fallback: latest still-active intent with signal_date <= fill_date.
    if fill_date is not None and "signal_date" in candidates.columns:
        eligible = candidates[candidates["signal_date"].notna() & candidates["signal_date"].le(fill_date)].copy()
        if "expires_date" in eligible.columns:
            eligible = eligible[eligible["expires_date"].isna() | eligible["expires_date"].ge(fill_date)]
        if not eligible.empty:
            latest_signal = eligible["signal_date"].max()
            latest = eligible[eligible["signal_date"].eq(latest_signal)]
            if len(latest) == 1:
                return latest.iloc[0], "matched_latest_active_intent", False
            return None, "ambiguous_multiple_latest_intents", True

    return None, "unmatched_no_date_match", True


def _upsert_buy_lot(
    lots: pd.DataFrame,
    *,
    trade_id: str,
    strategy_id: str,
    stock_no: str,
    fill_date: Any,
    qty: int,
    price: float,
    intent: Optional[pd.Series],
    review_required: bool,
) -> pd.DataFrame:
    """Create a lot or accumulate a partial fill into the same trade_id."""
    now = datetime.now()
    mask = lots["trade_id"].fillna("").astype(str).eq(str(trade_id)) if not lots.empty else pd.Series(dtype=bool)

    if not lots.empty and mask.any():
        idx = lots.index[mask][-1]
        old_qty = int(pd.to_numeric(pd.Series([lots.at[idx, "original_quantity"]]), errors="coerce").fillna(0).iloc[0])
        old_rem = int(pd.to_numeric(pd.Series([lots.at[idx, "remaining_quantity"]]), errors="coerce").fillna(0).iloc[0])
        old_price = pd.to_numeric(pd.Series([lots.at[idx, "entry_price"]]), errors="coerce").iloc[0]
        total_qty = old_qty + qty
        if total_qty > 0 and pd.notna(price):
            weighted = ((0 if pd.isna(old_price) else float(old_price)) * old_qty + float(price) * qty) / total_qty
        else:
            weighted = old_price
        lots.at[idx, "entry_price"] = weighted
        lots.at[idx, "original_quantity"] = total_qty
        lots.at[idx, "remaining_quantity"] = old_rem + qty
        lots.at[idx, "status"] = "open"
        lots.at[idx, "updated_at"] = now
        lots.at[idx, "review_required"] = bool(lots.at[idx, "review_required"]) or review_required
        return lots

    planned_exit_date = pd.NaT
    stop_price = np.nan
    if intent is not None:
        if "planned_exit_date" in intent.index:
            planned_exit_date = intent.get("planned_exit_date")
        if "stop_price" in intent.index:
            stop_price = intent.get("stop_price")

    lot = {
        "trade_id": trade_id,
        "strategy_id": strategy_id,
        "stock_no": stock_no,
        "entry_date": fill_date,
        "entry_price": price,
        "original_quantity": qty,
        "remaining_quantity": qty,
        "planned_exit_date": planned_exit_date,
        "stop_price": stop_price,
        "status": "open",
        "source": "matched_order_intent" if intent is not None else "manual_or_unknown_buy",
        "created_at": now,
        "updated_at": now,
        "review_required": review_required,
    }
    return pd.concat([lots, pd.DataFrame([lot])], ignore_index=True)


def _update_intent_statuses(
    intents: pd.DataFrame,
    execution_log: pd.DataFrame,
    target,
    config: PipelineConfig,
) -> pd.DataFrame:
    if intents.empty:
        return intents

    out = intents.copy()
    for col, default in {
        "status": "planned",
        "filled_date": pd.NaT,
        "filled_quantity": np.nan,
        "filled_price": np.nan,
        "updated_at": pd.NaT,
    }.items():
        if col not in out.columns:
            out[col] = default

    if not execution_log.empty:
        matched = execution_log[
            execution_log["trade_id"].fillna("").astype(str).ne("")
            & execution_log["allocation_method"].fillna("").astype(str).str.startswith("matched")
        ].copy()
        for trade_id, grp in matched.groupby("trade_id"):
            mask = out["trade_id"].astype(str).eq(str(trade_id))
            if not mask.any():
                continue
            qty = pd.to_numeric(grp["qty"], errors="coerce").fillna(0)
            price = pd.to_numeric(grp["price"], errors="coerce")
            total_qty = float(qty.sum())
            weighted_price = float((price * qty).sum() / total_qty) if total_qty > 0 else np.nan
            out.loc[mask, "status"] = "filled"
            out.loc[mask, "filled_date"] = max(grp["date"])
            out.loc[mask, "filled_quantity"] = total_qty
            out.loc[mask, "filled_price"] = weighted_price
            out.loc[mask, "updated_at"] = datetime.now()

    # Intents are deliberately kept active through expected order date.
    date_col = "expires_date" if "expires_date" in out.columns else None
    if date_col:
        exp = pd.to_datetime(out[date_col], errors="coerce").dt.date
        active = out["status"].fillna("planned").isin(["planned", "submitted", "open"])
        expired = active & exp.notna() & exp.lt(target)
        out.loc[expired, "status"] = "expired"
        out.loc[expired, "updated_at"] = datetime.now()

    path = _base._ledger_path(config, config.strategy_order_intent_file)
    _base._write_parquet_csv(out, path)
    return out


def update_strategy_ledger_complete(
    inventory: pd.DataFrame,
    trades: pd.DataFrame,
    target,
    config: PipelineConfig,
) -> dict[str, Any]:
    registry, registry_path = _base.ensure_strategy_registry(config)
    lots_path = _base._ledger_path(config, config.strategy_position_lots_file)
    execution_path = _base._ledger_path(config, config.strategy_execution_log_file)
    reconciliation_path = _base._ledger_path(config, config.strategy_reconciliation_file)

    lots_existed = lots_path.exists()
    lots = _base._read_parquet_or_empty(lots_path, _base.STRATEGY_LOT_COLUMNS)
    execution_log = _base._read_parquet_or_empty(execution_path, _base.STRATEGY_EXECUTION_COLUMNS)
    intents_all = _base.load_order_intents(config)
    intents = _active_intents(config)

    if not lots_existed:
        lots = _base.bootstrap_strategy_lots(inventory, config)
        bootstrap_exec = trades.copy()
        if not bootstrap_exec.empty:
            bootstrap_exec["execution_key"] = bootstrap_exec.apply(_base._execution_key, axis=1)
            bootstrap_exec["trade_id"] = ""
            bootstrap_exec["strategy_id"] = config.legacy_strategy_id
            bootstrap_exec["allocation_method"] = "historical_preledger_bootstrap"
            bootstrap_exec["review_required"] = False
            bootstrap_exec["processed_at"] = datetime.now()
            for c in _base.STRATEGY_EXECUTION_COLUMNS:
                if c not in bootstrap_exec.columns:
                    bootstrap_exec[c] = np.nan
            execution_log = bootstrap_exec[_base.STRATEGY_EXECUTION_COLUMNS].copy()
    else:
        known_keys = set(execution_log["execution_key"].astype(str)) if not execution_log.empty else set()
        fills = trades.copy()
        if not fills.empty:
            fills["execution_key"] = fills.apply(_base._execution_key, axis=1)
            fills = fills[~fills["execution_key"].astype(str).isin(known_keys)].copy()

        new_exec_rows = []
        for _, fill in fills.sort_values(["date", "time", "stock_no"]).iterrows():
            intent, allocation_method, match_review = _match_intent_safe(fill, intents)
            strategy_id = (
                str(intent.get("strategy_id"))
                if intent is not None and pd.notna(intent.get("strategy_id"))
                else config.unassigned_strategy_id
            )
            trade_id = (
                str(intent.get("trade_id"))
                if intent is not None and pd.notna(intent.get("trade_id"))
                else ""
            )
            side = _base.normalize_side(fill.get("side", ""))
            stock_no = _base.normalize_stock_no(fill.get("stock_no", ""))
            qty = int(fill.get("qty", 0) or 0)
            price = pd.to_numeric(pd.Series([fill.get("price")]), errors="coerce").iloc[0]
            review_required = bool(match_review)

            if side == "buy" and qty > 0:
                if not trade_id:
                    unique = fill.get("filled_no") or fill.get("order_no") or _base._timestamp()
                    trade_id = f"{strategy_id}_{stock_no}_{fill.get('date')}_{unique}"
                lots = _upsert_buy_lot(
                    lots,
                    trade_id=trade_id,
                    strategy_id=strategy_id,
                    stock_no=stock_no,
                    fill_date=fill.get("date"),
                    qty=qty,
                    price=price,
                    intent=intent,
                    review_required=review_required,
                )
            elif side == "sell" and qty > 0:
                lots, unallocated_qty, sell_method, sell_review = _base._allocate_sell_fifo(
                    lots,
                    stock_no,
                    qty,
                    strategy_id if intent is not None else None,
                    trade_id if trade_id else None,
                )
                allocation_method = sell_method
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
        lots["stock_no"] = lots["stock_no"].map(_base.normalize_stock_no)
        lots["original_quantity"] = pd.to_numeric(lots["original_quantity"], errors="coerce").fillna(0).astype(int)
        lots["remaining_quantity"] = pd.to_numeric(lots["remaining_quantity"], errors="coerce").fillna(0).astype(int)
        lots["status"] = np.where(lots["remaining_quantity"].gt(0), "open", "closed")
        lots = lots.drop_duplicates(subset=["trade_id"], keep="last").reset_index(drop=True)

    if not execution_log.empty:
        execution_log = execution_log.drop_duplicates(subset=["execution_key"], keep="last").reset_index(drop=True)

    intents_all = _update_intent_statuses(intents_all, execution_log, target, config)

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
    reconciliation = reconciliation[[
        "reconciliation_date", "stock_no", "total_today_qty",
        "remaining_quantity", "difference", "status",
    ]].sort_values("stock_no").reset_index(drop=True)

    _base._write_parquet_csv(lots, lots_path)
    _base._write_parquet_csv(execution_log, execution_path)
    old_recon = _base._read_parquet_or_empty(reconciliation_path, list(reconciliation.columns))
    recon_all = pd.concat([old_recon, reconciliation], ignore_index=True)
    recon_all = recon_all.drop_duplicates(subset=["reconciliation_date", "stock_no"], keep="last")
    _base._write_parquet_csv(recon_all, reconciliation_path)

    return {
        "registry": registry,
        "lots": lots,
        "execution_log": execution_log,
        "order_intents": intents_all,
        "reconciliation": reconciliation,
        "paths": {
            "strategy_registry": registry_path,
            "strategy_position_lots": lots_path,
            "strategy_execution_log": execution_path,
            "strategy_reconciliation": reconciliation_path,
            "strategy_order_intent": _base._ledger_path(config, config.strategy_order_intent_file),
        },
    }


# Patch the base module so its existing run_pipeline uses the hardened ledger.
_base.update_strategy_ledger = update_strategy_ledger_complete


def run_pipeline(config: Optional[PipelineConfig] = None) -> dict[str, Any]:
    return _base.run_pipeline(config)


def run_integrated_pipeline(config: Optional[PipelineConfig] = None) -> dict[str, Any]:
    return run_pipeline(config)
