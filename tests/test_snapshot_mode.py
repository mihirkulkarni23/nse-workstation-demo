"""uploaded_snapshot mode tests. Snapshots are built from synthetic fixtures by the real importer."""

import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
import unittest
from unittest import mock

import pandas as pd
from streamlit.testing.v1 import AppTest

from dashboard_data import NAV_OPTIONS, configured_mode
from producer.import_csv import run_import
from snapshot_reader import (
    SnapshotError, journal_stats, load_uploaded_snapshot, pnl_curve_stats, portfolio_stats, resolve_snapshot_path,
)
from snapshot_views import valid_risk
from test_importer import synthetic_rows, write_fixture

ROOT = Path(__file__).resolve().parents[1]


class SnapshotFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tmp = Path(cls._tmp.name)
        write_fixture(cls.tmp / "source", synthetic_rows(cls.tmp / "marker"))
        cls.private = cls.tmp / "private"
        cls.db = Path(run_import(cls.tmp / "source", cls.private)["snapshot_path"])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def mutated(self, sql: str) -> Path:
        target = Path(tempfile.mkdtemp(dir=self.tmp)) / "dashboard_snapshot.sqlite"
        shutil.copy(self.db, target)
        conn = sqlite3.connect(target)
        try:
            conn.executescript(sql)
            conn.commit()
        finally:
            conn.close()
        return target


class ModeTests(unittest.TestCase):
    def test_mode_is_explicit_and_typos_are_errors(self):
        self.assertEqual(configured_mode({}), ("demo", None))
        self.assertEqual(configured_mode({"NSE_DATA_MODE": "uploaded_snapshot", "NSE_SNAPSHOT_PATH": "x"}),
                         ("uploaded_snapshot", "x"))
        with self.assertRaisesRegex(ValueError, "NSE_SNAPSHOT_PATH"):
            configured_mode({"NSE_DATA_MODE": "uploaded_snapshot"})
        with self.assertRaisesRegex(ValueError, "Unknown NSE_DATA_MODE"):
            configured_mode({"NSE_DATA_MODE": "production"})


class ReaderTests(SnapshotFixture):
    def test_loads_features_and_rows(self):
        before = self.db.read_bytes()
        snap = load_uploaded_snapshot(self.private)
        self.assertEqual(Path(snap["path"]), self.db)
        for name in ("signals_archive", "factor_research", "app_positions", "app_closed_trades", "recorded_pnl"):
            self.assertEqual(snap["features"][name]["status"], "available", name)
        for name in ("quotes", "candles", "broker_book", "original_strategy", "backtests", "reviews"):
            self.assertEqual(snap["features"][name]["status"], "unavailable", name)
        self.assertEqual((len(snap["signals"]), len(snap["positions"]), len(snap["trades"]), len(snap["pnl"])),
                         (7, 2, 2, 2))
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(snap["signals"].candle_dt))
        hourly = snap["signals"][snap["signals"].dataset.eq("hourly_signal_scan")]
        self.assertIn("stop_non_positive:stop_price", hourly.warnings.iloc[1])
        self.assertNotIn("timezone_unverified", " ".join(snap["signals"].quality_flags))
        self.assertEqual(self.db.read_bytes(), before)

    def test_resolves_newest_completed_generation_only(self):
        root = Path(tempfile.mkdtemp(dir=self.tmp))
        for name in ("20200101T000000Z-aaaaaaaa", "20200102T000000Z-bbbbbbbb", ".incomplete-20200103T000000Z-cccccccc"):
            (root / "snapshots" / name).mkdir(parents=True)
            (root / "snapshots" / name / "dashboard_snapshot.sqlite").write_bytes(b"")
        self.assertEqual(resolve_snapshot_path(root).parent.name, "20200102T000000Z-bbbbbbbb")
        with self.assertRaisesRegex(SnapshotError, "No completed snapshot"):
            resolve_snapshot_path(Path(tempfile.mkdtemp(dir=self.tmp)))

    def test_structural_damage_fails_closed(self):
        garbage = Path(tempfile.mkdtemp(dir=self.tmp)) / "dashboard_snapshot.sqlite"
        garbage.write_bytes(b"not a database" * 100)
        cases = {
            "not found": self.tmp / "missing.sqlite",
            "not a readable snapshot": garbage,
            "schema version 7": self.mutated("PRAGMA user_version = 7;"),
            "uploaded_snapshot": self.mutated("UPDATE publication_meta SET value_json = '\"demo\"' WHERE key = 'mode';"),
            "lacks generation_id": self.mutated("DELETE FROM publication_meta WHERE key = 'generation_id';"),
            "data_quality is missing": self.mutated("DROP TABLE data_quality;"),
        }
        for message, path in cases.items():
            with self.assertRaisesRegex(SnapshotError, message):
                load_uploaded_snapshot(path)

    def test_damaged_feature_is_invalid_while_others_stay_available(self):
        snap = load_uploaded_snapshot(self.mutated("DELETE FROM app_open_positions WHERE source_row = 2;"
                                                   "DROP TABLE app_closed_trades;"))
        self.assertEqual(snap["features"]["app_positions"]["status"], "invalid")
        self.assertIn("1 rows present but 2 recorded", snap["features"]["app_positions"]["reason"])
        self.assertEqual(snap["features"]["app_closed_trades"]["status"], "invalid")
        self.assertIn("app_closed_trades is missing", snap["features"]["app_closed_trades"]["reason"])
        self.assertEqual(snap["features"]["signals_archive"]["status"], "available")

    def test_accounting_totals(self):
        snap = load_uploaded_snapshot(self.db)
        journal = journal_stats(snap["trades"])
        self.assertEqual({k: journal[k] for k in ("count", "pnl", "winners", "losers", "r_flagged", "undated")},
                         {"count": 2, "pnl": 50.0, "winners": 1, "losers": 1, "r_flagged": 1, "undated": 2})
        self.assertAlmostEqual(journal["win_rate"], 50.0)
        self.assertAlmostEqual(journal["profit_factor"], 2.0)
        self.assertAlmostEqual(journal["expectancy"], 25.0)
        self.assertAlmostEqual(journal["average_r"], 0.25)
        portfolio = portfolio_stats(snap["positions"])
        self.assertEqual(portfolio, {"count": 2, "long": 1, "short": 1, "capital": 2000.0, "risk_valid": 100.0,
                                     "risk_excluded": 0, "undated": 1})
        curve = pnl_curve_stats(snap["pnl"])
        self.assertEqual((curve["days"], curve["latest"], curve["peak"], curve["identity_failures"]),
                         (2, -30.25, 25.5, 0))
        self.assertAlmostEqual(curve["max_decline"], 55.75)
        self.assertEqual(curve["trough_date"], "2020-01-07")

    def test_empty_inputs_give_unknown_not_zero(self):
        snap = load_uploaded_snapshot(self.db)
        empty = journal_stats(snap["trades"].iloc[0:0])
        self.assertEqual((empty["pnl"], empty["win_rate"], empty["average_r"], empty["profit_factor"]),
                         (None, None, None, None))
        self.assertIsNone(pnl_curve_stats(snap["pnl"].assign(date=None)))
        self.assertIsNone(portfolio_stats(snap["positions"].assign(capital_used=float("nan")))["capital"])

    def test_risk_is_hidden_for_invalid_stops(self):
        frame = pd.DataFrame({"stop_status": ["valid", "invalid_non_positive", "missing"],
                              "risk_amount": [50.0, 70.0, 80.0], "risk_per_share": [5.0, 7.0, 8.0]})
        shown = valid_risk(frame)
        self.assertEqual(shown.risk_amount.tolist()[0], 50.0)
        self.assertTrue(shown.risk_amount.iloc[1:].isna().all() and shown.risk_per_share.iloc[1:].isna().all())
        self.assertEqual(frame.risk_amount.tolist(), [50.0, 70.0, 80.0])


