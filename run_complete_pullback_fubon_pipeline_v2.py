# -*- coding: utf-8 -*-
from datetime import datetime
import os
from pathlib import Path
import shutil
import subprocess
from zoneinfo import ZoneInfo

import integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2 as broker_base
from finlab_as_of_date import AsOfDateResolution, resolve_finlab_as_of_date
from finlab_market_breadth import fetch_finlab_market_snapshot
from integrated_stock_pipeline_strategy_complete_v2 import PipelineConfig
from complete_pullback_fubon_pipeline_v2 import CompletePipelineConfig, run_complete_daily_pipeline


# Production market breadth source: FinLab-only.
# Keep the base pipeline's downstream interface unchanged: the replacement
# returns (stock_df, taiex_pct, otc_pct), so Google Sheet / holdings breadth
# logic does not need to change in this phase.
broker_base.fetch_market_snapshot = fetch_finlab_market_snapshot


TAIPEI_TZ = ZoneInfo("Asia/Taipei")
REPO_NAME = "complete-pullback-fubon-pipeline"


def _git_value(project_dir: Path, *args: str, fallback: str = "unknown") -> str:
    try:
        return subprocess.check_output(
            ["git", *args], cwd=project_dir, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return fallback


def archive_run_to_google_drive(
    project_dir: Path,
    result: dict,
    as_of: AsOfDateResolution | None = None,
) -> Path | None:
    """Archive formal run outputs to Google Drive when Drive is mounted."""
    project_dir = Path(project_dir).resolve()

    override_root = os.getenv("QUANT_RESEARCH_DRIVE_ROOT", "").strip()
    if override_root:
        quant_root = Path(override_root).expanduser()
    else:
        mydrive = Path("/content/drive/MyDrive")
        if not mydrive.exists():
            print("[WARN] Google Drive 未掛載，略過 output archive。")
            return None
        quant_root = mydrive / "Quant_Research"

    timestamp = datetime.now(TAIPEI_TZ).strftime("%Y%m%d_%H%M%S")
    commit_full = _git_value(project_dir, "rev-parse", "HEAD")
    commit_short = _git_value(project_dir, "rev-parse", "--short", "HEAD")
    branch = _git_value(project_dir, "rev-parse", "--abbrev-ref", "HEAD")

    repo_root = quant_root / REPO_NAME
    repo_root.mkdir(parents=True, exist_ok=True)

    base_name = f"{timestamp}_{commit_short}"
    run_dir = repo_root / base_name
    suffix = 1
    while run_dir.exists():
        run_dir = repo_root / f"{base_name}_{suffix:02d}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)

    archived = []
    for folder_name in (
        "output_integrated_stock",
        "output_selector",
        "strategy_ledger",
    ):
        src = project_dir / folder_name
        if src.exists():
            shutil.copytree(src, run_dir / folder_name)
            archived.append(folder_name)

    selection = result.get("selection", {}) if isinstance(result, dict) else {}
    signal_date = selection.get("signal_date", "unknown")
    buy_candidates = len(selection.get("buy_list", [])) if selection else 0
    new_intents = result.get("new_order_intents", []) if isinstance(result, dict) else []

    requested = ""
    effective = str(signal_date)
    latest_complete = ""
    foreign_dataset = ""
    latest_by_source = {}
    if as_of is not None:
        requested = "" if as_of.requested_date is None else str(as_of.requested_date.date())
        effective = as_of.effective_date_str
        latest_complete = as_of.latest_complete_date_str
        foreign_dataset = as_of.foreign_dataset
        latest_by_source = as_of.latest_by_source

    run_info_lines = [
        f"timestamp_taipei={timestamp}",
        "timezone=Asia/Taipei",
        f"git_commit={commit_full}",
        f"git_commit_short={commit_short}",
        f"git_branch={branch}",
        f"requested_as_of_date={requested}",
        f"effective_as_of_date={effective}",
        f"latest_complete_finlab_date={latest_complete}",
        f"signal_date={signal_date}",
        f"buy_candidates={buy_candidates}",
        f"new_order_intents={len(new_intents)}",
        "market_breadth_source=FinLab",
        f"foreign_dataset={foreign_dataset}",
        f"archived_folders={','.join(archived)}",
    ]
    for source, latest in latest_by_source.items():
        run_info_lines.append(f"finlab_latest[{source}]={latest}")

    (run_dir / "run_info.txt").write_text(
        "\n".join(run_info_lines) + "\n",
        encoding="utf-8",
    )

    print(f"[OK] Output archive：{run_dir}")
    return run_dir


def resolve_production_as_of_date() -> AsOfDateResolution:
    """Resolve the one date used by broker, breadth and selector.

    Optional environment variable:
      AS_OF_DATE=YYYY-MM-DD

    Safety rule: the live production pipeline may only run the latest complete
    FinLab date. Historical dates require a separate research mode because the
    Fubon inventory API returns current inventory rather than historical
    inventory.
    """
    requested = os.getenv("AS_OF_DATE", "").strip() or None
    resolution = resolve_finlab_as_of_date(requested)

    print("\n========== FinLab as_of_date ==========")
    print("requested:", requested or "<auto>")
    print("effective:", resolution.effective_date_str)
    print("latest complete:", resolution.latest_complete_date_str)
    for source, latest in resolution.latest_by_source.items():
        print(f"  {source}: {latest}")

    if (
        resolution.requested_date is not None
        and resolution.effective_date != resolution.latest_complete_date
    ):
        raise RuntimeError(
            "目前 production live mode 不允許歷史 AS_OF_DATE。"
            "富邦 inventories API 是執行當下庫存，不能代表歷史庫存；"
            "歷史日期將在獨立 research mode 實作，避免產生錯誤帳本或 Google Sheet 紀錄。"
        )

    return resolution


if __name__ == "__main__":
    PROJECT_DIR = Path(__file__).resolve().parent

    # Resolve one complete FinLab date BEFORE any broker/Google Sheet write.
    as_of = resolve_production_as_of_date()

    broker_config = PipelineConfig(
        target_date=as_of.effective_date_str,
        google_credentials_file=Path("service_account.json"),
        spreadsheet_id="1-bs4-2mYutvQcUYY-np5zp-QqEJfab--TAjmfQi8RQY",
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
        continue_on_market_error=False,
        continue_on_gsheet_error=False,
        continue_on_mfe_error=True,
        debug=True,
    )

    config = CompletePipelineConfig(
        project_dir=PROJECT_DIR,
        strategy_id="pullback_macd_day35_v1",
        trade_id_prefix="PB",
        intent_valid_calendar_days=5,
        selector_output_dir=Path("output_selector"),
        selector_signal_date=as_of.effective_date_str,
        selector_total_equity=None,
        broker_config=broker_config,
        run_broker_first=True,
        create_order_intents=True,
    )

    result = run_complete_daily_pipeline(config)

    print("\n========== 完成 ==========")
    print("Effective as_of_date:", as_of.effective_date_str)
    print("Signal date:", result["selection"]["signal_date"])
    print("Buy candidates:", len(result["selection"]["buy_list"]))
    print("New intents:", len(result["new_order_intents"]))
    print("Intent file:", result["order_intent_path"])

    try:
        archive_run_to_google_drive(PROJECT_DIR, result, as_of=as_of)
    except Exception as exc:
        print(f"[WARN] Output archive 失敗：{exc}")
