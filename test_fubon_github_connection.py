"""Read-only Fubon Neo login smoke test for GitHub Actions."""

from integrated_stock_pipeline_exitlog_fixed_strategy_ledger_v2 import (
    login_fubon_from_env,
)


def main() -> None:
    sdk, account = login_fubon_from_env()
    account_id = getattr(account, "account", None) or getattr(account, "account_no", None)
    masked = f"***{str(account_id)[-4:]}" if account_id else "(account object returned)"
    print(f"Fubon login succeeded: {masked}")

    logout = getattr(sdk, "logout", None)
    if callable(logout):
        try:
            logout()
        except TypeError:
            # Some SDK releases require the account argument.
            logout(account)


if __name__ == "__main__":
    main()