def page_text(app: AppTest) -> str:
    parts = []
    for group in (app.title, app.subheader, app.markdown, app.caption, app.info, app.warning, app.error):
        parts += [str(element.value) for element in group]
    parts += [f"{m.label}: {m.value}" for m in app.metric]
    return "\n".join(parts)


class SnapshotAppTests(SnapshotFixture):
    def start(self, path, mode="uploaded_snapshot") -> AppTest:
        patcher = mock.patch.dict("os.environ", {"NSE_DATA_MODE": mode, "NSE_SNAPSHOT_PATH": str(path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return AppTest.from_file(str(ROOT / "Ldashboard_V9.py"), default_timeout=60).run()

    def view(self, app: AppTest, name: str) -> AppTest:
        app.radio(key="dashboard_view").set_value(name).run()
        self.assertFalse(app.exception, name)
        return app

    def metric(self, app: AppTest, label: str) -> str:
        return next(m.value for m in app.metric if m.label == label)

    def test_all_nine_views_render_from_snapshot_with_no_demo_content(self):
        app = self.start(self.private)
        self.assertFalse(app.exception)
        self.assertTrue(any("UPLOADED SNAPSHOT" in i.value for i in app.info))
        header = [(m.label, m.value) for m in app.metric[:6]]
        self.assertEqual(dict(header)["Open MTM"], "Unavailable")
        self.assertEqual(dict(header)["Source status"], "SNAPSHOT")
        for name in NAV_OPTIONS:
            self.view(app, name)
            self.assertEqual([(m.label, m.value) for m in app.metric[:6]], header, name)
            text = page_text(app)
            self.assertNotIn("DEMO DATA", text, name)
            self.assertNotIn("Synthetic", text, name)
        for name in ("Historical Cache", "Trade Reviews"):
            self.view(app, name)
            self.assertIn("ORIGINAL DATA NOT SUPPLIED", page_text(app))
            self.assertFalse(app.dataframe, name)
        self.view(app, "System Health")
        self.assertIn(self.db.parent.name, page_text(app))

    def test_signal_filters(self):
        app = self.view(self.start(self.private), "30M Scanner")
        self.assertEqual(len(app.dataframe[0].value), 1)
        app.selectbox(key="window_30M").select("All signals").run()
        self.assertEqual(len(app.dataframe[0].value), 2)
        app.selectbox(key="direction_30M").select("LONG").run()
        self.assertEqual(app.dataframe[0].value["Direction"].tolist(), ["LONG"])
        app.selectbox(key="window_30M").select("Earlier signals").run()
        self.assertEqual(len(app.dataframe[0].value), 1)
        self.view(app, "Daily Signals")
        app.selectbox(key="window_1D").select("All signals").run()
        app.date_input(key="dates_1D").set_value((date(2020, 1, 7), date(2020, 1, 7))).run()
        self.assertEqual(len(app.dataframe[0].value), 1)
        self.assertIn("derived from candle time", page_text(app))
        app.selectbox(key="grade_1D").select("A").run()
        self.assertEqual(app.dataframe[0].value["Grade"].tolist(), ["A"])

    def test_invalid_stops_and_missing_fields_are_explicit(self):
        app = self.view(self.start(self.private), "Hourly Signals")
        app.selectbox(key="window_1H").select("All signals").run()
        table = app.dataframe[0].value
        self.assertIn("invalid_non_positive", table["Stop status"].tolist())
        self.assertIn("1 rows have an invalid or unverified recorded stop", page_text(app))
        self.assertIn("outside the original Hourly loader", page_text(app))
        self.assertNotIn("Score", table.columns)
        self.view(app, "30M Scanner")
        self.assertNotIn("DI+", app.dataframe[0].value.columns)
        self.assertIn("Not supplied in this file", page_text(app))
        self.assertIn("DI+", page_text(app))
        self.assertIn("Supplied factor research", page_text(app))

    def test_damaged_timestamps_are_shown_as_unknown(self):
        app = self.view(self.start(self.private), "Portfolio")
        self.assertIn("1 of 2 entry times are time fragments", page_text(app))
        self.assertIn("Date unknown (raw '18:00.8')", app.dataframe[0].value["Entry time"].tolist())
        app.selectbox(key="positions_dated").select("Date unknown").run()
        self.assertEqual(app.dataframe[0].value["Trade ID"].tolist(), ["2"])
        self.view(app, "Journal")
        self.assertIn("2 of 2 trades have no calendar date", page_text(app))
        self.assertTrue(all(v.startswith("Date unknown") for v in app.dataframe[0].value["Exit time"]))

    def test_accounting_totals_are_displayed_from_source(self):
        app = self.view(self.start(self.private), "Journal")
        self.assertEqual(self.metric(app, "Reported P&L"), "₹50.00")
        self.assertEqual(self.metric(app, "Win rate"), "50.0%")
        self.assertEqual(self.metric(app, "R checks flagged"), "1")
        app.selectbox(key="journal_outcome").select("Losers").run()
        self.assertEqual(self.metric(app, "Reported P&L"), "-₹50.00")
        self.view(app, "Portfolio")
        self.assertEqual(self.metric(app, "Capital (recorded)"), "₹2,000.00")
        self.assertEqual(self.metric(app, "Open MTM"), "Unavailable")
        self.view(app, "Analytics")
        self.assertEqual(self.metric(app, "Latest recorded cumulative P&L"), "-₹30.25")
        self.assertEqual(self.metric(app, "Largest peak-to-trough decline"), "₹55.75")
        self.assertEqual(self.metric(app, "Reported P&L (sum)"), "₹50.00")
        text = page_text(app)
        self.assertIn("not funded account equity", text)
        self.assertNotIn("Account equity", text)

    def test_failed_loads_never_show_synthetic_figures(self):
        garbage = Path(tempfile.mkdtemp(dir=self.tmp)) / "dashboard_snapshot.sqlite"
        garbage.write_bytes(b"x" * 4096)
        for path, mode, message in ((self.tmp / "nowhere", "uploaded_snapshot", "UPLOADED SNAPSHOT UNAVAILABLE"),
                                    (garbage, "uploaded_snapshot", "UPLOADED SNAPSHOT UNAVAILABLE"),
                                    (self.private, "production", "DATA SOURCE NOT CONFIGURED")):
            app = self.start(path, mode)
            self.assertFalse(app.exception)
            self.assertTrue(any(message in e.value for e in app.error), message)
            self.assertEqual(len(app.metric), 0)
            self.assertEqual(len(app.dataframe), 0)
            self.assertNotIn("DEMO DATA", page_text(app))

    def test_invalid_feature_is_reported_in_its_view(self):
        app = self.view(self.start(self.mutated("DELETE FROM app_open_positions WHERE source_row = 2;")), "Portfolio")
        self.assertTrue(any("INVALID" in e.value for e in app.error))
        self.assertFalse(app.dataframe)
        self.assertEqual(self.metric(app, "Recorded open positions"), "Unavailable")
        self.view(app, "30M Scanner")
        self.assertTrue(app.dataframe)


if __name__ == "__main__":
    unittest.main()
