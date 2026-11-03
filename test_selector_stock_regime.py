"""Stock-regime regression tests; require no live FinLab credentials."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd

def load_selector():
    finlab = types.ModuleType("finlab")
    finlab.data = types.SimpleNamespace()
    markets = types.ModuleType("finlab.markets")
    tw = types.ModuleType("finlab.markets.tw")
    tw.TWMarket = type("DummyTWMarket", (), {})
    finlab.markets = markets
    markets.tw = tw
    spec = importlib.util.spec_from_file_location(
        "selector_regime_test_subject",
        Path(__file__).resolve().parent / "pullback_macdonly_daily_selector_v2.py",
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "finlab": finlab, "finlab.markets": markets, "finlab.markets.tw": tw,
    }):
        spec.loader.exec_module(module)
    return module

class StockRegimeMissingMATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.selector = load_selector()
        cls.cfg = cls.selector.CONFIG.copy()

    def test_missing_ma120_and_bear_stack_are_both_excluded(self):
        idx = pd.bdate_range("2026-01-01", periods=125)
        close = pd.DataFrame({
            "7772": np.arange(100., 225.),
            "7803": [np.nan] * 12 + list(np.arange(225., 112., -1)),
            "BEAR": np.arange(225., 100., -1),
            "FLAT": np.full(125, 100.),
        }, index=idx)
        passed, info = self.selector.build_stock_regime_filter(close, self.cfg)
        self.assertEqual(passed.iloc[-1].to_dict(), {
            "7772": True, "7803": False, "BEAR": False, "FLAT": True,
        })
        self.assertTrue(pd.isna(info["ma_slow"]["7803"].iloc[-1]))
        self.assertFalse(bool(info["ma_ready"]["7803"].iloc[-1]))
        self.assertTrue(bool(info["ma_ready"]["7772"].iloc[-1]))

    def test_partial_price_gap_in_120_day_window_is_excluded(self):
        idx = pd.bdate_range("2026-01-01", periods=125)
        close = pd.DataFrame({"GAP": np.arange(125, dtype=float)}, index=idx)
        close.iloc[50, 0] = np.nan
        passed, info = self.selector.build_stock_regime_filter(close, self.cfg)
        self.assertFalse(bool(info["ma_ready"]["GAP"].iloc[-1]))
        self.assertFalse(bool(passed["GAP"].iloc[-1]))

    def test_starts_passing_once_ma120_is_available(self):
        idx = pd.bdate_range("2026-01-01", periods=121)
        close = pd.DataFrame({"NEW": [np.nan] + list(np.arange(120, dtype=float))}, index=idx)
        passed, info = self.selector.build_stock_regime_filter(close, self.cfg)
        self.assertFalse(bool(info["ma_ready"]["NEW"].iloc[-2]))
        self.assertFalse(bool(passed["NEW"].iloc[-2]))
        self.assertTrue(bool(info["ma_ready"]["NEW"].iloc[-1]))
        self.assertTrue(bool(passed["NEW"].iloc[-1]))

    def test_other_regime_modes_remain_unchanged(self):
        idx = pd.bdate_range("2026-01-01", periods=125)
        close = pd.DataFrame({"BULL": np.arange(125, dtype=float) + 100}, index=idx)
        for mode in ("close_above_ma120", "ma60_above_ma120", "strong_bull_stack"):
            with self.subTest(mode=mode):
                cfg = {**self.cfg, "STOCK_REGIME_MODE": mode}
                passed, _ = self.selector.build_stock_regime_filter(close, cfg)
                self.assertTrue(bool(passed["BULL"].iloc[-1]))

    def test_strategy_parameters_unchanged(self):
        self.assertEqual(self.cfg["MARKET_REGIME_MODE"], "etf_drawdown_adx_or_di_plus")
        self.assertEqual(self.cfg["MARKET_ADX_THRESHOLD"], 25)
        self.assertEqual(self.cfg["STOP_LOSS"], -0.15)
        self.assertEqual(self.cfg["MAX_HOLD_DAYS"], 35)

if __name__ == "__main__":
    unittest.main()
