import ast
import copy
import hashlib
from pathlib import Path
import unittest

import pandas as pd
from streamlit.testing.v1 import AppTest

from dashboard_data import (
    DEMO_PATH, NAV_OPTIONS, load_snapshot, participation, positions_frame, summary, validate_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]


class SnapshotTests(unittest.TestCase):
    def test_accounting_and_price_consistency(self):
        snapshot = load_snapshot()
        totals = summary(snapshot)
        self.assertEqual(totals["open_positions"], 4)
        self.assertEqual(totals["closed_positions"], 48)
        self.assertAlmostEqual(totals["total_equity"], snapshot["equity"][-1]["total_equity"], places=2)
        for p in positions_frame(snapshot).to_dict("records"):
            sign = 1 if p["direction"] == "LONG" else -1
            self.assertAlmostEqual(p["mtm"], (p["current_price"] - p["entry_price"]) * p["qty"] * sign, places=2)
            self.assertAlmostEqual(p["current_price"], snapshot["candles"][p["symbol"]][-1]["close"], places=2)
        for t in snapshot["trades"]:
            expected = (t["exit_price"] - t["entry_price"]) * t["qty"] * (1 if t["direction"] == "LONG" else -1)
            self.assertAlmostEqual(t["pnl"], expected, places=2)

    def test_production_data_and_broken_references_fail_closed(self):
        snapshot = load_snapshot()
        wrong = copy.deepcopy(snapshot)
        wrong["mode"] = "production"
        with self.assertRaises(ValueError):
            validate_snapshot(wrong)
        wrong = copy.deepcopy(snapshot)
        wrong["reviews"][0]["trade_id"] = "unknown"
        with self.assertRaises(ValueError):
            validate_snapshot(wrong)

    def test_missing_signal_fields_and_invalid_timestamps_rejected(self):
        snapshot = load_snapshot()
        wrong = copy.deepcopy(snapshot)
        del wrong["signals"]["30M"][0]["volume_qualified"]
        with self.assertRaises(ValueError):
            validate_snapshot(wrong)
        wrong = copy.deepcopy(snapshot)
        wrong["signals"]["30M"][0]["candle_datetime"] = "not a date"
        with self.assertRaises(ValueError):
            validate_snapshot(wrong)

    def test_participation_uses_qualification(self):
        self.assertEqual(participation(pd.DataFrame({"volume_qualified": ["YES", "NO", "YES", "NO"]})), 50)
        self.assertEqual(participation(pd.DataFrame()), 0)

    def test_app_contains_no_writer_or_trading_imports(self):
        for filename in ("Ldashboard_V9.py", "dashboard_data.py", "snapshot_reader.py", "snapshot_views.py",
                         "drive_reader.py"):
            tree = ast.parse((ROOT / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("Lorder", "Lexecution", "Lmarket_data", "Lanalytics")))
                if isinstance(node, ast.Call):
                    name = ast.unparse(node.func)
                    self.assertNotIn(name.split(".")[-1], {"to_sql", "write_text", "write_bytes", "execute_trade", "create_trade", "close_trade", "save_daily_snapshot"})
                    # CSV export is in memory only; no file destination is allowed.
                    if name.endswith(".to_csv"):
                        self.assertFalse(node.args)


class InterfaceTests(unittest.TestCase):
    def test_all_nine_views_filters_and_reload_are_read_only(self):
        before = hashlib.sha256(DEMO_PATH.read_bytes()).hexdigest()
        app = AppTest.from_file(str(ROOT / "Ldashboard_V9.py"), default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("DEMO DATA" in w.value for w in app.warning))
        baseline = [m.value for m in app.metric[:6]]
        for view in NAV_OPTIONS:
            app.radio(key="dashboard_view").set_value(view).run()
            self.assertFalse(app.exception, view)
            self.assertEqual([m.value for m in app.metric[:6]], baseline, view)
        for view, timeframe in zip(NAV_OPTIONS[:3], ["30M", "1H", "1D"]):
            app.radio(key="dashboard_view").set_value(view).run()
            app.selectbox(key=f"window_{timeframe}").select("Earlier signals").run()
            app.selectbox(key=f"direction_{timeframe}").select("SHORT").run()
            self.assertFalse(app.exception, view)
        app.radio(key="dashboard_view").set_value("Historical Cache").run()
        app.selectbox[1].select("Signals").run()
        app.selectbox[0].select("1D").run()
        self.assertFalse(app.exception)
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(before, hashlib.sha256(DEMO_PATH.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
