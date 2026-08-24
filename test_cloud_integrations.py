"""Read-only smoke tests for Fubon, FinLab, and Google Sheets."""

import os
import tempfile
from pathlib import Path

import gspread
from finlab import data
from google.oauth2.service_account import Credentials

from integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2 import (
    PipelineConfig,
    fetch_inventory,
    login_fubon_from_env,
)


def test_finlab() -> None:
    close = data.get("price:收盤價")
    if close is None or close.empty:
        raise RuntimeError("FinLab returned an empty close-price dataset")
    print(f"FinLab read succeeded: latest date {close.index[-1]}")


def test_google_sheet() -> None:
    credentials_path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    spreadsheet_id = os.environ["GOOGLE_SPREADSHEET_ID"]
    worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "市場廣度")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    ]
    credentials = Credentials.from_service_account_file(credentials_path, scopes=scopes)
    worksheet = gspread.authorize(credentials).open_by_key(spreadsheet_id).worksheet(worksheet_name)
    worksheet.row_values(1)
    print(f"Google Sheet read succeeded: worksheet {worksheet.title}")


def test_fubon_inventory() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        config = PipelineConfig(
            use_cache=False,
            cache_dir=temp_path / "cache",
            output_dir=temp_path / "output",
            strategy_ledger_dir=temp_path / "ledger",
            enable_google_sheet=False,
            enable_strategy_ledger=False,
        )
        sdk, account = login_fubon_from_env()
        inventory = fetch_inventory(sdk, account, config)
        print(f"Fubon inventory read succeeded: {len(inventory)} position rows")

        logout = getattr(sdk, "logout", None)
        if callable(logout):
            try:
                logout()
            except TypeError:
                logout(account)


def main() -> None:
    test_fubon_inventory()
    test_finlab()
    test_google_sheet()
    print("All read-only cloud integration tests succeeded")


if __name__ == "__main__":
    main()
