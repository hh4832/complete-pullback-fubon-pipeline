# -*- coding: utf-8 -*-
from pathlib import Path

from integrated_stock_pipeline_strategy_complete_v2 import PipelineConfig
from complete_pullback_fubon_pipeline_v2 import CompletePipelineConfig, run_complete_daily_pipeline


if __name__ == "__main__":
    # Use the folder containing this script on both Windows and GitHub Actions.
    PROJECT_DIR = Path(__file__).resolve().parent

    broker_config = PipelineConfig(
        target_date=None,
        google_credentials_file=Path("service_account.json"),
        spreadsheet_id="1Re645rLgNH9_PDLYr57_QmuvQdu3y_soLbmFYIpqSmk",
        sheet_name="市場廣度",
        enable_google_sheet=True,
        entry_condition_file=Path("holdings_entry_conditions.csv"),
        use_cache=False,
        output_dir=Path("output_integrated_stock"),
        cache_dir=Path("cache_integrated_stock"),
        strategy_ledger_dir=Path("strategy_ledger"),
        enable_strategy_ledger=True,
        lookback_days=365 * 3,
        filled_history_chunk_days=29,
        mfe_threshold=0.40,
        min_holding_trading_days=35,
        pullback_threshold=0.25,
        close_loss_threshold=0.15,
        continue_on_market_error=True,
        continue_on_gsheet_error=True,
        continue_on_mfe_error=True,
        debug=True,
    )

    config = CompletePipelineConfig(
        project_dir=PROJECT_DIR,
        strategy_id="pullback_macd_day35_v1",
        trade_id_prefix="PB",
        intent_valid_calendar_days=5,
        selector_output_dir=Path("output_selector"),
        selector_signal_date=None,
        selector_total_equity=None,
        broker_config=broker_config,
        run_broker_first=True,
        create_order_intents=True,
    )

    result = run_complete_daily_pipeline(config)

    print("\n========== 完成 ==========")
    print("Signal date:", result["selection"]["signal_date"])
    print("Buy candidates:", len(result["selection"]["buy_list"]))
    print("New intents:", len(result["new_order_intents"]))
    print("Intent file:", result["order_intent_path"])
